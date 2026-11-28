// Preloaded only by the Brave stdio server. Its tools all use the same HTTP client.
import {createHash} from 'node:crypto';
import {mkdirSync} from 'node:fs';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {setTimeout as sleep} from 'node:timers/promises';

const maxRetries = 3;
const maxWaitMs = 30_000;
let db;

function quotaStore() {
    if (!db) {
        const dir = join(homedir(), '.local', 'state', 'reginald');
        mkdirSync(dir, {recursive: true, mode: 0o700});
        const connection = new DatabaseSync(join(dir, 'brave-rate-limits.sqlite'));
        connection.exec(`
            PRAGMA busy_timeout = 5000;
            CREATE TABLE IF NOT EXISTS quota (
                subscription TEXT PRIMARY KEY, spacing_ms INTEGER NOT NULL, next_ms INTEGER NOT NULL
            );
        `);
        db = connection;
    }
    return db;
}

function integers(headers, name) {
    return (headers.get('x-ratelimit-' + name) ?? '').split(',').map(value => {
        const n = Number(value.trim());
        return value.trim() && Number.isSafeInteger(n) && n >= 0 ? n : undefined;
    });
}

function readQuota(response, attempt) {
    const {headers} = response;
    const limits = integers(headers, 'limit');
    const remaining = integers(headers, 'remaining');
    const resets = integers(headers, 'reset');
    const policies = (headers.get('x-ratelimit-policy') ?? '').split(',').map(
        value => /^\s*(\d+)\s*;\s*w=(\d+)\s*$/.exec(value)
    );
    const burst = policies.some(Boolean) ? policies.findIndex(p => p && Number(p[2]) === 1) : 0;
    const limitAt = i => limits[i] ?? (policies[i] ? Number(policies[i][1]) : undefined);
    const rate = limitAt(burst);
    let delay = 0;
    for (const [i, seconds] of resets.entries()) {
        if (remaining[i] === 0 && limitAt(i) !== 0 && seconds !== undefined) {
            delay = Math.max(delay, seconds);
        }
    }
    if (response.status === 429) {
        // With a partial header set only the burst reset is safe to infer.
        delay = Math.max(delay, 2 ** attempt, remaining[burst] == null ? resets[burst] ?? 0 : 0);
    }
    return {spacing: rate > 0 ? Math.ceil(1000 / rate) : null, delayMs: delay * 1000};
}

async function reserve(store, key, signal) {
    // Atomically reserve a start time across processes. Never hold a lock during I/O.
    for (;;) {
        signal?.throwIfAborted();
        const sent = Date.now();
        const reserved = store.prepare(`
            INSERT INTO quota VALUES (?, 1000, ? + 1000)
            ON CONFLICT(subscription) DO UPDATE SET next_ms = ? + spacing_ms WHERE next_ms <= ?
        `).run(key, sent, sent, sent);
        if (reserved.changes) return sent;
        const {next_ms} = store.prepare('SELECT next_ms FROM quota WHERE subscription = ?').get(key);
        const wait = next_ms - Date.now();
        if (wait > maxWaitMs) {
            throw new Error(`Brave Search rate limit reached. Retry after ${Math.ceil(wait / 1000)} seconds.`);
        }
        if (wait > 0) await sleep(wait, undefined, {signal});
    }
}

const upstreamFetch = globalThis.fetch;
globalThis.fetch = async function braveFetch(input, init) {
    const request = input instanceof Request ? input : undefined;
    const url = new URL(request?.url ?? input);
    if (url.origin !== 'https://api.search.brave.com') {
        return upstreamFetch(input, init);
    }
    const headers = new Headers(init?.headers ?? request?.headers);
    const token = headers.get('x-subscription-token') ?? process.env.BRAVE_API_KEY ?? '';
    const key = createHash('sha256').update(token).digest('hex');
    const store = quotaStore();
    const signal = init?.signal ?? request?.signal;
    for (let attempt = 0; ; attempt++) {
        const sent = await reserve(store, key, signal);

        const response = await upstreamFetch(input, init);
        const {spacing, delayMs} = readQuota(response, attempt);
        store.prepare(`
            UPDATE quota SET spacing_ms = coalesce(?, spacing_ms),
            next_ms = max(next_ms, ? + coalesce(?, spacing_ms), ?) WHERE subscription = ?
        `).run(spacing, sent, spacing, Date.now() + delayMs, key);
        if (response.status !== 429 || attempt === maxRetries) return response;
        await response.body?.cancel();
    }
};

// This preload stands in for Brave's HTTP endpoint. Neither process can reach the network.
import assert from 'node:assert/strict';
import {syncBuiltinESMExports} from 'node:module';
import timers from 'node:timers/promises';

let now = 1_900_000_000_000;
Date.now = () => now;
// Yield to concurrent callers before advancing the clock to this request's wake-up time.
timers.setTimeout = ms => new Promise(resolve => {
    const deadline = now + ms;
    setImmediate(() => {
        now = Math.max(now, deadline);
        resolve();
    });
});
syncBuiltinESMExports();
const requests = [];
let replies = [];
globalThis.fetch = async (url, options) => {
    requests.push({url, options, time: now});
    assert.ok(replies.length, 'unexpected HTTP request');
    return replies.shift();
};

const search = 'https://api.search.brave.com/res/v1/web/search?q=test';
const options = {headers: {'X-Subscription-Token': 'offline'}};
const call = () => fetch(search, options);
const limited = () => new Response('Brave 429', {status: 429});
const quotas = (remaining, reset, limit = '5, 15000') => ({
    'X-RateLimit-Limit': limit, 'X-RateLimit-Policy': `${limit.split(', ')[0]};w=1, ${limit.split(', ')[1]};w=2592000`,
    'X-RateLimit-Remaining': remaining, 'X-RateLimit-Reset': reset,
});

globalThis.exerciseBrave = async scenario => {
    if (scenario === 'restart') {
        await assert.rejects(call, /rate limit.*retry/i);
        assert.equal(requests.length, 0);
        const result = new Response('ok');
        replies = [result];
        assert.equal(await fetch(search, {headers: {'X-Subscription-Token': 'other-key'}}), result);
        now += 91_000_000;
        replies = [result];
        assert.equal(await call(), result);
        return;
    }
    const result = new Response('complete result URL?x=1#fragment', {
        headers: quotas('4, 14999', '1, 90000'),
    });
    replies = [limited(), result];
    assert.equal((await call()).status, 200);
    assert.equal(requests[1].time - requests[0].time, 1000);
    assert.equal(requests[1].url, search);
    assert.equal(requests[1].options, options);

    // Learn the five-per-second plan and pace a concurrent burst.
    replies = [result, result, result, result];
    await Promise.all(Array.from({length: 4}, call));
    const times = requests.slice(-4).map(request => request.time);
    for (let i = 1; i < times.length; i++) assert.ok(times[i] - times[i - 1] >= 200);
    assert.ok(times.at(-1) - times[0] < 1000);

    const forbidden = new Response('Forbidden', {status: 403});
    replies = [forbidden];
    assert.equal(await call(), forbidden);
    replies = [new Response('', {status: 429, headers: quotas('0, 15', '6, 90000')}), result];
    const post = {method: 'POST', body: '{"stream":true}', headers: options.headers};
    const answers = 'https://api.search.brave.com/res/v1/chat/completions';
    assert.equal(await fetch(answers, post), result);
    assert.equal(requests.at(-1).time - requests.at(-2).time, 6000);
    assert.equal(requests.at(-1).options, post);

    const finalFailure = limited();
    replies = [limited(), limited(), limited(), finalFailure];
    assert.equal(await call(), finalFailure);
    const retries = requests.slice(-4).map(request => request.time);
    assert.deepEqual(retries.slice(1).map((time, i) => time - retries[i]), [1000, 2000, 4000]);

    // An unlimited monthly quota can report zero. An exhausted finite quota must survive restart.
    replies = [new Response('', {headers: quotas('0, 0', '2, 90000', '5, 0')}),
               new Response('', {headers: quotas('3, 0', '1, 90000')})];
    await call();
    await call();
    assert.equal(requests.at(-1).time - requests.at(-2).time, 2000);
    await assert.rejects(call, /rate limit.*retry/i);
    replies = [result];
    assert.equal(await fetch('https://other.example/search'), result);
};

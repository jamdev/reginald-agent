from sqlalchemy import select

from reginald.db.schema import (
    ContentEvent,
    Delivery,
    Escalation,
    Run,
    Topic,
)
from reginald.domain.models import AddSubscriber
from reginald.email.base import SendResult
from reginald.publisher.diff import UNROUTED_TOPIC
from reginald.services.outbox import OutboxService


class RecordingProvider:
    def __init__(self, fail: bool = False):
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    def send(self, to, subject, html_body, text_body):
        if self.fail:
            return SendResult(ok=False, error="simulated failure")
        self.sent.append((to, subject))
        return SendResult(ok=True, provider_message_id=f"msg-{len(self.sent)}")


def seed_events(sessions, topic_name: str, count: int = 2):
    with sessions.begin() as session:
        run = Run(task="research-new-developments", status="success")
        session.add(run)
        session.flush()
        topic = session.scalar(select(Topic).where(Topic.name == topic_name))
        for i in range(count):
            session.add(
                ContentEvent(
                    run_id=run.id,
                    topic_id=topic.id,
                    kind="brief_created",
                    file_path=f"Topics/{topic_name}/Briefs/2026-07-0{i + 1} - Dev {i} - UK.md",
                    commit_hash=f"c{i}",
                    title=f"Development {i}",
                    jurisdiction="UK",
                )
            )


def make_outbox(sessions, subscribers, config, provider):
    return OutboxService(sessions, subscribers, provider, config)


def test_drain_sends_one_digest_per_subscriber(sessions, subscribers, config):
    subscribers.add(AddSubscriber(email="a@example.org", topics=["Payments and E-money"]))
    subscribers.add(AddSubscriber(email="b@example.org", mode="all"))
    seed_events(sessions, "Payments and E-money")

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    results = outbox.drain()

    assert len(results) == 2
    assert all(r.status == "sent" for r in results)
    assert {to for to, _ in provider.sent} == {"a@example.org", "b@example.org"}


def test_drain_is_idempotent(sessions, subscribers, config):
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_events(sessions, "Payments and E-money")

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    outbox.drain()
    second = outbox.drain()

    assert second == []  # nothing new to send
    assert len(provider.sent) == 1


def test_topic_filtering(sessions, subscribers, config):
    subscribers.add(
        AddSubscriber(email="crypto@example.org", topics=["Crypto-assets and Digital Assets"])
    )
    seed_events(sessions, "Payments and E-money")

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    assert outbox.drain() == []
    assert provider.sent == []


def test_failed_send_stays_pending_and_retries(sessions, subscribers, config):
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_events(sessions, "Payments and E-money")

    failing = RecordingProvider(fail=True)
    outbox = make_outbox(sessions, subscribers, config, failing)
    results = outbox.drain()
    assert results[0].status == "failed"

    # retry with a working provider — same delivery row, no duplicate events
    working = RecordingProvider()
    outbox2 = make_outbox(sessions, subscribers, config, working)
    retried = outbox2.retry()
    assert retried[0].status == "sent"
    assert len(working.sent) == 1
    with sessions() as session:
        assert len(session.scalars(select(Delivery)).all()) == 1


def test_allowlist_rail(sessions, subscribers, config):
    config.email.allowlist = ["only@example.org"]
    subscribers.add(AddSubscriber(email="stranger@example.org", mode="all"))
    seed_events(sessions, "Payments and E-money")

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    results = outbox.drain()
    assert results[0].status == "failed"
    assert "allowlist" in results[0].error
    assert provider.sent == []


def test_digest_links_each_note_to_the_published_site(sessions, subscribers, config):
    config.site.base_url = "https://regdev.agenticgc.uk"
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_events(sessions, "Payments and E-money", count=1)

    outbox = make_outbox(sessions, subscribers, config, RecordingProvider())
    with sessions() as session:
        digest = outbox._digest_for(session, session.scalars(select(ContentEvent)).all())
        html, text = outbox.render(digest)

    url = (
        "https://regdev.agenticgc.uk/Topics/Payments%20and%20E-money/Briefs/"
        "2026-07-01%20-%20Dev%200%20-%20UK"
    )
    assert f'<a href="{url}"' in html
    assert url in text
    assert ".md" not in text  # the vault path is never shown raw
    for body in (html, text):
        assert "unsubscribe" not in body.lower()
        assert "manage subscription" not in body.lower()
        assert "?token=" not in body


def test_digest_falls_back_to_unlinked_titles_without_a_base_url(
    sessions, subscribers, config
):
    """An unpublished vault must not produce hrefs pointing at nothing."""
    assert config.site.base_url == ""
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_events(sessions, "Payments and E-money", count=1)

    outbox = make_outbox(sessions, subscribers, config, RecordingProvider())
    with sessions() as session:
        digest = outbox._digest_for(session, session.scalars(select(ContentEvent)).all())
        html, text = outbox.render(digest)

    assert "<a href" not in html.split("<hr")[0]  # footer links are not content
    assert "Development 0" in html
    assert "Topics/Payments and E-money/Briefs/2026-07-01 - Dev 0 - UK.md" in text


def test_preview_renders_without_sending(sessions, subscribers, config):
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_events(sessions, "Payments and E-money")

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    text = outbox.preview("a@example.org")
    assert "Development 0" in text
    assert provider.sent == []
    with sessions() as session:
        assert session.scalars(select(Delivery)).all() == []


# -- dry run -----------------------------------------------------------------


def test_dry_run_sends_nothing_but_a_later_real_drain_still_does(
    sessions, subscribers, config
):
    """A dry run must not consume events.

    It calls build_pending(), so Delivery and DeliveredEvent rows are written —
    but _attempt() returns before touching status, leaving the delivery
    `pending` for the next real drain to send. Verified because the alternative
    (dry run silently swallowing a digest) would be invisible in production.
    """
    subscribers.add(AddSubscriber(email="a@example.org", topics=["Payments and E-money"]))
    seed_events(sessions, "Payments and E-money", count=2)
    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)

    dry = outbox.drain(dry_run=True)
    assert provider.sent == []                      # nothing left the building
    assert [d.status for d in dry] == ["pending"]   # still queued, not consumed

    with sessions() as session:
        delivery_ids = [d.id for d in session.scalars(select(Delivery)).all()]
    assert len(delivery_ids) == 1                   # build_pending did write a row

    real = outbox.drain()
    assert len(provider.sent) == 1                  # the queued digest went out
    assert real[0].id == delivery_ids[0]            # the SAME delivery, not a new one
    assert real[0].status == "sent"
    assert real[0].event_count == 2                 # both events, none lost


def test_repeated_dry_runs_do_not_pile_up_deliveries(sessions, subscribers, config):
    subscribers.add(AddSubscriber(email="a@example.org", topics=["Payments and E-money"]))
    seed_events(sessions, "Payments and E-money", count=2)
    outbox = make_outbox(sessions, subscribers, config, RecordingProvider())

    outbox.drain(dry_run=True)
    outbox.drain(dry_run=True)
    outbox.drain(dry_run=True)

    with sessions() as session:
        assert len(session.scalars(select(Delivery)).all()) == 1


# -- multi-author: prefs, grouping, coalescing -------------------------------


def seed_update(sessions, topic_name: str, note_uuid: str, summary: str,
                author: str = "Chris", commit: str = "u1"):
    with sessions.begin() as session:
        run = Run(task="identify-changes", status="success")
        session.add(run)
        session.flush()
        topic = session.scalar(select(Topic).where(Topic.name == topic_name))
        session.add(
            ContentEvent(
                run_id=run.id, topic_id=topic.id, kind="brief_updated",
                file_path=f"Topics/{topic_name}/Briefs/2026-07-01 - Dev 0 - UK.md",
                commit_hash=commit, note_uuid=note_uuid,
                title="Development 0", jurisdiction="UK",
                author_name=author, author_kind="contributor", summary=summary,
            )
        )


def test_notify_updated_off_filters_update_events(sessions, subscribers, config):
    subscribers.add(AddSubscriber(email="a@example.org", mode="all", notify_updated=False))
    seed_events(sessions, "Payments and E-money", count=1)        # new
    seed_update(sessions, "Payments and E-money", "n-1", "fixed a date")

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    results = outbox.drain()

    assert len(results) == 1
    [(_to, subject)] = provider.sent
    assert subject.endswith("1 new")                              # update filtered out


def test_notify_new_off_still_delivers_updates(sessions, subscribers, config):
    subscribers.add(AddSubscriber(email="a@example.org", mode="all", notify_new=False))
    seed_events(sessions, "Payments and E-money", count=1)
    seed_update(sessions, "Payments and E-money", "n-1", "fixed a date")

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    outbox.drain()

    [(_to, subject)] = provider.sent
    assert subject.endswith("1 changed")


def seed_created(sessions, topic_name: str, note_uuid: str, commit: str = "c-new"):
    with sessions.begin() as session:
        run = Run(task="research-new-developments", status="success")
        session.add(run)
        session.flush()
        topic = session.scalar(select(Topic).where(Topic.name == topic_name))
        session.add(ContentEvent(
            run_id=run.id, topic_id=topic.id, kind="brief_created",
            file_path=f"Topics/{topic_name}/Briefs/2026-07-01 - Dev 0 - UK.md",
            commit_hash=commit, note_uuid=note_uuid, title="Development 0", jurisdiction="UK",
        ))


def seed_escalation(sessions, topic_name: str, title: str, commit: str,
                    note_uuid: str | None = None, file_path: str | None = None,
                    number: int | None = None, priority: str | None = None):
    """An escalation event as recorded during the vault scan: a note *in Escalations/*.
    Nothing filed under Topics/ can produce one."""
    with sessions.begin() as session:
        run = Run(task="research-new-developments", status="success")
        session.add(run)
        session.flush()
        topic = session.scalar(select(Topic).where(Topic.name == topic_name))
        path = file_path or f"Escalations/2026-08-17 - {title}.md"
        session.add(ContentEvent(
            run_id=run.id, topic_id=topic.id, kind="escalation",
            file_path=path, commit_hash=commit, note_uuid=note_uuid, title=title,
            author_name="Reginald", author_kind="reginald",
            summary="research-new-developments",
        ))
        if number is not None or priority is not None:
            session.add(Escalation(
                uuid=note_uuid or f"esc-{commit}", number=number, file_path=path,
                title=title, status="pending", priority=priority,
                raised_by="research-new-developments", topic_id=topic.id,
            ))


def test_escalations_render_in_their_own_section(sessions, subscribers, config):
    """One meaning for Escalation: a note raised in Escalations/. It gets its
    own section, never a marker smuggled onto a brief's line."""
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_escalation(sessions, "Payments and E-money", "Vault gap: BoE fee consultation",
                    commit="c-esc", number=21, priority="high")

    outbox = make_outbox(sessions, subscribers, config, RecordingProvider())
    with sessions() as session:
        esc = session.scalar(select(ContentEvent).where(ContentEvent.kind == "escalation"))
        digest = outbox._digest_for(session, [esc])
        html, text = outbox.render(digest)

    assert digest.new == [] and digest.changed == []
    [item] = digest.escalations
    assert item.number == 21 and item.priority == "high"
    assert item.topics == ["Payments and E-money"]
    assert "Escalations" in text
    assert "ESC-021" in text and "ESC-021" in html
    assert "Vault gap: BoE fee consultation" in text


def test_a_flagged_brief_is_not_an_escalation(sessions, subscribers, config):
    """A brief carrying `flag:` frontmatter is ordinary content. Only the
    Escalations/ folder makes an escalation."""
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_created(sessions, "Payments and E-money", "n-1", commit="c-new")

    outbox = make_outbox(sessions, subscribers, config, RecordingProvider())
    with sessions() as session:
        digest = outbox._digest_for(
            session, session.scalars(select(ContentEvent)).all()
        )
        _html, text = outbox.render(digest)

    assert digest.escalations == []
    assert len(digest.new) == 1
    assert "ESCALATION" not in text.upper()


def _seed_bare_escalation(sessions, commit="e1"):
    with sessions.begin() as session:
        run = Run(task="identify-changes", status="success")
        session.add(run)
        session.flush()
        topic = session.scalar(select(Topic).where(Topic.name == "Payments and E-money"))
        session.add(ContentEvent(
            run_id=run.id, topic_id=topic.id, kind="escalation",
            file_path="Escalations/2026-08-17 - Vault gap.md", commit_hash=commit,
            title="Vault gap: an untracked consultation",
        ))


def test_escalations_deliver_by_default(sessions, subscribers, config):
    """Default (notify_escalations on): a flagged item reaches a subscriber even
    with new/updated both off — escalations are the team's business."""
    subscribers.add(AddSubscriber(
        email="a@example.org", mode="all", notify_new=False, notify_updated=False
    ))
    _seed_bare_escalation(sessions)

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    results = outbox.drain()

    assert len(results) == 1 and results[0].status == "sent"


def test_notify_escalations_off_suppresses_escalations(sessions, subscribers, config):
    """With the toggle off, an escalation-only item is not delivered — it can be
    routed to just the subscriber who owns it."""
    subscribers.add(AddSubscriber(email="a@example.org", mode="all", notify_escalations=False))
    _seed_bare_escalation(sessions)

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)

    assert outbox.drain() == []
    assert provider.sent == []


def test_notify_escalations_off_still_gets_new_and_updated(sessions, subscribers, config):
    """Turning escalations off leaves ordinary new/updated content flowing."""
    subscribers.add(AddSubscriber(email="a@example.org", mode="all", notify_escalations=False))
    seed_events(sessions, "Payments and E-money", count=1)       # new
    seed_update(sessions, "Payments and E-money", "n-1", "fixed a date")  # updated

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    outbox.drain()

    [(_to, subject)] = provider.sent
    assert subject.endswith("1 new, 1 changed")


def test_digest_groups_new_and_changed_with_author_then_summary(
    sessions, subscribers, config
):
    """The changed line reads: title, then 'by <author> — <one-liner>'."""
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_events(sessions, "Payments and E-money", count=1)
    seed_update(sessions, "Payments and E-money", "n-1",
                "Deadline corrected to 14 Aug per FCA page")

    outbox = make_outbox(sessions, subscribers, config, RecordingProvider())
    with sessions() as session:
        digest = outbox._digest_for(session, session.scalars(select(ContentEvent)).all())
        html, text = outbox.render(digest)

    assert "New" in text and "Changed" in text
    assert "by Chris — Deadline corrected to 14 Aug per FCA page" in text
    assert "by Chris" in html and "Deadline corrected to 14 Aug per FCA page" in html


def test_repeated_edits_coalesce_into_one_line(sessions, subscribers, config):
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_update(sessions, "Payments and E-money", "n-1", "first pass", commit="c1")
    seed_update(sessions, "Payments and E-money", "n-1", "second pass", commit="c2")
    seed_update(sessions, "Payments and E-money", "n-1", "final wording", commit="c3")

    outbox = make_outbox(sessions, subscribers, config, RecordingProvider())
    with sessions() as session:
        digest = outbox._digest_for(session, session.scalars(select(ContentEvent)).all())
        _html, text = outbox.render(digest)

    [item] = digest.changed
    assert item.edits == 3
    assert item.summary == "final wording"                 # most recent wins
    assert "(3 edits)" in text
    assert text.count("Development 0") == 1                # one line, not three


def seed_cross_topic(sessions, note_uuid: str, topics: list[str], commit: str = "x1",
                     kind: str = "brief_created"):
    """A note filed under a primary and a secondary topic: one event row per
    topic, which is what routes it to a subscriber of either."""
    with sessions.begin() as session:
        run = Run(task="research-new-developments", status="success")
        session.add(run)
        session.flush()
        for name in topics:
            topic = session.scalar(select(Topic).where(Topic.name == name))
            session.add(ContentEvent(
                run_id=run.id, topic_id=topic.id, kind=kind,
                file_path=f"Topics/{topics[0]}/Briefs/2026-08-20 - Cross-filed - EU.md",
                commit_hash=commit, note_uuid=note_uuid,
                title="Cross-filed development", jurisdiction="EU",
                effective_date="2026-08-20", author_name="Reginald",
                author_kind="reginald",
            ))


def test_a_cross_filed_note_is_one_line_tagged_with_every_topic(
    sessions, subscribers, config
):
    """The repetition bug: a note filed under two topics used to render once
    under each topic heading. It is one line now, carrying both tags."""
    subscribers.add(AddSubscriber(email="a@example.org", mode="all"))
    seed_cross_topic(sessions, "n-x", ["AI and Algorithmic Systems", "Payments and E-money"])

    outbox = make_outbox(sessions, subscribers, config, RecordingProvider())
    with sessions() as session:
        digest = outbox._digest_for(session, session.scalars(select(ContentEvent)).all())
        html, text = outbox.render(digest)

    [item] = digest.new
    assert item.topics == ["AI and Algorithmic Systems", "Payments and E-money"]
    assert item.edits == 1                                  # two rows, one commit
    assert text.count("Cross-filed development") == 1
    assert html.count("Cross-filed development") == 1
    assert "AI and Algorithmic Systems" in text and "Payments and E-money" in text


def test_cross_filed_tags_show_topics_the_subscriber_does_not_follow(
    sessions, subscribers, config
):
    """Tags describe the note, not the subscription: someone following only
    Payments still sees that the note also sits under AI."""
    subscribers.add(AddSubscriber(email="a@example.org", topics=["Payments and E-money"]))
    seed_cross_topic(sessions, "n-x", ["AI and Algorithmic Systems", "Payments and E-money"])

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    outbox.drain()

    assert len(provider.sent) == 1
    with sessions() as session:
        rows = session.scalars(select(ContentEvent)).all()
        payments = [e for e in rows if e.topic.name == "Payments and E-money"]
        digest = outbox._digest_for(session, payments)
    [item] = digest.new
    assert item.topics == ["Payments and E-money"]


# -- unrouted escalations ----------------------------------------------------


def seed_unrouted_escalation(sessions, commit="u-esc"):
    """An escalation raised with no `topic:` — parked in __unrouted__."""
    with sessions.begin() as session:
        run = Run(task="research-new-developments", status="success")
        session.add(run)
        session.flush()
        topic = session.scalar(select(Topic).where(Topic.name == UNROUTED_TOPIC))
        if topic is None:
            topic = Topic(name=UNROUTED_TOPIC, slug="unrouted", active=False)
            session.add(topic)
            session.flush()
        session.add(ContentEvent(
            run_id=run.id, topic_id=topic.id, kind="escalation",
            file_path="Escalations/2026-08-17 - Untopiced vault gap.md",
            commit_hash=commit, title="Vault gap with no topic",
        ))


def test_an_unrouted_escalation_reaches_all_topics_subscribers(
    sessions, subscribers, config
):
    subscribers.add(AddSubscriber(email="all@example.org", mode="all"))
    seed_unrouted_escalation(sessions)

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)
    results = outbox.drain()

    assert len(results) == 1 and results[0].status == "sent"
    [(to, subject)] = provider.sent
    assert to == "all@example.org"
    assert subject.endswith("1 escalation")


def test_an_unrouted_escalation_skips_selected_topic_subscribers(
    sessions, subscribers, config
):
    """A per-topic subscriber has not opted into everything, so an escalation
    with no topic is not theirs."""
    subscribers.add(AddSubscriber(email="some@example.org", topics=["Payments and E-money"]))
    seed_unrouted_escalation(sessions)

    provider = RecordingProvider()
    outbox = make_outbox(sessions, subscribers, config, provider)

    assert outbox.drain() == []
    assert provider.sent == []


def test_an_unrouted_escalation_renders_without_a_topic_tag(
    sessions, subscribers, config
):
    """__unrouted__ is routing machinery, not a topic — it must never appear
    in the email."""
    subscribers.add(AddSubscriber(email="all@example.org", mode="all"))
    seed_unrouted_escalation(sessions)

    outbox = make_outbox(sessions, subscribers, config, RecordingProvider())
    with sessions() as session:
        digest = outbox._digest_for(session, session.scalars(select(ContentEvent)).all())
        html, text = outbox.render(digest)

    [item] = digest.escalations
    assert item.topics == []
    assert "unrouted" not in text and "unrouted" not in html
    assert "Vault gap with no topic" in text

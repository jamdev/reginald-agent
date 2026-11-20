import pytest

from reginald.db.schema import Topic
from reginald.domain.models import AddSubscriber
from reginald.services.subscribers import SubscriberError


def test_selected_mode(subscribers):
    view = subscribers.add(
        AddSubscriber(email="a@example.org", topics=["Payments and E-money"])
    )
    assert view.mode == "selected"
    assert view.effective_topics == ["Payments and E-money"]


def test_all_mode_with_exclusions(subscribers):
    view = subscribers.add(
        AddSubscriber(email="b@example.org", mode="all", excludes=["AI and Algorithmic Systems"])
    )
    assert view.effective_topics == [
        "Crypto-assets and Digital Assets",
        "Payments and E-money",
    ]
    assert view.excluded_topics == ["AI and Algorithmic Systems"]


def test_all_mode_covers_newly_added_topics(subscribers, topics, sessions, config):
    subscribers.add(AddSubscriber(email="c@example.org", mode="all"))
    # a new topic folder appears in the vault
    (config.vault_path / "Topics" / "Financial Crime, AML and CTF" / "Briefs").mkdir(parents=True)
    topics.sync()
    view = subscribers.get("c@example.org")
    assert "Financial Crime, AML and CTF" in view.effective_topics


def test_unknown_topic_rejected(subscribers):
    with pytest.raises(SubscriberError, match="no such topic"):
        subscribers.add(AddSubscriber(email="d@example.org", topics=["Nonexistent"]))


def test_duplicate_email_rejected(subscribers):
    subscribers.add(AddSubscriber(email="e@example.org", mode="all"))
    with pytest.raises(SubscriberError, match="already exists"):
        subscribers.add(AddSubscriber(email="e@example.org", mode="all"))


def test_inactive_topics_not_in_effective_set(subscribers, sessions):
    subscribers.add(AddSubscriber(email="g@example.org", mode="all"))
    with sessions.begin() as session:
        from sqlalchemy import select

        topic = session.scalar(select(Topic).where(Topic.name == "Payments and E-money"))
        topic.active = False
    view = subscribers.get("g@example.org")
    assert "Payments and E-money" not in view.effective_topics

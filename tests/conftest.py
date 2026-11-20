from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from reginald.core.config import Config
from reginald.db.session import init_db, make_session_factory
from reginald.services.subscribers import SubscriberService
from reginald.services.topics import TopicService

TOPICS = [
    "AI and Algorithmic Systems",
    "Payments and E-money",
    "Crypto-assets and Digital Assets",
]


def make_topic_folder(vault, name: str) -> None:
    (vault / "Topics" / name / "Briefs").mkdir(parents=True)
    (vault / "Topics" / name / "Index.md").write_text(
        f"---\ntitle: {name}\ntype: index\n---\n\n# {name}\n"
    )


@pytest.fixture
def config(tmp_path) -> Config:
    vault = tmp_path / "vault"
    for name in TOPICS:
        make_topic_folder(vault, name)
    return Config(
        env="dev",
        vault_path=vault,
        db_path=tmp_path / "test.db",
    )


@pytest.fixture
def config_with_logfire(config) -> Config:
    config.logfire.enabled = True
    config.logfire.base_url = "https://telemetry.example.invalid"
    return config


@pytest.fixture
def sessions(config) -> sessionmaker[Session]:
    init_db(config.db_path)
    return make_session_factory(config.db_path)


@pytest.fixture
def topics(sessions, config) -> TopicService:
    service = TopicService(sessions, config)
    service.sync()
    return service


@pytest.fixture
def subscribers(sessions, topics) -> SubscriberService:
    return SubscriberService(sessions, topics)


@pytest.fixture
def telemetry(monkeypatch):
    from reginald.core import observability

    logfire = pytest.importorskip("logfire")
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    spans = InMemorySpanExporter()
    metrics = InMemoryMetricReader()
    instance = logfire.configure(
        local=True, send_to_logfire=False, console=False, inspect_arguments=False,
        additional_span_processors=[SimpleSpanProcessor(spans)],
        metrics=logfire.MetricsOptions(additional_readers=[metrics]),
    )
    monkeypatch.setattr(observability, "_logfire", instance)
    return instance, spans, metrics

"""Fixtures shared by the web tests that need an app, a store and a stand-in embedder."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from flask.testing import FlaskClient

from jobscan import db
from jobscan.store import Store
from jobscan.web import create_app
from tests.fakes import BagEmbedder


class Harness:
    """An app over an in-memory store, with a scripted embedder in place of Gemini."""

    def __init__(self, store: Store, embedder: BagEmbedder) -> None:
        self.store = store
        self.embedder = embedder
        self.client = create_app(
            store, secret_key="test", make_embedder=lambda _config: embedder
        ).test_client()


@pytest.fixture
def store() -> Iterator[Store]:
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield Store(conn)
    conn.close()


@pytest.fixture
def embedder() -> BagEmbedder:
    return BagEmbedder()


@pytest.fixture
def web(store: Store, embedder: BagEmbedder) -> Harness:
    return Harness(store, embedder)


@pytest.fixture
def client(web: Harness) -> FlaskClient:
    return web.client


@pytest.fixture(autouse=True)
def no_langfuse_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test must never reach the real Langfuse, even when the keys are set in this terminal."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from jll_harvester.classify import SignalClassifier
from jll_harvester.config import GeocoderMode, Settings
from jll_harvester.db.repository import UpsertOutcome
from jll_harvester.gazetteer import Gazetteer
from jll_harvester.geocode import GazetteerGeocoder, LocationResolver
from jll_harvester.models import Article, Issue, Signal
from jll_harvester.parsing import parse_article

FIXTURES = Path(__file__).parent / "fixtures"
HTML = FIXTURES / "html"
BASE = "https://www.jll.com/de-de/newsroom/"


def load_article(slug: str) -> Article:
    return parse_article((HTML / f"{slug}.html").read_text(encoding="utf-8"), BASE + slug)


@pytest.fixture(scope="session")
def gazetteer() -> Gazetteer:
    return Gazetteer.bundled()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=None,
        geocoder=GeocoderMode.OFFLINE,
        raw_snapshot_dir=tmp_path / "raw",
        request_delay_seconds=0,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def classifier() -> SignalClassifier:
    return SignalClassifier()


@pytest.fixture
def offline_resolver(gazetteer: Gazetteer) -> LocationResolver:
    return LocationResolver(gazetteer, [GazetteerGeocoder(gazetteer)])


# ---------------------------------------------------------------------------------------------
# In-memory repository with the same contract as db.Repository (for fast pipeline unit tests)
# ---------------------------------------------------------------------------------------------
class FakeConn:
    def __init__(self) -> None:
        self.commits = 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        pass


class FakeRepository:
    def __init__(self) -> None:
        self.conn = FakeConn()
        self.runs: dict[uuid.UUID, dict[str, Any]] = {}
        self.issues: list[Issue] = []
        self.documents: dict[str, dict[str, Any]] = {}
        self.signals: dict[tuple[str, str], dict[str, Any]] = {}
        self.locked = False

    def acquire_harvest_lock(self) -> None:
        assert not self.locked
        self.locked = True

    def release_harvest_lock(self) -> None:
        self.locked = False

    def start_run(self, **kwargs: Any) -> uuid.UUID:
        run_id = uuid.uuid4()
        self.runs[run_id] = {"status": "running", **kwargs}
        return run_id

    def finish_run(self, run_id: uuid.UUID, **kwargs: Any) -> None:
        self.runs[run_id].update(kwargs)

    def record_source_check(self, run_id: uuid.UUID, source_check: dict[str, Any]) -> None:
        self.runs[run_id]["source_check"] = source_check

    def add_issues(self, run_id: uuid.UUID, issues: list[Issue]) -> None:
        self.issues.extend(issues)

    def known_documents(self, source_system: str) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.documents.items() if v["source_system"] == source_system}

    def upsert_document(self, **kwargs: Any) -> None:
        self.documents[kwargs["source_record_id"]] = {**self.documents.get(kwargs["source_record_id"], {}), **kwargs}

    def upsert_signal(self, signal: Signal, run_id: uuid.UUID) -> UpsertOutcome:
        key = (signal.source_system, signal.source_record_id)
        current = self.signals.get(key)
        if current is None:
            self.signals[key] = {"signal": signal, "version": 1, "retracted": None}
            return UpsertOutcome.INSERTED
        if current["signal"].content_hash == signal.content_hash and current["retracted"] is None:
            return UpsertOutcome.UNCHANGED
        self.signals[key] = {"signal": signal, "version": current["version"] + 1, "retracted": None}
        return UpsertOutcome.UPDATED

    def retract_signal(self, source_system: str, source_record_id: str, reason: str) -> bool:
        current = self.signals.get((source_system, source_record_id))
        if current and current["retracted"] is None:
            current["retracted"] = reason
            return True
        return False


@pytest.fixture
def fake_repo() -> FakeRepository:
    return FakeRepository()


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.getenv("IRIS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("IRIS_TEST_DATABASE_URL not set — PostGIS integration tests skipped")
    return url

"""Knowledge pages, scheduler, restore, entity merge helpers and the usage signal."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from src.knowledge import build_page
from src.main import app
from src.routes_workspace import clean_aliases, heads, usage_from_events
from src.scheduler import is_due
from src.ui_common import Viewer

NOW = datetime.now(UTC)


def _node(stub, *, weight=0.7, domains=None, days=1, version=1, scope="user"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        stub=stub,
        content=stub,
        weight=weight,
        domains=domains,
        updated_at=NOW - timedelta(days=days),
        created_at=NOW - timedelta(days=days),
        version=version,
        scope=scope,
    )


def test_knowledge_page_groups_folds_and_cites():
    a = _node("Prefers Podman over Docker", weight=0.9, domains=["tooling"])
    b = _node("Uses Podman for local containers", weight=0.6, domains=["tooling"])
    c = _node("Deploys to OpenShift", domains=["deploy"], days=100)
    d = _node("Likes short standups", domains=None, days=3)
    e1 = {"id": "e1", "name": "Podman", "type": "object"}
    page = build_page(
        [a, b, c, d],
        entities={a.id: [e1], b.id: [e1]},
        contradictions={c.id: 2},
        same=[(a.id, b.id, 0.95)],
        previous={"facts_index": {"gone": "Old fact"}, "generated_at": "yesterday"},
        fade_days=60,
    )
    assert page["source_count"] == 4
    assert page["summary"]["facts"] == 3 and page["summary"]["folded"] == 1
    tooling = next(s for s in page["sections"] if s["title"] == "tooling")
    fact = tooling["facts"][0]
    assert fact["text"] == "Prefers Podman over Docker" and fact["similar"] == 1
    assert set(fact["memory_ids"]) == {str(a.id), str(b.id)}
    assert page["sections"][-1]["title"] == "General"
    assert page["entities"][0] == {**e1, "count": 2}
    assert page["questions"][0]["memory_id"] == str(c.id)
    assert any(f["memory_id"] == str(c.id) for f in page["stale"])
    assert all(h["memory_id"] != str(c.id) for h in page["highlights"])  # contradicted facts are not highlights
    assert page["changes"]["removed"] == [{"memory_id": "gone", "text": "Old fact"}]
    assert len(page["changes"]["added"]) == 3


def test_schedule_due():
    now = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    assert is_due("nightly", "02:00", None, now)
    assert is_due("nightly", "02:00", now - timedelta(days=1), now)
    assert not is_due("nightly", "02:00", now.replace(hour=3), now)
    assert not is_due("nightly", "23:00", now - timedelta(hours=5), now)
    assert not is_due("weekly", "02:00", now - timedelta(days=3), now)
    assert is_due("weekly", "02:00", now - timedelta(days=8), now)
    assert not is_due("off", "02:00", None, now)


def test_restore_makes_newest_versions_current():
    v1 = SimpleNamespace(id=1, previous_version_id=None)
    v2 = SimpleNamespace(id=2, previous_version_id=1)
    branch = SimpleNamespace(id=3, previous_version_id=None)
    assert heads([v1, v2, branch]) == {2, 3}


def test_aliases_are_deduplicated_and_skip_the_name():
    assert clean_aliases(["Podman", " podman ", "pdm", "", "PDM", "Containers"], "podman") == ["pdm", "Containers"]


def test_usage_signal():
    t = NOW
    events = [
        ("m1", "agent", "memory.search", t),
        ("m1", "agent", "memory.read", t + timedelta(minutes=2)),
        ("m1", "agent", "memory.search", t + timedelta(hours=1)),
        ("m2", "agent", "memory.search", t),
        ("m2", "agent", "memory.read", t + timedelta(hours=2)),
        ("m3", "agent", "memory.search", t),
    ]
    usage = usage_from_events(events, full_text_ids={"m3"})
    assert usage["m1"]["used"] == 1 and usage["m1"]["skipped"] == 1 and usage["m1"]["use_rate"] == 0.5
    assert usage["m2"]["used"] == 0 and usage["m2"]["skipped"] == 1
    assert usage["m3"]["full"] == 1 and usage["m3"]["use_rate"] is None


@pytest.mark.asyncio
async def test_admins_get_no_knowledge_or_entity_pages(monkeypatch):
    admin = Viewer(username="dk", name="dk", email=None, source="account", role="admin")
    monkeypatch.setattr("src.access.viewer_from_request", lambda request, settings: admin)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/knowledge")).status_code == 403
        assert (await client.get("/api/knowledge/me/x")).status_code == 403
        assert (await client.get(f"/api/entities/{uuid.uuid4()}")).status_code == 403
        assert (
            await client.post(f"/api/entities/{uuid.uuid4()}/merge", json={"into": str(uuid.uuid4())})
        ).status_code == 403

"""Shared helpers for the dashboard (redesign) endpoints in ``ui_routes``.

Everything here is tenant-scoped by ``Settings.ui_tenant_id`` like the rest
of the BFF. Helpers never raise for an unreachable optional component; they
report it so the UI can show a degraded or "not configured" state instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request
from memoryhub_core.models.memory import MemoryNode
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings

logger = logging.getLogger(__name__)

SCOPES = ["user", "project", "campaign", "role", "organizational", "enterprise"]
WRITE_EVENTS = ("memory.write", "memory.update", "memory.delete", "memory.relationship_created")
SEARCH_EVENTS = ("memory.search",)
REVIEW_EVENT_PREFIX = "review."


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_uuid(value: str, what: str = "ID") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail=f"Invalid {what}: {value!r}") from None


def live_filters(tenant_id: str, *, current: bool = True) -> list:
    """Base predicates for memories an operator can see in the dashboard."""
    filters = [MemoryNode.tenant_id == tenant_id, MemoryNode.deleted_at.is_(None)]
    if current:
        filters.append(MemoryNode.is_current.is_(True))
    return filters


def clean_stub(stub: str | None) -> str:
    """Drop the "[scope=…, weight=…]" suffix the core service appends to stubs."""
    return re.sub(r"\s*\[scope=[^\]]*\]?.*$", "", stub or "").strip() or (stub or "")


def preview(content: str | None, limit: int = 280) -> str:
    content = content or ""
    return content if len(content) <= limit else content[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Who is using the dashboard
# ---------------------------------------------------------------------------


@dataclass
class Viewer:
    username: str
    name: str
    email: str | None
    source: str  # "oauth-proxy" | "api-key" | "anonymous"

    @property
    def initials(self) -> str:
        parts = [p for p in self.name.replace(".", " ").replace("-", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


def viewer_from_request(request: Request, settings: Settings) -> Viewer:
    """Identify the human behind the request.

    The OpenShift OAuth proxy in front of the UI forwards the authenticated
    user in ``X-Forwarded-User`` / ``X-Forwarded-Preferred-Username`` /
    ``X-Forwarded-Email``. Development API-key sessions carry the user in the
    signed session cookie.
    """
    headers = request.headers
    username = headers.get("x-forwarded-preferred-username") or headers.get("x-forwarded-user")
    email = headers.get("x-forwarded-email")
    if username:
        return Viewer(username=username, name=username, email=email, source="oauth-proxy")
    session_user = read_session_cookie(request, settings)
    if session_user:
        return Viewer(username=session_user, name=session_user, email=None, source="api-key")
    return Viewer(username="dashboard", name="Dashboard operator", email=None, source="anonymous")


# ---------------------------------------------------------------------------
# Dev API-key session cookie
# ---------------------------------------------------------------------------

SESSION_COOKIE = "memoryhub_ui_session"


def _sign(value: str, key: str) -> str:
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_session_cookie(username: str, settings: Settings) -> str:
    issued = str(int(time.time()))
    payload = f"{username}|{issued}"
    return f"{payload}|{_sign(payload, settings.ui_dev_api_key)}"


def read_session_cookie(request: Request, settings: Settings) -> str | None:
    if not settings.ui_dev_api_key:
        return None
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw or raw.count("|") != 2:
        return None
    username, issued, signature = raw.split("|")
    if not hmac.compare_digest(signature, _sign(f"{username}|{issued}", settings.ui_dev_api_key)):
        return None
    # Sessions last 12 hours.
    try:
        if time.time() - int(issued) > 12 * 3600:
            return None
    except ValueError:
        return None
    return username


# ---------------------------------------------------------------------------
# Audit trail for dashboard actions
# ---------------------------------------------------------------------------


async def audit(
    db: AsyncSession,
    *,
    tenant_id: str,
    viewer: Viewer,
    event_type: str,
    scope: str,
    owner_id: str,
    memory_id: uuid.UUID | None,
    decision: str = "allowed",
    metadata: dict | None = None,
) -> None:
    """Append a dashboard action to ``audit_log`` (append-only table)."""
    from memoryhub_core.models.audit import AuditLog

    db.add(
        AuditLog(
            event_type=event_type,
            actor_id=viewer.username,
            driver_id=viewer.username,
            scope=scope or "session",
            owner_id=owner_id or viewer.username,
            memory_id=memory_id,
            decision=decision,
            event_metadata={"via": "dashboard", **(metadata or {})},
            tenant_id=tenant_id,
        )
    )


# ---------------------------------------------------------------------------
# Embeddings / reranker
# ---------------------------------------------------------------------------


async def embed(text_value: str, embedding_url: str, timeout: float = 5.0) -> list[float] | None:
    if not embedding_url or not text_value:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(embedding_url, json={"inputs": text_value})
            response.raise_for_status()
            data = response.json()
            if not data:
                return None
            return data[0] if isinstance(data[0], list) else data
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("Embedding service unavailable: %s", exc)
        return None


async def rerank(query: str, texts: list[str], reranker_url: str) -> list[float] | None:
    """Score ``texts`` against ``query`` with a TEI-style reranker."""
    if not reranker_url or not texts:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(reranker_url, json={"query": query, "texts": texts})
            response.raise_for_status()
            data = response.json()
        scores = [0.0] * len(texts)
        for item in data:
            scores[int(item["index"])] = float(item["score"])
        return scores
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning("Reranker unavailable: %s", exc)
        return None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Tenant settings (dashboard-owned)
# ---------------------------------------------------------------------------

DEFAULT_TENANT_SETTINGS: dict[str, Any] = {
    "retrieval": {
        "max_results": 10,
        "max_response_tokens": 4000,
        "session_focus_weight": 0.4,
        "full_content_threshold": 0.8,
        "rerank": True,
        "cache_optimized_ordering": True,
        "realtime_push": True,
    },
    "governance": {
        "project_isolation": True,
        "role_isolation": True,
        "contradiction_threshold": 5,
        "enterprise_approvers": 1,
    },
    "audit": {
        "read_detail": "summary",
        "full_retention_days": 90,
        "mcp_profile": "compact",
    },
}

_SETTINGS_TABLE = "memoryhub_ui_settings"
_settings_table_ready = False


async def _ensure_settings_table(db: AsyncSession) -> None:
    global _settings_table_ready
    if _settings_table_ready:
        return
    await db.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {_SETTINGS_TABLE} (
                tenant_id VARCHAR(255) PRIMARY KEY,
                settings JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_by VARCHAR(255)
            )
            """
        )
    )
    await db.commit()
    _settings_table_ready = True


def _merge(defaults: dict, stored: dict) -> dict:
    merged = {}
    for key, value in defaults.items():
        if isinstance(value, dict):
            merged[key] = _merge(value, stored.get(key, {}) if isinstance(stored.get(key), dict) else {})
        else:
            merged[key] = stored.get(key, value)
    return merged


async def load_tenant_settings(db: AsyncSession, tenant_id: str) -> dict[str, Any]:
    """Return tenant settings merged over defaults. Never raises."""
    try:
        await _ensure_settings_table(db)
        row = (
            await db.execute(
                text(f"SELECT settings, updated_at, updated_by FROM {_SETTINGS_TABLE} WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        ).first()
    except Exception as exc:
        logger.warning("Could not read tenant settings, using defaults: %s", exc)
        await db.rollback()
        return {**DEFAULT_TENANT_SETTINGS, "_meta": {"updated_at": None, "updated_by": None, "persisted": False}}
    stored = row[0] if row else {}
    if isinstance(stored, str):
        stored = json.loads(stored)
    merged = _merge(DEFAULT_TENANT_SETTINGS, stored or {})
    merged["_meta"] = {
        "updated_at": row[1].isoformat() if row and row[1] else None,
        "updated_by": row[2] if row else None,
        "persisted": True,
    }
    return merged


async def save_tenant_settings(db: AsyncSession, tenant_id: str, values: dict, updated_by: str) -> None:
    await _ensure_settings_table(db)
    clean = _merge(DEFAULT_TENANT_SETTINGS, values)
    await db.execute(
        text(
            f"""
            INSERT INTO {_SETTINGS_TABLE} (tenant_id, settings, updated_at, updated_by)
            VALUES (:t, CAST(:s AS JSONB), now(), :u)
            ON CONFLICT (tenant_id) DO UPDATE
              SET settings = EXCLUDED.settings, updated_at = now(), updated_by = EXCLUDED.updated_by
            """
        ),
        {"t": tenant_id, "s": json.dumps(clean), "u": updated_by},
    )


# ---------------------------------------------------------------------------
# Component health probes
# ---------------------------------------------------------------------------


@dataclass
class Probe:
    key: str
    name: str
    status: str  # healthy | degraded | down | not_configured
    detail: str | None = None
    latency_ms: int | None = None

    def as_dict(self) -> dict:
        return self.__dict__.copy()


async def _timed_http(method: str, url: str, **kwargs) -> tuple[int | None, int]:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.request(method, url, **kwargs)
        return response.status_code, int((time.perf_counter() - started) * 1000)
    except Exception:
        return None, int((time.perf_counter() - started) * 1000)


async def _tcp_probe(url: str, default_port: int) -> tuple[bool, int]:
    started = time.perf_counter()
    parsed = urlparse(url if "://" in url else f"tcp://{url}")
    host, port = parsed.hostname, parsed.port or default_port
    if not host:
        return False, 0
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=2.0)
        writer.close()
        return True, int((time.perf_counter() - started) * 1000)
    except Exception:
        return False, int((time.perf_counter() - started) * 1000)


def _latency_status(ok: bool, ms: int, slow_ms: int) -> str:
    if not ok:
        return "down"
    return "degraded" if ms >= slow_ms else "healthy"


async def probe_components(db: AsyncSession, settings: Settings) -> list[dict]:
    async def mcp() -> Probe:
        if not settings.mcp_server_url:
            return Probe("mcp", "MCP server", "not_configured")
        code, ms = await _timed_http("GET", settings.mcp_server_url)
        ok = code is not None and code < 500
        return Probe("mcp", "MCP server", _latency_status(ok, ms, 1500), None if ok else "Not responding", ms)

    async def auth() -> Probe:
        if not settings.auth_service_url:
            return Probe("auth", "Auth service (OAuth 2.1)", "not_configured")
        code, ms = await _timed_http("GET", f"{settings.auth_service_url.rstrip('/')}/healthz")
        ok = code is not None and code < 400
        return Probe(
            "auth", "Auth service (OAuth 2.1)", _latency_status(ok, ms, 1500), None if ok else "Not responding", ms
        )

    async def embedding() -> Probe:
        if not settings.embedding_url:
            return Probe("embedding", "Embedding model", "not_configured")
        started = time.perf_counter()
        vector = await embed("health check", settings.embedding_url, timeout=3.0)
        ms = int((time.perf_counter() - started) * 1000)
        return Probe(
            "embedding",
            "Embedding model",
            _latency_status(vector is not None, ms, 300),
            f"{len(vector)} dims" if vector else "Not responding",
            ms,
        )

    async def reranker() -> Probe:
        if not settings.reranker_url:
            return Probe("reranker", "Cross-encoder reranker", "not_configured")
        started = time.perf_counter()
        scores = await rerank("health check", ["health check"], settings.reranker_url)
        ms = int((time.perf_counter() - started) * 1000)
        return Probe(
            "reranker",
            "Cross-encoder reranker",
            _latency_status(scores is not None, ms, 300),
            None if scores is not None else "Not responding",
            ms,
        )

    async def valkey() -> Probe:
        if not settings.valkey_url:
            return Probe("valkey", "Valkey (push + jobs)", "not_configured")
        ok, ms = await _tcp_probe(settings.valkey_url, 6379)
        return Probe(
            "valkey", "Valkey (push + jobs)", _latency_status(ok, ms, 500), None if ok else "Not reachable", ms
        )

    async def minio() -> Probe:
        if not settings.s3_endpoint:
            return Probe("minio", "MinIO object storage", "not_configured")
        endpoint = settings.s3_endpoint if "://" in settings.s3_endpoint else f"http://{settings.s3_endpoint}"
        code, ms = await _timed_http("GET", f"{endpoint.rstrip('/')}/minio/health/live")
        ok = code is not None and code < 500
        return Probe(
            "minio", "MinIO object storage", _latency_status(ok, ms, 1000), None if ok else "Not reachable", ms
        )

    started = time.perf_counter()
    pg = Probe("postgres", "PostgreSQL + pgvector", "healthy")
    try:
        has_vector = (await db.execute(text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'"))).scalar_one()
        pg.latency_ms = int((time.perf_counter() - started) * 1000)
        if not has_vector:
            pg.status, pg.detail = "degraded", "pgvector extension missing"
    except Exception:
        await db.rollback()
        pg.status, pg.detail = "down", "Query failed"

    others = await asyncio.gather(mcp(), auth(), embedding(), reranker(), valkey(), minio())
    ordered = [others[0], others[1], pg, others[2], others[3], others[4], others[5]]
    return [p.as_dict() for p in ordered]

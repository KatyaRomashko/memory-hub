"""Tests for write_memory tool."""

import inspect

import pytest
from fastmcp.exceptions import ToolError

import src.tools.auth as auth_mod
from src.tools.write_memory import write_memory


def test_write_memory_is_decorated():
    """Verify write_memory is a decorated MCP tool."""
    assert hasattr(write_memory, "__fastmcp__"), (
        "write_memory should be decorated with @mcp.tool"
    )


def test_write_memory_is_async():
    """The tool function must be async."""
    assert inspect.iscoroutinefunction(write_memory)


def test_write_memory_has_required_parameters():
    """Verify the function signature includes all expected parameters."""
    sig = inspect.signature(write_memory)
    param_names = set(sig.parameters.keys())

    required = {"content", "scope", "owner_id"}
    assert required.issubset(param_names), (
        f"Missing required params: {required - param_names}"
    )

    optional = {"weight", "parent_id", "branch_type", "metadata", "ctx", "force"}
    assert optional.issubset(param_names), (
        f"Missing optional params: {optional - param_names}"
    )


def test_write_memory_default_values():
    """Verify default values for optional parameters."""
    sig = inspect.signature(write_memory)
    params = sig.parameters

    assert params["weight"].default == 0.7
    assert params["parent_id"].default is None
    assert params["branch_type"].default is None
    assert params["metadata"].default is None
    assert params["force"].default is False


@pytest.mark.asyncio
async def test_write_memory_rejects_orphan_branch():
    """Regression for #48: branch_type without parent_id is invalid.

    A branch must attach to a parent memory. The previous validation only
    rejected the inverse (parent_id without branch_type), accepting orphans
    silently and producing memory nodes that claimed to be branches but had
    no parent.
    """
    auth_mod._current_session = {
        "user_id": "wjackson",
        "scopes": ["user"],
        "identity_type": "user",
    }
    try:
        with pytest.raises(ToolError) as exc_info:
            await write_memory(
                content="orphan branch attempt",
                scope="user",
                branch_type="rationale",
            )
    finally:
        auth_mod._current_session = None

    assert "parent_id is required" in str(exc_info.value), str(exc_info.value)
    assert "branch_type" in str(exc_info.value), str(exc_info.value)


@pytest.mark.asyncio
async def test_write_memory_still_rejects_parent_without_branch_type():
    """The inverse guard must still fire: parent_id without branch_type is invalid."""
    import uuid as _uuid

    auth_mod._current_session = {
        "user_id": "wjackson",
        "scopes": ["user"],
        "identity_type": "user",
    }
    try:
        with pytest.raises(ToolError) as exc_info:
            await write_memory(
                content="branch with no type",
                scope="user",
                parent_id=str(_uuid.uuid4()),
            )
    finally:
        auth_mod._current_session = None

    assert "branch_type is required" in str(exc_info.value), str(exc_info.value)


@pytest.mark.asyncio
async def test_write_memory_forwards_tenant_id_to_service():
    """Phase 3 (#46): the tool must forward the caller's tenant_id from
    claims into create_memory so the persisted row carries the correct
    tenant rather than falling through to the column server_default."""
    import datetime as _dt
    import uuid as _uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from memoryhub_core.models.schemas import MemoryNodeRead, MemoryScope, StorageType

    fake_node = MemoryNodeRead(
        id=_uuid.uuid4(),
        parent_id=None,
        content="hello",
        stub="hello",
        storage_type=StorageType.INLINE,
        content_ref=None,
        weight=0.7,
        scope=MemoryScope.USER,
        branch_type=None,
        owner_id="wjackson",
        tenant_id="tenant_a",
        is_current=True,
        version=1,
        previous_version_id=None,
        metadata=None,
        created_at=_dt.datetime.now(_dt.UTC),
        updated_at=_dt.datetime.now(_dt.UTC),
        expires_at=None,
        has_children=False,
        has_rationale=False,
        branch_count=0,
        content_type="experiential",
    )
    fake_curation = {
        "blocked": False,
        "reason": None,
        "detail": None,
        "similar_count": 0,
        "nearest_id": None,
        "nearest_score": None,
        "flags": [],
    }

    mock_session = MagicMock()
    mock_gen = AsyncMock()
    fake_claims = {
        "sub": "wjackson",
        "identity_type": "user",
        "tenant_id": "tenant_a",
        "scopes": ["memory:write:user", "memory:read:user"],
    }

    with (
        patch(
            "src.tools.write_memory.get_claims_from_context",
            return_value=fake_claims,
        ),
        patch(
            "src.tools.write_memory.get_db_session",
            return_value=(mock_session, mock_gen),
        ),
        patch(
            "src.tools.write_memory.release_db_session",
            new_callable=AsyncMock,
        ),
        patch(
            "src.tools.write_memory.get_embedding_service",
            return_value=MagicMock(max_tokens=8192),
        ),
        patch(
            "src.tools.write_memory.create_memory",
            new_callable=AsyncMock,
            return_value=(fake_node, fake_curation),
        ) as mock_create_memory,
        patch(
            "src.tools.write_memory.broadcast_after_write",
            new_callable=AsyncMock,
        ),
    ):
        result = await write_memory(
            content="hello",
            scope="user",
            owner_id="wjackson",
            content_type="experiential",
        )

    assert "error" not in result or result.get("error") is not True
    assert result["memory"]["content_type"] == "experiential"
    _, kwargs = mock_create_memory.call_args
    assert kwargs.get("tenant_id") == "tenant_a", (
        f"Expected tenant_id='tenant_a' forwarded from claims into create_memory, "
        f"got kwargs={kwargs}"
    )


@pytest.mark.asyncio
async def test_write_memory_gated_returns_structured_response():
    """Near-duplicate gate returns structured response, not ToolError."""
    import uuid as _uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    fake_curation = {
        "blocked": True,
        "gated": True,
        "reason": "near_duplicate",
        "detail": "Memory is 92% similar to existing memory abc",
        "similar_count": 1,
        "nearest_id": _uuid.uuid4(),
        "nearest_score": 0.92,
        "existing_memory_id": "abc-123",
        "existing_memory_stub": "Existing memory text.",
        "recommendation": "update_existing",
        "flags": [],
    }
    fake_claims = {
        "sub": "wjackson",
        "identity_type": "user",
        "tenant_id": "default",
        "scopes": ["memory:write:user", "memory:read:user"],
    }

    with (
        patch(
            "src.tools.write_memory.get_claims_from_context", return_value=fake_claims
        ),
        patch(
            "src.tools.write_memory.get_db_session",
            return_value=(MagicMock(), AsyncMock()),
        ),
        patch("src.tools.write_memory.release_db_session", new_callable=AsyncMock),
        patch("src.tools.write_memory.get_embedding_service", return_value=MagicMock(max_tokens=8192)),
        patch(
            "src.tools.write_memory.create_memory",
            new_callable=AsyncMock,
            return_value=(None, fake_curation),
        ),
        patch(
            "src.tools.write_memory._get_cache_impact",
            new_callable=AsyncMock,
            return_value={"compiled_count": 28, "will_recompile": True},
        ),
    ):
        result = await write_memory(content="near-dup content", scope="user", content_type="experiential")

    assert result["memory"] is None
    assert result["curation"]["gated"] is True
    assert result["curation"]["reason"] == "near_duplicate"
    assert result["curation"]["existing_memory_id"] == "abc-123"
    assert result["curation"]["existing_memory_stub"] == "Existing memory text."
    assert result["curation"]["cache_impact"]["compiled_count"] == 28


@pytest.mark.asyncio
async def test_write_memory_regex_block_still_raises_tool_error():
    """Hard blocks (regex: secrets) still raise ToolError, not structured response."""
    from unittest.mock import AsyncMock, MagicMock, patch

    fake_curation = {
        "blocked": True,
        "reason": "secrets_scan",
        "detail": "AWS access key detected",
        "similar_count": 0,
        "nearest_id": None,
        "nearest_score": None,
        "flags": [],
    }
    fake_claims = {
        "sub": "wjackson",
        "identity_type": "user",
        "tenant_id": "default",
        "scopes": ["memory:write:user", "memory:read:user"],
    }

    with (
        patch(
            "src.tools.write_memory.get_claims_from_context", return_value=fake_claims
        ),
        patch(
            "src.tools.write_memory.get_db_session",
            return_value=(MagicMock(), AsyncMock()),
        ),
        patch("src.tools.write_memory.release_db_session", new_callable=AsyncMock),
        patch("src.tools.write_memory.get_embedding_service", return_value=MagicMock(max_tokens=8192)),
        patch(
            "src.tools.write_memory.create_memory",
            new_callable=AsyncMock,
            return_value=(None, fake_curation),
        ),
        pytest.raises(ToolError, match="Curation rule blocked write: secrets_scan"),
    ):
        await write_memory(content="AKIAIOSFODNN7EXAMPLE", scope="user", content_type="experiential")


@pytest.mark.asyncio
async def test_write_memory_force_forwarded_to_create_memory():
    """force=True is forwarded to create_memory."""
    import datetime as _dt
    import uuid as _uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from memoryhub_core.models.schemas import MemoryNodeRead, MemoryScope, StorageType

    fake_node = MemoryNodeRead(
        id=_uuid.uuid4(),
        parent_id=None,
        content="forced write",
        stub="forced write",
        storage_type=StorageType.INLINE,
        content_ref=None,
        weight=0.7,
        scope=MemoryScope.USER,
        branch_type=None,
        owner_id="wjackson",
        tenant_id="default",
        is_current=True,
        version=1,
        previous_version_id=None,
        metadata=None,
        created_at=_dt.datetime.now(_dt.UTC),
        updated_at=_dt.datetime.now(_dt.UTC),
        expires_at=None,
        has_children=False,
        has_rationale=False,
        branch_count=0,
        content_type="experiential",
    )
    fake_curation = {
        "blocked": False,
        "reason": None,
        "detail": None,
        "similar_count": 1,
        "nearest_id": _uuid.uuid4(),
        "nearest_score": 0.92,
        "flags": ["possible_duplicate"],
    }
    fake_claims = {
        "sub": "wjackson",
        "identity_type": "user",
        "tenant_id": "default",
        "scopes": ["memory:write:user", "memory:read:user"],
    }

    with (
        patch(
            "src.tools.write_memory.get_claims_from_context", return_value=fake_claims
        ),
        patch(
            "src.tools.write_memory.get_db_session",
            return_value=(MagicMock(), AsyncMock()),
        ),
        patch("src.tools.write_memory.release_db_session", new_callable=AsyncMock),
        patch("src.tools.write_memory.get_embedding_service", return_value=MagicMock(max_tokens=8192)),
        patch(
            "src.tools.write_memory.create_memory",
            new_callable=AsyncMock,
            return_value=(fake_node, fake_curation),
        ) as mock_create,
        patch("src.tools.write_memory.broadcast_after_write", new_callable=AsyncMock),
    ):
        result = await write_memory(content="forced write", scope="user", force=True, content_type="experiential")

    assert result["memory"] is not None
    assert result["memory"]["content_type"] == "experiential"
    _, kwargs = mock_create.call_args
    assert kwargs.get("force") is True


@pytest.mark.asyncio
async def test_write_memory_response_includes_procedural_content_type():
    """#552: write echoes stored content_type so callers can confirm vs the default."""
    import datetime as _dt
    import uuid as _uuid
    from unittest.mock import AsyncMock, MagicMock, patch

    from memoryhub_core.models.schemas import MemoryNodeRead, MemoryScope, StorageType

    fake_node = MemoryNodeRead(
        id=_uuid.uuid4(),
        parent_id=None,
        content="Deploy to staging",
        stub="Deploy to staging",
        storage_type=StorageType.INLINE,
        content_ref=None,
        weight=0.9,
        scope=MemoryScope.USER,
        branch_type=None,
        owner_id="wjackson",
        tenant_id="default",
        is_current=True,
        version=1,
        previous_version_id=None,
        metadata=None,
        created_at=_dt.datetime.now(_dt.UTC),
        updated_at=_dt.datetime.now(_dt.UTC),
        expires_at=None,
        has_children=False,
        has_rationale=False,
        branch_count=0,
        content_type="procedural",
    )
    fake_curation = {
        "blocked": False,
        "reason": None,
        "detail": None,
        "similar_count": 0,
        "nearest_id": None,
        "nearest_score": None,
        "flags": [],
    }
    fake_claims = {
        "sub": "wjackson",
        "identity_type": "user",
        "tenant_id": "default",
        "scopes": ["memory:write:user", "memory:read:user"],
    }

    with (
        patch("src.tools.write_memory.get_claims_from_context", return_value=fake_claims),
        patch(
            "src.tools.write_memory.get_db_session",
            return_value=(MagicMock(), AsyncMock()),
        ),
        patch("src.tools.write_memory.release_db_session", new_callable=AsyncMock),
        patch(
            "src.tools.write_memory.get_embedding_service",
            return_value=MagicMock(max_tokens=8192),
        ),
        patch(
            "src.tools.write_memory.create_memory",
            new_callable=AsyncMock,
            return_value=(fake_node, fake_curation),
        ),
        patch("src.tools.write_memory.broadcast_after_write", new_callable=AsyncMock),
    ):
        result = await write_memory(
            content="Deploy to staging",
            scope="user",
            content_type="procedural",
        )

    assert result["memory"]["content_type"] == "procedural"


def _write_patches(curation, created=None, audit=None):
    """Patch the write path's collaborators; ``audit`` captures audit calls."""
    from unittest.mock import AsyncMock, MagicMock, patch

    claims = {
        "sub": "claude-code-wjackson",
        "identity_type": "user",
        "tenant_id": "default",
        "scopes": ["memory:write:user", "memory:read:user"],
    }
    return [
        patch("src.tools.write_memory.get_claims_from_context", return_value=claims),
        patch("src.tools.write_memory.get_db_session", return_value=(MagicMock(), AsyncMock())),
        patch("src.tools.write_memory.release_db_session", new_callable=AsyncMock),
        patch("src.tools.write_memory.get_embedding_service", return_value=MagicMock(max_tokens=8192)),
        patch("src.tools.write_memory.create_memory", new_callable=AsyncMock, return_value=(created, curation)),
        patch("src.tools.write_memory.record_audit_event", new=audit or AsyncMock()),
        patch("src.tools.write_memory.broadcast_after_write", new_callable=AsyncMock),
        patch("src.tools.write_memory._get_cache_impact", new_callable=AsyncMock, return_value=None),
    ]


@pytest.mark.asyncio
async def test_blocked_write_records_a_denied_event_naming_the_rule():
    """A curation block leaves a denied memory.write event with the rule name, not the content."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock

    audit = AsyncMock()
    curation = {"blocked": True, "reason": "secrets_scan", "detail": "AWS access key AKIA... detected",
                "similar_count": 0, "nearest_id": None, "nearest_score": None, "flags": []}
    with ExitStack() as stack:
        for p in _write_patches(curation, audit=audit):
            stack.enter_context(p)
        with pytest.raises(ToolError):
            await write_memory(content="AKIAIOSFODNN7EXAMPLE", scope="user", content_type="experiential")

    denied = [c.kwargs for c in audit.await_args_list if c.kwargs["decision"] == "denied"]
    assert len(denied) == 1
    assert denied[0]["event_type"] == "memory.write"
    assert denied[0]["memory_id"] is None
    assert denied[0]["metadata"] == {"rule": "secrets_scan", "blocked_by": "curation_rule"}
    assert "AKIA" not in str(denied[0])
    assert not [c for c in audit.await_args_list if c.kwargs["decision"] == "allowed"]


@pytest.mark.asyncio
async def test_similarity_gate_records_a_denied_event_too():
    from contextlib import ExitStack
    from unittest.mock import AsyncMock

    audit = AsyncMock()
    curation = {"blocked": True, "gated": True, "reason": "near_duplicate", "detail": None,
                "similar_count": 1, "nearest_id": None, "nearest_score": 0.97, "flags": []}
    with ExitStack() as stack:
        for p in _write_patches(curation, audit=audit):
            stack.enter_context(p)
        result = await write_memory(content="same as before", scope="user", content_type="experiential")

    assert result["curation"]["gated"] is True
    denied = [c.kwargs for c in audit.await_args_list if c.kwargs["decision"] == "denied"]
    assert denied[0]["metadata"] == {"rule": "near_duplicate", "blocked_by": "similarity_gate"}


@pytest.mark.asyncio
async def test_successful_write_records_the_new_memory_id():
    import uuid
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, MagicMock

    audit = AsyncMock()
    new_id = uuid.uuid4()
    created = MagicMock(id=new_id, upstream_trust_level="trusted")
    created.model_dump.return_value = {"id": str(new_id)}
    curation = {"blocked": False, "reason": None, "detail": None, "similar_count": 0,
                "nearest_id": None, "nearest_score": None, "flags": []}
    with ExitStack() as stack:
        for p in _write_patches(curation, created=created, audit=audit):
            stack.enter_context(p)
        await write_memory(content="a fact", scope="user", content_type="experiential", extract_facts="off")

    allowed = [c.kwargs for c in audit.await_args_list if c.kwargs["decision"] == "allowed"]
    assert [a["memory_id"] for a in allowed] == [new_id]

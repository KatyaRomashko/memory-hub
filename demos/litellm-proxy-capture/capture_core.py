"""Pure, dependency-free logic for proxy-side memory capture (WRIG-1482 PoC).

Nothing in here imports LiteLLM or the MemoryHub SDK, so it can be unit
tested in isolation. The LiteLLM callback (``memoryhub_capture.py``) feeds
this module the ``standard_logging_object`` LiteLLM builds for every call.

Responsibilities:

* normalize OpenAI- and Anthropic-shaped messages into ``(role, text)``;
* derive a *session key* for a request, and record how confident we are
  in it (explicit header vs. LiteLLM session id vs. inferred fingerprint);
* compute the *delta* of a request: chat APIs resend the full history on
  every call, so only messages not captured before should be appended;
* build an ``Observation`` describing what the proxy could and could not
  see. Observations are the research output of the PoC.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any

SESSION_HEADER = "x-memoryhub-session"
ACTOR_HEADER = "x-memoryhub-actor"
PROJECT_HEADER = "x-memoryhub-project"

# Harness-injected blocks that are not part of what the user said
# (Claude Code wraps reminders, hook output, etc. in these tags).
_INJECTED_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

# MemoryHub stores messages above conv_inline_max_bytes (8192) in S3, and the
# dreaming pipeline then sends "[content stored in S3]" to the LLM instead of
# the text. Keep captured messages under that limit so extraction sees them.
DEFAULT_MAX_MESSAGE_BYTES = 8000
TRUNCATION_MARKER = "\n[...truncated by proxy capture]"


def clean_text(text: str) -> str:
    return _INJECTED_RE.sub("", text).strip()


def truncate_bytes(text: str, max_bytes: int) -> str:
    """Trim ``text`` to at most ``max_bytes`` UTF-8 bytes (marker included)."""
    if max_bytes <= 0 or len(text.encode("utf-8")) <= max_bytes:
        return text
    budget = max_bytes - len(TRUNCATION_MARKER.encode("utf-8"))
    head = text.encode("utf-8")[: max(budget, 0)].decode("utf-8", errors="ignore")
    return head + TRUNCATION_MARKER


# ── agent-written memory detection ───────────────────────────────────────
#
# MCP calls never reach the proxy: the agent talks to MemoryHub directly.
# But the *decision* to call them is visible in the LLM traffic, because the
# model answers with a tool_use block and the next request carries its
# result. That is enough to tell "the agent saved this turn itself" from
# "nobody saved anything", without the proxy knowing anything about MCP.

DEFAULT_WRITE_TOOLS = r"write_memory|^memory$|memoryhub"
_TOOL_CALL_RE = re.compile(r"^\[tool_(?:call|use) (?P<name>[^\]]*)\]\s*(?P<args>.*)$", re.DOTALL)
_MEMORY_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def parse_tool_call(msg: Message) -> tuple[str, str] | None:
    """Return (tool name, raw arguments) for a normalized tool_call message."""
    if msg.role != "tool_call":
        return None
    m = _TOOL_CALL_RE.match(msg.content.strip())
    if not m:
        return None
    return m.group("name").strip(), m.group("args").strip()


def is_memory_write(msg: Message, pattern: re.Pattern) -> bool:
    """True when this tool call looks like the agent writing a memory itself.

    A read (``search``/``read``/``list``) does not count: the point is whether
    the agent *stored* something, not whether it used MemoryHub at all.
    """
    parsed = parse_tool_call(msg)
    if parsed is None:
        return False
    name, args = parsed
    if not pattern.search(name or ""):
        return False
    action = re.search(r'"action"\s*:\s*"(?P<a>[a-z_]+)"', args or "")
    if action:
        return action.group("a") in ("write", "update", "checkpoint", "relate")
    # A dedicated tool name (write_memory) carries the intent by itself.
    return "write" in (name or "").lower() or "update" in (name or "").lower()


def find_agent_writes(messages: list[Message], pattern: re.Pattern) -> list[dict]:
    """Memory writes the agent made itself, with the ids their results returned."""
    found: list[dict] = []
    for i, msg in enumerate(messages):
        if not is_memory_write(msg, pattern):
            continue
        entry: dict = {"index": i, "tool": (parse_tool_call(msg) or ("", ""))[0], "memory_id": None}
        for nxt in messages[i + 1: i + 4]:
            if nxt.role == "tool_result":
                found_id = _MEMORY_ID_RE.search(nxt.content)
                if found_id:
                    entry["memory_id"] = found_id.group(0)
                break
        found.append(entry)
    return found


# Roles accepted by MemoryHub's thread(action="append").
_ROLE_MAP = {
    "user": "user",
    "assistant": "assistant",
    "system": "system",
    "developer": "system",
    "tool": "tool_result",
    "function": "tool_result",
}


@dataclass(frozen=True)
class Message:
    role: str  # MemoryHub role: user | assistant | system | tool_call | tool_result
    content: str

    def digest(self) -> str:
        h = hashlib.sha256(f"{self.role}\x00{self.content}".encode())
        return h.hexdigest()[:16]


# ── message normalization ────────────────────────────────────────────────


def _block_text(block: Any) -> tuple[str, str | None]:
    """Return (text, role_override) for one content block."""
    if isinstance(block, str):
        return block, None
    if not isinstance(block, dict):
        return "", None
    btype = block.get("type")
    if btype in ("text", "input_text", "output_text"):
        return str(block.get("text", "")), None
    if btype == "tool_use":  # Anthropic
        args = json.dumps(block.get("input", {}), sort_keys=True)
        return f"[tool_use {block.get('name')}] {args}", "tool_call"
    if btype == "tool_result":  # Anthropic
        inner = block.get("content")
        if isinstance(inner, list):
            inner = "\n".join(_block_text(b)[0] for b in inner)
        return f"[tool_result] {inner if inner is not None else ''}", "tool_result"
    if btype in ("image", "image_url", "document"):
        return f"[{btype} omitted]", None
    if btype in ("thinking", "redacted_thinking"):
        return "", None  # never persist model reasoning
    return "", None


def normalize_message(raw: Any) -> list[Message]:
    """Normalize one OpenAI/Anthropic message into one or more Messages.

    A single Anthropic message can mix text, tool_use and tool_result
    blocks; those are split so tool traffic keeps its own role.
    """
    if not isinstance(raw, dict):
        return []
    role = _ROLE_MAP.get(str(raw.get("role", "")), None)
    if role is None:
        return []
    content = raw.get("content")
    out: list[Message] = []

    if isinstance(content, str):
        content = clean_text(content)
        if content:
            out.append(Message(role, content))
    elif isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            text, override = _block_text(block)
            if not text:
                continue
            if override:
                out.append(Message(override, text))
            else:
                text_parts.append(text)
        joined = clean_text("\n".join(text_parts))
        if joined:
            out.insert(0, Message(role, joined))

    # OpenAI tool calls live next to content, not inside it.
    for call in raw.get("tool_calls") or []:
        fn = (call or {}).get("function", {})
        out.append(Message("tool_call", f"[tool_call {fn.get('name')}] {fn.get('arguments', '')}"))
    return out


def normalize_messages(messages: Any, *, system: Any = None) -> list[Message]:
    out: list[Message] = []
    if system:  # Anthropic top-level system prompt
        if isinstance(system, list):
            system = "\n".join(_block_text(b)[0] for b in system)
        out.append(Message("system", str(system)))
    if isinstance(messages, dict):  # some call types log {"messages": [...]}
        system = messages.get("system")
        messages = messages.get("messages", [])
        if system:
            return normalize_messages(messages, system=system)
    for raw in messages or []:
        out.extend(normalize_message(raw))
    return out


def response_messages(response: Any) -> list[Message]:
    """Extract the assistant turn from a logged response (OpenAI or Anthropic)."""
    if isinstance(response, str):
        return [Message("assistant", response)] if response.strip() else []
    if not isinstance(response, dict):
        return []
    if "choices" in response:  # OpenAI chat completion
        choices = response.get("choices") or []
        if not choices:
            return []
        msg = choices[0].get("message") or {}
        msg = {**msg, "role": "assistant"}
        return normalize_message(msg)
    if response.get("type") == "message" or "content" in response:  # Anthropic
        return normalize_message({"role": "assistant", "content": response.get("content")})
    if "output" in response:  # OpenAI Responses API
        out: list[Message] = []
        for item in response.get("output") or []:
            if item.get("type") == "message":
                out.extend(normalize_message({"role": "assistant", "content": item.get("content")}))
        return out
    return []


# ── session identity ─────────────────────────────────────────────────────


@dataclass
class SessionInfo:
    key: str
    source: str  # header | litellm_session | end_user_fingerprint | fingerprint
    actor_id: str | None
    actor_source: str | None
    project_id: str | None


def _lower_keys(d: dict | None) -> dict[str, Any]:
    return {str(k).lower(): v for k, v in (d or {}).items()}


def request_headers(slo: dict, kwargs: dict | None = None) -> dict[str, Any]:
    """Collect client headers from wherever LiteLLM put them."""
    headers: dict[str, Any] = {}
    meta = slo.get("metadata") or {}
    headers.update(_lower_keys(meta.get("requester_custom_headers")))
    lp = (kwargs or {}).get("litellm_params") or {}
    psr = lp.get("proxy_server_request") or {}
    headers.update(_lower_keys(psr.get("headers")))
    headers.update(_lower_keys((lp.get("metadata") or {}).get("headers")))
    headers.pop("authorization", None)
    headers.pop("x-api-key", None)
    return headers


def conversation_fingerprint(messages: list[Message]) -> str:
    """Stable id for a conversation: model-independent hash of the first user message.

    The system prompt is deliberately excluded: harnesses such as Claude
    Code embed volatile data (date, git status) in it, which would split
    one conversation into many. Heuristic only -- two sessions that start
    with the same user message collide, and a client that rewrites the
    first message (compaction) splits a session. Both cases are exactly
    what the PoC is meant to surface.
    """
    first_user = next((m.content for m in messages if m.role == "user"), "")
    return hashlib.sha256(first_user.encode()).hexdigest()[:24]


def transcript(messages: list[Message], max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> list[Message]:
    """The part of a request that is tracked for delta computation.

    System messages are excluded for the same volatility reason as above.
    Content is truncated here (not at append time) so digests of what was
    stored and what is observed later stay comparable.
    """
    return [Message(m.role, truncate_bytes(m.content, max_bytes)) for m in messages if m.role != "system"]


def redact_messages(messages: list[Message], redactor) -> tuple[list[Message], dict[str, int]]:
    """Strip credentials before anything is stored or hashed.

    Applied to the *tracked* transcript, so the same message redacts to the
    same text every time the client resends it and delta tracking is not
    disturbed. ``redactor`` is anything callable returning
    ``(text, {rule: hits})`` -- see ``redact.Redactor``.
    """
    if redactor is None:
        return messages, {}
    out: list[Message] = []
    totals: dict[str, int] = {}
    for m in messages:
        text, hits = redactor(m.content)
        for k, v in hits.items():
            totals[k] = totals.get(k, 0) + v
        out.append(Message(m.role, text) if text != m.content else m)
    return out, totals


def derive_session(slo: dict, messages: list[Message], kwargs: dict | None = None) -> SessionInfo:
    headers = request_headers(slo, kwargs)
    meta = slo.get("metadata") or {}

    actor, actor_source = None, None
    if headers.get(ACTOR_HEADER):
        actor, actor_source = str(headers[ACTOR_HEADER]), "header"
    elif slo.get("end_user"):
        actor, actor_source = str(slo["end_user"]), "end_user"
    elif meta.get("user_api_key_user_id"):
        actor, actor_source = str(meta["user_api_key_user_id"]), "virtual_key_user"

    project = headers.get(PROJECT_HEADER)

    if headers.get(SESSION_HEADER):
        key, source = str(headers[SESSION_HEADER]), "header"
    elif slo.get("session_id") and slo.get("session_id") != slo.get("trace_id"):
        key, source = str(slo["session_id"]), "litellm_session"
    else:
        fp = conversation_fingerprint(messages)
        if actor:
            key, source = f"{actor}:{fp}", "end_user_fingerprint"
        else:
            key, source = fp, "fingerprint"
    return SessionInfo(key, source, actor, actor_source, str(project) if project else None)


# ── delta tracking ───────────────────────────────────────────────────────


@dataclass
class SessionState:
    thread_id: str | None = None
    digests: list[str] = field(default_factory=list)
    user_turns: int = 0
    last_seen: float = field(default_factory=time.time)
    # Turns the agent covered itself since the last extraction, and the ids of
    # the memories it wrote (kept for provenance in the thread metadata).
    agent_writes: int = 0
    agent_memory_ids: list[str] = field(default_factory=list)


@dataclass
class Delta:
    new: list[Message]
    prefix_len: int
    history_rewritten: bool


def compute_delta(state: SessionState, messages: list[Message]) -> Delta:
    """Messages not yet captured for this session.

    ``messages`` is the full transcript as the proxy saw it *including* the
    assistant response. We find the longest common prefix with what was
    captured before; anything after it is new. If the prefix is shorter
    than the captured history the client rewrote history (compaction,
    edit, retry with different context) and we flag it.
    """
    digests = [m.digest() for m in messages]
    prefix = 0
    for a, b in zip(state.digests, digests):
        if a != b:
            break
        prefix += 1
    rewritten = prefix < len(state.digests)
    return Delta(new=messages[prefix:], prefix_len=prefix, history_rewritten=rewritten)


def seed_state(state: SessionState, stored: list[Message]) -> None:
    """Initialise delta tracking from messages already stored in a thread.

    Used when the proxy restarts and re-finds an existing thread: without
    this, the whole resent history would be appended a second time.
    """
    state.digests = [m.digest() for m in stored]


def commit_delta(state: SessionState, messages: list[Message]) -> None:
    state.digests = [m.digest() for m in messages]
    state.last_seen = time.time()


# ── request filtering ────────────────────────────────────────────────────


def should_skip(
    slo: dict,
    messages: list[Message],
    *,
    ignore_models: re.Pattern | None,
) -> str | None:
    """Return a reason string when the call should not be captured."""
    if slo.get("status") not in (None, "success"):
        return "not_success"
    call_type = str(slo.get("call_type") or "")
    if call_type and not any(t in call_type for t in ("completion", "messages", "responses")):
        return f"call_type:{call_type}"
    model = str(slo.get("model_group") or slo.get("model") or "")
    if ignore_models is not None and ignore_models.search(model):
        return f"ignored_model:{model}"
    if not any(m.role == "user" for m in messages):
        return "no_user_message"
    return None


# ── observations (research output) ───────────────────────────────────────


@dataclass
class Observation:
    ts: float
    call_id: str | None
    call_type: str | None
    model: str | None
    stream: bool | None
    user_agent: str | None
    session_key: str
    session_source: str
    actor_id: str | None
    actor_source: str | None
    project_id: str | None
    total_messages: int
    new_messages: int
    new_roles: list[str]
    history_rewritten: bool
    has_tools_declared: bool
    has_tool_traffic: bool
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: float | None
    skipped: str | None = None
    agent_writes: int = 0
    agent_memory_ids: list[str] = field(default_factory=list)
    extract_skipped: str | None = None
    # {rule name: how many values that rule replaced in this request}
    redactions: dict[str, int] = field(default_factory=dict)
    thread_id: str | None = None
    appended: int = 0
    extraction: dict | None = None
    extract_ms: float | None = None
    error: str | None = None
    # "call" for an intercepted LLM call, "extraction" for the background
    # extraction pass it triggered (see MemoryHubCaptureLogger._extract_later)
    event: str = "call"

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)


def build_observation(
    slo: dict,
    session: SessionInfo,
    messages: list[Message],
    delta: Delta | None,
    *,
    skipped: str | None = None,
) -> Observation:
    params = slo.get("model_parameters") or {}
    meta = slo.get("metadata") or {}
    new = delta.new if delta else []
    start, end = slo.get("startTime"), slo.get("endTime")
    return Observation(
        ts=time.time(),
        call_id=slo.get("litellm_call_id") or slo.get("id"),
        call_type=slo.get("call_type"),
        model=slo.get("model_group") or slo.get("model"),
        stream=slo.get("stream"),
        user_agent=meta.get("user_agent") or slo.get("user_agent"),
        session_key=session.key,
        session_source=session.source,
        actor_id=session.actor_id,
        actor_source=session.actor_source,
        project_id=session.project_id,
        total_messages=len(messages),
        new_messages=len(new),
        new_roles=[m.role for m in new],
        history_rewritten=bool(delta and delta.history_rewritten),
        has_tools_declared=bool(params.get("tools")),
        has_tool_traffic=any(m.role in ("tool_call", "tool_result") for m in messages),
        prompt_tokens=slo.get("prompt_tokens"),
        completion_tokens=slo.get("completion_tokens"),
        latency_ms=(end - start) * 1000 if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None,
        skipped=skipped,
    )

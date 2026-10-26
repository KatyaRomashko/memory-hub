"""Procedural graph extraction from dreaming (#554).

Detects a multi-step procedure in one conversation session and writes it as a
procedural graph (#552 shape: a ``content_type="procedural"`` root, current
``procedure_step`` children, ``precedes``/``requires``/``alternative_to`` edges,
attributes in ``metadata_.procedure``). Later sessions that cover the same
procedure refine it through ``procedure_evolution`` instead of duplicating it.

Design and signed-off decisions: ``planning/procedural-graph-extraction.md``.

The pass is off by default (``AppSettings.procedural_extraction_mode``) and is
called from ``dreaming.extract_from_thread`` after the fact windows. It never
touches the fact cursor. All graph writes for one run, the provenance row, the
decision-log rows and the procedural cursor commit in one transaction.

Only observable evidence is reported as ``observed``: steps must be backed by
cited messages, and inferred items never reach the fields #553 renders into
guidance prose.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from memoryhub_core.config import AppSettings
from memoryhub_core.models.conversation import (
    ConversationExtraction,
    ConversationMessage,
    ConversationThread,
)
from memoryhub_core.models.memory import MemoryNode
from memoryhub_core.models.reconciliation import (
    ReconciliationDecision as ReconciliationDecisionRow,
)
from memoryhub_core.models.schemas import MemoryNodeCreate, RelationshipCreate
from memoryhub_core.services.curation.pipeline import run_curation_pipeline
from memoryhub_core.services.graph import create_relationship

logger = logging.getLogger(__name__)

MODES: tuple[str, ...] = ("off", "propose", "apply")
_MODE_RANK = {mode: rank for rank, mode in enumerate(MODES)}

#: Thread scopes that may produce procedures. Never organizational,
#: enterprise or role (task constraint); campaign excluded conservatively.
ALLOWED_THREAD_SCOPES = frozenset({"user", "project"})

CREATED_BY = "dreaming:procedural"
EXTRACTED_BY = "procedural_extraction_pipeline"
CURSOR_KEY = "procedural_extraction"

# Decision-log actions (reconciliation_decisions.action is String(20)).
PROC_CREATE = "proc_create"
PROC_MATCH = "proc_match"
PROC_PROPOSE = "proc_propose"
PROC_EDIT = "proc_edit"
PROC_REJECT = "proc_reject"

# Rejection reasons (#555 seam; see record_procedural_decision).
REASONS = frozenset({
    "below_confidence",
    "insufficient_evidence",
    "breaks_entry",
    "hand_authored",
    "cap_reached",
    "duplicate",
    "inferred_only",
    "conflicting_evidence",
    "ambiguous_match",
    "invalid_proposal",
    "curation_blocked",
    "scope_not_allowed",
    "evidence_evicted",
    "propose_mode",
})

_RETRY_DELAYS = [0.0, 30.0, 60.0, 120.0]
_MAX_SERVICE_ATTEMPTS = len(_RETRY_DELAYS)

# Transcript budget per message kind (characters).
_RESULT_HEAD, _RESULT_TAIL = 1500, 500
_CALL_HEAD, _CALL_TAIL = 800, 200
_TEXT_HEAD, _TEXT_TAIL = 3000, 1000

# Text caps for what is written (keeps every node far below the chunking and
# S3 thresholds, which create_memory(commit=False) refuses).
_ROOT_CONTENT_MAX = 1500
_ATTR_PER_FIELD_MAX = 5


# ---------------------------------------------------------------------------
# Mode resolution (D1)
# ---------------------------------------------------------------------------


def _norm_mode(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in _MODE_RANK else None


def resolve_mode(
    server_mode: Any,
    retention_policy: dict[str, Any] | None,
    *,
    caller_mode: str | None = None,
    procedures: bool | None = None,
) -> str:
    """Effective procedural mode for one run.

    The server setting is a ceiling. A thread's ``retention_policy`` override
    and the caller's ``procedure_mode`` can only lower it. ``procedures=False``
    turns the pass off for this call. An unknown server value is treated as
    ``off``; an unknown caller value raises ``ValueError``.
    """
    server = _norm_mode(server_mode)
    if server is None:
        if server_mode not in (None, ""):
            logger.warning("Unknown procedural_extraction_mode %r; treating as off", server_mode)
        server = "off"
    if caller_mode is not None and _norm_mode(caller_mode) is None:
        raise ValueError(
            f"procedure_mode must be one of {', '.join(MODES)}; got {caller_mode!r}"
        )
    if procedures is False:
        return "off"
    candidates = [server]
    if isinstance(retention_policy, dict):
        thread_mode = _norm_mode(retention_policy.get("procedural_extraction_mode"))
        if thread_mode is not None:
            candidates.append(thread_mode)
    if caller_mode is not None:
        candidates.append(_norm_mode(caller_mode))  # type: ignore[arg-type]
    return min(candidates, key=_MODE_RANK.__getitem__)


# ---------------------------------------------------------------------------
# Trajectory (D2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrajectoryEvent:
    """One message as the procedural pass sees it. Never an ORM row.

    Hydrating S3 content into the ORM row would mark it dirty and write tool
    output into the inline content column on the next commit.
    """

    seq: int
    role: str
    content: str
    tool_call_id: str | None = None
    outcome_marker: str | None = None
    available: bool = True


_OUTCOME_MARKERS = frozenset({"success", "failure", "partial"})


async def load_trajectory(
    session: AsyncSession,
    *,
    thread_id: uuid.UUID,
    s3_adapter: Any | None = None,
    turn_range: tuple[int, int] | None = None,
) -> list[TrajectoryEvent]:
    """Load the session's messages as events, hydrating S3-backed content."""
    stmt = select(ConversationMessage).where(ConversationMessage.thread_id == thread_id)
    if turn_range is not None:
        stmt = stmt.where(
            ConversationMessage.sequence_number >= turn_range[0],
            ConversationMessage.sequence_number <= turn_range[1],
        )
    stmt = stmt.order_by(ConversationMessage.sequence_number.asc())
    rows = list((await session.execute(stmt)).scalars().all())

    events: list[TrajectoryEvent] = []
    for msg in rows:
        content = msg.content
        available = content is not None
        if msg.storage_type == "s3" and msg.content_ref:
            available = False
            if s3_adapter is not None:
                try:
                    content = await s3_adapter.get_content(msg.content_ref)
                    available = content is not None
                except Exception as exc:  # pragma: no cover - logged and degraded
                    logger.warning(
                        "Could not hydrate S3 message seq=%s of thread %s: %s",
                        msg.sequence_number, thread_id, exc,
                    )
        marker = None
        metadata = msg.metadata_ if isinstance(msg.metadata_, dict) else {}
        raw_marker = metadata.get("outcome")
        if isinstance(raw_marker, str) and raw_marker.strip().lower() in _OUTCOME_MARKERS:
            marker = raw_marker.strip().lower()
        events.append(
            TrajectoryEvent(
                seq=msg.sequence_number,
                role=(msg.role or "").lower(),
                content=content if available and content is not None else "[content unavailable]",
                tool_call_id=msg.tool_call_id,
                outcome_marker=marker,
                available=available,
            )
        )
    return events


def _clip(text: str, head: int, tail: int) -> str:
    if len(text) <= head + tail + 40:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n[... {omitted} chars omitted ...]\n{text[-tail:]}"


def _render_event(event: TrajectoryEvent) -> str:
    if event.role == "tool_result":
        body = _clip(event.content, _RESULT_HEAD, _RESULT_TAIL)
    elif event.role == "tool_call":
        body = _clip(event.content, _CALL_HEAD, _CALL_TAIL)
    else:
        body = _clip(event.content, _TEXT_HEAD, _TEXT_TAIL)
    header = f"[seq={event.seq}] {event.role.upper()}"
    if event.tool_call_id:
        header += f" (tool_call_id={event.tool_call_id})"
    if event.outcome_marker:
        header += f" [outcome marker: {event.outcome_marker}]"
    return f"{header}\n{body}"


def format_trajectory(
    events: list[TrajectoryEvent], *, max_chars: int,
) -> tuple[str, set[int]]:
    """Render events within ``max_chars``. Returns (text, included sequence numbers).

    When the session does not fit, the first user message (the goal) and the
    most recent messages that fit are kept, with an explicit omission marker.
    Evidence may only cite included sequence numbers.
    """
    blocks = [(event.seq, _render_event(event)) for event in events]
    total = sum(len(block) + 2 for _, block in blocks)
    if total <= max_chars:
        return "\n\n".join(block for _, block in blocks), {seq for seq, _ in blocks}

    head: list[tuple[int, str]] = []
    first_user = next((i for i, event in enumerate(events) if event.role == "user"), None)
    budget = max_chars
    if first_user is not None:
        head = [blocks[first_user]]
        budget -= len(blocks[first_user][1]) + 2
    tail: list[tuple[int, str]] = []
    for index in range(len(blocks) - 1, -1, -1):
        if first_user is not None and index == first_user:
            break
        size = len(blocks[index][1]) + 2
        if size > budget - 80:
            break
        tail.append(blocks[index])
        budget -= size
    tail.reverse()
    kept = head + tail
    if not kept:  # one enormous message: keep a clipped version of the last
        seq, block = blocks[-1]
        kept = [(seq, block[: max(max_chars - 80, 200)])]
    included = {seq for seq, _ in kept}
    omitted = [seq for seq, _ in blocks if seq not in included]
    parts = [block for _, block in head]
    if omitted:
        parts.append(f"[messages {omitted[0]}-{omitted[-1]} omitted: {len(omitted)} messages]")
    parts.extend(block for _, block in tail)
    return "\n\n".join(parts), included


# ---------------------------------------------------------------------------
# Proposal schema (D3)
# ---------------------------------------------------------------------------

EvidenceKind = Literal["observed", "inferred"]


def _clip_str(limit: int) -> Callable[[Any], Any]:
    def _inner(value: Any) -> Any:
        if value is None:
            return value
        return str(value).strip()[:limit]

    return _inner


def _ints(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


Text40 = Annotated[str, BeforeValidator(_clip_str(40)), StringConstraints(min_length=1)]
Text200 = Annotated[str, BeforeValidator(_clip_str(200)), StringConstraints(min_length=1)]
Text300 = Annotated[str, BeforeValidator(_clip_str(300)), StringConstraints(min_length=1)]
Text500 = Annotated[str, BeforeValidator(_clip_str(500)), StringConstraints(min_length=1)]
Text1000 = Annotated[str, BeforeValidator(_clip_str(1000)), StringConstraints(min_length=1)]
Seqs = Annotated[list[int], BeforeValidator(_ints)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EvidencedText(_Strict):
    """A condition, pitfall or guidance item with its own evidence."""

    text: Text500
    evidence: EvidenceKind = "inferred"
    source_messages: Seqs = Field(default_factory=list)


def _coerce_evidenced(value: Any) -> Any:
    # A bare string is accepted but carries no evidence, so it is inferred.
    if isinstance(value, str):
        return {"text": value, "evidence": "inferred", "source_messages": []}
    return value


def _coerce_evidenced_list(value: Any) -> Any:
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return [_coerce_evidenced(item) for item in value[:10]]


EvidencedItem = Annotated[EvidencedText, BeforeValidator(_coerce_evidenced)]
EvidencedList = Annotated[list[EvidencedText], BeforeValidator(_coerce_evidenced_list)]


class StepProposal(_Strict):
    key: Text40
    action: Text300
    tool_ref: Annotated[str | None, BeforeValidator(_clip_str(200))] = None
    preconditions: EvidencedList = Field(default_factory=list)
    postconditions: EvidencedList = Field(default_factory=list)
    pitfalls: EvidencedList = Field(default_factory=list)
    guidance: EvidencedItem | None = None
    status: Literal["succeeded", "failed", "skipped", "unknown"] = "unknown"
    evidence: EvidenceKind = "inferred"
    source_messages: Seqs = Field(default_factory=list)

    @field_validator("tool_ref")
    @classmethod
    def _blank_tool_ref(cls, value: str | None) -> str | None:
        return value or None


class EdgeProposal(_Strict):
    source: Text40
    target: Text40
    type: Literal["precedes", "requires", "alternative_to"]
    condition: EvidencedItem | None = None
    guidance: EvidencedItem | None = None
    pitfalls: EvidencedList = Field(default_factory=list)
    evidence: EvidenceKind = "inferred"
    source_messages: Seqs = Field(default_factory=list)


class EdgeRef(_Strict):
    source: Text40
    target: Text40


class FailureProposal(_Strict):
    step: Annotated[str | None, BeforeValidator(_clip_str(40))] = None
    edge: EdgeRef | None = None
    failure_class: Literal[
        "procedure", "tool_error", "environment", "precondition_unmet", "unknown",
    ] = Field(default="unknown", alias="class")
    evidence: EvidenceKind = "inferred"
    source_messages: Seqs = Field(default_factory=list)


class OutcomeProposal(_Strict):
    result: Literal["success", "failure", "partial", "unknown"] = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: EvidenceKind = "inferred"
    source_messages: Seqs = Field(default_factory=list)
    failure: FailureProposal | None = None


class ProceduralProposal(_Strict):
    procedure_detected: bool
    goal: Annotated[str | None, BeforeValidator(_clip_str(200))] = None
    summary: Annotated[str | None, BeforeValidator(_clip_str(1000))] = None
    steps: list[StepProposal] = Field(default_factory=list, max_length=60)
    edges: list[EdgeProposal] = Field(default_factory=list, max_length=200)
    entry_step: Annotated[str | None, BeforeValidator(_clip_str(40))] = None
    outcome: OutcomeProposal | None = None
    detection_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------
# Prompt + LLM transport
# ---------------------------------------------------------------------------

_prompt_cache: dict[str, Any] = {}


def load_prompts() -> tuple[str, str, str]:
    """Return (system_prompt, tiebreaker_prompt, prompt_hash). Cached."""
    if "prompts" in _prompt_cache:
        return _prompt_cache["prompts"]
    path = Path(__file__).resolve().parents[3] / "prompts" / "procedural_extraction.yaml"
    data = yaml.safe_load(path.read_text())
    system_prompt = data["system_prompt"]
    tiebreaker = data["match_tiebreaker_prompt"]
    prompt_hash = hashlib.sha256(system_prompt.encode()).hexdigest()
    _prompt_cache["prompts"] = (system_prompt, tiebreaker, prompt_hash)
    return _prompt_cache["prompts"]


class ProceduralLLMError(RuntimeError):
    """The extraction endpoint could not be reached or kept failing."""


class ProposalFormatError(ValueError):
    """The model's answer was not a usable proposal after one correction."""


class ProceduralLLM:
    """OpenAI-compatible chat client for the procedural pass.

    Uses the ``conv_extraction_*`` endpoint (same URL convention as the fact
    path: ``{url}/chat/completions``). TLS verification stays on.
    """

    def __init__(
        self,
        *,
        model: str,
        url: str,
        api_key: str = "",
        timeout: float = 60,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.url = url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # TODO(#604): replace with the shared outbound LLM client factory once
        # it exists. Default TLS verification on purpose: no verify=False here.
        self._client = client or httpx.AsyncClient(timeout=timeout, headers=headers)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def complete(self, messages: list[dict[str, Any]]) -> tuple[str, str | None]:
        last_error: Exception | None = None
        for attempt in range(_MAX_SERVICE_ATTEMPTS):
            try:
                response = await self._client.post(
                    f"{self.url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": 0.0,
                        "max_tokens": 4000,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (429, 502, 503, 504):
                    raise ProceduralLLMError(
                        f"Procedural extraction LLM returned {exc.response.status_code}"
                    ) from exc
                last_error = exc
            else:
                try:
                    choice = response.json()["choices"][0]
                    content = choice["message"].get("content") or ""
                    return str(content), choice.get("finish_reason")
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise ProceduralLLMError(f"Unexpected LLM response structure: {exc}") from exc
            if attempt < _MAX_SERVICE_ATTEMPTS - 1:
                delay = _RETRY_DELAYS[attempt + 1]
                logger.warning(
                    "Procedural extraction LLM error (attempt %d/%d): %s -- retrying in %.0fs",
                    attempt + 1, _MAX_SERVICE_ATTEMPTS, last_error, delay,
                )
                await _sleep(delay)
        raise ProceduralLLMError(f"Procedural extraction LLM failed after retries: {last_error}")


async def _sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def _parse_json(text: str) -> Any:
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", (text or "").strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)
    return json.loads(cleaned)


def _parse_proposal(content: str, finish_reason: str | None) -> ProceduralProposal:
    if finish_reason == "length":
        raise ProposalFormatError("response was truncated (finish_reason=length)")
    try:
        data = _parse_json(content)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProposalFormatError(f"response is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ProposalFormatError("response is not a JSON object")
    try:
        return ProceduralProposal.model_validate(data)
    except ValidationError as exc:
        raise ProposalFormatError(f"response does not match the schema: {exc}") from exc


async def detect_procedure(llm: Any, transcript: str) -> ProceduralProposal:
    """Ask the model for a proposal; one correction retry on a format error."""
    system_prompt, _, _ = load_prompts()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": transcript},
    ]
    content, finish = await llm.complete(messages)
    try:
        return _parse_proposal(content, finish)
    except ProposalFormatError as first:
        logger.warning("Procedural proposal format error, sending one correction: %s", first)
        messages = [
            *messages,
            {"role": "assistant", "content": content[:4000]},
            {
                "role": "user",
                "content": (
                    f"That response cannot be used: {str(first)[:1500]}. Return exactly one "
                    "JSON object that follows the schema in the system message and nothing "
                    "else. Report only steps visible in the transcript, cite only seq numbers "
                    "that appear in it, and do not invent intermediate reasoning."
                ),
            },
        ]
        content, finish = await llm.complete(messages)
        return _parse_proposal(content, finish)


def make_tiebreaker(llm: Any) -> Callable[[str, str], Awaitable[str]]:
    """Build the D5 tiebreaker on the same endpoint. Returns 'same' or 'different'."""

    async def _tiebreak(new_description: str, existing_description: str) -> str:
        _, tiebreaker_prompt, _ = load_prompts()
        content, _finish = await llm.complete([
            {"role": "system", "content": tiebreaker_prompt},
            {
                "role": "user",
                "content": f"Procedure A:\n{new_description}\n\nProcedure B:\n{existing_description}",
            },
        ])
        verdict = _parse_json(content).get("verdict")
        if verdict not in ("same", "different"):
            raise ValueError(f"tiebreaker returned {verdict!r}")
        return verdict

    return _tiebreak


# ---------------------------------------------------------------------------
# Deterministic validation (D3)
# ---------------------------------------------------------------------------


@dataclass
class EvText:
    text: str
    evidence: str
    source_messages: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "evidence": self.evidence, "source_messages": self.source_messages}


@dataclass
class VStep:
    key: str
    action: str
    tool_ref: str | None
    preconditions: list[EvText]
    postconditions: list[EvText]
    pitfalls: list[EvText]
    guidance: EvText | None
    status: str
    evidence: str
    source_messages: list[int]

    @property
    def first_seq(self) -> int:
        return min(self.source_messages) if self.source_messages else 10**9

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "action": self.action, "tool_ref": self.tool_ref,
            "preconditions": [t.to_dict() for t in self.preconditions],
            "postconditions": [t.to_dict() for t in self.postconditions],
            "pitfalls": [t.to_dict() for t in self.pitfalls],
            "guidance": self.guidance.to_dict() if self.guidance else None,
            "status": self.status, "evidence": self.evidence,
            "source_messages": self.source_messages,
        }


@dataclass
class VEdge:
    source: str
    target: str
    type: str
    condition: EvText | None
    guidance: EvText | None
    pitfalls: list[EvText]
    evidence: str
    source_messages: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "target": self.target, "type": self.type,
            "condition": self.condition.to_dict() if self.condition else None,
            "guidance": self.guidance.to_dict() if self.guidance else None,
            "pitfalls": [t.to_dict() for t in self.pitfalls],
            "evidence": self.evidence, "source_messages": self.source_messages,
        }


@dataclass
class OutcomeInfo:
    """The session outcome as the gates see it (D-Outcome, D6a.5)."""

    result: str  # success | failure | partial | unknown
    source: str  # explicit_message | llm | none
    confidence: float
    strong: bool
    failure_class: str | None = None
    failure_step: str | None = None
    failure_edge: tuple[str, str] | None = None
    failure_observed: bool = False
    source_messages: list[int] = field(default_factory=list)

    @property
    def is_failure(self) -> bool:
        return self.result in ("failure", "partial")

    @property
    def strong_success(self) -> bool:
        return self.strong and self.result == "success"

    @property
    def attributable(self) -> bool:
        return (
            self.strong
            and self.is_failure
            and self.failure_class == "procedure"
            and self.failure_observed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result, "source": self.source, "confidence": self.confidence,
            "strong": self.strong, "failure_class": self.failure_class,
            "failure_step": self.failure_step,
            "failure_edge": list(self.failure_edge) if self.failure_edge else None,
            "failure_observed": self.failure_observed,
            "source_messages": self.source_messages,
        }


@dataclass
class ValidatedProcedure:
    goal: str
    summary: str
    steps: list[VStep]  # observed only, ordered by first cited sequence number
    edges: list[VEdge]  # observed only, between kept steps
    entry_key: str
    outcome: OutcomeInfo
    detection_confidence: float
    inferred_steps: list[VStep] = field(default_factory=list)
    inferred_edges: list[VEdge] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def description(self) -> str:
        return f"{self.goal}: {self.summary}".strip()[:_ROOT_CONTENT_MAX]

    @property
    def source_messages(self) -> list[int]:
        seqs: set[int] = set()
        for step in self.steps:
            seqs.update(step.source_messages)
        for edge in self.edges:
            seqs.update(edge.source_messages)
        seqs.update(self.outcome.source_messages)
        return sorted(seqs)

    def step(self, key: str) -> VStep | None:
        return next((s for s in self.steps if s.key == key), None)

    def all_text(self) -> list[str]:
        """Every string that may be written anywhere (for the curation pre-flight)."""
        texts = [self.goal, self.summary]
        for step in [*self.steps, *self.inferred_steps]:
            texts.extend([step.action, step.tool_ref or ""])
            for item in [*step.preconditions, *step.postconditions, *step.pitfalls]:
                texts.append(item.text)
            if step.guidance:
                texts.append(step.guidance.text)
        for edge in [*self.edges, *self.inferred_edges]:
            for item in [edge.condition, edge.guidance, *edge.pitfalls]:
                if item is not None:
                    texts.append(item.text)
        return [t for t in texts if t]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal, "summary": self.summary, "entry_step": self.entry_key,
            "steps": [s.to_dict() for s in self.steps],
            "edges": [e.to_dict() for e in self.edges],
            "inferred_steps": [s.to_dict() for s in self.inferred_steps],
            "inferred_edges": [e.to_dict() for e in self.inferred_edges],
            "outcome": self.outcome.to_dict(),
            "detection_confidence": self.detection_confidence,
            "notes": self.notes,
        }


@dataclass
class Rejection:
    reason: str
    detail: str


def _ev(item: EvidencedText | None, included: set[int]) -> EvText | None:
    if item is None:
        return None
    seqs = sorted({s for s in item.source_messages if s in included})
    evidence = item.evidence if seqs else "inferred"
    return EvText(text=item.text, evidence=evidence, source_messages=seqs)


def _ev_list(items: list[EvidencedText], included: set[int]) -> list[EvText]:
    return [x for x in (_ev(item, included) for item in items) if x is not None]


def _resolve_outcome(
    proposal: ProceduralProposal,
    events: list[TrajectoryEvent],
    included: set[int],
    step_keys: set[str],
    edge_keys: set[tuple[str, str]],
    settings: AppSettings,
) -> OutcomeInfo:
    raw = proposal.outcome or OutcomeProposal()
    failure = raw.failure
    failure_class = failure.failure_class if failure else None
    failure_step = failure.step if failure and failure.step in step_keys else None
    failure_edge = None
    if failure and failure.edge and (failure.edge.source, failure.edge.target) in edge_keys:
        failure_edge = (failure.edge.source, failure.edge.target)
    failure_seqs = sorted({s for s in (failure.source_messages if failure else []) if s in included})
    failure_observed = bool(failure and failure.evidence == "observed" and failure_seqs)

    marker_event = next((e for e in reversed(events) if e.outcome_marker), None)
    if marker_event is not None:
        return OutcomeInfo(
            result=marker_event.outcome_marker or "unknown",
            source="explicit_message",
            confidence=1.0,
            strong=True,
            failure_class=failure_class,
            failure_step=failure_step,
            failure_edge=failure_edge,
            failure_observed=failure_observed,
            source_messages=[marker_event.seq],
        )

    seqs = sorted({s for s in raw.source_messages if s in included})
    strong = (
        raw.result in ("success", "failure", "partial")
        and raw.evidence == "observed"
        and bool(seqs)
        and raw.confidence >= settings.procedural_min_outcome_confidence
    )
    return OutcomeInfo(
        result=raw.result,
        source="llm" if proposal.outcome is not None else "none",
        confidence=raw.confidence,
        strong=strong,
        failure_class=failure_class,
        failure_step=failure_step,
        failure_edge=failure_edge,
        failure_observed=failure_observed,
        source_messages=seqs,
    )


def validate_proposal(
    proposal: ProceduralProposal,
    events: list[TrajectoryEvent],
    included: set[int],
    settings: AppSettings,
) -> ValidatedProcedure | Rejection | None:
    """Apply the deterministic rules of D3. ``None`` means no procedure detected."""
    if not proposal.procedure_detected:
        return None
    roles = {e.seq: e.role for e in events}
    tool_seqs = {s for s, r in roles.items() if r in ("tool_call", "tool_result") and s in included}
    call_seqs = {s for s, r in roles.items() if r == "tool_call" and s in included}
    notes: list[str] = []

    if not proposal.goal:
        return Rejection("below_confidence", "proposal has no goal")
    if len(proposal.steps) > settings.procedural_max_steps:
        return Rejection(
            "cap_reached",
            f"{len(proposal.steps)} steps exceeds procedural_max_steps={settings.procedural_max_steps}",
        )

    # Steps: unique keys, citations restricted to the transcript, downgrades.
    steps: dict[str, VStep] = {}
    for raw in proposal.steps:
        if raw.key in steps:
            notes.append(f"duplicate step key {raw.key} dropped")
            continue
        seqs = sorted({s for s in raw.source_messages if s in included})
        evidence = raw.evidence if seqs else "inferred"
        if evidence == "observed" and raw.tool_ref and not (set(seqs) & tool_seqs):
            evidence = "inferred"
            notes.append(f"step {raw.key}: tool_ref without a cited tool message; downgraded")
        steps[raw.key] = VStep(
            key=raw.key,
            action=raw.action,
            tool_ref=raw.tool_ref,
            preconditions=_ev_list(raw.preconditions, included),
            postconditions=_ev_list(raw.postconditions, included),
            pitfalls=_ev_list(raw.pitfalls, included),
            guidance=_ev(raw.guidance, included),
            status=raw.status,
            evidence=evidence,
            source_messages=seqs,
        )

    observed = {k: s for k, s in steps.items() if s.evidence == "observed"}
    inferred_steps = [s for s in steps.values() if s.evidence != "observed"]

    # Edges: known endpoints, no self/duplicate edges, ordering for precedes.
    edges: list[VEdge] = []
    inferred_edges: list[VEdge] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in proposal.edges:
        triple = (raw.source, raw.target, raw.type)
        if raw.source == raw.target or triple in seen:
            continue
        if raw.source not in steps or raw.target not in steps:
            notes.append(f"edge {raw.source}->{raw.target} references an unknown step; dropped")
            continue
        seen.add(triple)
        seqs = sorted({s for s in raw.source_messages if s in included})
        evidence = raw.evidence if seqs else "inferred"
        if raw.source not in observed or raw.target not in observed:
            evidence = "inferred"
        elif raw.type == "precedes" and observed[raw.source].first_seq >= observed[raw.target].first_seq:
            evidence = "inferred"
            notes.append(f"edge {raw.source}->{raw.target}: order contradicts citations; downgraded")
        edge = VEdge(
            source=raw.source,
            target=raw.target,
            type=raw.type,
            condition=_ev(raw.condition, included),
            guidance=_ev(raw.guidance, included),
            pitfalls=_ev_list(raw.pitfalls, included),
            evidence=evidence,
            source_messages=seqs,
        )
        (edges if evidence == "observed" else inferred_edges).append(edge)

    # Entry: the proposal's entry when valid, otherwise the single candidate.
    incoming = {e.target for e in edges if e.type == "precedes"}
    entry = proposal.entry_step
    if entry not in observed or entry in incoming:
        candidates = sorted(k for k in observed if k not in incoming)
        if len(candidates) == 1:
            notes.append(f"entry_step {entry!r} invalid; using the only candidate {candidates[0]}")
            entry = candidates[0]
        else:
            return Rejection("breaks_entry", f"no valid entry step (proposed {entry!r}, candidates {candidates})")

    # Reachability from the entry: precedes directed, requires/alternative_to both ways.
    adjacency: dict[str, set[str]] = {k: set() for k in observed}
    for edge in edges:
        adjacency[edge.source].add(edge.target)
        if edge.type != "precedes":
            adjacency[edge.target].add(edge.source)
    reached = {entry}
    frontier = [entry]
    while frontier:
        node = frontier.pop()
        for nxt in adjacency.get(node, ()):
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)
    unreachable = sorted(set(observed) - reached)
    if unreachable:
        notes.append(f"steps unreachable from the entry dropped: {unreachable}")
        for key in unreachable:
            inferred_steps.append(observed.pop(key))
        edges = [e for e in edges if e.source in observed and e.target in observed]

    ordered = sorted(observed.values(), key=lambda s: (s.first_seq, s.key))
    if len(ordered) < settings.procedural_min_steps:
        return Rejection(
            "insufficient_evidence",
            f"{len(ordered)} observed steps; procedural_min_steps={settings.procedural_min_steps}",
        )
    if not any(set(s.source_messages) & call_seqs for s in ordered):
        return Rejection("insufficient_evidence", "no observed step cites a tool_call message")
    if proposal.detection_confidence < settings.procedural_min_detection_confidence:
        return Rejection(
            "below_confidence",
            f"detection_confidence {proposal.detection_confidence:.2f} < "
            f"{settings.procedural_min_detection_confidence:.2f}",
        )

    outcome = _resolve_outcome(
        proposal, events, included,
        set(observed), {(e.source, e.target) for e in edges}, settings,
    )
    return ValidatedProcedure(
        goal=proposal.goal.strip(),
        summary=(proposal.summary or "").strip(),
        steps=ordered,
        edges=edges,
        entry_key=entry,
        outcome=outcome,
        detection_confidence=proposal.detection_confidence,
        inferred_steps=inferred_steps,
        inferred_edges=inferred_edges,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Thread snapshot, cursor, decision log (D9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThreadInfo:
    """Plain copy of the thread fields the pass needs (survives a rollback)."""

    id: uuid.UUID
    tenant_id: str
    owner_id: str
    scope: str
    scope_id: str | None
    actor_id: str | None

    @classmethod
    def of(cls, thread: ConversationThread) -> ThreadInfo:
        return cls(
            id=thread.id,
            tenant_id=thread.tenant_id,
            owner_id=thread.owner_id,
            scope=thread.scope,
            scope_id=thread.scope_id,
            actor_id=thread.actor_id,
        )


def cursor_state(thread: ConversationThread) -> dict[str, Any]:
    metadata = thread.metadata_ if isinstance(thread.metadata_, dict) else {}
    state = metadata.get(CURSOR_KEY)
    return dict(state) if isinstance(state, dict) else {}


def set_cursor_state(thread: ConversationThread, **updates: Any) -> None:
    metadata = copy.deepcopy(thread.metadata_) if isinstance(thread.metadata_, dict) else {}
    state = metadata.get(CURSOR_KEY) if isinstance(metadata.get(CURSOR_KEY), dict) else {}
    state = {**state, **updates}
    metadata[CURSOR_KEY] = state
    thread.metadata_ = metadata  # reassign: the JSON column is not mutation-tracked


def record_procedural_decision(
    session: AsyncSession,
    *,
    kind: str,
    reason: str | None,
    thread: ThreadInfo,
    run_id: str,
    payload: dict[str, Any],
    source_messages: list[int] | None = None,
    procedure_id: uuid.UUID | None = None,
    nearest_id: uuid.UUID | None = None,
    score: float | None = None,
    stub: str = "",
) -> None:
    """The single path for every procedural decision, accepted or rejected.

    Rejections (``kind=proc_reject`` or a proposal with a reason) carry the
    proposal, the reason, the source thread and messages and the run id. #555
    can back this function with a rejection store without changing callers.
    Rows land in ``reconciliation_decisions`` (no new table).
    """
    if reason is not None and reason not in REASONS:
        raise ValueError(f"unknown procedural decision reason {reason!r}")
    body = {
        "kind": kind,
        "thread_id": str(thread.id),
        "source_messages": source_messages or [],
        "run_id": run_id,
        **payload,
    }
    session.add(
        ReconciliationDecisionRow(
            id=uuid.uuid4(),
            extraction_run_id=run_id,
            candidate_content=json.dumps(body, default=str, sort_keys=True),
            candidate_stub=(stub or kind)[:200],
            nearest_match_id=nearest_id,
            similarity_score=score,
            action=kind,
            memory_id=procedure_id,
            reason=reason,
            owner_id=thread.owner_id,
            tenant_id=thread.tenant_id,
            scope=thread.scope,
            scope_id=thread.scope_id,
        )
    )


async def curation_preflight(
    session: AsyncSession, texts: list[str], *, thread: ThreadInfo,
) -> str | None:
    """Run the regex tier (secrets, PII) over all text that will be written.

    Returns a block detail, or ``None`` when the text may be written. Also
    seeds the tenant's default rules (which commits) before any write in the
    graph transaction, so that commit cannot split it.
    """
    if not texts:
        return None
    result = await run_curation_pipeline(
        content="\n".join(texts),
        embedding=None,
        owner_id=thread.owner_id,
        scope=thread.scope,
        session=session,
        tenant_id=thread.tenant_id,
    )
    if result.get("blocked"):
        return str(result.get("reason") or "blocked")
    return None


# ---------------------------------------------------------------------------
# Creation writer (D4)
# ---------------------------------------------------------------------------


@dataclass
class RunContext:
    run_id: str
    model: str
    prompt_hash: str
    mode: str
    settings: AppSettings
    now: datetime
    roles: dict[int, str]
    created_node_ids: list[uuid.UUID] = field(default_factory=list)
    created_node_contents: dict[uuid.UUID, str] = field(default_factory=dict)

    def extraction_source(self, thread: ThreadInfo, seqs: list[int]) -> dict[str, Any]:
        return {
            "thread_id": str(thread.id),
            "source_messages": sorted(set(seqs)),
            "model": self.model,
            "prompt_hash": self.prompt_hash,
            "run_id": self.run_id,
        }

    def trust_for(self, seqs: list[int]) -> str:
        return "mixed" if any(self.roles.get(s) == "tool_result" for s in seqs) else "trusted"

    @property
    def weight(self) -> float:
        return min(self.settings.procedural_auto_weight, self.settings.procedural_max_auto_weight)


def observed_texts(items: list[EvText]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item.evidence == "observed" and item.text not in out:
            out.append(item.text)
    return out[:_ATTR_PER_FIELD_MAX]


def attribute_evidence_entry(item: EvText, thread_id: uuid.UUID) -> dict[str, Any]:
    return {
        "text": item.text,
        "evidence": item.evidence,
        "source_messages": item.source_messages,
        "threads": [str(thread_id)],
    }


def step_metadata(step: VStep, thread: ThreadInfo, ctx: RunContext) -> dict[str, Any]:
    """``metadata_`` for a new step. Render fields hold observed items only."""
    attr: dict[str, list[dict[str, Any]]] = {}
    for name in ("preconditions", "postconditions", "pitfalls"):
        items = getattr(step, name)
        if items:
            attr[name] = [attribute_evidence_entry(i, thread.id) for i in items]
    if step.guidance:
        attr["guidance"] = [attribute_evidence_entry(step.guidance, thread.id)]
    guidance = step.guidance.text if step.guidance and step.guidance.evidence == "observed" else None
    return {
        "extraction_source": ctx.extraction_source(thread, step.source_messages),
        "procedure": {
            "action": step.action,
            "tool_ref": step.tool_ref,
            "preconditions": observed_texts(step.preconditions),
            "postconditions": observed_texts(step.postconditions),
            "pitfalls": observed_texts(step.pitfalls),
            "guidance": guidance,
            "origin": "extracted",
            "step_key": step.key,
            "attribute_evidence": attr,
        },
    }


def edge_metadata(edge: VEdge, thread: ThreadInfo, ctx: RunContext) -> dict[str, Any]:
    attr: dict[str, list[dict[str, Any]]] = {}
    for name in ("condition", "guidance"):
        item = getattr(edge, name)
        if item is not None:
            attr[name] = [attribute_evidence_entry(item, thread.id)]
    if edge.pitfalls:
        attr["pitfalls"] = [attribute_evidence_entry(i, thread.id) for i in edge.pitfalls]
    condition = edge.condition.text if edge.condition and edge.condition.evidence == "observed" else None
    guidance = edge.guidance.text if edge.guidance and edge.guidance.evidence == "observed" else None
    return {
        "condition": condition,
        "guidance": guidance,
        "pitfalls": observed_texts(edge.pitfalls),
        "origin": "extracted",
        "attribute_evidence": attr,
        "extraction_source": ctx.extraction_source(thread, edge.source_messages),
    }


async def create_step_node(
    session: AsyncSession,
    embedding_service: Any,
    *,
    root_id: uuid.UUID,
    content: str,
    metadata: dict[str, Any],
    thread: ThreadInfo,
    ctx: RunContext,
    seqs: list[int],
) -> uuid.UUID:
    from memoryhub_core.services.memory import create_memory

    data = MemoryNodeCreate(
        content=content,
        scope=thread.scope,
        weight=ctx.weight,
        owner_id=thread.owner_id,
        scope_id=thread.scope_id,
        parent_id=root_id,
        branch_type="procedure_step",
        metadata=metadata,
        content_type="procedural",
        source="dreaming",
        upstream_trust_level=ctx.trust_for(seqs),
    )
    if thread.actor_id:
        data.actor_id = thread.actor_id
    node, _ = await create_memory(
        data, session, embedding_service,
        tenant_id=thread.tenant_id, skip_curation=True, commit=False,
    )
    assert node is not None  # skip_curation=True never blocks
    ctx.created_node_ids.append(node.id)
    ctx.created_node_contents[node.id] = content
    return node.id


async def create_procedural_edge(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    rel_type: str,
    metadata: dict[str, Any],
) -> uuid.UUID:
    rel = await create_relationship(
        RelationshipCreate(
            source_id=source_id,
            target_id=target_id,
            relationship_type=rel_type,
            created_by=CREATED_BY,
            metadata=metadata,
        ),
        session,
        commit=False,
    )
    return rel.id


async def write_new_procedure(
    session: AsyncSession,
    embedding_service: Any,
    *,
    thread: ThreadInfo,
    vp: ValidatedProcedure,
    ctx: RunContext,
    max_seq: int,
) -> uuid.UUID:
    """Write root, steps, edges and the entry inside the caller's transaction.

    Nothing is committed here. The caller commits once (with the provenance
    row, the decision row and the cursor) or rolls everything back.
    """
    from memoryhub_core.services.memory import create_memory
    from memoryhub_core.services.procedure_evolution import (
        build_session_entry,
        refresh_derived_evidence,
    )

    root_seqs = vp.source_messages
    root_data = MemoryNodeCreate(
        content=vp.description,
        scope=thread.scope,
        weight=ctx.weight,
        owner_id=thread.owner_id,
        scope_id=thread.scope_id,
        metadata={
            "extraction_source": ctx.extraction_source(thread, root_seqs),
            "procedure": {
                "origin": "extracted",
                "schema_version": 1,
                "status": "candidate",
                "goal": vp.goal,
                "entry_step_id": None,
                "evidence": {"sessions": {}, "evicted": {}},
                "pending_edits": [],
                "contradictions": [],
            },
        },
        content_type="procedural",
        source="dreaming",
        upstream_trust_level=ctx.trust_for(root_seqs),
    )
    if thread.actor_id:
        root_data.actor_id = thread.actor_id
    root_read, _ = await create_memory(
        root_data, session, embedding_service,
        tenant_id=thread.tenant_id, skip_curation=True, commit=False,
    )
    assert root_read is not None
    root_id = root_read.id
    ctx.created_node_ids.append(root_id)
    ctx.created_node_contents[root_id] = vp.description

    ids: dict[str, uuid.UUID] = {}
    for step in vp.steps:
        ids[step.key] = await create_step_node(
            session, embedding_service,
            root_id=root_id,
            content=step.action,
            metadata=step_metadata(step, thread, ctx),
            thread=thread, ctx=ctx, seqs=step.source_messages,
        )
    active_edges: dict[tuple[str, str, str], str] = {}
    for edge in vp.edges:
        edge_id = await create_procedural_edge(
            session,
            source_id=ids[edge.source],
            target_id=ids[edge.target],
            rel_type=edge.type,
            metadata=edge_metadata(edge, thread, ctx),
        )
        active_edges[(str(ids[edge.source]), str(ids[edge.target]), edge.type)] = str(edge_id)

    root = await session.get(MemoryNode, root_id)
    assert root is not None
    metadata = copy.deepcopy(root.metadata_)
    procedure = metadata["procedure"]
    procedure["entry_step_id"] = str(ids[vp.entry_key])
    entry = build_session_entry(
        vp=vp, alignment=ids, active_edges=active_edges, run_id=ctx.run_id, max_seq=max_seq, now=ctx.now,
    )
    procedure["evidence"]["sessions"][str(thread.id)] = entry
    root.metadata_ = metadata

    session.add(
        ConversationExtraction(
            id=uuid.uuid4(),
            memory_node_id=root_id,
            thread_id=thread.id,
            source_messages=root_seqs,
            extracted_by=EXTRACTED_BY,
            extraction_model=ctx.model,
            extraction_prompt_hash=ctx.prompt_hash,
            tenant_id=thread.tenant_id,
        )
    )
    await session.flush()
    await refresh_derived_evidence(session, root_id=root_id, tenant_id=thread.tenant_id, settings=ctx.settings)
    return root_id


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _proposal_texts(proposal: ProceduralProposal) -> list[str]:
    texts = [proposal.goal or "", proposal.summary or ""]
    for step in proposal.steps:
        texts.extend([step.action, step.tool_ref or ""])
        for item in [*step.preconditions, *step.postconditions, *step.pitfalls, step.guidance]:
            if item is not None:
                texts.append(item.text)
    for edge in proposal.edges:
        for item in [edge.condition, edge.guidance, *edge.pitfalls]:
            if item is not None:
                texts.append(item.text)
    return [t for t in texts if t]


def _empty_report(mode: str, run_id: str, cursor: int) -> dict[str, Any]:
    return {
        "mode": mode,
        "run_id": run_id,
        "detected": 0,
        "created": 0,
        "matched": 0,
        "edits_proposed": 0,
        "edits_applied": 0,
        "edits_rejected": 0,
        "procedure_ids": [],
        "cursor": cursor,
        "rejections": [],
        "overlapping_facts": [],
    }


async def _overlapping_facts(
    session: AsyncSession, embedding_service: Any, description: str, fact_ids: list[uuid.UUID],
) -> list[dict[str, Any]]:
    """D8: fact memories from the same run that restate the procedure."""
    if not fact_ids:
        return []
    from memoryhub_core.services.procedure_evolution import _cosine

    embedding = await embedding_service.embed(description)
    rows = (await session.execute(select(MemoryNode).where(MemoryNode.id.in_(fact_ids)))).scalars().all()
    out = []
    for node in rows:
        score = _cosine(embedding, node.embedding)
        if score >= 0.85:
            out.append({"memory_id": str(node.id), "score": round(score, 4)})
    return out


async def extract_procedures_from_thread(
    session: AsyncSession,
    *,
    thread: ConversationThread,
    embedding_service: Any,
    mode: str,
    model: str,
    url: str,
    api_key: str = "",
    timeout: float = 60,
    s3_adapter: Any | None = None,
    turn_range: tuple[int, int] | None = None,
    dry_run: bool = False,
    llm: Any | None = None,
    settings: AppSettings | None = None,
    fact_memory_ids: list[uuid.UUID] | None = None,
) -> dict[str, Any]:
    """Run the procedural pass over one thread and return the ``procedures`` report.

    Never raises for model, validation or write failures: those are reported
    under ``error`` or ``rejections`` so the fact results are unaffected.
    """
    from memoryhub_core.services.procedure_evolution import (
        ProceduralInvariantError,
        check_invariants,
        evolve_procedure,
        find_matching_procedure,
    )

    if mode not in ("propose", "apply"):
        raise ValueError(f"extract_procedures_from_thread needs mode propose or apply, got {mode!r}")
    settings = settings or AppSettings()
    _, _, prompt_hash = load_prompts()
    now = datetime.now(UTC)
    run_id = f"dream-proc:{model}:{prompt_hash}:{now.isoformat()}"
    info = ThreadInfo.of(thread)

    if not dry_run:
        # Serialize procedural runs per thread (a no-op on SQLite).
        await session.execute(
            select(ConversationThread)
            .where(ConversationThread.id == info.id, ConversationThread.tenant_id == info.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    cursor = int(cursor_state(thread).get("last_seq", 0) or 0)
    report = _empty_report(mode, run_id, cursor)
    if dry_run:
        report["dry_run"] = True

    async def finish(*, advance: bool, error: str | None = None) -> None:
        if dry_run:
            return
        updates: dict[str, Any] = {"last_run_id": run_id, "last_attempt_at": now.isoformat(), "last_error": error}
        if advance and turn_range is None:
            updates["last_seq"] = max(cursor, max_seq)
            report["cursor"] = updates["last_seq"]
        set_cursor_state(thread, **updates)
        await session.commit()

    def reject(reason: str, detail: str, payload: dict[str, Any] | None = None, **kwargs: Any) -> None:
        report["rejections"].append({"reason": reason, "detail": detail})
        if not dry_run:
            record_procedural_decision(
                session, kind=PROC_REJECT, reason=reason, thread=info, run_id=run_id,
                payload={"detail": detail, **(payload or {})}, stub=detail, **kwargs,
            )

    if info.scope not in ALLOWED_THREAD_SCOPES:
        report["skipped"] = "scope_not_allowed"
        reject("scope_not_allowed", f"thread scope {info.scope!r} may not produce procedures")
        if not dry_run:
            await session.commit()
        return report

    events = await load_trajectory(session, thread_id=info.id, s3_adapter=s3_adapter, turn_range=turn_range)
    max_seq = max((e.seq for e in events), default=0)
    if turn_range is None and max_seq <= cursor:
        report["skipped"] = "no_new_messages"
        if not dry_run:
            await session.commit()
        return report
    if not any(e.role == "tool_call" for e in events):
        report["skipped"] = "no_tool_calls"
        if not dry_run:
            await session.commit()
        return report

    transcript, included = format_trajectory(events, max_chars=settings.procedural_max_transcript_chars)
    roles = {e.seq: e.role for e in events}
    owns_llm = llm is None
    if llm is None:
        llm = ProceduralLLM(model=model, url=url, api_key=api_key, timeout=timeout)
    ctx = RunContext(
        run_id=run_id, model=model, prompt_hash=prompt_hash, mode=mode,
        settings=settings, now=now, roles=roles,
    )

    try:
        try:
            proposal = await detect_procedure(llm, transcript)
        except ProposalFormatError as exc:
            reject("invalid_proposal", str(exc)[:500], source_messages=sorted(included))
            await finish(advance=True)
            return report
        except Exception as exc:
            logger.warning("Procedural extraction failed for thread %s: %s", info.id, exc)
            report["error"] = str(exc)[:500]
            await finish(advance=False, error=str(exc)[:500])
            return report

        if proposal.procedure_detected:
            report["detected"] = 1
        validated = validate_proposal(proposal, events, included, settings)
        if validated is None:
            await finish(advance=True)
            return report

        if isinstance(validated, Rejection):
            payload: dict[str, Any] = {}
            if dry_run:
                payload = {"proposal": proposal.model_dump(by_alias=True)}
                report["proposal"] = payload["proposal"]
            elif await curation_preflight(session, _proposal_texts(proposal), thread=info) is None:
                payload = {"proposal": proposal.model_dump(by_alias=True)}
            reject(validated.reason, validated.detail, payload, source_messages=sorted(included))
            await finish(advance=True)
            return report

        if dry_run:
            report["proposal"] = validated.to_dict()
            report["curation"] = "not_run_in_dry_run"
        else:
            blocked = await curation_preflight(session, validated.all_text(), thread=info)
            if blocked is not None:
                # The proposal text is withheld from the log on purpose.
                reject("curation_blocked", f"curation blocked the proposal: {blocked}",
                       source_messages=validated.source_messages)
                await finish(advance=True)
                return report

        match = await find_matching_procedure(
            session, thread=info, vp=validated, embedding_service=embedding_service,
            settings=settings, tiebreaker=make_tiebreaker(llm),
        )
        report["match"] = match.to_dict()
        proposal_payload = {"proposal": validated.to_dict(), "match": match.to_dict()}

        if match.kind == "ambiguous":
            reject("ambiguous_match", "procedure matches an existing one ambiguously; nothing created",
                   proposal_payload, source_messages=validated.source_messages)
            await finish(advance=True)
            return report

        if match.kind == "new":
            if validated.outcome.is_failure:
                reject("insufficient_evidence", "failed_session: a failed session does not create a procedure",
                       proposal_payload, source_messages=validated.source_messages)
                await finish(advance=True)
                return report
            if dry_run:
                report["plan"] = {"action": "create", "status": "candidate"}
                report["created"] = 0
                return report
            try:
                root_id = await write_new_procedure(
                    session, embedding_service, thread=info, vp=validated, ctx=ctx, max_seq=max_seq,
                )
                await check_invariants(session, root_id=root_id, tenant_id=info.tenant_id, settings=settings)
                record_procedural_decision(
                    session, kind=PROC_CREATE, reason=None, thread=info, run_id=run_id,
                    payload={**proposal_payload, "created_nodes": [str(n) for n in ctx.created_node_ids]},
                    source_messages=validated.source_messages, procedure_id=root_id,
                    stub=validated.description,
                )
                await finish(advance=True)
            except ProceduralInvariantError as exc:
                await session.rollback()
                await session.refresh(thread)
                ctx.created_node_ids.clear()
                reject("breaks_entry", str(exc)[:500], proposal_payload,
                       source_messages=validated.source_messages)
                await finish(advance=True)
                return report
            except Exception as exc:
                logger.exception("Procedural graph write failed for thread %s", info.id)
                await session.rollback()
                await session.refresh(thread)
                ctx.created_node_ids.clear()
                report["error"] = f"write failed: {str(exc)[:400]}"
                await finish(advance=False, error=report["error"])
                return report
            report["created"] = 1
            report["procedure_ids"] = [str(root_id)]
            await _trigger_entity_extraction(ctx, info, embedding_service)
            report["overlapping_facts"] = await _overlapping_facts(
                session, embedding_service, validated.description, fact_memory_ids or [],
            )
            return report

        # match.kind == "match"
        report["matched"] = 1
        report["procedure_ids"] = [str(match.root_id)]
        try:
            result = await evolve_procedure(
                session, embedding_service, thread=info, vp=validated, match=match,
                ctx=ctx, max_seq=max_seq, dry_run=dry_run,
            )
        except ProceduralInvariantError as exc:
            logger.warning("Structural edits for %s broke an invariant; retrying without them: %s",
                           match.root_id, exc)
            await session.rollback()
            await session.refresh(thread)
            ctx.created_node_ids.clear()
            report["structural_rollback"] = str(exc)[:500]
            result = await evolve_procedure(
                session, embedding_service, thread=info, vp=validated, match=match,
                ctx=ctx, max_seq=max_seq, dry_run=dry_run, structural_allowed=False,
            )

        if result.status in ("duplicate", "evicted"):
            reason = "duplicate" if result.status == "duplicate" else "evidence_evicted"
            reject(reason, f"session already accounted for procedure {match.root_id}",
                   source_messages=validated.source_messages, procedure_id=match.root_id)
            await finish(advance=True)
            return report

        report["edits_proposed"] = len(result.edits)
        report["edits_applied"] = len(result.applied)
        report["edits_rejected"] = len(result.rejected)
        if dry_run:
            report["plan"] = {
                "action": "refine",
                "edits": [e.to_dict() for e in result.edits],
                "would_apply": [e.to_dict() for e in result.applied],
            }
            return report

        record_procedural_decision(
            session, kind=PROC_MATCH, reason=None, thread=info, run_id=run_id,
            payload={
                **proposal_payload,
                "applied": [e.to_dict() for e in result.applied],
                "snapshot": result.snapshot,
            },
            source_messages=validated.source_messages, procedure_id=match.root_id,
            nearest_id=match.root_id, score=match.score, stub=validated.description,
        )
        proposed = [p for p in result.pending if p.get("reason") == "propose_mode"]
        if proposed:
            record_procedural_decision(
                session, kind=PROC_PROPOSE, reason="propose_mode", thread=info, run_id=run_id,
                payload={"edits": proposed}, source_messages=validated.source_messages,
                procedure_id=match.root_id, stub=f"{len(proposed)} proposed edits",
            )
        if result.applied:
            record_procedural_decision(
                session, kind=PROC_EDIT, reason=None, thread=info, run_id=run_id,
                payload={"edits": [e.to_dict() for e in result.applied]},
                source_messages=validated.source_messages, procedure_id=match.root_id,
                stub=f"{len(result.applied)} applied edits",
            )
        for edit in result.rejected:
            reject(edit.gate or "insufficient_evidence", f"{edit.kind} on {edit.target}",
                   {"edit": edit.to_dict()}, source_messages=validated.source_messages,
                   procedure_id=match.root_id)
        await finish(advance=True)
        await _trigger_entity_extraction(ctx, info, embedding_service)
        report["overlapping_facts"] = await _overlapping_facts(
            session, embedding_service, validated.description, fact_memory_ids or [],
        )
        return report
    finally:
        if owns_llm:
            await llm.aclose()


async def _trigger_entity_extraction(ctx: RunContext, thread: ThreadInfo, embedding_service: Any) -> None:
    """Fire entity extraction for every node the committed run created."""
    from memoryhub_core.services.extraction_runner import trigger_extraction

    for node_id in ctx.created_node_ids:
        await trigger_extraction(
            memory_id=node_id,
            content=ctx.created_node_contents.get(node_id, ""),
            tenant_id=thread.tenant_id,
            owner_id=thread.owner_id,
            embedding_service=embedding_service,
        )

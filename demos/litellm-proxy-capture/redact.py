"""Strip credentials out of captured traffic before anything is stored.

The proxy sees *everything* an agent sends to the model, which routinely
includes secrets: a pasted token, an ``export AWS_SECRET_ACCESS_KEY=...``
line in a terminal transcript, a connection string, the body of a private
key. Memory is durable, searchable and -- in the team case -- shared, so a
secret that reaches the store is worse than a secret in a chat log: it
outlives the conversation and comes back in someone else's recall.

Redaction happens in ``capture_core.transcript``, the single funnel that
produces both what is stored *and* what delta digests are computed from, so
a redacted message hashes the same way every time it is resent.

Deliberate design choices:

* **Fail closed on shape, not on value.** Rules match the *shape* of a
  credential (prefix, charset, length) or an assignment to a
  secret-sounding name. That catches the common cases and cannot catch a
  secret that looks like an English sentence.
* **Replace, never drop.** The placeholder keeps the sentence readable, so
  extraction still learns "the user configured an AWS key" without
  learning the key.
* **Count what was hit.** Every redaction is counted per rule and lands in
  the Observation, so the demo can show a number rather than a promise.

This is a PoC-grade filter. It is deliberately NOT presented as a
compliance control: see ``REDACTION.md`` notes in the walkthrough.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PLACEHOLDER = "[redacted:{rule}]"


def _rule(name: str, pattern: str, flags: int = 0, group: int | None = None):
    return (name, re.compile(pattern, flags), group)


# Ordered most-specific first: an AWS key inside an `aws_key = ...`
# assignment should be reported as the key, not as the assignment.
RULES: list[tuple[str, re.Pattern, int | None]] = [
    _rule("private_key",
          r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
          re.DOTALL),
    _rule("aws_access_key_id", r"\b(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}\b"),
    _rule("github_token", r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    _rule("slack_token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    _rule("openai_key", r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b"),
    _rule("google_api_key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    _rule("jwt", r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"),
    # https://user:password@host -- only the credentials, keep the host
    _rule("url_credentials", r"(?<=://)[^\s/@:]+:[^\s/@]+(?=@)"),
    _rule("bearer_token", r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    # key = value / "key": "value" for secret-sounding names; only the value
    _rule("secret_assignment",
          r"(?i)\b(?:pass(?:word|wd)?|secret|secret[_-]?key|api[_-]?key|access[_-]?key|"
          r"auth[_-]?token|token|credentials?)\b\s*[:=]\s*[\"']?(?P<v>[^\s\"',;]{8,})[\"']?",
          0, 1),
]


@dataclass
class Redactor:
    """Applies RULES plus any operator-supplied extra patterns."""

    enabled: bool = True
    extra: list[tuple[str, re.Pattern, int | None]] = field(default_factory=list)

    def rules(self) -> list[tuple[str, re.Pattern, int | None]]:
        return [*RULES, *self.extra]

    def __call__(self, text: str) -> tuple[str, dict[str, int]]:
        return self.apply(text)

    def apply(self, text: str) -> tuple[str, dict[str, int]]:
        """Return (redacted text, {rule name: hits})."""
        if not self.enabled or not text:
            return text, {}
        hits: dict[str, int] = {}
        for name, pattern, group in self.rules():
            placeholder = PLACEHOLDER.format(rule=name)

            def repl(m: re.Match, _p=placeholder, _g=group, _n=name) -> str:
                if _g is None:
                    hits[_n] = hits.get(_n, 0) + 1
                    return _p
                # replace only the captured value, keep the surrounding
                # `api_key = ` so the sentence still reads
                whole, start, end = m.group(0), m.start(_g) - m.start(0), m.end(_g) - m.start(0)
                if m.group(_g) is None or m.group(_g).startswith("[redacted:"):
                    return whole
                hits[_n] = hits.get(_n, 0) + 1
                return whole[:start] + _p + whole[end:]

            text = pattern.sub(repl, text)
        return text, hits


def parse_extra(spec: str) -> list[tuple[str, re.Pattern, int | None]]:
    """Parse ``name=regex`` entries separated by newlines or ``;;``.

    Lets an operator add a site-specific pattern (an internal token prefix,
    an employee id format) without touching this file.
    """
    out: list[tuple[str, re.Pattern, int | None]] = []
    for chunk in re.split(r";;|\n", spec or ""):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        name, _, pattern = chunk.partition("=")
        if not pattern:
            name, pattern = "custom", chunk
        out.append((name.strip() or "custom", re.compile(pattern), None))
    return out


def redactor_from_env(env: dict | None = None) -> Redactor:
    import os

    env = env if env is not None else os.environ
    val = str(env.get("MEMORYHUB_CAPTURE_REDACT", "true")).strip().lower()
    enabled = val in ("1", "true", "yes", "on")
    return Redactor(enabled=enabled, extra=parse_extra(env.get("MEMORYHUB_CAPTURE_REDACT_EXTRA", "")))

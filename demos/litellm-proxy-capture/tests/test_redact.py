"""Redaction must catch credential shapes and leave prose alone."""

import pytest

from capture_core import Message, redact_messages
from redact import Redactor, parse_extra, redactor_from_env


@pytest.fixture
def r():
    return Redactor()


@pytest.mark.parametrize("rule,text", [
    ("aws_access_key_id", "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"),
    ("github_token", "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 works"),
    # Split so the source has no contiguous Slack token for push protection.
    ("slack_token", "xox" + "b-example-token-value"),
    ("openai_key", 'api_key = "sk-proj-abcdefghijklmnopqrstuvwxyz0123"'),
    ("google_api_key", "key AIzaSyA1234567890abcdefghijklmnopqrstuv done"),
    ("jwt", "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.dBjftJeZ4CVPmB92K27u"),
    ("url_credentials", "psql postgres://admin:hunter2@db.internal:5432/prod"),
    ("secret_assignment", "set password: correct-horse-battery"),
    ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIB\n-----END RSA PRIVATE KEY-----"),
])
def test_each_rule_fires(r, rule, text):
    out, hits = r.apply(text)
    assert rule in hits, f"{rule} missed in {out!r}"
    assert "[redacted:" in out


@pytest.mark.parametrize("text", [
    "After a meeting we decided to migrate the backend to Rust, not Go.",
    "The API must stay backwards compatible with v2 clients until the end of the year.",
    "CI moves to Tekton; the pipeline runs on every merge to main.",
    "sk-poc-local",                      # the demo's own short master key stays readable
    "Use token-based pagination here.",  # the word 'token' without a value
])
def test_prose_is_untouched(r, text):
    out, hits = r.apply(text)
    assert out == text
    assert hits == {}


def test_the_host_survives_but_the_credentials_do_not(r):
    out, _ = r.apply("postgres://admin:hunter2@db.internal:5432/prod")
    assert "db.internal:5432/prod" in out
    assert "hunter2" not in out and "admin" not in out


def test_more_specific_rule_wins(r):
    """An AWS key inside an assignment is reported as the key, not as 'secret_assignment'."""
    _, hits = r.apply("aws_access_key_id = AKIAIOSFODNN7EXAMPLE")
    assert "aws_access_key_id" in hits


def test_redaction_is_stable_so_delta_tracking_survives(r):
    """The same input must redact to the same output, or resent history looks new."""
    text = "my key is ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    first, _ = r.apply(text)
    second, _ = r.apply(text)
    assert first == second
    assert Message("user", first).digest() == Message("user", second).digest()


def test_redacting_twice_does_not_re_flag(r):
    once, _ = r.apply("password: correct-horse-battery")
    twice, hits = r.apply(once)
    assert twice == once and hits == {}


def test_disabled_redactor_is_a_passthrough():
    off = Redactor(enabled=False)
    text = "AKIAIOSFODNN7EXAMPLE"
    assert off.apply(text) == (text, {})


def test_operator_supplied_pattern():
    r = Redactor(extra=parse_extra("rh_offline=RH-[0-9]{6}"))
    out, hits = r.apply("ticket RH-123456 attached")
    assert hits == {"rh_offline": 1} and "[redacted:rh_offline]" in out


def test_env_switch():
    assert redactor_from_env({"MEMORYHUB_CAPTURE_REDACT": "false"}).enabled is False
    assert redactor_from_env({}).enabled is True


def test_redact_messages_counts_across_a_transcript(r):
    msgs = [
        Message("user", "deploy with AWS_SECRET=AKIAIOSFODNN7EXAMPLE"),
        Message("assistant", "Sure. Also rotate ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345."),
        Message("user", "thanks"),
    ]
    out, totals = redact_messages(msgs, r)
    assert totals == {"aws_access_key_id": 1, "github_token": 1}
    assert out[2] is msgs[2]          # untouched messages are not copied
    assert "AKIA" not in out[0].content


def test_redact_messages_without_a_redactor_is_a_noop():
    msgs = [Message("user", "AKIAIOSFODNN7EXAMPLE")]
    assert redact_messages(msgs, None) == (msgs, {})

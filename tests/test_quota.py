"""Quota / rate-limit detection and retry-after parsing."""
import pytest

import pdf2md


@pytest.mark.parametrize("text,expected", [
    ("rate_limit_error: number of request tokens has exceeded", True),
    ("Rate limit reached for messages requests", True),
    ("HTTP 429 Too Many Requests", True),
    ("anthropic.RateLimitError: status 429", True),
    ("Your credit balance is too low to access the Claude API", True),
    ("overloaded_error: API is currently overloaded", True),
    ("usage limit reached", True),
    ("connection reset by peer", False),
    ("invalid request: missing required field", False),
    ("", False),
])
def test_is_quota_or_rate_limit(text, expected):
    assert pdf2md.is_quota_or_rate_limit(text) is expected


def test_parse_retry_after_seconds():
    assert pdf2md.parse_retry_after("Retry-After: 60") == 60.0


def test_parse_retry_after_iso_timestamp_returns_none_or_float():
    # ISO timestamps are converted via _wait_from_reset, not parse_retry_after
    # itself; here we just confirm the function doesn't crash on garbage.
    out = pdf2md.parse_retry_after("Retry-After: 2026-04-26T15:42:00Z")
    assert out is None or isinstance(out, float)


def test_parse_retry_after_missing():
    assert pdf2md.parse_retry_after("nothing useful here") is None


def test_parse_retry_after_garbage():
    assert pdf2md.parse_retry_after("retry-after: soon") is None

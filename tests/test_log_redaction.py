import logging

from doomtp_bot.log import RedactQueryFilter


def test_oauth_code_and_state_are_redacted_from_access_log_records() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", "/auth/callback?code=abc123&scope=user%3Abot&state=xyz", "1.1", 200), None,
    )  # fmt: skip
    assert RedactQueryFilter().filter(record)
    message = record.getMessage()
    assert "abc123" not in message and "xyz" not in message
    assert "code=[redacted]" in message and "scope=user%3Abot" in message and "state=[redacted]" in message

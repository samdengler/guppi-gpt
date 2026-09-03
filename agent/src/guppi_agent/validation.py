"""Run input validation and trimming.

The page resends the whole thread on every run, so the agent checks the shape once here
and trims from the front until the estimated model input fits a token budget.
"""

from __future__ import annotations

from ag_ui.core import Message, RunAgentInput

MAX_MESSAGES = 40
MAX_CONTENT_CHARS = 4000
INPUT_TOKEN_BUDGET = 16000
CHARS_PER_TOKEN = 4


def validate_run(run: RunAgentInput) -> str | None:
    """Return a reason the run input is unusable, or None when it is fine."""
    messages = run.messages
    if not 1 <= len(messages) <= MAX_MESSAGES:
        return f"expected 1 to {MAX_MESSAGES} messages, got {len(messages)}"
    expected = "user"
    for index, message in enumerate(messages):
        if message.role not in ("user", "assistant"):
            return (
                f"message {index} has role {message.role!r}; only user and assistant are accepted"
            )
        if message.role != expected:
            return f"message {index} should be a {expected} turn; roles must alternate"
        content = message.content
        if not isinstance(content, str) or not content.strip():
            return f"message {index} content must be a non-empty string"
        if len(content) >= MAX_CONTENT_CHARS:
            return (
                f"message {index} is {len(content)} characters; under {MAX_CONTENT_CHARS} required"
            )
        expected = "assistant" if expected == "user" else "user"
    if messages[-1].role != "user":
        return "the last message must be a user turn"
    return None


def estimate_tokens(messages: list[Message]) -> int:
    return sum(len(str(message.content)) // CHARS_PER_TOKEN for message in messages)


def trim_messages(messages: list[Message], budget: int = INPUT_TOKEN_BUDGET) -> list[Message]:
    """Drop whole user and assistant turns from the front until the estimate fits.

    The list is assumed valid: alternating roles starting and ending with a user turn. The
    final user turn is always kept.
    """
    trimmed = list(messages)
    while len(trimmed) > 1 and estimate_tokens(trimmed) > budget:
        trimmed = trimmed[2:]
    return trimmed

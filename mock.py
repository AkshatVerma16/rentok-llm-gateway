"""Built-in mock provider — the fallback when there is no provider key
configured, or when the primary provider fails after its retry.

It is deliberately *deterministic and obviously fake*: the reply echoes
the user's last message and says it is a mock. A fallback that pretended
to be a real model would be worse than an error — callers must be able
to tell from the response itself (provider: "mock", fallback: true)
that no real LLM answered.
"""


def mock_reply(messages):
    """Build the mock reply text from the last user message."""
    last_user = None
    for message in reversed(messages):
        if message.get("role") == "user":
            last_user = message.get("content", "")
            break
    asked = str(last_user)[:200] if last_user else "(no user message)"
    return (
        "Mock gateway response — no real LLM answered this request "
        "(no provider key is configured, or the primary provider failed). "
        f'You asked: "{asked}". '
        "Configure OPENAI_API_KEY to get real model output."
    )

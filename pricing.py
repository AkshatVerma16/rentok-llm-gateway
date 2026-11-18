"""Token estimates and cost math — plain numbers, no magic.

Prices are USD per 1 million tokens, from public OpenAI pricing at the
time of writing (Nov 2026). They are estimates, not a live price feed:
if the provider changes prices, this table goes stale. That is a known
limitation, documented in DECISIONS.md.
"""

# model -> {"input": $/1M input tokens, "output": $/1M output tokens}
PRICES_USD_PER_MILLION = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    # The mock provider is free — but only when the caller actually asked
    # for the "mock" model. If a caller asked for gpt-4o-mini and the
    # gateway fell back to the mock, they are still charged gpt-4o-mini
    # rates (see app.py). Otherwise a caller could dodge their budget by
    # forcing a fallback, and the budget could never be exhausted in
    # mock mode — which would make the 402 path untestable.
    "mock": {"input": 0.0, "output": 0.0},
}

DEFAULT_MODEL = "gpt-4o-mini"

# How many completion tokens we *reserve* in the pre-flight budget check.
# The real reply length is unknown before the provider answers, so we
# reserve a fixed allowance (64 tokens — roughly a short reply). The
# actual cost is charged after the call; if the real reply is longer,
# the key simply has less left for the next request. Tradeoff: a small
# allowance keeps short calls usable on tiny budgets, but a very long
# reply can push a key slightly over budget before the next request
# blocks it. See DECISIONS.md.
ESTIMATED_COMPLETION_TOKENS = 64


def estimate_tokens(text):
    """Rough token estimate: ~4 characters per token for English text.
    This is the standard back-of-the-envelope rule (OpenAI's own
    tokenizer guidance). It is an *estimate* — real token counts come
    from the provider's `usage` field when one is available."""
    if not text:
        return 1
    return max(1, -(-len(text) // 4))  # ceil(len / 4)


def price_for(model):
    """Price table entry for a model; unknown models fall back to the
    default model's prices (never crash on an unknown model name)."""
    return PRICES_USD_PER_MILLION.get(model, PRICES_USD_PER_MILLION[DEFAULT_MODEL])


def cost_usd(model, prompt_tokens, completion_tokens):
    """Cost of a finished request, in USD."""
    price = price_for(model)
    return (prompt_tokens * price["input"]
            + completion_tokens * price["output"]) / 1_000_000


def estimated_cost_usd(model, prompt_tokens):
    """Cost of a request we have not made yet: prompt + reserved completion."""
    return cost_usd(model, prompt_tokens, ESTIMATED_COMPLETION_TOKENS)

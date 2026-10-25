"""Approximate price table, per 1M tokens, used only for cost estimation.
Values are rough public list prices; subscription providers (codex / zcode /
llm-web-gateway) are billed as subscription and cost 0 here."""
import re

# (provider id or None, model regex, input, output, currency)
PRICE_ROWS = [
    ("deepseek", r"deepseek-chat", 2.0, 8.0, "CNY"),
    ("deepseek", r"deepseek-reasoner", 4.0, 16.0, "CNY"),
    (None, r"^gpt-5\.1$", 1.25, 10.0, "USD"),
    (None, r"^gpt-5$", 1.25, 10.0, "USD"),
    (None, r"^gpt-5-codex", 1.25, 10.0, "USD"),
    (None, r"^gpt-4o$", 2.5, 10.0, "USD"),
    (None, r"^gpt-4o-mini$", 0.15, 0.6, "USD"),
    (None, r"^o4-mini$", 1.1, 4.4, "USD"),
    (None, r"claude-.*opus", 15.0, 75.0, "USD"),
    (None, r"claude-.*sonnet", 3.0, 15.0, "USD"),
    (None, r"claude-.*haiku", 1.0, 5.0, "USD"),
    (None, r"^gemini-2\.5-pro", 1.25, 10.0, "USD"),
    (None, r"^gemini-2\.5-flash", 0.3, 2.5, "USD"),
    (None, r"^grok-4", 3.0, 15.0, "USD"),
    (None, r"^kimi-k2", 4.0, 16.0, "CNY"),
    (None, r"^glm-4\.6", 2.0, 6.0, "CNY"),
    (None, r"^glm-4\.5", 2.0, 6.0, "CNY"),
    (None, r"^qwen3?-max", 2.4, 9.6, "CNY"),
    (None, r"^qwen3?-plus", 0.8, 2.0, "CNY"),
    (None, r"^deepseek-v", 2.0, 8.0, "CNY"),
]


def estimate(provider_id, model, in_tok, out_tok, billing):
    if billing == "subscription":
        return 0.0, 0.0, None
    for pid, pat, pin, pout, cur in PRICE_ROWS:
        if pid and pid != provider_id:
            continue
        if re.search(pat, model or ""):
            return in_tok / 1e6 * pin, out_tok / 1e6 * pout, cur
    return 0.0, 0.0, None

#!/usr/bin/env python3
"""Pricing table + savings calc. Args: <current_tier> <suggested_tier> <prompt_chars>.
Output: "<savings_pct>" (positive = savings, negative = extra cost)."""
import sys

PRICE = {
    "haiku":  {"in": 1.0,  "out": 5.0},
    "sonnet": {"in": 3.0,  "out": 15.0},
    "opus":   {"in": 15.0, "out": 75.0},
}

OUT_TO_IN_RATIO = 2.0
CHARS_PER_TOKEN = 4


def cost(tier: str, in_tok: int, out_tok: int) -> float:
    p = PRICE[tier]
    return (in_tok / 1_000_000) * p["in"] + (out_tok / 1_000_000) * p["out"]


def savings_pct(current: str, suggested: str, prompt_chars: int) -> float:
    in_tok = max(prompt_chars // CHARS_PER_TOKEN, 50)
    out_tok = int(in_tok * OUT_TO_IN_RATIO)
    cur = cost(current, in_tok, out_tok)
    sug = cost(suggested, in_tok, out_tok)
    if cur == 0:
        return 0.0
    return (cur - sug) / cur * 100


def tier_of(model_id: str) -> str:
    m = (model_id or "").lower()
    if "haiku" in m:
        return "haiku"
    if "opus" in m:
        return "opus"
    return "sonnet"


if __name__ == "__main__":
    cur = sys.argv[1]
    sug = sys.argv[2]
    chars = int(sys.argv[3]) if len(sys.argv) > 3 else 200
    print(f"{savings_pct(cur, sug, chars):.0f}")

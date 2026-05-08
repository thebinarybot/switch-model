#!/usr/bin/env python3
"""Effort-level picker for routed prompts.

Maps (tier, prompt characteristics) -> effort level for Claude's --effort flag.
Higher effort = more reasoning tokens = better answers on hard problems but more cost.

CLI: lib/effort.py <tier>
     reads prompt from stdin, prints effort level on stdout.

Levels: low, medium, high, xhigh, max
"""
import re
import sys

LEVELS = ("low", "medium", "high", "xhigh", "max")

DEEP_KEYWORDS = re.compile(
    r"\b(deep\s+dive|deeply|exhaustive|comprehensive|thorough|"
    r"step\s+by\s+step|step-by-step|detailed\s+analysis|"
    r"all\s+(edge\s+cases|implications|tradeoffs)|"
    r"prove|formal|rigorous)\b",
    re.IGNORECASE,
)
HIGH_KEYWORDS = re.compile(
    r"\b(audit|security\s+review|architect|architecture|"
    r"design\b.*?\b(system|api|protocol|service|architecture|infra|platform|schema|database)|"
    r"\bdesign\s+a\b|"
    r"root\s+cause|investigate|tradeoff|trade-?off|"
    r"why\s+(does|did|would|should)|how\s+would|"
    r"analyze|evaluate|compare|critique|propose|"
    r"plan\s+(out|the|a)\s+\w+\s+(system|architecture))\b",
    re.IGNORECASE,
)
MEDIUM_KEYWORDS = re.compile(
    r"\b(implement|build|refactor|migrate|debug|"
    r"design\s+(class|function|module)|\bplan\b|review\s+code|"
    r"test\s+strategy)\b",
    re.IGNORECASE,
)
LOW_KEYWORDS = re.compile(
    r"\b(rename|list|show|cat|read|grep|find|"
    r"format|lint|fix\s+typo|reformat|echo|"
    r"what\s+is|where\s+is|when\s+(was|did)|who\s+is|"
    r"copy|move|delete|print)\b",
    re.IGNORECASE,
)

# Tier defaults: balance cost vs typical task profile
TIER_DEFAULTS = {
    "haiku": "low",
    "sonnet": "medium",
    "opus": "medium",
}

# Tier ceilings: don't exceed (effort beyond this point is rarely useful)
TIER_CEILINGS = {
    "haiku": "low",        # small model, extra effort doesn't help much
    "sonnet": "high",      # cap at high; xhigh/max better used on opus
    "opus": "max",         # full range available
}


def _clamp(level: str, ceiling: str) -> str:
    if LEVELS.index(level) > LEVELS.index(ceiling):
        return ceiling
    return level


def pick_effort(tier: str, prompt: str) -> str:
    text = (prompt or "").strip()
    base = TIER_DEFAULTS.get(tier, "medium")

    if not text:
        return _clamp(base, TIER_CEILINGS.get(tier, "high"))

    # Strongest signal first
    if DEEP_KEYWORDS.search(text):
        level = "xhigh"
        keyword_hit = True
    elif HIGH_KEYWORDS.search(text):
        level = "high"
        keyword_hit = True
    elif MEDIUM_KEYWORDS.search(text):
        level = "medium"
        keyword_hit = True
    elif LOW_KEYWORDS.search(text):
        level = "low"
        keyword_hit = True
    else:
        level = base
        keyword_hit = False

    # Length nudges only when no keyword pinned the level.
    n = len(text)
    if not keyword_hit:
        if n > 1500:
            level = "high"
        elif n < 60:
            level = "low"

    # Long prompts always escalate at least one step beyond keyword default.
    if keyword_hit and n > 1500 and LEVELS.index(level) < LEVELS.index("high"):
        level = "high"

    return _clamp(level, TIER_CEILINGS.get(tier, "high"))


def main() -> int:
    if len(sys.argv) < 2:
        print("medium")
        return 0
    tier = sys.argv[1].strip().lower()
    prompt = sys.stdin.read()
    print(pick_effort(tier, prompt))
    return 0


if __name__ == "__main__":
    sys.exit(main())

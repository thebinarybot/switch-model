#!/usr/bin/env python3
"""Hybrid prompt classifier: heuristics + haiku fallback + transcript context.

stdin: prompt text.
stdout: tier name (haiku/sonnet/opus).
stderr: optional debug trace if SWITCH_MODEL_DEBUG=1.

Env:
  SWITCH_MODEL_NO_LLM=1            skip haiku fallback (heuristics only)
  SWITCH_MODEL_DEBUG=1             print decision trace to stderr
  SWITCH_MODEL_NO_CONTEXT=1        skip transcript context analysis
  SWITCH_MODEL_TRANSCRIPT_PATH     path to session JSONL (set by router.sh)
"""
import json as _json
import os
import re
import subprocess
import sys

TIER_PATTERNS = {
    "haiku": [
        r"\b(rename|format|lint|list|show|read|cat|grep|find|ls|"
        r"add\s+comment|fix\s+typo|reword|reformat|"
        r"what\s+is|where\s+is|when\s+(was|did)|who\s+is|"
        r"echo|print|copy|move|delete\s+file)\b",
    ],
    "opus": [
        r"\b(design|architect|plan|research|analyze|compare|"
        r"evaluate|tradeoff|trade-?off|why\s+(does|did|would|should)|"
        r"explain\s+(how|why)|review|audit|strategize|"
        r"investigate|root\s+cause|propose|brainstorm|deep\s+dive|"
        r"think\s+through|figure\s+out|reason\s+about|debate|"
        r"refactor\s+(architecture|design)|security\s+review)\b",
    ],
    "sonnet": [
        r"\b(implement|build|write|edit|fix\s+bug|add\s+feature|"
        r"refactor|test|debug|update|migrate|create|"
        r"convert|translate|generate|scaffold|wire\s+up|"
        r"port|patch|rewrite|extract|extend)\b",
    ],
}

PRIORITY = ["opus", "sonnet", "haiku"]
LONG_PROMPT_CHARS = 600
HAIKU_TIMEOUT_SEC = 15

LLM_PROMPT_TEMPLATE = """You are a tier classifier. Reply with exactly ONE WORD from: haiku, sonnet, opus.

Tiers:
- haiku: trivial mechanical (rename, list, lookup, format, simple Q&A)
- sonnet: standard implementation/edit/test/debug (default for code work)
- opus: deep reasoning, architecture, design, research, audit, security review, root-cause analysis

Classify this prompt and reply with ONLY the tier name, nothing else.

Prompt to classify: \"\"\"{prompt}\"\"\"

Tier:"""


def debug(msg: str) -> None:
    if os.environ.get("SWITCH_MODEL_DEBUG") == "1":
        print(f"[classify] {msg}", file=sys.stderr)


def heuristic(prompt: str) -> tuple[str, float, set]:
    """Return (tier, confidence, matched_tiers).

    Confidence:
      0.9 → exactly one tier matched (clear winner)
      0.6 → multiple tiers matched (priority resolves but ambiguous)
      0.3 → no matches (default sonnet, low confidence)
    """
    text = prompt.lower().strip()
    matched = set()
    for tier, patterns in TIER_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                matched.add(tier)
                break

    if not matched:
        tier = "sonnet"
        conf = 0.3
    elif len(matched) == 1:
        tier = next(iter(matched))
        conf = 0.9
    else:
        for t in PRIORITY:
            if t in matched:
                tier = t
                break
        conf = 0.6

    if len(text) > LONG_PROMPT_CHARS:
        if tier == "haiku":
            tier = "sonnet"
            conf = min(conf, 0.6)
        elif tier == "sonnet" and re.search(r"\?|why|how|should|design", text):
            tier = "opus"
            conf = min(conf, 0.6)

    return tier, conf, matched


def haiku_classify(prompt: str) -> str | None:
    """Call haiku for tier classification. Returns tier or None on failure."""
    try:
        env = os.environ.copy()
        env["MODEL_ROUTER_BYPASS"] = "1"
        wrapped = LLM_PROMPT_TEMPLATE.format(prompt=prompt.replace('"""', '"'))
        result = subprocess.run(
            [
                "claude", "-p",
                "--model", "haiku",
                "--output-format", "json",
                wrapped,
            ],
            capture_output=True,
            text=True,
            timeout=HAIKU_TIMEOUT_SEC,
            env=env,
        )
        if result.returncode != 0:
            debug(f"haiku call failed rc={result.returncode}: {result.stderr[:200]}")
            return None
        import json
        data = json.loads(result.stdout)
        answer = (data.get("result") or "").strip().lower()
        for tier in ("haiku", "sonnet", "opus"):
            if tier in answer:
                return tier
        debug(f"haiku unparseable answer: {answer!r}")
        return None
    except subprocess.TimeoutExpired:
        debug("haiku call timed out")
        return None
    except Exception as e:
        debug(f"haiku call error: {e}")
        return None


def load_context() -> dict:
    """Read session signals from transcript via context.py. Empty on failure."""
    if os.environ.get("SWITCH_MODEL_NO_CONTEXT") == "1":
        return {}
    path = os.environ.get("SWITCH_MODEL_TRANSCRIPT_PATH", "")
    if not path or not os.path.isfile(path):
        return {}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from context import analyze
        return analyze(path)
    except Exception as e:
        debug(f"context load failed: {e}")
        return {}


def apply_context(tier: str, conf: float, ctx: dict) -> tuple[str, float]:
    """Adjust tier/conf based on session signals. Pure function."""
    if not ctx or ctx.get("turn_count", 0) == 0:
        return tier, conf

    errors = ctx.get("recent_errors", 0)
    edits = ctx.get("recent_edits", 0)
    bash = ctx.get("recent_bash", 0)
    streak = ctx.get("consecutive_same_tier", 0)
    last_tier = ctx.get("last_tier")
    tools = ctx.get("recent_tools", 0)

    # Heavy debugging: 3+ tool errors recently → bump up
    if errors >= 3 and tier in ("haiku", "sonnet"):
        new = "opus" if tier == "sonnet" else "sonnet"
        debug(f"context: {errors} recent errors → bump {tier}→{new}")
        tier = new
        conf = max(conf, 0.7)

    # High edit/bash velocity: active code work, never haiku
    if (edits + bash) >= 5 and tier == "haiku":
        debug(f"context: {edits} edits + {bash} bash → bump haiku→sonnet")
        tier = "sonnet"
        conf = max(conf, 0.7)

    # Anti-flap: 5+ consecutive turns on same tier and current heuristic is low-confidence
    if streak >= 5 and conf < 0.9 and last_tier:
        # Only lock if streak tier is same-or-higher than current suggestion (avoid downgrading active sessions)
        order = {"haiku": 0, "sonnet": 1, "opus": 2}
        if order.get(last_tier, 1) >= order.get(tier, 1):
            debug(f"context: streak={streak} on {last_tier} → lock to {last_tier}")
            tier = last_tier
            conf = 0.9

    # Many tool calls without errors: routine codework, prefer sonnet
    if tools >= 8 and errors == 0 and tier == "opus" and conf < 0.9:
        debug(f"context: {tools} routine tool calls → downgrade opus→sonnet")
        tier = "sonnet"

    return tier, conf


def classify(prompt: str) -> str:
    tier, conf, matched = heuristic(prompt)
    debug(f"heuristic: tier={tier} conf={conf} matched={matched}")

    ctx = load_context()
    if ctx:
        debug(f"context: {ctx}")
        tier, conf = apply_context(tier, conf, ctx)
        debug(f"after context: tier={tier} conf={conf}")

    if conf >= 0.9:
        return tier

    if os.environ.get("SWITCH_MODEL_NO_LLM") == "1":
        debug("LLM fallback disabled, using heuristic result")
        return tier

    debug("escalating to haiku classifier")
    llm_tier = haiku_classify(prompt)
    if llm_tier:
        debug(f"haiku decided: {llm_tier}")
        return llm_tier

    debug("haiku failed, falling back to heuristic result")
    return tier


if __name__ == "__main__":
    prompt = sys.stdin.read()
    print(classify(prompt))

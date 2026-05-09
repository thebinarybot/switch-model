#!/usr/bin/env python3
"""Follow-up detector. Decides if a prompt is a continuation of a prior turn
that the subprocess can't answer correctly without the main session's context.

If true, router.sh exits 0 → main session handles natively (with full history).

stdin: prompt text.
exit 0: not a follow-up (continue routing).
exit 1: is a follow-up (router should skip).
stdout: short reason if follow-up, else empty.

Env:
  SWITCH_MODEL_NO_FOLLOWUP=1   disable the detector (always treat as not-followup)
  SWITCH_MODEL_DEBUG=1         print detection trace to stderr
"""
import os
import re
import sys

SHORT_PROMPT_CHARS = 40

PRONOUN_START = re.compile(
    r"^\s*(it|that|this|they|them|those|these|he|she|him|her)\b",
    re.IGNORECASE,
)

CONTINUATION_CUES = re.compile(
    r"^\s*("
    r"now\s+(also|do|make|change|fix|add|remove|try)|"
    r"and\s+(also|now|then)|"
    r"also\s|"
    r"but\s+(what\s+about|wait)|"
    r"instead\b|"
    r"undo\b|redo\b|"
    r"again\b|"
    r"keep\s+going|continue\b|"
    r"more\s+of\s+(that|the\s+same)|"
    r"another\s+(one|example)|"
    r"do\s+the\s+same"
    r")",
    re.IGNORECASE,
)

REFERENTIAL_WORDS = re.compile(
    r"\b(previous|above|last|earlier|prior)\s+(answer|response|message|"
    r"version|attempt|one|reply|output|result)\b",
    re.IGNORECASE,
)

# Bare references with no concrete subject ("show me more", "redo it",
# "explain again"). Distinct from CONTINUATION_CUES because they appear
# anywhere in the prompt, not just at the start.
ANAPHORIC_BARE = re.compile(
    r"\b(show\s+me\s+more|tell\s+me\s+more|explain\s+(it|that)\s+again|"
    r"why\s+is\s+(it|that)|what\s+about\s+(it|that)|"
    r"do\s+(it|that)\s+(again|differently)|fix\s+(it|that))\b",
    re.IGNORECASE,
)


def debug(msg: str) -> None:
    if os.environ.get("SWITCH_MODEL_DEBUG") == "1":
        print(f"[followup] {msg}", file=sys.stderr)


def is_followup(prompt: str) -> tuple[bool, str]:
    """Return (is_followup, reason).

    Conservative — only flags strong, unambiguous follow-ups. Prefers false
    negatives (route a follow-up to subprocess) over false positives (skip
    routing on a self-contained prompt).
    """
    text = (prompt or "").strip()
    if not text:
        return False, ""

    if os.environ.get("SWITCH_MODEL_NO_FOLLOWUP") == "1":
        return False, "disabled via env"

    # 1. Short prompt that opens with a pronoun → almost certainly references
    #    something from a prior turn ("it should be red", "that's wrong").
    if len(text) < SHORT_PROMPT_CHARS and PRONOUN_START.search(text):
        return True, "short pronoun-led prompt"

    # 2. Starts with a continuation cue ("now also do X", "and then Y").
    if CONTINUATION_CUES.search(text):
        return True, "continuation cue at start"

    # 3. References "previous/above/last/earlier <answer/response/message/...>".
    if REFERENTIAL_WORDS.search(text):
        return True, "references previous turn explicitly"

    # 4. Pure anaphoric short phrases ("show me more", "redo it", "fix it").
    if len(text) < 80 and ANAPHORIC_BARE.search(text):
        return True, "bare anaphoric phrase"

    return False, ""


def main() -> int:
    prompt = sys.stdin.read()
    flag, reason = is_followup(prompt)
    debug(f"prompt={prompt[:60]!r} followup={flag} reason={reason!r}")
    if flag:
        print(reason)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

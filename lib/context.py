#!/usr/bin/env python3
"""Transcript signal extractor for context-aware routing.

Reads a Claude Code session JSONL and returns signals about recent activity.
Used by classify.py to adjust tier decisions based on session state.

Signals extracted from the last N assistant turns:
  - turn_count: total assistant turns in transcript
  - recent_tools: number of tool_use blocks
  - recent_errors: number of tool_results with is_error=true
  - recent_edits: number of Edit/Write/MultiEdit tool calls
  - recent_bash: number of Bash tool calls
  - last_model: model id of latest assistant turn
  - last_tier: tier derived from last_model
  - consecutive_same_tier: streak of recent turns on the same tier

CLI: lib/context.py <transcript_path> [--window N]
Output: JSON to stdout. Empty {} on missing/unreadable file.
"""
import json
import os
import sys

DEFAULT_WINDOW = 10
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _tier_of(model_id: str) -> str:
    m = (model_id or "").lower()
    if "haiku" in m:
        return "haiku"
    if "opus" in m:
        return "opus"
    return "sonnet"


def analyze(transcript_path: str, window: int = DEFAULT_WINDOW) -> dict:
    if not transcript_path or not os.path.isfile(transcript_path):
        return {}

    assistants: list[dict] = []
    user_tool_results: list[dict] = []

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get("type")
                if t == "assistant":
                    msg = o.get("message", {}) or {}
                    assistants.append({
                        "model": msg.get("model", ""),
                        "content": msg.get("content", []) or [],
                        "stop_reason": msg.get("stop_reason"),
                    })
                elif t == "user":
                    msg = o.get("message", {}) or {}
                    content = msg.get("content")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "tool_result":
                                user_tool_results.append({
                                    "is_error": bool(c.get("is_error", False)),
                                })
    except Exception:
        return {}

    turn_count = len(assistants)
    if turn_count == 0:
        return {"turn_count": 0}

    recent = assistants[-window:]
    recent_errors_window = user_tool_results[-window * 2 :]

    tools = 0
    edits = 0
    bash = 0
    for a in recent:
        for c in a.get("content", []):
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use":
                tools += 1
                name = c.get("name", "")
                if name in EDIT_TOOLS:
                    edits += 1
                elif name == "Bash":
                    bash += 1

    errors = sum(1 for r in recent_errors_window if r.get("is_error"))

    last_model = recent[-1].get("model", "")
    last_tier = _tier_of(last_model)

    streak = 0
    for a in reversed(assistants):
        if _tier_of(a.get("model", "")) == last_tier:
            streak += 1
        else:
            break

    return {
        "turn_count": turn_count,
        "recent_tools": tools,
        "recent_errors": errors,
        "recent_edits": edits,
        "recent_bash": bash,
        "last_model": last_model,
        "last_tier": last_tier,
        "consecutive_same_tier": streak,
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("{}")
        return 0
    path = sys.argv[1]
    window = DEFAULT_WINDOW
    if "--window" in sys.argv:
        i = sys.argv.index("--window")
        if i + 1 < len(sys.argv):
            try:
                window = int(sys.argv[i + 1])
            except ValueError:
                pass
    print(json.dumps(analyze(path, window)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

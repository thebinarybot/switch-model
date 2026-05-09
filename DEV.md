# switch-model — Developer Documentation

Internal architecture and code-level reference for contributors and integrators.

## Plugin layout

```
switch-model/
├── .claude-plugin/
│   └── plugin.json          # plugin manifest (auto-discovers hooks/hooks.json)
├── hooks/
│   ├── hooks.json           # hook registration (UserPromptSubmit)
│   └── router.sh            # main hook entry point (bash)
├── lib/
│   ├── classify.py          # hybrid heuristic + LLM + context classifier
│   ├── context.py           # transcript signal extractor
│   ├── effort.py            # effort-level picker
│   └── pricing.py           # tier pricing + savings calc
├── statusline.sh            # optional statusline (user wires manually)
├── README.md                # quick reference
├── DOC.md                   # high-level overview
└── DEV.md                   # this file
```

The marketplace manifest lives one level up:

```
~/skills/.claude-plugin/marketplace.json
```

## Manifest schemas

### plugin.json

```json
{
  "name": "switch-model",
  "version": "0.1.0",
  "description": "...",
  "author": { "name": "nithin" }
}
```

**Note:** Do NOT add a `"hooks": "./hooks/hooks.json"` field. Claude Code auto-discovers `hooks/hooks.json`; declaring it explicitly causes a "duplicate hooks file" load error.

### hooks/hooks.json

Uses the matcher-grouped schema (the only one Claude Code's Zod validator accepts):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/router.sh" }
        ]
      }
    ]
  }
}
```

The flat `[{"type":"command","command":"..."}]` form fails validation.

### marketplace.json (parent dir)

```json
{
  "name": "switch-model",
  "owner": { "name": "nithin" },
  "plugins": [
    {
      "name": "switch-model",
      "source": "./switch-model",
      "description": "..."
    }
  ]
}
```

## Request lifecycle

```
user types prompt
        |
        v
[Claude Code TUI emits UserPromptSubmit event]
        |
        v
[router.sh receives stdin JSON]
   {prompt, transcript_path, session_id, cwd, permission_mode, hook_event_name}
        |
        v
   recursion guard: if MODEL_ROUTER_BYPASS=1 -> exit 0 (passthrough)
        |
        v
   bypass prefix: if prompt starts with +force/+keep -> exit 0
   (note: ! triggers Claude Code shell mode, # triggers memory shortcut —
    both eat the prompt before the hook fires. + has no special meaning.)
        |
        v
   upgrade-consent prefix: if prompt starts with "+upgrade " ->
     strip prefix, set local FORCE_UPGRADE=1 (skip the upgrade gate later)
        |
        v
   short prompt: if len(prompt) < 8 -> exit 0
        |
        v
   classify.py reads prompt from stdin, returns tier on stdout
   effort.py reads prompt from stdin + tier arg, returns effort on stdout
        |                               |
        |                               v
        |              [stage 1] heuristic regex over TIER_PATTERNS
        |              -> (tier, confidence, matched_set)
        |                  conf 0.9 (one match)
        |                  conf 0.6 (multi match)
        |                  conf 0.3 (no match)
        |                       |
        |                       v
        |              [stage 2] context.analyze(transcript_path)
        |                  reads SWITCH_MODEL_TRANSCRIPT_PATH env
        |                  extracts: turn_count, recent_tools, recent_errors,
        |                            recent_edits, recent_bash, last_model,
        |                            last_tier, consecutive_same_tier
        |                  apply_context() adjusts (tier, conf):
        |                    - 3+ errors -> bump up
        |                    - 5+ edits/bash combined -> never haiku
        |                    - 5+ streak same tier + low conf -> lock
        |                    - 8+ tools no errors + opus + low conf -> downgrade
        |                       |
        |                       v
        |              if conf >= 0.9 -> return tier (skip LLM)
        |              else escalate to haiku_classify:
        |                  MODEL_ROUTER_BYPASS=1 claude -p --model haiku ...
        |                  inline classification prompt, single-word answer
        |              parse first matching tier word from result
        |              fallback to heuristic+context result on error/timeout
        v
   detect current model:
     1. grep latest "model":"claude-..." from transcript_path JSONL
     2. fallback: ~/.claude/settings.json model field
     3. final fallback: "sonnet"
        |
        v
   compute current_tier via pricing.tier_of(model_id)
        |
        +-- if current_tier == suggested -> write state.json status:match (with effort), exit 0
        |
        +-- else (mismatch):
              compute savings_pct  (negative when upgrading)
                |
                +-- upgrade gate: if tier_rank(suggested) > tier_rank(current_tier)
                |     AND FORCE_UPGRADE != 1
                |     AND SWITCH_MODEL_AUTO_UPGRADE != 1
                |     -> emit JSON
                |          {"decision":"block","reason":"...upgrade requires +upgrade prefix..."}
                |          write state.json status:upgrade_pending, exit 0
                |
                +-- otherwise (downgrade, same-cost, or upgrade with consent):
                      spawn: MODEL_ROUTER_BYPASS=1 timeout 120 claude -p --model <suggested> --effort <effort> "<prompt>"
                      (skip --effort flag if SWITCH_MODEL_NO_EFFORT=1)
                      capture stdout
                        |
                        +-- success: emit JSON
                        |     {"decision":"block","reason":"<header>\n\n<answer>\n\n<footer>"}
                        |     header reads "saves ~X%" on downgrades, "costs ~+X%" on upgrades
                        |     write state.json status:routed
                        |
                        +-- failure: emit JSON
                              {"decision":"block","reason":"...subprocess failed; run /model X manually..."}
                              write state.json status:failed
        |
        v
   exit 0
```

## Classifier (`lib/classify.py`)

### Heuristic stage

Three tier patterns (haiku / sonnet / opus). Each is a single multiline regex compiled per call. A prompt matches a tier if any keyword in that tier's regex hits.

Confidence:

| matched tiers | confidence | next step                    |
|---------------|------------|------------------------------|
| 1             | 0.9        | return tier (no LLM call)    |
| 2 or 3        | 0.6        | escalate to LLM              |
| 0             | 0.3        | escalate (fallback: sonnet)  |

PRIORITY ordering (`opus` > `sonnet` > `haiku`) resolves multi-match before escalation, so the heuristic tier is still set even when escalating — used as the LLM-failure fallback.

Long-prompt adjustment: if `len(prompt) > 600`:
- haiku → bumped to sonnet
- sonnet with `?|why|how|should|design` → bumped to opus

### LLM fallback stage

`haiku_classify(prompt)` spawns:

```
MODEL_ROUTER_BYPASS=1 claude -p --model haiku --output-format json <wrapped_prompt>
```

`wrapped_prompt` is constructed inline (no `--system-prompt` override) using `LLM_PROMPT_TEMPLATE`:

```
You are a tier classifier. Reply with exactly ONE WORD from: haiku, sonnet, opus.

Tiers:
- haiku: trivial mechanical (rename, list, lookup, format, simple Q&A)
- sonnet: standard implementation/edit/test/debug (default for code work)
- opus: deep reasoning, architecture, design, research, audit, security review, root-cause analysis

Classify this prompt and reply with ONLY the tier name, nothing else.

Prompt to classify: """<prompt>"""

Tier:
```

Inline wrapping was chosen over `--append-system-prompt` because Claude Code's default system prompt overrides any appended classifier instructions, and `--system-prompt` strips harness functionality.

The result string is parsed by `for tier in ('haiku','sonnet','opus'): if tier in answer: return tier`. First match wins. Unparseable replies fall back to the heuristic tier.

`HAIKU_TIMEOUT_SEC = 15` caps the subprocess wall time; a timeout falls back to heuristic.

### Env knobs

| Var                            | Effect                                              |
|--------------------------------|-----------------------------------------------------|
| `SWITCH_MODEL_NO_LLM=1`         | Skip the LLM stage; heuristic + context only        |
| `SWITCH_MODEL_NO_CONTEXT=1`     | Skip transcript context analysis (stage 2)          |
| `SWITCH_MODEL_NO_EFFORT=1`      | Skip effort selection; subprocess uses Claude default |
| `SWITCH_MODEL_AUTO_UPGRADE=1`   | Skip the upgrade-confirmation gate (silent upgrades)|
| `SWITCH_MODEL_DEBUG=1`          | Print classify decisions to stderr (hook log)       |
| `SWITCH_MODEL_TRANSCRIPT_PATH`  | Path to session JSONL; set by router.sh             |
| `MODEL_ROUTER_BYPASS=1`        | Recursion guard; subprocess sets this               |

## Context analyzer (`lib/context.py`)

Reads the session JSONL line-by-line, extracts assistant turns and user tool_results, and computes signals over a window (default last 10 assistant turns).

Output dict:

```python
{
  "turn_count": int,                # total assistant turns
  "recent_tools": int,              # tool_use blocks in window
  "recent_errors": int,             # tool_results with is_error=true (window*2 user msgs)
  "recent_edits": int,              # Edit/Write/MultiEdit/NotebookEdit calls
  "recent_bash": int,               # Bash calls
  "last_model": str,                # claude-opus-4-7 etc.
  "last_tier": str,                 # haiku|sonnet|opus
  "consecutive_same_tier": int,     # streak length from tail
}
```

Empty `{}` returned for missing/unreadable file or 0 assistant turns. CLI usage:

```bash
python3 lib/context.py /path/to/session.jsonl [--window 10]
```

`apply_context(tier, conf, ctx)` is a pure function in `classify.py` that consumes the dict and returns adjusted `(tier, conf)`. Adjustment rules listed in DEV.md flow diagram and README context section.

### Adjustment rules (current)

```
errors >= 3 and tier in (haiku, sonnet)
  -> bump tier one step up, conf >= 0.7

edits + bash >= 5 and tier == haiku
  -> tier = sonnet, conf >= 0.7

streak >= 5 and conf < 0.9 and last_tier >= current tier
  -> tier = last_tier, conf = 0.9   (anti-flap lock)

tools >= 8 and errors == 0 and tier == opus and conf < 0.9
  -> tier = sonnet (downgrade for routine codework)
```

Tie-break: rules apply in order. Later rules can override earlier ones if their conditions hold.

## Effort picker (`lib/effort.py`)

Maps `(tier, prompt)` → one of `low | medium | high | xhigh | max`. Used as `--effort` value on the routed subprocess.

### Decision rules

```
Keyword groups (first match wins):
  DEEP    → xhigh    (deep dive, exhaustive, comprehensive, prove, formal, rigorous)
  HIGH    → high     (audit, security review, architect, design a <noun>, root cause, why does, tradeoff, analyze, evaluate, compare, critique, propose)
  MEDIUM  → medium   (implement, build, refactor, migrate, debug, plan, review code, test strategy, design class/function/module)
  LOW     → low      (rename, list, show, cat, read, grep, find, format, lint, fix typo, what is, where is, copy, move, delete, print)

If no keyword hit, use TIER_DEFAULTS[tier] then nudge by length:
  prompt > 1500 chars → high
  prompt < 60   chars → low

If keyword hit AND prompt > 1500 chars → bump to at least high.

Final clamp to TIER_CEILINGS[tier]:
  haiku  → low
  sonnet → high
  opus   → max
```

### Tier defaults / ceilings

```python
TIER_DEFAULTS = { "haiku": "low", "sonnet": "medium", "opus": "medium" }
TIER_CEILINGS = { "haiku": "low", "sonnet": "high",   "opus": "max"    }
```

CLI usage:

```bash
echo "audit auth code for security" | python3 lib/effort.py opus    # → high
echo "rename foo to bar"             | python3 lib/effort.py haiku   # → low
echo "deep dive consensus algorithm" | python3 lib/effort.py opus    # → xhigh
```

### Why per-tier ceilings

- `haiku → low`: Haiku is a small model; extra reasoning tokens rarely improve answer quality but cost the same.
- `sonnet → high`: `xhigh` and `max` reasoning are designed for Opus's deeper layers. On Sonnet they spend tokens without proportional gain.
- `opus → max`: Full range available. Deep research / formal analysis can justify max.

## Pricing (`lib/pricing.py`)

Hardcoded table per 1M tokens (USD):

```python
PRICE = {
  "haiku":  {"in": 1.0,  "out": 5.0},
  "sonnet": {"in": 3.0,  "out": 15.0},
  "opus":   {"in": 15.0, "out": 75.0},
}
```

Token estimate: `prompt_chars / 4` (rough), output tokens estimated as `2× input`. `savings_pct(current, suggested, prompt_chars)` returns:

```python
(cost_current - cost_suggested) / cost_current * 100
```

Negative values = cost increase (when routing UP from haiku to opus).

`tier_of(model_id)` substring-matches `haiku`/`opus` in the lowercased ID, defaults to `sonnet`.

When Anthropic changes pricing, update the `PRICE` dict.

## Hook script (`hooks/router.sh`)

Bash. Key sections:

- **stdin parsing** — extracts `prompt`, `transcript_path`, `session_id` via inline `python3 -c`. Bash `jq` would be cleaner but adds a dependency.
- **bypass / consent prefixes** — `+force ` and `+keep ` exit 0 immediately (passthrough). `+upgrade ` strips the prefix and sets a local `FORCE_UPGRADE=1` flag that the upgrade gate later checks.
- **classify** — pipes prompt to `python3 $PLUGIN_ROOT/lib/classify.py`. Result captured.
- **current model detection** — `grep -o '"model":"claude-[a-z0-9-]*"' "$TRANSCRIPT" | tail -1`. First-prompt sessions have empty/missing transcript; falls back to `settings.json`.
- **tier_rank()** — bash function mapping `haiku=0`, `sonnet=1`, `opus=2`. Used to detect upgrade direction (suggested rank > current rank).
- **upgrade gate** — only fires on tier mismatch + `tier_rank(suggested) > tier_rank(current)`. Skipped when `FORCE_UPGRADE=1` or `SWITCH_MODEL_AUTO_UPGRADE=1`. Emits a `decision:block` reason instructing the user to re-submit with `+upgrade ` (or `+force `, or set the env var). State written as `status:upgrade_pending`.
- **state.json write** — JSON with `current`, `suggested`, `status` (`match` / `routed` / `failed` / `upgrade_pending`), and `savings`.
- **subprocess spawn** — `MODEL_ROUTER_BYPASS=1 timeout 120 claude -p --model <suggested> "<prompt>"`. Output captured. Recursion guarded by env var.
- **block emission** — Python heredoc emits `{"decision":"block","reason":"..."}` JSON on stdout. Header reads `saves ~X%` when `savings_pct >= 0` and `costs ~+X%` when negative (upgrade-routed answers). Exit 0.

The `printf '%s' "$INPUT" | python3 -c "..."` pattern avoids shell-escape issues with prompt content.

## State files

| Path                              | Contents                                       |
|-----------------------------------|------------------------------------------------|
| `~/.cache/switch-model/state.json` | `{current, suggested, status, effort, savings?}` — `status` ∈ `match` / `routed` / `failed` / `upgrade_pending` |
| `~/.cache/switch-model/router.log` | stderr of subprocess + classify.py debug       |

State is written every hook fire (match cases too) so statusline always has fresh data.

## Statusline (`statusline.sh`)

Reads stdin JSON from Claude Code containing:

```json
{
  "model": { "display_name": "Sonnet 4.6", "id": "claude-sonnet-4-6" },
  "context_window": { "used_percentage": 42 },
  "cost": { "total_cost_usd": 0.12 },
  "session_id": "...",
  "workspace": { "current_dir": "..." }
}
```

Combines `model.display_name` with the last decision from `state.json`. Output formats:

- match / no state: `[Sonnet 4.6]`
- routed: `[Sonnet 4.6] [router →haiku saves ~67%]`
- failed: `[Sonnet 4.6] [router !haiku (manual)]`

**Conflict warning:** Claude Code allows only one statusline. If the user already has another (e.g. `caveman`), they must merge manually. Plugin can NOT auto-bind statusline; it's a `settings.json` user-level setting.

## Subprocess/recursion model

The hook spawns `claude` subprocesses for two reasons:

1. **Classifier** — `claude -p --model haiku` for ambiguous prompts.
2. **Routed answer** — `claude -p --model <suggested>` for the actual response.

Both inherit env from the hook process. `MODEL_ROUTER_BYPASS=1` is set for both, so when the subprocess fires its own UserPromptSubmit, `router.sh`'s first check returns immediately.

The subprocess is non-interactive (`-p`) and writes its output to stdout. The hook captures it via shell command substitution.

For the routed answer, output is wrapped in `{"decision":"block","reason":"..."}` and printed to stdout. Claude Code's TUI renders the `reason` as a system-style message and shows the original prompt under it. The main session's model never sees the prompt.

## Why subprocess + block reason instead of model switch

Investigated alternatives:

| Approach                          | Verdict                                     |
|-----------------------------------|---------------------------------------------|
| Hook switches model programmatically | NOT POSSIBLE — no documented API         |
| Custom keybinding chains `/model` + submit | NOT POSSIBLE — keybindings are namespaced action dispatch only, no shell/slash-command invocation, no input-buffer access, no plugin bundling |
| Hook rewrites prompt              | NOT POSSIBLE — only block / additionalContext / sessionTitle |
| Subprocess writes to active session JSONL via `--resume` | TUI doesn't live-render filesystem changes; injected turn invisible |
| Subprocess + return as block reason | **WORKS** — current implementation         |

These constraints are documented at https://code.claude.com/docs/en/hooks.md and https://code.claude.com/docs/en/keybindings.md.

## Install / lifecycle

### Plugin install (permanent)

```bash
claude plugin marketplace add ~/skills
claude plugin install switch-model@switch-model
claude plugin list                # confirm enabled
```

### Per-session test mode

```bash
claude --plugin-dir ~/skills/switch-model
```

### Reload semantics

| Edited file                | Reload required?                       |
|----------------------------|----------------------------------------|
| `lib/*.py`                 | No — read fresh each invocation        |
| `hooks/router.sh`          | No — read fresh each invocation        |
| `hooks/hooks.json`         | Yes — restart session (or disable+enable) |
| `.claude-plugin/plugin.json` | Yes — restart session                |
| Marketplace manifest       | Yes — `claude plugin marketplace update` |

Disable / re-enable:

```bash
claude plugin disable switch-model@switch-model
claude plugin enable  switch-model@switch-model
```

Uninstall:

```bash
claude plugin uninstall switch-model@switch-model
```

## Debug

Enable Claude Code debug + classify trace:

```bash
SWITCH_MODEL_DEBUG=1 claude --debug
```

Hook stderr lands in:

```
~/.claude/debug/<session-id>.txt
```

Manually run hook:

```bash
echo '{"prompt":"design a system","session_id":"x","transcript_path":"/dev/null","cwd":"/tmp","hook_event_name":"UserPromptSubmit"}' \
  | CLAUDE_PLUGIN_ROOT=~/skills/switch-model bash ~/skills/switch-model/hooks/router.sh
```

Manually run classifier:

```bash
echo "design and implement auth" | SWITCH_MODEL_DEBUG=1 python3 ~/skills/switch-model/lib/classify.py
```

Manually compute savings:

```bash
python3 ~/skills/switch-model/lib/pricing.py opus haiku 200
```

Inspect state:

```bash
cat ~/.cache/switch-model/state.json
tail -50 ~/.cache/switch-model/router.log
```

## Extension points

| Want to...                                   | Edit                                  |
|----------------------------------------------|---------------------------------------|
| Add new keyword rules                        | `lib/classify.py` → `TIER_PATTERNS`   |
| Change long-prompt threshold                 | `LONG_PROMPT_CHARS`                   |
| Tune confidence thresholds for escalation    | `classify()` body                     |
| Add new escalation policy (e.g. embeddings)  | Replace `haiku_classify()`            |
| Add new context signals                      | `lib/context.py` → `analyze()`        |
| Add new context-based tier rules             | `lib/classify.py` → `apply_context()` |
| Change context window size                   | `lib/context.py` → `DEFAULT_WINDOW`   |
| Tune effort keywords                         | `lib/effort.py` → `*_KEYWORDS`        |
| Change tier effort defaults / ceilings       | `lib/effort.py` → `TIER_DEFAULTS`, `TIER_CEILINGS` |
| Update pricing                               | `lib/pricing.py` → `PRICE`            |
| Change block-reason formatting               | `hooks/router.sh` Python heredoc      |
| Add new env knobs                            | `lib/classify.py` and document in README/DEV |
| Change recursion guard env var name          | `hooks/router.sh` and `classify.haiku_classify` (must stay in sync) |
| Tune upgrade-gate behavior / message         | `hooks/router.sh` upgrade-gate block (after `tier_rank()`)          |
| Add new bypass / consent prefixes            | `hooks/router.sh` near existing `+force` / `+keep` / `+upgrade` checks |

## Known issues

- **Cold-start latency:** First subprocess in a session pays full system-prompt cache creation (~$0.04, ~6s). Subsequent calls hit cache (~$0.013, ~2s API).
- **Long answers:** Block-reason text > a few KB renders awkwardly in TUI; no scrollback within block.
- **Non-English prompts:** Heuristic patterns are English-keyword. Falls through to LLM more often (extra latency).
- **Code-only prompts:** Pasted code with no descriptive verbs falls to "no match → LLM escalation". Could improve by adding code-shape detectors.
- **Multi-turn opus chain:** Subprocess answers don't enter main-session in-memory transcript. Follow-up Q's lose context. To work around, user can `/model opus` manually for sustained opus chains.
- **`/cost` undercount:** Subprocess token usage isn't reflected in main-session cost output.
- **Hardcoded pricing:** Manual update needed when Anthropic changes prices.

## Testing

No automated test suite yet. Manual test recipes:

```bash
# unit: classifier
for p in "rename foo" "design auth" "fix bug" "make sandwich"; do
  echo "[$p] -> $(echo "$p" | python3 lib/classify.py)"
done

# unit: classifier with debug + context
JSONL=$(ls -t ~/.claude/projects/*/*.jsonl | head -1)
echo "rename x" | SWITCH_MODEL_DEBUG=1 SWITCH_MODEL_TRANSCRIPT_PATH="$JSONL" python3 lib/classify.py

# unit: context analyzer
python3 lib/context.py "$JSONL"
python3 lib/context.py "$JSONL" --window 5

# unit: effort picker
for tier in haiku sonnet opus; do
  for p in "rename foo" "implement auth" "audit security" "deep dive analysis"; do
    e=$(echo "$p" | python3 lib/effort.py $tier)
    echo "[$tier] $e ← $p"
  done
done

# unit: pricing
python3 lib/pricing.py opus haiku 200       # expect ~93
python3 lib/pricing.py haiku opus  200      # expect ~-1400

# integration: full hook (no actual claude subprocess; bypass cleanly)
MODEL_ROUTER_BYPASS=1 echo '{"prompt":"hi","transcript_path":"/dev/null","session_id":"x","cwd":"/tmp","hook_event_name":"UserPromptSubmit"}' \
  | CLAUDE_PLUGIN_ROOT=~/skills/switch-model bash hooks/router.sh
# expect: empty stdout, exit 0

# unit: upgrade gate fires (haiku → opus, no consent)
TMP=$(mktemp -d) && mkdir -p "$TMP/.claude"
echo '{"model":"claude-haiku-4-5"}' > "$TMP/.claude/settings.json"
echo '{"prompt":"design an architecture for our rate limiter and explain why","transcript_path":"","session_id":"t"}' \
  | HOME="$TMP" SWITCH_MODEL_NO_LLM=1 SWITCH_MODEL_NO_EFFORT=1 bash hooks/router.sh
# expect: {"decision":"block","reason":"[switch-model] Upgrade suggested: haiku → opus ..."} exit 0

# unit: +upgrade prefix bypasses gate
echo '{"prompt":"+upgrade design an architecture and explain why","transcript_path":"","session_id":"t"}' \
  | HOME="$TMP" SWITCH_MODEL_NO_LLM=1 SWITCH_MODEL_NO_EFFORT=1 bash hooks/router.sh
# expect: real subprocess fires (or mock claude in PATH); header says "costs ~+X%"

# unit: SWITCH_MODEL_AUTO_UPGRADE=1 bypasses gate session-wide
echo '{"prompt":"design an architecture and explain why","transcript_path":"","session_id":"t"}' \
  | HOME="$TMP" SWITCH_MODEL_AUTO_UPGRADE=1 SWITCH_MODEL_NO_LLM=1 SWITCH_MODEL_NO_EFFORT=1 bash hooks/router.sh
# expect: real subprocess fires; no upgrade-pending block.
rm -rf "$TMP"

# unit: downgrade still auto-routes (opus → haiku, no consent needed)
TMP=$(mktemp -d) && mkdir -p "$TMP/.claude"
echo '{"model":"claude-opus-4-7"}' > "$TMP/.claude/settings.json"
echo '{"prompt":"list files in lib","transcript_path":"","session_id":"t"}' \
  | HOME="$TMP" SWITCH_MODEL_NO_LLM=1 SWITCH_MODEL_NO_EFFORT=1 bash hooks/router.sh
# expect: real subprocess fires; header says "saves ~93%".
rm -rf "$TMP"

# e2e: tmux with throwaway session
SID="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
tmux new-session -d -s wmtest "claude --session-id $SID"
sleep 6
tmux send-keys -t wmtest "list 3 prime numbers" Enter
sleep 25
tmux capture-pane -t wmtest -p
cat ~/.cache/switch-model/state.json
tmux kill-session -t wmtest
rm /home/$USER/.claude/projects/*/${SID}.jsonl
```

## Code style notes

- Bash uses `set -u`; not `-e` because we want to continue on classifier errors.
- Python target is 3.10+ (uses `str | None` PEP 604 syntax in `classify.py`).
- No external Python dependencies.
- All files chmod 755 for executables, 644 for manifests.


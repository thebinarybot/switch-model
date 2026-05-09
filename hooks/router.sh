#!/usr/bin/env bash
# UserPromptSubmit hook: classify prompt, route to suggested model via subprocess,
# return answer as block reason. Main session model untouched.

set -u

# Recursion guard: subprocess sets this to skip routing.
if [[ "${MODEL_ROUTER_BYPASS:-0}" == "1" ]]; then
  exit 0
fi

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(dirname "$(dirname "$(realpath "$0")")")}"
STATE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/switch-model"
mkdir -p "$STATE_DIR"
LOG="$STATE_DIR/router.log"

INPUT=$(cat)
PROMPT=$(printf '%s' "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('prompt',''))" 2>/dev/null)
TRANSCRIPT=$(printf '%s' "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null)
SESSION_ID=$(printf '%s' "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)

# Bypass prefix: +force or +keep → strip and passthrough.
# (note: !force/!keep don't work because ! triggers Claude Code shell mode
# before the hook sees the prompt; same for #. + has no special meaning.)
if [[ "$PROMPT" == "+force "* || "$PROMPT" == "+keep "* ]]; then
  exit 0
fi

# +upgrade prefix → strip, mark explicit consent for cheaper→pricier route.
FORCE_UPGRADE=0
if [[ "$PROMPT" == "+upgrade "* ]]; then
  FORCE_UPGRADE=1
  PROMPT="${PROMPT#+upgrade }"
fi

# Empty / very short prompts → passthrough.
if [[ ${#PROMPT} -lt 8 ]]; then
  exit 0
fi

# Follow-up detection: if prompt is a continuation of a prior turn,
# skip routing — subprocess has no main-session context, so let the
# main model handle natively (it has full history).
if [[ "${SWITCH_MODEL_NO_FOLLOWUP:-0}" != "1" ]]; then
  if printf '%s' "$PROMPT" | python3 "$PLUGIN_ROOT/lib/followup.py" 2>>"$LOG" >/dev/null; then
    : # rc=0 → not a follow-up, continue routing
  else
    printf '{"status":"skipped_followup"}\n' > "$STATE_DIR/state.json"
    exit 0
  fi
fi

tier_rank() {
  case "$1" in
    haiku) echo 0 ;;
    opus)  echo 2 ;;
    *)     echo 1 ;;
  esac
}

# Classify (with transcript context if available).
SUGGESTED=$(printf '%s' "$PROMPT" | SWITCH_MODEL_TRANSCRIPT_PATH="$TRANSCRIPT" python3 "$PLUGIN_ROOT/lib/classify.py" 2>>"$LOG")
[[ -z "$SUGGESTED" ]] && exit 0

# Pick effort level based on tier + prompt characteristics.
if [[ "${SWITCH_MODEL_NO_EFFORT:-0}" == "1" ]]; then
  EFFORT=""
else
  EFFORT=$(printf '%s' "$PROMPT" | python3 "$PLUGIN_ROOT/lib/effort.py" "$SUGGESTED" 2>>"$LOG")
  [[ -z "$EFFORT" ]] && EFFORT="medium"
fi

# Detect current model from transcript (latest assistant turn).
CURRENT_MODEL=""
if [[ -n "$TRANSCRIPT" && -f "$TRANSCRIPT" ]]; then
  CURRENT_MODEL=$(grep -o '"model":"claude-[a-z0-9-]*"' "$TRANSCRIPT" 2>/dev/null | tail -1 | sed 's/"model":"//; s/"//')
fi

# Fallback: read settings.json.
if [[ -z "$CURRENT_MODEL" ]]; then
  for cfg in "$HOME/.claude/settings.json" "$PWD/.claude/settings.json"; do
    [[ -f "$cfg" ]] || continue
    CURRENT_MODEL=$(python3 -c "import json,sys
try: print(json.load(open('$cfg')).get('model',''))
except: pass" 2>/dev/null)
    [[ -n "$CURRENT_MODEL" ]] && break
  done
fi

CURRENT_TIER=$(python3 -c "import sys; sys.path.insert(0,'$PLUGIN_ROOT/lib'); from pricing import tier_of; print(tier_of('$CURRENT_MODEL'))")

# Match → passthrough, write state for statusline.
if [[ "$CURRENT_TIER" == "$SUGGESTED" ]]; then
  printf '{"current":"%s","suggested":"%s","effort":"%s","status":"match"}\n' "$CURRENT_TIER" "$SUGGESTED" "$EFFORT" > "$STATE_DIR/state.json"
  exit 0
fi

# Mismatch → spawn subprocess on suggested model + effort.
SAVINGS=$(python3 "$PLUGIN_ROOT/lib/pricing.py" "$CURRENT_TIER" "$SUGGESTED" "${#PROMPT}")

# Upgrade gate: cheaper→pricier needs explicit consent.
# Bypass: +upgrade prefix (this turn) or SWITCH_MODEL_AUTO_UPGRADE=1 (session).
CUR_RANK=$(tier_rank "$CURRENT_TIER")
SUG_RANK=$(tier_rank "$SUGGESTED")
if (( SUG_RANK > CUR_RANK )) && [[ "$FORCE_UPGRADE" != "1" && "${SWITCH_MODEL_AUTO_UPGRADE:-0}" != "1" ]]; then
  COST_PCT="${SAVINGS#-}"  # strip leading minus; savings is negative on upgrade
  printf '{"current":"%s","suggested":"%s","effort":"%s","status":"upgrade_pending"}\n' "$CURRENT_TIER" "$SUGGESTED" "$EFFORT" > "$STATE_DIR/state.json"
  python3 -c "
import json
msg = (
  '[switch-model] Upgrade suggested: $CURRENT_TIER → $SUGGESTED @ $EFFORT effort '
  '(~+${COST_PCT}% cost vs $CURRENT_TIER).\n\n'
  'Re-submit with \`+upgrade \` prefix to run on $SUGGESTED.\n'
  'Or \`+force \` to keep $CURRENT_TIER for this prompt.\n'
  'Or set SWITCH_MODEL_AUTO_UPGRADE=1 to auto-confirm upgrades this session.'
)
print(json.dumps({'decision': 'block', 'reason': msg}))
"
  exit 0
fi

if [[ -n "$EFFORT" ]]; then
  ANSWER=$(MODEL_ROUTER_BYPASS=1 timeout 120 claude -p --model "$SUGGESTED" --effort "$EFFORT" "$PROMPT" 2>>"$LOG")
else
  ANSWER=$(MODEL_ROUTER_BYPASS=1 timeout 120 claude -p --model "$SUGGESTED" "$PROMPT" 2>>"$LOG")
fi
SUBPROC_RC=$?

if [[ $SUBPROC_RC -ne 0 || -z "$ANSWER" ]]; then
  # Subprocess failed → fall back to manual instruction.
  printf '{"current":"%s","suggested":"%s","effort":"%s","status":"failed"}\n' "$CURRENT_TIER" "$SUGGESTED" "$EFFORT" > "$STATE_DIR/state.json"
  python3 -c "
import json
print(json.dumps({
  'decision': 'block',
  'reason': '[switch-model] suggested: $SUGGESTED@$EFFORT (saves ~${SAVINGS}%) — subprocess failed. Run /model $SUGGESTED manually, or prefix prompt with +force to keep $CURRENT_TIER.'
}))
"
  exit 0
fi

# Success → return answer as block reason.
printf '{"current":"%s","suggested":"%s","effort":"%s","status":"routed","savings":%s}\n' "$CURRENT_TIER" "$SUGGESTED" "$EFFORT" "$SAVINGS" > "$STATE_DIR/state.json"

python3 <<PY
import json
ans = """$ANSWER"""
sav = float("$SAVINGS")
if sav >= 0:
    cost_note = f"saves ~{sav:.0f}%"
else:
    cost_note = f"costs ~+{-sav:.0f}%"
header = f"[switch-model: routed to $SUGGESTED @ $EFFORT effort · {cost_note} vs $CURRENT_TIER]"
footer = "\n\n_(answered by subprocess. main session still on $CURRENT_TIER. prefix +force to bypass router.)_"
print(json.dumps({"decision": "block", "reason": header + "\n\n" + ans + footer}))
PY

exit 0

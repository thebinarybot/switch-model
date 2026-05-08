#!/usr/bin/env bash
# Optional statusline. Reads model.display_name from stdin + state file.
# If user already has a statusline, integrate this output manually.

INPUT=$(cat)
STATE_FILE="${XDG_CACHE_HOME:-$HOME/.cache}/switch-model/state.json"

MODEL=$(printf '%s' "$INPUT" | python3 -c "import json,sys
try: print(json.load(sys.stdin).get('model',{}).get('display_name','?'))
except: print('?')")

if [[ -f "$STATE_FILE" ]]; then
  STATE=$(cat "$STATE_FILE" 2>/dev/null)
  STATUS=$(printf '%s' "$STATE" | python3 -c "import json,sys
try:
  s=json.load(sys.stdin)
  eff=s.get('effort','')
  tag=f\"{s['suggested']}\" + (f'@{eff}' if eff else '')
  if s.get('status')=='routed':
    print(f\"→{tag} saves ~{s.get('savings',0)}%\")
  elif s.get('status')=='failed':
    print(f\"!{tag} (manual)\")
  else: print('')
except: print('')")
  if [[ -n "$STATUS" ]]; then
    printf '[%s] [router %s]' "$MODEL" "$STATUS"
    exit 0
  fi
fi

printf '[%s]' "$MODEL"

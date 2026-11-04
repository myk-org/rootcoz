#!/bin/sh
# RootCoz health alert: read /metrics once, build one message from the failing
# checks and post it to Slack. Rendered into a ConfigMap by
# chart/templates/cronjob-health-alerts.yaml and exercised by
# tests/test_health_alert_script.py.
set -eu

METRICS_URL="${METRICS_URL:-}"
METRICS_FILE="${METRICS_FILE:-}"
ENV_LABEL="${ENV_LABEL:-RootCoz}"
ERROR_RATE_PERCENT="${ERROR_RATE_PERCENT:-5}"
DISK_WARN_PERCENT="${DISK_WARN_PERCENT:-75}"
DISK_CRIT_PERCENT="${DISK_CRIT_PERCENT:-90}"
SLACK_WEBHOOK_URL="${SLACK_WEBHOOK_URL:-}"

LINES=""

add_line() {
  LINES="$LINES$1
"
}

# Field 1 only, so a "# HELP <name> ..." comment can never be read as a sample.
metric() {
  printf '%s\n' "$1" | awk -v key="$2" '$1 == key { print $2; exit }'
}

human() {
  awk -v b="$1" 'BEGIN {
    if (b >= 1073741824) printf "%.1fG", b / 1073741824
    else if (b >= 1048576) printf "%.1fM", b / 1048576
    else if (b >= 1024) printf "%.1fK", b / 1024
    else printf "%.0fB", b
  }'
}

percent() {
  awk -v p="$1" 'BEGIN { print p / 100 }'
}

ge() {
  awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 >= b + 0) }'
}

gt() {
  awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 > b + 0) }'
}

# JSON string body. Backslash, quote and the control characters that would
# otherwise produce invalid JSON (a raw tab or CR makes Slack reject the
# payload silently) — built per character because gsub replacement strings
# cannot carry a literal backslash portably.
json_escape() {
  awk '
    function esc(s,   out, i, c) {
      out = ""
      for (i = 1; i <= length(s); i++) {
        c = substr(s, i, 1)
        if (c == "\\") out = out "\\\\"
        else if (c == "\"") out = out "\\\""
        else if (c == "\t") out = out "\\t"
        else if (c == "\r") out = out "\\r"
        else out = out c
      }
      return out
    }
    BEGIN { ORS = "" }
    { if (NR > 1) printf "\\n"; printf "%s", esc($0) }
  '
}

post() {
  if [ -n "$SLACK_WEBHOOK_URL" ]; then
    payload=$(printf '{"text": "%s"}' "$(printf '%s' "$1" | json_escape)")
    curl -fsS --max-time 10 -X POST -H 'Content-type: application/json' \
      --data "$payload" "$SLACK_WEBHOOK_URL" >/dev/null
  else
    # Dry mode: no webhook configured (the CronJob secret key is optional), so
    # the message lands in the job log and an operator can run this by hand.
    printf '%s\n' "$1"
  fi
}

if [ -n "$METRICS_FILE" ]; then
  metrics=$(cat "$METRICS_FILE" 2>/dev/null) || metrics=""
elif [ -n "$METRICS_URL" ]; then
  metrics=$(curl -fsS --max-time 10 "$METRICS_URL" 2>/dev/null) || metrics=""
else
  metrics=""
fi

if [ -z "$metrics" ]; then
  post "🔴 $ENV_LABEL: /metrics unreachable"
  exit 0
fi

health=$(metric "$metrics" rootcoz_health_up)
if [ -n "$health" ] && [ "$health" = "0" ]; then
  add_line "🔴 health: unhealthy"
fi

rate=$(metric "$metrics" rootcoz_error_rate)
if [ -n "$rate" ] && gt "$rate" "$(percent "$ERROR_RATE_PERCENT")"; then
  add_line "⚠️ error rate $(awk -v r="$rate" 'BEGIN { printf "%.2f", r * 100 }')% (threshold ${ERROR_RATE_PERCENT}%)"
fi

# The disk check is skipped silently when the ratio is absent, so this alert can
# be deployed before the image that carries the PVC gauges has rolled out.
ratio=$(metric "$metrics" rootcoz_pvc_used_ratio)
if [ -n "$ratio" ]; then
  used=$(metric "$metrics" rootcoz_pvc_used_bytes)
  capacity=$(metric "$metrics" rootcoz_pvc_capacity_bytes)
  sizes="$(human "${used:-0}") / $(human "${capacity:-0}") ($(awk -v r="$ratio" 'BEGIN { printf "%.0f", r * 100 }')%)"
  if ge "$ratio" "$(percent "$DISK_CRIT_PERCENT")"; then
    add_line "🔴 disk $sizes"
  elif ge "$ratio" "$(percent "$DISK_WARN_PERCENT")"; then
    add_line "⚠️ disk $sizes"
  fi
fi

[ -n "$LINES" ] || exit 0
post "$(printf '%s' "$LINES")"

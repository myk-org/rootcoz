#!/bin/sh
# RootCoz health alert: read /metrics once, build one message from the failing
# checks and post it to Slack. Rendered into a ConfigMap by
# chart/templates/cronjob-health-alerts.yaml and exercised by
# tests/test_health_alert_script.py.
set -eu

METRICS_URL="${METRICS_URL:-}"
METRICS_FILE="${METRICS_FILE:-}"
ENV_LABEL="${ENV_LABEL:-RootCoz}"
SERVER_ERROR_PERCENT="${SERVER_ERROR_PERCENT:-5}"
CLIENT_ERROR_PERCENT="${CLIENT_ERROR_PERCENT:-25}"
CLIENT_ERROR_MIN_REQUESTS="${CLIENT_ERROR_MIN_REQUESTS:-10}"
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

ne() {
  awk -v a="$1" -v b="$2" 'BEGIN { exit !(a + 0 != b + 0) }'
}

# A field 2 with no numeric prefix ("n/a") coerces to 0, which would read as "no 5xx"
# and hide the class it belongs to. Text after a number still counts (see count()).
numeric() {
  awk -v v="$1" 'BEGIN { exit !(v ~ /^[+-]?[0-9]/) }'
}

# errors / total as a share, strictly above the threshold, and only for a non-empty
# window: a share of nothing is not comparable. Every operand is coerced, so a
# non-numeric threshold ("5%") can never turn a page off.
above() {
  awk -v n="$1" -v d="$2" -v t="$3" \
    'BEGIN { exit !((d + 0) > 0 && (n + 0) / (d + 0) * 100 > (t + 0)) }'
}

# The display twin of above(): what the message prints is what the decision used.
pct() {
  awk -v n="$1" -v d="$2" 'BEGIN { printf "%.2f", (n + 0) / (d + 0) * 100 }'
}

# Every metric-derived number enters a message through this, so a hostile field 2
# ("3|https://evil.example") is rendered as the 3 the decision used, never as text.
count() {
  awk -v v="$1" 'BEGIN { printf "%d", v + 0 }'
}

# JSON string body. Backslash, quote and every character below U+0020, because a raw
# control character (a tab, backspace or form feed from a configured label) makes the
# body invalid JSON and Slack rejects the whole message silently — built per character
# because gsub replacement strings cannot carry a literal backslash portably.
json_escape() {
  awk '
    function esc(s,   out, i, c) {
      out = ""
      for (i = 1; i <= length(s); i++) {
        c = substr(s, i, 1)
        if (c == "\\") out = out "\\\\"
        else if (c == "\"") out = out "\\\""
        else if (c in ctrl) out = out ctrl[c]
        else out = out c
      }
      return out
    }
    BEGIN {
      ORS = ""
      for (i = 1; i < 32; i++) ctrl[sprintf("%c", i)] = sprintf("\\u%04x", i)
      # Short forms JSON also defines for these two.
      ctrl["\t"] = "\\t"
      ctrl["\r"] = "\\r"
    }
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

# A non-empty 200 is not proof of metrics: a login page or proxy error page served
# from a wrong URL parses as "no samples", which would silence every check below.
# rootcoz_error_rate is always exported, so its absence means this is not a
# rootcoz /metrics body — say so loudly instead of staying quiet.
if [ -z "$(metric "$metrics" rootcoz_error_rate)" ]; then
  post "🔴 $ENV_LABEL: metrics endpoint returned no rootcoz metrics"
  exit 0
fi

health=$(metric "$metrics" rootcoz_health_up)
if [ -n "$health" ] && [ "$health" = "0" ]; then
  add_line "🔴 health: unhealthy"
fi

# The class selectors are single-quoted literals on purpose: metric() compares awk
# $1 byte-exactly and $1 of a labelled sample is the whole token including its
# labels, so an unquoted selector silently matches nothing.
total=$(metric "$metrics" rootcoz_requests_total)
etotal=$(metric "$metrics" rootcoz_errors_total)
serr=$(metric "$metrics" 'rootcoz_errors_by_class{status_class="5xx"}')
cerr=$(metric "$metrics" 'rootcoz_errors_by_class{status_class="4xx"}')
# An absent class sample means zero errors of that class in the window.
serr="${serr:-0}"
cerr="${cerr:-0}"

# A body that cannot answer "share of requests" gets one line naming the series at
# fault, and the health and disk checks still run: exiting here would silence a
# full filesystem, the very failure this guard exists to catch. Samples only — a
# HELP-keyed sniff would refuse a comment-less body that answers perfectly well.
config=""
if [ -z "$etotal" ]; then
  config="🔴 configuration: /metrics has no rootcoz_errors_total, so server errors cannot be judged"
elif ! numeric "$serr" || ! numeric "$cerr" || ! numeric "$etotal"; then
  config="🔴 configuration: /metrics has a non-numeric rootcoz_errors_by_class or rootcoz_errors_total count, so server errors cannot be judged"
elif ne "$(awk -v a="$serr" -v b="$cerr" 'BEGIN { printf "%d", (a + 0) + (b + 0) }')" "$etotal"; then
  config="🔴 configuration: rootcoz_errors_by_class does not add up to rootcoz_errors_total, so server errors cannot be judged"
# An absent denominator is loud even with zero errors: silently reading it as "nothing
# to judge" leaves a renamed series unnoticed until the first error. A present 0 with
# zero errors is an empty window, which is a legitimate answer and stays silent.
elif ! gt "$total" 0 && { [ -z "$total" ] || ne "$serr" 0 || ne "$cerr" 0 || ne "$etotal" 0; }; then
  config="🔴 configuration: /metrics has no usable rootcoz_requests_total, so the error share cannot be judged"
fi

storm=0
if [ -z "$config" ]; then
  # Server errors page on any non-empty window: one failed request is an incident,
  # and a one-shot sample has no history to hold a condition against.
  if above "$serr" "$total" "$SERVER_ERROR_PERCENT"; then
    add_line "🔴 server errors 5xx $(count "$serr")/$(count "$total") ($(pct "$serr" "$total")%) above ${SERVER_ERROR_PERCENT}% threshold"
  fi
  # A 4xx storm is a different, high-threshold signal and needs a denominator big
  # enough to mean anything: one mistyped login must not read as 100 %.
  if ge "$total" "$CLIENT_ERROR_MIN_REQUESTS" &&
    above "$cerr" "$total" "$CLIENT_ERROR_PERCENT"; then
    add_line "⚠️ client error storm 4xx $(count "$cerr")/$(count "$total") ($(pct "$cerr" "$total")%) above ${CLIENT_ERROR_PERCENT}% threshold — client errors, not an outage"
    storm=1
  fi
fi
if [ -n "$config" ]; then
  add_line "$config"
fi

# The disk check is skipped silently when the ratio is absent, so this alert can
# be deployed before the image that carries the data-directory gauges has rolled out.
ratio=$(metric "$metrics" rootcoz_data_used_ratio)
if [ -n "$ratio" ]; then
  used=$(metric "$metrics" rootcoz_data_used_bytes)
  capacity=$(metric "$metrics" rootcoz_data_capacity_bytes)
  sizes="$(human "${used:-0}") / $(human "${capacity:-0}") ($(awk -v r="$ratio" 'BEGIN { printf "%.0f", r * 100 }')%)"
  if ge "$ratio" "$(percent "$DISK_CRIT_PERCENT")"; then
    add_line "🔴 disk $sizes"
  elif ge "$ratio" "$(percent "$DISK_WARN_PERCENT")"; then
    add_line "⚠️ disk $sizes"
  fi
fi

# "4xx happened" rides along on a message a real problem already raises, but only
# when the share was judgeable and the class counts were readable: otherwise the
# same message would contradict the configuration line above it.
if [ -n "$LINES" ] && [ "$storm" = 0 ] && [ -z "$config" ] &&
  ge "$total" "$CLIENT_ERROR_MIN_REQUESTS" && gt "$cerr" 0; then
  add_line "ℹ️ client errors 4xx $(count "$cerr")/$(count "$total") ($(pct "$cerr" "$total")%) at or below the ${CLIENT_ERROR_PERCENT}% storm threshold"
fi

[ -n "$LINES" ] || exit 0
post "$(printf '%s' "$LINES")"

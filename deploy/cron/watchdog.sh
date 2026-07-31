#!/usr/bin/env bash
#
# Restart the bot if its event loop has stopped turning.
#
# Supervisor restarts a process that *exits*. It cannot tell that a process still
# holding its PID has stopped doing anything — a wedged event loop and a quietly
# idle one look identical from outside. The bot writes a heartbeat file every
# minute; if that timestamp stops advancing, the loop is stuck.
#
# Installed by deploy/cron/turon-bot-cron, which runs it every ten minutes.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/turon-avto-test-bot}"
HEARTBEAT="${HEARTBEAT:-$APP_DIR/logs/heartbeat}"
PROGRAM="${PROGRAM:-turon-bot}"

# The bot beats once a minute. Five minutes is four missed beats — comfortably
# past a slow tick or a long import, and well short of a missed posting slot.
MAX_AGE_SECONDS="${MAX_AGE_SECONDS:-300}"

log() {
    echo "$(date '+%F %T') watchdog: $*"
}

# --- is supervisor even meant to be running it? -------------------------------
# Do nothing while the program is deliberately stopped, or the watchdog would
# fight an operator who is mid-maintenance.
state="$(supervisorctl status "$PROGRAM" 2>/dev/null | awk '{print $2}' || true)"
if [[ "$state" != "RUNNING" ]]; then
    log "program is '${state:-unknown}', not RUNNING — leaving it alone"
    exit 0
fi

# --- has it beaten recently? ---------------------------------------------------
if [[ ! -f "$HEARTBEAT" ]]; then
    # Absent immediately after a restart is normal; the first beat is a minute
    # away. Only complain, so a genuinely missing file shows up in the log
    # without triggering a restart loop.
    log "no heartbeat file at $HEARTBEAT yet"
    exit 0
fi

now="$(date +%s)"
beat="$(stat -c %Y "$HEARTBEAT")"
age=$(( now - beat ))

if (( age <= MAX_AGE_SECONDS )); then
    exit 0
fi

log "heartbeat is ${age}s old (limit ${MAX_AGE_SECONDS}s) — restarting $PROGRAM"
supervisorctl restart "$PROGRAM"
log "restart issued"

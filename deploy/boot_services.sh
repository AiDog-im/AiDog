#!/usr/bin/env bash
# Idempotent starter for the AiDog runtime tmux sessions (post-swap P5-5 view):
#   aidog_web - situational web console (127.0.0.1:19167 — car A2 port
#               standard; AIDOG_WEB_PORT env > web/port.local > 19167)
#   aidog_svc - mycore chat service (P5-2; gated fail-closed until the
#               dedicated bot token + allowlist are provisioned)
#   aidog_mwd - mycore watchdog loop (--execute per owner authorization
#               2026-01-19; log-only notify until MYCORE_WD_CHAT is set)
# Transition phase only (shim present): aidog_gw (kernel gateway) and
# aidog_wd (legacy gateway watchdog). Post-swap they are RETIRED: never
# started, and `status` flags any lingering session as a leftover (rc=1).
#
# Designed to be the single entry point invoked at boot (Windows Task
# Scheduler -> wsl.exe, or /etc/wsl.conf [boot] command - see docs for the
# chosen option). Safe to run repeatedly: existing sessions/processes are
# left untouched (idempotent), missing ones are brought up.
#
# Usage:
#   boot_services.sh            # start anything that is missing
#   boot_services.sh status     # report session/process/endpoint state
#   boot_services.sh stop       # stop all three sessions (maintenance)
#
# The `aidog` CLI exposes the same three verbs (`aidog start|status|stop`)
# as the single human entry point. `start` self-checks the console entry
# point and repairs it (see repair_web below): a dead/hung session is
# recycled, a wedged process we own is restarted, and a FOREIGN process on
# the port is reported, never killed. Test seams (read-only defaults):
#   AIDOG_BOOT_SETTLE_SEC  settle time before self-verify (default 6)
#   AIDOG_REPAIR_GRACE     seconds a slow console gets before recycling (15)
#
# English comments per repo i18n policy.
set -u

# shq <value> — POSIX-shell-quote one argument for embedding in a tmux
# command STRING (wrap-up audit W4-03). tmux re-parses that string with the
# default shell, so an unquoted $REPO/$PROXY_URL with spaces or shell
# metacharacters would word-split or execute. python3 is already a hard
# requirement of this script (web_up below) and shlex.quote emits pure
# POSIX single-quote style — safe for any inner shell, unlike bash printf %q.
shq() { python3 -c 'import shlex,sys; print(shlex.quote(sys.argv[1]), end="")' "$1"; }

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)  # self-deriving: no machine path
# aibox difficulty car (0.5.91): user-space tmux lives behind the wrapper in
# ~/.local/bin (deploy/bootstrap_tmux.sh) — a non-login launcher (Task
# Scheduler -> wsl.exe, console subprocess) may not have that dir on PATH.
if ! command -v tmux >/dev/null 2>&1 && [ -x "$HOME/.local/bin/tmux" ]; then
  PATH="$HOME/.local/bin:$PATH"; export PATH
fi
# P5-5 phase resolver: the upstream shim marks the transition stack.
#   transition (shim present) = current behavior: source it, run gw/wd.
#   post (shim deleted)       = gw/wd retired; no upstream facts are sourced,
#                               GW_PATTERN/GWLOG stay empty (see guards below).
# python3 is a hard requirement (shq above, web_up below): fail honestly and
# early rather than shell-quoting with a missing interpreter.
if ! command -v python3 >/dev/null 2>&1; then
  echo "boot_services: FAIL - python3 not found on PATH (install Python 3 first)" >&2
  exit 1
fi

if [ -f "$REPO/deploy/upstream_shim.env" ]; then
  PHASE=transition
  # shellcheck source=upstream_shim.env
  source "$REPO/deploy/upstream_shim.env" || { echo "boot_services: missing upstream shim" >&2; exit 1; }
  GW_PATTERN=$AIDOG_KERNEL_CMD
  GWLOG=$AIDOG_PROFILE_DIR/logs/gateway_run.log
else
  PHASE=post
  GW_PATTERN=""
  GWLOG=""
fi
WEB_LOG=$REPO/deploy/web_console.log
# Car A2 (port standard 2026-11-01): env override > web/port.local (settings
# card pin) > 19167 (product default). Mirrors web/server.py _resolve_web_port.
WEB_PORT="${AIDOG_WEB_PORT:-}"
if [ -z "$WEB_PORT" ] && [ -f "$REPO/web/port.local" ]; then
  WEB_PORT="$(tr -cd '0-9' < "$REPO/web/port.local")"
fi
WEB_PORT="${WEB_PORT:-19167}"

# Post-reboot egress fix (owner-approved, 2026-10-17): direct Telegram API egress
# is unreachable from this host after a reboot, and tmux children inherit the
# SERVER environment (not ours) — so every session must carry the local proxy
# explicitly: global env table for any later `tmux new` (incl. watchdog restarts)
# plus an `env` prefix on each start command for determinism.
PROXY_URL=${HTTPS_PROXY:-${https_proxy:-http://127.0.0.1:12334}}
NO_PROXY_VAL=${NO_PROXY:-${no_proxy:-localhost,127.0.0.1,::1}}
# Car K (recovery center B4, owner ask 2026-11-01): deploy/proxy.local wins
# over the env chain — a NON-EMPTY first line pins the proxy URL, an EMPTY
# file means DIRECT MODE (no proxy env at all, for a dead local proxy).
# The console's DIRECT MODE switch writes it; the change applies on the next
# service restart (honest restart_required, never a live re-bind).
PROXY_FILE="$REPO/deploy/proxy.local"
PROXY_DIRECT=""
if [ -f "$PROXY_FILE" ]; then
  _pv="$(head -n1 "$PROXY_FILE" | tr -d '[:space:]')"
  if [ -n "$_pv" ]; then PROXY_URL="$_pv"; else PROXY_DIRECT="1"; fi
fi
if [ -n "$PROXY_DIRECT" ]; then
  PROXY_ENV="env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy"
else
  PROXY_ENV="env HTTP_PROXY=$(shq "$PROXY_URL") HTTPS_PROXY=$(shq "$PROXY_URL") http_proxy=$(shq "$PROXY_URL") https_proxy=$(shq "$PROXY_URL") NO_PROXY=$(shq "$NO_PROXY_VAL") no_proxy=$(shq "$NO_PROXY_VAL")"
fi

inject_proxy() {
  tmux set-environment -g HTTP_PROXY "$PROXY_URL"
  tmux set-environment -g HTTPS_PROXY "$PROXY_URL"
  tmux set-environment -g http_proxy "$PROXY_URL"
  tmux set-environment -g https_proxy "$PROXY_URL"
  tmux set-environment -g NO_PROXY "$NO_PROXY_VAL"
  tmux set-environment -g no_proxy "$NO_PROXY_VAL"
}

# Dir-agnostic on purpose: must match both `bash deploy/watchdog.sh` (relative
# launch) and any absolute repo path — never hard-code the repo directory name.
WD_PATTERN='bash .*/deploy/watchdog.sh'

srv_pid() { tmux display-message -p '#{pid}' 2>/dev/null; }

our_pgrep() {  # pids matching pattern, excluding the tmux SERVER: its argv quotes
  local p srv  # our own `tmux new -d ... "<command>"` string and would self-match.
  srv=$(srv_pid)
  for p in $(pgrep -f "$1" 2>/dev/null); do
    [ "$p" = "$srv" ] && continue
    printf '%s\n' "$p"
  done
}

log() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

session_exists() { tmux has-session -t "$1" 2>/dev/null; }

web_up() {
  # Probe OUR endpoint, not just port 80-style liveness: a foreign server
  # squatting on the port must never count as "console up".
  python3 - "$WEB_PORT" <<'PY' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=3).read(64)
PY
}

ensure_tmux() {
  command -v tmux >/dev/null 2>&1 && return 0
  log "tmux not found - bootstrapping user-space tmux (no admin rights needed)"
  bash "$REPO/deploy/bootstrap_tmux.sh" || true
  if [ -x "$HOME/.local/bin/tmux" ]; then PATH="$HOME/.local/bin:$PATH"; export PATH; fi
  command -v tmux >/dev/null 2>&1
}

web_port_pid() {  # pid listening on $WEB_PORT (best effort; empty = none/unknown)
  local out=""
  if command -v ss >/dev/null 2>&1; then
    out=$(ss -ltnpH "sport = :$WEB_PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2)
  fi
  if [ -z "$out" ] && command -v lsof >/dev/null 2>&1; then
    out=$(lsof -nP -iTCP:$WEB_PORT -sTCP:LISTEN -t 2>/dev/null | head -n1)
  fi
  printf '%s' "$out"
}

is_our_console_pid() {  # ours = argv names server.py AND cwd is <repo>/web
  local pid=$1 cwd cmd
  [ -r "/proc/$pid/cmdline" ] || return 1   # no /proc (macOS): never kill what we cannot identify
  cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
  case "$cmd" in *server.py*) ;; *) return 1 ;; esac
  cwd=$(readlink "/proc/$pid/cwd" 2>/dev/null || true)
  [ "$cwd" = "$REPO/web" ]
}

wait_web() {  # $1 = seconds to keep trying (each probe costs at most 3s)
  local deadline=$(( $(date +%s) + ${1:-0} ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    web_up && return 0
    sleep 1
  done
  web_up
}

repair_web() {
  # Self-check + self-repair for the CONSOLE entry point (`aidog start`,
  # install, boot). Bounded and honest: a foreign process on the port is
  # reported, never killed; only our own session / our own server.py are
  # recycled. Every branch says what it did.
  local holder grace="${AIDOG_REPAIR_GRACE:-15}"
  log "aidog_web: console self-check failed - repairing"
  holder=$(web_port_pid)
  if [ -n "$holder" ] && ! is_our_console_pid "$holder"; then
    log "aidog_web: port $WEB_PORT is held by ANOTHER process (pid $holder) - NOT killed; stop it yourself or set AIDOG_WEB_PORT"
    return 1
  fi
  if [ -n "$holder" ]; then
    log "aidog_web: wedged console process (pid $holder) - stopping it"
    kill "$holder" 2>/dev/null || true
    sleep 1
  elif session_exists aidog_web; then
    log "aidog_web: session alive but nothing is listening - recycling"
    tmux kill-session -t aidog_web 2>/dev/null || true
    sleep 1
  fi
  start_web
  if wait_web "$grace"; then log "aidog_web: console is up"; return 0; fi
  # one more recycle round (a half-dead session can survive one restart)
  if session_exists aidog_web; then
    log "aidog_web: still down - recycling once more"
    tmux kill-session -t aidog_web 2>/dev/null || true
    sleep 1
    start_web
    wait_web 12 && { log "aidog_web: console is up"; return 0; }
  fi
  log "aidog_web: console still down - last lines of $WEB_LOG:"
  tail -n 5 "$WEB_LOG" 2>/dev/null | sed -E 's/((Bearer|token|TOKEN|PAT|SECRET|PASSWORD)[=: ]+)[A-Za-z0-9._~+/-]{8,}/\1REDACTED/g' >&2 || true
  return 1
}

start_gw() {
  if [ "$PHASE" = post ]; then log "aidog_gw: retired (post-swap)"; return 0; fi
  if session_exists aidog_gw || [ -n "$(our_pgrep "$GW_PATTERN")" ]; then
    log "aidog_gw: already running"
  else
    # First tmux command after a hard reboot can fail on a stale socket
    # (observed 2026-10-17: gateway session silently missing at boot) — verify
    # the session appeared and retry once before letting status report FAIL.
    # AIDOG_KERNEL_CMD stays word-split on purpose: it is a multi-word
    # command from the owner's machine-local shim (same trust as this very
    # script) and quoting it as one word would break it. Everything PATH-like
    # goes through shq (wrap-up audit W4-03).
    if ! tmux new -d -s aidog_gw "$PROXY_ENV $AIDOG_KERNEL_CMD --force > $(shq "$GWLOG") 2>&1"; then
      log "aidog_gw: tmux new failed (attempt 1), retrying"
      sleep 1
      tmux new -d -s aidog_gw "$PROXY_ENV $AIDOG_KERNEL_CMD --force > $(shq "$GWLOG") 2>&1" || \
        log "aidog_gw: tmux new failed (attempt 2)"
    elif ! session_exists aidog_gw; then
      sleep 1
      session_exists aidog_gw || tmux new -d -s aidog_gw "$PROXY_ENV $AIDOG_KERNEL_CMD --force > $(shq "$GWLOG") 2>&1" || \
        log "aidog_gw: session missing after start, retry failed"
    fi
    log "aidog_gw: started"
  fi
}

start_wd() {
  if [ "$PHASE" = post ]; then log "aidog_wd: retired (post-swap)"; return 0; fi
  if session_exists aidog_wd || [ -n "$(our_pgrep "$WD_PATTERN")" ]; then
    log "aidog_wd: already running"
  else
    tmux new -d -s aidog_wd "$PROXY_ENV bash $(shq "$REPO")/deploy/watchdog.sh"
    log "aidog_wd: started"
  fi
}

start_web() {
  if session_exists aidog_web || web_up; then
    log "aidog_web: already running"
  else
    tmux new -d -s aidog_web "cd $(shq "$REPO")/web && $PROXY_ENV python3 server.py > $(shq "$WEB_LOG") 2>&1"
    log "aidog_web: started"
  fi
}

start_svc() {
  # P5-2: readiness lives in deploy/run_service.sh --check (single source of
  # provisioning logic; credentials never touch argv).
  if session_exists aidog_svc; then
    log "aidog_svc: already running"
  elif bash "$REPO/deploy/run_service.sh" --check; then
    mkdir -p "$REPO/data/mycore_service"
    tmux new -d -s aidog_svc "cd $(shq "$REPO") && $PROXY_ENV bash deploy/run_service.sh > data/mycore_service/boot.log 2>&1"
    log "aidog_svc: started"
  else
    log "aidog_svc: skipped (fail-closed: allowlist/token not provisioned — awaiting dedicated bot)"
  fi
}

start_mwd() {
  local p pid="" comm etimes cfg_age i
  if session_exists aidog_mwd || [ -n "$(our_pgrep 'python3 -m mycore\.watchdog')" ]; then
    # Config-snapshot guard (2026-11-01 incident): the watchdog reads its
    # config ONCE at start, so a config edited while the session lives
    # keeps the process probing the OLD targets forever — observed with the
    # 8790 -> 19167 port move: 79 blind console restarts while the console
    # was healthy on the new port (panel: permanent "watchdog FAULT").
    # Config younger than the python process = stale snapshot -> recycle.
    for p in $(our_pgrep 'python3 -m mycore\.watchdog'); do
      comm=$(ps -o comm= -p "$p" 2>/dev/null | tr -d ' ')
      case "$comm" in python*) pid=$p; break ;; esac
    done
    if [ -n "$pid" ]; then
      etimes=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
      cfg_age=$(( $(date +%s) - $(stat -c %Y "$REPO/deploy/watchdog.mycore.local" 2>/dev/null || echo 0) ))
      if [ -n "$etimes" ] && [ "$cfg_age" -lt "$etimes" ]; then
        log "aidog_mwd: config newer than the process (stale snapshot) — recycling"
        tmux kill-session -t aidog_mwd 2>/dev/null
        # kill ONLY python processes: a bare pattern match would also reap
        # an operator's `grep … mycore.watchdog` (self-match, same class as
        # the tmux-server exclusion above)
        for p in $(our_pgrep 'python3 -m mycore\.watchdog'); do
          comm=$(ps -o comm= -p "$p" 2>/dev/null | tr -d ' ')
          case "$comm" in python*) kill "$p" 2>/dev/null ;; esac
        done
        i=0
        while [ "$i" -lt 5 ] && [ -n "$(our_pgrep 'python3 -m mycore\.watchdog')" ]; do
          sleep 1; i=$((i+1))
        done
        mkdir -p "$REPO/data"
        tmux new -d -s aidog_mwd "cd $(shq "$REPO") && $PROXY_ENV python3 -m mycore.watchdog --config deploy/watchdog.mycore.local --execute > data/mycore_watchdog.log 2>&1"
        log "aidog_mwd: restarted with the fresh config (--execute per owner authorization)"
        return 0
      fi
    fi
    log "aidog_mwd: already running"
  else
    mkdir -p "$REPO/data"
    tmux new -d -s aidog_mwd "cd $(shq "$REPO") && $PROXY_ENV python3 -m mycore.watchdog --config deploy/watchdog.mycore.local --execute > data/mycore_watchdog.log 2>&1"
    log "aidog_mwd: started (--execute per owner authorization)"
  fi
}

status() {
  local rc=0 s
  command -v tmux >/dev/null 2>&1 || { echo "❌ tmux missing (bootstrap failed)"; return 1; }
  for s in aidog_gw aidog_wd aidog_web aidog_mwd; do
    if [ "$PHASE" = post ] && { [ "$s" = aidog_gw ] || [ "$s" = aidog_wd ]; }; then
      # P5-5: gw/wd sessions must be GONE post-swap — lingering = leftover.
      if session_exists "$s"; then
        echo "⚠️ tmux $s: leftover (retired at P5-5 — kill manually)"; rc=1
      else
        echo "✅ tmux $s: retired (absent)"
      fi
    elif session_exists "$s"; then echo "✅ tmux $s: up"; else echo "❌ tmux $s: DOWN"; rc=1; fi
  done
  # The chat service is expected-absent until provisioning exists: pending is
  # not a failure; provisioned-but-down is.
  if session_exists aidog_svc; then
    echo "✅ tmux aidog_svc: up"
  elif bash "$REPO/deploy/run_service.sh" --check; then
    echo "❌ tmux aidog_svc: provisioned but DOWN"; rc=1
  else
    echo "⏳ tmux aidog_svc: pending (fail-closed: awaiting dedicated bot)"
  fi
  # Gateway liveness only exists in the transition phase (no GW_PATTERN post).
  if [ "$PHASE" = transition ]; then
    if [ -n "$GW_PATTERN" ] && [ -n "$(our_pgrep "$GW_PATTERN")" ]; then echo "✅ gateway process: up"; else echo "❌ gateway process: DOWN"; rc=1; fi
  fi
  if web_up; then echo "✅ web console :$WEB_PORT: up"; else echo "❌ web console :$WEB_PORT: DOWN"; rc=1; fi
  return $rc
}

case "${1:-start}" in
  start)
    log "boot_services: ensuring AiDog runtime sessions"
    ensure_tmux || { log "boot_services: FAIL - tmux unavailable (bootstrap failed)"; exit 1; }
    inject_proxy
    start_gw; start_wd; start_web; start_svc; start_mwd
    # Give slow starts (gateway Telegram connect) a moment, then self-verify.
    sleep "${AIDOG_BOOT_SETTLE_SEC:-6}"
    if ! web_up; then repair_web || true; fi
    if status; then
      log "boot_services: all green"
      log "console: http://127.0.0.1:$WEB_PORT/"
    else
      log "boot_services: FAIL - some services did not come up"
      exit 1
    fi
    ;;
  status) ensure_tmux >/dev/null 2>&1 || { echo "❌ tmux missing (bootstrap failed)"; return 1; }; status ;;
  stop)
    for s in aidog_mwd aidog_svc aidog_web aidog_wd aidog_gw; do
      session_exists "$s" && { tmux kill-session -t "$s"; log "$s: stopped"; }
    done
    # watchdog/gateway may linger outside tmux; only kill our own patterns
    pkill -f "$WD_PATTERN" 2>/dev/null && log "watchdog process: stopped"
    # DANGER: GW_PATTERN is empty in the post-swap phase and `pkill -f ''`
    # would match (and kill) EVERYTHING incl. this shell — guard on non-empty.
    if [ -n "$GW_PATTERN" ]; then
      pkill -f "$GW_PATTERN" 2>/dev/null && log "gateway process: stopped"
    fi
    pkill -f 'python3 -m mycore.service' 2>/dev/null && log "mycore service: stopped"
    pkill -f 'python3 -m mycore.watchdog' 2>/dev/null && log "mycore watchdog: stopped"
    ;;
  *) echo "usage: $0 [start|status|stop]"; exit 2 ;;
esac

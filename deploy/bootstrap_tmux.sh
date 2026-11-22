#!/usr/bin/env bash
# bootstrap_tmux.sh — install a user-space tmux WITHOUT administrator rights.
#
# Why: tmux is the one hard prerequisite of deploy/install.sh that used to
# demand `sudo apt-get install tmux` — the single biggest install-difficulty
# driver on a fresh WSL/Ubuntu box (aibox.im catalog: aidog difficulty 4/5).
# `apt-get download` + `dpkg -x` work as a plain user, so the installer can
# provision tmux itself: packages are unpacked under
# ~/.local/share/aidog/tmux and a PATH wrapper at ~/.local/bin/tmux scopes
# LD_LIBRARY_PATH to that prefix (system libraries are never shadowed for
# anything except the wrapper's own tmux process).
#
# Scope: Debian / Ubuntu / WSL (apt + dpkg present). Anything else returns 1
# and the caller keeps the manual instructions (macOS: conda/brew tmux).
# No sudo, no package-manager writes outside $HOME — the whole footprint is
# ~/.local/share/aidog/tmux + ~/.local/bin/tmux.
#
# Usage:
#   bash deploy/bootstrap_tmux.sh          # idempotent: no-op when tmux works
#   bash deploy/bootstrap_tmux.sh --force  # re-provision even if tmux works
#   bash deploy/bootstrap_tmux.sh --check  # report only, never writes
#
# Exit codes:
#   0 = tmux usable now (system tmux, existing wrapper, or freshly verified)
#   1 = bootstrap failed (caller prints the manual fallback)
#
# English output per repo i18n policy (all docs/source are English).
set -uo pipefail

PREFIX="${AIDOG_TMUX_PREFIX:-$HOME/.local/share/aidog/tmux}"
BINDIR="${AIDOG_TMUX_BINDIR:-$HOME/.local/bin}"
SOCK="aidog-bootstraptmux"   # private socket: never touches a system tmux server

say()  { printf '[tmux-boot] %s\n' "$*"; }
fail() { printf '[tmux-boot] FAIL: %s\n' "$*" >&2; exit 1; }

MODE="run"
case "${1:-}" in
  --force) MODE="force" ;;
  --check) MODE="check" ;;
  "")      ;;
  *) fail "unknown argument: $1 (use --force or --check)" ;;
esac

# --- already usable? -------------------------------------------------------
# Two honest sources: a real system tmux, or a wrapper from a previous run
# (a fresh login shell may simply not have ~/.local/bin on PATH yet — verify
# the wrapper directly instead of trusting `command -v`).
have_working_tmux() {
  if command -v tmux >/dev/null 2>&1 && tmux -V >/dev/null 2>&1; then
    return 0
  fi
  [ -x "$BINDIR/tmux" ] && "$BINDIR/tmux" -V >/dev/null 2>&1
}

if [ "$MODE" = "check" ]; then
  if have_working_tmux; then say "usable: $(command -v tmux 2>/dev/null || echo "$BINDIR/tmux")"; exit 0
  else say "missing: no system tmux and no working wrapper"; exit 1; fi
fi
if [ "$MODE" = "run" ] && have_working_tmux; then exit 0; fi

# --- platform guards -------------------------------------------------------
[ "$(uname -s)" = "Linux" ] || \
  fail "user-space bootstrap supports Linux/WSL only (macOS: conda install -y -c conda-forge tmux)"
command -v apt-get >/dev/null 2>&1 || \
  fail "apt-get not found — only Debian/Ubuntu/WSL are supported; install tmux manually"
command -v dpkg   >/dev/null 2>&1 || fail "dpkg not found — install tmux manually"

WORK="$(mktemp -d)" || fail "mktemp failed"
trap 'rm -rf "$WORK"' EXIT

# --- package set: tmux + every direct Depends that is NOT installed --------
# (libc6/libsystemd0/... are always present, so they are skipped — we never
# extract a second libc into the prefix.)
PKGS="tmux"
DEPS="$(apt-cache depends tmux 2>/dev/null | awk '/^  Depends:/{print $2}' | grep -v '^<' || true)"
for dep in $DEPS; do
  dpkg -s "$dep" >/dev/null 2>&1 || PKGS="$PKGS $dep"
done

# --- download (no root) ----------------------------------------------------
# apt-get download needs the package lists; a fresh image may have them
# cleared. Fall back to a USER-SPACE lists dir (apt update with overridden
# Dir::State::lists writes only under $HOME) before giving up.
download_pkgs() { # $@ = packages ; downloads into $WORK/debs
  mkdir -p "$WORK/debs"
  ( cd "$WORK/debs" && apt-get download "$@" >/dev/null 2>&1 )
}
if ! download_pkgs $PKGS; then
  say "apt lists unavailable — refreshing into a user-space dir (no root)"
  ALIST="$HOME/.cache/aidog-apt/lists"
  mkdir -p "$ALIST/partial" "$HOME/.cache/aidog-apt/cache/archives/partial" || fail "mkdir failed"
  apt-get -o Dir::State::lists="$ALIST" update >/dev/null 2>&1 || \
    fail "cannot fetch package lists (network/mirror) — install tmux manually: sudo apt-get install tmux"
  ( cd "$WORK/debs" && \
    apt-get -o Dir::State::lists="$ALIST" download "$@" >/dev/null 2>&1 ) || \
    fail "download failed for: $* — install tmux manually: sudo apt-get install tmux"
fi

# --- unpack into the user prefix (no root) ---------------------------------
mkdir -p "$PREFIX" || fail "cannot create $PREFIX"
for deb in "$WORK"/debs/*.deb; do
  [ -e "$deb" ] || fail "no .deb downloaded"
  dpkg -x "$deb" "$PREFIX" 2>/dev/null || fail "dpkg -x failed: $deb"
done
[ -x "$PREFIX/usr/bin/tmux" ] || fail "tmux binary missing after unpack ($PREFIX/usr/bin/tmux)"

# --- wrapper: prefer a real system tmux if one appears later; scope libs ----
LIBDIRS="$(find "$PREFIX" -name '*.so*' -type f -printf '%h\n' 2>/dev/null | sort -u | paste -sd: -)"
mkdir -p "$BINDIR" || fail "cannot create $BINDIR"
if [ -n "$LIBDIRS" ]; then
  cat > "$BINDIR/tmux" <<EOF
#!/usr/bin/env bash
# tmux (user-space wrapper) — generated by deploy/bootstrap_tmux.sh.
# Defers to a real system tmux when one is installed later; otherwise runs
# the unpacked binary with its bundled library path (LD_LIBRARY_PATH is
# scoped to THIS process only).
for c in /usr/bin/tmux /usr/local/bin/tmux; do
  [ -x "\$c" ] && exec "\$c" "\$@"
done
export LD_LIBRARY_PATH="$LIBDIRS\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
exec "$PREFIX/usr/bin/tmux" "\$@"
EOF
else
  cat > "$BINDIR/tmux" <<EOF
#!/usr/bin/env bash
# tmux (user-space wrapper) — generated by deploy/bootstrap_tmux.sh.
for c in /usr/bin/tmux /usr/local/bin/tmux; do
  [ -x "\$c" ] && exec "\$c" "\$@"
done
exec "$PREFIX/usr/bin/tmux" "\$@"
EOF
fi
chmod 0755 "$BINDIR/tmux" || fail "cannot chmod $BINDIR/tmux"

# --- verify: version + a real session on a PRIVATE socket -------------------
# (the private socket guarantees we never touch a system tmux server)
if ! "$BINDIR/tmux" -V >/dev/null 2>&1; then
  rm -f "$BINDIR/tmux"          # never leave a broken wrapper on PATH
  fail "tmux does not run after unpack (missing libraries?) — install manually: sudo apt-get install tmux"
fi
if ! "$BINDIR/tmux" -L "$SOCK" new -d -s probe >/dev/null 2>&1; then
  rm -f "$BINDIR/tmux"
  fail "tmux smoke session failed — install manually: sudo apt-get install tmux"
fi
"$BINDIR/tmux" -L "$SOCK" kill-server 2>/dev/null || true

say "installed: $BINDIR/tmux ($("$BINDIR/tmux" -V 2>/dev/null | tr -d '\n'))"
say "note: add $BINDIR to PATH if this shell does not see it (new login shells pick it up automatically)"
exit 0

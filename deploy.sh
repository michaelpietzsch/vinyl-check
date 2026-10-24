#!/bin/bash
# deploy.sh — deploy vinyl-check to a Raspberry Pi (Raspberry Pi OS / Debian) from your own computer
#
#   VC_TARGET=user@host ./deploy.sh      — ensure packages, deploy code + unit, run the tests on the Pi, restart
#   ./deploy.sh --target=user@host        — the same, target as an option
#   … --no-test                           — skip the test run
#   … --now                               — restart immediately, even while a recording or analysis is running
#                                           (aborts it!)
#
# The target is passed in and never stored in the repository. SSH must work with a key, sudo without a password.
# Note: the tests on the Pi take a good ten minutes of CPU. Do not start them next to a running recording.
#
# The code first goes to /opt/vinyl-check.staged. If nothing is running on the Pi, the service is restarted right
# away; ExecStartPre of the unit swaps the code into /opt/vinyl-check (root:root, read-only for the service). If a
# recording or analysis is running, the script sets the marker /var/lib/vinyl-check/restart-when-idle and returns:
# the service restarts itself as soon as nothing is running. Versions before 1.0 look for the marker under its old
# name restart-wenn-frei, so both are written. An older version on the Pi that does not know the marker at all is
# waited for by the script itself (up to 3 h). No recording is aborted, except with --now.
# Configuration lives in /etc/vinyl-check/config.toml (created from config.example.toml only the first time).

set -euo pipefail

TARGET="${VC_TARGET:-}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_TESTS=1
NOW=0
for a in "$@"; do
  case "$a" in
    --no-test) RUN_TESTS=0 ;;
    --now|--sofort) NOW=1 ;;                 # --sofort, --ziel=: option names of versions before 1.0
    --target=*) TARGET="${a#--target=}" ;;
    --ziel=*) TARGET="${a#--ziel=}" ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done
if [ -z "$TARGET" ]; then
  echo "Target missing: VC_TARGET=user@host ./deploy.sh  or  ./deploy.sh --target=user@host" >&2
  exit 2
fi
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$DIR/vinylcheck/__init__.py")"

CTL="$(mktemp -u /tmp/deploy-vinylcheck.XXXXXX)"
ssh -o ControlMaster=yes -o ControlPath="$CTL" -o ControlPersist=10m -fN "$TARGET"
trap 'ssh -o ControlPath="$CTL" -O exit "$TARGET" 2>/dev/null || true' EXIT
SSH=(ssh -o ControlPath="$CTL" "$TARGET")

echo "=== vinyl-check $VERSION deploy → $TARGET ==="

echo "[1/5] Debian packages (kept up to date by unattended-upgrades)"
"${SSH[@]}" "sudo DEBIAN_FRONTEND=noninteractive apt-get -y -q install --no-install-recommends \
  python3-numpy python3-scipy python3-soundfile python3-fastapi python3-uvicorn python3-websockets \
  python3-pytest python3-httpx python3-torch python3-reportlab python3-pil python3-cryptography \
  fonts-ibm-plex alsa-utils >/dev/null && echo '    ok'"

echo "[2/5] User + directories"
"${SSH[@]}" 'sudo bash -s' <<'REMOTE'
set -e
id vinylcheck >/dev/null 2>&1 || useradd --system --home-dir /var/lib/vinyl-check --shell /usr/sbin/nologin vinylcheck
usermod -a -G audio vinylcheck
install -d -m 755 -o root -g root /opt/vinyl-check /etc/vinyl-check
if mountpoint -q /mnt/data; then install -d -m 750 -o vinylcheck -g vinylcheck /mnt/data/vinyl-check; fi
REMOTE

echo "[3/5] Code (to /opt/vinyl-check.staged) + unit"
# Code as a tarball over the master connection into the staging directory; it is swapped in when the service starts
tar -C "$DIR" --exclude='__pycache__' --exclude='.pytest_cache' -czf - vinylcheck tests \
  | "${SSH[@]}" "sudo bash -c 'rm -rf /opt/vinyl-check.staged && mkdir /opt/vinyl-check.staged && tar -xzf - -C /opt/vinyl-check.staged \
      && chown -R root:root /opt/vinyl-check.staged && chmod -R go-w /opt/vinyl-check.staged'"
"${SSH[@]}" "sudo install -m 644 -o root -g root /dev/stdin /etc/systemd/system/vinyl-check.service && sudo systemctl daemon-reload" < "$DIR/systemd/vinyl-check.service"
"${SSH[@]}" "sudo test -f /etc/vinyl-check/config.toml || sudo install -m 640 -o root -g vinylcheck /dev/stdin /etc/vinyl-check/config.toml" < "$DIR/config.example.toml"

if [ $RUN_TESTS -eq 1 ]; then
  echo "[4/5] Tests on the Pi (Debian package versions)"
  "${SSH[@]}" "cd /tmp && PYTHONPATH=/opt/vinyl-check.staged PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider /opt/vinyl-check.staged/tests 2>&1 | tail -3"
else
  echo "[4/5] Tests skipped"
fi

# Is anything running? Output: "<busy 0/1> <knows the restart marker 0/1>"; no answer (service down) gives "0 0"
busy_state() {
  "${SSH[@]}" "curl -fsS -m 5 localhost:8080/api/status 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin); w = d[\"worker\"]
print(int(bool(d[\"recorder\"][\"active\"]) or bool(w[\"current\"]) or w[\"queued\"] > 0), int(\"restart_pending\" in d))' 2>/dev/null || echo '0 0'"
}
read -r BUSY KNOWS_MARKER <<< "$(busy_state)"
if [ "$BUSY" = "1" ] && [ $NOW -eq 0 ] && [ "$KNOWS_MARKER" = "0" ]; then
  echo "[5/5] The Pi is busy and the running version does not know the idle restart yet: waiting (at most 3 h)"
  for i in $(seq 1 540); do
    sleep 20
    read -r BUSY KNOWS_MARKER <<< "$(busy_state)"
    [ "$BUSY" = "0" ] && break
  done
  [ "$BUSY" = "0" ] || { echo "    still busy after 3 h, no restart. Later: ./deploy.sh --no-test"; exit 1; }
  echo "    idle after $((i*20)) s"
fi
if [ "$BUSY" = "1" ] && [ $NOW -eq 0 ]; then
  echo "[5/5] The Pi is busy (recording or analysis): the restart follows by itself once idle"
  # Both marker names: the service running right now may be a version from before 1.0
  "${SSH[@]}" "sudo bash -c 'for f in restart-when-idle restart-wenn-frei; do touch /var/lib/vinyl-check/\$f \
      && chown vinylcheck:vinylcheck /var/lib/vinyl-check/\$f; done'"
  echo "    Until then the web interface shows that a new version is ready."
  echo "    Follow it with: ssh $TARGET journalctl -u vinyl-check -f"
  echo "=== staged: $VERSION is waiting for the service to become idle ==="
  exit 0
fi
echo "[5/5] Restarting the service"
"${SSH[@]}" "sudo systemctl enable -q vinyl-check && sudo systemctl restart vinyl-check \
  && for i in 1 2 3 4 5 6 7 8 9 10; do sleep 2; curl -fsS localhost:8080/api/health 2>/dev/null && echo && break; done \
  && systemctl is-active vinyl-check"
LIVE="$("${SSH[@]}" "curl -fsS -m 5 localhost:8080/api/status 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)[\"version\"])' 2>/dev/null || echo '?'")"
if [ "$LIVE" != "$VERSION" ]; then
  echo "ERROR: the Pi reports version $LIVE, expected $VERSION (code not swapped in? journalctl -u vinyl-check)"
  exit 1
fi
echo "=== done: $VERSION is running, http://${TARGET#*@}:8080 ==="

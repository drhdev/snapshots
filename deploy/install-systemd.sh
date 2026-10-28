#!/usr/bin/env bash
# Installs (or updates) the systemd service and timer for snapshots.py.
# Run it as the user that owns the installation, it asks for sudo where needed:
#   ./deploy/install-systemd.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
UNIT_DIR=/etc/systemd/system

if [[ "$RUN_USER" == "root" ]]; then
    echo "Please run this script as the user that should run the snapshots, not as root." >&2
    exit 1
fi
if [[ ! -x "$DIR/venv/bin/python" ]]; then
    echo "Missing $DIR/venv. Create it first: python3 -m venv venv && venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if [[ ! -f "$DIR/snapshots.config" ]]; then
    echo "Missing $DIR/snapshots.config. Copy snapshots.config.sample and configure it first." >&2
    exit 1
fi

# The service may only write to logs/ (lock file and log file)
mkdir -p "$DIR/logs"
chmod 600 "$DIR/snapshots.config"
find "$DIR/configs" -maxdepth 1 -name '*.json' -exec chmod 600 {} +

echo "Installing units for user '$RUN_USER' in $DIR"
sed -e "s|__USER__|$RUN_USER|g" -e "s|__DIR__|$DIR|g" "$DIR/deploy/systemd/snapshots.service" \
    | sudo tee "$UNIT_DIR/snapshots.service" > /dev/null
sudo cp "$DIR/deploy/systemd/snapshots.timer" "$UNIT_DIR/snapshots.timer"

# Makes time-sync.target wait until the clock is really synchronised (Raspberry Pi OS uses systemd-timesyncd)
if systemctl is-active --quiet systemd-timesyncd.service; then
    sudo systemctl enable systemd-time-wait-sync.service
else
    echo "Note: systemd-timesyncd is not active, make sure the clock is synchronised by NTP." >&2
fi

sudo systemctl daemon-reload
sudo systemctl enable --now snapshots.timer

echo
systemctl list-timers snapshots.timer --no-pager
echo
echo "Done. Start a run now with:  sudo systemctl start snapshots.service"
echo "Follow the output with:      journalctl -u snapshots.service -f"

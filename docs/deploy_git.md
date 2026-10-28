# Deployment on a Raspberry Pi with git and systemd

This guide installs `snapshots.py` on a Raspberry Pi (tested setup: Raspberry Pi 3, Raspberry Pi OS
Lite, Bookworm or newer) in the home directory of the user that runs it, sets up the
configuration, a daily systemd timer and an Uptime Kuma heartbeat, and shows how to update it
with `git pull`.

The examples use the user `pi` and the directory `~/python/snapshots`, i.e. `/home/pi/python/snapshots`.
Use your own user name; the commands below take it from `$USER` and `~` automatically.

**Contents**

1. [Prepare the Raspberry Pi](#1-prepare-the-raspberry-pi)
2. [Download with git](#2-download-with-git)
3. [Python environment](#3-python-environment)
4. [API tokens and server IDs](#4-api-tokens-and-server-ids)
5. [Server configuration files](#5-server-configuration-files)
6. [Global configuration](#6-global-configuration-snapshotsconfig)
7. [Uptime Kuma push monitor](#7-uptime-kuma-push-monitor)
8. [Test by hand](#8-test-by-hand)
9. [Install the systemd timer](#9-install-the-systemd-timer)
10. [Test the systemd service](#10-test-the-systemd-service)
11. [Daily operation](#11-daily-operation)
12. [Updates with git pull](#12-updates-with-git-pull)
13. [Troubleshooting](#13-troubleshooting)
14. [Uninstall](#14-uninstall)

---

## 1. Prepare the Raspberry Pi

Log in to the Pi (e.g. `ssh pi@raspberrypi.local`) and install the required packages:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
python3 --version   # must be 3.9 or newer
```

Set the time zone, it decides when the daily run starts:

```bash
sudo timedatectl set-timezone Europe/Berlin
timedatectl
```

`timedatectl` must show `System clock synchronized: yes` and `NTP service: active`. The Raspberry Pi
has no hardware clock and gets its time from the network after every boot.

## 2. Download with git

```bash
mkdir -p ~/python
cd ~/python
git clone https://github.com/drhdev/mdosnapshots.git snapshots
cd ~/python/snapshots
```

The repository content is now in `~/python/snapshots`. All following commands are run in this
directory unless stated otherwise.

## 3. Python environment

The virtual environment keeps the dependencies separate from the system Python:

```bash
cd ~/python/snapshots
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 4. API tokens and server IDs

### DigitalOcean

1. Open [API → Tokens](https://cloud.digitalocean.com/account/api/tokens) and click **Generate New Token**
2. Name: e.g. `snapshots-raspberrypi`
3. **Expiration**: choose **No expiry**, or put a reminder in your calendar. An expired token makes
   every run fail (the heartbeat will report it).
4. **Scopes**: **Full Access**, or **Custom Scopes** with read and write access to Droplets,
   Snapshots and Actions
5. Copy the token, it is only shown once

The **droplet ID** is the number in the browser address bar when you open a droplet:
`https://cloud.digitalocean.com/droplets/`**`412345678`**`/graphs`

### Hetzner Cloud

1. Open the [Cloud Console](https://console.hetzner.cloud/), select the project
2. **Security** → **API Tokens** → **Generate API Token**
3. Permission: **Read & Write**
4. Copy the token, it is only shown once. Tokens are per project: servers in different projects
   need different tokens.

The **server ID** is the number in the address bar when you open a server:
`https://console.hetzner.cloud/projects/1234567/servers/`**`48123456`**`/overview`

### Optional: list IDs and names with the API

`read -s` keeps the token out of the shell history:

```bash
read -s -p "DigitalOcean token: " TOKEN; echo
curl -s -H "Authorization: Bearer $TOKEN" "https://api.digitalocean.com/v2/droplets?per_page=200" \
  | python3 -c 'import json,sys; [print(d["id"], d["name"]) for d in json.load(sys.stdin)["droplets"]]'

read -s -p "Hetzner token: " TOKEN; echo
curl -s -H "Authorization: Bearer $TOKEN" "https://api.hetzner.cloud/v1/servers?per_page=50" \
  | python3 -c 'import json,sys; [print(s["id"], s["name"]) for s in json.load(sys.stdin)["servers"]]'
unset TOKEN
```

### Optional: Telegram bot

1. In Telegram, write to [@BotFather](https://t.me/BotFather): `/newbot`, choose a name. You get the **bot token**.
2. Send any message to your new bot (or add it to a group and write there)
3. Get the **chat ID**:

```bash
read -s -p "Bot token: " BOT; echo
curl -s "https://api.telegram.org/bot$BOT/getUpdates" \
  | python3 -c 'import json,sys; [print(u["message"]["chat"]["id"], u["message"]["chat"].get("title") or u["message"]["chat"].get("first_name")) for u in json.load(sys.stdin)["result"] if "message" in u]'
unset BOT
```

## 5. Server configuration files

Create one JSON file per server in `configs/`. The file name is free, it only has to end in `.json`.

```bash
cd ~/python/snapshots
cp configs/hetzner_cloud_server1.json.sample configs/hetzner_web1.json
cp configs/digitalocean_droplet1.json.sample configs/digitalocean_shop.json
nano configs/hetzner_web1.json
```

Example for a Hetzner server, with Telegram credentials taken from `snapshots.config` and no webhook:

```json
{
  "hetzner_cloud_server": {
    "provider": "hetzner",
    "id": "48123456",
    "name": "web-1",
    "api_token": "PASTE_HETZNER_TOKEN_HERE",
    "retain_last_snapshots": 3,
    "telegram": { "enabled": true },
    "webhook": { "enabled": false },
    "heartbeat": { "enabled": false }
  }
}
```

A DigitalOcean droplet looks the same with `"digitalocean_droplet"` as top-level key and
`"provider": "digitalocean"`.

| Field | What to enter |
|---|---|
| `id` | Server or droplet ID from step 4 |
| `name` | Short name without spaces, used in snapshot names (`web-1-20250101030012`). **Do not change it later**: snapshots with the old name are no longer cleaned up. If the server already has snapshots from an earlier version of this script, use the same name as before. |
| `api_token` | Token from step 4 |
| `retain_last_snapshots` | How many older snapshots to keep in addition to the new one. `3` means 4 snapshots exist after a successful run. Snapshots cost money at both providers. |
| `telegram`, `webhook` | `{"enabled": false}` switches the channel off for this server. `{"enabled": true}` without credentials uses those from `snapshots.config`. Omit the block to follow the global `enabled` setting. |
| `heartbeat` | Optional own Uptime Kuma monitor for this server, see step 7 |

In nano: save with `Ctrl+O`, `Enter`, quit with `Ctrl+X`.

Check the JSON syntax and protect the files, they contain tokens:

```bash
for f in configs/*.json; do python3 -m json.tool "$f" > /dev/null && echo "OK  $f"; done
chmod 600 configs/*.json
```

## 6. Global configuration (`snapshots.config`)

```bash
cd ~/python/snapshots
cp snapshots.config.sample snapshots.config
chmod 600 snapshots.config
nano snapshots.config
```

Every option is explained in the file. Usually you only change these:

```ini
[TIMING]
# Upper limit per snapshot. Larger servers can take more than 15 minutes.
snapshot_creation_timeout = 3600

[LOGGING]
level = INFO

[TELEGRAM]
enabled = true
bot_token = 123456789:AAH...your bot token...
chat_id = 987654321

[HEARTBEAT]
enabled = true
url = https://uptime.example.com/api/push/AbCdEf1234?status=up&msg=OK&ping=
```

To keep Telegram off completely, set `enabled = false` in `[TELEGRAM]`. The heartbeat URL comes
from the next step.

## 7. Uptime Kuma push monitor

The heartbeat reports a failed run **and** a run that did not happen at all (Pi switched off,
SD card broken, timer disabled). After each run the script calls the push URL with `status=up` and
a message like `OK: 12/12 snapshots created in 47min`, or with `status=down` and the reason, e.g.
`FAILED 1/12 in 52min: web-1`. If no push arrives within the heartbeat interval, Uptime Kuma marks
the monitor as down by itself.

### Create the monitor

1. Open Uptime Kuma and click **Add New Monitor**
2. **Monitor Type**: `Push`
3. **Friendly Name**: `Snapshots (Raspberry Pi)`
4. **Push URL**: Uptime Kuma shows a URL like
   `https://uptime.example.com/api/push/AbCdEf1234?status=up&msg=OK&ping=`.
   Click **Copy**. Keep this URL private, anyone who has it can report to the monitor.
5. **Heartbeat Interval**: `90000` seconds (25 hours) for a daily run.
   If your Uptime Kuma version does not accept values above `86400`, enter `86400`.
6. **Retries**: `1`
7. **Heartbeat Retry Interval**: `7200` seconds (2 hours)
8. **Notifications**: enable the channels that should alert you (e.g. Telegram, e-mail).
   If none exist yet, create one under **Settings → Notifications** first.
9. Click **Save**

The monitor shows *Pending* until the first push arrives.

Why these values: the run starts at 03:00 (plus up to 10 minutes random delay) and takes some
minutes up to a few hours, so the time between two pushes is about 24 hours plus the difference in
run time. Interval plus retry interval (here 27 hours) must be longer than that, otherwise you get
false alarms on days with a slow run. With **Retries** `1`, Uptime Kuma first switches to
*Pending* and only alerts after the retry interval.

### Enter the URL

Paste the copied URL unchanged into `snapshots.config`:

```ini
[HEARTBEAT]
enabled = true
url = https://uptime.example.com/api/push/AbCdEf1234?status=up&msg=OK&ping=
```

The script replaces `status`, `msg` and `ping` on every call and masks the URL in the log.

### Optional: one monitor per server

To see every server separately, create one more push monitor per server with the same settings and
enter its URL in the server file:

```json
"heartbeat": { "enabled": true, "url": "https://uptime.example.com/api/push/Web1Token" }
```

It reports `OK: web-1-20250101030012, 4 snapshots exist` or `FAILED: <reason>` for this server.
The global monitor keeps reporting the run as a whole.

## 8. Test by hand

Run these steps as your normal user in `~/python/snapshots`.

**a) Reach Uptime Kuma from the Pi**: send a test push with the URL from step 7 (replace the
parameters at the end):

```bash
curl "https://uptime.example.com/api/push/AbCdEf1234?status=up&msg=manual-test"
```

The answer must be `{"ok":true}` and the monitor turns green.

**b) Dry run**: reads all configs and lists snapshots, but creates and deletes nothing and sends no
notifications:

```bash
venv/bin/python snapshots.py --dry-run -v
```

Check for every server:

- no `Skipping '...json'` errors (invalid config)
- `Found N managed snapshot(s)` matches the snapshots you expect
- the `would delete` lines only list snapshots you really want to lose

**c) One real run for a single server**: this creates a real snapshot and applies the retention:

```bash
venv/bin/python snapshots.py hetzner_web1.json -v
echo "exit code: $?"
```

Expected: `Snapshot web-1-... created`, a `FINAL_STATUS ... SUCCESS` line and exit code `0`. The
Telegram message and the heartbeat arrive. The snapshot shows up in the provider's console.

## 9. Install the systemd timer

The repository contains a service and a timer in `deploy/systemd/`. The install script fills in
your user name and directory, copies both to `/etc/systemd/system/`, makes `time-sync.target`
wait for a synchronised clock and enables the timer:

```bash
cd ~/python/snapshots
./deploy/install-systemd.sh
```

Run it as your normal user (not with `sudo`), it asks for the sudo password itself. At the end it
shows the next start time:

```
NEXT                        LEFT      LAST PASSED UNIT              ACTIVATES
Thu 2025-01-02 03:04:51 CET 10h left  -    -      snapshots.timer   snapshots.service
```

What the service does (see `deploy/systemd/snapshots.service`):

- runs `venv/bin/python snapshots.py` as your user in `~/python/snapshots`
- starts only after the network is up and the clock is synchronised
- stops a run that hangs for more than 12 hours (`TimeoutStartSec`); the script then reports `down`
  to the heartbeat
- runs with low CPU and disk priority and may only write to `logs/`

The timer starts the service daily at 03:00 (plus up to 10 minutes) and catches up on a missed run
after the Pi was switched off. To change the time, edit `OnCalendar` in
`deploy/systemd/snapshots.timer` and run the install script again.

## 10. Test the systemd service

Start a run now (this creates real snapshots for all servers) and follow it:

```bash
sudo systemctl start --no-block snapshots.service
journalctl -u snapshots.service -f
```

`Ctrl+C` ends the `journalctl` view, not the run. Without `--verbose` the journal only shows warnings
and errors; the full log is in the log file:

```bash
tail -f ~/python/snapshots/logs/snapshots.log
```

After the run:

```bash
systemctl status snapshots.service      # "Deactivated successfully" / status=0/SUCCESS
systemctl list-timers snapshots.timer   # LAST shows the run, NEXT the next one
grep FINAL_STATUS ~/python/snapshots/logs/snapshots.log | tail
```

In Uptime Kuma the monitor shows the message `OK: ... snapshots created in ...`.

## 11. Daily operation

| Task | Command |
|---|---|
| When is the next run? | `systemctl list-timers snapshots.timer` |
| Result of the last run | `systemctl status snapshots.service` |
| Results per server | `grep FINAL_STATUS ~/python/snapshots/logs/snapshots.log \| tail -20` |
| Full log | `less ~/python/snapshots/logs/snapshots.log` |
| Run now | `sudo systemctl start --no-block snapshots.service` |
| Stop a running run | `sudo systemctl stop snapshots.service` (reports `down` to the heartbeat) |
| Pause the daily runs | `sudo systemctl disable --now snapshots.timer` |
| Resume | `sudo systemctl enable --now snapshots.timer` |
| Add a server | create `configs/<name>.json`, `chmod 600` it, `venv/bin/python snapshots.py --dry-run -v` |

The log file is rotated at 1 MB and keeps 2 old files, so it never uses more than 3 MB. To write
nothing at all to the SD card, set `file_enabled = false` in `[LOGGING]`; the full log then goes to
the journal (`journalctl -u snapshots.service`).

## 12. Updates with git pull

Your configuration (`snapshots.config`, `configs/*.json`) and the logs are not part of the
repository, `git pull` never changes them.

```bash
cd ~/python/snapshots
git pull --ff-only
venv/bin/pip install -r requirements.txt
venv/bin/python snapshots.py --dry-run -v
```

Then check what changed:

```bash
git log --oneline ORIG_HEAD..HEAD           # new commits
git diff --stat ORIG_HEAD HEAD              # changed files
```

- **New options in `snapshots.config.sample`**: missing options in your `snapshots.config` use their
  defaults, so nothing breaks. To adopt a new option, compare the files and copy what you need:
  `diff snapshots.config.sample snapshots.config`
- **Files in `deploy/` changed**: install the units again with `./deploy/install-systemd.sh`
- `git pull --ff-only` refuses to merge if you changed tracked files yourself. Show them with
  `git status`, then keep a copy and reset them, e.g. `git stash`, pull again, `git stash pop`.

To go back to the previous version: `git checkout ORIG_HEAD` right after the pull, or
`git log --oneline` and `git checkout <commit>`. Back to the latest version: `git checkout main && git pull --ff-only`.

## 13. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `status=203/EXEC` in `systemctl status` | `venv` missing or moved: repeat step 3, then `./deploy/install-systemd.sh` |
| `Read-only file system` in the journal | `logs_dir` in `snapshots.config` points outside `logs/`. The service may only write to `logs/`; keep the default. |
| `Skipping 'x.json': ...` | The reason is in the message; fix the file and run `--dry-run -v` |
| `HTTP 401` | Token wrong, revoked or expired; create a new one (step 4) |
| `HTTP 404` when creating a snapshot | Wrong `id`, or the token belongs to another Hetzner project |
| `not confirmed within 3600s` | Snapshot took longer than `snapshot_creation_timeout`. Nothing was deleted; raise the value. |
| `Another instance is already running` | A run is still active: `systemctl status snapshots.service` |
| `Files with credentials are readable by other users` | `chmod 600 snapshots.config configs/*.json` |
| Uptime Kuma stays *Pending* | No push yet. Test with `curl` (step 8a) and check `[HEARTBEAT] enabled = true`. |
| Uptime Kuma shows *Down* with a message | The message says which server failed; details in `logs/snapshots.log` |
| Uptime Kuma shows *Down* without a message | No push within the interval: Pi off, timer disabled (`systemctl list-timers`) or no network |
| Run starts long after 03:00 after a reboot | Expected: `Persistent=true` catches up, and the service waits for the clock to be synchronised |

Exit codes: `0` all snapshots created, `1` all failed / configuration error / stopped, `2` some
servers failed, `3` another instance is running. `systemctl status` shows any code other than `0` as
failed.

## 14. Uninstall

```bash
sudo systemctl disable --now snapshots.timer
sudo rm /etc/systemd/system/snapshots.service /etc/systemd/system/snapshots.timer
sudo systemctl daemon-reload
rm -rf ~/python/snapshots
```

Existing snapshots at DigitalOcean and Hetzner stay and keep costing money until you delete them in
the provider's console. Delete the Uptime Kuma monitor and revoke the API tokens as well.

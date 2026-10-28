# snapshots – automatic snapshots for DigitalOcean and Hetzner Cloud

`snapshots.py` creates a snapshot of each configured DigitalOcean droplet and Hetzner Cloud
server, deletes old snapshots according to a retention policy and reports the result via
Telegram, webhook (e.g. n8n) and a heartbeat monitor (e.g. Uptime Kuma).

It is a single Python file with `requests` as its only dependency and is built to run
unattended on small hardware such as a Raspberry Pi 3.

## Table of Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Configuration](#configuration)
- [Heartbeat with Uptime Kuma](#heartbeat-with-uptime-kuma)
- [Running it](#running-it)
- [Logging](#logging)
- [Exit codes](#exit-codes)
- [Development and tests](#development-and-tests)
- [License](#license)

## Features

- **DigitalOcean and Hetzner Cloud** via their REST APIs, no CLI tools needed
- **Safe retention**: old snapshots are only deleted after the new one is confirmed
- **Manual snapshots are never touched**: only snapshots of the configured server that follow
  the script's naming scheme are managed
- **Retries** for network problems, rate limits (HTTP 429) and server errors (5xx)
- **Notifications** via Telegram and webhooks, with templates per server or globally
- **Heartbeat** to Uptime Kuma (or any push URL) for the whole run and optionally per server
- **No secrets in logs or notifications**: tokens and webhook/heartbeat URLs are masked
- **Small, rarely written log file** to protect SD cards; file logging can be switched off
- **Dry run** to check what would happen without changing anything
- **Lock file** prevents two runs at the same time

## How it works

For each server, one after another:

1. List the existing snapshots of this server that were created by the script
2. Create a new snapshot named `<name>-<YYYYmmddHHMMSS>` and wait until the provider reports it as done
3. Only if the new snapshot is confirmed: delete the oldest snapshots so that
   `retain_last_snapshots` previous snapshots remain **in addition to** the new one
4. Send Telegram/webhook notifications and the per-server heartbeat

After all servers, the global heartbeat reports `up` (all snapshots created) or `down`.

A snapshot counts as **managed** when both apply:

- it was taken from the configured server (DigitalOcean `resource_id`, Hetzner `created_from.id`)
- its name is exactly `<name>-<14 digits>`, where `<name>` is the `name` from the server config

So a snapshot you create by hand (e.g. `before-upgrade`) is never deleted. If you rename a server
in its config, snapshots with the old name are no longer managed and have to be deleted by hand.

If creating the snapshot fails or is not confirmed within `snapshot_creation_timeout`, no
snapshots are deleted and the server counts as failed. The next run cleans up as usual.

## Installation

Raspberry Pi OS (or any Linux) with Python 3.9 or newer:

```bash
sudo apt install python3-venv git
git clone https://github.com/drhdev/mdosnapshots.git snapshots
cd snapshots
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

Create API tokens with read and write access:

- **DigitalOcean**: [API → Tokens](https://cloud.digitalocean.com/account/api/tokens)
- **Hetzner Cloud**: Cloud Console → your project → Security → API Tokens

## Configuration

Configuration files with credentials are excluded from git. Start from the samples:

```bash
cp snapshots.config.sample snapshots.config
cp configs/digitalocean_droplet1.json.sample configs/my-droplet.json
cp configs/hetzner_cloud_server1.json.sample configs/my-server.json
chmod 600 snapshots.config configs/*.json
```

The script warns in the log if these files are readable by other users.

### Server files (`configs/*.json`)

Every `*.json` file in `configs/` is one server. The top-level key selects the provider:
`digitalocean_droplet` or `hetzner_cloud_server`.

| Field | Required | Description |
|---|---|---|
| `provider` | no | `digitalocean` or `hetzner`, must match the top-level key |
| `id` | yes | Droplet ID or Hetzner server ID |
| `name` | yes | Used for snapshot names (`<name>-<timestamp>`) and messages |
| `api_token` | yes | API token of the account or project |
| `retain_last_snapshots` | yes | Previous snapshots to keep in addition to the new one (`0` = keep only the new one) |
| `telegram` | no | `enabled`, `bot_token`, `chat_id`, `message_success`, `message_failure` |
| `webhook` | no | `enabled`, `url`, `payload_success`, `payload_failure` (JSON objects) |
| `heartbeat` | no | `enabled`, `url`: push URL for a monitor of this one server |

Example:

```json
{
  "hetzner_cloud_server": {
    "provider": "hetzner",
    "id": "12345678",
    "name": "web-1",
    "api_token": "...",
    "retain_last_snapshots": 3,
    "telegram": { "enabled": true },
    "heartbeat": { "enabled": true, "url": "https://uptime.example.com/api/push/AbCdEf1234" }
  }
}
```

Values that still contain the sample placeholders (`your_...`, `example.com`) are treated as empty.

### Global settings (`snapshots.config`)

All options are documented in [`snapshots.config.sample`](snapshots.config.sample).

| Section | Purpose |
|---|---|
| `[DIRECTORIES]`, `[FILES]` | Location of server configs, log file and lock file |
| `[TIMING]` | Pause between servers, snapshot timeout (default 3600 s), poll interval |
| `[LOGGING]` | Log file on/off, level, rotation size |
| `[TELEGRAM]` | Global Telegram credentials and message templates |
| `[WEBHOOK]` | Global webhook URL and payload templates |
| `[HEARTBEAT]` | Push URL for the whole run, see below |

### Which notification settings apply

For Telegram and webhooks:

1. `enabled` in the server file wins (`true` or `false`)
2. Without `enabled` in the server file, `enabled` from `snapshots.config` applies
3. Credentials, URL and templates come from the server file, otherwise from `snapshots.config`
4. Without any template, a built-in message or payload is used

### Template placeholders

Usable in Telegram messages and in string values of webhook payloads:

`{script}` `{provider}` `{server_name}` `{server_id}` `{snapshot_name}` `{total_snapshots}`
`{snapshot_info}` `{status}` (`SUCCESS`/`FAILURE`) `{hostname}` `{timestamp}` `{error}`

`{droplet_name}` and `{droplet_id}` still work for older templates.

## Heartbeat with Uptime Kuma

The heartbeat tells you both when a snapshot failed **and** when the script did not run at all,
e.g. because the Raspberry Pi is off, the SD card died or the cron job is missing.

After every run the script calls the push URL:

| Situation | Sent to Uptime Kuma |
|---|---|
| All snapshots created | `status=up`, `msg=OK: 12/12 snapshots created in 47min` |
| At least one snapshot failed | `status=down`, `msg=FAILED 1/12 in 52min: web-1` |
| Configuration error or crash | `status=down`, `msg=Configuration error: ...` / `Crashed: ...` |
| Script did not run | nothing; Uptime Kuma marks the monitor down when the interval passes |

`--dry-run` and a second instance blocked by the lock file send no heartbeat.

### 1. Create the push monitor in Uptime Kuma

1. In Uptime Kuma click **Add New Monitor**
2. **Monitor Type**: `Push`
3. **Friendly Name**: e.g. `Snapshots (Raspberry Pi)`
4. **Push URL**: shown by Uptime Kuma, e.g.
   `https://uptime.example.com/api/push/AbCdEf1234?status=up&msg=OK&ping=`
   Copy it with the **Copy** button.
5. **Heartbeat Interval**: time between two runs plus the maximum run time.
   For a daily run use `90000` (25 hours).
   If your Uptime Kuma version does not accept more than `86400`, enter `86400` and use
   the retry settings below as buffer.
6. **Retries**: `1`
7. **Heartbeat Retry Interval**: `7200` (2 hours). Uptime Kuma first shows the monitor as
   *Pending* and only alerts after this extra time, which absorbs runs that take longer than usual.
8. Under **Notifications**, enable the channels Uptime Kuma should alert (e.g. Telegram, e-mail)
9. Click **Save**

The monitor stays *Pending* until the first push arrives.

### 2. Enter the push URL in `snapshots.config`

```ini
[HEARTBEAT]
enabled = true
url = https://uptime.example.com/api/push/AbCdEf1234?status=up&msg=OK&ping=
```

You can paste the URL exactly as copied: the script replaces `status`, `msg` and `ping` itself.
The URL contains the push token, so it is treated as a secret and masked in the log.

### 3. Test it

Send a manual test push, then run the script once:

```bash
curl "https://uptime.example.com/api/push/AbCdEf1234?status=up&msg=manual-test"
venv/bin/python snapshots.py --verbose
```

In Uptime Kuma the monitor should turn green and show the message
`OK: ... snapshots created in ...`.

### Optional: one monitor per server

To see each server separately in Uptime Kuma, create one push monitor per server (same interval
settings) and enter its URL in the server file:

```json
"heartbeat": { "enabled": true, "url": "https://uptime.example.com/api/push/Server1Token" }
```

The per-server heartbeat reports `OK: <snapshot>, <n> snapshots exist` or `FAILED: <error>`.
The global heartbeat in `snapshots.config` keeps reporting the run as a whole.

## Running it

```bash
venv/bin/python snapshots.py                  # all servers in configs/
venv/bin/python snapshots.py my-server.json   # selected servers
venv/bin/python snapshots.py --dry-run -v     # show what would happen, change nothing
```

Run `--dry-run -v` once after setting up or changing a server: it lists the snapshots the
script considers its own and which ones would be deleted.

### Daily with a systemd timer (recommended on a Raspberry Pi)

The Raspberry Pi has no hardware clock. A systemd timer waits until the time is synchronised
and catches up on runs missed while the Pi was off.

`/etc/systemd/system/snapshots.service`:

```ini
[Unit]
Description=Cloud server snapshots
Wants=network-online.target time-sync.target
After=network-online.target time-sync.target

[Service]
Type=oneshot
User=pi
WorkingDirectory=/home/pi/snapshots
ExecStart=/home/pi/snapshots/venv/bin/python snapshots.py
```

`/etc/systemd/system/snapshots.timer`:

```ini
[Unit]
Description=Daily cloud server snapshots

[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

Adjust `User` and the paths to your installation, then enable the timer:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now snapshots.timer
```

Check when it runs next and what the last run did:

```bash
systemctl list-timers snapshots.timer
journalctl -u snapshots.service -n 50
```

### Daily with cron

```cron
0 3 * * * cd /home/pi/snapshots && venv/bin/python snapshots.py
```

Without `--verbose` the console only gets warnings and errors, so cron stays quiet on success.

## Logging

- Log file: `logs/snapshots.log`, rotated at 1 MB with 2 old files kept (at most 3 MB)
- Level `INFO` writes about 10 lines per server and run; the file is opened only when a line is written
- `[LOGGING] file_enabled = false` writes nothing to disk and logs to stderr instead, e.g. into the
  journal when run by systemd
- API tokens, Telegram bot tokens, webhook URLs and heartbeat URLs are masked as `***`, also in
  error messages and in the error text sent with notifications
- Each server ends with one summary line, easy to search for:

```
FINAL_STATUS | hetzner | web-1 | SUCCESS | web-1-20250101030012 | 4 snapshots | 1 deleted
```

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All snapshots created |
| `1` | All servers failed, no valid configuration, or unexpected error |
| `2` | Some servers failed |
| `3` | Another instance is already running |

## Development and tests

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest
```

The tests never contact a real API: all HTTP traffic goes to fake sessions, and any real
request makes the test fail. No credentials are needed.

API references: [DigitalOcean API](https://docs.digitalocean.com/reference/api/api-reference/),
[Hetzner Cloud API](https://docs.hetzner.cloud/),
[Telegram Bot API](https://core.telegram.org/bots/api).

## License

GNU General Public License v3.0, see [LICENSE](LICENSE).

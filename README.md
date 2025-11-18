# snapshots - multiple cloud provider snapshots

![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)

`snapshots` is a Python-based tool designed to manage snapshots for multiple DigitalOcean Droplets and Hetzner Cloud Servers. It automates the creation, retention, and deletion of snapshots, ensuring your servers are backed up efficiently. Additionally, it integrates with Telegram and webhooks to notify you of the snapshot operations' status, providing real-time updates directly to your messaging app or custom endpoints.

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Setting Up Cronjobs](#setting-up-cronjobs)
- [Logging](#logging)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Multi-Provider Support**: Manage snapshots for both DigitalOcean Droplets and Hetzner Cloud Servers.
- **Automated Snapshot Management**: Schedule regular snapshots for your cloud servers.
- **Retention Policy**: Define how many recent snapshots to retain and automatically delete older ones.
- **Telegram Notifications**: Receive real-time updates on snapshot operations via Telegram with customizable message templates.
- **Webhook Notifications**: Send notifications to custom webhook endpoints (n8n compatible) with customizable JSON payloads.
- **Flexible Configuration**: Manage multiple servers with individual settings using JSON configuration files.
- **Fallback Configuration**: Global fallback settings for credentials and message templates in `snapshots.config`.
- **Logging**: Detailed logging with log rotation to monitor operations and troubleshoot issues.

## Prerequisites

- **Python 3.7+**
- **Cloud Provider Account(s)**:
  - **DigitalOcean**: Account with Droplets and API Tokens
  - **Hetzner Cloud**: Account with Cloud Servers and API Tokens
- **API Access**: 
  - **DigitalOcean**: API token with read/write permissions (no CLI tools required)
  - **Hetzner Cloud**: API token with read/write permissions (no CLI tools required)
- **Telegram Bot** (optional): A Telegram bot token and chat ID for sending notifications (can be disabled in config)

## Installation

1. **Clone the Repository**

   ```bash
   git clone https://github.com/drhdev/snapshots.git
   cd snapshots
   ```

2. **Create a Virtual Environment (Optional but Recommended)**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Configure API Tokens**

   - **For DigitalOcean**: Create a Personal Access Token in your [DigitalOcean account settings](https://cloud.digitalocean.com/account/api/tokens) with read/write permissions.
   - **For Hetzner Cloud**: Create an API token in your [Hetzner Cloud Console](https://console.hetzner.cloud/) under `Security` → `API Tokens` with read/write permissions.
   
   **Note**: No CLI tools are required! The script uses direct API calls. Add your API tokens to the server JSON configuration files.

## Configuration

### Initial Setup

**Important**: All configuration files containing credentials (`.config` and `.json` files) are excluded from git to prevent credential leaks. Sample template files with `.sample` extension are provided as examples.

To get started:
1. Copy `snapshots.config.sample` to `snapshots.config` and configure it
2. Copy the sample JSON files from `configs/` directory (e.g., `digitalocean_droplet1.json.sample` → `digitalocean_droplet1.json`) and configure them with your server details

### Configuration Files Overview

The project uses two types of configuration files:

1. **`snapshots.config`** - Main script configuration (INI format)
   - Global settings for directories, logging, timing
   - Global fallback credentials for Telegram and webhooks

2. **Server JSON files** (`configs/*.json`) - Per-server configuration
   - DigitalOcean: `configs/digitalocean_droplet*.json`
   - Hetzner Cloud: `configs/hetzner_cloud_server*.json`
   - Each file contains server-specific settings including API tokens, retention policies, and per-server notification settings

### What Can Be Configured in `snapshots.config`

The `snapshots.config` file (INI format) contains the following configurable sections:

#### `[DIRECTORIES]`
- **`configs_dir`**: Directory containing server JSON configuration files (default: `configs`)
- **`logs_dir`**: Directory for storing log files and lock files (default: `logs`)

#### `[FILES]`
- **`log_file`**: Log file name (default: `snapshots.log`)
- **`lock_file`**: Lock file name to prevent concurrent execution (default: `snapshots.lock`)

#### `[TIMING]`
- **`delay_between_servers`**: Delay in seconds between processing different servers (default: `20`)

#### `[LOGGING]`
- **`max_bytes`**: Maximum log file size in bytes before rotation (default: `5242880` = 5MB)
- **`backup_count`**: Number of backup log files to keep (default: `5`)
- **`level`**: Log level - `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` (default: `DEBUG`)

#### `[TELEGRAM]` (Global Fallback)
These settings are used as **global fallback** when per-server Telegram settings are not defined in JSON files. Per-server settings take precedence.

- **`enabled`**: Enable Telegram notifications globally (default: `true`)
- **`bot_token`**: Telegram bot token (leave empty to set per-server)
- **`chat_id`**: Telegram chat ID (leave empty to set per-server)
- **`timeout`**: API timeout in seconds (default: `10`)
- **`retries`**: Number of retries when sending messages fails (default: `3`)
- **`base_delay_between_retries`**: Base delay in seconds for exponential backoff (default: `2`)
- **`message_success`**: Custom message template for successful snapshots (supports template variables, leave empty for default format)
- **`message_failure`**: Custom message template for failed snapshots (supports template variables, leave empty for default format)

#### `[WEBHOOK]` (Global Fallback)
These settings are used as **global fallback** when per-server webhook settings are not defined in JSON files. Per-server settings take precedence.

- **`enabled`**: Enable webhook notifications globally (default: `false`)
- **`url`**: Webhook URL (leave empty to set per-server)
- **`timeout`**: Timeout in seconds for webhook requests (default: `10`)
- **`retries`**: Number of retries when webhook calls fail (default: `3`)
- **`base_delay_between_retries`**: Base delay in seconds for exponential backoff (default: `2`)
- **`payload_success`**: Custom JSON payload template for successful snapshots (JSON string with template variables, leave empty for default structure)
- **`payload_failure`**: Custom JSON payload template for failed snapshots (JSON string with template variables, leave empty for default structure)

### What Can Be Configured in DigitalOcean JSON Files

Create files named `digitalocean_droplet*.json` in the `configs/` directory:

#### Required Fields
- **`provider`**: Must be `"digitalocean"`
- **`id`**: Your DigitalOcean droplet ID (numeric or string)
- **`name`**: Your droplet name (used for snapshot naming)
- **`api_token`**: Your DigitalOcean API token with read/write permissions
- **`retain_last_snapshots`**: Number of recent snapshots to retain (integer, e.g., `3`)

#### Optional Telegram Settings (Per-Server)
If not set, falls back to global Telegram settings from `snapshots.config`:
- **`telegram.enabled`**: Enable Telegram notifications for this server (boolean, default: `false`)
- **`telegram.bot_token`**: Telegram bot token (string, optional)
- **`telegram.chat_id`**: Telegram chat ID (string, optional)
- **`telegram.message_success`**: Custom message template for successful snapshots (string, supports template variables)
- **`telegram.message_failure`**: Custom message template for failed snapshots (string, supports template variables)

#### Optional Webhook Settings (Per-Server)
If not set, falls back to global webhook settings from `snapshots.config`:
- **`webhook.enabled`**: Enable webhook notifications for this server (boolean, default: `false`)
- **`webhook.url`**: Webhook URL to call (string, optional)
- **`webhook.payload_success`**: Custom JSON payload object for successful snapshots (object, supports template variables in string values)
- **`webhook.payload_failure`**: Custom JSON payload object for failed snapshots (object, supports template variables in string values)

#### DigitalOcean JSON Example

```json
{
  "digitalocean_droplet": {
    "provider": "digitalocean",
    "id": "your-digitalocean-droplet-id-1",
    "name": "your-digitalocean-droplet-name-1",
    "api_token": "your_digitalocean_api_token_1",
    "retain_last_snapshots": 3,
    "telegram": {
      "enabled": true,
      "bot_token": "your_telegram_bot_token_here",
      "chat_id": "your_telegram_chat_id_here",
      "message_success": "*Snapshot Success*\nServer: `{server_name}`\nSnapshot: `{snapshot_name}`\nTotal: `{total_snapshots}` snapshots",
      "message_failure": "*Snapshot Failed*\nServer: `{server_name}`\nError occurred during snapshot creation"
    },
    "webhook": {
      "enabled": true,
      "url": "https://your-webhook-url.com/notify",
      "payload_success": {
        "script": "{script}",
        "provider": "digitalocean",
        "server": "{server_name}",
        "server_id": "{server_id}",
        "status": "{status}",
        "hostname": "{hostname}",
        "timestamp": "{timestamp}",
        "snapshot_name": "{snapshot_name}",
        "total_snapshots": "{total_snapshots}",
        "snapshot_info": "{snapshot_info}"
      },
      "payload_failure": {
        "script": "{script}",
        "provider": "digitalocean",
        "server": "{server_name}",
        "server_id": "{server_id}",
        "status": "{status}",
        "hostname": "{hostname}",
        "timestamp": "{timestamp}",
        "snapshot_name": "{snapshot_name}",
        "total_snapshots": "{total_snapshots}",
        "snapshot_info": "{snapshot_info}"
      }
    }
  }
}
```

### What Can Be Configured in Hetzner Cloud JSON Files

Create files named `hetzner_cloud_server*.json` in the `configs/` directory:

#### Required Fields
- **`provider`**: Must be `"hetzner"`
- **`id`**: Your Hetzner Cloud server ID (numeric or string)
- **`name`**: Your server name (used for snapshot naming)
- **`api_token`**: Your Hetzner Cloud API token with read/write permissions
- **`retain_last_snapshots`**: Number of recent snapshots to retain (integer, e.g., `3`)

#### Optional Telegram Settings (Per-Server)
If not set, falls back to global Telegram settings from `snapshots.config`:
- **`telegram.enabled`**: Enable Telegram notifications for this server (boolean, default: `false`)
- **`telegram.bot_token`**: Telegram bot token (string, optional)
- **`telegram.chat_id`**: Telegram chat ID (string, optional)
- **`telegram.message_success`**: Custom message template for successful snapshots (string, supports template variables)
- **`telegram.message_failure`**: Custom message template for failed snapshots (string, supports template variables)

#### Optional Webhook Settings (Per-Server)
If not set, falls back to global webhook settings from `snapshots.config`:
- **`webhook.enabled`**: Enable webhook notifications for this server (boolean, default: `false`)
- **`webhook.url`**: Webhook URL to call (string, optional)
- **`webhook.payload_success`**: Custom JSON payload object for successful snapshots (object, supports template variables in string values)
- **`webhook.payload_failure`**: Custom JSON payload object for failed snapshots (object, supports template variables in string values)

#### Hetzner Cloud JSON Example

```json
{
  "hetzner_cloud_server": {
    "provider": "hetzner",
    "id": "your-hetzner-cloud-server-id-1",
    "name": "your-hetzner-cloud-server-name-1",
    "api_token": "your_hetzner_api_token_1",
    "retain_last_snapshots": 3,
    "telegram": {
      "enabled": true,
      "bot_token": "your_telegram_bot_token_here",
      "chat_id": "your_telegram_chat_id_here",
      "message_success": "*Snapshot Success*\nServer: `{server_name}`\nSnapshot: `{snapshot_name}`\nTotal: `{total_snapshots}` snapshots",
      "message_failure": "*Snapshot Failed*\nServer: `{server_name}`\nError occurred during snapshot creation"
    },
    "webhook": {
      "enabled": true,
      "url": "https://your-webhook-url.com/notify",
      "payload_success": {
        "script": "{script}",
        "provider": "hetzner",
        "server": "{server_name}",
        "server_id": "{server_id}",
        "status": "{status}",
        "hostname": "{hostname}",
        "timestamp": "{timestamp}",
        "snapshot_name": "{snapshot_name}",
        "total_snapshots": "{total_snapshots}",
        "snapshot_info": "{snapshot_info}"
      },
      "payload_failure": {
        "script": "{script}",
        "provider": "hetzner",
        "server": "{server_name}",
        "server_id": "{server_id}",
        "status": "{status}",
        "hostname": "{hostname}",
        "timestamp": "{timestamp}",
        "snapshot_name": "{snapshot_name}",
        "total_snapshots": "{total_snapshots}",
        "snapshot_info": "{snapshot_info}"
      }
    }
  }
}
```

### Template Variables

The following template variables are available in Telegram messages and webhook payloads:

- `{script}`: Script name (snapshots.py)
- `{provider}`: Cloud provider (digitalocean or hetzner)
- `{server_name}`: Server name
- `{server_id}`: Server ID
- `{snapshot_name}`: Snapshot name
- `{total_snapshots}`: Total number of snapshots
- `{snapshot_info}`: Formatted snapshot info string (e.g., "3 snapshots exist")
- `{status}`: Status (SUCCESS or FAILURE)
- `{hostname}`: Hostname of the machine running the script
- `{timestamp}`: Timestamp of the operation

**Backward Compatibility**: The template variables `{droplet_name}` and `{droplet_id}` are still supported but deprecated. Use `{server_name}` and `{server_id}` instead.

### Configuration Priority

The script uses a three-tier priority system for notifications:

1. **Per-server JSON settings** take highest precedence
   - Custom messages/payloads defined in individual server JSON files
   - Per-server credentials (bot_token, chat_id, webhook URL)

2. **Global `snapshots.config` fallback settings** are used when per-server settings are missing
   - Fallback credentials (bot_token, chat_id, webhook URL)
   - Fallback message templates (`message_success`, `message_failure`)
   - Fallback payload templates (`payload_success`, `payload_failure`)

3. **Default standardized format** is used when no custom templates are defined
   - Standardized data structure with all fields
   - Formatted message for Telegram
   - JSON payload for webhooks

**Note**: By default, both Telegram messages and webhook payloads use the same standardized data structure with fields: `script`, `provider`, `server`, `server_id`, `status`, `hostname`, `timestamp`, `snapshot_name`, `total_snapshots`, and `snapshot_info`. This ensures consistency between notification methods.

**Fallback Message Templates**: You can configure fallback message templates in `snapshots.config` that will be used when per-server templates are not defined. This allows you to set a consistent message format across all servers while still allowing per-server customization when needed.

#### Example: Fallback Message Configuration in `snapshots.config`

```ini
[TELEGRAM]
enabled = true
bot_token = your_telegram_bot_token_here
chat_id = your_telegram_chat_id_here
timeout = 10
retries = 3
base_delay_between_retries = 2

# Fallback message templates (used when per-server templates are not defined)
message_success = *Snapshot Success*\nServer: `{server_name}`\nSnapshot: `{snapshot_name}`\nTotal: `{total_snapshots}` snapshots
message_failure = *Snapshot Failed*\nServer: `{server_name}`\nError occurred during snapshot creation

[WEBHOOK]
enabled = false
url = https://your-webhook-url.com/notify
timeout = 10
retries = 3
base_delay_between_retries = 2

# Fallback payload templates (used when per-server payloads are not defined)
# Note: These are JSON strings - use \n for newlines, escape quotes properly
payload_success = {"script": "{script}", "provider": "{provider}", "server": "{server_name}", "server_id": "{server_id}", "status": "{status}", "hostname": "{hostname}", "timestamp": "{timestamp}", "snapshot_name": "{snapshot_name}", "total_snapshots": "{total_snapshots}", "snapshot_info": "{snapshot_info}"}
payload_failure = {"script": "{script}", "provider": "{provider}", "server": "{server_name}", "server_id": "{server_id}", "status": "{status}", "hostname": "{hostname}", "timestamp": "{timestamp}", "snapshot_name": "{snapshot_name}", "total_snapshots": "{total_snapshots}", "snapshot_info": "{snapshot_info}"}
```

**Note**: The `\n` in message templates will be converted to actual newlines. For webhook payloads, use valid JSON format with template variables as string values.

```json
{
  "digitalocean_droplet": {
    "provider": "digitalocean",
    "id": "your-digitalocean-droplet-id-1",
    "name": "your-digitalocean-droplet-name-1",
    "api_token": "your_digitalocean_api_token_1",
    "retain_last_snapshots": 3,
    "telegram": {
      "enabled": true,
      "bot_token": "your_telegram_bot_token",
      "chat_id": "your_telegram_chat_id",
      "message_success": "*Snapshot Success*\nServer: `{server_name}`\nSnapshot: `{snapshot_name}`",
      "message_failure": "*Snapshot Failed*\nServer: `{server_name}`"
    },
    "webhook": {
      "enabled": true,
      "url": "https://your-webhook-url.com/notify",
      "payload_success": {
        "script": "{script}",
        "provider": "digitalocean",
        "server": "{server_name}",
        "server_id": "{server_id}",
        "status": "{status}",
        "hostname": "{hostname}",
        "timestamp": "{timestamp}",
        "snapshot_name": "{snapshot_name}",
        "total_snapshots": "{total_snapshots}",
        "snapshot_info": "{snapshot_info}"
      },
      "payload_failure": {
        "script": "{script}",
        "provider": "digitalocean",
        "server": "{server_name}",
        "server_id": "{server_id}",
        "status": "{status}",
        "hostname": "{hostname}",
        "timestamp": "{timestamp}",
        "snapshot_name": "{snapshot_name}",
        "total_snapshots": "{total_snapshots}",
        "snapshot_info": "{snapshot_info}"
      }
    }
  }
}
```

#### Hetzner Cloud Server Configuration

Create files named `hetzner_cloud_server*.json`:

```json
{
  "hetzner_cloud_server": {
    "provider": "hetzner",
    "id": "your-hetzner-cloud-server-id-1",
    "name": "your-hetzner-cloud-server-name-1",
    "api_token": "your_hetzner_api_token_1",
    "retain_last_snapshots": 3,
    "telegram": {
      "enabled": true,
      "bot_token": "your_telegram_bot_token",
      "chat_id": "your_telegram_chat_id",
      "message_success": "*Snapshot Success*\nServer: `{server_name}`\nSnapshot: `{snapshot_name}`",
      "message_failure": "*Snapshot Failed*\nServer: `{server_name}`"
    },
    "webhook": {
      "enabled": true,
      "url": "https://your-webhook-url.com/notify",
      "payload_success": {
        "script": "{script}",
        "provider": "hetzner",
        "server": "{server_name}",
        "server_id": "{server_id}",
        "status": "{status}",
        "hostname": "{hostname}",
        "timestamp": "{timestamp}",
        "snapshot_name": "{snapshot_name}",
        "total_snapshots": "{total_snapshots}",
        "snapshot_info": "{snapshot_info}"
      },
      "payload_failure": {
        "script": "{script}",
        "provider": "hetzner",
        "server": "{server_name}",
        "server_id": "{server_id}",
        "status": "{status}",
        "hostname": "{hostname}",
        "timestamp": "{timestamp}",
        "snapshot_name": "{snapshot_name}",
        "total_snapshots": "{total_snapshots}",
        "snapshot_info": "{snapshot_info}"
      }
    }
  }
}
```


## Usage

### Managing Snapshots

To manually run the snapshot management script:

```bash
python snapshots.py -v
```

- **Options**:
  - `-v`, `--verbose`: Enable verbose logging to the console.
  - Specify specific configuration files by listing them as arguments.

Example:

```bash
python snapshots.py digitalocean_droplet1.json hetzner_cloud_server1.json -v
```

### Telegram Notifications

Telegram notifications are automatically sent after each server's snapshot management completes. The notifications are configured in the `[TELEGRAM]` section of `snapshots.config` or per-server in the JSON configuration files.

- **Enable/Disable**: Set `enabled = true` or `enabled = false` in the config file
- **Credentials**: Set `bot_token` and `chat_id` in the config file
- **Timing**: Configure delays and retries in the config file
- **Per-Server**: Each server can have its own Telegram credentials and custom message templates

Notifications are sent automatically for each `FINAL_STATUS` entry, so no separate script is needed.

### Webhook Notifications

Webhook notifications are automatically sent after each server's snapshot management completes. The webhooks are configured in the `[WEBHOOK]` section of `snapshots.config` or per-server in the JSON configuration files.

- **Enable/Disable**: Set `enabled = true` or `enabled = false` in the config file
- **URL**: Set the webhook URL in the config file or per-server
- **Custom Payloads**: Define custom JSON payloads for success/failure scenarios
- **Per-Server**: Each server can have its own webhook URL and custom payload structure

Webhooks send POST requests with JSON payloads containing the snapshot operation details.

## Setting Up Cronjobs

To automate the snapshot management and notification process, set up a cronjob that runs the script. Telegram notifications are sent automatically if enabled in the configuration.

### Bulletproof Cronjob Setup

For reliable cronjob execution, follow these steps:

1. **Find Your Python Executable Path**

   ```bash
   # If using a virtual environment:
   which python3
   # or
   which python
   
   # Example output: /usr/local/bin/python3 or /path/to/venv/bin/python3
   ```

2. **Find Your Script Directory**

   ```bash
   # Navigate to the snapshots directory and get the absolute path:
   cd /path/to/snapshots
   pwd
   # Example output: /home/user/snapshots or /opt/snapshots
   ```

3. **Open the Crontab Editor**

   ```bash
   crontab -e
   ```

4. **Add the Cronjob Entry**

   The following cronjob runs daily at 6:00 AM and uses bulletproof execution with absolute paths:

   ```cron
   0 6 * * * PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin && cd /absolute/path/to/snapshots && /absolute/path/to/python3 snapshots.py > logs/cronjob.log 2>&1
   ```

   **Important Configuration:**
   - **Time**: `0 6 * * *` = 6:00 AM every day
   - **PATH**: Set explicitly to ensure all commands are found
   - **Working Directory**: `cd` to the script directory before execution
   - **Python Path**: Use absolute path to Python executable
   - **Log File**: `> logs/cronjob.log` overwrites the log file each execution (use `>>` to append instead)
   - **Error Redirection**: `2>&1` redirects stderr to stdout, capturing all output

5. **Example with Virtual Environment**

   If using a virtual environment, use the venv's Python:

   ```cron
   0 6 * * * PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin && cd /home/user/snapshots && /home/user/snapshots/venv/bin/python3 snapshots.py > logs/cronjob.log 2>&1
   ```

6. **Example with System Python**

   If using system Python:

   ```cron
   0 6 * * * PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin && cd /opt/snapshots && /usr/bin/python3 snapshots.py > logs/cronjob.log 2>&1
   ```

7. **Verify the Cronjob**

   ```bash
   # List your cronjobs:
   crontab -l
   
   # Check cron service status (Linux):
   sudo systemctl status cron
   # or
   sudo service cron status
   
   # Check cron service status (macOS):
   sudo launchctl list | grep cron
   ```

8. **Test the Cronjob Manually**

   Before relying on the cronjob, test the command manually:

   ```bash
   # Test the exact command that cron will run:
   PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin && cd /absolute/path/to/snapshots && /absolute/path/to/python3 snapshots.py > logs/cronjob.log 2>&1
   
   # Check the log file:
   cat logs/cronjob.log
   ```

### Cronjob Log File

- **Location**: `logs/cronjob.log` (in the `logs/` subdirectory)
- **Behavior**: Overwritten with each execution (using `>` redirect)
- **Content**: All stdout and stderr output from the script execution
- **Rotation**: The log file is overwritten each time the cronjob runs, keeping only the latest execution output

**Note**: If you prefer to append logs instead of overwriting, change `>` to `>>` in the cronjob entry. However, overwriting (`>`) is recommended for cronjobs to prevent log file growth and make it easier to see the latest execution results.

### Troubleshooting Cronjobs

If the cronjob doesn't run as expected:

1. **Check Cron Service**: Ensure cron is running
   ```bash
   sudo systemctl status cron  # Linux
   sudo service cron status    # Linux (older systems)
   ```

2. **Check Cron Logs**: View system cron logs
   ```bash
   # Linux (varies by distribution):
   sudo tail -f /var/log/cron
   sudo tail -f /var/log/syslog | grep CRON
   
   # macOS:
   log show --predicate 'process == "cron"' --last 1h
   ```

3. **Check Script Logs**: Review the cronjob log file
   ```bash
   cat logs/cronjob.log
   ```

4. **Verify Permissions**: Ensure the script and directories are executable
   ```bash
   chmod +x snapshots.py
   chmod 755 logs/
   ```

5. **Test Environment**: Cron runs with minimal environment variables. The script handles this, but verify paths are absolute.

6. **Check Lock File**: If a previous execution failed, the lock file might still exist
   ```bash
   # Remove lock file if needed (only if you're sure no instance is running):
   rm logs/snapshots.lock
   ```

## Logging

All logs are stored in the `logs/` subdirectory:

- **Snapshot Manager Log**: `logs/snapshots.log` – Logs detailed information about snapshot operations, API calls, and notifications. Uses log rotation to prevent uncontrolled growth.
- **Lock File**: `logs/snapshots.lock` – Prevents concurrent execution (automatically managed).
- **Cronjob Log**: `logs/cronjob.log` – Captures all stdout and stderr output from cron-executed runs. Overwritten with each execution to keep only the latest results.

Logs are managed with log rotation to prevent uncontrolled growth. The `logs/` directory is automatically created if it doesn't exist.

### Reliability Features

- **Lock File Mechanism**: Prevents concurrent execution when run via cronjobs, avoiding conflicts.
- **JSON Validation**: Only processes valid `.json` files. Invalid files are skipped and logged.
- **macOS Resource Fork Handling**: Automatically skips `._*` files (macOS resource fork files) to prevent encoding errors.
- **Provider Detection**: Automatically detects provider type from JSON structure and uses appropriate API endpoints.
- **Error Recovery**: Continues processing remaining servers even if one fails.
- **Direct API Integration**: Uses direct REST API calls instead of CLI tools, eliminating external dependencies.
- **Rate Limit Handling**: Automatically handles API rate limits with exponential backoff and retry logic.
- **Fallback Configuration**: Comprehensive fallback system for credentials and message templates ensures notifications work even when per-server settings are incomplete.
- **Exit Codes**: 
  - `0`: Success
  - `1`: All servers failed or critical error
  - `2`: Partial failure (some servers succeeded, some failed)
  - `3`: Lock file error (another instance is running)

### API Integration and Documentation

The script uses direct API calls to interact with DigitalOcean and Hetzner Cloud services. This eliminates the need for external CLI tools (`doctl` and `hcloud`) and provides better error handling and maintainability.

#### API Documentation References

- **DigitalOcean API v2**: [Official API Reference](https://docs.digitalocean.com/reference/api/api-reference/)
  - **Snapshots Endpoint**: [List Snapshots](https://docs.digitalocean.com/reference/api/api-reference/#operation/list_snapshots)
  - **Droplet Actions**: [Create Snapshot Action](https://docs.digitalocean.com/reference/api/api-reference/#operation/create_droplet_action)
  - **Actions Endpoint**: [Get Action Status](https://docs.digitalocean.com/reference/api/api-reference/#operation/get_action)
  - **Delete Snapshot**: [Delete Snapshot](https://docs.digitalocean.com/reference/api/api-reference/#operation/delete_snapshot)

- **Hetzner Cloud API v1**: [Official API Reference](https://docs.hetzner.cloud/)
  - **Images Endpoint**: [List Images](https://docs.hetzner.cloud/#images-get-all-images)
  - **Server Actions**: [Create Image Action](https://docs.hetzner.cloud/#server-actions-create-an-image-from-a-server)
  - **Delete Image**: [Delete Image](https://docs.hetzner.cloud/#images-delete-an-image)

#### API Endpoints Used in the Script

**DigitalOcean API (Base URL: `https://api.digitalocean.com/v2`):**

1. **List Snapshots** (`GET /snapshots`)
   - Used in: `get_digitalocean_snapshots()`
   - Query Parameters: `resource_type=droplet`, `per_page=200`, `page={page_number}`
   - Authentication: Bearer token in `Authorization` header
   - Response: JSON with `snapshots` array containing snapshot objects

2. **Create Snapshot** (`POST /droplets/{id}/actions`)
   - Used in: `create_digitalocean_snapshot()`
   - Request Body: `{"type": "snapshot", "name": "{snapshot_name}"}`
   - Authentication: Bearer token in `Authorization` header
   - Response: JSON with `action` object containing `id` for status polling

3. **Get Action Status** (`GET /actions/{action_id}`)
   - Used in: `_wait_for_action_completion()`
   - Authentication: Bearer token in `Authorization` header
   - Response: JSON with `action` object containing `status` field (`completed`, `in-progress`, `errored`)

4. **Delete Snapshot** (`DELETE /snapshots/{id}`)
   - Used in: `delete_digitalocean_snapshots()`
   - Authentication: Bearer token in `Authorization` header
   - Response: Empty body (204 No Content on success)

**Hetzner Cloud API (Base URL: `https://api.hetzner.cloud/v1`):**

1. **List Images** (`GET /images`)
   - Used in: `get_hetzner_snapshots()`
   - Query Parameters: `type=snapshot`, `per_page=50`, `page={page_number}`
   - Authentication: Bearer token in `Authorization` header
   - Response: JSON with `images` array containing image objects

2. **Create Image (Snapshot)** (`POST /servers/{id}/actions/create_image`)
   - Used in: `create_hetzner_snapshot()`
   - Request Body: `{"type": "snapshot", "description": "{snapshot_name}"}`
   - Authentication: Bearer token in `Authorization` header
   - Response: JSON with `action` and `image` objects

3. **Delete Image** (`DELETE /images/{id}`)
   - Used in: `delete_hetzner_snapshots()`
   - Authentication: Bearer token in `Authorization` header
   - Response: Empty body (204 No Content on success)

#### Updating the Script for API Changes

If the cloud providers update their APIs, you can update the script by modifying the following methods in `snapshots.py`:

**For DigitalOcean API changes:**
- **List Snapshots**: Update `get_digitalocean_snapshots()` method (around line 655)
  - Modify the URL: `https://api.digitalocean.com/v2/snapshots`
  - Adjust query parameters if pagination or filtering changes
  - Update response parsing if JSON structure changes

- **Create Snapshot**: Update `create_digitalocean_snapshot()` method (around line 731)
  - Modify the URL: `https://api.digitalocean.com/v2/droplets/{id}/actions`
  - Adjust request payload if required fields change
  - Update `_wait_for_action_completion()` if action status polling changes

- **Delete Snapshot**: Update `delete_digitalocean_snapshots()` method (around line 767)
  - Modify the URL: `https://api.digitalocean.com/v2/snapshots/{id}`
  - Adjust error handling if response codes change

**For Hetzner Cloud API changes:**
- **List Snapshots**: Update `get_hetzner_snapshots()` method (around line 677)
  - Modify the URL: `https://api.hetzner.cloud/v1/images`
  - Adjust query parameters if filtering changes
  - Update response parsing if JSON structure changes

- **Create Snapshot**: Update `create_hetzner_snapshot()` method (around line 744)
  - Modify the URL: `https://api.hetzner.cloud/v1/servers/{id}/actions/create_image`
  - Adjust request payload if required fields change

- **Delete Snapshot**: Update `delete_hetzner_snapshots()` method (around line 784)
  - Modify the URL: `https://api.hetzner.cloud/v1/images/{id}`
  - Adjust error handling if response codes change

**Common Update Steps:**
1. Check the official API documentation for breaking changes
2. Update the base URLs if the API version changes (e.g., `/v2` → `/v3`)
3. Modify request/response handling in the `_make_api_request()` helper method if authentication or error handling changes
4. Update pagination logic if pagination structure changes
5. Test thoroughly with a single server before deploying changes

**Rate Limiting:**
Both APIs implement rate limiting. The script handles rate limit responses (HTTP 429) automatically by:
- Reading the `Retry-After` header
- Waiting for the specified duration
- Retrying the request once

If rate limiting behavior changes, update the `_make_api_request()` method accordingly.

### Provider-Specific Notes

#### DigitalOcean
- Uses direct API calls to DigitalOcean API v2
- Snapshots are created via droplet actions endpoint and polled for completion
- Snapshot names follow pattern: `{server-name}-{timestamp}`
- API Base URL: `https://api.digitalocean.com/v2`
- Authentication: Bearer token in `Authorization` header

#### Hetzner Cloud
- Uses direct API calls to Hetzner Cloud API v1
- Snapshots are created as images with `type=snapshot`
- Snapshot names are stored in the `description` field
- API Base URL: `https://api.hetzner.cloud/v1`
- Authentication: Bearer token in `Authorization` header

## Testing and Quality Assurance

The script has been thoroughly tested to ensure production readiness and reliability. Comprehensive testing covers all major functionality, edge cases, error handling, and security aspects.

### Test Coverage

**Configuration Management:**
- ✅ Configuration file loading with defaults
- ✅ Invalid configuration file handling
- ✅ Missing configuration file graceful degradation
- ✅ Configuration validation and error reporting

**Credential Security:**
- ✅ API token sanitization in all log outputs
- ✅ Bot token masking in Telegram notifications
- ✅ Pattern-based credential detection and sanitization
- ✅ Prevention of credential leakage in error messages
- ✅ Multiple token sanitization scenarios
- ✅ Very long token handling
- ✅ Empty and None value handling

**Notification Systems:**
- ✅ Telegram notification success scenarios
- ✅ Telegram notification retry logic with exponential backoff
- ✅ Telegram notification timeout handling
- ✅ Webhook notification success scenarios
- ✅ Webhook notification retry logic
- ✅ Webhook notification timeout handling
- ✅ Custom message template support
- ✅ Missing credentials graceful handling (no errors)

**Server Configuration:**
- ✅ Valid JSON configuration loading
- ✅ Invalid JSON error handling
- ✅ Missing required fields detection
- ✅ Provider type validation
- ✅ Provider mismatch handling
- ✅ Data type validation and conversion
- ✅ Optional field handling (Telegram, webhook)

**Snapshot Operations:**
- ✅ Snapshot retrieval for DigitalOcean
- ✅ Snapshot retrieval for Hetzner Cloud
- ✅ Snapshot creation for both providers
- ✅ Snapshot deletion identification
- ✅ Retention policy enforcement
- ✅ Empty snapshot list handling
- ✅ Boundary conditions (retain all, retain zero)

**Error Handling:**
- ✅ File system errors (missing directories, permission issues)
- ✅ Network errors (timeouts, connection failures)
- ✅ API errors (HTTP errors, rate limiting)
- ✅ Subprocess errors (command failures, missing executables)
- ✅ Lock file errors (acquisition failures, release errors)
- ✅ JSON parsing errors
- ✅ Invalid input handling

**Edge Cases:**
- ✅ Empty data structures
- ✅ None/null values
- ✅ Very long strings and tokens
- ✅ Special characters in server names
- ✅ Boundary values (zero, maximum)
- ✅ Concurrent execution prevention
- ✅ Invalid provider types

**Code Quality:**
- ✅ Import usage validation
- ✅ Constant definitions verification
- ✅ Dataclass structure validation
- ✅ Function signature correctness
- ✅ Type hint compliance

### Security Validation

All security-critical aspects have been validated:

- **Credential Protection**: All API tokens, bot tokens, passwords, and secrets are automatically sanitized before being written to logs
- **Pattern Detection**: Credential patterns are detected and masked even without explicit token parameters
- **Error Message Safety**: Error messages are sanitized to prevent information leakage
- **Git Exclusion**: Configuration files containing credentials are properly excluded from version control

### Reliability Validation

The script has been tested for:

- **Concurrent Execution Prevention**: Lock file mechanism prevents multiple instances from running simultaneously
- **Graceful Degradation**: Script continues processing remaining servers even if one fails
- **Error Recovery**: Proper error handling and recovery mechanisms throughout
- **Resource Management**: Proper cleanup of resources (lock files, file handles)
- **Exit Code Correctness**: Proper exit codes for different scenarios (success, failure, partial failure, lock error)

### Production Readiness

The script has been validated for production use with:

- ✅ Comprehensive error handling
- ✅ Extensive logging with credential sanitization
- ✅ Robust configuration management
- ✅ Security best practices implementation
- ✅ Edge case handling
- ✅ Code quality standards
- ✅ Documentation completeness

All tests validate that the script handles real-world scenarios reliably, including network failures, API errors, invalid configurations, and edge cases that could occur in production environments.

## Contributing

Contributions are welcome! Please fork the repository and submit a pull request for any enhancements or bug fixes.

**Repository**: [https://github.com/drhdev/snapshots](https://github.com/drhdev/snapshots)

## License

This project is licensed under the GNU General Public License v3.0. See the [LICENSE](LICENSE) file for details.

**Author**: drhdev  
**License**: GPL v3

#!/usr/bin/env python3
# snapshots.py
# Version: 0.3
# Author: drhdev
# License: GPL v3
#
# Description:
# This script manages snapshots for multiple DigitalOcean droplets and Hetzner Cloud servers, including creation, retention, and deletion.
# Configuration is handled via JSON files located in the 'configs' subfolder, allowing individual settings per server.
# Telegram notifications are automatically sent after each server's snapshot management completes (if enabled in config).

import logging
from logging.handlers import RotatingFileHandler
import datetime
import os
import sys
import configparser
import json
import time
import argparse
import fcntl
import re
import requests
from dataclasses import dataclass
from typing import List, Optional

# Get project directory (where this script is located)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Configuration file path (relative to project directory)
CONFIG_FILE = os.path.join(PROJECT_DIR, "snapshots.config")

def load_config() -> configparser.ConfigParser:
    """Load configuration from INI file with defaults."""
    config = configparser.ConfigParser()
    
    # Set defaults
    defaults = {
        'DIRECTORIES': {
            'configs_dir': 'configs',
            'logs_dir': 'logs'
        },
        'FILES': {
            'log_file': 'snapshots.log',
            'lock_file': 'snapshots.lock'
        },
        'TIMING': {
            'delay_between_servers': '5',
            'snapshot_creation_timeout': '900'  # 15 minutes in seconds
        },
        'LOGGING': {
            'max_bytes': '5242880',
            'backup_count': '5',
            'level': 'DEBUG'
        },
        'TELEGRAM': {
            'enabled': 'true',
            'bot_token': '',
            'chat_id': '',
            'timeout': '10',
            'retries': '3',
            'base_delay_between_retries': '2',
            'message_success': '*Snapshot Success*\\nServer: `{server_name}`\\nSnapshot: `{snapshot_name}`\\nTotal: `{total_snapshots}` snapshots',
            'message_failure': '*Snapshot Failed*\\nServer: `{server_name}`\\nError occurred during snapshot creation'
        },
        'WEBHOOK': {
            'enabled': 'false',
            'url': '',
            'timeout': '10',
            'retries': '3',
            'base_delay_between_retries': '2',
            'payload_success': '{"script": "{script}", "provider": "{provider}", "server": "{server_name}", "server_id": "{server_id}", "status": "{status}", "hostname": "{hostname}", "timestamp": "{timestamp}", "snapshot_name": "{snapshot_name}", "total_snapshots": "{total_snapshots}", "snapshot_info": "{snapshot_info}"}',
            'payload_failure': '{"script": "{script}", "provider": "{provider}", "server": "{server_name}", "server_id": "{server_id}", "status": "{status}", "hostname": "{hostname}", "timestamp": "{timestamp}", "snapshot_name": "{snapshot_name}", "total_snapshots": "{total_snapshots}", "snapshot_info": "{snapshot_info}"}'
        }
    }
    
    # Set defaults first
    for section, options in defaults.items():
        config.add_section(section)
        for key, value in options.items():
            config.set(section, key, value)
    
    # Load from file if it exists
    if os.path.exists(CONFIG_FILE):
        try:
            config.read(CONFIG_FILE)
        except Exception as e:
            print(f"WARNING: Failed to load config file '{CONFIG_FILE}': {e}. Using defaults.", file=sys.stderr)
    
    return config

# Load configuration
_CONFIG = load_config()

# Constants (loaded from config, all paths relative to project directory)
CONFIGS_DIR = os.path.join(PROJECT_DIR, _CONFIG.get('DIRECTORIES', 'configs_dir', fallback='configs'))
LOGS_DIR = os.path.join(PROJECT_DIR, _CONFIG.get('DIRECTORIES', 'logs_dir', fallback='logs'))
DEFAULT_CONFIG_FILE = os.path.join(CONFIGS_DIR, "config.json")
LOG_FILE = os.path.join(LOGS_DIR, _CONFIG.get('FILES', 'log_file', fallback='snapshots.log'))
LOCK_FILE = os.path.join(LOGS_DIR, _CONFIG.get('FILES', 'lock_file', fallback='snapshots.lock'))
DELAY_BETWEEN_SERVERS = _CONFIG.getint('TIMING', 'delay_between_servers', fallback=20)
SNAPSHOT_CREATION_TIMEOUT = _CONFIG.getint('TIMING', 'snapshot_creation_timeout', fallback=900)  # 15 minutes default
LOG_MAX_BYTES = _CONFIG.getint('LOGGING', 'max_bytes', fallback=5242880)
LOG_BACKUP_COUNT = _CONFIG.getint('LOGGING', 'backup_count', fallback=5)
LOG_LEVEL = getattr(logging, _CONFIG.get('LOGGING', 'level', fallback='DEBUG').upper(), logging.DEBUG)

# Telegram configuration (global fallback - used when not set in server JSON files)
TELEGRAM_ENABLED = _CONFIG.getboolean('TELEGRAM', 'enabled', fallback=True)
TELEGRAM_BOT_TOKEN = _CONFIG.get('TELEGRAM', 'bot_token', fallback='').strip() or None
TELEGRAM_CHAT_ID = _CONFIG.get('TELEGRAM', 'chat_id', fallback='').strip() or None
TELEGRAM_TIMEOUT = _CONFIG.getint('TELEGRAM', 'timeout', fallback=10)
TELEGRAM_RETRIES = _CONFIG.getint('TELEGRAM', 'retries', fallback=3)
TELEGRAM_BASE_DELAY_BETWEEN_RETRIES = _CONFIG.getint('TELEGRAM', 'base_delay_between_retries', fallback=2)

# Helper function to decode escape sequences in config strings
def decode_config_string(s: str) -> str:
    """Decode escape sequences like \\n to actual newlines."""
    if not s:
        return s
    return s.encode().decode('unicode_escape')

TELEGRAM_MESSAGE_SUCCESS = decode_config_string(_CONFIG.get('TELEGRAM', 'message_success', fallback='').strip()) or None
TELEGRAM_MESSAGE_FAILURE = decode_config_string(_CONFIG.get('TELEGRAM', 'message_failure', fallback='').strip()) or None

# Set Telegram API URL if credentials are available
if TELEGRAM_ENABLED and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
else:
    TELEGRAM_API_URL = None
    if TELEGRAM_ENABLED:
        # Only warn if Telegram is enabled but credentials are missing
        pass  # Will be checked later when actually trying to send

# Webhook configuration (global fallback)
WEBHOOK_ENABLED = _CONFIG.getboolean('WEBHOOK', 'enabled', fallback=False)
WEBHOOK_URL = _CONFIG.get('WEBHOOK', 'url', fallback='').strip() or None
WEBHOOK_TIMEOUT = _CONFIG.getint('WEBHOOK', 'timeout', fallback=10)
WEBHOOK_RETRIES = _CONFIG.getint('WEBHOOK', 'retries', fallback=3)
WEBHOOK_BASE_DELAY_BETWEEN_RETRIES = _CONFIG.getint('WEBHOOK', 'base_delay_between_retries', fallback=2)
WEBHOOK_PAYLOAD_SUCCESS = _CONFIG.get('WEBHOOK', 'payload_success', fallback='').strip() or None
WEBHOOK_PAYLOAD_FAILURE = _CONFIG.get('WEBHOOK', 'payload_failure', fallback='').strip() or None

def sanitize_log_output(text: str, api_token: str = None) -> str:
    """
    Remove credentials and sensitive information from log output.
    Masks API tokens, passwords, and other sensitive patterns.
    """
    if not text:
        return text
    
    # Mask API tokens (DigitalOcean tokens are typically long alphanumeric strings)
    if api_token:
        text = text.replace(api_token, api_token[:6] + '...' + api_token[-6:] if len(api_token) > 12 else '***')
    
    # Mask common token patterns (long alphanumeric strings that might be tokens)
    # Pattern for potential tokens: long alphanumeric strings (20+ chars)
    token_pattern = r'\b([A-Za-z0-9]{20,})\b'
    def mask_token(match):
        token = match.group(1)
        # Don't mask if it looks like a snapshot ID or droplet ID (usually shorter)
        if len(token) < 20:
            return token
        return token[:6] + '...' + token[-6:]
    
    text = re.sub(token_pattern, mask_token, text)
    
    # Mask common credential patterns
    credential_patterns = [
        (r'api[_-]?token["\']?\s*[:=]\s*["\']?([A-Za-z0-9_-]{10,})["\']?', r'api_token="***"'),
        (r'bot[_-]?token["\']?\s*[:=]\s*["\']?([A-Za-z0-9_-]{10,})["\']?', r'bot_token="***"'),
        (r'password["\']?\s*[:=]\s*["\']?([^\s"\']+)["\']?', r'password="***"'),
        (r'secret["\']?\s*[:=]\s*["\']?([A-Za-z0-9_-]{10,})["\']?', r'secret="***"'),
    ]
    
    for pattern, replacement in credential_patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    return text

def sanitize_telegram_output(text: str, token: str = None) -> str:
    """
    Remove credentials and sensitive information from Telegram log output.
    Masks Telegram bot tokens and other sensitive patterns.
    """
    if not text:
        return text
    
    # Mask Telegram bot token if provided
    if token:
        text = text.replace(token, token[:6] + '...' + token[-6:] if len(token) > 12 else '***')
    
    # Mask common token patterns
    token_pattern = r'\b([A-Za-z0-9_-]{20,})\b'
    def mask_token(match):
        token_str = match.group(1)
        if len(token_str) < 20:
            return token_str
        return token_str[:6] + '...' + token_str[-6:]
    
    text = re.sub(token_pattern, mask_token, text)
    
    # Mask common credential patterns in JSON/text responses
    credential_patterns = [
        (r'bot[_-]?token["\']?\s*[:=]\s*["\']?([A-Za-z0-9_-]{10,})["\']?', r'bot_token="***"'),
        (r'api[_-]?token["\']?\s*[:=]\s*["\']?([A-Za-z0-9_-]{10,})["\']?', r'api_token="***"'),
        (r'password["\']?\s*[:=]\s*["\']?([^\s"\']+)["\']?', r'password="***"'),
        (r'secret["\']?\s*[:=]\s*["\']?([A-Za-z0-9_-]{10,})["\']?', r'secret="***"'),
    ]
    
    for pattern, replacement in credential_patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    return text

def create_notification_data(script_name: str, server_name: str, server_id: str, status: str, 
                             hostname: str, timestamp: str, snapshot_name: str, total_snapshots: int,
                             provider: str = "unknown") -> dict:
    """
    Create a standardized notification data structure used by both Telegram and webhook.
    Returns a dictionary with all notification data.
    """
    return {
        "script": script_name,
        "provider": provider,
        "server": server_name,
        "server_id": server_id,
        "status": status.upper(),
        "hostname": hostname,
        "timestamp": timestamp,
        "snapshot_name": snapshot_name,
        "total_snapshots": total_snapshots,
        "snapshot_info": f"{total_snapshots} snapshots exist"
    }

def format_telegram_message_from_data(data: dict) -> str:
    """
    Formats notification data into a Markdown message for Telegram.
    Uses the same data structure as webhook payloads.
    """
    provider_label = data.get('provider', 'unknown').upper()
    formatted_message = (
        f"*FINAL_STATUS*\n"
        f"*Script:* `{data['script']}`\n"
        f"*Provider:* `{provider_label}`\n"
        f"*Server:* `{data['server']}`\n"
        f"*Status:* `{data['status']}`\n"
        f"*Hostname:* `{data['hostname']}`\n"
        f"*Timestamp:* `{data['timestamp']}`\n"
        f"*Snapshot:* `{data['snapshot_name']}`\n"
        f"*Total Snapshots:* `{data['snapshot_info']}`"
    )
    return formatted_message

def format_telegram_message(raw_message: str) -> str:
    """
    Formats the raw FINAL_STATUS log entry into a Markdown message for Telegram.
    This is a compatibility function that parses the log format and converts it.
    """
    parts = raw_message.split(" | ")
    if len(parts) != 9:  # Updated to include provider
        return raw_message  # Return as is if format is unexpected

    _, script_name, provider, server_name, status, hostname, timestamp, snapshot_name, snapshot_info = parts
    
    # Extract server_id from snapshot_info if possible, otherwise use server_name
    server_id = server_name  # Default fallback
    
    # Parse total_snapshots from snapshot_info (e.g., "3 snapshots exist" -> 3)
    total_snapshots = 0
    try:
        match = re.search(r'(\d+)\s+snapshots', snapshot_info)
        if match:
            total_snapshots = int(match.group(1))
    except:
        pass
    
    data = create_notification_data(
        script_name, server_name, server_id, status,
        hostname, timestamp, snapshot_name, total_snapshots, provider
    )
    return format_telegram_message_from_data(data)

def send_telegram_notification(data: dict, logger: logging.Logger, 
                              bot_token: Optional[str] = None, 
                              chat_id: Optional[str] = None,
                              custom_message: Optional[str] = None,
                              retries: int = None,
                              base_delay: int = None) -> bool:
    """
    Send a Telegram notification using standardized notification data.
    Uses per-server credentials if provided, otherwise falls back to global config.
    Implements exponential backoff for retries.
    Returns True if successful, False otherwise.
    """
    # Use per-server credentials if provided, otherwise use global config
    token = bot_token if bot_token else TELEGRAM_BOT_TOKEN
    chat = chat_id if chat_id else TELEGRAM_CHAT_ID
    
    if not token or not chat:
        logger.debug("Telegram notifications skipped: missing bot_token or chat_id")
        return False
    
    # Use provided retries/delay or fall back to global config
    max_retries = retries if retries is not None else TELEGRAM_RETRIES
    base_delay_seconds = base_delay if base_delay is not None else TELEGRAM_BASE_DELAY_BETWEEN_RETRIES
    
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    # Use custom message if provided, otherwise format from standardized data
    if custom_message:
        formatted_message = custom_message
    else:
        formatted_message = format_telegram_message_from_data(data)
    
    for attempt in range(1, max_retries + 1):
        try:
            payload = {
                "chat_id": chat,
                "text": formatted_message,
                "parse_mode": "Markdown"
            }
            response = requests.post(api_url, data=payload, timeout=TELEGRAM_TIMEOUT)
            
            if response.status_code == 200:
                logger.debug("Telegram notification sent successfully")
                return True
            else:
                sanitized_response = sanitize_telegram_output(response.text, token)
                logger.warning(f"Telegram API error: {response.status_code} - {sanitized_response}")
                
        except requests.exceptions.RequestException as e:
            error_msg = sanitize_telegram_output(str(e), token)
            logger.warning(f"Telegram notification failed: {error_msg}")
        
        if attempt < max_retries:
            # Exponential backoff: delay = base_delay * (2 ^ (attempt - 1))
            delay_seconds = base_delay_seconds * (2 ** (attempt - 1))
            logger.debug(f"Retrying Telegram notification in {delay_seconds}s (attempt {attempt}/{max_retries}, exponential backoff)")
            time.sleep(delay_seconds)
    
    logger.warning(f"Failed to send Telegram notification after {max_retries} attempts")
    return False

def send_webhook_notification(logger: logging.Logger,
                              webhook_url: str,
                              payload: dict,
                              timeout: int = None,
                              retries: int = None,
                              base_delay: int = None) -> bool:
    """
    Send a webhook notification with JSON payload.
    Compatible with n8n webhook nodes and other standard webhook receivers.
    Implements exponential backoff for retries.
    Returns True if successful, False otherwise.
    
    The payload is sent as JSON in the request body with Content-Type: application/json header.
    n8n webhook nodes will receive this data in the $json context variable.
    """
    if not webhook_url:
        logger.debug("Webhook notification skipped: no URL provided")
        return False
    
    # Use provided values or fall back to global config
    timeout_seconds = timeout if timeout is not None else WEBHOOK_TIMEOUT
    max_retries = retries if retries is not None else WEBHOOK_RETRIES
    base_delay_seconds = base_delay if base_delay is not None else WEBHOOK_BASE_DELAY_BETWEEN_RETRIES
    
    # Sanitize URL for logging (remove potential tokens/credentials)
    sanitized_url = sanitize_log_output(webhook_url)
    logger.debug(f"[WEBHOOK] Preparing notification to: {sanitized_url}")
    logger.debug(f"[WEBHOOK] Payload size: {len(json.dumps(payload))} bytes")
    logger.debug(f"[WEBHOOK] Payload keys: {list(payload.keys())}")
    
    for attempt in range(1, max_retries + 1):
        logger.debug(f"[WEBHOOK] Attempt {attempt}/{max_retries} - Sending notification")
        start_time = time.time()
        
        try:
            logger.debug(f"[WEBHOOK] POST request to webhook (timeout: {timeout_seconds}s)")
            # Ensure proper headers for n8n compatibility
            headers = {
                'Content-Type': 'application/json',
                'Accept': 'application/json'
            }
            response = requests.post(webhook_url, json=payload, headers=headers, timeout=timeout_seconds)
            request_time = time.time() - start_time
            
            logger.debug(f"[WEBHOOK] Response received in {request_time:.2f}s (status: {response.status_code})")
            
            if response.status_code >= 200 and response.status_code < 300:
                logger.debug(f"Webhook notification sent successfully (status: {response.status_code})")
                sanitized_response = sanitize_log_output(response.text[:200])
                logger.debug(f"[WEBHOOK] Response body (first 200 chars): {sanitized_response}")
                return True
            else:
                sanitized_response = sanitize_log_output(response.text[:200])
                logger.warning(f"Webhook API error: {response.status_code} - {sanitized_response}")
                
        except requests.exceptions.Timeout as e:
            request_time = time.time() - start_time
            error_msg = sanitize_log_output(str(e))
            logger.warning(f"Webhook notification timeout after {request_time:.2f}s: {error_msg}")
        except requests.exceptions.RequestException as e:
            request_time = time.time() - start_time
            error_msg = sanitize_log_output(str(e))
            logger.warning(f"Webhook notification failed after {request_time:.2f}s: {error_msg}")
        
        if attempt < max_retries:
            # Exponential backoff: delay = base_delay * (2 ^ (attempt - 1))
            delay_seconds = base_delay_seconds * (2 ** (attempt - 1))
            logger.debug(f"Retrying webhook notification in {delay_seconds}s (attempt {attempt}/{max_retries}, exponential backoff)")
            time.sleep(delay_seconds)
    
    logger.warning(f"Failed to send webhook notification after {max_retries} attempts")
    return False

@dataclass
class ServerConfig:
    provider: str  # "digitalocean" or "hetzner"
    id: str
    name: str
    api_token: str
    retain_last_snapshots: int
    # Telegram settings (optional - if not set, Telegram notifications are skipped for this server)
    telegram_enabled: bool = False
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    telegram_message_success: Optional[str] = None  # Custom message template for success
    telegram_message_failure: Optional[str] = None  # Custom message template for failure
    # Webhook settings (optional - if not set, webhook notifications are skipped for this server)
    webhook_enabled: bool = False
    webhook_url: Optional[str] = None
    webhook_payload_success: Optional[dict] = None  # Custom JSON payload for success
    webhook_payload_failure: Optional[dict] = None  # Custom JSON payload for failure

class SnapshotManager:
    def __init__(self, config_paths: List[str], verbose: bool = False):
        self.config_paths = config_paths
        self.verbose = verbose
        self.ensure_directories()
        self.setup_logging()
        
        # Log initialization
        self.logger.info("=" * 80)
        self.logger.info("Initializing SnapshotManager")
        self.logger.info(f"Configuration files: {', '.join(config_paths) if config_paths else 'All .json files'}")
        self.logger.info(f"Verbose mode: {verbose}")
        self.logger.debug(f"Log file: {LOG_FILE}")
        self.logger.debug(f"Lock file: {LOCK_FILE}")
        
        self.servers = self.load_configs()
        if not self.servers:
            self.logger.warning("No valid server configurations found. Exiting.")
            sys.exit(0)
        
        self.logger.info(f"Successfully loaded {len(self.servers)} server configuration(s)")
        providers = {server.provider for server in self.servers}
        self.logger.debug(f"Configured providers: {', '.join(providers)}")
        
        # Using direct API calls - no CLI tools required
        self.logger.debug("Using direct API calls for cloud provider interactions")

    def ensure_directories(self):
        """Ensure required directories exist."""
        for directory in [LOGS_DIR, CONFIGS_DIR]:
            if not os.path.exists(directory):
                try:
                    os.makedirs(directory, exist_ok=True)
                except OSError as e:
                    print(f"ERROR: Failed to create directory '{directory}': {e}", file=sys.stderr)
                    sys.exit(1)

    def load_configs(self) -> List[ServerConfig]:
        """Load and validate JSON configuration files. Skip invalid files and log errors."""
        servers = []
        for path in self.config_paths:
            full_path = os.path.join(CONFIGS_DIR, path)
            
            # Skip macOS resource fork files (._*)
            if os.path.basename(path).startswith('._'):
                if hasattr(self, 'logger'):
                    self.logger.debug(f"Skipping macOS resource fork file: '{full_path}'")
                continue
            
            # Skip non-JSON files
            if not path.lower().endswith('.json'):
                if hasattr(self, 'logger'):
                    self.logger.warning(f"Skipping non-JSON file: '{full_path}'")
                else:
                    print(f"WARNING: Skipping non-JSON file: '{full_path}'", file=sys.stderr)
                continue
            
            if not os.path.exists(full_path):
                if hasattr(self, 'logger'):
                    self.logger.error(f"Configuration file '{full_path}' does not exist. Skipping.")
                else:
                    print(f"ERROR: Configuration file '{full_path}' does not exist. Skipping.", file=sys.stderr)
                continue
            
            # Validate JSON syntax and structure
            try:
                # Try to open with UTF-8 encoding, skip if encoding error
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                except UnicodeDecodeError as e:
                    if hasattr(self, 'logger'):
                        self.logger.warning(f"Configuration file '{full_path}' is not valid UTF-8 (likely binary file). Skipping.")
                    else:
                        print(f"WARNING: Configuration file '{full_path}' is not valid UTF-8. Skipping.", file=sys.stderr)
                    continue
                
                # Check if JSON is valid but empty or None
                if config is None:
                    if hasattr(self, 'logger'):
                        self.logger.error(f"Configuration file '{full_path}' is empty or null. Skipping.")
                    else:
                        print(f"ERROR: Configuration file '{full_path}' is empty or null. Skipping.", file=sys.stderr)
                    continue
                
                # Validate structure
                if not isinstance(config, dict):
                    if hasattr(self, 'logger'):
                        self.logger.error(f"Configuration file '{full_path}' does not contain a valid JSON object. Skipping.")
                    else:
                        print(f"ERROR: Configuration file '{full_path}' does not contain a valid JSON object. Skipping.", file=sys.stderr)
                    continue
                
                # Detect provider type from JSON structure
                server_data = None
                provider = None
                
                if 'digitalocean_droplet' in config:
                    server_data = config['digitalocean_droplet']
                    provider = 'digitalocean'
                elif 'hetzner_cloud_server' in config:
                    server_data = config['hetzner_cloud_server']
                    provider = 'hetzner'
                else:
                    if hasattr(self, 'logger'):
                        self.logger.error(f"Configuration file '{full_path}' must contain either 'digitalocean_droplet' or 'hetzner_cloud_server' key. Skipping.")
                    else:
                        print(f"ERROR: Configuration file '{full_path}' must contain either 'digitalocean_droplet' or 'hetzner_cloud_server' key. Skipping.", file=sys.stderr)
                    continue
                
                if not isinstance(server_data, dict):
                    if hasattr(self, 'logger'):
                        self.logger.error(f"Configuration file '{full_path}': server key does not contain a dictionary. Skipping.")
                    else:
                        print(f"ERROR: Configuration file '{full_path}': server key does not contain a dictionary. Skipping.", file=sys.stderr)
                    continue
                
                # Validate provider field matches
                config_provider = server_data.get('provider', '').lower()
                if config_provider and config_provider != provider:
                    if hasattr(self, 'logger'):
                        self.logger.warning(f"Configuration file '{full_path}': provider field '{config_provider}' doesn't match key type '{provider}'. Using '{provider}'.")
                    provider = config_provider  # Use the provider from the config
                
                # Validate required fields
                required_fields = ['id', 'name', 'api_token', 'retain_last_snapshots']
                missing_fields = [field for field in required_fields if field not in server_data]
                if missing_fields:
                    if hasattr(self, 'logger'):
                        self.logger.error(f"Configuration file '{full_path}' is missing required field(s): {', '.join(missing_fields)}. Skipping.")
                    else:
                        print(f"ERROR: Configuration file '{full_path}' is missing required field(s): {', '.join(missing_fields)}. Skipping.", file=sys.stderr)
                    continue
                
                # Extract optional Telegram settings
                telegram_config = server_data.get('telegram', {})
                # Check if telegram is explicitly configured in JSON
                telegram_enabled_json = telegram_config.get('enabled', None) if isinstance(telegram_config, dict) else None
                telegram_bot_token_raw = telegram_config.get('bot_token', '').strip() if isinstance(telegram_config, dict) else None
                telegram_chat_id_raw = telegram_config.get('chat_id', '').strip() if isinstance(telegram_config, dict) else None
                telegram_message_success = telegram_config.get('message_success') if isinstance(telegram_config, dict) else None
                telegram_message_failure = telegram_config.get('message_failure') if isinstance(telegram_config, dict) else None
                
                # Check if credentials are missing or placeholder values
                telegram_bot_token = None
                telegram_chat_id = None
                if telegram_bot_token_raw and telegram_bot_token_raw.lower() not in ['', 'your_telegram_bot_token_here', 'your_telegram_bot_token']:
                    telegram_bot_token = telegram_bot_token_raw
                if telegram_chat_id_raw and telegram_chat_id_raw.lower() not in ['', 'your_telegram_chat_id_here', 'your_telegram_chat_id']:
                    telegram_chat_id = telegram_chat_id_raw
                
                # Determine if Telegram should be enabled
                # If explicitly enabled in JSON, use it (with fallback credentials if needed)
                # If not configured in JSON, use global enabled flag
                if telegram_enabled_json is True:
                    telegram_enabled = True
                    # If Telegram is enabled in JSON but credentials are missing, use fallback from config
                    if not telegram_bot_token or not telegram_chat_id:
                        if hasattr(self, 'logger'):
                            self.logger.debug(f"[CONFIG] Telegram enabled in JSON for '{server_data.get('name', 'unknown')}' but credentials missing, using fallback from snapshots.config")
                        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                            telegram_bot_token = TELEGRAM_BOT_TOKEN
                            telegram_chat_id = TELEGRAM_CHAT_ID
                            if hasattr(self, 'logger'):
                                self.logger.debug(f"[CONFIG] Using fallback Telegram credentials from snapshots.config")
                        else:
                            if hasattr(self, 'logger'):
                                self.logger.error(f"[CONFIG] Telegram enabled in JSON for '{server_data.get('name', 'unknown')}' but no credentials found in JSON or fallback config. Telegram notifications will be skipped.")
                            telegram_enabled = False  # Disable if no credentials available
                elif telegram_enabled_json is False:
                    # Explicitly disabled in JSON, don't use global fallback
                    telegram_enabled = False
                else:
                    # Not configured in JSON, use global enabled flag
                    telegram_enabled = TELEGRAM_ENABLED
                    if telegram_enabled:
                        # Use global credentials if per-server credentials are not available
                        if not telegram_bot_token and TELEGRAM_BOT_TOKEN:
                            telegram_bot_token = TELEGRAM_BOT_TOKEN
                        if not telegram_chat_id and TELEGRAM_CHAT_ID:
                            telegram_chat_id = TELEGRAM_CHAT_ID
                        if hasattr(self, 'logger') and (telegram_bot_token or telegram_chat_id):
                            self.logger.debug(f"[CONFIG] Using global Telegram settings for '{server_data.get('name', 'unknown')}'")
                
                # Extract optional webhook settings
                webhook_config = server_data.get('webhook', {})
                # Check if webhook is explicitly configured in JSON
                webhook_enabled_json = webhook_config.get('enabled', None) if isinstance(webhook_config, dict) else None
                webhook_url_raw = webhook_config.get('url', '').strip() if isinstance(webhook_config, dict) else None
                webhook_payload_success = webhook_config.get('payload_success') if isinstance(webhook_config, dict) else None
                webhook_payload_failure = webhook_config.get('payload_failure') if isinstance(webhook_config, dict) else None
                
                # Check if URL is missing or placeholder value
                webhook_url = None
                if webhook_url_raw and webhook_url_raw.lower() not in ['', 'https://your-webhook-url.com/notify', 'your-webhook-url.com', 'your_webhook_url_here']:
                    webhook_url = webhook_url_raw
                
                # Determine if webhook should be enabled
                # If explicitly enabled in JSON, use it (with fallback URL if needed)
                # If not configured in JSON, use global enabled flag
                if webhook_enabled_json is True:
                    webhook_enabled = True
                    # If webhook is enabled in JSON but URL is missing, use fallback from config
                    if not webhook_url:
                        if hasattr(self, 'logger'):
                            self.logger.debug(f"[CONFIG] Webhook enabled in JSON for '{server_data.get('name', 'unknown')}' but URL missing, using fallback from snapshots.config")
                        if WEBHOOK_URL:
                            webhook_url = WEBHOOK_URL
                            if hasattr(self, 'logger'):
                                self.logger.debug(f"[CONFIG] Using fallback webhook URL from snapshots.config")
                        else:
                            if hasattr(self, 'logger'):
                                self.logger.error(f"[CONFIG] Webhook enabled in JSON for '{server_data.get('name', 'unknown')}' but no URL found in JSON or fallback config. Webhook notifications will be skipped.")
                            webhook_enabled = False  # Disable if no URL available
                elif webhook_enabled_json is False:
                    # Explicitly disabled in JSON, don't use global fallback
                    webhook_enabled = False
                else:
                    # Not configured in JSON, use global enabled flag
                    webhook_enabled = WEBHOOK_ENABLED
                    if webhook_enabled:
                        # Use global URL if per-server URL is not available
                        if not webhook_url and WEBHOOK_URL:
                            webhook_url = WEBHOOK_URL
                        if hasattr(self, 'logger') and webhook_url:
                            self.logger.debug(f"[CONFIG] Using global webhook settings for '{server_data.get('name', 'unknown')}'")
                
                # Validate and convert data types
                try:
                    server_config = ServerConfig(
                        provider=provider,
                        id=str(server_data['id']),
                        name=str(server_data['name']),
                        api_token=str(server_data['api_token']),
                        retain_last_snapshots=int(server_data['retain_last_snapshots']),
                        telegram_enabled=bool(telegram_enabled) if telegram_enabled else False,
                        telegram_bot_token=telegram_bot_token if telegram_bot_token else None,
                        telegram_chat_id=telegram_chat_id if telegram_chat_id else None,
                        telegram_message_success=str(telegram_message_success) if telegram_message_success else None,
                        telegram_message_failure=str(telegram_message_failure) if telegram_message_failure else None,
                        webhook_enabled=bool(webhook_enabled) if webhook_enabled else False,
                        webhook_url=webhook_url if webhook_url else None,
                        webhook_payload_success=webhook_payload_success if isinstance(webhook_payload_success, dict) else None,
                        webhook_payload_failure=webhook_payload_failure if isinstance(webhook_payload_failure, dict) else None
                    )
                    servers.append(server_config)
                    if hasattr(self, 'logger'):
                        masked_token = server_config.api_token[:6] + '...' + server_config.api_token[-6:] if len(server_config.api_token) > 12 else '***'
                        self.logger.debug(f"Loaded config: {server_config.name} (Provider: {provider}, ID: {server_config.id}, Retain: {server_config.retain_last_snapshots}, API Token: {masked_token})")
                except ValueError as ve:
                    if hasattr(self, 'logger'):
                        self.logger.error(f"Invalid data type in '{full_path}': {ve}. Skipping.")
                    else:
                        print(f"ERROR: Invalid data type in '{full_path}': {ve}. Skipping.", file=sys.stderr)
                    continue
                    
            except json.JSONDecodeError as e:
                if hasattr(self, 'logger'):
                    self.logger.error(f"Error parsing JSON file '{full_path}': {e}. Skipping.")
                else:
                    print(f"ERROR: Error parsing JSON file '{full_path}': {e}. Skipping.", file=sys.stderr)
                continue
            except Exception as e:
                # Sanitize error message to avoid leaking config file contents
                error_msg = str(e)
                # Remove any potential token-like strings from error messages
                error_msg = sanitize_log_output(error_msg)
                if hasattr(self, 'logger'):
                    self.logger.error(f"Unexpected error processing '{full_path}': {error_msg}. Skipping.")
                else:
                    print(f"ERROR: Unexpected error processing '{full_path}': {error_msg}. Skipping.", file=sys.stderr)
                continue
        
        return servers


    def setup_logging(self):
        self.logger = logging.getLogger('snapshots.py')
        self.logger.setLevel(LOG_LEVEL)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
        
        # Improved formatter with better structure and alignment
        formatter = logging.Formatter(
            fmt='%(asctime)s | %(levelname)-8s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

        if self.verbose:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

    def error_exit(self, message: str, exit_code: int = 1):
        """Log error and exit with specified exit code."""
        if hasattr(self, 'logger'):
            self.logger.error(message)
        else:
            print(f"ERROR: {message}", file=sys.stderr)
        sys.exit(exit_code)


    def get_snapshots(self, server: ServerConfig) -> List[dict]:
        """Get snapshots for a server, routing to provider-specific method."""
        self.logger.info(f"Retrieving snapshots for {server.provider} server '{server.name}' (ID: {server.id})")
        if server.provider == "digitalocean":
            return self.get_digitalocean_snapshots(server)
        elif server.provider == "hetzner":
            return self.get_hetzner_snapshots(server)
        else:
            self.logger.error(f"Unknown provider: {server.provider}")
            return []
    
    def get_digitalocean_snapshots(self, server: ServerConfig) -> List[dict]:
        """Get snapshots for a DigitalOcean droplet using direct API call."""
        self.logger.debug(f"Listing DigitalOcean snapshots for droplet ID: {server.id}")
        snapshots = []
        
        # DigitalOcean API v2: GET /snapshots?resource_type=droplet
        base_url = "https://api.digitalocean.com/v2"
        headers = {
            "Authorization": f"Bearer {server.api_token}",
            "Content-Type": "application/json"
        }
        
        page = 1
        per_page = 200
        
        while True:
            try:
                url = f"{base_url}/snapshots"
                params = {
                    "resource_type": "droplet",
                    "per_page": per_page,
                    "page": page
                }
                
                self.logger.debug(f"[DIGITALOCEAN] Fetching snapshots page {page}")
                response = requests.get(url, headers=headers, params=params, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                snapshot_list = data.get("snapshots", [])
                
                if not snapshot_list:
                    break
                
                for snapshot in snapshot_list:
                    # Filter snapshots that match our droplet ID or name
                    resource_id = snapshot.get("resource_id")
                    snapshot_name = snapshot.get("name", "")
                    
                    if str(resource_id) == str(server.id) or server.name in snapshot_name:
                        snapshot_id = snapshot.get("id")
                        created_at_str = snapshot.get("created_at", "")
                        
                        try:
                            # DigitalOcean uses ISO format: "2024-12-02T13:32:34Z"
                            created_at = datetime.datetime.fromisoformat(created_at_str.replace('Z', '+00:00')).astimezone(datetime.timezone.utc)
                            snapshots.append({
                                "id": str(snapshot_id),
                                "name": snapshot_name,
                                "created_at": created_at
                            })
                            self.logger.debug(f"  Found snapshot: {snapshot_name} (ID: {snapshot_id}, Created: {created_at.strftime('%Y-%m-%d %H:%M:%S')})")
                        except (ValueError, KeyError) as e:
                            self.logger.error(f"  Invalid date format for snapshot '{snapshot_name}': {created_at_str}")
                
                # Check if there are more pages
                links = data.get("links", {})
                if "pages" in links and "next" in links["pages"]:
                    page += 1
                else:
                    break
                    
            except requests.exceptions.RequestException as e:
                error_msg = sanitize_log_output(str(e), server.api_token)
                self.logger.error(f"[DIGITALOCEAN] Failed to fetch snapshots: {error_msg}")
                break
            except Exception as e:
                error_msg = sanitize_log_output(str(e), server.api_token)
                self.logger.error(f"[DIGITALOCEAN] Unexpected error fetching snapshots: {error_msg}")
                break

        self.logger.info(f"Found {len(snapshots)} snapshot(s) for droplet '{server.name}'")
        return snapshots
    
    def get_hetzner_snapshots(self, server: ServerConfig) -> List[dict]:
        """Get snapshots for a Hetzner Cloud server using direct API call."""
        self.logger.debug(f"Listing Hetzner Cloud snapshots for server ID: {server.id}")
        snapshots = []
        
        # Hetzner Cloud API v1: GET /images?type=snapshot
        base_url = "https://api.hetzner.cloud/v1"
        headers = {
            "Authorization": f"Bearer {server.api_token}",
            "Content-Type": "application/json"
        }
        
        page = 1
        per_page = 50
        
        while True:
            try:
                url = f"{base_url}/images"
                params = {
                    "type": "snapshot",
                    "per_page": per_page,
                    "page": page
                }
                
                self.logger.debug(f"[HETZNER] Fetching snapshots page {page}")
                response = requests.get(url, headers=headers, params=params, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                images = data.get("images", [])
                
                if not images:
                    break
                
                for image in images:
                    # Hetzner images have a 'description' field that contains the snapshot name
                    # and 'created' field for timestamp
                    description = image.get("description", "")
                    # Filter snapshots that match our server name pattern or were created from this server
                    if server.name in description or description.startswith(server.name):
                        snapshot_id = str(image.get("id", ""))
                        snapshot_name = description
                        created_at_str = image.get("created", "")
                        
                        try:
                            # Hetzner uses ISO format: "2024-12-02T13:32:34Z"
                            created_at = datetime.datetime.fromisoformat(created_at_str.replace('Z', '+00:00')).astimezone(datetime.timezone.utc)
                            snapshots.append({
                                "id": snapshot_id,
                                "name": snapshot_name,
                                "created_at": created_at
                            })
                            self.logger.debug(f"  Found snapshot: {snapshot_name} (ID: {snapshot_id}, Created: {created_at.strftime('%Y-%m-%d %H:%M:%S')})")
                        except (ValueError, KeyError) as e:
                            self.logger.error(f"  Invalid date format for snapshot '{snapshot_name}': {created_at_str}")
                
                # Check if there are more pages
                meta = data.get("meta", {})
                pagination = meta.get("pagination", {})
                if pagination.get("next_page"):
                    page += 1
                else:
                    break
                    
            except requests.exceptions.RequestException as e:
                error_msg = sanitize_log_output(str(e), server.api_token)
                self.logger.error(f"[HETZNER] Failed to fetch snapshots: {error_msg}")
                break
            except Exception as e:
                error_msg = sanitize_log_output(str(e), server.api_token)
                self.logger.error(f"[HETZNER] Unexpected error fetching snapshots: {error_msg}")
                break

        self.logger.info(f"Found {len(snapshots)} snapshot(s) for server '{server.name}'")
        return snapshots

    def identify_snapshots_to_delete(self, server: ServerConfig, snapshots: List[dict], retain: int) -> List[dict]:
        snapshots.sort(key=lambda x: x['created_at'], reverse=True)
        to_delete = snapshots[retain:]
        if to_delete:
            self.logger.info(f"Identified {len(to_delete)} snapshot(s) for deletion:")
            for snap in to_delete:
                self.logger.info(f"  - {snap['name']} (ID: {snap['id']})")
        return to_delete

    def create_snapshot(self, server: ServerConfig) -> Optional[str]:
        """Create a snapshot for a server, routing to provider-specific method."""
        if server.provider == "digitalocean":
            return self.create_digitalocean_snapshot(server)
        elif server.provider == "hetzner":
            return self.create_hetzner_snapshot(server)
        else:
            self.logger.error(f"Unknown provider: {server.provider}")
            return None
    
    def create_digitalocean_snapshot(self, server: ServerConfig) -> Optional[str]:
        """Create a snapshot for a DigitalOcean droplet using direct API call."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        snapshot_name = f"{server.name}-{timestamp}"
        self.logger.info(f"Creating new snapshot: {snapshot_name} for droplet ID: {server.id}")
        self.logger.debug(f"Snapshot creation started at: {datetime.datetime.now().isoformat()}")
        
        # DigitalOcean API v2: POST /droplets/{id}/actions
        base_url = "https://api.digitalocean.com/v2"
        url = f"{base_url}/droplets/{server.id}/actions"
        headers = {
            "Authorization": f"Bearer {server.api_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "type": "snapshot",
            "name": snapshot_name
        }
        
        try:
            self.logger.debug(f"[DIGITALOCEAN] Sending snapshot creation request")
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            
            data = response.json()
            action = data.get("action", {})
            action_id = action.get("id")
            
            if action_id:
                self.logger.debug(f"[DIGITALOCEAN] Snapshot action created (ID: {action_id}), waiting for completion")
                # Poll for action completion
                action_status = self._wait_for_digitalocean_action(server.api_token, action_id, timeout=SNAPSHOT_CREATION_TIMEOUT)
                
                if action_status == "completed":
                    self.logger.info(f"  ✓ Snapshot created successfully: {snapshot_name}")
                    self.logger.debug(f"Snapshot creation completed at: {datetime.datetime.now().isoformat()}")
                    return snapshot_name
                else:
                    self.logger.error(f"  ✗ Snapshot creation failed with status: {action_status}")
                    return None
            else:
                self.logger.error(f"  ✗ Failed to create snapshot: No action ID returned")
                return None
                
        except requests.exceptions.RequestException as e:
            error_msg = sanitize_log_output(str(e), server.api_token)
            self.logger.error(f"  ✗ Failed to create snapshot: {error_msg}")
            return None
        except Exception as e:
            error_msg = sanitize_log_output(str(e), server.api_token)
            self.logger.error(f"  ✗ Unexpected error creating snapshot: {error_msg}")
            return None
    
    def _wait_for_digitalocean_action(self, api_token: str, action_id: int, timeout: int = 300) -> str:
        """Wait for a DigitalOcean action to complete."""
        base_url = "https://api.digitalocean.com/v2"
        url = f"{base_url}/actions/{action_id}"
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = requests.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                action = data.get("action", {})
                status = action.get("status", "unknown")
                
                if status == "completed":
                    return "completed"
                elif status == "errored":
                    return "errored"
                elif status == "in-progress":
                    time.sleep(5)  # Wait 5 seconds before checking again
                    continue
                else:
                    time.sleep(5)
                    continue
                    
            except requests.exceptions.RequestException:
                time.sleep(5)
                continue
        
        return "timeout"
    
    def create_hetzner_snapshot(self, server: ServerConfig) -> Optional[str]:
        """Create a snapshot for a Hetzner Cloud server using direct API call."""
        timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        snapshot_name = f"{server.name}-{timestamp}"
        self.logger.info(f"Creating new snapshot: {snapshot_name} for server ID: {server.id}")
        self.logger.debug(f"Snapshot creation started at: {datetime.datetime.now().isoformat()}")
        
        # Hetzner Cloud API v1: POST /servers/{id}/actions/create_image
        base_url = "https://api.hetzner.cloud/v1"
        url = f"{base_url}/servers/{server.id}/actions/create_image"
        headers = {
            "Authorization": f"Bearer {server.api_token}",
            "Content-Type": "application/json"
        }
        payload = {
            "type": "snapshot",
            "description": snapshot_name
        }
        
        try:
            self.logger.debug(f"[HETZNER] Sending snapshot creation request")
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            
            data = response.json()
            action = data.get("action", {})
            image = data.get("image", {})
            
            if action.get("id") and image.get("id"):
                self.logger.debug(f"[HETZNER] Snapshot action created (Action ID: {action.get('id')}, Image ID: {image.get('id')})")
                # Hetzner snapshots are usually created synchronously, but we can wait for the action
                action_status = self._wait_for_hetzner_action(server.api_token, action.get("id"), timeout=SNAPSHOT_CREATION_TIMEOUT)
                
                if action_status == "success":
                    self.logger.info(f"  ✓ Snapshot created successfully: {snapshot_name}")
                    self.logger.debug(f"Snapshot creation completed at: {datetime.datetime.now().isoformat()}")
                    return snapshot_name
                else:
                    self.logger.error(f"  ✗ Snapshot creation failed with status: {action_status}")
                    return None
            else:
                self.logger.error(f"  ✗ Failed to create snapshot: No action/image ID returned")
                return None
                
        except requests.exceptions.RequestException as e:
            error_msg = sanitize_log_output(str(e), server.api_token)
            self.logger.error(f"  ✗ Failed to create snapshot: {error_msg}")
            return None
        except Exception as e:
            error_msg = sanitize_log_output(str(e), server.api_token)
            self.logger.error(f"  ✗ Unexpected error creating snapshot: {error_msg}")
            return None
    
    def _wait_for_hetzner_action(self, api_token: str, action_id: int, timeout: int = 300) -> str:
        """Wait for a Hetzner Cloud action to complete."""
        base_url = "https://api.hetzner.cloud/v1"
        url = f"{base_url}/actions/{action_id}"
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = requests.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                
                data = response.json()
                action = data.get("action", {})
                status = action.get("status", "unknown")
                
                if status == "success":
                    return "success"
                elif status == "error":
                    return "error"
                elif status == "running":
                    time.sleep(5)  # Wait 5 seconds before checking again
                    continue
                else:
                    time.sleep(5)
                    continue
                    
            except requests.exceptions.RequestException:
                time.sleep(5)
                continue
        
        return "timeout"

    def delete_snapshots(self, server: ServerConfig, snapshots: List[dict]):
        """Delete snapshots for a server, routing to provider-specific method."""
        if server.provider == "digitalocean":
            self.delete_digitalocean_snapshots(server, snapshots)
        elif server.provider == "hetzner":
            self.delete_hetzner_snapshots(server, snapshots)
        else:
            self.logger.error(f"Unknown provider: {server.provider}")
    
    def delete_digitalocean_snapshots(self, server: ServerConfig, snapshots: List[dict]):
        """Delete snapshots for a DigitalOcean droplet using direct API call."""
        if not snapshots:
            return
        self.logger.info(f"Deleting {len(snapshots)} snapshot(s) for droplet '{server.name}':")
        
        # DigitalOcean API v2: DELETE /snapshots/{id}
        base_url = "https://api.digitalocean.com/v2"
        headers = {
            "Authorization": f"Bearer {server.api_token}",
            "Content-Type": "application/json"
        }
        
        for idx, snap in enumerate(snapshots, 1):
            self.logger.info(f"  [{idx}/{len(snapshots)}] Deleting: {snap['name']} (ID: {snap['id']})")
            self.logger.debug(f"  Initiating deletion of snapshot ID: {snap['id']}")
            
            url = f"{base_url}/snapshots/{snap['id']}"
            
            try:
                response = requests.delete(url, headers=headers, timeout=30)
                
                if response.status_code == 204:
                    self.logger.info(f"    ✓ Deleted successfully")
                    self.logger.debug(f"    Deletion confirmed for snapshot ID: {snap['id']}")
                elif response.status_code == 404:
                    self.logger.warning(f"    Snapshot not found (likely already deleted)")
                else:
                    error_msg = sanitize_log_output(response.text[:200], server.api_token)
                    self.logger.error(f"    ✗ Failed to delete snapshot (status {response.status_code}): {error_msg}")
                    
            except requests.exceptions.RequestException as e:
                error_msg = sanitize_log_output(str(e), server.api_token)
                self.logger.error(f"    ✗ Failed to delete snapshot: {error_msg}")
            except Exception as e:
                error_msg = sanitize_log_output(str(e), server.api_token)
                self.logger.error(f"    ✗ Unexpected error deleting snapshot: {error_msg}")
    
    def delete_hetzner_snapshots(self, server: ServerConfig, snapshots: List[dict]):
        """Delete snapshots for a Hetzner Cloud server using direct API call."""
        if not snapshots:
            return
        self.logger.info(f"Deleting {len(snapshots)} snapshot(s) for server '{server.name}':")
        
        # Hetzner Cloud API v1: DELETE /images/{id}
        base_url = "https://api.hetzner.cloud/v1"
        headers = {
            "Authorization": f"Bearer {server.api_token}",
            "Content-Type": "application/json"
        }
        
        for idx, snap in enumerate(snapshots, 1):
            self.logger.info(f"  [{idx}/{len(snapshots)}] Deleting: {snap['name']} (ID: {snap['id']})")
            self.logger.debug(f"  Initiating deletion of snapshot ID: {snap['id']}")
            
            url = f"{base_url}/images/{snap['id']}"
            
            try:
                response = requests.delete(url, headers=headers, timeout=30)
                
                if response.status_code == 204:
                    self.logger.info(f"    ✓ Deleted successfully")
                    self.logger.debug(f"    Deletion confirmed for snapshot ID: {snap['id']}")
                elif response.status_code == 404:
                    self.logger.warning(f"    Snapshot not found (likely already deleted)")
                else:
                    error_msg = sanitize_log_output(response.text[:200], server.api_token)
                    self.logger.error(f"    ✗ Failed to delete snapshot (status {response.status_code}): {error_msg}")
                    
            except requests.exceptions.RequestException as e:
                error_msg = sanitize_log_output(str(e), server.api_token)
                self.logger.error(f"    ✗ Failed to delete snapshot: {error_msg}")
            except Exception as e:
                error_msg = sanitize_log_output(str(e), server.api_token)
                self.logger.error(f"    ✗ Unexpected error deleting snapshot: {error_msg}")

    def replace_template_variables(self, template: str, server: ServerConfig, snapshot_name: str, 
                                   total_snapshots: int, status: str, hostname: str, timestamp: str) -> str:
        """Replace template variables in custom messages."""
        replacements = {
            '{script}': 'snapshots.py',
            '{provider}': server.provider,
            '{server_name}': server.name,
            '{server_id}': server.id,
            '{droplet_name}': server.name,  # Backward compatibility
            '{droplet_id}': server.id,  # Backward compatibility
            '{snapshot_name}': snapshot_name,
            '{total_snapshots}': str(total_snapshots),
            '{snapshot_info}': f'{total_snapshots} snapshots exist',
            '{status}': status.upper(),
            '{hostname}': hostname,
            '{timestamp}': timestamp
        }
        result = template
        for placeholder, value in replacements.items():
            result = result.replace(placeholder, value)
        return result
    
    def replace_template_variables_dict(self, template_dict: dict, server: ServerConfig, snapshot_name: str,
                                       total_snapshots: int, status: str, hostname: str, timestamp: str) -> dict:
        """Replace template variables in dictionary (for webhook payloads)."""
        result = {}
        replacements = {
            '{script}': 'snapshots.py',
            '{provider}': server.provider,
            '{server_name}': server.name,
            '{server_id}': server.id,
            '{droplet_name}': server.name,  # Backward compatibility
            '{droplet_id}': server.id,  # Backward compatibility
            '{snapshot_name}': snapshot_name,
            '{total_snapshots}': str(total_snapshots),
            '{snapshot_info}': f'{total_snapshots} snapshots exist',
            '{status}': status.upper(),
            '{hostname}': hostname,
            '{timestamp}': timestamp
        }
        for key, value in template_dict.items():
            if isinstance(value, str):
                for placeholder, replacement in replacements.items():
                    value = value.replace(placeholder, replacement)
                result[key] = value
            elif isinstance(value, dict):
                result[key] = self.replace_template_variables_dict(value, server, snapshot_name, 
                                                                   total_snapshots, status, hostname, timestamp)
            elif isinstance(value, list):
                result[key] = [
                    self.replace_template_variables_dict(item, server, snapshot_name, total_snapshots, 
                                                        status, hostname, timestamp) if isinstance(item, dict)
                    else self.replace_template_variables(str(item), server, snapshot_name, total_snapshots,
                                                        status, hostname, timestamp) if isinstance(item, str)
                    else item
                    for item in value
                ]
            else:
                result[key] = value
        return result

    def write_final_status(self, server: ServerConfig, snapshot_name: str, total_snapshots: int, status: str):
        hostname = os.uname().nodename
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        final_status_message = f"FINAL_STATUS | snapshots.py | {server.provider} | {server.name} | {status.upper()} | {hostname} | {timestamp} | {snapshot_name} | {total_snapshots} snapshots exist"
        self.logger.info(final_status_message)
        
        # Create standardized notification data structure (used by both Telegram and webhook)
        notification_data = create_notification_data(
            script_name="snapshots.py",
            server_name=server.name,
            server_id=server.id,
            status=status.upper(),
            hostname=hostname,
            timestamp=timestamp,
            snapshot_name=snapshot_name,
            total_snapshots=total_snapshots,
            provider=server.provider
        )
        
        # Send Telegram notification if enabled for this server
        self.logger.debug(f"[NOTIFICATIONS] Checking Telegram notification configuration")
        # Only send if explicitly enabled (either per-server or global fallback)
        # Don't send if global enabled is False, even if credentials exist
        if server.telegram_enabled:
            if server.telegram_bot_token and server.telegram_chat_id:
                self.logger.debug(f"[NOTIFICATIONS] Using Telegram credentials for server '{server.name}'")
                # Use custom message if provided (per-server or fallback), otherwise use default format from data
                custom_message = None
                if status.upper() == "SUCCESS":
                    if server.telegram_message_success:
                        custom_message = self.replace_template_variables(
                            server.telegram_message_success, server, snapshot_name, 
                            total_snapshots, status, hostname, timestamp
                        )
                        self.logger.debug(f"[NOTIFICATIONS] Using per-server custom success message template")
                    elif TELEGRAM_MESSAGE_SUCCESS:
                        custom_message = self.replace_template_variables(
                            TELEGRAM_MESSAGE_SUCCESS, server, snapshot_name, 
                            total_snapshots, status, hostname, timestamp
                        )
                        self.logger.debug(f"[NOTIFICATIONS] Using fallback success message template from config")
                elif status.upper() == "FAILURE":
                    if server.telegram_message_failure:
                        custom_message = self.replace_template_variables(
                            server.telegram_message_failure, server, snapshot_name,
                            total_snapshots, status, hostname, timestamp
                        )
                        self.logger.debug(f"[NOTIFICATIONS] Using per-server custom failure message template")
                    elif TELEGRAM_MESSAGE_FAILURE:
                        custom_message = self.replace_template_variables(
                            TELEGRAM_MESSAGE_FAILURE, server, snapshot_name,
                            total_snapshots, status, hostname, timestamp
                        )
                        self.logger.debug(f"[NOTIFICATIONS] Using fallback failure message template from config")
                
                send_telegram_notification(
                    notification_data, 
                    self.logger,
                    bot_token=server.telegram_bot_token,
                    chat_id=server.telegram_chat_id,
                    custom_message=custom_message
                )
            else:
                # Telegram enabled but credentials missing - should have been handled in load_configs
                # but check fallback one more time just in case
                if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                    self.logger.debug(f"[NOTIFICATIONS] Using global Telegram credentials (fallback) for server '{server.name}'")
                    # Use custom message if provided (per-server or fallback), otherwise use default format from data
                    custom_message = None
                    if status.upper() == "SUCCESS":
                        if server.telegram_message_success:
                            custom_message = self.replace_template_variables(
                                server.telegram_message_success, server, snapshot_name, 
                                total_snapshots, status, hostname, timestamp
                            )
                            self.logger.debug(f"[NOTIFICATIONS] Using per-server custom success message template (fallback credentials)")
                        elif TELEGRAM_MESSAGE_SUCCESS:
                            custom_message = self.replace_template_variables(
                                TELEGRAM_MESSAGE_SUCCESS, server, snapshot_name, 
                                total_snapshots, status, hostname, timestamp
                            )
                            self.logger.debug(f"[NOTIFICATIONS] Using fallback success message template from config")
                    elif status.upper() == "FAILURE":
                        if server.telegram_message_failure:
                            custom_message = self.replace_template_variables(
                                server.telegram_message_failure, server, snapshot_name,
                                total_snapshots, status, hostname, timestamp
                            )
                            self.logger.debug(f"[NOTIFICATIONS] Using per-server custom failure message template (fallback credentials)")
                        elif TELEGRAM_MESSAGE_FAILURE:
                            custom_message = self.replace_template_variables(
                                TELEGRAM_MESSAGE_FAILURE, server, snapshot_name,
                                total_snapshots, status, hostname, timestamp
                            )
                            self.logger.debug(f"[NOTIFICATIONS] Using fallback failure message template from config")
                    
                    send_telegram_notification(notification_data, self.logger, custom_message=custom_message)
                else:
                    self.logger.debug(f"[NOTIFICATIONS] Telegram notifications skipped - no credentials available for server '{server.name}'")
        else:
            self.logger.debug(f"[NOTIFICATIONS] Telegram notifications skipped (not enabled for server '{server.name}')")
        
        # Send webhook notification if enabled for this server
        # Only send if explicitly enabled (either per-server or global fallback)
        # Don't send if global enabled is False, even if URL exists
        webhook_url_to_use = None
        if server.webhook_enabled:
            if server.webhook_url:
                self.logger.debug(f"[NOTIFICATIONS] Using webhook URL for server '{server.name}'")
                webhook_url_to_use = server.webhook_url
            else:
                # Webhook enabled but URL missing - should have been handled in load_configs
                # but check fallback one more time just in case
                if WEBHOOK_URL:
                    self.logger.debug(f"[NOTIFICATIONS] Using global webhook URL (fallback) for server '{server.name}'")
                    webhook_url_to_use = WEBHOOK_URL
                else:
                    self.logger.debug(f"[NOTIFICATIONS] Webhook notifications skipped - no URL available for server '{server.name}'")
        else:
            self.logger.debug(f"[NOTIFICATIONS] Webhook notifications skipped (not enabled for server '{server.name}')")
        
        if webhook_url_to_use:
            self.logger.debug(f"[NOTIFICATIONS] Preparing webhook notification")
            # Use custom payload if provided (per-server or fallback), otherwise use standardized data structure
            payload = None
            if status.upper() == "SUCCESS":
                if server.webhook_payload_success:
                    payload = self.replace_template_variables_dict(
                        server.webhook_payload_success.copy(), server, snapshot_name,
                        total_snapshots, status, hostname, timestamp
                    )
                    self.logger.debug(f"[NOTIFICATIONS] Using per-server custom success payload template")
                elif WEBHOOK_PAYLOAD_SUCCESS:
                    try:
                        fallback_payload = json.loads(WEBHOOK_PAYLOAD_SUCCESS)
                        payload = self.replace_template_variables_dict(
                            fallback_payload, server, snapshot_name,
                            total_snapshots, status, hostname, timestamp
                        )
                        self.logger.debug(f"[NOTIFICATIONS] Using fallback success payload template from config")
                    except json.JSONDecodeError as e:
                        self.logger.warning(f"[NOTIFICATIONS] Invalid JSON in fallback success payload template: {e}. Using default.")
            elif status.upper() == "FAILURE":
                if server.webhook_payload_failure:
                    payload = self.replace_template_variables_dict(
                        server.webhook_payload_failure.copy(), server, snapshot_name,
                        total_snapshots, status, hostname, timestamp
                    )
                    self.logger.debug(f"[NOTIFICATIONS] Using per-server custom failure payload template")
                elif WEBHOOK_PAYLOAD_FAILURE:
                    try:
                        fallback_payload = json.loads(WEBHOOK_PAYLOAD_FAILURE)
                        payload = self.replace_template_variables_dict(
                            fallback_payload, server, snapshot_name,
                            total_snapshots, status, hostname, timestamp
                        )
                        self.logger.debug(f"[NOTIFICATIONS] Using fallback failure payload template from config")
                    except json.JSONDecodeError as e:
                        self.logger.warning(f"[NOTIFICATIONS] Invalid JSON in fallback failure payload template: {e}. Using default.")
            
            # If no custom payload was used, use the standardized data structure
            if payload is None:
                # Use the same standardized data structure as Telegram
                # Ensure all values are JSON-serializable for n8n compatibility
                payload = notification_data.copy()
                # Convert total_snapshots to int if it's not already (for consistency)
                if isinstance(payload.get('total_snapshots'), str):
                    try:
                        payload['total_snapshots'] = int(payload['total_snapshots'])
                    except (ValueError, TypeError):
                        pass  # Keep as string if conversion fails
            
            # Ensure payload is JSON-serializable (n8n requirement)
            try:
                json.dumps(payload)
            except (TypeError, ValueError) as e:
                self.logger.error(f"[NOTIFICATIONS] Payload is not JSON-serializable: {e}")
                payload = {str(k): str(v) for k, v in payload.items()}  # Fallback: convert all to strings
            
            send_webhook_notification(
                self.logger,
                webhook_url=webhook_url_to_use,
                payload=payload,
                timeout=WEBHOOK_TIMEOUT,
                retries=WEBHOOK_RETRIES,
                base_delay=WEBHOOK_BASE_DELAY_BETWEEN_RETRIES
            )

    def manage_snapshots_for_server(self, server: ServerConfig):
        provider_label = server.provider.upper()
        self.logger.info("=" * 80)
        self.logger.info(f"Managing {provider_label} server: {server.name} (ID: {server.id})")
        self.logger.info(f"  Configuration: Retain last {server.retain_last_snapshots} snapshot(s)")
        self.logger.info(f"  Snapshot naming: {server.name}-<timestamp>")
        masked_token = server.api_token[:6] + '...' + server.api_token[-6:] if len(server.api_token) > 12 else '***'
        self.logger.debug(f"  API token configured: Yes (masked: {masked_token})")
        
        # Log notification settings
        # Note: server.telegram_enabled and server.webhook_enabled already reflect
        # the correct state (per-server enabled OR global enabled when not configured)
        # If global enabled=false, these will be False even if credentials/URL exist
        notifications = []
        if server.telegram_enabled and server.telegram_bot_token and server.telegram_chat_id:
            notifications.append("Telegram")
        
        webhook_url_to_use = None
        if server.webhook_enabled and server.webhook_url:
            notifications.append("Webhook")
            webhook_url_to_use = server.webhook_url
        
        if notifications:
            self.logger.info(f"  Notifications: {', '.join(notifications)}")
        else:
            self.logger.info(f"  Notifications: None")
        
        self.logger.info("-" * 80)

        # Retrieve existing snapshots
        self.logger.debug(f"Step 1/5: Retrieving existing snapshots for server '{server.name}'")
        snapshots = self.get_snapshots(server)
        self.logger.debug(f"Step 1/5: Completed - Found {len(snapshots)} existing snapshot(s)")

        # Identify snapshots to delete based on retention policy
        self.logger.debug(f"Step 2/5: Identifying snapshots to delete (retain: {server.retain_last_snapshots})")
        to_delete = self.identify_snapshots_to_delete(server, snapshots, server.retain_last_snapshots)
        self.logger.debug(f"Step 2/5: Completed - {len(to_delete)} snapshot(s) marked for deletion")

        # Create a new snapshot
        self.logger.debug(f"Step 3/5: Creating new snapshot for server '{server.name}'")
        snapshot_name = self.create_snapshot(server)
        if snapshot_name:
            self.logger.debug(f"Step 3/5: Completed - Snapshot '{snapshot_name}' created")
        else:
            self.logger.debug(f"Step 3/5: Failed - Snapshot creation unsuccessful")

        # Delete old snapshots
        self.logger.debug(f"Step 4/5: Deleting old snapshots")
        if to_delete:
            self.delete_snapshots(server, to_delete)
            self.logger.debug(f"Step 4/5: Completed - Deleted {len(to_delete)} snapshot(s)")
        else:
            self.logger.info("No snapshots to delete (retention policy satisfied)")
            self.logger.debug(f"Step 4/5: Skipped - No snapshots to delete")

        # Re-fetch snapshots after creation and deletion to get the updated count
        self.logger.debug(f"Step 5/5: Re-fetching snapshots to get final count")
        updated_snapshots = self.get_snapshots(server)
        total_snapshots = len(updated_snapshots)
        self.logger.debug(f"Step 5/5: Completed - Final snapshot count: {total_snapshots}")

        # Write final status to the log
        if snapshot_name:
            status = "success"
        else:
            status = "failure"
        self.write_final_status(server, snapshot_name if snapshot_name else "none", total_snapshots, status)

        self.logger.info("-" * 80)
        self.logger.info(f"Completed snapshot management for {provider_label} server: {server.name}")
        self.logger.info("=" * 80)
        self.logger.info("")  # Empty line for visual separation

    def run(self):
        """Run snapshot management for all servers. Continue processing even if one fails."""
        self.logger.info("")
        self.logger.info("=" * 80)
        self.logger.info("SNAPSHOT MANAGEMENT SESSION STARTED")
        self.logger.info(f"Timestamp: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"Hostname: {os.uname().nodename}")
        self.logger.info(f"Total servers configured: {len(self.servers)}")
        self.logger.info("=" * 80)
        self.logger.info("")
        
        success_count = 0
        failure_count = 0
        
        for idx, server in enumerate(self.servers):
            self.logger.info(f"Processing server {idx + 1}/{len(self.servers)}")
            try:
                self.manage_snapshots_for_server(server)
                success_count += 1
                
                if idx < len(self.servers) - 1:
                    self.logger.info(f"Waiting for {DELAY_BETWEEN_SERVERS} seconds before processing the next server...")
                    time.sleep(DELAY_BETWEEN_SERVERS)
            except Exception as e:
                failure_count += 1
                error_msg = sanitize_log_output(str(e), server.api_token if hasattr(server, 'api_token') else None)
                self.logger.error(f"An unexpected error occurred for {server.provider} server '{server.name}': {error_msg}", exc_info=True)
        
        # Log summary
        self.logger.info("")
        self.logger.info("=" * 80)
        self.logger.info(f"SUMMARY: {success_count} succeeded, {failure_count} failed out of {len(self.servers)} total")
        self.logger.info("=" * 80)
        
        # Print summary to stderr (will appear in cronjob.log)
        summary_msg = f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] SUMMARY: {success_count} succeeded, {failure_count} failed out of {len(self.servers)} total"
        print(summary_msg, file=sys.stderr)
        
        # Exit with error code if all failed, or if any failed (for cron monitoring)
        if failure_count > 0:
            if success_count == 0:
                self.logger.error("All servers failed")
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: All servers failed", file=sys.stderr)
                sys.exit(1)
            else:
                self.logger.warning(f"Partial failure: {failure_count} server(s) failed")
                print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] WARNING: Partial failure: {failure_count} server(s) failed", file=sys.stderr)
                sys.exit(2)  # Partial failure

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage snapshots for multiple DigitalOcean droplets and Hetzner Cloud servers.")
    parser.add_argument(
        'configs',
        nargs='*',
        help=f"JSON configuration files for servers located in the '{CONFIGS_DIR}' directory. Defaults to all .json files in the directory if not specified."
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help="Enable verbose logging to the console."
    )
    return parser.parse_args()

def acquire_lock():
    """Acquire a lock file to prevent concurrent execution."""
    try:
        # Ensure logs directory exists
        if not os.path.exists(LOGS_DIR):
            os.makedirs(LOGS_DIR, exist_ok=True)
        
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Write PID to lock file
            os.write(lock_fd, str(os.getpid()).encode())
            print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Lock acquired successfully (PID: {os.getpid()})", file=sys.stderr)
            return lock_fd
        except BlockingIOError:
            os.close(lock_fd)
            print(f"ERROR: Another instance of snapshots.py is already running. Lock file: {LOCK_FILE}", file=sys.stderr)
            sys.exit(3)  # Exit code 3 for lock failure
    except Exception as e:
        print(f"ERROR: Failed to acquire lock: {e}", file=sys.stderr)
        sys.exit(3)

def release_lock(lock_fd):
    """Release the lock file."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass  # Ignore errors removing lock file
    except Exception:
        pass  # Ignore errors releasing lock

def main():
    args = parse_arguments()
    
    # Print startup message to stderr (will appear in cronjob.log)
    print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting snapshot management script...", file=sys.stderr)

    # Ensure directories exist
    if not os.path.isdir(CONFIGS_DIR):
        print(f"ERROR: The configuration directory '{CONFIGS_DIR}' does not exist.", file=sys.stderr)
        sys.exit(1)

    # Acquire lock to prevent concurrent execution
    lock_fd = None
    try:
        lock_fd = acquire_lock()
        
        if args.configs:
            config_files = args.configs
        else:
            # Get all .json files in the configs directory, sorted alphabetically
            # Exclude macOS resource fork files (._*)
            config_files = sorted(f for f in os.listdir(CONFIGS_DIR) 
                                 if f.lower().endswith('.json') and not f.startswith('._'))
            if not config_files:
                print(f"WARNING: No '.json' configuration files found in the '{CONFIGS_DIR}' directory.", file=sys.stderr)
                sys.exit(0)  # Exit successfully if no configs (might be intentional)

        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Found {len(config_files)} configuration file(s).", file=sys.stderr)
        
        # Initialize the SnapshotManager with the provided configuration files
        manager = SnapshotManager(config_paths=config_files, verbose=args.verbose)
        manager.run()
        
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Script completed successfully.", file=sys.stderr)
    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: Script failed with exception: {e}", file=sys.stderr)
        raise
    finally:
        # Always release lock
        if lock_fd is not None:
            release_lock(lock_fd)

if __name__ == "__main__":
    main()

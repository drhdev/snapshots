#!/usr/bin/env python3
# snapshots.py
# Version: 0.4
# Author: drhdev
# License: GPL v3
#
# Description:
# Creates snapshots for DigitalOcean droplets and Hetzner Cloud servers, applies a retention
# policy, sends Telegram/webhook notifications and reports to a heartbeat monitor (e.g. Uptime Kuma).
# Server settings live in JSON files in 'configs/', global settings in 'snapshots.config'.

import argparse
import configparser
import datetime
import fcntl
import json
import logging
import os
import re
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

__version__ = "0.4"
SCRIPT_NAME = "snapshots.py"
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PROJECT_DIR, "snapshots.config")

# Timeout for a single API request (seconds)
API_REQUEST_TIMEOUT = 30
TELEGRAM_API_URL = "https://api.telegram.org"
# Uptime Kuma accepts longer messages, but short ones stay readable in the UI and in alerts
HEARTBEAT_MESSAGE_LIMIT = 200

# JSON top-level key -> provider name
CONFIG_KEYS = {"digitalocean_droplet": "digitalocean", "hetzner_cloud_server": "hetzner"}

logger = logging.getLogger("snapshots")


class ConfigError(Exception):
    """Invalid or incomplete configuration."""


class ProviderError(Exception):
    """A cloud provider API call failed."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class Terminated(BaseException):
    """
    SIGTERM received (systemd timeout, shutdown, 'systemctl stop'). Derived from BaseException so
    the per-server error handling does not swallow it and the run stops right away.
    """


# ============================================================================
# Secret redaction
# ============================================================================

class SecretRedactor(logging.Filter):
    """
    Removes registered secrets (API tokens, bot tokens, webhook and heartbeat URLs) from log
    records and notification texts. For URLs the path and query are registered as well, because
    HTTP error messages often contain only the path (e.g. "Max retries exceeded with url: /...").
    """

    PATTERNS = [
        (re.compile(r"bot\d+:[\w-]+"), "bot***"),
        (re.compile(r"(Bearer\s+)\S+", re.IGNORECASE), r"\1***"),
    ]

    def __init__(self):
        super().__init__()
        self._secrets = set()

    def add(self, secret: Optional[str]):
        if not secret:
            return
        self._secrets.add(secret)
        if "://" in secret:
            parts = urlsplit(secret)
            if len(parts.path) > 1:
                self._secrets.add(parts.path)
            if parts.query:
                self._secrets.add(parts.query)

    def redact(self, text: Optional[str]) -> str:
        if not text:
            return text or ""
        # Longest first, so a full URL is replaced before its path
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, "***")
        for pattern, replacement in self.PATTERNS:
            text = pattern.sub(replacement, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


REDACTOR = SecretRedactor()


# ============================================================================
# Settings (snapshots.config)
# ============================================================================

DEFAULT_SETTINGS = {
    "DIRECTORIES": {"configs_dir": "configs", "logs_dir": "logs"},
    "FILES": {"log_file": "snapshots.log", "lock_file": "snapshots.lock"},
    "TIMING": {"delay_between_servers": "20", "snapshot_creation_timeout": "3600", "action_poll_interval": "15"},
    "LOGGING": {"file_enabled": "true", "level": "INFO", "max_bytes": "1048576", "backup_count": "2"},
    "TELEGRAM": {"enabled": "true", "bot_token": "", "chat_id": "", "timeout": "10", "retries": "3",
                 "base_delay_between_retries": "2", "message_success": "", "message_failure": ""},
    "WEBHOOK": {"enabled": "false", "url": "", "timeout": "10", "retries": "3",
                "base_delay_between_retries": "2", "payload_success": "", "payload_failure": ""},
    "HEARTBEAT": {"enabled": "false", "url": "", "timeout": "10", "retries": "3", "base_delay_between_retries": "2"},
}


@dataclass(frozen=True)
class Delivery:
    """How to deliver an outgoing notification: request timeout and attempts with exponential backoff."""
    timeout: float = 10
    retries: int = 3
    base_delay: float = 2


@dataclass
class TelegramSettings:
    enabled: bool = True
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    message_success: Optional[str] = None
    message_failure: Optional[str] = None
    delivery: Delivery = Delivery()


@dataclass
class WebhookSettings:
    enabled: bool = False
    url: Optional[str] = None
    payload_success: Optional[dict] = None
    payload_failure: Optional[dict] = None
    delivery: Delivery = Delivery()


@dataclass
class HeartbeatSettings:
    enabled: bool = False
    url: Optional[str] = None
    delivery: Delivery = Delivery()


@dataclass
class Settings:
    configs_dir: str = os.path.join(PROJECT_DIR, "configs")
    log_file: str = os.path.join(PROJECT_DIR, "logs", "snapshots.log")
    lock_file: str = os.path.join(PROJECT_DIR, "logs", "snapshots.lock")
    delay_between_servers: float = 20
    snapshot_creation_timeout: float = 3600
    action_poll_interval: float = 15
    log_to_file: bool = True
    log_level: int = logging.INFO
    log_max_bytes: int = 1048576
    log_backup_count: int = 2
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    webhook: WebhookSettings = field(default_factory=WebhookSettings)
    heartbeat: HeartbeatSettings = field(default_factory=HeartbeatSettings)
    # Problems found while loading, logged once logging is set up
    warnings: List[str] = field(default_factory=list)


def is_placeholder(value: str) -> bool:
    """Detect values copied unchanged from the .sample files."""
    lowered = value.lower()
    return "your_" in lowered or "your-" in lowered or "example.com" in lowered


def clean_value(value) -> Optional[str]:
    """Return the stripped string, or None for empty values and sample placeholders."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or is_placeholder(text):
        return None
    return text


def decode_newlines(text: str) -> Optional[str]:
    """INI values cannot contain line breaks, so templates use a literal '\\n'."""
    return text.replace("\\n", "\n") if text else None


def _delivery(section: configparser.SectionProxy) -> Delivery:
    return Delivery(timeout=section.getfloat("timeout"),
                    retries=max(1, section.getint("retries")),
                    base_delay=section.getfloat("base_delay_between_retries"))


def _json_template(section: configparser.SectionProxy, key: str, warnings: List[str]) -> Optional[dict]:
    raw = section.get(key, "").strip()
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        warnings.append(f"[{section.name}] {key} is not valid JSON ({e}), using default payload")
        return None
    if not isinstance(value, dict):
        warnings.append(f"[{section.name}] {key} must be a JSON object, using default payload")
        return None
    return value


def load_settings(path: str = CONFIG_FILE) -> Settings:
    """Load snapshots.config on top of the defaults. Relative paths are relative to the project directory."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_dict(DEFAULT_SETTINGS)
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as e:
        raise ConfigError(f"Cannot parse '{path}': {e}") from e

    def project_path(*parts: str) -> str:
        return os.path.join(PROJECT_DIR, *parts)

    try:
        logs_dir = project_path(parser.get("DIRECTORIES", "logs_dir"))
        warnings: List[str] = []
        level_name = parser.get("LOGGING", "level").upper()
        log_level = logging.getLevelName(level_name)
        if not isinstance(log_level, int):
            warnings.append(f"[LOGGING] unknown level '{level_name}', using INFO")
            log_level = logging.INFO

        tg = parser["TELEGRAM"]
        wh = parser["WEBHOOK"]
        hb = parser["HEARTBEAT"]
        settings = Settings(
            configs_dir=project_path(parser.get("DIRECTORIES", "configs_dir")),
            log_file=os.path.join(logs_dir, parser.get("FILES", "log_file")),
            lock_file=os.path.join(logs_dir, parser.get("FILES", "lock_file")),
            delay_between_servers=parser.getfloat("TIMING", "delay_between_servers"),
            snapshot_creation_timeout=parser.getfloat("TIMING", "snapshot_creation_timeout"),
            action_poll_interval=parser.getfloat("TIMING", "action_poll_interval"),
            log_to_file=parser.getboolean("LOGGING", "file_enabled"),
            log_level=log_level,
            log_max_bytes=parser.getint("LOGGING", "max_bytes"),
            log_backup_count=parser.getint("LOGGING", "backup_count"),
            telegram=TelegramSettings(
                enabled=tg.getboolean("enabled"),
                bot_token=clean_value(tg.get("bot_token")),
                chat_id=clean_value(tg.get("chat_id")),
                message_success=decode_newlines(tg.get("message_success", "").strip()),
                message_failure=decode_newlines(tg.get("message_failure", "").strip()),
                delivery=_delivery(tg),
            ),
            webhook=WebhookSettings(
                enabled=wh.getboolean("enabled"),
                url=clean_value(wh.get("url")),
                payload_success=_json_template(wh, "payload_success", warnings),
                payload_failure=_json_template(wh, "payload_failure", warnings),
                delivery=_delivery(wh),
            ),
            heartbeat=HeartbeatSettings(
                enabled=hb.getboolean("enabled"),
                url=clean_value(hb.get("url")),
                delivery=_delivery(hb),
            ),
            warnings=warnings,
        )
    except ValueError as e:
        raise ConfigError(f"Invalid value in '{path}': {e}") from e

    if settings.heartbeat.enabled and not settings.heartbeat.url:
        settings.warnings.append("[HEARTBEAT] enabled but no url configured, heartbeat disabled")
        settings.heartbeat.enabled = False

    for secret in (settings.telegram.bot_token, settings.webhook.url, settings.heartbeat.url):
        REDACTOR.add(secret)
    return settings


# ============================================================================
# Server configuration (configs/*.json)
# ============================================================================

@dataclass
class TelegramTarget:
    bot_token: str
    chat_id: str
    message_success: Optional[str] = None
    message_failure: Optional[str] = None


@dataclass
class WebhookTarget:
    url: str
    payload_success: Optional[dict] = None
    payload_failure: Optional[dict] = None


@dataclass
class ServerConfig:
    provider: str  # "digitalocean" or "hetzner"
    id: str
    name: str
    api_token: str
    retain_last_snapshots: int
    telegram: Optional[TelegramTarget] = None
    webhook: Optional[WebhookTarget] = None
    heartbeat_url: Optional[str] = None


def _block(data: dict, key: str) -> dict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def channel_enabled(block: dict, global_enabled: bool, has_credentials: bool, channel: str, server_name: str) -> bool:
    """
    'enabled' in the server JSON wins; without it the global flag from snapshots.config applies.
    Credentials come from the server JSON, with snapshots.config as fallback.
    """
    explicit = block.get("enabled")
    if explicit is False or (explicit is None and not global_enabled):
        return False
    if not has_credentials:
        if explicit is True:
            logger.error(f"{channel} enabled for '{server_name}' but no credentials in JSON or snapshots.config - skipped")
        return False
    return True


def parse_server_config(path: str, settings: Settings) -> ServerConfig:
    """Read and validate one server JSON file. Raises ConfigError with a readable reason."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ConfigError(f"cannot read file: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("file does not contain a JSON object")

    keys = [key for key in CONFIG_KEYS if key in raw]
    if len(keys) != 1:
        raise ConfigError(f"must contain exactly one of: {', '.join(CONFIG_KEYS)}")
    provider = CONFIG_KEYS[keys[0]]
    data = raw[keys[0]]
    if not isinstance(data, dict):
        raise ConfigError(f"'{keys[0]}' must be a JSON object")

    declared = str(data.get("provider") or provider).strip().lower()
    if declared != provider:
        raise ConfigError(f"provider '{declared}' does not match key '{keys[0]}'")

    missing = [key for key in ("id", "name", "api_token", "retain_last_snapshots") if data.get(key) in (None, "")]
    if missing:
        raise ConfigError(f"missing required field(s): {', '.join(missing)}")

    api_token = clean_value(data["api_token"])
    if not api_token:
        raise ConfigError("api_token is empty or still the sample placeholder")
    server_id = clean_value(data["id"])
    if not server_id:
        raise ConfigError("id is still the sample placeholder")
    try:
        retain = int(data["retain_last_snapshots"])
    except (TypeError, ValueError):
        raise ConfigError("retain_last_snapshots must be an integer")
    if retain < 0:
        raise ConfigError("retain_last_snapshots must not be negative")
    name = str(data["name"]).strip()
    REDACTOR.add(api_token)

    tg = _block(data, "telegram")
    bot_token = clean_value(tg.get("bot_token")) or settings.telegram.bot_token
    chat_id = clean_value(tg.get("chat_id")) or settings.telegram.chat_id
    telegram = None
    if channel_enabled(tg, settings.telegram.enabled, bool(bot_token and chat_id), "Telegram", name):
        REDACTOR.add(bot_token)
        telegram = TelegramTarget(
            bot_token=bot_token,
            chat_id=chat_id,
            message_success=tg.get("message_success") or settings.telegram.message_success,
            message_failure=tg.get("message_failure") or settings.telegram.message_failure,
        )

    wh = _block(data, "webhook")
    webhook_url = clean_value(wh.get("url")) or settings.webhook.url
    webhook = None
    if channel_enabled(wh, settings.webhook.enabled, bool(webhook_url), "Webhook", name):
        REDACTOR.add(webhook_url)
        webhook = WebhookTarget(
            url=webhook_url,
            payload_success=_block(wh, "payload_success") or settings.webhook.payload_success,
            payload_failure=_block(wh, "payload_failure") or settings.webhook.payload_failure,
        )

    # Per-server heartbeat has no global fallback: the global heartbeat reports the whole run
    hb = _block(data, "heartbeat")
    heartbeat_url = clean_value(hb.get("url")) if hb.get("enabled", True) is not False else None
    REDACTOR.add(heartbeat_url)

    return ServerConfig(provider=provider, id=server_id, name=name, api_token=api_token,
                        retain_last_snapshots=retain, telegram=telegram, webhook=webhook,
                        heartbeat_url=heartbeat_url)


def config_files(configs_dir: str, names: Optional[List[str]] = None) -> List[str]:
    """Server config paths: the given names, or all *.json files in configs_dir (macOS '._*' files skipped)."""
    if not names:
        names = sorted(f for f in os.listdir(configs_dir) if f.lower().endswith(".json") and not f.startswith("._"))
    return [os.path.join(configs_dir, name) for name in names]


def warn_if_readable_by_others(paths: List[str]):
    exposed = [os.path.basename(p) for p in paths if os.path.exists(p) and os.stat(p).st_mode & 0o077]
    if exposed:
        logger.warning(f"Files with credentials are readable by other users, run 'chmod 600' on: {', '.join(exposed)}")


def load_servers(settings: Settings, names: Optional[List[str]] = None) -> List[ServerConfig]:
    """Load all valid server configs. Invalid files are logged and skipped."""
    servers: List[ServerConfig] = []
    seen = set()
    paths = config_files(settings.configs_dir, names)
    warn_if_readable_by_others([CONFIG_FILE] + paths)
    for path in paths:
        try:
            server = parse_server_config(path, settings)
        except ConfigError as e:
            logger.error(f"Skipping '{os.path.basename(path)}': {e}")
            continue
        key = (server.provider, server.id)
        if key in seen:
            logger.error(f"Skipping '{os.path.basename(path)}': {server.provider} server ID {server.id} is configured twice")
            continue
        seen.add(key)
        servers.append(server)
    return servers


# ============================================================================
# Cloud providers
# ============================================================================

@dataclass
class Snapshot:
    id: str
    name: str
    created_at: datetime.datetime
    owner_id: str  # ID of the server the snapshot was taken from


def snapshot_name_for(server: ServerConfig, now: Optional[datetime.datetime] = None) -> str:
    return f"{server.name}-{(now or datetime.datetime.now()).strftime('%Y%m%d%H%M%S')}"


def is_managed_name(server: ServerConfig, snapshot_name: str) -> bool:
    """Only '<server.name>-<YYYYmmddHHMMSS>' snapshots are managed, so manual snapshots are never touched."""
    return re.fullmatch(re.escape(server.name) + r"-\d{14}", snapshot_name or "") is not None


def select_for_deletion(snapshots: List[Snapshot], retain: int) -> List[Snapshot]:
    """Keep the newest `retain` snapshots (in addition to the one just created), return the rest."""
    return sorted(snapshots, key=lambda s: s.created_at, reverse=True)[retain:]


def parse_timestamp(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(datetime.timezone.utc)


def build_session(api_token: str) -> requests.Session:
    """
    HTTP session with retries for transient errors (connection problems, 429, 5xx).
    POST is only retried on connection errors, so a snapshot is never requested twice.
    """
    retry = Retry(total=5, connect=5, read=2, status=4, backoff_factor=2,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET", "DELETE"}),
                  respect_retry_after_header=True, raise_on_status=False)
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
        "User-Agent": f"{SCRIPT_NAME}/{__version__}",
    })
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class CloudProvider:
    """Common API flow; subclasses describe the provider-specific endpoints and fields."""

    label = ""
    base_url = ""
    list_path = ""
    list_params: Dict[str, object] = {}
    list_key = ""
    action_done = ""
    action_failed = ""

    def __init__(self, server: ServerConfig, session: Optional[requests.Session] = None):
        self.server = server
        self.session = session or build_session(server.api_token)

    # Provider-specific parts
    def _parse_snapshot(self, item: dict) -> Snapshot:
        raise NotImplementedError

    def _has_next_page(self, data: dict) -> bool:
        raise NotImplementedError

    def _create_request(self, name: str):
        raise NotImplementedError

    def _delete_path(self, snapshot_id: str) -> str:
        raise NotImplementedError

    # Common flow
    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self.session.request(method, self.base_url + path, timeout=API_REQUEST_TIMEOUT, **kwargs)
        except requests.RequestException as e:
            raise ProviderError(f"{self.label} {method} {path} failed: {e}") from e
        if not 200 <= response.status_code < 300:
            raise ProviderError(f"{self.label} {method} {path} returned HTTP {response.status_code}: {response.text[:200]}",
                                status_code=response.status_code)
        try:
            return response.json() if response.content else {}
        except ValueError as e:
            raise ProviderError(f"{self.label} {method} {path} returned invalid JSON") from e

    def list_snapshots(self) -> List[Snapshot]:
        """All snapshots of this server that were created by this script."""
        snapshots: List[Snapshot] = []
        page = 1
        while True:
            data = self._request("GET", self.list_path, params={**self.list_params, "page": page})
            for item in data.get(self.list_key) or []:
                try:
                    snapshot = self._parse_snapshot(item)
                except (KeyError, TypeError, ValueError):
                    logger.warning(f"Ignoring {self.label} snapshot with unexpected format (ID: {item.get('id')})")
                    continue
                if snapshot.owner_id == str(self.server.id) and is_managed_name(self.server, snapshot.name):
                    snapshots.append(snapshot)
            if not self._has_next_page(data):
                return snapshots
            page += 1

    def create_snapshot(self, name: str) -> str:
        """Start the snapshot and return the action ID to poll."""
        path, payload = self._create_request(name)
        data = self._request("POST", path, json=payload)
        action_id = (data.get("action") or {}).get("id")
        if not action_id:
            raise ProviderError(f"{self.label} did not return an action ID for the snapshot")
        return str(action_id)

    def wait_for_action(self, action_id: str, timeout: float, poll_interval: float) -> str:
        """Returns 'done', 'failed' or 'timeout'. Client errors (e.g. 401) abort immediately."""
        deadline = time.monotonic() + timeout
        while True:
            status = None
            try:
                status = (self._request("GET", f"/actions/{action_id}").get("action") or {}).get("status")
            except ProviderError as e:
                if e.status_code and 400 <= e.status_code < 500 and e.status_code != 429:
                    raise
                logger.warning(f"Checking snapshot progress failed, will retry: {e}")
            if status == self.action_done:
                return "done"
            if status == self.action_failed:
                return "failed"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(poll_interval)

    def delete_snapshot(self, snapshot: Snapshot) -> bool:
        try:
            self._request("DELETE", self._delete_path(snapshot.id))
        except ProviderError as e:
            if e.status_code == 404:
                logger.warning(f"Snapshot {snapshot.name} was already deleted")
                return True
            logger.error(f"Deleting snapshot {snapshot.name} failed: {e}")
            return False
        logger.info(f"Deleted snapshot {snapshot.name} (ID: {snapshot.id})")
        return True


class DigitalOceanProvider(CloudProvider):
    label = "DigitalOcean"
    base_url = "https://api.digitalocean.com/v2"
    list_path = "/snapshots"
    list_params = {"resource_type": "droplet", "per_page": 200}
    list_key = "snapshots"
    action_done = "completed"
    action_failed = "errored"

    def _parse_snapshot(self, item: dict) -> Snapshot:
        return Snapshot(id=str(item["id"]), name=item.get("name") or "",
                        created_at=parse_timestamp(item["created_at"]), owner_id=str(item.get("resource_id")))

    def _has_next_page(self, data: dict) -> bool:
        return bool(((data.get("links") or {}).get("pages") or {}).get("next"))

    def _create_request(self, name: str):
        return f"/droplets/{self.server.id}/actions", {"type": "snapshot", "name": name}

    def _delete_path(self, snapshot_id: str) -> str:
        return f"/snapshots/{snapshot_id}"


class HetznerProvider(CloudProvider):
    label = "Hetzner"
    base_url = "https://api.hetzner.cloud/v1"
    list_path = "/images"
    list_params = {"type": "snapshot", "per_page": 50}
    list_key = "images"
    action_done = "success"
    action_failed = "error"

    def _parse_snapshot(self, item: dict) -> Snapshot:
        return Snapshot(id=str(item["id"]), name=item.get("description") or "",
                        created_at=parse_timestamp(item["created"]),
                        owner_id=str((item.get("created_from") or {}).get("id")))

    def _has_next_page(self, data: dict) -> bool:
        return bool(((data.get("meta") or {}).get("pagination") or {}).get("next_page"))

    def _create_request(self, name: str):
        return f"/servers/{self.server.id}/actions/create_image", {"type": "snapshot", "description": name}

    def _delete_path(self, snapshot_id: str) -> str:
        return f"/images/{snapshot_id}"


PROVIDERS = {"digitalocean": DigitalOceanProvider, "hetzner": HetznerProvider}


# ============================================================================
# Notifications (Telegram, webhook, heartbeat)
# ============================================================================

@dataclass
class ServerResult:
    server: ServerConfig
    success: bool
    snapshot_name: Optional[str] = None
    total_snapshots: Optional[int] = None
    deleted: int = 0
    error: Optional[str] = None


def notification_context(result: ServerResult, hostname: str, timestamp: str) -> Dict[str, str]:
    """Values for the {placeholders} in Telegram messages and webhook payloads."""
    server = result.server
    total = "unknown" if result.total_snapshots is None else str(result.total_snapshots)
    return {
        "script": SCRIPT_NAME,
        "provider": server.provider,
        "server_name": server.name,
        "server_id": server.id,
        "droplet_name": server.name,  # backward compatibility
        "droplet_id": server.id,  # backward compatibility
        "snapshot_name": result.snapshot_name or "none",
        "total_snapshots": total,
        "snapshot_info": f"{total} snapshots exist",
        "status": "SUCCESS" if result.success else "FAILURE",
        "hostname": hostname,
        "timestamp": timestamp,
        "error": REDACTOR.redact(result.error),
    }


def render_template(template, context: Dict[str, str]):
    """Replace {placeholders} in strings, recursively inside dicts and lists."""
    if isinstance(template, str):
        for key, value in context.items():
            template = template.replace("{" + key + "}", value)
        return template
    if isinstance(template, dict):
        return {key: render_template(value, context) for key, value in template.items()}
    if isinstance(template, list):
        return [render_template(value, context) for value in template]
    return template


def default_telegram_message(context: Dict[str, str]) -> str:
    lines = [
        f"*{context['status']}* snapshot `{context['server_name']}`",
        f"Provider: `{context['provider'].upper()}`",
        f"Snapshot: `{context['snapshot_name']}`",
        f"Total: `{context['snapshot_info']}`",
        f"Host: `{context['hostname']}` at `{context['timestamp']}`",
    ]
    if context["error"]:
        lines.append(f"Error: `{context['error']}`")
    return "\n".join(lines)


def default_webhook_payload(result: ServerResult, context: Dict[str, str]) -> dict:
    return {
        "script": context["script"],
        "provider": context["provider"],
        "server": context["server_name"],
        "server_id": context["server_id"],
        "status": context["status"],
        "hostname": context["hostname"],
        "timestamp": context["timestamp"],
        "snapshot_name": context["snapshot_name"],
        "total_snapshots": result.total_snapshots,
        "snapshot_info": context["snapshot_info"],
        "error": context["error"] or None,
    }


def heartbeat_url(base_url: str, up: bool, message: str) -> str:
    """
    Uptime Kuma push URL with status and message. Any status/msg/ping parameters copied along
    with the push URL from Uptime Kuma are replaced.
    """
    parts = urlsplit(base_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in ("status", "msg", "ping")]
    query += [("status", "up" if up else "down"), ("msg", message[:HEARTBEAT_MESSAGE_LIMIT])]
    return urlunsplit(parts._replace(query=urlencode(query)))


def deliver(label: str, send: Callable[[], requests.Response], delivery: Delivery) -> bool:
    """Call `send` until it returns a 2xx response, with exponential backoff. Client errors are not retried."""
    for attempt in range(1, delivery.retries + 1):
        try:
            response = send()
            if 200 <= response.status_code < 300:
                logger.debug(f"{label} sent")
                return True
            logger.warning(f"{label} failed: HTTP {response.status_code} - {response.text[:200]}")
            if 400 <= response.status_code < 500 and response.status_code != 429:
                break
        except requests.RequestException as e:
            logger.warning(f"{label} failed: {e}")
        if attempt < delivery.retries:
            time.sleep(delivery.base_delay * 2 ** (attempt - 1))
    logger.warning(f"{label} not delivered")
    return False


class Notifier:
    def __init__(self, settings: Settings, http: Optional[requests.Session] = None):
        self.settings = settings
        self.http = http or requests.Session()
        self.hostname = socket.gethostname()

    def server_finished(self, result: ServerResult):
        """Telegram, webhook and per-server heartbeat. Never raises."""
        try:
            context = notification_context(result, self.hostname, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            server = result.server
            if server.telegram:
                template = server.telegram.message_success if result.success else server.telegram.message_failure
                text = render_template(template, context) if template else default_telegram_message(context)
                self.send_telegram(server.telegram, text)
            if server.webhook:
                template = server.webhook.payload_success if result.success else server.webhook.payload_failure
                payload = render_template(template, context) if template else default_webhook_payload(result, context)
                self.send_webhook(server.webhook.url, payload)
            if server.heartbeat_url:
                if result.success:
                    message = f"OK: {result.snapshot_name}, {context['snapshot_info']}"
                else:
                    message = f"FAILED: {context['error'] or 'snapshot not created'}"
                self.send_heartbeat(server.heartbeat_url, result.success, message, f"Heartbeat for '{server.name}'")
        except Exception:
            logger.exception(f"Sending notifications for '{result.server.name}' failed")

    def run_finished(self, up: bool, message: str):
        """Global heartbeat for the whole run. Never raises."""
        hb = self.settings.heartbeat
        if not hb.enabled:
            return
        try:
            self.send_heartbeat(hb.url, up, message, "Heartbeat")
        except Exception:
            logger.exception("Sending heartbeat failed")

    def send_telegram(self, target: TelegramTarget, text: str) -> bool:
        url = f"{TELEGRAM_API_URL}/bot{target.bot_token}/sendMessage"
        delivery = self.settings.telegram.delivery

        def send():
            response = self.http.post(url, data={"chat_id": target.chat_id, "text": text, "parse_mode": "Markdown"},
                                      timeout=delivery.timeout)
            # Values such as server names with '_' can break Markdown, plain text still gets the message through
            if response.status_code == 400 and "parse" in response.text.lower():
                logger.warning("Telegram rejected the Markdown formatting, sending as plain text")
                response = self.http.post(url, data={"chat_id": target.chat_id, "text": text}, timeout=delivery.timeout)
            return response

        return deliver("Telegram notification", send, delivery)

    def send_webhook(self, url: str, payload: dict) -> bool:
        delivery = self.settings.webhook.delivery
        return deliver("Webhook notification",
                       lambda: self.http.post(url, json=payload, headers={"Accept": "application/json"},
                                              timeout=delivery.timeout),
                       delivery)

    def send_heartbeat(self, base_url: str, up: bool, message: str, label: str) -> bool:
        delivery = self.settings.heartbeat.delivery
        message = REDACTOR.redact(message)
        logger.info(f"{label}: {'up' if up else 'down'} - {message}")
        return deliver(label, lambda: self.http.get(heartbeat_url(base_url, up, message), timeout=delivery.timeout),
                       delivery)


# ============================================================================
# Snapshot management
# ============================================================================

class SnapshotManager:
    def __init__(self, settings: Settings, servers: List[ServerConfig], notifier: Notifier,
                 provider_factory: Optional[Callable[[ServerConfig], CloudProvider]] = None):
        self.settings = settings
        self.servers = servers
        self.notifier = notifier
        self.provider_factory = provider_factory or (lambda server: PROVIDERS[server.provider](server))

    def process_server(self, server: ServerConfig, dry_run: bool = False) -> ServerResult:
        provider = self.provider_factory(server)
        try:
            existing: Optional[List[Snapshot]] = provider.list_snapshots()
            logger.info(f"Found {len(existing)} managed snapshot(s)")
        except ProviderError as e:
            logger.error(f"Listing snapshots failed: {e}")
            existing = None

        if dry_run:
            for snapshot in select_for_deletion(existing or [], server.retain_last_snapshots):
                logger.info(f"[dry run] would delete {snapshot.name} (ID: {snapshot.id})")
            logger.info(f"[dry run] would create {snapshot_name_for(server)}")
            return ServerResult(server, success=existing is not None,
                                total_snapshots=None if existing is None else len(existing))

        result = ServerResult(server, success=False)
        name = snapshot_name_for(server)
        logger.info(f"Creating snapshot {name}")
        try:
            action_id = provider.create_snapshot(name)
            outcome = provider.wait_for_action(action_id, self.settings.snapshot_creation_timeout,
                                               self.settings.action_poll_interval)
        except ProviderError as e:
            outcome, result.error = "failed", str(e)

        if outcome == "done":
            result.success, result.snapshot_name = True, name
            logger.info(f"Snapshot {name} created")
        elif outcome == "timeout":
            result.error = f"not confirmed within {self.settings.snapshot_creation_timeout:.0f}s, it may still complete"
        elif not result.error:
            result.error = f"{provider.label} reported the snapshot action as failed"

        # Old snapshots are only deleted once the new one is confirmed, otherwise we could end up without any backup
        if not result.success:
            logger.error(f"Snapshot {name} failed: {result.error}. Keeping all existing snapshots.")
        elif existing is None:
            logger.warning("Skipping retention because existing snapshots could not be listed")
        else:
            for snapshot in select_for_deletion(existing, server.retain_last_snapshots):
                result.deleted += provider.delete_snapshot(snapshot)

        try:
            result.total_snapshots = len(provider.list_snapshots())
        except ProviderError as e:
            logger.warning(f"Could not count snapshots: {e}")
        return result

    def run(self, dry_run: bool = False) -> List[ServerResult]:
        """Process all servers one after another. A failing server never stops the others."""
        results = []
        for idx, server in enumerate(self.servers, 1):
            logger.info(f"[{idx}/{len(self.servers)}] {server.provider} '{server.name}' "
                        f"(ID: {server.id}, retain: {server.retain_last_snapshots})")
            try:
                result = self.process_server(server, dry_run=dry_run)
            except Exception as e:
                logger.exception(f"Unexpected error for '{server.name}'")
                result = ServerResult(server, success=False, error=f"unexpected error: {e}")
            logger.info(f"FINAL_STATUS | {server.provider} | {server.name} | "
                        f"{'SUCCESS' if result.success else 'FAILURE'} | {result.snapshot_name or 'none'} | "
                        f"{'unknown' if result.total_snapshots is None else result.total_snapshots} snapshots | "
                        f"{result.deleted} deleted")
            if not dry_run:
                self.notifier.server_finished(result)
            results.append(result)
            if idx < len(self.servers):
                time.sleep(self.settings.delay_between_servers)
        return results


def exit_code(results: List[ServerResult]) -> int:
    """0 = all succeeded, 1 = all failed (or nothing to do), 2 = partial failure."""
    failed = sum(not r.success for r in results)
    if not results or failed == len(results):
        return 1
    return 2 if failed else 0


def run_summary(results: List[ServerResult], duration: float) -> str:
    failed = [r.server.name for r in results if not r.success]
    minutes = f"{duration / 60:.0f}min"
    if not failed:
        return f"OK: {len(results)}/{len(results)} snapshots created in {minutes}"
    return f"FAILED {len(failed)}/{len(results)} in {minutes}: {', '.join(failed)}"


# ============================================================================
# Runtime: logging, lock, entry point
# ============================================================================

def setup_logging(settings: Settings, verbose: bool = False):
    """
    The log file is the main record. To limit SD card writes it is small and rotated, and the
    console (cron mail, journald) only gets warnings unless --verbose is given or file logging is off.
    """
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG if verbose else settings.log_level)
    if REDACTOR not in logger.filters:
        logger.addFilter(REDACTOR)
    formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S")

    if settings.log_to_file:
        os.makedirs(os.path.dirname(settings.log_file), exist_ok=True)
        file_handler = RotatingFileHandler(settings.log_file, maxBytes=settings.log_max_bytes,
                                           backupCount=settings.log_backup_count, encoding="utf-8", delay=True)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.setLevel(logging.DEBUG if verbose or not settings.log_to_file else logging.WARNING)
    logger.addHandler(console)


def acquire_lock(path: str) -> Optional[int]:
    """Returns the lock file descriptor, or None if another instance holds the lock."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def release_lock(fd: int):
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def raise_terminated(signum, frame):
    raise Terminated()


def parse_arguments(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage snapshots for DigitalOcean droplets and Hetzner Cloud servers.")
    parser.add_argument("configs", nargs="*",
                        help="Server JSON files in the configs directory (default: all *.json files)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show debug output on the console")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Only show which snapshots would be created and deleted. No changes, no notifications.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_arguments(argv)
    try:
        settings = load_settings(CONFIG_FILE)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    setup_logging(settings, args.verbose)
    for warning in settings.warnings:
        logger.warning(warning)

    lock_fd = acquire_lock(settings.lock_file)
    if lock_fd is None:
        logger.error("Another instance is already running, exiting")
        return 3

    notifier = Notifier(settings)
    started = time.monotonic()
    signal.signal(signal.SIGTERM, raise_terminated)
    try:
        logger.info(f"=== {SCRIPT_NAME} {__version__} started{' (dry run)' if args.dry_run else ''} ===")
        if not os.path.isdir(settings.configs_dir):
            raise ConfigError(f"configs directory '{settings.configs_dir}' does not exist")
        servers = load_servers(settings, args.configs)
        if not servers:
            raise ConfigError("no valid server configuration found")

        results = SnapshotManager(settings, servers, notifier).run(dry_run=args.dry_run)
        code = exit_code(results)
        summary = run_summary(results, time.monotonic() - started)
        logger.log(logging.INFO if code == 0 else logging.ERROR, f"=== {summary} ===")
        if not args.dry_run:
            notifier.run_finished(code == 0, summary)
        return code
    except ConfigError as e:
        logger.error(f"Configuration error: {e}")
        if not args.dry_run:
            notifier.run_finished(False, f"Configuration error: {e}")
        return 1
    except Terminated:
        # Ignore a second SIGTERM so the down heartbeat still gets out
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        logger.error("Stopped by SIGTERM (systemd timeout, shutdown or manual stop)")
        if not args.dry_run:
            notifier.run_finished(False, "Stopped: terminated before all servers were processed")
        return 1
    except Exception as e:
        logger.exception("Unexpected error")
        if not args.dry_run:
            notifier.run_finished(False, f"Crashed: {type(e).__name__}: {e}")
        return 1
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        release_lock(lock_fd)


if __name__ == "__main__":
    sys.exit(main())

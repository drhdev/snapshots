"""Tests for snapshots.py. All HTTP traffic goes to fake sessions, see conftest.py."""

import json
import logging
import os
import signal
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

import snapshots
from snapshots import (Delivery, HeartbeatSettings, ServerConfig, ServerResult, Settings, Snapshot,
                       TelegramSettings, TelegramTarget, WebhookSettings, WebhookTarget)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else (json.dumps(json_data) if json_data is not None else "")
        self.content = self.text.encode()

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Answers requests from a route table {(METHOD, url suffix): response or [responses]}."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (route_method, suffix), response in self.routes.items():
            if route_method == method and url.split("?")[0].endswith(suffix):
                if isinstance(response, list):
                    return response.pop(0) if len(response) > 1 else response[0]
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"Unexpected request: {method} {url}")

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)


def make_server(provider="digitalocean", id="123", name="web-1", retain=1, **kwargs):
    return ServerConfig(provider=provider, id=id, name=name, api_token="secret-api-token-0123456789",
                        retain_last_snapshots=retain, **kwargs)


def make_settings(**kwargs):
    defaults = dict(delay_between_servers=0, snapshot_creation_timeout=60, action_poll_interval=0,
                    telegram=TelegramSettings(enabled=False),
                    heartbeat=HeartbeatSettings(delivery=Delivery(retries=2, base_delay=0)),
                    webhook=WebhookSettings(delivery=Delivery(retries=2, base_delay=0)))
    defaults.update(kwargs)
    return Settings(**defaults)


def snap(id, name, day, owner="123"):
    return Snapshot(id=str(id), name=name, created_at=datetime(2025, 1, day, tzinfo=timezone.utc), owner_id=owner)


def write_json(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


def server_json(**overrides):
    block = {"provider": "digitalocean", "id": "123", "name": "web-1",
             "api_token": "token-abcdefghijklmnopqrstuvwxyz", "retain_last_snapshots": 2}
    block.update(overrides)
    return {"digitalocean_droplet": block}


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

class TestSettings:
    def test_defaults_without_file(self, tmp_path):
        settings = snapshots.load_settings(str(tmp_path / "missing.config"))
        assert settings.log_level == logging.INFO
        assert settings.log_max_bytes == 1048576
        assert settings.log_backup_count == 2
        assert settings.log_to_file is True
        assert settings.heartbeat.enabled is False
        assert settings.snapshot_creation_timeout == 3600

    def test_values_from_file(self, tmp_path):
        path = tmp_path / "snapshots.config"
        path.write_text(
            "[LOGGING]\nlevel = warning\nfile_enabled = false\n"
            "[TELEGRAM]\nmessage_success = Hallo {server_name}\\nGrüße 100%\n"
            "[HEARTBEAT]\nenabled = true\nurl = https://kuma.test/api/push/AbC123?status=up&msg=OK&ping=\n",
            encoding="utf-8")
        settings = snapshots.load_settings(str(path))
        assert settings.log_level == logging.WARNING
        assert settings.log_to_file is False
        # Newlines decoded, umlauts and '%' kept
        assert settings.telegram.message_success == "Hallo {server_name}\nGrüße 100%"
        assert settings.heartbeat.enabled is True

    def test_heartbeat_placeholder_url_disables_heartbeat(self, tmp_path):
        path = tmp_path / "snapshots.config"
        path.write_text("[HEARTBEAT]\nenabled = true\nurl = https://uptime.example.com/api/push/your_push_token\n")
        settings = snapshots.load_settings(str(path))
        assert settings.heartbeat.enabled is False
        assert any("HEARTBEAT" in w for w in settings.warnings)

    def test_invalid_payload_template_falls_back_with_warning(self, tmp_path):
        path = tmp_path / "snapshots.config"
        path.write_text("[WEBHOOK]\npayload_success = {not json\n")
        settings = snapshots.load_settings(str(path))
        assert settings.webhook.payload_success is None
        assert settings.warnings

    def test_invalid_number_raises_config_error(self, tmp_path):
        path = tmp_path / "snapshots.config"
        path.write_text("[TIMING]\ndelay_between_servers = soon\n")
        with pytest.raises(snapshots.ConfigError):
            snapshots.load_settings(str(path))


# ----------------------------------------------------------------------------
# Server configuration
# ----------------------------------------------------------------------------

class TestServerConfig:
    def test_valid_digitalocean(self, tmp_path):
        server = snapshots.parse_server_config(write_json(tmp_path, "a.json", server_json()), make_settings())
        assert (server.provider, server.id, server.name, server.retain_last_snapshots) == ("digitalocean", "123", "web-1", 2)

    def test_valid_hetzner(self, tmp_path):
        data = {"hetzner_cloud_server": {"provider": "hetzner", "id": 42, "name": "db-1",
                                         "api_token": "token-abcdefghijklmnopqrstuvwxyz", "retain_last_snapshots": 0}}
        server = snapshots.parse_server_config(write_json(tmp_path, "h.json", data), make_settings())
        assert (server.provider, server.id) == ("hetzner", "42")

    @pytest.mark.parametrize("overrides, reason", [
        ({"api_token": "your_digitalocean_api_token_1"}, "placeholder"),
        ({"id": "your-digitalocean-droplet-id-1"}, "placeholder"),
        ({"retain_last_snapshots": -1}, "negative"),
        ({"retain_last_snapshots": "three"}, "integer"),
        ({"name": ""}, "missing"),
        ({"provider": "hetzner"}, "does not match"),
    ])
    def test_invalid_values(self, tmp_path, overrides, reason):
        path = write_json(tmp_path, "a.json", server_json(**overrides))
        with pytest.raises(snapshots.ConfigError, match=reason):
            snapshots.parse_server_config(path, make_settings())

    def test_unreadable_files(self, tmp_path):
        broken = tmp_path / "broken.json"
        broken.write_text("{nope")
        with pytest.raises(snapshots.ConfigError):
            snapshots.parse_server_config(str(broken), make_settings())
        with pytest.raises(snapshots.ConfigError, match="exactly one"):
            snapshots.parse_server_config(write_json(tmp_path, "x.json", {"other": {}}), make_settings())

    def test_telegram_json_credentials(self, tmp_path):
        data = server_json(telegram={"enabled": True, "bot_token": "111:json-token", "chat_id": "5"})
        server = snapshots.parse_server_config(write_json(tmp_path, "a.json", data), make_settings())
        assert server.telegram.bot_token == "111:json-token"

    def test_telegram_enabled_in_json_uses_global_credentials(self, tmp_path):
        settings = make_settings(telegram=TelegramSettings(enabled=False, bot_token="222:global", chat_id="9",
                                                           message_failure="global failure"))
        data = server_json(telegram={"enabled": True, "bot_token": "your_telegram_bot_token_here"})
        server = snapshots.parse_server_config(write_json(tmp_path, "a.json", data), settings)
        assert (server.telegram.bot_token, server.telegram.chat_id) == ("222:global", "9")
        assert server.telegram.message_failure == "global failure"

    def test_telegram_disabled_in_json_ignores_global(self, tmp_path):
        settings = make_settings(telegram=TelegramSettings(enabled=True, bot_token="222:global", chat_id="9"))
        data = server_json(telegram={"enabled": False})
        assert snapshots.parse_server_config(write_json(tmp_path, "a.json", data), settings).telegram is None

    def test_telegram_follows_global_flag_when_not_configured(self, tmp_path):
        path = write_json(tmp_path, "a.json", server_json())
        on = make_settings(telegram=TelegramSettings(enabled=True, bot_token="222:global", chat_id="9"))
        off = make_settings(telegram=TelegramSettings(enabled=False, bot_token="222:global", chat_id="9"))
        assert snapshots.parse_server_config(path, on).telegram is not None
        assert snapshots.parse_server_config(path, off).telegram is None

    def test_webhook_and_heartbeat(self, tmp_path):
        data = server_json(webhook={"enabled": True, "url": "https://hooks.test/abc"},
                           heartbeat={"url": "https://kuma.test/api/push/srv1"})
        server = snapshots.parse_server_config(write_json(tmp_path, "a.json", data), make_settings())
        assert server.webhook.url == "https://hooks.test/abc"
        assert server.heartbeat_url == "https://kuma.test/api/push/srv1"

    def test_heartbeat_disabled_or_placeholder(self, tmp_path):
        for heartbeat in ({"enabled": False, "url": "https://kuma.test/api/push/srv1"},
                          {"enabled": True, "url": "https://uptime.example.com/api/push/your_push_token"}):
            path = write_json(tmp_path, "a.json", server_json(heartbeat=heartbeat))
            assert snapshots.parse_server_config(path, make_settings()).heartbeat_url is None

    def test_load_servers_skips_invalid_and_duplicates(self, tmp_path, caplog):
        write_json(tmp_path, "a.json", server_json())
        write_json(tmp_path, "b.json", server_json(name="same-id"))
        write_json(tmp_path, "c.json", server_json(id="456", api_token=""))
        (tmp_path / "._a.json").write_bytes(b"\x00\x01")
        (tmp_path / "notes.txt").write_text("ignored")
        servers = snapshots.load_servers(make_settings(configs_dir=str(tmp_path)))
        assert [s.name for s in servers] == ["web-1"]
        assert "configured twice" in caplog.text


# ----------------------------------------------------------------------------
# Retention
# ----------------------------------------------------------------------------

class TestRetention:
    def test_managed_names(self):
        server = make_server(name="app.example.org")
        assert snapshots.is_managed_name(server, "app.example.org-20250101120000")
        assert not snapshots.is_managed_name(server, "before upgrade app.example.org")
        assert not snapshots.is_managed_name(server, "x-app.example.org-20250101120000")
        assert not snapshots.is_managed_name(server, "app.example.org-manual")
        assert not snapshots.is_managed_name(server, "appXexample.org-20250101120000")
        assert not snapshots.is_managed_name(server, None)

    def test_select_for_deletion_keeps_newest(self):
        items = [snap(1, "a", 1), snap(3, "c", 3), snap(2, "b", 2)]
        assert [s.id for s in snapshots.select_for_deletion(items, 1)] == ["2", "1"]
        assert [s.id for s in snapshots.select_for_deletion(items, 0)] == ["3", "2", "1"]
        assert snapshots.select_for_deletion(items, 5) == []


# ----------------------------------------------------------------------------
# Providers
# ----------------------------------------------------------------------------

class TestProviders:
    def test_digitalocean_lists_only_own_managed_snapshots_over_pages(self):
        server = make_server(id="123", name="web-1")
        page1 = {"snapshots": [
            {"id": 1, "resource_id": 123, "name": "web-1-20250101060000", "created_at": "2025-01-01T06:00:00Z"},
            {"id": 2, "resource_id": 999, "name": "web-1-20250101060000", "created_at": "2025-01-01T06:00:00Z"},
            {"id": 3, "resource_id": 123, "name": "manual web-1 backup", "created_at": "2025-01-01T06:00:00Z"},
            {"id": 4, "resource_id": 123, "name": "web-1-20250101070000", "created_at": "garbage"},
        ], "links": {"pages": {"next": "page2"}}}
        page2 = {"snapshots": [
            {"id": 5, "resource_id": 123, "name": "web-1-20250102060000", "created_at": "2025-01-02T06:00:00Z"},
        ], "links": {}}
        session = FakeSession({("GET", "/snapshots"): [FakeResponse(json_data=page1), FakeResponse(json_data=page2)]})
        result = snapshots.DigitalOceanProvider(server, session).list_snapshots()
        assert [s.id for s in result] == ["1", "5"]
        assert [c[2]["params"]["page"] for c in session.calls] == [1, 2]

    def test_hetzner_lists_only_own_managed_snapshots(self):
        server = make_server(provider="hetzner", id="42", name="db-1")
        data = {"images": [
            {"id": 1, "description": "db-1-20250101060000", "created": "2025-01-01T06:00:00Z", "created_from": {"id": 42}},
            {"id": 2, "description": "db-1-20250101060000", "created": "2025-01-01T06:00:00Z", "created_from": {"id": 43}},
            {"id": 3, "description": "pre-upgrade db-1", "created": "2025-01-01T06:00:00Z", "created_from": {"id": 42}},
            {"id": 4, "description": "db-1-20250101060000", "created": "2025-01-01T06:00:00Z", "created_from": None},
        ], "meta": {"pagination": {"next_page": None}}}
        session = FakeSession({("GET", "/images"): FakeResponse(json_data=data)})
        assert [s.id for s in snapshots.HetznerProvider(server, session).list_snapshots()] == ["1"]

    def test_create_and_wait(self):
        server = make_server(provider="hetzner", id="42", name="db-1")
        session = FakeSession({
            ("POST", "/servers/42/actions/create_image"): FakeResponse(201, {"action": {"id": 7}, "image": {"id": 8}}),
            ("GET", "/actions/7"): [FakeResponse(json_data={"action": {"status": "running"}}),
                                    FakeResponse(json_data={"action": {"status": "success"}})],
        })
        provider = snapshots.HetznerProvider(server, session)
        action_id = provider.create_snapshot("db-1-20250101060000")
        assert session.calls[0][2]["json"] == {"type": "snapshot", "description": "db-1-20250101060000"}
        assert provider.wait_for_action(action_id, timeout=60, poll_interval=0) == "done"

    def test_wait_reports_failed_action(self):
        session = FakeSession({("GET", "/actions/7"): FakeResponse(json_data={"action": {"status": "errored"}})})
        assert snapshots.DigitalOceanProvider(make_server(), session).wait_for_action("7", 60, 0) == "failed"

    def test_wait_times_out(self, monkeypatch):
        clock = iter(range(0, 1000, 30))
        monkeypatch.setattr(snapshots.time, "monotonic", lambda: next(clock))
        session = FakeSession({("GET", "/actions/7"): FakeResponse(json_data={"action": {"status": "in-progress"}})})
        assert snapshots.DigitalOceanProvider(make_server(), session).wait_for_action("7", 60, 0) == "timeout"

    def test_wait_retries_server_errors_and_aborts_on_auth_error(self):
        flaky = FakeSession({("GET", "/actions/7"): [FakeResponse(503, text="busy"),
                                                     FakeResponse(json_data={"action": {"status": "completed"}})]})
        assert snapshots.DigitalOceanProvider(make_server(), flaky).wait_for_action("7", 60, 0) == "done"
        unauthorized = FakeSession({("GET", "/actions/7"): FakeResponse(401, text="unauthorized")})
        with pytest.raises(snapshots.ProviderError):
            snapshots.DigitalOceanProvider(make_server(), unauthorized).wait_for_action("7", 60, 0)

    def test_create_without_action_id_fails(self):
        session = FakeSession({("POST", "/droplets/123/actions"): FakeResponse(json_data={})})
        with pytest.raises(snapshots.ProviderError):
            snapshots.DigitalOceanProvider(make_server(), session).create_snapshot("web-1-20250101060000")

    def test_network_error_becomes_provider_error(self):
        session = FakeSession({("GET", "/snapshots"): requests.ConnectionError("down")})
        with pytest.raises(snapshots.ProviderError):
            snapshots.DigitalOceanProvider(make_server(), session).list_snapshots()

    def test_delete(self):
        session = FakeSession({
            ("DELETE", "/snapshots/1"): FakeResponse(204, text=""),
            ("DELETE", "/snapshots/2"): FakeResponse(404, text="not found"),
            ("DELETE", "/snapshots/3"): FakeResponse(500, text="boom"),
        })
        provider = snapshots.DigitalOceanProvider(make_server(), session)
        assert provider.delete_snapshot(snap(1, "a", 1)) is True
        assert provider.delete_snapshot(snap(2, "b", 1)) is True
        assert provider.delete_snapshot(snap(3, "c", 1)) is False

    def test_session_retries_only_idempotent_methods(self):
        retry = snapshots.build_session("token").get_adapter("https://api.hetzner.cloud").max_retries
        assert "POST" not in retry.allowed_methods
        assert 429 in retry.status_forcelist


# ----------------------------------------------------------------------------
# Snapshot manager
# ----------------------------------------------------------------------------

class FakeProvider:
    label = "Fake"

    def __init__(self, existing, outcome="done", list_error=False):
        self.existing = existing
        self.outcome = outcome
        self.list_error = list_error
        self.deleted = []
        self.created = []

    def list_snapshots(self):
        if self.list_error:
            raise snapshots.ProviderError("list failed")
        return [s for s in self.existing if s.id not in self.deleted] + \
               [snap(f"new-{n}", n, 28) for n in self.created]

    def create_snapshot(self, name):
        if self.outcome == "error":
            raise snapshots.ProviderError("HTTP 422: droplet is locked")
        self.created.append(name)
        return "action-1"

    def wait_for_action(self, action_id, timeout, poll_interval):
        return self.outcome

    def delete_snapshot(self, snapshot):
        self.deleted.append(snapshot.id)
        return True


class RecordingNotifier:
    def __init__(self):
        self.results = []

    def server_finished(self, result):
        self.results.append(result)


def run_manager(provider, servers=None, dry_run=False):
    servers = servers or [make_server(retain=1)]
    notifier = RecordingNotifier()
    manager = snapshots.SnapshotManager(make_settings(), servers, notifier, provider_factory=lambda s: provider)
    return manager.run(dry_run=dry_run), notifier


class TestSnapshotManager:
    existing = [snap(1, "web-1-20250101060000", 1), snap(2, "web-1-20250102060000", 2), snap(3, "web-1-20250103060000", 3)]

    def test_success_deletes_beyond_retention(self):
        provider = FakeProvider(list(self.existing))
        (result,), notifier = run_manager(provider)
        assert result.success and result.deleted == 2
        assert provider.deleted == ["2", "1"]
        assert result.total_snapshots == 2  # newest old one + the new one
        assert notifier.results == [result]

    @pytest.mark.parametrize("outcome", ["failed", "timeout", "error"])
    def test_unconfirmed_snapshot_keeps_everything(self, outcome):
        provider = FakeProvider(list(self.existing), outcome=outcome)
        (result,), _ = run_manager(provider)
        assert not result.success
        assert provider.deleted == []
        assert result.error

    def test_listing_failure_skips_retention_but_creates_snapshot(self):
        provider = FakeProvider(list(self.existing), list_error=True)
        (result,), _ = run_manager(provider)
        assert result.success
        assert provider.deleted == [] and len(provider.created) == 1
        assert result.total_snapshots is None

    def test_dry_run_changes_nothing_and_sends_nothing(self):
        provider = FakeProvider(list(self.existing))
        (result,), notifier = run_manager(provider, dry_run=True)
        assert provider.created == [] and provider.deleted == []
        assert notifier.results == []

    def test_unexpected_error_does_not_stop_other_servers(self):
        class Exploding(FakeProvider):
            def list_snapshots(self):
                raise ZeroDivisionError("bug")
        providers = {"a": Exploding([]), "b": FakeProvider([])}
        servers = [make_server(name="a"), make_server(name="b", id="2")]
        manager = snapshots.SnapshotManager(make_settings(), servers, RecordingNotifier(),
                                            provider_factory=lambda s: providers[s.name])
        results = manager.run()
        assert [r.success for r in results] == [False, True]
        assert snapshots.exit_code(results) == 2

    def test_exit_codes(self):
        ok, bad = ServerResult(make_server(), True), ServerResult(make_server(), False)
        assert snapshots.exit_code([ok, ok]) == 0
        assert snapshots.exit_code([ok, bad]) == 2
        assert snapshots.exit_code([bad]) == 1
        assert snapshots.exit_code([]) == 1

    def test_run_summary(self):
        ok, bad = ServerResult(make_server(name="a"), True), ServerResult(make_server(name="b"), False)
        assert snapshots.run_summary([ok], 120).startswith("OK: 1/1")
        assert snapshots.run_summary([ok, bad], 120) == "FAILED 1/2 in 2min: b"


# ----------------------------------------------------------------------------
# Notifications and heartbeat
# ----------------------------------------------------------------------------

def make_notifier(session, **settings_kwargs):
    settings = make_settings(telegram=TelegramSettings(delivery=Delivery(retries=2, base_delay=0)), **settings_kwargs)
    return snapshots.Notifier(settings, http=session)


class TestNotifications:
    def test_render_template_recursive(self):
        template = {"text": "{server_name} is {status}", "nested": [{"id": "{server_id}"}, 5], "n": 1}
        context = {"server_name": "web-1", "status": "SUCCESS", "server_id": "123"}
        assert snapshots.render_template(template, context) == {
            "text": "web-1 is SUCCESS", "nested": [{"id": "123"}, 5], "n": 1}

    def test_telegram_template_and_markdown_fallback(self):
        session = FakeSession({("POST", "/sendMessage"): [FakeResponse(400, text="Bad Request: can't parse entities"),
                                                          FakeResponse(200, {"ok": True})]})
        server = make_server(telegram=TelegramTarget("111:tok", "5", message_failure="Failed {server_name}: {error}"))
        make_notifier(session).server_finished(ServerResult(server, False, error="timeout"))
        assert [c[2]["data"].get("parse_mode") for c in session.calls] == ["Markdown", None]
        assert session.calls[1][2]["data"]["text"] == "Failed web-1: timeout"

    def test_telegram_default_message_contains_error(self):
        session = FakeSession({("POST", "/sendMessage"): FakeResponse(200, {"ok": True})})
        server = make_server(telegram=TelegramTarget("111:tok", "5"))
        make_notifier(session).server_finished(ServerResult(server, False, error="quota exceeded"))
        text = session.calls[0][2]["data"]["text"]
        assert "FAILURE" in text and "quota exceeded" in text

    def test_webhook_default_and_custom_payload(self):
        session = FakeSession({("POST", "/hook"): FakeResponse(200, {})})
        notifier = make_notifier(session)
        notifier.server_finished(ServerResult(make_server(webhook=WebhookTarget("https://hooks.test/hook")),
                                              True, snapshot_name="web-1-20250101060000", total_snapshots=2))
        payload = session.calls[0][2]["json"]
        assert payload["status"] == "SUCCESS" and payload["total_snapshots"] == 2 and payload["error"] is None
        custom = WebhookTarget("https://hooks.test/hook", payload_failure={"msg": "{server_name}: {error}"})
        notifier.server_finished(ServerResult(make_server(webhook=custom), False, error="boom"))
        assert session.calls[1][2]["json"] == {"msg": "web-1: boom"}

    def test_delivery_retries_server_errors_but_not_client_errors(self):
        calls = []

        def send(status):
            calls.append(status)
            return FakeResponse(status, text="x")
        assert snapshots.deliver("x", lambda: send(500), Delivery(retries=3, base_delay=0)) is False
        assert calls == [500, 500, 500]
        calls.clear()
        assert snapshots.deliver("x", lambda: send(404), Delivery(retries=3, base_delay=0)) is False
        assert calls == [404]

    def test_notification_errors_never_raise(self):
        session = FakeSession({("POST", "/sendMessage"): requests.ConnectionError("offline")})
        server = make_server(telegram=TelegramTarget("111:tok", "5"))
        make_notifier(session).server_finished(ServerResult(server, True))  # must not raise


class TestHeartbeat:
    def test_url_replaces_parameters_from_uptime_kuma(self):
        url = snapshots.heartbeat_url("https://kuma.test/api/push/AbC123?status=up&msg=OK&ping=", False, "FAILED 1/2: b")
        parts = urlsplit(url)
        assert parts.path == "/api/push/AbC123"
        assert parse_qs(parts.query) == {"status": ["down"], "msg": ["FAILED 1/2: b"]}

    def test_message_is_truncated(self):
        url = snapshots.heartbeat_url("https://kuma.test/api/push/x", True, "a" * 500)
        assert len(parse_qs(urlsplit(url).query)["msg"][0]) == snapshots.HEARTBEAT_MESSAGE_LIMIT

    def test_run_heartbeat_only_when_enabled(self):
        session = FakeSession({("GET", "/api/push/run"): FakeResponse(200, {"ok": True})})
        make_notifier(session).run_finished(True, "OK")
        assert session.calls == []
        hb = HeartbeatSettings(enabled=True, url="https://kuma.test/api/push/run", delivery=Delivery(retries=1))
        make_notifier(session, heartbeat=hb).run_finished(False, "FAILED 1/1: web-1")
        assert parse_qs(urlsplit(session.calls[0][1]).query)["status"] == ["down"]

    def test_per_server_heartbeat(self):
        session = FakeSession({("GET", "/api/push/srv"): FakeResponse(200, {"ok": True})})
        server = make_server(heartbeat_url="https://kuma.test/api/push/srv")
        make_notifier(session).server_finished(ServerResult(server, True, snapshot_name="web-1-20250101060000",
                                                            total_snapshots=2))
        query = parse_qs(urlsplit(session.calls[0][1]).query)
        assert query["status"] == ["up"] and "web-1-20250101060000" in query["msg"][0]


# ----------------------------------------------------------------------------
# Secrets and logging
# ----------------------------------------------------------------------------

class TestRedaction:
    def test_redacts_registered_secrets_and_url_parts(self):
        redactor = snapshots.SecretRedactor()
        redactor.add("dop_v1_abcdefghijklmnop")
        redactor.add("https://kuma.test/api/push/AbC123")
        text = redactor.redact("token dop_v1_abcdefghijklmnop, url: /api/push/AbC123?status=up, "
                               "https://kuma.test/api/push/AbC123")
        assert "abcdefghijklmnop" not in text and "AbC123" not in text

    def test_redacts_telegram_tokens_and_bearer_headers(self):
        text = snapshots.SecretRedactor().redact("url: /bot123456:AAH-secret_part/sendMessage, Authorization: Bearer xyz")
        assert "AAH-secret_part" not in text and "xyz" not in text

    def test_log_records_and_tracebacks_are_redacted(self, tmp_path):
        snapshots.REDACTOR.add("super-secret-value-123")
        settings = make_settings(log_file=str(tmp_path / "logs" / "test.log"))
        snapshots.setup_logging(settings)
        snapshots.logger.error("failed with %s", "super-secret-value-123")
        try:
            raise ValueError("super-secret-value-123")
        except ValueError:
            snapshots.logger.exception("boom")
        for handler in snapshots.logger.handlers:
            handler.flush()
        content = (tmp_path / "logs" / "test.log").read_text()
        assert "super-secret-value-123" not in content and content.count("***") >= 2

    def test_notification_error_text_is_redacted(self):
        snapshots.REDACTOR.add("token-in-error-message-42")
        context = snapshots.notification_context(
            ServerResult(make_server(), False, error="401 for token-in-error-message-42"), "host", "now")
        assert "token-in-error-message-42" not in context["error"]


class TestLogging:
    def test_file_logging_is_small_and_lazy(self, tmp_path):
        settings = make_settings(log_file=str(tmp_path / "logs" / "s.log"))
        snapshots.setup_logging(settings)
        file_handler, console = snapshots.logger.handlers
        assert file_handler.maxBytes == 1048576 and file_handler.backupCount == 2
        assert console.level == logging.WARNING
        assert not os.path.exists(settings.log_file)  # created on first write only
        snapshots.logger.debug("not written at INFO level")
        assert not os.path.exists(settings.log_file)

    def test_without_file_logging_info_goes_to_console(self, tmp_path):
        settings = make_settings(log_to_file=False, log_file=str(tmp_path / "s.log"))
        snapshots.setup_logging(settings)
        (console,) = snapshots.logger.handlers
        assert console.level == logging.DEBUG and snapshots.logger.level == logging.INFO
        assert not os.path.exists(settings.log_file)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

class TestMain:
    @pytest.fixture
    def project(self, tmp_path, monkeypatch):
        (tmp_path / "configs").mkdir()
        config = tmp_path / "snapshots.config"
        config.write_text("[TIMING]\ndelay_between_servers = 0\n"
                          "[HEARTBEAT]\nenabled = true\nurl = https://kuma.test/api/push/run\nretries = 1\n")
        monkeypatch.setattr(snapshots, "PROJECT_DIR", str(tmp_path))
        monkeypatch.setattr(snapshots, "CONFIG_FILE", str(config))
        heartbeats = []
        monkeypatch.setattr(snapshots.Notifier, "run_finished", lambda self, up, msg: heartbeats.append((up, msg)))
        return tmp_path, heartbeats

    def test_no_configs_reports_down(self, project):
        _, heartbeats = project
        assert snapshots.main([]) == 1
        assert heartbeats == [(False, "Configuration error: no valid server configuration found")]

    def test_full_run_reports_up(self, project, monkeypatch):
        tmp_path, heartbeats = project
        write_json(tmp_path / "configs", "web.json", server_json())
        monkeypatch.setitem(snapshots.PROVIDERS, "digitalocean", lambda server: FakeProvider([]))
        assert snapshots.main([]) == 0
        assert heartbeats[0][0] is True and heartbeats[0][1].startswith("OK: 1/1")

    def test_dry_run_sends_no_heartbeat(self, project, monkeypatch):
        tmp_path, heartbeats = project
        write_json(tmp_path / "configs", "web.json", server_json())
        provider = FakeProvider([])
        monkeypatch.setitem(snapshots.PROVIDERS, "digitalocean", lambda server: provider)
        assert snapshots.main(["--dry-run"]) == 0
        assert heartbeats == [] and provider.created == []

    def test_sigterm_stops_run_and_reports_down(self, project, monkeypatch):
        tmp_path, heartbeats = project
        write_json(tmp_path / "configs", "a.json", server_json())
        write_json(tmp_path / "configs", "b.json", server_json(id="456", name="web-2"))
        processed = []

        class Stopping(FakeProvider):
            def list_snapshots(self):
                processed.append(1)
                os.kill(os.getpid(), signal.SIGTERM)  # real signal, handled by main()
        monkeypatch.setitem(snapshots.PROVIDERS, "digitalocean", lambda server: Stopping([]))
        assert snapshots.main([]) == 1
        assert len(processed) == 1  # second server not started
        assert heartbeats == [(False, "Stopped: terminated before all servers were processed")]

    def test_second_instance_exits_with_3(self, project):
        tmp_path, heartbeats = project
        fd = snapshots.acquire_lock(str(tmp_path / "logs" / "snapshots.lock"))
        try:
            assert snapshots.main([]) == 3
        finally:
            snapshots.release_lock(fd)
        assert heartbeats == []

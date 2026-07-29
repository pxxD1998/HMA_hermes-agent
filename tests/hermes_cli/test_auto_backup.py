"""Tests for scheduled auto-backup and `hermes backup --list` (#12238).

Covers:

  1. maybe_create_auto_backup — disabled-by-default gate, schedule parsing,
     interval gating via last_run_at, archive creation, keep_last pruning,
     failure stamping (no retry-hammering), custom dir override.
  2. list_backup_archives / run_backup_list — kind classification, ordering,
     empty-state output.
"""

from __future__ import annotations

import json
import logging
import threading
import zipfile
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import backup as B
from hermes_cli.config_defaults import DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model:\n  provider: openrouter\n", encoding="utf-8")
    (home / "skills").mkdir()
    (home / "skills" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    return home


def _set_cfg(monkeypatch, cfg: dict) -> None:
    monkeypatch.setattr(B, "_get_backup_config", lambda: cfg)


def _state(home: Path) -> dict:
    path = home / "backups" / B._AUTO_STATE_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_state(home: Path, state: dict) -> None:
    backups = home / "backups"
    backups.mkdir(exist_ok=True)
    (backups / B._AUTO_STATE_FILE).write_text(json.dumps(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

class TestScheduleParsing:
    def test_default_config_declares_disabled_backup_schema(self):
        assert DEFAULT_CONFIG["backup"] == {
            "enabled": False,
            "schedule": "daily",
            "keep_last": 7,
            "dir": None,
        }

    def test_named_schedules(self):
        assert B._auto_backup_interval_hours({"schedule": "hourly"}) == 1.0
        assert B._auto_backup_interval_hours({"schedule": "daily"}) == 24.0
        assert B._auto_backup_interval_hours({"schedule": "weekly"}) == 168.0

    def test_default_is_daily(self):
        assert B._auto_backup_interval_hours({}) == 24.0

    def test_numeric_hours(self):
        assert B._auto_backup_interval_hours({"schedule": 6}) == 6.0
        assert B._auto_backup_interval_hours({"schedule": "12"}) == 12.0

    def test_numeric_floor_one_hour(self):
        assert B._auto_backup_interval_hours({"schedule": 0}) == 1.0

    def test_garbage_falls_back_to_daily(self):
        assert B._auto_backup_interval_hours({"schedule": "fortnightly"}) == 24.0
        assert B._auto_backup_interval_hours({"schedule": True}) == 24.0

    def test_enabled_parsing(self):
        assert B._auto_backup_enabled({}) is False
        assert B._auto_backup_enabled({"enabled": True}) is True
        assert B._auto_backup_enabled({"enabled": "true"}) is True
        assert B._auto_backup_enabled({"enabled": "false"}) is False

    def test_keep_floor_is_one(self):
        assert B._auto_backup_keep({"keep_last": 0}) == 1
        assert B._auto_backup_keep({"keep_last": "junk"}) == B._AUTO_DEFAULT_KEEP


# ---------------------------------------------------------------------------
# maybe_create_auto_backup
# ---------------------------------------------------------------------------

class TestMaybeCreateAutoBackup:
    def test_disabled_by_default(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {})
        assert B.maybe_create_auto_backup(hermes_home=home) is None
        assert not (home / "backups").exists()

    def test_first_run_creates_archive(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {"enabled": True, "schedule": "daily"})
        result = B.maybe_create_auto_backup(hermes_home=home)
        assert result is not None
        assert result.name.startswith("auto-")
        assert result.suffix == ".zip"
        assert zipfile.is_zipfile(result)
        with zipfile.ZipFile(result) as zf:
            assert "config.yaml" in zf.namelist()
        state = _state(home)
        assert state["last_status"] == "ok"
        assert state["last_run_at"]

    def test_active_profile_is_source_and_default_destination(self, tmp_path, monkeypatch):
        root = tmp_path / ".hermes"
        profile = root / "profiles" / "work"
        profile.mkdir(parents=True)
        (root / "root-only.txt").write_text("root", encoding="utf-8")
        (profile / "profile-only.txt").write_text("profile", encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile))
        _set_cfg(monkeypatch, {"enabled": True})

        result = B.maybe_create_auto_backup()

        assert result is not None
        assert result.parent == profile / "backups"
        with zipfile.ZipFile(result) as zf:
            names = set(zf.namelist())
        assert "profile-only.txt" in names
        assert "root-only.txt" not in names

    def test_not_due_returns_none(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {"enabled": True, "schedule": "daily"})
        now = datetime.now(timezone.utc)
        _write_state(home, {"last_run_at": (now - timedelta(hours=2)).isoformat()})
        assert B.maybe_create_auto_backup(hermes_home=home, now=now) is None

    def test_due_after_interval(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {"enabled": True, "schedule": "daily"})
        now = datetime.now(timezone.utc)
        _write_state(home, {"last_run_at": (now - timedelta(hours=25)).isoformat()})
        result = B.maybe_create_auto_backup(hermes_home=home, now=now)
        assert result is not None

    def test_hourly_schedule(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {"enabled": True, "schedule": "hourly"})
        now = datetime.now(timezone.utc)
        _write_state(home, {"last_run_at": (now - timedelta(minutes=61)).isoformat()})
        assert B.maybe_create_auto_backup(hermes_home=home, now=now) is not None

    def test_unparseable_last_run_recovers(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {"enabled": True})
        _write_state(home, {"last_run_at": "not-a-date"})
        assert B.maybe_create_auto_backup(hermes_home=home) is not None

    def test_naive_last_run_treated_as_utc(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {"enabled": True, "schedule": "daily"})
        now = datetime.now(timezone.utc)
        naive = (now - timedelta(hours=2)).replace(tzinfo=None)
        _write_state(home, {"last_run_at": naive.isoformat()})
        assert B.maybe_create_auto_backup(hermes_home=home, now=now) is None

    def test_prunes_beyond_keep_last(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {"enabled": True, "keep_last": 2})
        backups = home / "backups"
        backups.mkdir()
        for i in range(3):
            (backups / f"auto-2026-01-0{i + 1}-000000.zip").write_bytes(b"old")
        # Manual + pre-update archives in the same dir must never be touched.
        (backups / "pre-update-2026-01-01-000000.zip").write_bytes(b"keep")
        (backups / "my-manual.zip").write_bytes(b"keep")

        result = B.maybe_create_auto_backup(hermes_home=home)
        assert result is not None
        autos = sorted(p.name for p in backups.glob("auto-*.zip"))
        assert len(autos) == 2
        assert result.name in autos
        assert (backups / "pre-update-2026-01-01-000000.zip").exists()
        assert (backups / "my-manual.zip").exists()

    def test_failure_stamps_state_no_hammering(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {"enabled": True, "schedule": "daily"})
        monkeypatch.setattr(B, "_write_full_zip_backup", lambda out, root: None)
        now = datetime.now(timezone.utc)
        assert B.maybe_create_auto_backup(hermes_home=home, now=now) is None
        state = _state(home)
        assert state["last_status"] == "failed"
        # The failure stamped last_run_at, so an immediate re-poll is gated.
        monkeypatch.undo()
        _set_cfg(monkeypatch, {"enabled": True, "schedule": "daily"})
        assert B.maybe_create_auto_backup(hermes_home=home, now=now) is None

    def test_custom_dir(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        dest = tmp_path / "external-drive"
        _set_cfg(monkeypatch, {"enabled": True, "dir": str(dest)})
        result = B.maybe_create_auto_backup(hermes_home=home)
        assert result is not None
        assert result.parent == dest

    def test_custom_dir_inside_hermes_home_falls_back_without_recursion(
        self, tmp_path, monkeypatch, caplog
    ):
        home = _make_home(tmp_path)
        unsafe_dest = home / "scheduled-backups"
        _set_cfg(monkeypatch, {"enabled": True, "dir": str(unsafe_dest)})
        first_now = datetime(2026, 1, 1, tzinfo=timezone.utc)

        with caplog.at_level(logging.WARNING):
            first = B.maybe_create_auto_backup(hermes_home=home, now=first_now)
            second = B.maybe_create_auto_backup(
                hermes_home=home,
                now=first_now + timedelta(hours=25),
            )

        assert first is not None and second is not None
        assert first.parent == home / "backups"
        assert second.parent == home / "backups"
        assert not unsafe_dest.exists()
        assert "inside HERMES_HOME" in caplog.text
        with zipfile.ZipFile(second) as zf:
            assert not any(name.startswith("scheduled-backups/") for name in zf.namelist())

    def test_gateway_housekeeping_polls_auto_backup_hourly(self, monkeypatch):
        from gateway import run as gateway_run

        calls = []
        monkeypatch.setattr(B, "maybe_create_auto_backup", lambda: calls.append(True))

        class StopAfterHour(threading.Event):
            def __init__(self):
                super().__init__()
                self.waits = 0

            def is_set(self):
                return self.waits >= 60

            def wait(self, timeout=None):
                self.waits += 1
                return self.is_set()

        gateway_run._start_gateway_housekeeping(
            StopAfterHour(), adapters=None, loop=None, interval=0
        )

        assert calls == [True]

    def test_missing_home_returns_none(self, tmp_path, monkeypatch):
        _set_cfg(monkeypatch, {"enabled": True})
        assert B.maybe_create_auto_backup(hermes_home=tmp_path / "nope") is None

    def test_never_raises_on_config_error(self, tmp_path, monkeypatch):
        """A broken config load degrades to 'disabled', not an exception."""
        import hermes_cli.config as config_mod

        home = _make_home(tmp_path)

        def boom():
            raise RuntimeError("config exploded")

        monkeypatch.setattr(config_mod, "load_config", boom)
        assert B._get_backup_config() == {}
        assert B.maybe_create_auto_backup(hermes_home=home) is None

    def test_archive_restores_with_import_validation(self, tmp_path, monkeypatch):
        """The auto-backup zip passes the same validation `hermes import` uses."""
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {"enabled": True})
        result = B.maybe_create_auto_backup(hermes_home=home)
        with zipfile.ZipFile(result) as zf:
            ok, reason = B._validate_backup_zip(zf)
        assert ok, reason


# ---------------------------------------------------------------------------
# list_backup_archives / run_backup_list
# ---------------------------------------------------------------------------

class TestListArchives:
    def test_default_listing_is_scoped_to_active_profile(self, tmp_path, monkeypatch):
        root = tmp_path / ".hermes"
        profile = root / "profiles" / "work"
        root_backups = root / "backups"
        profile_backups = profile / "backups"
        root_backups.mkdir(parents=True)
        profile_backups.mkdir(parents=True)
        (root_backups / "root.zip").write_bytes(b"root")
        (profile_backups / "profile.zip").write_bytes(b"profile")
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(profile))
        _set_cfg(monkeypatch, {})

        archives = B.list_backup_archives()

        assert [archive["path"].name for archive in archives] == ["profile.zip"]

    def test_classifies_kinds(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {})
        backups = home / "backups"
        backups.mkdir()
        (backups / "auto-2026-01-01-000000.zip").write_bytes(b"a")
        (backups / "pre-update-2026-01-02-000000.zip").write_bytes(b"b")
        (backups / "pre-migration-2026-01-03-000000.zip").write_bytes(b"c")
        (backups / "hand-rolled.zip").write_bytes(b"d")
        (backups / "not-a-backup.txt").write_text("ignored", encoding="utf-8")

        archives = B.list_backup_archives(hermes_home=home)
        kinds = {a["path"].name: a["kind"] for a in archives}
        assert kinds == {
            "auto-2026-01-01-000000.zip": "auto",
            "pre-update-2026-01-02-000000.zip": "pre-update",
            "pre-migration-2026-01-03-000000.zip": "pre-migration",
            "hand-rolled.zip": "manual",
        }

    def test_includes_custom_dir(self, tmp_path, monkeypatch):
        home = _make_home(tmp_path)
        dest = tmp_path / "elsewhere"
        dest.mkdir()
        (dest / "auto-2026-01-01-000000.zip").write_bytes(b"a")
        _set_cfg(monkeypatch, {"dir": str(dest)})
        archives = B.list_backup_archives(hermes_home=home)
        assert [a["path"].parent for a in archives] == [dest]

    def test_sorted_newest_first(self, tmp_path, monkeypatch):
        import os
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {})
        backups = home / "backups"
        backups.mkdir()
        older = backups / "auto-2026-01-01-000000.zip"
        newer = backups / "auto-2026-01-02-000000.zip"
        older.write_bytes(b"a")
        newer.write_bytes(b"b")
        os.utime(older, (1000000000, 1000000000))
        os.utime(newer, (2000000000, 2000000000))
        archives = B.list_backup_archives(hermes_home=home)
        assert [a["path"].name for a in archives] == [newer.name, older.name]

    def test_run_backup_list_empty(self, tmp_path, monkeypatch, capsys):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {})
        monkeypatch.setattr(B, "get_hermes_home", lambda: home)
        B.run_backup_list(Namespace())
        out = capsys.readouterr().out
        assert "No backup archives found" in out

    def test_run_backup_list_output(self, tmp_path, monkeypatch, capsys):
        home = _make_home(tmp_path)
        _set_cfg(monkeypatch, {})
        monkeypatch.setattr(B, "get_hermes_home", lambda: home)
        backups = home / "backups"
        backups.mkdir()
        (backups / "auto-2026-01-01-000000.zip").write_bytes(b"x" * 2048)
        B.run_backup_list(Namespace())
        out = capsys.readouterr().out
        assert "auto-2026-01-01-000000.zip" in out
        assert "[auto]" in out
        assert "hermes import" in out

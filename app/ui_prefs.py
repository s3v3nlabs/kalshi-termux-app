"""Saved trading-mode choice. Lives under data/ (gitignored). No secrets."""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from app.config import settings


def prefs_path():
    return settings.data_dir / "ui_prefs.json"


def _as_bool(val: Any) -> Optional[bool]:
    if isinstance(val, bool):
        return val
    if isinstance(val, int) and val in (0, 1):
        return bool(val)
    if isinstance(val, str):
        low = val.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return None


def _read_file() -> dict:
    path = prefs_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_file(data: dict) -> None:
    settings.ensure_dirs()
    path = prefs_path()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_dry_run_override(store=None) -> Optional[bool]:
    """Return saved paper flag, or None when the user has never chosen."""
    data = _read_file()
    if "dry_run" in data:
        return _as_bool(data.get("dry_run"))
    if store is not None:
        raw = store.get_setting("dry_run")
        if raw is not None:
            return _as_bool(raw)
    return None


def save_dry_run_override(dry_run: bool, store=None) -> None:
    data = _read_file()
    data["dry_run"] = bool(dry_run)
    _write_file(data)
    if store is not None:
        store.set_setting("dry_run", "1" if dry_run else "0")


def clear_dry_run_override(store=None) -> None:
    data = _read_file()
    data.pop("dry_run", None)
    if data:
        _write_file(data)
    else:
        path = prefs_path()
        if path.is_file():
            path.unlink(missing_ok=True)
    if store is not None:
        store.delete_setting("dry_run")


def load_skip_live_confirm(store=None) -> bool:
    """True when the user opted out of the LIVE confirm sheet."""
    data = _read_file()
    if "skip_live_confirm" in data:
        val = _as_bool(data.get("skip_live_confirm"))
        if val is not None:
            return val
    if store is not None:
        raw = store.get_setting("skip_live_confirm")
        if raw is not None:
            val = _as_bool(raw)
            if val is not None:
                return val
    return False


def save_skip_live_confirm(skip: bool, store=None) -> None:
    data = _read_file()
    data["skip_live_confirm"] = bool(skip)
    _write_file(data)
    if store is not None:
        store.set_setting("skip_live_confirm", "1" if skip else "0")


def apply_saved_mode(store=None) -> None:
    """Env DRY_RUN is the boot default only when nothing has been saved."""
    override = load_dry_run_override(store)
    if override is None:
        settings.dry_run = bool(settings.env_dry_run)
        return
    settings.dry_run = override
    if store is not None:
        store.set_setting("dry_run", "1" if override else "0")
    if "dry_run" not in _read_file():
        save_dry_run_override(override, store=None)

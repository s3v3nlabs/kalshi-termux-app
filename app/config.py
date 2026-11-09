"""Runtime configuration from env + sensible Termux defaults."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

HOSTS = {
    "external": "https://external-api.kalshi.com",
    "elections": "https://api.elections.kalshi.com",
    "demo": "https://demo-api.kalshi.co",
    # Legacy alias seen in early engine scraps
    "trading": "https://trading-api.kalshi.com",
}

WS_HOSTS = {
    "external": "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
    "elections": "wss://api.elections.kalshi.com/trade-api/ws/v2",
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    "trading": "wss://trading-api.kalshi.com/trade-api/ws/v2",
}


def _bool(val: str | None, default: bool = False) -> bool:
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _float(val: str | None, default: float) -> float:
    try:
        return float(val) if val is not None else default
    except ValueError:
        return default


def _int(val: str | None, default: int) -> int:
    try:
        return int(val) if val is not None else default
    except ValueError:
        return default


class Settings:
    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    secrets_dir: Path = ROOT / "secrets"
    db_path: Path = ROOT / "data" / "kalshi_portal.db"
    vault_path: Path = ROOT / "data" / "vault.enc"

    host_key: str = os.getenv("KALSHI_HOST", "external").strip().lower()
    key_id: str = os.getenv("KALSHI_KEY_ID", "").strip()
    key_path: Path = Path(
        os.getenv("KALSHI_KEY_PATH", str(ROOT / "secrets" / "kalshi.key"))
    )
    if not key_path.is_absolute():
        key_path = ROOT / key_path

    # Boot default from the environment. A saved Settings choice overrides this.
    env_dry_run: bool = _bool(os.getenv("DRY_RUN"), False)  # LIVE by default; PAPER via top switch
    dry_run: bool = env_dry_run
    kelly_fraction: float = _float(os.getenv("KELLY_FRACTION"), 0.5)
    kelly_max_fraction: float = _float(os.getenv("KELLY_MAX_FRACTION"), 0.05)

    scan_limit: int = _int(os.getenv("SCAN_LIMIT"), 100)
    cache_ttl: int = _int(os.getenv("CACHE_TTL_SECONDS"), 10)
    max_contract_price_cents: float = _float(os.getenv("MAX_CONTRACT_PRICE_CENTS"), 40.0)
    min_payout_multiple: float = _float(os.getenv("MIN_PAYOUT_MULTIPLE"), 2.0)

    bind_host: str = os.getenv("HOST", "127.0.0.1")
    bind_port: int = _int(os.getenv("PORT"), 8787)

    @property
    def api_base(self) -> str:
        return HOSTS.get(self.host_key, HOSTS["external"])

    @property
    def ws_url(self) -> str:
        return WS_HOSTS.get(self.host_key, WS_HOSTS["external"])

    @property
    def has_keys(self) -> bool:
        return bool(self.key_id) and self.key_path.is_file()

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.secrets_dir, 0o700)
        except OSError:
            pass


settings = Settings()
settings.ensure_dirs()

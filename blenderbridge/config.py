"""Configuration and secret storage for the connector.

Layout (Windows):
    %APPDATA%\\BlenderBridge\\config.json     settings (no secrets in clear text)
    %APPDATA%\\BlenderBridge\\secrets.bin     DPAPI protected blob (per user)
    %APPDATA%\\BlenderBridge\\logs\\           audit + debug logs
    %APPDATA%\\BlenderBridge\\jobs\\           snapshots / before-after .blend files

On Linux/macOS the same structure lives under ``~/.config/blenderbridge`` (used
for development and CI; DPAPI is Windows only, so the fallback is an in-memory
key derived from a local file with 0600 permissions and a loud warning).
"""

from __future__ import annotations

import json
import os
import platform
import stat
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import security

APP_NAME = "BlenderBridge"
DEFAULT_HTTP_PORT = 8765
DEFAULT_ADDON_PORT = 9876
CONFIG_VERSION = 1


def platform_dir() -> Path:
    """Per-user directory for config, logs and job artifacts."""
    override = os.environ.get("BLENDERBRIDGE_HOME")
    if override:
        return Path(override).expanduser().resolve()
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
        return Path(base) / APP_NAME
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / APP_NAME.lower()


def app_dir() -> Path:
    """Directory that contains the running program (for the bundled cloudflared)."""
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


@dataclass
class Config:
    version: int = CONFIG_VERSION

    # networking
    http_port: int = DEFAULT_HTTP_PORT
    bind_host: str = "127.0.0.1"
    addon_host: str = "127.0.0.1"
    addon_port: int = DEFAULT_ADDON_PORT
    use_tls: bool = True

    # safety
    approval_threshold: str = "moderate"   # read | low | moderate | high | never
    approval_timeout_s: int = 300
    allow_trust_window: bool = True
    trust_window_s: int = 120
    max_job_timeout_s: float = 600.0
    allow_script_mode: bool = True
    script_requires_approval: bool = True
    rate_limit_per_min: int = 120
    allow_cors: bool = False
    allowed_hosts: List[str] = field(default_factory=list)

    # tunnel
    tunnel_enabled: bool = False
    tunnel_provider: str = "cloudflared"   # cloudflared | relay | none
    cloudflared_path: str = ""
    tunnel_require_approval: bool = True
    tunnel_public_note: str = ""

    # relay (polling mailbox via GitHub issues)
    relay_enabled: bool = False
    relay_repo: str = ""
    relay_issue: int = 0
    relay_interval_s: float = 3.0

    # bookkeeping
    audit_log: bool = True
    log_payloads: bool = True
    keep_jobs: int = 500
    snapshot_before_risky: bool = True
    snapshot_keep: int = 40
    preview_max_px: int = 900
    theme: str = "system"
    first_run_done: bool = False

    def to_public_dict(self) -> Dict[str, Any]:
        """Config for the UI/API. Never contains secrets."""
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in (data or {}).items() if k in known}
        cfg = cls()
        for k, v in clean.items():
            setattr(cfg, k, v)
        return cfg


SECRET_KEYS = (
    "addon_token",       # shared secret between connector and Blender add-on
    "ui_session_token",  # cookie/token for the local browser UI (regenerated per start)
    "relay_token",       # GitHub token used by the relay
    "relay_secret",      # shared secret used to seal relay payloads
    "agent_key_master",  # master key used to derive agent API keys
)


class ConfigStore:
    """Loads/saves config and secrets. Secrets never touch config.json."""

    def __init__(self, directory: Optional[Path] = None):
        self.dir = Path(directory) if directory else platform_dir()
        self.config_path = self.dir / "config.json"
        self.secrets_path = self.dir / "secrets.bin"
        self.config = Config()
        self._secrets: Dict[str, str] = {}
        self.secrets_encrypted = False
        self.warnings: List[str] = []

    # -- lifecycle ---------------------------------------------------------
    def load(self) -> Config:
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                self.config = Config.from_dict(raw)
            except (OSError, ValueError) as exc:
                self.warnings.append(f"config.json could not be read ({exc}); using defaults")
                self.config = Config()
        self._load_secrets()
        return self.config

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self.config), indent=2), encoding="utf-8")
        os.replace(tmp, self.config_path)
        self._save_secrets()

    # -- secrets -----------------------------------------------------------
    def _load_secrets(self) -> None:
        if not self.secrets_path.exists():
            self._secrets = {}
            return
        try:
            blob = self.secrets_path.read_bytes()
            data, encrypted = security.open_secret_blob(blob)
            self._secrets = json.loads(data.decode("utf-8")) if data else {}
            self.secrets_encrypted = encrypted
            if not encrypted:
                self.warnings.append(
                    "Secrets are stored with file permissions only (no DPAPI on this platform)."
                )
        except (OSError, ValueError) as exc:
            self.warnings.append(f"secrets file could not be read ({exc}); secrets reset")
            self._secrets = {}

    def _save_secrets(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._secrets).encode("utf-8")
        blob, encrypted = security.seal_secret_blob(payload)
        self.secrets_encrypted = encrypted
        tmp = self.secrets_path.with_suffix(".bin.tmp")
        tmp.write_bytes(blob)
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        os.replace(tmp, self.secrets_path)
        try:
            os.chmod(self.secrets_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def get_secret(self, key: str) -> str:
        return self._secrets.get(key, "")

    def set_secret(self, key: str, value: str) -> None:
        if value:
            self._secrets[key] = value
        else:
            self._secrets.pop(key, None)

    def ensure_secret(self, key: str, length: int = 32) -> str:
        value = self.get_secret(key)
        if not value:
            value = security.random_token(length)
            self.set_secret(key, value)
        return value

    # -- paths -------------------------------------------------------------
    @property
    def log_dir(self) -> Path:
        return self.dir / "logs"

    @property
    def job_dir(self) -> Path:
        return self.dir / "jobs"

    def ensure_dirs(self) -> None:
        for p in (self.dir, self.log_dir, self.job_dir):
            p.mkdir(parents=True, exist_ok=True)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass

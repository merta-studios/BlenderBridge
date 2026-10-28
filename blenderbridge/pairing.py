"""Automatic pairing between the connector and the Blender add-on.

Both programs read the same small file inside the user's config directory.  The
first one to start creates the token, the second one adopts it.  The file is
readable only by the current user, and the token can always be copied by hand in
the UI if the automatic route fails.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Optional

TOKEN_FILENAME = "addon_token.txt"


def token_path(config_dir: Path) -> Path:
    return Path(config_dir) / TOKEN_FILENAME


def load_or_create(config_dir: Path) -> str:
    """Read the shared add-on token, creating it if necessary."""
    path = token_path(config_dir)
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if len(value) >= 24:
                return value
    except OSError:
        pass
    value = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return value


def read(config_dir: Path) -> Optional[str]:
    try:
        path = token_path(config_dir)
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    except OSError:
        return None
    return None

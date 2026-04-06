from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def user_cache_root() -> Path:
    if sys.platform == "win32":
        for env_var in ("LOCALAPPDATA", "APPDATA"):
            value = os.environ.get(env_var)
            if value:
                return Path(value)
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Caches"
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache:
            return Path(xdg_cache)
        return Path.home() / ".cache"
    return Path(tempfile.gettempdir()) / "lunascope-cache"


def app_cache_root() -> Path:
    preferred = user_cache_root() / "lunascope"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "lunascope-cache"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def app_state_file(*parts: str) -> Path:
    return app_cache_root().joinpath(*parts)

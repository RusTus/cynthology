"""Small persistent key/value settings (settings.json in the state dir)."""
import json
import os

from .util import STATE_DIR

FILE = os.path.join(STATE_DIR, "settings.json")
DEFAULTS = {"setup_done": False, "https_redirect": False, "http_port": 5000, "https_port": 5001}


def get_all() -> dict:
    try:
        with open(FILE) as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def get(key: str):
    return get_all().get(key)


def update(**values) -> dict:
    s = get_all()
    s.update(values)
    tmp = FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, FILE)
    return s

"""Portal modules.

Each portal exposes a ``check(emirates_id, **kwargs)`` function that opens the
provider portal, logs in, looks up the patient, and returns:

    {
        "portal":   "almadallah",
        "status":   "ELIGIBLE" | "NOT_ELIGIBLE" | "ERROR",
        "message":  str,
        "details":  dict,
    }
"""
import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def _read_full_config() -> dict:
    """Read config from env var CONFIG_JSON (Railway/cloud) or from file."""
    raw = os.getenv("CONFIG_JSON", "").strip()
    if raw:
        return json.loads(raw)
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_config(portal_name: str) -> dict:
    return _read_full_config()["portals"][portal_name]


def format_eid(raw: str) -> str:
    """Normalise an Emirates ID to ``784-1982-4107547-6`` form."""
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 15:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:14]}-{digits[14]}"
    return raw


def headless() -> bool:
    """Whether portal browsers should run hidden.

    Set ``TAMER_HEADLESS=1`` for the web UI (hidden), leave unset for the CLI
    and explore.py (visible — easier to debug).
    """
    return os.getenv("TAMER_HEADLESS", "0") == "1"

"""Minimal structured audit events without credentials or target query values."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _audit_path() -> Path:
    configured = os.environ.get("TESTPILOT_AUDIT_LOG_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "data" / "audit.log"


def write_event(event: dict[str, Any]) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    path = _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")

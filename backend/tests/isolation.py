"""Keep tests that persist reports away from the developer's local database."""

import os
import tempfile

_directory: tempfile.TemporaryDirectory | None = None
_previous: str | None = None


def use_temporary_database() -> None:
    global _directory, _previous
    _directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    _previous = os.environ.get("TESTPILOT_DB_PATH")
    os.environ["TESTPILOT_DB_PATH"] = os.path.join(_directory.name, "runs.db")


def restore_database() -> None:
    global _directory
    if _previous is None:
        os.environ.pop("TESTPILOT_DB_PATH", None)
    else:
        os.environ["TESTPILOT_DB_PATH"] = _previous
    if _directory is not None:
        _directory.cleanup()
        _directory = None


_alert_patch = None


def isolate_alerts() -> None:
    """Send alerts nowhere real: a temporary feed, and no Zapier hook or email key from the developer's .env."""
    global _alert_patch
    from unittest.mock import patch

    folder = tempfile.mkdtemp()
    _alert_patch = patch.dict(os.environ, {"TESTPILOT_ALERTS_DIR": folder, "TESTPILOT_ZAPIER_WEBHOOK_URL": "",
                                           "TESTPILOT_BREVO_API_KEY": ""})
    _alert_patch.start()


def restore_alerts() -> None:
    if _alert_patch is not None:
        _alert_patch.stop()

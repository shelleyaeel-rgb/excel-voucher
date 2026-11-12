"""Bounded local failure metadata; never persist payloads or error messages."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

from . import __version__
from .ai_settings import _atomic_write
from .wps_service import safe_wps_diagnostic

_LOCK = threading.RLock()
_ACTIONS = {"wps_inspect", "wps_read_table", "wps_prepare_update", "wps_apply_update", "wps_check_task"}
MAX_RECORDS = 100
MAX_BYTES = 1024 * 1024


def record_failure(path, *, action, code, diagnostic=None, mutation_attempted=False, document_url=None):
    """Log an allowlisted record; logging failure never hides the original error."""
    if not isinstance(action, str) or action not in _ACTIONS:
        return None
    record = dict(id=uuid.uuid4().hex, time=datetime.now(timezone.utc).isoformat(),
                  app_version=__version__, action=action,
                  code=code if isinstance(code, str) and re.fullmatch(r"[A-Z_]{1,60}", code) else "WPS_ERROR",
                  mutation_attempted=mutation_attempted is True,
                  diagnostic=safe_wps_diagnostic(diagnostic))
    if isinstance(document_url, str) and document_url:
        record["document_ref"] = hashlib.sha256(document_url.encode("utf8")).hexdigest()[:16]
    try:
        target = Path(path)
        with _LOCK:
            for candidate in (target, *target.parents):
                if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                    return None
            previous = []
            if target.exists():
                if target.stat().st_size > MAX_BYTES:
                    return None
                saved = json.loads(target.read_text(encoding="utf8"))
                if not isinstance(saved, dict) or saved.get("schema") != 1 or not isinstance(saved.get("records"), list):
                    return None
                previous = saved["records"][-(MAX_RECORDS - 1):]
            _atomic_write(target, {"schema": 1, "records": previous + [record]})
        return record["id"]
    except (OSError, ValueError, TypeError, RecursionError):
        return None

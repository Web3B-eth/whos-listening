"""Which Venice models are private and which are anonymized.

Venice runs some models itself with no data kept ("private") and passes others to the company that
made them, with your identity stripped but the content visible ("anonymized": Claude, Gemini and
others). Venice publishes this for every model at a public address that needs no key:
``GET https://api.venice.ai/api/v1/models?type=all`` (``model_spec.privacy``).

This module keeps that list on the gateway (``venice-models.json`` in the whos-listening folder),
refreshes it in the background at most once a day, and answers ``privacy(model)``. The request
sends nothing about the user. Nothing here imports Hermes.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

MODELS_URL = "https://api.venice.ai/api/v1/models?type=all"
CACHE_FILE = "venice-models.json"
MAX_AGE = 24 * 3600          # refresh the list once a day
RETRY_AFTER = 3600           # after a failed refresh, wait an hour before trying again
MAX_BYTES = 4_000_000
PRIVATE, ANONYMIZED = "private", "anonymized"

_lock = threading.Lock()
_state: dict[str, Any] = {"path": None, "models": None, "fetched_at": 0.0, "tried_at": 0.0, "running": False}


def model_id(model: str) -> str:
    """Venice's id for a model name as Hermes reports it: "venice/grok-4-5" → "grok-4-5"."""
    return str(model or "").strip().lower().rsplit("/", 1)[-1]


def parse(raw: bytes) -> dict[str, str]:
    """``{model id: "private" | "anonymized"}`` from Venice's model list. Unknown labels are left out."""
    data = json.loads(raw.decode("utf-8"))
    out: dict[str, str] = {}
    for m in (data.get("data") if isinstance(data, Mapping) else None) or []:
        if not isinstance(m, Mapping):
            continue
        spec = m.get("model_spec") if isinstance(m.get("model_spec"), Mapping) else {}
        label = str(spec.get("privacy") or "").strip().lower()
        mid = model_id(str(m.get("id") or ""))
        if mid and label in (PRIVATE, ANONYMIZED):
            out[mid] = label
    return out


def configure(data_dir: Path) -> None:
    """Where the list is kept. Loads what's there; call refresh_if_stale() to update it."""
    path = Path(data_dir) / CACHE_FILE
    with _lock:
        if _state["path"] == path and _state["models"] is not None:
            return
        _state["path"] = path
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            models = doc.get("models")
            if isinstance(models, Mapping):
                _state["models"] = {str(k): str(v) for k, v in models.items() if v in (PRIVATE, ANONYMIZED)}
                _state["fetched_at"] = float(doc.get("fetched_at") or 0)
        except (OSError, ValueError, TypeError):
            pass


def loaded() -> bool:
    """Whether Venice's list is here yet (a fresh gateway fetches it in the background)."""
    with _lock:
        return bool(_state["models"])


def privacy(model: str) -> Optional[str]:
    """"private", "anonymized", or None when the model isn't on the list or there is no list yet."""
    with _lock:
        models = _state["models"]
    return models.get(model_id(model)) if models else None


def refresh(opener: Callable[..., Any] = urllib.request.urlopen, timeout: float = 10.0) -> bool:
    """Fetch the list now and keep it. Returns whether it worked. Never raises."""
    with _lock:
        path = _state["path"]
        _state["tried_at"] = time.time()
    try:
        req = urllib.request.Request(MODELS_URL, headers={"User-Agent": "whos-listening", "Accept": "application/json"})
        with opener(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return False
        models = parse(raw)
        if not models:
            return False
    except Exception:
        return False
    now = time.time()
    with _lock:
        _state["models"], _state["fetched_at"] = models, now
    if path is not None:
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(path).with_name(f".{CACHE_FILE}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps({"fetched_at": now, "models": models}, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
    return True


def refresh_if_stale(start: Callable[[Callable[[], Any]], Any] = None) -> bool:
    """Start a background refresh if the list is missing or older than a day (and no refresh
    was tried in the last hour). ``start`` runs a function in the background. Returns whether
    one started."""
    now = time.time()
    with _lock:
        fresh = _state["models"] is not None and now - _state["fetched_at"] < MAX_AGE
        if fresh or _state["running"] or now - _state["tried_at"] < RETRY_AFTER:
            return False
        _state["running"] = True

    def run() -> None:
        try:
            refresh()
        finally:
            with _lock:
                _state["running"] = False

    try:
        (start or (lambda fn: threading.Thread(target=fn, name="whos-listening-venice", daemon=True).start()))(run)
        return True
    except Exception:
        with _lock:
            _state["running"] = False
        return False


def _reset_for_tests(models: Optional[dict] = None) -> None:
    with _lock:
        _state.update(path=None, models=models, fetched_at=time.time() if models else 0.0, tried_at=0.0, running=False)

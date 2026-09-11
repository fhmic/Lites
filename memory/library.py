#library.py
"""
Saved command/prompt templates — the "Library" tab on the Remote
Dashboard. A small, reusable set of phrases Felix fires off often
("build me a landing page", "summarize today's opportunities", etc.)
without retyping them every time from the phone or the dashboard.

Persisted to memory/library.json:
    {"items": [{"id": "...", "name": "...", "text": "...", "created": "..."}]}
"""
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR     = _get_base_dir()
LIBRARY_PATH = BASE_DIR / "memory" / "library.json"
_lock        = Lock()
MAX_ITEMS    = 200


def _load() -> list:
    if not LIBRARY_PATH.exists():
        return []
    with _lock:
        try:
            data  = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
            items = data.get("items", []) if isinstance(data, dict) else []
            return items if isinstance(items, list) else []
        except Exception as e:
            print(f"[Library] ⚠️ Load error: {e}")
            return []


def _save(items: list) -> None:
    with _lock:
        LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIBRARY_PATH.write_text(
            json.dumps({"items": items[-MAX_ITEMS:]}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def list_items() -> list:
    return _load()


def add_item(name: str, text: str):
    name = (name or "").strip()
    text = (text or "").strip()
    if not name or not text:
        return None
    items = _load()
    entry = {
        "id":      uuid.uuid4().hex[:10],
        "name":    name[:80],
        "text":    text[:2000],
        "created": datetime.now().strftime("%Y-%m-%d"),
    }
    items.append(entry)
    _save(items)
    return entry


def delete_item(item_id: str) -> bool:
    items = _load()
    kept  = [i for i in items if i.get("id") != item_id]
    if len(kept) == len(items):
        return False
    _save(kept)
    return True


def get_item(item_id: str):
    for i in _load():
        if i.get("id") == item_id:
            return i
    return None

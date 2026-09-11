#project_registry.py
"""
Named registry of project *working directories* — separate from
long_term.json's memory["projects"] category (memory_manager.remember /
format_memory_for_prompt), which holds free-text notes/goals the AI
remembers about a project, not a filesystem path.

This one exists so:
  - The Remote Dashboard's "Projects" tab has something concrete to list
    and switch between (a name -> absolute path mapping).
  - actions/code_agent.py can resolve a scope="external" task against an
    "active" project without Felix having to repeat the full path on
    every single request.

Persisted to memory/projects.json:
    {
      "active": "acme-website" | null,
      "projects": {
        "acme-website": {"path": "C:\\Users\\felix\\dev\\acme", "added": "2026-09-08"}
      }
    }
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from threading import Lock


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR      = _get_base_dir()
REGISTRY_PATH = BASE_DIR / "memory" / "projects.json"
_lock         = Lock()


def _empty() -> dict:
    return {"active": None, "projects": {}}


def _load() -> dict:
    if not REGISTRY_PATH.exists():
        return _empty()
    with _lock:
        try:
            data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _empty()
            data.setdefault("active", None)
            data.setdefault("projects", {})
            if not isinstance(data["projects"], dict):
                data["projects"] = {}
            return data
        except Exception as e:
            print(f"[ProjectRegistry] ⚠️ Load error: {e}")
            return _empty()


def _save(data: dict) -> None:
    with _lock:
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def list_projects() -> dict:
    """{name: {"path": ..., "added": ..., "active": bool}}"""
    data   = _load()
    active = data.get("active")
    return {
        name: {**meta, "active": name == active}
        for name, meta in data.get("projects", {}).items()
    }


def add_project(name: str, path: str) -> str:
    name = (name or "").strip()
    path = (path or "").strip()
    if not name or not path:
        return "Both a project name and a path are required, sir."
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        return f"{resolved} doesn't exist or isn't a folder, sir."
    data = _load()
    data["projects"][name] = {
        "path":  str(resolved),
        "added": datetime.now().strftime("%Y-%m-%d"),
    }
    if data.get("active") is None:
        data["active"] = name
    _save(data)
    return f"Project '{name}' added ({resolved})."


def remove_project(name: str) -> str:
    data = _load()
    if name not in data.get("projects", {}):
        return f"No project named '{name}', sir."
    del data["projects"][name]
    if data.get("active") == name:
        data["active"] = next(iter(data["projects"]), None)
    _save(data)
    return f"Project '{name}' removed."


def set_active(name: str) -> str:
    data = _load()
    if name not in data.get("projects", {}):
        return f"No project named '{name}', sir."
    data["active"] = name
    _save(data)
    return f"Active project set to '{name}'."


def get_active():
    """Returns (name, path) for the active project, or None."""
    data   = _load()
    active = data.get("active")
    if not active or active not in data.get("projects", {}):
        return None
    return active, data["projects"][active]["path"]

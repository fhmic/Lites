#step_bus.py
"""
Lightweight event bus for live step-by-step progress of long-running,
multi-phase background tasks — currently wired to actions/code_agent.py's
Checkpoint -> Preflight -> Delegate -> Verify -> Rollback/Commit pipeline —
so the Remote Dashboard's "Steps" tab can show what's happening in near
real time instead of only the final result.

Runs entirely in-process — no network calls, no extra dependency.
code_agent (and anything else that wants a live step feed) calls
emit_step()/start_run()/end_run() from whatever thread it's running on.
That matters here specifically because code_agent is dispatched via
`loop.run_in_executor(...)` in main.py — i.e. a worker thread, NOT the
asyncio event loop thread — so this module bridges back onto the loop
with asyncio.run_coroutine_threadsafe() so DashboardServer.broadcast()
(an async method) is still awaited correctly instead of raising
"no running event loop" or silently doing nothing.

register(dashboard, loop) is called once, from main.py, right after the
DashboardServer is constructed. Until that happens (dashboard disabled,
or not started yet), emit_step()/start_run()/end_run() still update the
in-memory ring buffer — readable any time via snapshot() / the
dashboard's GET /api/steps — they just skip the live broadcast.
"""
import asyncio
import time
import uuid
from threading import Lock

_lock      = Lock()
_dashboard = None
_loop      = None

_RUN_MAX   = 20    # keep the most recent N runs
_STEP_MAX  = 200   # keep the most recent N steps per run

_runs: dict = {}        # run_id -> {"task": str, "started": float, "steps": [...], "done": bool, "status": str}
_run_order: list = []   # run_ids, oldest first


def register(dashboard, loop) -> None:
    """Wire the bus to a live DashboardServer + its running event loop.
    Safe to call again later (e.g. dashboard restarted) — just replaces
    the previous target."""
    global _dashboard, _loop
    _dashboard = dashboard
    _loop      = loop


def start_run(task: str) -> str:
    """Begin tracking a new multi-step run. Returns a run_id — pass it to
    emit_step()/end_run() for the rest of that run's life."""
    run_id = uuid.uuid4().hex[:12]
    with _lock:
        _runs[run_id] = {
            "task":    (task or "")[:200],
            "started": time.time(),
            "steps":   [],
            "done":    False,
            "status":  "running",
        }
        _run_order.append(run_id)
        while len(_run_order) > _RUN_MAX:
            old = _run_order.pop(0)
            _runs.pop(old, None)
    _broadcast({"type": "step", "event": "start", "run_id": run_id, "task": (task or "")[:200]})
    return run_id


def emit_step(run_id: str, label: str, status: str = "running") -> None:
    """Record one step of an in-progress run. `status` is purely
    descriptive ('running' | 'ok' | 'error') — the dashboard UI colors
    the entry by it, nothing here interprets it."""
    if not run_id:
        return
    entry = {"label": (label or "")[:300], "status": status, "ts": time.time()}
    with _lock:
        run = _runs.get(run_id)
        if run is None:
            return
        run["steps"].append(entry)
        run["steps"] = run["steps"][-_STEP_MAX:]
    _broadcast({"type": "step", "event": "step", "run_id": run_id, **entry})


def end_run(run_id: str, status: str = "ok") -> None:
    """Mark a run finished. status: 'ok' | 'error'."""
    if not run_id:
        return
    with _lock:
        run = _runs.get(run_id)
        if run is None:
            return
        run["done"]   = True
        run["status"] = status
    _broadcast({"type": "step", "event": "end", "run_id": run_id, "status": status})


def snapshot() -> list:
    """Everything the dashboard's GET /api/steps needs — most recent run
    first, each with its full step list."""
    with _lock:
        out = []
        for run_id in reversed(_run_order):
            run = _runs.get(run_id)
            if run:
                out.append({"run_id": run_id, **run})
        return out


def _broadcast(msg: dict) -> None:
    if _dashboard is None or _loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_dashboard.broadcast(msg), _loop)
    except Exception as e:
        print(f"[StepBus] broadcast failed: {e}")

#usage_tracker.py
"""
Per-provider call/usage tracking for core/ai_client.py's fallback chain
(Gemini -> Claude -> Groq -> custom/local), so the Remote Dashboard's
"Usage" tab can show which providers are actually carrying the load and
roughly how much text is moving through them.

Not billing-accurate token counts — no provider's exact tokenizer is
used here, just a fast, dependency-free proxy: prompt/response
character counts. Good enough to answer "am I about to blow through my
Gemini quota" without adding a tokenizer dependency to every call site.

Persisted to memory/usage.json:
    {
      "totals": {
        "gemini": {"calls": 12, "chars_in": 4300, "chars_out": 9100, "last": "2026-09-08T14:02:11"}
      },
      "daily": {
        "2026-09-08": {"gemini": {"calls": 3, "chars_in": 900, "chars_out": 2100}}
      }
    }
`daily` is capped to the most recent _DAILY_MAX days so this file
doesn't grow forever.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR   = _get_base_dir()
USAGE_PATH = BASE_DIR / "memory" / "usage.json"
_lock      = Lock()
_DAILY_MAX = 90   # ~3 months of daily rollups


def _empty() -> dict:
    return {"totals": {}, "daily": {}}


def _load() -> dict:
    if not USAGE_PATH.exists():
        return _empty()
    with _lock:
        try:
            data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _empty()
            data.setdefault("totals", {})
            data.setdefault("daily", {})
            return data
        except Exception as e:
            print(f"[UsageTracker] ⚠️ Load error: {e}")
            return _empty()


def _save(data: dict) -> None:
    with _lock:
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        USAGE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def record(provider: str, prompt: str, response_text: str) -> None:
    """Call once per successful AI provider response. Never raises —
    a usage-tracking hiccup should never take down the actual call it's
    recording."""
    try:
        if not provider:
            return
        now   = datetime.now()
        today = now.strftime("%Y-%m-%d")
        data  = _load()

        tot = data["totals"].setdefault(provider, {"calls": 0, "chars_in": 0, "chars_out": 0})
        tot["calls"]     = tot.get("calls", 0) + 1
        tot["chars_in"]  = tot.get("chars_in", 0) + len(prompt or "")
        tot["chars_out"] = tot.get("chars_out", 0) + len(response_text or "")
        tot["last"] = now.isoformat()

        day = data["daily"].setdefault(today, {})
        d   = day.setdefault(provider, {"calls": 0, "chars_in": 0, "chars_out": 0})
        d["calls"]     = d.get("calls", 0) + 1
        d["chars_in"]  = d.get("chars_in", 0) + len(prompt or "")
        d["chars_out"] = d.get("chars_out", 0) + len(response_text or "")

        if len(data["daily"]) > _DAILY_MAX:
            for old_day in sorted(data["daily"])[:-_DAILY_MAX]:
                del data["daily"][old_day]

        _save(data)
    except Exception as e:
        print(f"[UsageTracker] ⚠️ record() failed: {e}")


def summary(days: int = 7) -> dict:
    """All-time totals per provider, plus a per-day breakdown for the
    last `days` days (today included, most recent first)."""
    data   = _load()
    today  = datetime.now().date()
    window = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    recent = {d: data["daily"].get(d, {}) for d in window}
    return {"totals": data["totals"], "recent": recent}

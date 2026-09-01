#data_analytics_agent.py
"""
Data Analytics Agent — reports to LITE (the Executive Orchestrator). Third
specialist sub-agent, following the same pattern as automation_coding_agent.py
and rics_agent.py: same call signature, mode-inference front door, same
"[Agent Name] ..." report prefix.

Two distinct data sources, both covered from day one rather than bolting the
second on later:
  1. Business data already in Supabase (GAS's earnings/run tables + RICS's
     CRM tables) — answered via FIXED, parameterized GAS routes
     (/analytics/*), never open SQL. No query this agent can construct lets
     a caller run arbitrary SQL against production data — every shape is
     hardcoded server-side; only date-range/stage filters are caller-
     controlled.
  2. Ad-hoc files the user hands it directly (CSV/Excel) — delegated
     straight to actions/file_processor.py's existing pandas-based
     analyze/stats/info handling. Not duplicated here.

Deliberately NOT included yet: charting/visualization. That needs a new
dependency (matplotlib isn't in requirements.txt) and a rendering path in
LITE's content panel that doesn't exist — a clean follow-up once the three
capabilities below are proven out, not bundled in half-built.

Capabilities (parameters["action"], inferred from a plain-language task if
not given — see _infer_action):
    pipeline_summary     - deal counts + total value by stage
    earnings_trend       - affiliate earnings by network over a window
    interaction_volume   - CRM touchpoint counts by type over a window
    tasks_overview        - open / overdue / done task counts
    analyze_file          - CSV/Excel analysis, delegates to file_processor.py
    report                 - narrative summary combining the four queries above
"""
from actions.affiliate_growth_agent import _worker_request
from actions.file_processor import file_processor
import re

AGENT_NAME = "Data Analytics Agent"


def _report(body: str) -> str:
    return f"[{AGENT_NAME}] {body}"


def _infer_action(p: dict) -> str:
    """Explicit action always wins. Otherwise a light keyword pass — LITE
    should be able to hand this agent a plain-language question."""
    action = (p.get("action") or "").strip().lower()
    if action:
        return action

    if p.get("file_path"):
        return "analyze_file"

    text = (p.get("description") or p.get("query") or "").lower()
    if any(w in text for w in ("pipeline", "deals", "stage")):
        return "pipeline_summary"
    if re.search(r"\bearn(ed|ing|ings|s)?\b", text) or any(w in text for w in ("revenue", "affiliate", "income")):
        return "earnings_trend"
    if any(w in text for w in ("interaction", "touchpoint", "calls", "contacted")):
        return "interaction_volume"
    if any(w in text for w in ("task", "follow-up", "follow up", "overdue")):
        return "tasks_overview"
    if any(w in text for w in ("how's business", "how is business", "overview",
                                  "summary", "snapshot", "how are we doing")):
        return "report"
    return "report"  # safest broad default — read-only, gives the fullest picture


def _analytics_get(path: str, **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    full_path = f"{path}?{query}" if query else path
    return _worker_request("GET", full_path)


# ── Capabilities ─────────────────────────────────────────────────────────

def _do_pipeline_summary(p: dict) -> str:
    result = _analytics_get("/analytics/pipeline-summary")
    pipeline = result.get("pipeline", {})
    if not pipeline:
        return "No deals in the pipeline yet."
    lines = [f"{result.get('total_deals', 0)} deal(s) total:"]
    for stage, stats in pipeline.items():
        lines.append(f"  • {stage}: {stats['count']} deal(s), value {stats['total_value']:.2f}")
    return "\n".join(lines)


def _do_earnings_trend(p: dict) -> str:
    days = int(p.get("days") or 30)
    result = _analytics_get("/analytics/earnings-trend", days=days)
    by_network = result.get("earnings_by_network", {})
    if not by_network:
        return f"No earnings data in the last {days} days."
    lines = [f"Earnings, last {days} days ({result.get('snapshot_count', 0)} snapshot(s)):"]
    total = 0.0
    for network, amount in by_network.items():
        lines.append(f"  • {network}: {amount:.2f}")
        total += amount
    lines.append(f"  Total: {total:.2f}")
    return "\n".join(lines)


def _do_interaction_volume(p: dict) -> str:
    days = int(p.get("days") or 30)
    result = _analytics_get("/analytics/interaction-volume", days=days)
    by_type = result.get("interactions_by_type", {})
    if not by_type:
        return f"No CRM activity in the last {days} days."
    lines = [f"CRM activity, last {days} days ({result.get('total', 0)} total):"]
    for itype, count in by_type.items():
        lines.append(f"  • {itype}: {count}")
    return "\n".join(lines)


def _do_tasks_overview(p: dict) -> str:
    result = _analytics_get("/analytics/tasks-overview")
    open_n, overdue, done = result.get("open", 0), result.get("overdue", 0), result.get("done", 0)
    flag = f" ⚠ {overdue} overdue" if overdue else ""
    return f"{open_n} open task(s){flag}, {done} completed."


def _do_analyze_file(p: dict, player=None, speak=None) -> str:
    if not p.get("file_path"):
        return "Which file should I analyze?"
    fp_params = {**p, "action": p.get("file_action") or "analyze"}
    return file_processor(parameters=fp_params, player=player, speak=speak)


def _do_report(p: dict) -> str:
    """Narrative summary combining all four business-data queries into one
    'how's the business doing' answer — the natural default when the ask
    is broad rather than pointed at one specific metric."""
    sections = [
        _do_pipeline_summary(p),
        _do_earnings_trend({**p, "days": p.get("days") or 7}),
        _do_interaction_volume({**p, "days": p.get("days") or 7}),
        _do_tasks_overview(p),
    ]
    return "\n\n".join(sections)


_ACTIONS = {
    "pipeline_summary":    _do_pipeline_summary,
    "earnings_trend":       _do_earnings_trend,
    "interaction_volume":   _do_interaction_volume,
    "tasks_overview":       _do_tasks_overview,
    "report":               _do_report,
}


def data_analytics_agent(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Single entry point LITE delegates any business-data question or file-
    analysis task to. Routes internally (see _infer_action) so LITE doesn't
    need to pre-decide the exact query."""
    p = parameters or {}
    action = _infer_action(p)

    if action == "analyze_file":
        return _report(_do_analyze_file(p, player=player, speak=speak))

    handler = _ACTIONS.get(action)
    if not handler:
        return _report(f"Unknown action '{action}'. Try: {', '.join(_ACTIONS)}, analyze_file.")

    try:
        return _report(handler(p))
    except Exception as e:
        print(f"[{AGENT_NAME}] Error in '{action}': {e}")
        return _report(f"Hit an error: {e}")

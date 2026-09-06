#conversation_log.py
"""
A durable, append-as-it-happens conversation transcript — separate from
long_term.json's session summaries, which are a 1-2 sentence, 3-entry-max
scratchpad that gets popped/consumed for LITE's own next-session context,
not a real history.

If an Obsidian vault is configured (config/api_keys.json ->
obsidian_vault_path), actions/obsidian.py has actually been journaling
full conversations to a daily note there all along via
append_to_daily_note() — so that history already existed for anyone with
a vault set up; recall() below reads it as a fallback for any date this
module doesn't have its own log for (see _from_obsidian), rather than
treating pre-this-feature days as unreachable.

What this module actually adds on top of that:
  - Works with NO vault configured at all — Obsidian journaling is
    optional; this isn't.
  - Real-time, per-turn writes instead of one batch at clean session end
    — survives a crash, force-quit, or dropped connection mid-conversation,
    none of which currently reach the Obsidian journal or the
    long_term.json summary (both only fire in the `finally` block once a
    session ends normally).
  - A single voice-facing search across both sources (recall_conversation),
    where previously the vault could only be searched via Obsidian's own
    search_notes function, not asked about directly in conversation.

One plain-text file per day, kept indefinitely, at
memory/conversation_logs/YYYY-MM-DD.log.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR   = _get_base_dir()
LOG_DIR    = BASE_DIR / "memory" / "conversation_logs"
_lock      = Lock()


def _path_for(date: datetime) -> Path:
    return LOG_DIR / f"{date.strftime('%Y-%m-%d')}.log"


def append_turn(line: str) -> None:
    """Appends one already-formatted line ('You: ...' / 'LITE: ...') to
    today's log, timestamped. Never lets a disk hiccup interrupt the live
    conversation — failures are printed, not raised."""
    try:
        with _lock:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = _path_for(datetime.now())
            stamp = datetime.now().strftime("%H:%M:%S")
            with path.open("a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {line}\n")
    except Exception as e:
        print(f"[ConversationLog] Failed to append: {e}")


def _resolve_dates(when: str, days_back: int) -> list[datetime]:
    when = (when or "").strip().lower()
    today = datetime.now()
    if when in ("today", ""):
        return [today]
    if when == "yesterday":
        return [today - timedelta(days=1)]
    if when in ("this week", "week", "last 7 days"):
        return [today - timedelta(days=i) for i in range(7)]
    # explicit YYYY-MM-DD
    try:
        return [datetime.strptime(when, "%Y-%m-%d")]
    except ValueError:
        pass
    return [today - timedelta(days=i) for i in range(max(1, days_back))]


def _from_obsidian(date: datetime, query: str) -> list[str]:
    """Falls back to the Obsidian daily journal for a date this plain-text
    log has nothing for — covers every day before this feature existed,
    since append_to_daily_note() has been journaling full conversations
    there all along (see actions/obsidian.py). Only reached when the new
    log file for that date doesn't exist, so this never duplicates a day
    that's already covered by the faster, more granular plain-text log."""
    try:
        from actions.obsidian import has_vault_configured, read_daily_note
    except Exception:
        return []
    if not has_vault_configured():
        return []
    text = read_daily_note(date.strftime("%Y-%m-%d"))
    if text.startswith("No journal entry") or text.startswith("No Obsidian vault"):
        return []
    day_label = date.strftime("%Y-%m-%d")
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not query or query in line.lower():
            out.append(f"{day_label} [journal] {line}")
    return out


def recall(query: str = "", when: str = "", days_back: int = 1, max_lines: int = 40) -> str:
    """Searches the persisted daily logs, falling back to the Obsidian
    journal (if configured) for any date this log doesn't have — so dates
    from before this feature existed are still reachable, not just a dead
    end. query filters to lines containing that text (case-insensitive)
    across every matched day; without a query, returns the most recent
    lines from the matched day(s) instead. Returns a plain-text block,
    newest first, capped at max_lines."""
    dates = _resolve_dates(when, days_back)
    query = (query or "").strip().lower()
    matches: list[str] = []

    for date in dates:
        path = _path_for(date)
        day_label = date.strftime("%Y-%m-%d")
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines:
                if not query or query in line.lower():
                    matches.append(f"{day_label} {line}")
        else:
            matches.extend(_from_obsidian(date, query))

    if not matches:
        scope = when or (f"the last {days_back} day(s)" if days_back > 1 else "today")
        needle = f" matching '{query}'" if query else ""
        return f"Nothing found{needle} in {scope}."

    matches = matches[-max_lines:]
    return "\n".join(matches)

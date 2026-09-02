#scheduling_docs_agent.py
"""
Scheduling, Document & Presentation Agent — reports to LITE (the Executive
Orchestrator). Eighth and final specialist sub-agent from the original
9-agent plan (now 6 after the two confirmed merges).

Mostly wiring existing capabilities into agent form, per the original
blueprint's own scoping note — schedule_reminder is a thin delegate to
actions/reminder.py's already-working, cross-platform reminder engine.
create_document/create_presentation are genuinely new: an LLM generates
the OUTLINE (a legitimate language task — what the content should say),
then real code (python-docx / python-pptx) deterministically renders that
outline into an actual file — the same "LLM produces content, code
produces the artifact" split used for FTA's statement extraction, just
applied to document structure instead of financial figures.

Generated files are saved to a dedicated folder under the user's home
directory (~/Documents/LITE Generated/), not the vault (that's for notes,
not deliverables) and not the git repo (lesson learned from the earlier
Business Opportunities folder ending up inside LITE-v2's working tree).

Capabilities:
    schedule_reminder     - delegates to actions/reminder.py, unchanged
    create_document        - generates a .docx from a description/outline
    create_presentation    - generates a .pptx from a description/outline
"""
import json
import sys
import time
import threading
import uuid
from pathlib import Path

AGENT_NAME = "Scheduling & Docs Agent"

OUTPUT_DIR = Path.home() / "Documents" / "LITE Generated"


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR             = _get_base_dir()
PENDING_EVENTS_PATH   = BASE_DIR / "memory" / "pending_calendar_events.json"
GMAIL_SCAN_STATE_PATH = BASE_DIR / "memory" / "gmail_scan_state.json"
DEFAULT_SCAN_TIMES = [(10, 0), (16, 0)]  # 10am and 4pm, local time, every day


def _report(body: str) -> str:
    return f"[{AGENT_NAME}] {body}"


def _parse_json_block(text: str) -> dict | None:
    """Same tolerant JSON extraction used throughout this build (web_search,
    fta_agent) — strips code fences, finds outermost braces, never raises."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start:end + 1])
    except Exception:
        return None


def _safe_filename(title: str) -> str:
    keep = "".join(c if c.isalnum() or c in " -_" else "" for c in title).strip()
    return (keep or "Untitled")[:80]


def _do_schedule_reminder(p: dict, player=None, speak=None) -> str:
    from actions.reminder import reminder
    return reminder(parameters=p, player=player)


def _generate_outline(description: str, provided_outline, kind: str) -> dict | None:
    """If the caller already supplied a structured outline, use it as-is —
    no LLM call needed. Otherwise generate one from the description. `kind`
    is 'document' or 'presentation', which shapes both the prompt and the
    expected JSON structure."""
    if isinstance(provided_outline, dict) and provided_outline:
        return provided_outline
    if not description:
        return None

    from core.ai_client import generate_content

    if kind == "presentation":
        schema = (
            '{"title": "...", "slides": [{"title": "...", "bullets": ["...", "..."]}]}'
        )
        shape_hint = "4-8 slides, 3-5 short bullets each"
    else:
        schema = (
            '{"title": "...", "sections": [{"heading": "...", "content": "..."}]}'
        )
        shape_hint = "clear sections with a heading and a few paragraphs of content each"

    prompt = (
        f"Create a {kind} outline for: {description}\n\n"
        f"Structure: {shape_hint}. Respond with ONLY this JSON, no other text, no "
        f"markdown fences:\n{schema}"
    )
    try:
        resp = generate_content(prompt)
        return _parse_json_block(resp.text if resp else "")
    except Exception as e:
        print(f"[{AGENT_NAME}] Outline generation failed: {e}")
        return None


def _do_create_document(p: dict) -> str:
    try:
        import docx
    except ImportError:
        return "python-docx is not installed. Run: pip install python-docx"

    outline = _generate_outline(p.get("description", ""), p.get("outline"), kind="document")
    if not outline or not outline.get("sections"):
        return "I need either a description to generate content from, or a structured outline with sections."

    doc = docx.Document()
    doc.add_heading(outline.get("title", "Untitled Document"), level=0)
    for section in outline["sections"]:
        heading = section.get("heading", "").strip()
        content = section.get("content", "").strip()
        if heading:
            doc.add_heading(heading, level=1)
        if content:
            doc.add_paragraph(content)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{_safe_filename(outline.get('title', 'Untitled Document'))}.docx"
    try:
        doc.save(str(out_path))
    except Exception as e:
        return f"Generated the content but couldn't save the file — {e}"
    return f"Document created: {out_path}"


def _do_create_presentation(p: dict) -> str:
    try:
        from pptx import Presentation
    except ImportError:
        return "python-pptx is not installed. Run: pip install python-pptx"

    outline = _generate_outline(p.get("description", ""), p.get("outline"), kind="presentation")
    if not outline or not outline.get("slides"):
        return "I need either a description to generate content from, or a structured outline with slides."

    prs = Presentation()
    title_layout = prs.slide_layouts[0]
    bullet_layout = prs.slide_layouts[1]

    title_slide = prs.slides.add_slide(title_layout)
    title_slide.shapes.title.text = outline.get("title", "Untitled Presentation")

    for slide_data in outline["slides"]:
        slide = prs.slides.add_slide(bullet_layout)
        slide.shapes.title.text = slide_data.get("title", "")
        body = slide.placeholders[1].text_frame
        bullets = slide_data.get("bullets", [])
        if bullets:
            body.text = bullets[0]
            for bullet in bullets[1:]:
                p_new = body.add_paragraph()
                p_new.text = bullet

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"{_safe_filename(outline.get('title', 'Untitled Presentation'))}.pptx"
    try:
        prs.save(str(out_path))
    except Exception as e:
        return f"Generated the content but couldn't save the file — {e}"
    return f"Presentation created: {out_path}"


# ── Gmail / Calendar: pending-event store (for the attendee confirm gate) ───

def _load_pending_events() -> dict:
    try:
        return json.loads(PENDING_EVENTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_pending_events(store: dict):
    PENDING_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_EVENTS_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")


def _not_configured_message(e: Exception) -> str:
    return str(e)


def _do_check_inbox(p: dict, player=None, speak=None) -> str:
    from core.google_workspace import list_recent_emails, GoogleWorkspaceNotConfigured
    try:
        emails = list_recent_emails(
            max_results=int(p.get("max_results") or 10),
            unread_only=p.get("unread_only", True) not in (False, "false", "False"),
        )
    except GoogleWorkspaceNotConfigured as e:
        return _not_configured_message(e)
    except Exception as e:
        return f"Couldn't reach Gmail: {e}"

    if not emails:
        return "Nothing unread in the inbox right now."

    if player is not None and hasattr(player, "show_content"):
        try:
            columns = ["From", "Subject", "Preview"]
            rows = [[e["from_display"][:40], e["subject"][:70], e["snippet"][:90]] for e in emails]
            player.show_content(
                f"INBOX — {len(emails)} unread",
                "\n".join(f"• {e['from_display']} — \"{e['subject']}\"" for e in emails),
                kind="table",
                payload={"columns": columns, "rows": rows},
            )
        except Exception:
            pass

    return (
        f"{len(emails)} unread — showing them on screen. Tell me which one "
        f"(by sender or subject) if you'd like a reply drafted."
    )


def _find_email_by_hint(p: dict) -> dict | None:
    """draft_reply may get an explicit email_id, or just a sender/subject
    hint from natural conversation — this resolves the hint against the
    current unread list."""
    from core.google_workspace import list_recent_emails
    if p.get("email_id"):
        return {"id": p["email_id"]}
    hint = (p.get("from") or p.get("subject") or p.get("search") or "").strip().lower()
    if not hint:
        return None
    for e in list_recent_emails(max_results=25, unread_only=False):
        if hint in e["from"].lower() or hint in e["from_display"].lower() or hint in e["subject"].lower():
            return e
    return None


def _do_draft_reply(p: dict) -> str:
    from core.google_workspace import get_email_body, create_draft_reply, GoogleWorkspaceNotConfigured
    try:
        target = _find_email_by_hint(p)
        if not target:
            return "Couldn't find that email — give me the id from check_inbox, or a sender/subject to match on."

        original = get_email_body(target["id"])
        instructions = p.get("instructions") or p.get("description") or "Write a polite, brief, helpful reply."

        from core.ai_client import generate_content
        prompt = (
            f"Draft an email reply. Original message from {original['from']}, "
            f"subject \"{original['subject']}\":\n\n{original['body'][:2000]}\n\n"
            f"What the reply should say, per Felix's instructions: {instructions}\n\n"
            f"Write ONLY the reply body text — no subject line, no \"Dear X\" if the "
            f"original didn't use formal address, matching a natural professional tone."
        )
        resp = generate_content(prompt)
        body = (resp.text if resp else "").strip()
        if not body:
            return "Couldn't generate a reply — try again with clearer instructions."

        draft = create_draft_reply(target["id"], body)
        preview = body[:280] + ("..." if len(body) > 280 else "")
        return (
            f"Draft saved to Gmail (To: {draft['to']}, Subject: {draft['subject']}) — "
            f"review and send it yourself when ready:\n\n{preview}"
        )
    except GoogleWorkspaceNotConfigured as e:
        return _not_configured_message(e)
    except Exception as e:
        return f"Couldn't draft that reply: {e}"


def _do_schedule_event(p: dict) -> str:
    from core.google_workspace import create_event, GoogleWorkspaceNotConfigured
    title = (p.get("title") or p.get("summary") or "").strip()
    start = (p.get("start") or "").strip()
    end   = (p.get("end") or "").strip()
    if not (title and start and end):
        return "Need at least a title, start, and end time (ISO format, e.g. 2026-09-05T14:00:00) to schedule an event."

    attendees   = [a.strip() for a in (p.get("attendees") or []) if a.strip()]
    description = p.get("description", "")
    location    = p.get("location", "")

    if not attendees:
        try:
            event = create_event(title, start, end, description=description, location=location)
        except GoogleWorkspaceNotConfigured as e:
            return _not_configured_message(e)
        except Exception as e:
            return f"Couldn't create that event: {e}"
        return f"Event created: \"{title}\" — {event['link']}"

    # Has attendees — Google emails them the instant the event is created,
    # so this always stops for confirmation first, no exceptions.
    event_id = uuid.uuid4().hex[:8]
    store = _load_pending_events()
    store[event_id] = {
        "title": title, "start": start, "end": end,
        "attendees": attendees, "description": description, "location": location,
    }
    _save_pending_events(store)
    return (
        f"Ready to create \"{title}\" from {start} to {end}, inviting: "
        f"{', '.join(attendees)}. This will send them a real calendar invite the "
        f"moment it's created — say \"confirm event {event_id}\" to go ahead, or "
        f"\"cancel event {event_id}\" to drop it."
    )


def _do_confirm_event(p: dict) -> str:
    from core.google_workspace import create_event, GoogleWorkspaceNotConfigured
    event_id = (p.get("event_id") or "").strip()
    store = _load_pending_events()
    pending = store.get(event_id)
    if not pending:
        return f"No pending event with id '{event_id}' — it may have already been confirmed, cancelled, or the id's off."
    try:
        event = create_event(
            pending["title"], pending["start"], pending["end"],
            attendees=pending["attendees"], description=pending.get("description", ""),
            location=pending.get("location", ""),
        )
    except GoogleWorkspaceNotConfigured as e:
        return _not_configured_message(e)
    except Exception as e:
        return f"Couldn't create that event: {e}"
    del store[event_id]
    _save_pending_events(store)
    return f"Confirmed — \"{pending['title']}\" created and invites sent: {event['link']}"


def _do_cancel_event(p: dict) -> str:
    event_id = (p.get("event_id") or "").strip()
    store = _load_pending_events()
    if event_id not in store:
        return f"No pending event with id '{event_id}'."
    title = store[event_id]["title"]
    del store[event_id]
    _save_pending_events(store)
    return f"Cancelled — \"{title}\" was never created, no invites sent."


def _do_list_events(p: dict, player=None, speak=None) -> str:
    from core.google_workspace import list_upcoming_events, GoogleWorkspaceNotConfigured
    try:
        events = list_upcoming_events(max_results=int(p.get("max_results") or 10))
    except GoogleWorkspaceNotConfigured as e:
        return _not_configured_message(e)
    except Exception as e:
        return f"Couldn't reach Calendar: {e}"
    if not events:
        return "Nothing on the calendar coming up."

    if player is not None and hasattr(player, "show_content"):
        try:
            columns = ["When", "Event", "Attendees"]
            rows = [
                [e["start"], e["summary"], str(len(e["attendees"])) if e["attendees"] else "—"]
                for e in events
            ]
            player.show_content(
                f"UPCOMING — {len(events)} on the calendar",
                "\n".join(f"• {e['start']} — {e['summary']}" for e in events),
                kind="table",
                payload={"columns": columns, "rows": rows},
            )
        except Exception:
            pass

    return f"{len(events)} upcoming — showing them on screen."


# ── Gmail: periodic background scan ─────────────────────────────────────────

def _load_scan_state() -> dict:
    try:
        return json.loads(GMAIL_SCAN_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_ids": []}


def _save_scan_state(state: dict):
    GMAIL_SCAN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # cap the seen-id log so this file doesn't grow forever
    state["seen_ids"] = state.get("seen_ids", [])[-500:]
    GMAIL_SCAN_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def _scan_inbox_and_draft(player=None):
    """One scan cycle: look at unread mail not seen before, ask the model
    which ones genuinely need a reply (vs. a notification/newsletter/no-reply
    sender), draft replies for those, and report only if there's something
    new — silence otherwise, no noise for an empty scan."""
    from core.google_workspace import list_recent_emails, GoogleWorkspaceNotConfigured
    from core.ai_client import generate_content

    try:
        emails = list_recent_emails(max_results=15, unread_only=True)
    except GoogleWorkspaceNotConfigured:
        return  # not set up yet — silently skip, this is a background pass
    except Exception as e:
        print(f"[{AGENT_NAME}] Background Gmail scan failed: {e}")
        return

    state = _load_scan_state()
    seen = set(state.get("seen_ids", []))
    new_emails = [e for e in emails if e["id"] not in seen]
    if not new_emails:
        return

    if player is not None and hasattr(player, "set_active_agent"):
        try:
            player.set_active_agent("adeola")
        except Exception:
            pass

    drafted = []
    for e in new_emails:
        state.setdefault("seen_ids", []).append(e["id"])
        try:
            classify = generate_content(
                f"An email arrived. From: {e['from']}. Subject: {e['subject']}. "
                f"Preview: {e['snippet'][:300]}\n\n"
                f"Does this genuinely need a personal reply from Felix, or is it a "
                f"notification, newsletter, receipt, or automated/no-reply email? "
                f"Respond with ONLY one word: REPLY or SKIP."
            )
            verdict = (classify.text or "").strip().upper() if classify else "SKIP"
        except Exception:
            verdict = "SKIP"

        if "REPLY" not in verdict:
            continue

        try:
            result = _do_draft_reply({"email_id": e["id"], "instructions": "Write a brief, professional reply."})
            if result.startswith("Draft saved"):
                drafted.append((e, result))
        except Exception as ex:
            print(f"[{AGENT_NAME}] Auto-draft failed for {e['id']}: {ex}")

    _save_scan_state(state)

    if player is not None and hasattr(player, "set_active_agent"):
        try:
            player.set_active_agent("lite")
        except Exception:
            pass

    if not drafted:
        return

    n = len(drafted)
    if player is not None and hasattr(player, "notify"):
        try:
            player.notify(
                "INBOX",
                f"{n} new repl{'y' if n == 1 else 'ies'} drafted for your review — check Gmail Drafts.",
            )
        except Exception:
            pass

    if player is not None and hasattr(player, "show_content"):
        try:
            columns = ["From", "Subject", "Draft ready"]
            rows = [[e["from_display"][:40], e["subject"][:60], "Yes — in Gmail Drafts"] for e, _ in drafted]
            player.show_content(
                f"INBOX SCAN — {n} repl{'y' if n == 1 else 'ies'} drafted for your review",
                "\n".join(f"• {e['from_display']} — \"{e['subject']}\"" for e, _ in drafted),
                kind="table",
                payload={"columns": columns, "rows": rows},
            )
        except Exception:
            pass


def _seconds_until_next_run(scan_times: list[tuple[int, int]]):
    from datetime import datetime, timedelta
    now = datetime.now()
    candidates = []
    for h, m in scan_times:
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    next_run = min(candidates)
    return (next_run - now).total_seconds(), next_run


def start_gmail_scan_scheduler(player=None, scan_times: list[tuple[int, int]] | None = None):
    """Starts the one genuinely proactive background loop in the app — wakes
    at fixed clock times every day (default 10:00 and 16:00, local time),
    not a rolling interval from whenever LITE happened to start (which
    would drift to a different time of day every restart). Call once at
    startup."""
    times = scan_times or DEFAULT_SCAN_TIMES

    def _loop():
        while True:
            wait_s, next_run = _seconds_until_next_run(times)
            time.sleep(wait_s)
            try:
                _scan_inbox_and_draft(player=player)
            except Exception as e:
                print(f"[{AGENT_NAME}] Gmail scan scheduler error: {e}")

    threading.Thread(target=_loop, daemon=True).start()
    clock = ", ".join(f"{h:02d}:{m:02d}" for h, m in times)
    print(f"[{AGENT_NAME}] Background Gmail scan scheduled daily at {clock}.")


_ACTIONS = {
    "create_document":     _do_create_document,
    "create_presentation":  _do_create_presentation,
    "draft_reply":          _do_draft_reply,
    "schedule_event":       _do_schedule_event,
    "confirm_event":        _do_confirm_event,
    "cancel_event":         _do_cancel_event,
}
_ACTIONS_WITH_IO = {
    "schedule_reminder": _do_schedule_reminder,
    "check_inbox":       _do_check_inbox,
    "list_events":        _do_list_events,
}


def scheduling_docs_agent(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Single entry point LITE delegates scheduling, document, or
    presentation tasks to."""
    p = parameters or {}
    action = (p.get("action") or "").strip().lower()
    if not action:
        if p.get("date") and p.get("time"):
            action = "schedule_reminder"
        elif "presentation" in (p.get("description") or "").lower() or "slide" in (p.get("description") or "").lower():
            action = "create_presentation"
        else:
            action = "create_document"

    if action in _ACTIONS_WITH_IO:
        try:
            return _report(_ACTIONS_WITH_IO[action](p, player=player, speak=speak))
        except Exception as e:
            print(f"[{AGENT_NAME}] Error in '{action}': {e}")
            return _report(f"Hit an error: {e}")

    handler = _ACTIONS.get(action)
    if not handler:
        return _report(
            f"Unknown action '{action}'. Available: "
            f"{', '.join(list(_ACTIONS_WITH_IO) + list(_ACTIONS))}."
        )

    try:
        return _report(handler(p))
    except Exception as e:
        print(f"[{AGENT_NAME}] Error in '{action}': {e}")
        return _report(f"Hit an error: {e}")

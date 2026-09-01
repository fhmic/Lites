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
from pathlib import Path

AGENT_NAME = "Scheduling & Docs Agent"

OUTPUT_DIR = Path.home() / "Documents" / "LITE Generated"


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


_ACTIONS = {
    "create_document": _do_create_document,
    "create_presentation": _do_create_presentation,
}
_ACTIONS_WITH_IO = {
    "schedule_reminder": _do_schedule_reminder,
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

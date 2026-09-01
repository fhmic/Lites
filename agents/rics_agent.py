#rics_agent.py
"""
RICS (Research, Intelligence & CRM/Sales) Agent — reports to LITE (the
Executive Orchestrator). Second of the specialist sub-agents, following the
exact pattern automation_coding_agent.py established: same call signature,
same mode-inference front door, same "[Agent Name] ..." report prefix.

RICS owns the whole research -> log -> pipeline motion, on the premise that
prospect/company research isn't a separate deliverable from sales work —
it's a step inside it. It wraps, rather than duplicates:

  - actions/affiliate_growth_agent.py's `_worker_request` (already-hardened
    HTTP client for the GAS Cloudflare Worker) for all CRM reads/writes —
    CRM storage was deliberately routed through GAS's EXISTING Worker
    rather than standing up a second one, since CRM is read/write-on-
    demand, not autonomous/cron-driven like GAS's own growth-ops tables.
  - affiliate_growth_agent() itself, for the growth_job / growth_report
    capabilities — GAS's own affiliate-growth flow lives under RICS now,
    unchanged, just delegated to rather than called directly by LITE.
  - actions/web_search.py's DDG helpers + core.ai_client's shared LLM
    fallback chain, for the `research` capability — no new search
    provider or API key handling here.

Capabilities (parameters["action"], inferred from a plain-language task if
not given — see _infer_action):
    research            - look up a company/person, optionally save findings
    log_company         - create/update a CRM company record
    log_contact         - create/update a CRM contact record
    log_interaction     - record a touchpoint against a contact/company/deal
    update_deal         - create/move a deal (stage, value, probability)
    list_pipeline       - summarize open deals, optionally by stage
    list_tasks          - show open/overdue follow-ups
    growth_job          - delegates to GAS's affiliate-growth job assignment
    growth_report       - pulls GAS's latest "while you were away" report
"""
from actions.affiliate_growth_agent import _worker_request, affiliate_growth_agent

AGENT_NAME = "RICS Agent"


def _report(body: str) -> str:
    """Same reporting convention automation_coding_agent.py established —
    keeps a multi-agent answer attributable to which agent said what."""
    return f"[{AGENT_NAME}] {body}"


def _infer_action(p: dict) -> str:
    """Explicit action always wins. Otherwise a light keyword pass over the
    task description — LITE should be able to hand RICS a plain-language
    request without pre-deciding the exact capability."""
    action = (p.get("action") or "").strip().lower()
    if action:
        return action

    text = (p.get("description") or p.get("task") or "").lower()
    if any(w in text for w in ("review", "pending", "approve", "reject", "draft")):
        return "update_content" if any(w in text for w in ("approve", "reject", "edit", "change")) else "review_content"
    if any(w in text for w in ("research", "look up", "find out about", "who is", "what is")):
        return "research"
    if any(w in text for w in ("deal", "pipeline", "stage", "close")):
        return "update_deal" if any(w in text for w in ("move", "update", "close", "won", "lost")) else "list_pipeline"
    if any(w in text for w in ("task", "follow up", "follow-up", "reminder", "due")):
        return "list_tasks"
    if any(w in text for w in ("called", "emailed", "met with", "spoke", "replied", "logged")):
        return "log_interaction"
    if any(w in text for w in ("affiliate", "growth job", "overnight agent")):
        return "growth_job"
    if "report" in text and any(w in text for w in ("affiliate", "growth", "earnings", "leads")):
        return "growth_report"
    return "research"  # safest default — read-only, never writes on a guess


# ── CRM client — thin wrappers over the already-hardened GAS HTTP client ────

def _crm_get(path: str, **params) -> dict:
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    full_path = f"{path}?{query}" if query else path
    return _worker_request("GET", full_path)

def _crm_post(path: str, body: dict) -> dict:
    return _worker_request("POST", path, {k: v for k, v in body.items() if v is not None})


# ── Capabilities ─────────────────────────────────────────────────────────

def _do_research(p: dict) -> str:
    target = (p.get("target") or p.get("company") or p.get("query") or p.get("description") or "").strip()
    if not target:
        return "Who or what company should I research?"

    from actions.web_search import _ddg_search, _ddg_news, _format_ddg
    from core.ai_client import generate_content

    results = _ddg_news(target, max_results=6) or _ddg_search(target, max_results=6)
    raw = _format_ddg(target, results) if results else f"No fresh signal found for: {target}"

    prompt = (
        f"Summarize what's known about '{target}' for a sales/business-development "
        f"context — what they do, size/industry if determinable, and anything timely "
        f"worth knowing before reaching out. Be concise, factual, no filler.\n\n{raw}"
    )
    try:
        resp = generate_content(prompt)
        summary = (resp.text if resp else "").strip() or raw
    except Exception:
        summary = raw

    saved_note = ""
    if p.get("save", True):
        try:
            company = _crm_post("/crm/companies", {"name": target, "notes": summary, "source": "research"})
            company_id = (company.get("company") or {}).get("id")
            if company_id:
                _crm_post("/crm/interactions", {
                    "company_id": company_id, "type": "research", "summary": summary,
                })
            saved_note = " Saved to CRM."
        except Exception as e:
            saved_note = f" (CRM save failed — {e})"

    return f"Research on {target}:\n{summary}{saved_note}"


def _do_log_company(p: dict) -> str:
    name = (p.get("name") or p.get("company") or "").strip()
    if not name:
        return "What's the company name?"
    result = _crm_post("/crm/companies", {
        "id": p.get("id"), "name": name, "domain": p.get("domain"),
        "industry": p.get("industry"), "size": p.get("size"),
        "notes": p.get("notes"), "source": p.get("source", "manual"),
    })
    c = result.get("company", {})
    return f"Company logged: {c.get('name')} (id: {c.get('id')})"


def _do_log_contact(p: dict) -> str:
    name = (p.get("name") or "").strip()
    if not name:
        return "What's the contact's name?"
    result = _crm_post("/crm/contacts", {
        "id": p.get("id"), "company_id": p.get("company_id"), "name": name,
        "email": p.get("email"), "role": p.get("role"),
        "notes": p.get("notes"), "source": p.get("source", "manual"),
    })
    c = result.get("contact", {})
    return f"Contact logged: {c.get('name')} (id: {c.get('id')})"


def _do_log_interaction(p: dict) -> str:
    summary = (p.get("summary") or p.get("description") or "").strip()
    itype = (p.get("type") or "note").strip()
    if not summary:
        return "What should I log?"
    if not any([p.get("contact_id"), p.get("company_id"), p.get("deal_id")]):
        return "I need a contact, company, or deal to log this against."
    result = _crm_post("/crm/interactions", {
        "contact_id": p.get("contact_id"), "company_id": p.get("company_id"),
        "deal_id": p.get("deal_id"), "type": itype, "summary": summary,
    })
    return f"Logged ({itype}): {result.get('interaction', {}).get('id', 'saved')}"


def _do_update_deal(p: dict) -> str:
    title = (p.get("title") or "").strip()
    if not p.get("id") and not title:
        return "What's the deal called, or which deal id am I updating?"
    result = _crm_post("/crm/deals", {
        "id": p.get("id"), "contact_id": p.get("contact_id"), "company_id": p.get("company_id"),
        "title": title or None, "stage": p.get("stage"), "value": p.get("value"),
        "currency": p.get("currency"), "probability": p.get("probability"),
        "expected_close": p.get("expected_close"), "notes": p.get("notes"),
    })
    d = result.get("deal", {})
    return f"Deal '{d.get('title')}' -> stage: {d.get('stage')} (id: {d.get('id')})"


def _do_list_pipeline(p: dict) -> str:
    result = _crm_get("/crm/deals", stage=p.get("stage"))
    deals = result.get("deals", [])
    if not deals:
        scope = f" in stage '{p['stage']}'" if p.get("stage") else ""
        return f"No open deals{scope}."
    lines = [f"{len(deals)} deal(s):"]
    for d in deals:
        val = f" — {d.get('currency', 'USD')} {d['value']}" if d.get("value") else ""
        lines.append(f"  • {d['title']} [{d['stage']}]{val}")
    return "\n".join(lines)


def _do_list_tasks(p: dict) -> str:
    result = _crm_get("/crm/tasks", done="false")
    tasks = result.get("tasks", [])
    if not tasks:
        return "No open tasks."
    lines = [f"{len(tasks)} open task(s):"]
    for t in tasks:
        due = f" (due {t['due_at'][:10]})" if t.get("due_at") else ""
        lines.append(f"  • {t['description']}{due}")
    return "\n".join(lines)


def _do_review_content(p: dict) -> str:
    """
    Surfaces what's actually sitting in GAS's content_queue for review —
    the piece that was missing entirely before. Nothing in GAS auto-
    publishes; insertDrafts() already writes everything as
    pending_approval. This just gives LITE (and the user) a way to see it.
    """
    result = _crm_get("/content", status=p.get("status") or "pending_approval", job_id=p.get("job_id"))
    items = result.get("content", [])
    if not items:
        status = p.get("status") or "pending_approval"
        return f"Nothing in '{status}' right now."
    lines = [f"{len(items)} item(s) awaiting review:"]
    for item in items:
        lines.append(
            f"\n— [{item['id']}] {item['platform']} / {item['content_type']} — \"{item['title']}\"\n"
            f"{item['body'][:400]}{'...' if len(item['body']) > 400 else ''}"
        )
    return "\n".join(lines)


def _do_update_content(p: dict) -> str:
    """Approve, reject, or edit a specific draft by id. Only fields
    actually supplied get changed — approving without touching body/title
    leaves the content exactly as generated; supplying a new body/title
    IS the manual-edit path."""
    content_id = (p.get("id") or p.get("content_id") or "").strip()
    if not content_id:
        return "Which item — I need its id (from review_content's output)."
    fields = {
        "id": content_id,
        "status": p.get("status"),
        "title": p.get("title"),
        "body": p.get("body"),
    }
    result = _crm_post("/content", fields)
    item = result.get("content", {})
    return f"[{item.get('id')}] {item.get('title')} -> status: {item.get('status')}"


_ACTIONS = {
    "research":        _do_research,
    "log_company":      _do_log_company,
    "log_contact":      _do_log_contact,
    "log_interaction":  _do_log_interaction,
    "update_deal":      _do_update_deal,
    "list_pipeline":    _do_list_pipeline,
    "list_tasks":       _do_list_tasks,
    "review_content":   _do_review_content,
    "update_content":   _do_update_content,
}


def rics_agent(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Single entry point LITE delegates any research, prospecting, or CRM/
    sales task to. Routes internally to the right capability (see
    _infer_action) so LITE doesn't need to pre-decide the exact procedure."""
    p = parameters or {}
    action = _infer_action(p)

    # These two are pure delegation to the existing, unchanged GAS flow.
    if action in ("growth_job", "growth_report"):
        gas_action = "assign_job" if action == "growth_job" else "get_report"
        result = affiliate_growth_agent({**p, "action": gas_action}, player=player, speak=speak)
        return _report(result) if not result.startswith("[") else result

    handler = _ACTIONS.get(action)
    if not handler:
        return _report(f"Unknown action '{action}'. Try: {', '.join(_ACTIONS)}, growth_job, growth_report.")

    try:
        return _report(handler(p))
    except Exception as e:
        print(f"[{AGENT_NAME}] Error in '{action}': {e}")
        return _report(f"Hit an error: {e}")

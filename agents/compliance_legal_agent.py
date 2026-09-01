#compliance_legal_agent.py
"""
Compliance & Legal Agent — reports to LITE (the Executive Orchestrator).
Sixth specialist sub-agent, following the same pattern as every prior one:
same call signature, mode-inference-free explicit action dispatch, same
"[Agent Name] ..." report prefix.

SCOPE: Nigerian-primary (NDPR/data protection, CAC company law, sector
licensing, contract law), never Nigeria-only by construction — every
capability touching regulation accepts a `jurisdiction` parameter,
defaulting to Nigeria when unspecified. Same posture as FTA throughout:
presents analysis and options, never a single prescriptive "you must do X"
directive — this isn't a substitute for a licensed lawyer, and every
capability here says so explicitly when the situation warrants it.

Capabilities:
    contract_review        - reviews a contract (file or pasted text) for
                              risks, non-standard terms, missing clauses
    compliance_check        - ALWAYS live search — is a described activity/
                              practice compliant with current regulation
    regulatory_research     - ALWAYS live search — current requirements,
                              licensing, registration for a business activity
    risk_assessment          - legal/compliance risk areas for a described
                              scenario, blending framework + live search for
                              current relevant regulation/precedent
"""
AGENT_NAME = "Compliance & Legal Agent"


def _report(body: str) -> str:
    return f"[{AGENT_NAME}] {body}"


def _live_search(query: str, angles: list[str], label: str = "") -> str:
    """Same shared search plumbing as FTA — search first, synthesize
    second, never state a regulatory position from training data."""
    from actions.web_search import _ddg_search, _ddg_news, _format_ddg

    combined = []
    for angle in angles:
        try:
            combined.extend(_ddg_news(angle, max_results=5) or _ddg_search(angle, max_results=5))
        except Exception as e:
            print(f"[{AGENT_NAME}] {label} search failed for {angle!r}: {e}")
    if not combined:
        return ""
    return _format_ddg(query, combined)


def _gather_text(p: dict, player=None, speak=None) -> tuple[str, str | None]:
    """Same file-or-pasted-text gathering pattern as FTA's statement
    interpretation — collects contract text from an uploaded file and/or
    directly pasted text."""
    parts = []
    file_path = (p.get("file_path") or "").strip()
    if file_path:
        from pathlib import Path
        from actions.file_processor import file_processor, _detect_type
        path = Path(file_path)
        if not path.exists():
            return "", f"File not found: {file_path}"
        ftype = _detect_type(path)
        action = "extract_text" if ftype == "pdf" else "analyze"
        parts.append(file_processor(parameters={"file_path": file_path, "action": action}, player=player, speak=speak))

    text = (p.get("statement_text") or p.get("contract_text") or "").strip()
    if text:
        parts.append(text)

    if not parts:
        return "", (
            "I need a contract to review — either a file_path to an uploaded document, "
            "or contract_text with it pasted directly."
        )
    return "\n\n".join(parts), None


def _do_contract_review(p: dict, player=None, speak=None) -> str:
    from core.ai_client import generate_content

    raw_text, error = _gather_text(p, player=player, speak=speak)
    if error:
        return error
    context = (p.get("context") or "").strip()

    prompt = (
        "You are reviewing a contract in plain English for a business owner who is not "
        "a lawyer. Identify: (1) the key commercial terms, (2) any non-standard or "
        "one-sided clauses, (3) anything missing that a contract like this would "
        "typically include, (4) genuine risk areas. Present ANALYSIS and OPTIONS — "
        "never a single prescriptive 'sign this' or 'don't sign this' directive. "
        "Explicitly flag if any specific clause is significant enough that a licensed "
        "lawyer should review it directly before signing.\n\n"
        + (f"Context from prior research/analysis (from another agent): {context}\n\n" if context else "")
        + "Respond in exactly this structure:\n\n"
        "CONTRACT SUMMARY:\n<2-3 sentences on what this contract is and its key terms>\n\n"
        "FLAGS & CONSIDERATIONS:\n<non-standard clauses, missing provisions, risk areas — "
        "each with why it matters>\n\n"
        "OPTIONS:\n<possible responses — accept as-is, negotiate specific clauses, seek "
        "clarification — with trade-offs, not a single directive>\n\n"
        "CONSULT A LAWYER IF:\n<when this specific contract genuinely needs licensed review>\n\n"
        f"Contract:\n{raw_text[:20000]}"
    )
    try:
        resp = generate_content(prompt)
        return (resp.text if resp else "").strip() or "Couldn't generate a review for this."
    except Exception as e:
        return f"Couldn't review that — {e}"


def _do_compliance_check(p: dict) -> str:
    from core.ai_client import generate_content
    from datetime import datetime

    query = (p.get("description") or p.get("query") or "").strip()
    if not query:
        return "What activity or practice should I check compliance for?"
    jurisdiction = (p.get("jurisdiction") or "Nigeria").strip()
    today = datetime.now().strftime("%B %Y")

    raw = _live_search(
        query,
        angles=[f"{query} {jurisdiction} compliance regulation {today}", f"{query} {jurisdiction} legal requirement current"],
        label="Compliance check",
    )
    if not raw:
        return (
            f"No current information found for '{query}' in {jurisdiction} — this may "
            f"need a licensed lawyer or compliance professional's direct input."
        )

    prompt = (
        f"Based on the current search results below, assess compliance for this "
        f"activity/practice in {jurisdiction}: '{query}'\n\n"
        "Present the current regulatory position clearly, with practical options where "
        "relevant — never a single prescriptive directive. Be precise about what the "
        "search results actually support versus general principles. Explicitly flag "
        "if this is complex/high-stakes enough that licensed legal counsel should be "
        "consulted directly.\n\n"
        "Respond in exactly this structure:\n\n"
        "CURRENT POSITION:\n<what the search found, with specific requirements where available>\n\n"
        "PRACTICAL IMPLICATIONS:\n<what this means in practice, as options where relevant>\n\n"
        "CONSULT A LAWYER IF:\n<when this specific situation needs licensed advice>\n\n"
        f"Search results:\n{raw}"
    )
    try:
        resp = generate_content(prompt)
        return (resp.text if resp else "").strip() or raw
    except Exception as e:
        print(f"[{AGENT_NAME}] Compliance check synthesis failed: {e}")
        return raw


def _do_regulatory_research(p: dict) -> str:
    from core.ai_client import generate_content
    from datetime import datetime

    query = (p.get("description") or p.get("query") or "").strip()
    if not query:
        return "What regulatory/licensing question should I research?"
    jurisdiction = (p.get("jurisdiction") or "Nigeria").strip()
    today = datetime.now().strftime("%B %Y")

    raw = _live_search(
        query,
        angles=[f"{query} {jurisdiction} requirements {today}", f"{query} {jurisdiction} registration licensing"],
        label="Regulatory research",
    )
    if not raw:
        return f"No current information found for '{query}' in {jurisdiction} — try rephrasing or a narrower query."

    prompt = (
        f"Based on the current search results below, answer this regulatory/licensing "
        f"question for {jurisdiction}: '{query}'\n\n"
        "Present the current requirements clearly, as options/steps where multiple paths "
        "exist — never a single prescriptive directive without noting alternatives.\n\n"
        "Respond in exactly this structure:\n\n"
        "CURRENT REQUIREMENTS:\n<what's actually needed, per the search results>\n\n"
        "OPTIONS & CONSIDERATIONS:\n<paths available, if more than one, with trade-offs>\n\n"
        f"Search results:\n{raw}"
    )
    try:
        resp = generate_content(prompt)
        return (resp.text if resp else "").strip() or raw
    except Exception as e:
        print(f"[{AGENT_NAME}] Regulatory research synthesis failed: {e}")
        return raw


def _do_risk_assessment(p: dict) -> str:
    from core.ai_client import generate_content

    query = (p.get("description") or p.get("query") or "").strip()
    if not query:
        return "What business decision or scenario should I assess for legal/compliance risk?"
    context = (p.get("context") or "").strip()
    jurisdiction = (p.get("jurisdiction") or "Nigeria").strip()

    raw = _live_search(
        query,
        angles=[f"{query} {jurisdiction} legal risk", f"{query} {jurisdiction} regulation precedent"],
        label="Risk assessment",
    )

    prompt = (
        f"Assess the legal/compliance risk areas for this scenario in {jurisdiction}: "
        f"'{query}'\n\n"
        + (f"Context from prior research/analysis (from another agent): {context}\n\n" if context else "")
        + (f"Current relevant signal found via search:\n{raw}\n\n" if raw else "No current market-specific signal was found via search — rely on general risk frameworks and say so explicitly.\n\n")
        + "Present ANALYSIS and OPTIONS — never a single prescriptive directive. Identify "
        "the specific risk areas for THIS scenario, not a generic checklist.\n\n"
        "Respond in exactly this structure:\n\n"
        "SCENARIO SUMMARY:\n<2-3 sentences>\n\n"
        "RISK AREAS:\n<the specific legal/compliance risks here, each with severity context>\n\n"
        "MITIGATION OPTIONS:\n<ways to address each risk area, with trade-offs>\n\n"
        "CONSULT A LAWYER IF:\n<when this specific scenario needs licensed advice>"
    )
    try:
        resp = generate_content(prompt)
        return (resp.text if resp else "").strip() or "Couldn't generate a risk assessment for this."
    except Exception as e:
        return f"Hit an error generating the assessment: {e}"


_ACTIONS = {
    "compliance_check": _do_compliance_check,
    "regulatory_research": _do_regulatory_research,
    "risk_assessment": _do_risk_assessment,
}
_ACTIONS_WITH_IO = {
    "contract_review": _do_contract_review,
}


def compliance_legal_agent(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Single entry point LITE delegates any contract, compliance, or legal
    risk task to."""
    p = parameters or {}
    action = (p.get("action") or "").strip().lower()
    if not action:
        action = "contract_review" if (p.get("file_path") or p.get("contract_text")) else "regulatory_research"

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

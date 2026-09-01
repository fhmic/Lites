#fta_agent.py
"""
FTA (Finance, FP&A & Treasury/Cashflow) Agent — reports to LITE (the
Executive Orchestrator). Fourth specialist sub-agent, same pattern as
automation_coding_agent.py / rics_agent.py / data_analytics_agent.py.

SCOPE (from the FTA blueprint): Nigerian-primary market, never Nigeria-
only by construction — every capability that touches market data or
regulation accepts a market/currency/jurisdiction parameter, defaulting
to Nigeria when unspecified. Presents analysis and options, never a
single prescriptive recommendation.

Five capability groups planned; built in phases, each tested before the
next starts:
    A. Financial statement interpretation & advisory   <- THIS PHASE (new)
    B. Investment/market research (live rates)          [not yet built]
    C. Investment appraisal (NPV/IRR/MIRR/PBP/ARR/PI)   [built]
    D. M&A advisory                                     [not yet built]
    E. Tax & regulatory (live search)                   [not yet built]

§3 cross-agent flow (RICS -> Data Analytics -> FTA) is exercised for the
first time in Phase A: statement_interpretation accepts an optional
`context` string — pre-computed findings from another agent, passed via
LITE — so FTA can derive financial insight FROM another agent's work,
not just from a fresh standalone request.

Phase A's arithmetic discipline matches Phase C's: ratios are never
computed by the LLM. The flow is EXTRACT (LLM turns a raw statement into
structured figures — a legitimate language task) -> COMPUTE (real code,
agents/financial_ratios.py) -> INTERPRET (LLM turns the numbers into
analysis + options — legitimate again). Arithmetic never happens inside
a model call.

This file only exposes what's actually built (Phases A and C) — its tool
description in main.py must stay honest about that, the same discipline
already applied to Data Analytics's deferred 'visualize' capability.
"""
import json

from agents.financial_ratios import compute_all
from agents.appraisal import appraise

AGENT_NAME = "FTA Agent"


def _report(body: str) -> str:
    return f"[{AGENT_NAME}] {body}"


def _fmt_pct(x: float | None) -> str:
    return f"{x * 100:.2f}%" if x is not None else "n/a (no real root)"


def _parse_json_block(text: str) -> dict | None:
    """Same tolerant JSON extraction as web_search.py's opportunities
    pipeline — strips code fences, finds the outermost braces, never
    raises. Returns None (not an exception) on anything unparseable."""
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


def _gather_statement_text(p: dict, player=None, speak=None) -> tuple[str, str | None]:
    """Collects the raw statement text from whichever source was given —
    an uploaded file, pasted text, or both — returning (text, error).
    error is None on success."""
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
        extracted = file_processor(
            parameters={"file_path": file_path, "action": action},
            player=player, speak=speak,
        )
        parts.append(extracted)

    statement_text = (p.get("statement_text") or "").strip()
    if statement_text:
        parts.append(statement_text)

    if not parts:
        return "", (
            "I need a statement to work with — either a file_path to an uploaded "
            "income statement/cash flow/balance sheet, or statement_text with the "
            "figures pasted directly."
        )
    return "\n\n".join(parts), None


def _do_statement_interpretation(p: dict, player=None, speak=None) -> str:
    from core.ai_client import generate_content

    raw_text, error = _gather_statement_text(p, player=player, speak=speak)
    if error:
        return error

    context = (p.get("context") or "").strip()
    ask = (p.get("description") or "").strip()

    # Step 1: EXTRACT — LLM turns raw statement text into structured figures.
    # Legitimate language task; no arithmetic happens in this call.
    extract_prompt = (
        "Extract these financial figures from the statement below, in NGN unless "
        "another currency is explicitly stated. Use null for anything not "
        "determinable from the text — do not guess or estimate a figure that "
        "isn't actually present.\n\n"
        "Respond with ONLY this JSON, no other text, no markdown fences:\n"
        '{"currency": "NGN", "revenue": <num or null>, "cogs": <num or null>, '
        '"net_income": <num or null>, "current_assets": <num or null>, '
        '"current_liabilities": <num or null>, "inventory": <num or null>, '
        '"total_liabilities": <num or null>, "total_equity": <num or null>, '
        '"total_assets": <num or null>, "ebit": <num or null>, '
        '"interest_expense": <num or null>}\n\n'
        f"Statement:\n{raw_text[:20000]}"
    )
    try:
        resp = generate_content(extract_prompt)
        figures = _parse_json_block(resp.text if resp else "") or {}
    except Exception as e:
        return f"Couldn't read the statement — {e}"

    if not any(v is not None for k, v in figures.items() if k != "currency"):
        return (
            "I couldn't find identifiable financial figures in what was provided — "
            "double-check the file/text actually contains statement data."
        )

    # Step 2: COMPUTE — real code, never the LLM. Same discipline as Phase C.
    ratios = compute_all(figures)
    currency = figures.get("currency") or "NGN"

    # Step 3: INTERPRET — LLM turns the numbers (not raw text) into analysis
    # + options. Ratios are passed in already-computed; the model is never
    # asked to calculate anything here, only to explain what's already true.
    interpret_prompt = (
        "You are a financial analyst reviewing a company's statement. You are given "
        "ALREADY-COMPUTED figures and ratios below — do not recalculate anything, "
        "just interpret what's given. Present analysis and options; NEVER a single "
        "prescriptive 'you should do X' recommendation — lay out the option set with "
        "trade-offs and let the reader decide.\n\n"
        f"Extracted figures ({currency}): {json.dumps(figures)}\n\n"
        f"Computed ratios: {json.dumps(ratios)}\n\n"
        + (f"Context from prior research/analysis (from another agent): {context}\n\n" if context else "")
        + (f"Specific question to address: {ask}\n\n" if ask else "")
        + "Respond in exactly this structure:\n\n"
        "FINANCIAL POSITION SUMMARY:\n<2-3 sentences, plain language>\n\n"
        "KEY OBSERVATIONS:\n<bullet points on liquidity, leverage, profitability — "
        "only discuss ratios that were actually computable (not null)>\n\n"
        "OPTIONS & CONSIDERATIONS:\n<2-4 possible directions/actions with their "
        "trade-offs, not a single directive>"
    )
    try:
        resp = generate_content(interpret_prompt)
        analysis = (resp.text if resp else "").strip()
    except Exception as e:
        return f"Computed the ratios but couldn't generate the interpretation — {e}"

    ratio_lines = [f"  {k}: {v:.3f}" if v is not None else f"  {k}: n/a" for k, v in ratios.items()]
    return f"{analysis}\n\n---\nComputed ratios:\n" + "\n".join(ratio_lines)


def _live_search(query: str, angles: list[str], label: str = "") -> str:
    """
    Shared search plumbing for every FTA capability that needs current,
    never-from-training-data information (market research, M&A precedent,
    tax/regulatory). Returns formatted raw results, or "" if nothing was
    found — callers decide how to handle an empty result themselves, since
    "no data found" means something different for a market rate (probably
    a bad query) than for tax law (could genuinely mean nothing recent
    changed, which is itself useful to know).
    """
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


def _do_market_research(p: dict) -> str:
    """
    Phase B — live investment/market research. ALWAYS searches live; never
    answers a rate, yield, or regulatory question from training data, same
    discipline you've already established for Nigerian tax positions and
    LITE's own opportunities briefing. Nigerian-primary by default (MPR,
    T-bills, FGN bonds, money market, capital market) but never Nigeria-only
    by construction — `market` accepts any jurisdiction/currency, and a
    query naming another market overrides the default entirely.
    """
    from core.ai_client import generate_content
    from datetime import datetime

    query = (p.get("description") or p.get("query") or "").strip()
    if not query:
        return (
            "What should I research — a rate (MPR, T-bill, money market), a "
            "specific instrument (FGN bonds, a fund), or a general 'best options "
            "for X risk/return' question?"
        )
    market = (p.get("market") or "Nigeria").strip()
    today = datetime.now().strftime("%B %Y")

    raw = _live_search(
        query,
        angles=[f"{query} {market} {today}", f"{query} current rate {market}"],
        label="Market research",
    )
    if not raw:
        return f"No current data found for '{query}' in {market} — try rephrasing or a narrower query."

    prompt = (
        f"You are a financial-markets analyst. Based on the current search results below, "
        f"answer this investment/market research question for the {market} market: "
        f"'{query}'\n\n"
        "Present ANALYSIS and OPTIONS — never a single prescriptive 'put your money in X' "
        "recommendation. For each relevant option, note its approximate current rate/yield "
        "if determinable, and its risk profile (e.g. sovereign-backed and lower-risk vs. "
        "market-linked and higher-risk). Flag clearly if the search results don't give a "
        "precise current figure rather than guessing one.\n\n"
        "Respond in exactly this structure:\n\n"
        "CURRENT SIGNAL:\n<what the search actually found, with figures where available>\n\n"
        "OPTIONS & RISK PROFILE:\n<the relevant instruments/funds, each with its "
        "approximate rate/yield and risk level>\n\n"
        "CONSIDERATIONS:\n<2-3 factors relevant to choosing between them — not a directive>\n\n"
        f"Search results:\n{raw}"
    )
    try:
        resp = generate_content(prompt)
        return (resp.text if resp else "").strip() or raw
    except Exception as e:
        print(f"[{AGENT_NAME}] Market research synthesis failed: {e}")
        return raw


def _do_ma_advisory(p: dict) -> str:
    """
    Phase D — M&A advisory. Less formulaic than market research (there's no
    single "current rate" to look up), so this blends framework/conceptual
    guidance (valuation approaches, deal-structure considerations) with a
    live search pass for current precedent/context — always searches
    rather than trying to classify whether a given question "needs" current
    data, since that classification is itself a source of silent errors
    and the search cost is cheap. Same posture as every other FTA
    capability: options and trade-offs, never a single prescriptive
    "you should acquire/merge" directive.
    """
    from core.ai_client import generate_content

    query = (p.get("description") or p.get("query") or "").strip()
    if not query:
        return (
            "What's the M&A question or scenario — considering an acquisition, "
            "evaluating a valuation approach, structuring a deal, or something else?"
        )
    context = (p.get("context") or "").strip()

    raw = _live_search(
        query,
        angles=[f"{query} M&A precedent", f"{query} recent deal valuation multiple"],
        label="M&A advisory",
    )

    prompt = (
        f"You are an M&A advisor. Address this scenario/question: '{query}'\n\n"
        + (f"Context from prior research/analysis (from another agent): {context}\n\n" if context else "")
        + (f"Current market signal found via search:\n{raw}\n\n" if raw else "No current market-specific precedent was found via search — rely on general M&A frameworks and say so explicitly rather than inventing a specific figure.\n\n")
        + "Present ANALYSIS and OPTIONS — never a single prescriptive 'you should do X' "
        "recommendation. Cover relevant valuation approaches (e.g. DCF, comparable "
        "company analysis, precedent transactions) and structural considerations (e.g. "
        "stock vs. asset deal, earn-outs, financing structure) as they apply to THIS "
        "scenario specifically, not as a generic textbook list.\n\n"
        "Respond in exactly this structure:\n\n"
        "SITUATION SUMMARY:\n<2-3 sentences>\n\n"
        "VALUATION & STRUCTURAL OPTIONS:\n<the relevant approaches/structures for this "
        "specific scenario, with trade-offs>\n\n"
        "KEY CONSIDERATIONS:\n<risks, diligence points, or open questions worth "
        "resolving before proceeding>"
    )
    try:
        resp = generate_content(prompt)
        return (resp.text if resp else "").strip() or "Couldn't generate advisory guidance for this."
    except Exception as e:
        print(f"[{AGENT_NAME}] M&A advisory synthesis failed: {e}")
        return f"Search found: {raw}" if raw else f"Hit an error generating guidance: {e}"


def _do_tax_regulatory(p: dict) -> str:
    """
    Phase E — tax & regulatory. ALWAYS live search, no exception — matches
    your own standing instruction to search before stating any Nigerian tax
    position rather than relying on training data, applied here with even
    more force since this agent will be asked repeatedly, not just once.
    Nigerian-primary default `jurisdiction`, expandable like `market` in
    Phase B. Explicitly flags when a situation is complex enough to warrant
    a licensed professional, given real consequences of getting tax wrong —
    same caution posture as investment advice, arguably more so.
    """
    from core.ai_client import generate_content
    from datetime import datetime

    query = (p.get("description") or p.get("query") or "").strip()
    if not query:
        return "What's the tax or regulatory question — a rate, a filing requirement, a recent law change?"
    jurisdiction = (p.get("jurisdiction") or p.get("market") or "Nigeria").strip()
    today = datetime.now().strftime("%B %Y")

    raw = _live_search(
        query,
        angles=[f"{query} {jurisdiction} {today}", f"{query} {jurisdiction} tax regulation current"],
        label="Tax/regulatory",
    )
    if not raw:
        return (
            f"No current information found for '{query}' in {jurisdiction} — try "
            f"rephrasing, or this may need a licensed tax professional's direct input."
        )

    prompt = (
        f"You are a tax/regulatory analyst. Based on the current search results below, "
        f"answer this question for {jurisdiction}: '{query}'\n\n"
        "Present the current position clearly, with practical options/implications where "
        "relevant — never a single prescriptive directive on what the user must do. "
        "Explicitly flag if this situation is complex or high-stakes enough that a "
        "licensed tax professional or lawyer should be consulted directly, rather than "
        "relying solely on this analysis. Be precise about what the search results "
        "actually say versus general principles — don't state a specific rate or "
        "provision unless it's actually supported by the results.\n\n"
        "Respond in exactly this structure:\n\n"
        "CURRENT POSITION:\n<what the search found, with figures/provisions where available>\n\n"
        "PRACTICAL IMPLICATIONS:\n<what this means in practice, as options where relevant>\n\n"
        "CONSULT A PROFESSIONAL IF:\n<when this specific situation genuinely needs "
        "licensed advice beyond this analysis>\n\n"
        f"Search results:\n{raw}"
    )
    try:
        resp = generate_content(prompt)
        return (resp.text if resp else "").strip() or raw
    except Exception as e:
        print(f"[{AGENT_NAME}] Tax/regulatory synthesis failed: {e}")
        return raw


def _format_appraisal(result: dict, rate: float) -> str:
    """Shared by investment_appraisal (caller-supplied cashflows) and
    opportunity_appraisal (LLM-estimated cashflows, still run through the
    same real appraise() math) — one formatting path, one place to get it
    right."""
    lines = [
        f"Investment appraisal (discount rate {rate*100:.1f}%):",
        f"  NPV:  {result['npv']:,.2f}",
        f"  IRR:  {_fmt_pct(result['irr'])}",
        f"  Payback period:  {result['payback_period']:.2f} periods" if result['payback_period'] is not None else "  Payback period:  never recovers within this horizon",
        f"  Discounted payback:  {result['discounted_payback_period']:.2f} periods" if result['discounted_payback_period'] is not None else "  Discounted payback:  never recovers within this horizon",
        f"  Profitability Index:  {result['profitability_index']:.3f}",
    ]
    if "mirr" in result:
        lines.append(f"  MIRR:  {_fmt_pct(result['mirr'])}")
    if "arr" in result:
        lines.append(f"  ARR:  {_fmt_pct(result['arr'])}")

    npv_verdict = "positive — value-accretive at this discount rate" if result['npv'] > 0 else "negative — value-destructive at this discount rate"
    lines.append(f"\nNPV is {npv_verdict}.")
    if result['irr'] is not None:
        vs_rate = "above" if result['irr'] > rate else "below"
        lines.append(f"IRR is {vs_rate} the {rate*100:.1f}% discount rate used.")

    return "\n".join(lines)


def _do_investment_appraisal(p: dict) -> str:
    cashflows = p.get("cashflows")
    if not cashflows or not isinstance(cashflows, list) or len(cashflows) < 2:
        return (
            "I need a cashflow list to appraise — the initial outlay as a negative "
            "number, followed by each period's expected return, e.g. "
            "[-10000, 3000, 3000, 3000, 3000, 3000]."
        )
    try:
        cashflows = [float(x) for x in cashflows]
    except (TypeError, ValueError):
        return "Cashflows must all be numbers."

    rate = p.get("rate")
    if rate is None:
        return "What discount rate should I use (as a percentage, e.g. 10 for 10%)?"
    rate = float(rate) / 100.0 if float(rate) > 1 else float(rate)  # accept 10 or 0.10

    kwargs = {}
    if p.get("finance_rate") is not None and p.get("reinvest_rate") is not None:
        fr, rr = float(p["finance_rate"]), float(p["reinvest_rate"])
        kwargs["finance_rate"] = fr / 100.0 if fr > 1 else fr
        kwargs["reinvest_rate"] = rr / 100.0 if rr > 1 else rr
    if p.get("avg_annual_profit") is not None and p.get("initial_investment") is not None:
        kwargs["avg_annual_profit"] = float(p["avg_annual_profit"])
        kwargs["initial_investment"] = float(p["initial_investment"])

    try:
        result = appraise(cashflows, rate, **kwargs)
    except Exception as e:
        return f"Couldn't compute that — {e}"

    return _format_appraisal(result, rate)


def _do_opportunity_appraisal(p: dict) -> str:
    """
    Takes a described business/income opportunity (not a caller-supplied
    cashflow list — that's investment_appraisal, for when the numbers are
    already known) and produces a real financial appraisal from it. Two
    strictly separate steps, matching this whole file's arithmetic
    discipline:

      1. ESTIMATE (LLM) — turns a plain-language opportunity description
         (optionally with upstream research/viability context from RICS
         and the achievability-scoring pass) into a concrete, conservative
         cashflow projection: an initial outlay and a period-by-period
         return estimate, explicit about the assumptions behind each
         number. This is a legitimate language task — same as EXTRACT in
         statement_interpretation above.
      2. COMPUTE (real code) — those estimated cashflows are run through
         the exact same appraise() used by investment_appraisal. The LLM
         never computes NPV/IRR itself; it only proposes the inputs.

    The result is explicitly a projection built on stated assumptions, not
    a guarantee — the output says so, and lists the assumptions so they
    can be checked/challenged rather than taken on faith.
    """
    from core.ai_client import generate_content

    description = (p.get("description") or p.get("query") or "").strip()
    if not description:
        return "What opportunity should I appraise financially?"
    context = (p.get("context") or "").strip()
    rate = p.get("rate")
    rate = (float(rate) / 100.0 if float(rate) > 1 else float(rate)) if rate is not None else 0.15
    periods = int(p.get("periods") or 12)  # months, by default — most of these are monthly-cadence side income

    estimate_prompt = (
        "You are a conservative financial analyst estimating a cashflow projection for "
        "a proposed income/business opportunity, for someone building this alongside a "
        "full-time job — solo, no existing team, based in Nigeria, international-first "
        "where possible.\n\n"
        f"Opportunity: {description}\n\n"
        + (f"Research/viability context from prior analysis:\n{context}\n\n" if context else "")
        + f"Estimate a {periods}-period (monthly, unless the opportunity clearly implies "
        "another cadence) cashflow projection:\n"
        "- initial_outlay: realistic one-off cost to start (tools, fees, time-value if "
        "material) — as a positive number, we apply the sign\n"
        "- monthly_returns: a list of exactly "
        f"{periods} numbers, the realistic net income expected each period — build in a "
        "ramp-up (early periods lower or zero), do not assume instant full income\n"
        "- assumptions: 2-4 short bullet points stating what these numbers depend on "
        "(pricing assumed, conversion/close rate assumed, hours/week assumed, etc.) so "
        "they can be sanity-checked, not taken on faith\n\n"
        "Be conservative and specific to THIS opportunity, not a generic template. If the "
        "opportunity as described doesn't support a credible estimate (too vague, no real "
        "revenue mechanism), say so honestly in assumptions rather than inventing numbers.\n\n"
        "Respond with ONLY this JSON, no other text, no markdown fences:\n"
        '{"initial_outlay": 0, "monthly_returns": [0, 0, ...], "assumptions": ["...", "..."]}'
    )

    try:
        resp = generate_content(estimate_prompt)
        parsed = _parse_json_block(resp.text if resp else "")
        if not parsed or "monthly_returns" not in parsed:
            raise ValueError("estimation pass did not return usable cashflow JSON")
        initial_outlay = float(parsed.get("initial_outlay") or 0)
        returns = [float(x) for x in parsed["monthly_returns"]]
        assumptions = parsed.get("assumptions") or []
    except Exception as e:
        return (
            f"Couldn't produce a credible financial estimate for this opportunity ({e}). "
            "This usually means the opportunity description is too vague to project "
            "numbers from — worth tightening before a financial appraisal is meaningful."
        )

    cashflows = [-abs(initial_outlay)] + returns
    if len(cashflows) < 2 or all(cf == 0 for cf in cashflows[1:]):
        return (
            "The estimated cashflows for this opportunity are effectively zero — not "
            "enough of a projected return to appraise meaningfully. Recommendation: "
            "do not proceed on financial grounds as currently scoped."
        )

    try:
        result = appraise(cashflows, rate)
    except Exception as e:
        return f"Couldn't compute the appraisal from the estimated cashflows — {e}"

    lines = [
        f"Estimated initial outlay: {initial_outlay:,.2f}",
        f"Estimated {periods}-period return total: {sum(returns):,.2f}",
        "",
        _format_appraisal(result, rate),
    ]
    if assumptions:
        lines.append("\nAssumptions behind these numbers:")
        lines.extend(f"  - {a}" for a in assumptions)

    recommendation = (
        "RECOMMEND pursuing on financial grounds" if result["npv"] > 0
        else "DO NOT recommend on financial grounds as currently scoped"
    )
    lines.append(f"\nFinancial verdict: {recommendation} (NPV {'positive' if result['npv'] > 0 else 'negative'} at {rate*100:.0f}% discount rate).")
    lines.append("This is a projection built on stated assumptions, not a guarantee — sanity-check the assumptions above before treating these figures as fact.")

    return "\n".join(lines)


_ACTIONS = {
    "investment_appraisal": _do_investment_appraisal,
    "opportunity_appraisal": _do_opportunity_appraisal,
    "market_research": _do_market_research,
    "ma_advisory": _do_ma_advisory,
    "tax_regulatory": _do_tax_regulatory,
}

# Handlers that need player/speak (e.g. to delegate to file_processor) are
# dispatched separately from the uniform _ACTIONS above, since forcing one
# call signature onto both would mean either passing unused params to the
# simple ones or losing them for the ones that need them.
_ACTIONS_WITH_IO = {
    "statement_interpretation": _do_statement_interpretation,
}


def fta_agent(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """Single entry point LITE delegates finance/treasury tasks to.
    investment_appraisal (Phase C) and statement_interpretation (Phase A)
    are built so far — other actions return an honest 'not built yet'
    rather than guessing."""
    p = parameters or {}
    action = (p.get("action") or "investment_appraisal").strip().lower()

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

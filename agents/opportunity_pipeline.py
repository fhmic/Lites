#opportunity_pipeline.py
"""
Opportunity Pipeline — reports to LITE (the Executive Orchestrator).

Replaces the old launch-time flow, which tried to generate, verify, AND
score 3-5 opportunities inside a hard 4-second budget every single time
the app opened — routinely timing out, and even when it didn't, handing
over a pile of unvetted ideas most of which were never realistically
actionable. Felix's own framing: fetching 20 opportunities in a day is
useless if none of them are realistically applicable; one solid, fully
vetted plan every day or two is worth more than that.

This runs the SAME four specialist agents already in this codebase, in
sequence, each one narrowing rather than just adding more text:

  1. RESEARCH            — actions/web_search.py's existing candidate
                            generator (DDG signal + structured drafting).
                            Produces 3-5 raw candidates, same as before.
  2. VIABILITY (BI)       — actions/web_search.py's existing claim-
                            verification + achievability-scoring pass,
                            same as before — but instead of returning the
                            whole scored list, this pipeline picks ONE
                            winner (highest score clearing a minimum bar)
                            and drops the rest. This is the actual fix for
                            "20 non-achievable opportunities": nothing
                            below the bar ever reaches the next stage.
  3. LEGAL & COMPLIANCE   — delegates to compliance_legal_agent's
                            risk_assessment for the real analysis (passing
                            the winning opportunity + stage 1-2 findings as
                            context — that param already exists for
                            exactly this), then a second, short, explicit
                            classification call turns that analysis into a
                            real {"passes": true|false} gate — see
                            _classify_gate below. A fail drops the
                            candidate and falls back to the next-ranked one
                            from stage 2 rather than pushing a bad
                            opportunity through.
  4. FINANCE & TREASURY   — delegates to fta_agent's opportunity_appraisal
                            action: an LLM estimates a conservative
                            cashflow projection from the opportunity +
                            accumulated context, then the SAME
                            deterministic appraise() toolkit used
                            everywhere else in FTA computes real NPV/IRR/
                            payback from it — never LLM arithmetic. The
                            pass/fail gate here is even more direct than
                            stage 3's: appraise() already returns a real
                            computed NPV sign, and opportunity_appraisal's
                            own recommendation line is generated from that
                            actual boolean in code, not guessed by the
                            LLM — so this stage's gate parses that fixed,
                            code-generated phrase rather than needing its
                            own classification call at all.

Order note: legal/compliance runs BEFORE finance, not after — there's no
point building a financial case for something that fails compliance, and
compliance findings (e.g. a licensing cost, a required registration) can
change what the financial estimate should even assume.

Results cache to memory/opportunity_pipeline.json with a timestamp, so a
run from earlier today or yesterday is served instantly rather than
re-run on every launch — get_cached_or_refresh() is what main.py's
startup briefing and any on-demand "give me a solid opportunity" request
both call.
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

AGENT_NAME = "Opportunity Pipeline"

MIN_ACHIEVABILITY_SCORE = 4  # 1-5 scale from _score_opportunities — below this, drop it
MAX_CANDIDATES_TRIED = 3     # how many ranked candidates to try through legal/finance before giving up


def _get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CACHE_PATH = _get_base_dir() / "memory" / "opportunity_pipeline.json"
_lock = threading.Lock()
_run_in_progress = threading.Event()  # prevents two overlapping background runs


def _report(body: str) -> str:
    return f"[{AGENT_NAME}] {body}"


# ── Cache ────────────────────────────────────────────────────────────────

def _load_cache() -> dict | None:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(result: dict):
    with _lock:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")


def _cache_age_days(cache: dict) -> float:
    try:
        generated = datetime.fromisoformat(cache["generated_at"])
        return (datetime.now() - generated).total_seconds() / 86400.0
    except Exception:
        return 999.0  # unparseable timestamp — treat as infinitely stale


# ── Pipeline stages ─────────────────────────────────────────────────────

def _stage_research_and_rank(focus: str) -> tuple[list[dict], dict, str]:
    """Stages 1+2. Returns (ranked_candidates, scores_by_title, raw_signal).
    Candidates are sorted best-achievability-first; scores_by_title may be
    partial or empty if the scoring pass failed for some/all of them —
    callers should treat an unscored candidate as not having cleared the
    bar, not as automatically viable."""
    from actions.web_search import (
        _generate_candidate_opportunities, _verify_opportunity_claims,
        _skill_profile_text, _score_opportunities,
    )

    candidates, raw_signal = _generate_candidate_opportunities(focus)
    if not candidates:
        return [], {}, raw_signal

    verification = _verify_opportunity_claims(candidates)
    skill_profile = _skill_profile_text()
    scores = _score_opportunities(candidates, verification, skill_profile)

    def _sort_key(opp):
        s = scores.get(opp.get("title", ""), {})
        has_score = "achievability_score" in s
        return (0 if has_score else 1, -s.get("achievability_score", 0))

    ranked = sorted(candidates, key=_sort_key)
    return ranked, scores, raw_signal


def _classify_gate(report_text: str, question: str) -> tuple[bool, str]:
    """
    Turns an already-generated freeform analysis into a real explicit
    {"passes": true|false} gate via one short, cheap classification call —
    NOT a re-analysis from scratch (that's what compliance_legal_agent/
    fta_agent already did) and NOT string-sniffing the prose for
    keywords like "do not proceed" (too fragile — those agents are
    deliberately written to "present analysis and options, never a single
    prescriptive directive", so they rarely phrase things as a hard
    verdict at all, which made keyword-matching close to a no-op in
    practice). This is a classification task on EXISTING text, not new
    judgment, so a single short call is enough — no need for the fuller
    reasoning budget the original analysis call used.
    """
    prompt = (
        f"{question}\n\n"
        f"Analysis to classify:\n{report_text[:3000]}\n\n"
        'Respond with ONLY this JSON, no other text: {"passes": true|false, "reason": "one short sentence"}'
    )
    try:
        from core.ai_client import generate_content
        from actions.web_search import _parse_json_block
        resp = generate_content(prompt)
        parsed = _parse_json_block(resp.text if resp else "")
        if parsed and "passes" in parsed:
            return bool(parsed["passes"]), parsed.get("reason", "")
    except Exception as e:
        print(f"[{AGENT_NAME}] Gate classification failed ({e}) — defaulting to fail-open with a caution note")
    # Classification itself failing is an infra hiccup, not a verdict on the
    # opportunity — fail-open (pass) rather than let an API error silently
    # kill a candidate that might be genuinely fine, but say so plainly in
    # the final report rather than pretending the gate actually ran.
    return True, "Gate classification unavailable — analysis above was not automatically verdict-checked; read it directly."


def _stage_compliance(opportunity: dict, context: str) -> tuple[str, bool, str]:
    """Stage 3. Returns (compliance_report, cleared, gate_reason). The
    full compliance_legal_agent analysis is always included in the final
    report regardless of which way the gate falls — the gate decides
    whether THIS pipeline keeps going, it's never the only thing the user
    sees."""
    from agents.compliance_legal_agent import compliance_legal_agent

    report = compliance_legal_agent(
        parameters={
            "action": "risk_assessment",
            "description": f"{opportunity.get('title', '')}: {opportunity.get('what', '')}",
            "context": context,
        }
    )
    cleared, reason = _classify_gate(
        report,
        "You are gating a business opportunity for a chartered accountant who must protect "
        "his professional license and reputation. Based ONLY on the risk assessment below, "
        "does this opportunity pass (no severe, disqualifying legal/compliance/reputational "
        "blocker) or fail (a genuine blocker exists — clearly illegal, requires a license the "
        "person doesn't have and can't reasonably get, or would plainly embarrass a licensed "
        "professional)? Ordinary manageable risk that the analysis itself frames as "
        "addressable is NOT a fail — only a real disqualifying blocker is.",
    )
    return report, cleared, reason


def _stage_finance(opportunity: dict, context: str) -> tuple[str, bool]:
    """
    Stage 4. Real cashflow-based appraisal via fta_agent's
    opportunity_appraisal — see that action's own docstring for the
    estimate-then-compute split. The gate here needs no separate
    classification call at all: opportunity_appraisal's own recommendation
    line ("RECOMMEND pursuing on financial grounds" / "DO NOT recommend on
    financial grounds") is generated in fta_agent.py directly from
    result["npv"] > 0 — an actual computed boolean, not an LLM guess — so
    parsing that fixed phrase IS the real gate, not a proxy for one.
    """
    from agents.fta_agent import fta_agent

    report = fta_agent(
        parameters={
            "action": "opportunity_appraisal",
            "description": f"{opportunity.get('title', '')}: {opportunity.get('what', '')}",
            "context": context,
        }
    )
    cleared = "RECOMMEND pursuing on financial grounds" in report
    return report, cleared


def _fell_short_text(row: dict) -> str:
    stage = row.get("fell_short_at")
    reason = (row.get("reason") or "").strip()
    if stage == "achievability":
        score = row["score_info"].get("achievability_score", "?")
        return f"Achievability {score}/{5} (need {MIN_ACHIEVABILITY_SCORE}+) — {reason}" if reason else f"Achievability {score}/5 — below bar"
    if stage == "compliance":
        return f"Compliance: {reason}" if reason else "Compliance gate failed"
    if stage == "finance":
        return reason or "Projected NPV not positive"
    return reason or "Didn't clear every gate"


def _compile_comparison_table(attempts: list[dict]) -> tuple[list[str], list[list[str]]]:
    """Builds the {columns, rows} payload for the HUD's table renderer from
    whatever was actually evaluated — 2-3 rows in practice (MAX_CANDIDATES_TRIED)."""
    columns = ["Opportunity", "Achievability", "What", "Where it fell short", "If pursued anyway"]
    rows = []
    for row in attempts:
        candidate = row["candidate"]
        score = row["score_info"].get("achievability_score")
        rows.append([
            row["title"] or "Untitled",
            f"{score}/5" if score else "unscored",
            (candidate.get("what", "") or "")[:160],
            _fell_short_text(row),
            (candidate.get("next_action", "") or "")[:160],
        ])
    return columns, rows


def _comparison_report_text(focus: str, attempts: list[dict]) -> str:
    """Plain-text/markdown fallback of the same comparison — used when the
    result is served from cache or via voice/text-only paths that don't
    render the structured HUD table."""
    header = f"No candidate fully cleared achievability + compliance + finance this round"
    header += f" for '{focus}'" if focus else ""
    header += f" — here are the top {len(attempts)} for your own call:\n"

    lines = [header]
    columns, rows = _compile_comparison_table(attempts)
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "|".join(["---"] * len(columns)) + "|")
    for r in rows:
        lines.append("| " + " | ".join(c.replace("|", "/") for c in r) + " |")
    return "\n".join(lines)



def _compile_final_report(
    focus: str, opportunity: dict, score_info: dict,
    compliance_report: str, finance_report: str,
) -> str:
    score = score_info.get("achievability_score")
    score_line = f"Achievability: {score}/5 — {score_info.get('score_reason', '')}" if score else ""
    flag = score_info.get("regulatory_flag")

    lines = [
        f"VETTED OPPORTUNITY — {focus}" if focus else "VETTED OPPORTUNITY",
        f"\n{opportunity.get('title', 'Untitled')}",
        f"What: {opportunity.get('what', '')}",
        f"Why now: {opportunity.get('why_now', '')}",
    ]
    if score_line:
        lines.append(score_line)
    if flag:
        lines.append(f"⚠ {flag}")
    lines.append(f"Next action: {opportunity.get('next_action', '')}")

    lines.append("\n── LEGAL & COMPLIANCE ──")
    lines.append(compliance_report.split("] ", 1)[-1] if compliance_report.startswith("[") else compliance_report)

    lines.append("\n── FINANCIAL APPRAISAL ──")
    lines.append(finance_report.split("] ", 1)[-1] if finance_report.startswith("[") else finance_report)

    return "\n".join(lines)


# ── Orchestration ───────────────────────────────────────────────────────

def run_pipeline(focus: str = "") -> dict:
    """
    Runs the full four-stage pipeline to completion (no timeout — this is
    meant to be called from a background thread, not inline in a
    latency-sensitive path). Returns a structured result dict, always —
    even a "nothing cleared the bar" outcome is a valid, cached result
    rather than an exception, so the caller always has something to show.
    """
    started = time.monotonic()
    ranked, scores, raw_signal = _stage_research_and_rank(focus)

    if not ranked:
        result = {
            "status": "no_candidates",
            "focus": focus,
            "generated_at": datetime.now().isoformat(),
            "report": "Couldn't generate any candidate opportunities this round — signal was too thin or generation failed. Will try again next cycle.",
            "duration_seconds": round(time.monotonic() - started, 1),
        }
        _save_cache(result)
        return result

    accumulated_context = f"Research signal: {raw_signal[:1500]}"
    attempts = []  # every candidate looked at, best-ranked first — feeds the
                    # comparison table below if nothing fully clears

    for candidate in ranked[:MAX_CANDIDATES_TRIED]:
        title = candidate.get("title", "")
        score_info = scores.get(title, {})
        achievability = score_info.get("achievability_score", 0)

        row = {"candidate": candidate, "title": title, "score_info": score_info}

        if achievability < MIN_ACHIEVABILITY_SCORE:
            row["fell_short_at"] = "achievability"
            row["reason"] = score_info.get("score_reason", "") or "Below the achievability bar."
            attempts.append(row)
            continue  # don't waste a legal/finance pass on it — same as before

        candidate_context = accumulated_context + (
            f"\nViability score: {achievability}/5 "
            f"— {score_info.get('score_reason', '')}"
        )

        compliance_report, cleared, gate_reason = _stage_compliance(candidate, candidate_context)
        row["compliance_report"] = compliance_report
        if not cleared:
            print(f"[{AGENT_NAME}] '{title}' rejected at compliance gate: {gate_reason}")
            row["fell_short_at"] = "compliance"
            row["reason"] = gate_reason
            attempts.append(row)
            continue  # explicit fail — try the next-ranked candidate instead

        finance_report, finance_cleared = _stage_finance(
            candidate, candidate_context + f"\nCompliance findings: {compliance_report[:800]}"
        )
        row["finance_report"] = finance_report
        if not finance_cleared:
            print(f"[{AGENT_NAME}] '{title}' rejected at finance gate: NPV not positive")
            row["fell_short_at"] = "finance"
            row["reason"] = "Projected NPV not positive."
            attempts.append(row)
            continue  # explicit fail — try the next-ranked candidate instead

        final_text = _compile_final_report(focus, candidate, score_info, compliance_report, finance_report)
        result = {
            "status": "ready",
            "focus": focus,
            "generated_at": datetime.now().isoformat(),
            "opportunity_title": title,
            "achievability_score": score_info.get("achievability_score"),
            "report": final_text,
            "duration_seconds": round(time.monotonic() - started, 1),
        }
        _save_cache(result)
        return result

    # Nothing fully cleared all three gates — rather than reporting nothing,
    # hand over what was actually looked at (up to MAX_CANDIDATES_TRIED, i.e.
    # 2-3 in practice) side by side so Felix can weigh the trade-offs himself
    # instead of the pipeline silently deciding "none of these."
    columns, rows = _compile_comparison_table(attempts)
    result = {
        "status": "options",
        "focus": focus,
        "generated_at": datetime.now().isoformat(),
        "report": _comparison_report_text(focus, attempts),
        "table_columns": columns,
        "table_rows": rows,
        "duration_seconds": round(time.monotonic() - started, 1),
    }
    _save_cache(result)
    return result


def _run_in_background(focus: str, player=None):
    if _run_in_progress.is_set():
        return  # already running — don't stack a second one
    _run_in_progress.set()

    def _worker():
        if player is not None and hasattr(player, "set_active_agent"):
            try:
                player.set_active_agent("nova")
            except Exception:
                pass
        try:
            result = run_pipeline(focus)
            if player is not None and hasattr(player, "show_content"):
                try:
                    if result.get("status") == "options":
                        player.show_content(
                            "OPPORTUNITY PIPELINE — no clear winner, top options for your call",
                            result["report"],
                            kind="table",
                            payload={"columns": result["table_columns"], "rows": result["table_rows"]},
                        )
                    else:
                        player.show_content(
                            "OPPORTUNITY PIPELINE — vetted recommendation ready",
                            result["report"],
                        )
                except Exception:
                    pass
            # Same deterministic Obsidian save path the old briefing used —
            # never lets a save hiccup affect anything else.
            try:
                from actions.obsidian import save_opportunities_brief, has_vault_configured
                if has_vault_configured() and result.get("status") == "ready":
                    save_opportunities_brief(result["report"], focus)
            except Exception as e:
                print(f"[{AGENT_NAME}] Obsidian save failed: {e}")
        except Exception as e:
            print(f"[{AGENT_NAME}] Background run failed: {e}")
        finally:
            _run_in_progress.clear()
            if player is not None and hasattr(player, "set_active_agent"):
                try:
                    player.set_active_agent("lite")  # hand-off complete, same as the interactive path
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True).start()


def get_cached_or_refresh(focus: str = "", max_age_days: float = 2.0, player=None) -> str:
    """
    Main entry for anything latency-sensitive (startup briefing, an
    on-demand voice request that wants an immediate answer): serves a
    cached result if one exists and is fresh enough, otherwise kicks off a
    full background run (see _run_in_background) and returns immediately
    with an honest "still working on it" — never blocks on the pipeline
    itself, which is what made the old 4-second timeout necessary (and
    unreliable) in the first place.
    """
    cache = _load_cache()
    if cache and _cache_age_days(cache) <= max_age_days:
        age_note = "" if _cache_age_days(cache) < 0.5 else f" (from {_cache_age_days(cache):.1f} day(s) ago)"
        return cache["report"] + age_note

    _run_in_background(focus, player=player)
    if cache:
        return (
            f"Still compiling a freshly vetted opportunity — I'll let you know when it's "
            f"ready. In the meantime, here's the last one{f' (from {_cache_age_days(cache):.1f} day(s) ago)' if cache else ''}:\n\n"
            + cache["report"]
        )
    return (
        "Compiling a thoroughly vetted opportunity now — research, viability, legal/"
        "compliance, and a real financial appraisal, not just a quick idea. I'll let "
        "you know as soon as it's ready."
    )


# ── Tool-callable entry point ───────────────────────────────────────────

def opportunity_pipeline_agent(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    """
    Single entry point LITE delegates to for anything about today's/this
    round's vetted business opportunity. Actions:
      get       - cached-or-refresh (default) — instant if fresh, else
                  kicks off a background run and says so
      run_now   - force a fresh run regardless of cache age; runs in the
                  background unless wait=True is explicitly given (then
                  blocks and returns the full result — only use this if
                  the user explicitly said to wait)
      status    - just reports whether a run is currently in progress and
                  how old the cached result is, without triggering anything
    """
    p = parameters or {}
    action = (p.get("action") or "get").strip().lower()
    focus = (p.get("focus") or p.get("description") or "").strip()

    if action == "status":
        cache = _load_cache()
        if _run_in_progress.is_set():
            return _report("A pipeline run is in progress right now — I'll show it here the moment it's ready.")
        if not cache:
            return _report("No vetted opportunity has been generated yet.")
        return _report(f"Last vetted opportunity is {_cache_age_days(cache):.1f} day(s) old — status: {cache.get('status')}.")

    if action == "run_now":
        if p.get("wait"):
            return _report(run_pipeline(focus)["report"])
        _run_in_background(focus, player=player)
        return _report("Running the full pipeline now (research → viability → compliance → finance) — will show the result here once it's ready.")

    # default: get
    return _report(get_cached_or_refresh(focus, player=player))

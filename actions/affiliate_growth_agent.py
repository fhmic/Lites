#affiliate_growth_agent.py
"""
Affiliate Marketing Social Media Growth Agent — an autonomous subagent that
reports to LITE. Routes through the same shared AI client every other action
uses (core.ai_client.generate_content), so it automatically inherits LITE's
Gemini -> Claude -> Groq -> custom fallback chain and cooldown behaviour —
no separate API key handling here.

Every response follows the agent's fixed persona (SYSTEM_PROMPT below) and
always ends with a "## LITE EXECUTIVE SUMMARY" block (Opportunity /
Recommended Action / Expected ROI / Risk Level / Priority / Next Actions),
which this module parses into a dict so the calling code (or a future
dashboard widget) can act on `priority` / `risk_level` without re-parsing
free text.

Call shape matches every other action module (dev_agent, self_maintain,
flight_finder, ...):

    def affiliate_growth_agent(parameters: dict, player=None, speak=None) -> str

Actions (parameters["action"]):
    rank_offers        - rank affiliate offers for a niche
    research_audience   - target-audience research for a niche
    growth_strategy     - social media growth strategy
    content_calendar    - weekly/monthly content calendar
    post_ideas          - platform-specific post ideas
    video_script         - short-form video script
    email_sequence      - affiliate nurture/conversion email sequence
    landing_page        - landing page copy
    ad_copy             - paid ad copy
    performance_review  - review metrics against the success-metrics ladder
    custom              - free-form task, uses "task" parameter verbatim

Several further actions talk to GAS (Growth Agent Service) — a small
Cloudflare Worker + Supabase deployment, in gas/ — that keeps working
affiliate offers even while your laptop is off:

    assign_job     - hand the agent a standing job (niche/platforms/cadence)
                     it will keep working on its own via a cron trigger
    get_report     - pull the "while you were away" report: drafts written,
                     leads, conversions, earnings delta since you last checked
                     (also pulls pending drafts into the review queue below)
    generate_ads   - one-off, on-demand generation from a free-text idea —
                     "generate an ad for X" — separate from the standing
                     6h auto-generator, no job/cadence created
    list_queue     - re-show pending drafts (optionally filter by status)
    edit_draft     - change a draft's title/body/tracking_subid/platform/
                     content_type before approval
    approve_draft  - mark a draft approved (posting itself stays manual)
    reject_draft   - mark a draft rejected (stays in the database)
    delete_draft   - permanently remove a draft from the database (not a
                     status flag — this is gone for good, R2 media cleaned
                     up too)
    download_draft - save a draft (text, plus any rendered video/images/
                     narration audio) to a local folder

These require gas_worker_url / gas_worker_key in config/api_keys.json (see
config/api_keys.example.json). Everything else in this file works with no
extra config, same as before.

See bottom of file for the exact main.py FUNCTION_DECLARATIONS entry and
dispatch snippet needed to wire this in (kept out of main.py itself so this
stays a self-contained, reviewable diff).
"""
import json
import re
import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR     = get_base_dir()
LOG_PATH     = BASE_DIR / "memory" / "affiliate_growth_agent_log.jsonl"
MODEL_NAME   = "gemini-3.5-flash"   # passed through to generate_content; the
                                     # shared client still falls back to
                                     # Claude/Groq/custom if Gemini fails
WORKER_TIMEOUT = 20   # seconds — the cloud service does the slow work async;
                       # these calls should just be reads/writes against Supabase


# ── Persona ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """# AGENT NAME
Affiliate Marketing Social Media Growth Agent

# ROLE
You are an autonomous Social Media Marketing Expert reporting directly to LITE.

Your mission is to build, grow, and optimise profitable affiliate marketing
businesses through strategic social media marketing, audience growth, content
creation, lead generation, conversion optimisation, and performance analytics.

You think like a world-class combination of:
- Affiliate Marketing Director
- Social Media Strategist
- Content Marketing Expert
- Performance Marketer
- Copywriter
- Community Builder
- Digital Growth Consultant

Your sole objective is to maximise long-term affiliate revenue while building
trusted audiences.

# PRIMARY RESPONSIBILITIES
1. Identify profitable affiliate marketing opportunities.
2. Analyse affiliate offers and rank them by potential.
3. Research target audiences.
4. Create social media growth strategies.
5. Design content calendars.
6. Generate high-converting content ideas.
7. Write platform-specific posts.
8. Optimise engagement and conversion rates.
9. Monitor performance metrics.
10. Recommend experiments and improvements.
11. Report findings and recommendations to LITE.

# SUCCESS METRICS
Prioritise, in order:
1. Affiliate revenue
2. Qualified leads generated
3. Conversion rate
4. Click-through rate
5. Audience growth
6. Engagement rate
7. Email list growth
8. Cost efficiency

Never optimise vanity metrics at the expense of revenue.

# OPERATING PRINCIPLES
You must:
- Think strategically before acting.
- Use data-driven reasoning.
- Focus on ROI.
- Continuously test assumptions.
- Look for leverage opportunities.
- Recommend automation whenever practical.
- Prioritise sustainable audience trust.

You must challenge weak ideas and explain why better alternatives exist.
Do not blindly agree with requests.

# SPECIAL EXPERTISE
Affiliate marketing, social media marketing, influencer marketing, personal
branding, community building, content strategy, short-form video marketing,
SEO, email marketing, marketing funnels, conversion optimisation, behavioural
psychology, consumer decision-making, analytics.

# SOCIAL PLATFORMS
Develop strategies for: Facebook, Instagram, TikTok, X, LinkedIn, YouTube,
YouTube Shorts, Pinterest, Threads. Recommend the best platforms based on
audience behaviour rather than defaulting to all of them.

# PLATFORM FORMAT NOTES
Do not write every platform like it's TikTok. In particular:
- TikTok / Instagram Reels / YouTube Shorts: casual tone, fast hook in the
  first 1-2 seconds, trend-aware language is fine, hard CTA (link in bio,
  swipe up, follow for part 2) is expected and works.
- LinkedIn: professional register, no slang or trend-audio references. Open
  with a credibility or insight statement rather than a shock hook. Keep the
  CTA soft — invite a comment, connection, or DM rather than pushing a
  direct sale. Native video should run 30-90s, square or vertical, and lead
  with value before any mention of an offer.
Match the platform given in the task; don't default to TikTok voice for
every piece just because that's the more common niche.

Note: this action module (LITE's live chat path) always returns text —
video_script here is a written script only. Real, rendered video only
happens on GAS's overnight pipeline (assign_job with content_type 'video'
in the underlying job), since actual rendering takes minutes and can't
happen inline in a conversation. Use get_report / list_queue to see
video_url / video_status once GAS has produced one.

# CONTENT RESPONSIBILITIES
When creating content:
1. Identify audience pain points.
2. Create attention-grabbing hooks.
3. Increase curiosity.
4. Deliver value.
5. Build authority.
6. Generate trust.
7. Include a clear CTA.

Generate as requested: post ideas, reels ideas, video scripts, carousel
posts, lead magnets, email sequences, ads, landing page copy, community
engagement prompts.

# MARKET RESEARCH FRAMEWORK
Before recommending any campaign, analyse: audience demographics, audience
psychology, competitor activity, trending topics, affiliate commission
levels, product-market fit, market saturation, traffic opportunities.
Provide evidence-based conclusions, and flag explicitly when you are
reasoning from general knowledge rather than current data because you have
no live data feed.

# CONTENT CALENDAR FRAMEWORK
When asked to produce a content plan, provide: monthly strategy, weekly
themes, daily content ideas, CTA strategy, content objectives, expected
outcomes.

# DECISION FRAMEWORK
For every recommendation, state:
1. Objective
2. Reasoning
3. Expected benefit
4. Potential risks
5. Success metrics

# REPORTING TO LITE
Every deliverable must end with exactly this block, verbatim headers, so it
can be parsed programmatically:

## LITE EXECUTIVE SUMMARY

Opportunity:
[Summary]

Recommended Action:
[Summary]

Expected ROI:
[Estimate]

Risk Level:
[Low/Medium/High]

Priority:
[Critical/High/Medium/Low]

Next Actions:
1.
2.
3.

# AUTONOMOUS BEHAVIOUR
If information is incomplete:
- Make reasonable assumptions.
- State assumptions clearly, labelled "Assumptions:".
- Continue working.
- Present alternatives where needed.
Do not stop merely because some information is missing.

# AFFILIATE MARKETING OBJECTIVE
The ultimate goal is to build multiple scalable affiliate marketing income
streams that can generate sustainable side income and eventually become a
significant revenue source.

Every recommendation should be evaluated against:
"Will this increase revenue, audience trust, and long-term scalability?"
If not, reject it and propose a better alternative.
"""


# ── LITE EXECUTIVE SUMMARY parsing ──────────────────────────────────────

_SUMMARY_BLOCK_RE = re.compile(r"##\s*LITE EXECUTIVE SUMMARY\s*(.*)", re.IGNORECASE | re.DOTALL)
_FIELD_RE = re.compile(
    r"(Opportunity|Recommended Action|Expected ROI|Risk Level|Priority)\s*:\s*(.*?)"
    r"(?=\n(?:Opportunity|Recommended Action|Expected ROI|Risk Level|Priority|Next Actions)\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_NEXT_ACTIONS_RE = re.compile(r"Next Actions\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)


def _parse_lite_summary(text: str) -> dict:
    """Extract the mandatory LITE EXECUTIVE SUMMARY block into a dict.
    Tolerant of minor formatting drift — falls back to empty fields rather
    than raising, since a subagent formatting slip should never crash the
    main assistant loop."""
    summary = {
        "opportunity": "", "recommended_action": "", "expected_roi": "",
        "risk_level": "", "priority": "", "next_actions": [],
    }
    block_match = _SUMMARY_BLOCK_RE.search(text)
    block = block_match.group(1) if block_match else text

    key_map = {
        "opportunity": "opportunity",
        "recommended action": "recommended_action",
        "expected roi": "expected_roi",
        "risk level": "risk_level",
        "priority": "priority",
    }
    for match in _FIELD_RE.finditer(block):
        label = match.group(1).strip().lower()
        summary[key_map[label]] = match.group(2).strip()

    actions_match = _NEXT_ACTIONS_RE.search(block)
    if actions_match:
        for ln in actions_match.group(1).splitlines():
            cleaned = re.sub(r"^\d+[\.\)]\s*", "", ln.strip()).strip()
            if cleaned:
                summary["next_actions"].append(cleaned)
    return summary


def _log_run(action: str, task: str, ok: bool, summary: dict, error: str = "") -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "action": action, "task": task, "ok": ok,
            "summary": summary, "error": error,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass  # logging must never break the agent


# ── Task builders (one per PRIMARY RESPONSIBILITY) ──────────────────────

def _task_rank_offers(p: dict) -> str:
    niche  = p.get("niche", "").strip()
    offers = p.get("offers", "")
    return (
        f"Analyse and rank affiliate offers for the '{niche}' niche by "
        f"revenue potential, applying the Market Research Framework "
        f"(commission level, product-market fit, saturation, traffic "
        f"opportunity). Give a ranked shortlist with reasoning.\n\n"
        f"Offers:\n{offers}"
    )


def _task_research_audience(p: dict) -> str:
    niche = p.get("niche", "").strip()
    return f"Research the target audience for the '{niche}' affiliate niche."


def _task_growth_strategy(p: dict) -> str:
    niche     = p.get("niche", "").strip()
    platforms = p.get("platforms", "").strip()
    budget    = p.get("budget_notes", "").strip()
    task = f"Design a social media growth strategy for a '{niche}' affiliate business."
    task += (f" Prioritise these platforms: {platforms}." if platforms
             else " Recommend the best-fit platforms yourself and justify the choice.")
    if budget:
        task += f" Budget/resourcing notes: {budget}"
    return task


def _task_content_calendar(p: dict) -> str:
    niche     = p.get("niche", "").strip()
    weeks     = p.get("weeks", 4)
    platforms = p.get("platforms", "").strip()
    task = (
        f"Produce a {weeks}-week content calendar for the '{niche}' "
        f"affiliate business, following the Content Calendar Framework "
        f"(monthly strategy, weekly themes, daily content ideas, CTA "
        f"strategy, objectives, expected outcomes)."
    )
    if platforms:
        task += f" Platforms: {platforms}."
    return task


def _task_post_ideas(p: dict) -> str:
    niche    = p.get("niche", "").strip()
    platform = p.get("platform", "Instagram").strip()
    count    = p.get("count", 10)
    return f"Generate {count} high-converting {platform} post ideas for the '{niche}' niche."


def _task_video_script(p: dict) -> str:
    niche    = p.get("niche", "").strip()
    platform = p.get("platform", "TikTok").strip()
    angle    = p.get("angle", "").strip()
    return (
        f"Write a short-form video script for {platform} in the '{niche}' "
        f"niche, angle: '{angle}'. Include hook, value, CTA, and an "
        f"estimated runtime."
    )


def _task_email_sequence(p: dict) -> str:
    niche       = p.get("niche", "").strip()
    goal        = p.get("goal", "convert to sale").strip()
    num_emails  = p.get("num_emails", 5)
    return (
        f"Write a {num_emails}-email nurture/conversion sequence for the "
        f"'{niche}' affiliate offer. Goal: {goal}. Include subject lines "
        f"and CTAs for each email."
    )


def _task_landing_page(p: dict) -> str:
    niche = p.get("niche", "").strip()
    offer = p.get("offer", "").strip()
    return f"Write landing page copy for the '{niche}' offer: {offer}."


def _task_ad_copy(p: dict) -> str:
    niche    = p.get("niche", "").strip()
    offer    = p.get("offer", "").strip()
    platform = p.get("platform", "Facebook").strip()
    return f"Write {platform} ad copy (multiple variants) for the '{niche}' offer: {offer}."


def _task_performance_review(p: dict) -> str:
    metrics = p.get("metrics", "")
    return (
        "Review the following performance metrics against the Success "
        "Metrics priority order (revenue > qualified leads > conversion "
        "rate > CTR > audience growth > engagement rate > email list "
        "growth > cost efficiency). Identify what is underperforming, "
        "propose experiments, and flag anything that looks like "
        f"vanity-metric optimisation.\n\nMetrics:\n{metrics}"
    )


def _handle_delete_draft(p: dict) -> str:
    item_id = (p.get("item_id") or p.get("id") or "").strip()
    if not item_id:
        return "Which draft should I delete, sir? Give me its id."
    try:
        _worker_request("DELETE", f"/queue/{item_id}")
    except Exception as e:
        return f"Couldn't delete that draft: {e}"
    return f"Deleted draft [{item_id}] permanently, sir — it's gone from the database, not just marked rejected."


def _is_real_url(u) -> bool:
    # GAS's fallback pipeline stores a "r2-key:...(set MEDIA_PUBLIC_BASE_URL...)"
    # placeholder when no public R2 URL is configured yet — that's not
    # something requests.get() can fetch, so it needs its own message
    # rather than a confusing download failure.
    return isinstance(u, str) and u.startswith(("http://", "https://"))


def _handle_download_draft(p: dict) -> str:
    """Fetches one draft's full row from GAS and writes it — plus any real
    rendered media it has — to a local folder. Runs off the Qt UI thread
    (ui.py wraps this call in a background thread), so a slow download
    never freezes the HUD."""
    import requests

    item_id = (p.get("item_id") or p.get("id") or "").strip()
    if not item_id:
        return "Which draft should I download, sir? Give me its id."
    save_dir = (p.get("save_dir") or "").strip() or str(Path.home() / "Downloads")
    try:
        Path(save_dir).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"Couldn't use '{save_dir}' as a save location: {e}"

    try:
        result = _worker_request("GET", f"/queue/{item_id}")
    except Exception as e:
        return f"Couldn't fetch that draft to download it: {e}"

    item = result.get("item")
    if not item:
        return f"No draft found with id [{item_id}], sir."

    base = f"{item.get('platform','draft')}_{item.get('content_type','')}_{item_id[:8]}".replace(" ", "-")
    saved: list[str] = []
    errors: list[str] = []

    text_path = Path(save_dir) / f"{base}.txt"
    try:
        text_path.write_text(
            f"Title: {item.get('title','')}\n"
            f"Platform: {item.get('platform','')}\n"
            f"Content type: {item.get('content_type','')}\n"
            f"Tracking subid: {item.get('tracking_subid','')}\n\n"
            f"{item.get('body','')}\n",
            encoding="utf-8",
        )
        saved.append(text_path.name)
    except Exception as e:
        errors.append(f"text: {e}")

    video_url = item.get("video_url")
    if _is_real_url(video_url):
        try:
            resp = requests.get(video_url, timeout=60)
            resp.raise_for_status()
            video_path = Path(save_dir) / f"{base}_video.mp4"
            video_path.write_bytes(resp.content)
            saved.append(video_path.name)
        except Exception as e:
            errors.append(f"video: {e}")

    assets = item.get("video_assets") or {}
    for i, img_url in enumerate(assets.get("images") or []):
        if not _is_real_url(img_url):
            continue
        try:
            resp = requests.get(img_url, timeout=30)
            resp.raise_for_status()
            img_path = Path(save_dir) / f"{base}_scene-{i}.png"
            img_path.write_bytes(resp.content)
            saved.append(img_path.name)
        except Exception as e:
            errors.append(f"scene {i} image: {e}")

    audio_url = assets.get("audio_url")
    if _is_real_url(audio_url):
        try:
            resp = requests.get(audio_url, timeout=30)
            resp.raise_for_status()
            audio_path = Path(save_dir) / f"{base}_narration.wav"
            audio_path.write_bytes(resp.content)
            saved.append(audio_path.name)
        except Exception as e:
            errors.append(f"narration audio: {e}")

    if assets.get("captions"):
        try:
            cap_path = Path(save_dir) / f"{base}_captions.txt"
            cap_path.write_text("\n".join(assets["captions"]), encoding="utf-8")
            saved.append(cap_path.name)
        except Exception as e:
            errors.append(f"captions: {e}")

    if not saved:
        return f"Couldn't save anything for draft [{item_id}]: {'; '.join(errors) or 'unknown error'}"

    msg = f"Saved {len(saved)} file(s) to {save_dir}: {', '.join(saved)}."
    if errors:
        msg += f" ({len(errors)} issue(s): {'; '.join(errors)})"
    return msg


def _handle_generate_ads(p: dict) -> str:
    """On-demand generation from a one-off idea — separate from the
    standing 6h auto-generator. No job/cadence created; this fires once."""
    description = (p.get("description") or p.get("idea") or p.get("task") or "").strip()
    if not description:
        return "What should the ad/content be about, sir? Give me a short description of the idea."

    body: dict = {"description": description}
    platforms = [s.strip() for s in (p.get("platforms") or "").split(",") if s.strip()]
    if platforms:
        body["platforms"] = platforms
    if p.get("count"):
        body["count"] = int(p["count"])
    if p.get("content_type"):
        body["content_type"] = p["content_type"]

    try:
        result = _worker_request("POST", "/generate", body)
    except Exception as e:
        return f"Couldn't reach the overnight agent to generate that: {e}"

    if not result.get("ok"):
        return f"Generation didn't complete cleanly: {result.get('error','unknown error')}"

    return f"Generated {result.get('draftsCreated', 0)} piece(s) for that idea, sir — pulling them into your review queue now."


def _worker_request(method: str, path: str, json_body: dict = None) -> dict:
    """Talk to the always-on GAS (Growth Agent Service) Cloudflare Worker.
    Raises on any failure — callers turn that into a spoken-friendly error
    rather than a stack trace."""
    from config import get_config
    import requests

    cfg = get_config()
    # .strip() defends against the classic copy-paste artifact — a trailing
    # newline/whitespace picked up when selecting a full line in an editor —
    # which produces exactly this symptom: both sides "look" identical but
    # the exact-match check on the Worker fails silently every time.
    base_url = (cfg.get("gas_worker_url") or "").strip().rstrip("/")
    # Accept either field name — "gas_worker_key" is what this file has always
    # read, but "worker_shared_secret" mirrors the Cloudflare secret's own
    # name closely enough that it's a natural, easy mistake to make when
    # hand-editing config/api_keys.json. Checking both means that mix-up can
    # no longer silently produce an empty key.
    key = (cfg.get("gas_worker_key") or cfg.get("worker_shared_secret") or "").strip()
    if not base_url or not key:
        raise RuntimeError(
            "gas_worker_url / gas_worker_key are not set "
            "in config/api_keys.json — the overnight cloud agent isn't "
            "connected yet."
        )

    try:
        resp = requests.request(
            method,
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=json_body,
            timeout=WORKER_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 401:
            raise RuntimeError(
                "401 Unauthorized — gas_worker_key in config/api_keys.json doesn't match "
                "WORKER_SHARED_SECRET on the deployed gas Worker. Run `npx wrangler secret "
                "list` in your gas/ folder to confirm the secret is actually bound, then "
                "re-set both values together (wrangler secret put + config/api_keys.json) "
                "rather than trusting either one is still correct."
            ) from e
        if resp.status_code == 500:
            # Don't assume this is the auth-misconfiguration 500 — the Worker
            # returns a specific JSON body for that case. Any OTHER uncaught
            # exception server-side (e.g. Supabase credentials missing) also
            # surfaces as a generic 500, and conflating the two sent past
            # troubleshooting down the wrong path. Check the body first.
            try:
                body = resp.json()
            except Exception:
                body = {}
            if body.get("error") == "server misconfigured":
                raise RuntimeError(
                    "500 from the gas Worker — WORKER_SHARED_SECRET was likely never set on "
                    "Cloudflare at all. Run `npx wrangler secret put WORKER_SHARED_SECRET` in "
                    "your gas/ folder."
                ) from e
            raise RuntimeError(
                f"500 from the gas Worker (not an auth issue — the auth check passed). "
                f"Server said: {body.get('error') or resp.text[:200]}"
            ) from e
        raise
    return resp.json()


def _handle_assign_job(p: dict) -> str:
    niche = p.get("niche", "").strip()
    if not niche:
        return "What niche should the overnight agent work on, sir?"

    platforms = [s.strip() for s in (p.get("platforms") or "").split(",") if s.strip()] or None
    body = {"niche": niche, "goal": p.get("goal") or "grow leads and affiliate revenue"}
    if platforms:
        body["platforms"] = platforms
    if p.get("cadence_hours"):
        body["cadence_hours"] = int(p["cadence_hours"])
    if p.get("posts_per_run"):
        body["posts_per_run"] = int(p["posts_per_run"])

    try:
        result = _worker_request("POST", "/jobs", body)
    except Exception as e:
        return f"Couldn't reach the overnight agent to assign that job: {e}"

    job = result.get("job", {})
    cadence = job.get("cadence_hours", 6)
    return (
        f"Done, sir. The growth agent will work the '{niche}' niche every "
        f"{cadence} hours while you're away, drafting content for your "
        f"approval and tracking PartnerStack and Exness performance. Ask me "
        f"for a report whenever you're back."
    )


def _handle_get_report(p: dict) -> str:
    try:
        result = _worker_request("GET", "/report/latest")
    except Exception as e:
        return f"Couldn't reach the overnight agent for a report: {e}"

    report = result.get("report")
    if not report:
        return "No overnight report yet, sir — the agent hasn't completed a cycle since you assigned a job."

    text = report.get("summary_text", "").strip()
    pending = report.get("pending_review", 0)
    if pending:
        text += f"\n\n{pending} draft(s) are waiting in your approval queue."
    return text or "Report came back empty — worth checking the Worker logs."


def _handle_list_queue(p: dict) -> tuple[str, list]:
    """Returns (summary_text, items) — items is the raw content_queue rows
    from GAS, used to build the interactive review payload pushed to LITE's
    HUD. Never posts anywhere; this is a read straight from the queue you
    review, not a redirect to any external dashboard."""
    status = (p.get("status") or "pending_approval").strip()
    try:
        result = _worker_request("GET", f"/queue?status={status}")
    except Exception as e:
        return f"Couldn't reach the overnight agent's queue: {e}", []

    items = result.get("items") or []
    if not items:
        return f"No drafts with status '{status}', sir.", []

    lines = [f"{len(items)} draft(s) with status '{status}', sir — review below:"]
    for it in items:
        line = f"[{it.get('id')}] ({it.get('platform')}/{it.get('content_type')}) {it.get('title') or '(untitled)'}"
        video_status = it.get("video_status")
        if video_status == "ready":
            line += f" — video ready: {it.get('video_url')}"
        elif video_status == "fallback_ready":
            assets = it.get("video_assets") or {}
            n_images = len(assets.get("images") or [])
            line += f" — fallback assets ready ({n_images} image(s) + narration, no Runway key configured or Runway failed)"
        elif video_status in ("queued", "rendering"):
            line += " — video still rendering, check again shortly"
        elif video_status == "failed":
            line += f" — video render FAILED: {it.get('render_error')}"
        lines.append(line)
    return "\n".join(lines), items


def _handle_edit_draft(p: dict) -> str:
    item_id = (p.get("item_id") or p.get("id") or "").strip()
    if not item_id:
        return "Which draft should I edit, sir? Give me its id."
    body = {}
    for field in ("title", "body", "tracking_subid", "platform", "content_type"):
        if p.get(field) is not None:
            body[field] = p[field]
    if not body:
        return "Tell me what to change (title/body/tracking_subid/platform/content_type), sir."
    try:
        result = _worker_request("PATCH", f"/queue/{item_id}", body)
    except Exception as e:
        return f"Couldn't save that edit: {e}"
    item = result.get("item", {})
    title_note = f" — \"{item['title']}\"" if item.get("title") else ""
    return f"Saved your edit to draft [{item_id}]{title_note} — still pending your approval."


def _handle_approve_draft(p: dict) -> str:
    item_id = (p.get("item_id") or p.get("id") or "").strip()
    if not item_id:
        return "Which draft should I approve, sir? Give me its id."
    try:
        _worker_request("POST", f"/queue/{item_id}/approve")
    except Exception as e:
        return f"Couldn't approve that draft: {e}"
    return f"Approved draft [{item_id}], sir. Posting is still manual by design — go ahead and publish it yourself."


def _handle_reject_draft(p: dict) -> str:
    item_id = (p.get("item_id") or p.get("id") or "").strip()
    if not item_id:
        return "Which draft should I reject, sir? Give me its id."
    try:
        _worker_request("POST", f"/queue/{item_id}/reject")
    except Exception as e:
        return f"Couldn't reject that draft: {e}"
    return f"Rejected draft [{item_id}], sir — it's out of the queue."


_TASK_BUILDERS = {
    "rank_offers":        _task_rank_offers,
    "research_audience":  _task_research_audience,
    "growth_strategy":    _task_growth_strategy,
    "content_calendar":   _task_content_calendar,
    "post_ideas":         _task_post_ideas,
    "video_script":       _task_video_script,
    "email_sequence":     _task_email_sequence,
    "landing_page":       _task_landing_page,
    "ad_copy":            _task_ad_copy,
    "performance_review": _task_performance_review,
}


# ── Entry point (same call shape as every other LITE action) ───────────

def _push_content(player, title: str, text: str, kind: str = "text", payload=None):
    """Same graceful-degrade helper pattern as automation_coding_agent.py —
    falls back to a plain-text push if the running ui.py doesn't yet accept
    kind/payload (older HUD build)."""
    if player is None or not hasattr(player, "show_content"):
        return
    try:
        player.show_content(title, text, kind=kind, payload=payload)
    except TypeError:
        try:
            player.show_content(title, text)
        except Exception:
            pass
    except Exception:
        pass


def affiliate_growth_agent(parameters: dict, player=None, speak=None) -> str:
    p = parameters or {}
    action = (p.get("action") or "custom").strip().lower()

    # These hit the always-on cloud service directly — no LLM call needed here.
    if action == "assign_job":
        text = _handle_assign_job(p)
        _push_content(player, "AFFILIATE GROWTH AGENT — JOB ASSIGNED", text)
        return text

    if action == "get_report":
        text = _handle_get_report(p)
        # Pull the actual pending drafts too (not just the count) and push
        # them to LITE's HUD as an editable review queue — this is the
        # review-in-LITE flow itself, not a pointer to Supabase or any
        # external dashboard. If the queue fetch fails for any reason, the
        # plain-text report above still went through, so nothing is lost.
        queue_summary, items = _handle_list_queue({"status": "pending_approval"})
        if items:
            _push_content(
                player,
                "AFFILIATE GROWTH AGENT — REVIEW QUEUE",
                text + "\n\n" + queue_summary,
                kind="gas_queue",
                payload={"items": items},
            )
        else:
            _push_content(player, "AFFILIATE GROWTH AGENT — OVERNIGHT REPORT", text)
        return text

    if action == "list_queue":
        text, items = _handle_list_queue(p)
        _push_content(
            player, "AFFILIATE GROWTH AGENT — REVIEW QUEUE", text,
            kind="gas_queue" if items else "text",
            payload={"items": items} if items else None,
        )
        return text

    if action == "edit_draft":
        text = _handle_edit_draft(p)
        _push_content(player, "AFFILIATE GROWTH AGENT — DRAFT EDITED", text)
        return text

    if action == "approve_draft":
        text = _handle_approve_draft(p)
        _push_content(player, "AFFILIATE GROWTH AGENT — DRAFT APPROVED", text)
        return text

    if action == "reject_draft":
        text = _handle_reject_draft(p)
        _push_content(player, "AFFILIATE GROWTH AGENT — DRAFT REJECTED", text)
        return text

    if action == "delete_draft":
        text = _handle_delete_draft(p)
        _push_content(player, "AFFILIATE GROWTH AGENT — DRAFT DELETED", text)
        return text

    if action == "download_draft":
        text = _handle_download_draft(p)
        _push_content(player, "AFFILIATE GROWTH AGENT — DRAFT DOWNLOADED", text)
        return text

    if action == "generate_ads":
        text = _handle_generate_ads(p)
        # Pull the freshly-created (and any still-pending) drafts into the
        # same interactive review queue get_report uses, so the new pieces
        # are immediately reviewable rather than requiring a separate
        # list_queue call.
        queue_summary, items = _handle_list_queue({"status": "pending_approval"})
        if items:
            _push_content(
                player,
                "AFFILIATE GROWTH AGENT — GENERATED",
                text + "\n\n" + queue_summary,
                kind="gas_queue",
                payload={"items": items},
            )
        else:
            _push_content(player, "AFFILIATE GROWTH AGENT — GENERATED", text)
        return text

    if action == "custom":
        task = (p.get("task") or "").strip()
        if not task:
            return "Tell me what you want the growth agent to work on, sir."
    else:
        builder = _TASK_BUILDERS.get(action)
        if not builder:
            valid = ", ".join(sorted(_TASK_BUILDERS) | {"custom"})
            return f"Unknown affiliate_growth_agent action '{action}'. Valid actions: {valid}."
        task = builder(p)

    full_prompt = (
        SYSTEM_PROMPT.strip()
        + "\n\n---\n\nTASK:\n" + task
        + "\n\nRespond as the Affiliate Marketing Social Media Growth Agent. "
          "Follow the Decision Framework for any recommendation and end "
          "with the mandatory LITE EXECUTIVE SUMMARY block."
    )

    from core.ai_client import generate_content as _ai_generate

    try:
        response = _ai_generate(full_prompt, model=MODEL_NAME)
        text = response.text.strip()
    except Exception as e:
        error_msg = f"Affiliate growth agent couldn't reach any AI provider: {e}"
        _log_run(action, task, ok=False, summary={}, error=str(e))
        return error_msg

    if "LITE EXECUTIVE SUMMARY" not in text.upper():
        try:
            follow_up = (
                "Your previous response did not include the mandatory "
                "'## LITE EXECUTIVE SUMMARY' block. Reply with ONLY that "
                "block, summarising the response below, using the exact "
                "field labels Opportunity / Recommended Action / Expected "
                "ROI / Risk Level / Priority / Next Actions.\n\n---\n" + text
            )
            addendum = _ai_generate(SYSTEM_PROMPT.strip() + "\n\n" + follow_up, model=MODEL_NAME)
            text = text + "\n\n" + addendum.text.strip()
        except Exception:
            pass  # ship what we have rather than fail the whole call

    summary = _parse_lite_summary(text)
    _log_run(action, task, ok=True, summary=summary)

    if player is not None and hasattr(player, "show_content"):
        try:
            player.show_content("AFFILIATE GROWTH AGENT", text)
        except Exception:
            pass

    return text


# ── main.py wiring (paste manually — kept out of this file on purpose) ──
#
# 1) Import, near the other actions/* imports:
#
#    from actions.affiliate_growth_agent import affiliate_growth_agent
#
# 2) Add to the FUNCTION_DECLARATIONS list, alongside dev_agent/self_maintain:
#
#    {
#        "name": "affiliate_growth_agent",
#        "description": (
#            "Autonomous Affiliate Marketing Social Media Growth subagent "
#            "reporting to LITE. Ranks affiliate offers, researches "
#            "audiences, builds social growth strategies and content "
#            "calendars, writes posts/reels/video scripts/email sequences/"
# ── main.py wiring ───────────────────────────────────────────────────────
#
# main.py already imports affiliate_growth_agent and wires it into
# FUNCTION_DECLARATIONS + the dispatch block — this section originally
# documented that wiring inline, but it drifted out of sync with main.py's
# real (evolved) declaration more than once, so it's been replaced with
# this pointer instead of a second copy to keep updated by hand:
#
#   see main.py's "affiliate_growth_agent" entry in FUNCTION_DECLARATIONS
#   for the current action enum + parameters, and the
#   `elif name == "affiliate_growth_agent":` dispatch line for the call.
#
# If you're wiring this into a fresh main.py that doesn't have it yet:
#
# 1) Import, near the other actions/* imports:
#      from actions.affiliate_growth_agent import affiliate_growth_agent
#
# 2) Add an entry to FUNCTION_DECLARATIONS with "action" accepting:
#      rank_offers | research_audience | growth_strategy | content_calendar |
#      post_ideas | video_script | email_sequence | landing_page | ad_copy |
#      performance_review | assign_job | get_report | generate_ads |
#      list_queue | edit_draft | approve_draft | reject_draft |
#      delete_draft | download_draft | custom (default: custom)
#    plus whatever per-action parameters each builder above reads from `p`
#    (niche, goal, platforms, description, item_id, save_dir, etc.).
#
# 3) Add to the dispatch block:
#      elif name == "affiliate_growth_agent":
#          r = await loop.run_in_executor(
#              None, lambda: affiliate_growth_agent(parameters=args, player=self.ui, speak=self.speak)
#          )
#          result = r or "Done."

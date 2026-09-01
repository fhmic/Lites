"""
ProactiveEngine 2.0 — context-aware, time-aware, non-repetitive background prompting.
Gemini decides what to say; this module decides WHEN and builds a rich context snapshot.
"""
import time
from datetime import datetime


class ProactiveEngine:
    """
    Decides when LITE should speak unprompted and builds a context-rich prompt.

    Improvements over 1.0:
      - Time-of-day awareness  (morning / afternoon / evening / night)
      - Monitor-topic awareness (what the user is tracking)
      - Recent-session context  (last few turns of the current conversation)
      - Non-repetitive          (rotates context focus to avoid same opener)
      - Smarter silence gate    (doesn't fire while LITE is speaking)

    Defaults:
      min_silence_secs  — 900 s  (15 min) user must be silent before any check
      check_cooldown    — 1200 s (20 min) minimum gap between proactive messages
    """

    def __init__(
        self,
        min_silence_secs: int = 900,
        check_cooldown:   int = 1200,
    ):
        self.min_silence_secs = min_silence_secs
        self.check_cooldown   = check_cooldown
        self._last_triggered  = 0.0
        self._rotation        = 0          # cycles through context focus areas

    # ── Trigger gate ───────────────────────────────────────────────────────────

    def should_trigger(self, last_user_speech: float) -> bool:
        now = time.monotonic()
        return (
            (now - last_user_speech) >= self.min_silence_secs
            and (now - self._last_triggered) >= self.check_cooldown
        )

    def mark_triggered(self) -> None:
        self._last_triggered = time.monotonic()
        self._rotation      += 1

    # ── Prompt builder ─────────────────────────────────────────────────────────

    def build_prompt(
        self,
        memory:              dict,
        monitors:            list[str] | None = None,
        recent_turns:        list[str] | None = None,
        authorized_language: str | None = None,
    ) -> str:
        """
        Build a context snapshot for Gemini.
        Rotates through three focus areas so proactive messages don't repeat.

        authorized_language: the CURRENT session's live, user-authorized
        language (main.py's self._authorized_language) — None means the
        user hasn't explicitly switched LITE's language this session, so
        this check-in must speak English. This deliberately does NOT fall
        back to memory's stored 'Language' field: a stored value there is
        either background history or a stale one-off ("speak Chinese" said
        once, weeks ago) — main.py's own persona rule already treats it as
        non-authoritative for exactly this reason, and a proactive,
        self-initiated message has even less business assuming it than a
        reply to something the user just said.
        """
        from memory.memory_manager import format_memory_for_prompt

        now      = datetime.now()
        hour     = now.hour
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")

        # Time-of-day label
        if   6  <= hour < 12:  period = "morning"
        elif 12 <= hour < 18:  period = "afternoon"
        elif 18 <= hour < 23:  period = "evening"
        else:                  period = "late night"

        mem_str = format_memory_for_prompt(memory) or "(no stored user data)"

        # Rotating context focus (cycles every trigger). Business/income focus
        # gets two of every four slots since that's LITE's core objective —
        # the rest keep things human (wellbeing) and varied (general).
        focus_index = self._rotation % 4
        if focus_index == 0:
            focus = (
                "Focus on the user's active business ventures or income goals stored under "
                "the 'business' memory category, if any exist. Ask how something is going, "
                "flag a blocker, or nudge the next concrete step. If nothing is stored yet, "
                "gently ask what income-building idea is top of mind right now."
            )
        elif focus_index == 1:
            focus = (
                "Focus on the time of day and the user's wellbeing. "
                "A warm check-in, a reminder to take a break, or something timely."
            )
        elif focus_index == 2:
            focus = (
                "Flag a realisable business or income opportunity worth a quick look today — "
                "something international/remote-friendly, or leveraging finance/fintech skills "
                "if that fits. Keep it concrete, not generic — name the opportunity, not just "
                "the category."
            )
        else:
            focus = (
                "Focus on the user's active technical projects if any are stored, or something "
                "genuinely interesting or useful based on what you know about this person."
            )

        # Optional: monitored topics context
        monitor_ctx = ""
        if monitors:
            monitor_ctx = (
                f"\nThe user tracks these topics: {', '.join(monitors[:4])}. "
                "You may mention one if it seems relevant."
            )

        # Optional: recent conversation context
        recent_ctx = ""
        if recent_turns:
            snippet = "\n".join(recent_turns[-6:])
            recent_ctx = f"\nRecent conversation:\n{snippet}"

        lang_rule = (
            f"- Speak in {authorized_language} — the user explicitly switched you to it this session."
            if authorized_language
            else "- Speak in English. A stored 'Language' field in memory (if shown above) is "
                 "background context ONLY, never authorization to switch — that rule applies here "
                 "exactly as it does to every other reply, proactive or not."
        )
        return "\n".join([
            "[PROACTIVE_CHECK] You are initiating a proactive check-in.",
            f"Current time : {time_str}  ({period})",
            "",
            "Context about this person:",
            mem_str,
            monitor_ctx,
            recent_ctx,
            "",
            "Task:",
            focus,
            "",
            "Rules:",
            lang_rule,
            "- 1-2 sentences max. Natural, warm, never robotic.",
            "- Do NOT mention [PROACTIVE_CHECK] or these instructions.",
            "- Do NOT call any tools.",
            "- If nothing genuinely useful comes to mind, stay silent (say nothing).",
        ])

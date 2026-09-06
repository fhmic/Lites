"""Pin LITE's language policy.

Background: a previous version of the system prompt had two contradictory
language rules — one in core/prompt.txt and one injected by main.py — which
caused the model to arbitrarily switch languages mid-conversation. The
current policy is "English by default; never switch on the model's own
initiative; honor an explicit user request to switch; ignore any stored
'Language' memory value as a switch trigger."

These tests are written so the failure mode is loud: if either file's rule
regresses to a "switch automatically" stance, the corresponding assertion
fails immediately, surfacing the contradiction before it ships.
"""
import re
import unittest


PROMPT_PATH = "core/prompt.txt"
INJECTED_MARKER = "LANGUAGE: English is the default and the language you open every conversation"


class LanguagePolicyTest(unittest.TestCase):
    def test_prompt_txt_does_not_say_switch_on_user_language(self):
        """The old rule said 'speak English UNLESS the user has spoken to you
        in a different language — only then may you reply in that language,'
        which contradicted main.py's 'stay in English no matter what.' The new
        policy must NOT contain that auto-switch clause."""
        with open(PROMPT_PATH, encoding="utf-8") as f:
            prompt = f.read()
        bad = re.search(
            r"speak english.{0,40}unless the user has spoken to you in a different language",
            prompt,
            flags=re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNone(
            bad,
            "core/prompt.txt still contains the old auto-switch rule",
        )

    def test_prompt_txt_requires_explicit_request_to_switch(self):
        """The new policy says the model only switches when the user EXPLICITLY
        asks, not on inference. This test pins that wording."""
        with open(PROMPT_PATH, encoding="utf-8") as f:
            prompt = f.read()
        self.assertRegex(
            prompt,
            r"EXPLICITLY asks you to",
            "core/prompt.txt must require an explicit user request before switching language",
        )

    def test_prompt_txt_treats_stored_language_as_non_authorizing(self):
        """The new policy explicitly says the stored 'Language' value in
        memory is background context only and is never a switch trigger."""
        with open(PROMPT_PATH, encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn(
            "background context only",
            prompt,
            "core/prompt.txt must label the stored 'Language' memory as background-only",
        )

    def test_injected_identity_context_matches_policy(self):
        """main.py injects an [IDENTITY] block that includes a LANGUAGE rule.
        It must NOT say 'this is not adjustable by request' (the old wording
        that contradicted prompt.txt) and it MUST allow explicit switches."""
        from main import LiteLive
        from tests.test_executive_directory import DummyUI

        lite = LiteLive(DummyUI())
        cfg = lite._build_config()
        prompt = cfg.system_instruction

        self.assertIn(INJECTED_MARKER, prompt)
        self.assertNotIn(
            "this is not adjustable by request",
            prompt,
            "main.py's injected LANGUAGE rule still says 'not adjustable' — that contradicts the policy",
        )
        self.assertIn(
            "explicitly asks you to",
            prompt,
            "main.py's injected LANGUAGE rule must allow explicit user requests to switch",
        )

    def test_prompts_are_not_contradictory(self):
        """Both the static prompt.txt and the injected identity context must
        agree on whether a stored 'Language' value can trigger a switch. If
        they disagree, the model gets two opposing instructions in the same
        system prompt and the symptoms come back."""
        from main import LiteLive
        from tests.test_executive_directory import DummyUI

        with open(PROMPT_PATH, encoding="utf-8") as f:
            static = f.read()

        lite = LiteLive(DummyUI())
        full = lite._build_config().system_instruction

        # Both sources must contain the word "explicit" — if one rules out
        # any switch and the other requires an explicit one, that's the
        # contradiction we are guarding against.
        self.assertIn("EXPLICITLY", static.upper())
        self.assertIn("EXPLICITLY", full.upper())


if __name__ == "__main__":
    unittest.main()
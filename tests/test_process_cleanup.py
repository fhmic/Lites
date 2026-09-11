import os
import unittest
from unittest.mock import patch

from actions import computer_settings
from actions.code_agent import _command_template, _minimal_command_template


class FakeProcess:
    def __init__(self, pid, name, memory_mb):
        self.pid = pid
        self._name = name
        self._memory_mb = memory_mb
        self.terminated = False
        self.info = {"pid": pid, "name": name}

    def name(self):
        return self._name

    def memory_info(self):
        return type("Memory", (), {"rss": self._memory_mb * 1024 * 1024})()

    def terminate(self):
        self.terminated = True

    def wait(self, timeout):
        return None


class ProcessCleanupTest(unittest.TestCase):
    def test_code_agent_command_uses_user_template(self):
        command = _command_template({"cline_command": ["cline.cmd", "--auto-approve", "true", "{prompt}"]})

        # User's cline_command list is the full override — should pass through
        # verbatim, no flags added or stripped by LITE.
        self.assertEqual(command, ["cline.cmd", "--auto-approve", "true", "{prompt}"])

    def test_code_agent_command_falls_back_to_defaults(self):
        # No cline_command / cline_cli / env override → default template.
        # Cline 3.x shape: prompt is a positional argument, --auto-approve
        # takes a boolean value, no --no-session flag exists in current Cline.
        command = _command_template({})

        self.assertEqual(command[0], "cline.cmd" if os.name == "nt" else "cline")
        self.assertIn("--auto-approve", command)
        self.assertIn("true", command)
        self.assertIn("{prompt}", command)

    def test_code_agent_command_uses_cline_cli_override(self):
        command = _command_template({"cline_cli": "/custom/path/to/cline"})

        self.assertEqual(command[0], "/custom/path/to/cline")
        self.assertIn("--auto-approve", command)
        self.assertIn("{prompt}", command)

    def test_code_agent_minimal_template_strips_user_overrides(self):
        # User's full template can include any of Cline 3.x's optional
        # flags (--model, --system, --provider, --thinking, etc.) — the
        # minimal retry template must drop them and keep only what makes
        # Cline run non-interactively in act mode.
        command = _minimal_command_template(
            ["cline.cmd", "--auto-approve", "true", "{prompt}",
             "--model", "bad", "--system", "evil", "--provider", "openrouter",
             "--thinking", "high", "--cwd", "C:\\proj"]
        )

        # Non-essential flags get dropped, but the executable, the auto-
        # approve switch, and the {prompt} placeholder must stay.
        self.assertNotIn("--model", command)
        self.assertNotIn("bad", command)
        self.assertNotIn("--system", command)
        self.assertNotIn("evil", command)
        self.assertNotIn("--provider", command)
        self.assertNotIn("openrouter", command)
        self.assertNotIn("--thinking", command)
        self.assertNotIn("--cwd", command)
        self.assertIn("{prompt}", command)
        self.assertIn("--auto-approve", command)
        self.assertIn("true", command)

    def test_code_agent_minimal_template_rebuilds_when_template_empty(self):
        # If the user's template lost the executable or the headless
        # switches, the minimal template rebuilds a safe default from the
        # first positional (the executable).
        command = _minimal_command_template(["cline.cmd"])

        self.assertEqual(command[0], "cline.cmd")
        self.assertIn("{prompt}", command)
        self.assertIn("--auto-approve", command)
        self.assertIn("true", command)

    def test_cleanup_reports_candidates_without_confirmation(self):
        chrome = FakeProcess(101, "chrome.exe", 900)
        protected = FakeProcess(102, "explorer.exe", 1200)

        with patch.object(computer_settings.psutil, "process_iter", return_value=[chrome, protected]):
            result = computer_settings.process_cleanup({})

        self.assertIn("no processes were closed", result.lower())
        self.assertIn("chrome.exe", result)
        self.assertFalse(chrome.terminated)
        self.assertFalse(protected.terminated)

    def test_cleanup_terminates_only_explicit_allowed_target(self):
        chrome = FakeProcess(101, "chrome.exe", 900)
        spotify = FakeProcess(103, "spotify.exe", 700)

        with patch.object(computer_settings.psutil, "process_iter", return_value=[chrome, spotify]):
            result = computer_settings.process_cleanup({
                "process_name": "chrome",
                "confirmed": "yes",
            })

        self.assertTrue(chrome.terminated)
        self.assertFalse(spotify.terminated)
        self.assertIn("1", result)


if __name__ == "__main__":
    unittest.main()

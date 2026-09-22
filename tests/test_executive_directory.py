import asyncio
import os
import types
import unittest

# Safe to run standalone while LITE itself is running (importing main runs
# its single-instance guard, which would otherwise exit this process).
os.environ.setdefault("LITE_ALLOW_MULTI_INSTANCE", "1")

from main import LiteLive, TOOL_DECLARATIONS


class DummyUI:
    def __init__(self):
        self.muted = False
        self.calls = []
        self.current_file = None

    def set_state(self, state):
        self.calls.append(("state", state))

    def set_active_agent(self, agent_id):
        self.calls.append(("agent", agent_id))

    def show_executive_directory(self, visible=True):
        self.calls.append(("directory", bool(visible)))


class ExecutiveDirectoryTest(unittest.TestCase):
    def test_tool_is_declared_for_voice_commands(self):
        names = {tool["name"] for tool in TOOL_DECLARATIONS}
        self.assertIn("show_executive_directory", names)
        self.assertIn("hide_executive_directory", names)

    def test_execute_tool_opens_directory(self):
        lite = LiteLive(DummyUI())
        fc = types.SimpleNamespace(name="show_executive_directory", id="tool-1", args={})

        result = asyncio.run(lite._execute_tool(fc))

        self.assertEqual(result.response["result"], "Executive directory opened.")
        self.assertIn(("directory", True), lite.ui.calls)

    def test_prompts_include_executive_roster(self):
        lite = LiteLive(DummyUI())
        cfg = lite._build_config()
        prompt = cfg.system_instruction

        self.assertIn("EXECUTIVE DIRECTORY", prompt)
        self.assertIn("Mike: Code Agent", prompt)
        self.assertIn("Ava: RICS Agent", prompt)
        self.assertIn("Nova: Opportunity Pipeline Director", prompt)
        self.assertIn("reports to LITE", prompt)


if __name__ == "__main__":
    unittest.main()

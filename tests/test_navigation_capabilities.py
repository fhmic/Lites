import unittest

from main import LiteLive, TOOL_DECLARATIONS


class NavigationCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.tools = {tool["name"]: tool for tool in TOOL_DECLARATIONS}

    def test_browser_schema_exposes_deep_navigation_actions(self):
        browser = self.tools["browser_control"]
        actions = browser["parameters"]["properties"]["action"]["description"]
        self.assertIn("zoom", actions)
        self.assertIn("list_tabs", actions)
        self.assertIn("deep_dive", actions)
        self.assertIn("fields", browser["parameters"]["properties"])

    def test_computer_schema_exposes_interface_navigation(self):
        computer = self.tools["computer_control"]
        actions = computer["parameters"]["properties"]["action"]["description"]
        self.assertIn("zoom", actions)
        self.assertIn("back", actions)
        self.assertIn("forward", actions)

    def test_system_prompt_routes_deep_interaction_without_manual_clicks(self):
        class DummyUI:
            muted = False
            current_file = None

        prompt = LiteLive(DummyUI())._build_config().system_instruction
        self.assertIn("deep_dive", prompt)
        self.assertIn("smart_click", prompt)
        self.assertIn("Do not tell Felix to perform ordinary navigation manually", prompt)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

from agents.scheduling_docs_agent import _do_check_inbox


class Display:
    def __init__(self):
        self.content = None

    def show_content(self, title, text, **kwargs):
        self.content = (title, text, kwargs)


class GmailDisplayTest(unittest.TestCase):
    @patch("core.google_workspace.list_recent_emails")
    def test_inbox_renders_when_sender_has_no_display_alias(self, list_emails):
        list_emails.return_value = [{
            "from": "sender@example.com",
            "subject": "A subject",
            "snippet": "A preview",
        }]
        player = Display()

        result = _do_check_inbox({}, player=player)

        self.assertIn("1 unread", result)
        self.assertIsNotNone(player.content)
        self.assertIn("sender@example.com", player.content[1])


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from unittest.mock import patch

# Safe to run standalone while LITE itself is running (agent imports could
# transitively pull in main.py, whose single-instance guard would exit this
# process on import).
os.environ.setdefault("LITE_ALLOW_MULTI_INSTANCE", "1")

from agents.scheduling_docs_agent import _do_check_inbox


class Display:
    def __init__(self):
        self.content = None

    def show_content(self, title, text, **kwargs):
        self.content = (title, text, kwargs)


class GmailDisplayTest(unittest.TestCase):
    @patch("core.google_workspace.list_recent_emails")
    def test_inbox_renders_when_sender_has_no_display_alias(self, list_emails):
        # Shape mirrors core.google_workspace.list_recent_emails' contract:
        # {id, thread_id, from, from_display, subject, date, snippet} — "id"
        # is ALWAYS present (it comes straight from the Gmail API) and the
        # inbox table needs it. "from_display" is deliberately omitted here:
        # this test exercises the fallback to the raw address when the
        # sender has no display alias.
        list_emails.return_value = [{
            "id": "msg-001",
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

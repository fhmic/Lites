import unittest
from unittest.mock import patch

from actions import computer_settings


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

import asyncio
import os
import unittest

# Safe to run standalone while LITE itself is running (importing main runs
# its single-instance guard, which would otherwise exit this process).
os.environ.setdefault("LITE_ALLOW_MULTI_INSTANCE", "1")

from main import LiteLive
from ui import LiteUI


class DeadLogSignal:
    def emit(self, text):
        raise RuntimeError("wrapped C/C++ object of type HudWindow has been deleted")


class DeadWindow:
    _log_sig = DeadLogSignal()
    _state_sig = DeadLogSignal()


class UiLifecycleTest(unittest.TestCase):
    def test_write_log_ignores_deleted_qt_window(self):
        ui = LiteUI.__new__(LiteUI)
        ui._win = DeadWindow()

        try:
            ui.write_log("hello")
            ui.set_state("LISTENING")
        except RuntimeError:
            self.fail("UI signal emission should ignore a deleted Qt window")

    def test_live_queue_drops_oldest_when_full(self):
        lite = LiteLive.__new__(LiteLive)
        lite.out_queue = asyncio.Queue(maxsize=1)
        lite.out_queue.put_nowait({"data": b"old", "mime_type": "audio/pcm;rate=16000"})

        lite._queue_live_audio(b"new")

        self.assertEqual(lite.out_queue.qsize(), 1)
        self.assertEqual(lite.out_queue.get_nowait()["data"], b"new")


class RaiseToFrontTest(unittest.TestCase):
    def test_raise_to_front_uses_qt_window_calls(self):
        # The single-instance raise path (a duplicate launch pings the
        # running instance) must drive the real Qt window — not the old
        # Tkinter methods, which silently AttributeError'd on the Qt shim
        # and made every re-launch look like "LITE won't start".
        from unittest.mock import MagicMock, patch

        import ui

        lite_ui = ui.LiteUI.__new__(ui.LiteUI)
        win = MagicMock()
        lite_ui._win = win

        with patch.object(ui, "QTimer") as fake_timer:
            lite_ui.raise_to_front()

        win.showNormal.assert_called_once_with()
        win.raise_.assert_called_once_with()
        win.activateWindow.assert_called_once_with()
        win.setWindowFlag.assert_any_call(ui.Qt.WindowType.WindowStaysOnTopHint, True)
        win.show.assert_called()
        fake_timer.singleShot.assert_called_once()


if __name__ == "__main__":
    unittest.main()

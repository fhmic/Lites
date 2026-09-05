import asyncio
import unittest

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


if __name__ == "__main__":
    unittest.main()

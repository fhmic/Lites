import asyncio
import os
import threading
import time
import types
import unittest

os.environ.setdefault("LITE_ALLOW_MULTI_INSTANCE", "1")

import main


def _make_lite():
    """A LiteLive without its heavy __init__ — the same pattern
    tests/test_ui_lifecycle.py uses — with only what the echo guard and
    _send_realtime touch."""
    lite = main.LiteLive.__new__(main.LiteLive)
    lite._speaking_lock  = threading.Lock()
    lite._is_speaking    = False
    lite._speech_ended_at = 0.0
    lite._phone_active   = False
    lite._aec            = None      # no echo canceller → mic gated while speaking
    lite.audio_in_queue  = asyncio.Queue(maxsize=10)
    lite._turn_done_event = None
    lite.ui = types.SimpleNamespace(
        muted=False, set_state=lambda state: None, write_log=lambda text: None
    )
    return lite


class EchoGuardTest(unittest.TestCase):
    """Mic capture must stay open while LITE is idle, shut while it speaks
    (so speaker echo can't flip-flop the session via Gemini's VAD), and
    reopen after the short tail window."""

    def test_mic_open_when_idle(self):
        lite = _make_lite()
        self.assertTrue(lite._mic_capture_allowed())

    def test_mic_blocked_while_speaking(self):
        lite = _make_lite()
        lite.set_speaking(True)
        self.assertFalse(lite._mic_capture_allowed())

    def test_mic_blocked_within_tail_window_after_speech(self):
        lite = _make_lite()
        lite.set_speaking(True)
        lite.set_speaking(False)
        self.assertFalse(lite._mic_capture_allowed())

    def test_mic_reopens_after_tail_window(self):
        lite = _make_lite()
        lite.set_speaking(True)
        lite.set_speaking(False)
        lite._speech_ended_at = time.monotonic() - main.ECHO_GUARD_S - 0.05
        self.assertTrue(lite._mic_capture_allowed())

    def test_mic_blocked_when_muted(self):
        lite = _make_lite()
        lite.ui.muted = True
        self.assertFalse(lite._mic_capture_allowed())

    def test_mic_blocked_while_phone_active(self):
        lite = _make_lite()
        lite._phone_active = True
        self.assertFalse(lite._mic_capture_allowed())

    def test_mic_stays_open_while_speaking_with_echo_cancellation(self):
        # With the echo canceller active, LITE's own voice is subtracted from
        # the capture — so the mic intentionally stays open while it speaks
        # (that's what makes voice barge-in possible again).
        lite = _make_lite()
        lite._aec = object()  # any non-None marker counts as "canceller active"
        lite.set_speaking(True)
        self.assertTrue(lite._mic_capture_allowed())
        # …and the tail window still applies once it stops speaking.
        lite.set_speaking(False)
        self.assertFalse(lite._mic_capture_allowed())

    def test_echo_guard_is_short(self):
        # The tail window exists to absorb speaker echo — it must stay short
        # enough that the mic feels immediately responsive between turns.
        self.assertGreater(main.ECHO_GUARD_S, 0.0)
        self.assertLessEqual(main.ECHO_GUARD_S, 1.0)


class InterruptReopensMicTest(unittest.TestCase):
    def test_interrupt_resets_speaking_and_tail(self):
        lite = _make_lite()
        lite.set_speaking(True)
        self.assertFalse(lite._mic_capture_allowed())
        lite.interrupt()
        self.assertFalse(lite._is_speaking)
        # Still inside the (deliberate) tail right after the interrupt…
        self.assertFalse(lite._mic_capture_allowed())
        # …but the tail is anchored to NOW, not to the original speech end.
        lite._speech_ended_at = time.monotonic() - main.ECHO_GUARD_S - 0.05
        self.assertTrue(lite._mic_capture_allowed())


class _FlakySession:
    def __init__(self, fail_times: int = 0, always: bool = False):
        self.calls      = 0
        self.fail_times = fail_times
        self.always     = always

    async def send_realtime_input(self, media=None):
        self.calls += 1
        if self.always or self.calls <= self.fail_times:
            raise RuntimeError("simulated websocket hiccup")


class SendRealtimeTest(unittest.IsolatedAsyncioTestCase):
    """A transient send failure must drop one frame, not the whole voice
    session; sustained failure must escalate so the reconnect logic runs."""

    async def _feed(self, lite, n):
        q = asyncio.Queue()
        for _ in range(n):
            q.put_nowait({"data": b"x", "mime_type": main.INPUT_AUDIO_MIME})
        lite.out_queue = q

    async def test_transient_failures_survive(self):
        lite = _make_lite()
        lite.session = _FlakySession(fail_times=3)
        await self._feed(lite, 5)
        task = asyncio.create_task(lite._send_realtime())
        for _ in range(200):
            if lite.session.calls >= 5:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(lite.session.calls, 5)  # all frames attempted
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_sustained_failures_escalate(self):
        lite = _make_lite()
        lite.session = _FlakySession(always=True)
        await self._feed(lite, 20)
        task = asyncio.create_task(lite._send_realtime())
        try:
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(asyncio.shield(task), timeout=2)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, RuntimeError):
                    pass
        # Escalation happens after the threshold — not on the first blip.
        self.assertGreaterEqual(lite.session.calls, 10)


if __name__ == "__main__":
    unittest.main()

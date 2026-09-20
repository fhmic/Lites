import os
import unittest

os.environ.setdefault("LITE_ALLOW_MULTI_INSTANCE", "1")

import main


class _MutexGuardTestBase(unittest.TestCase):
    """Runs the real (env-bypassed) guard against a throwaway mutex name so
    the tests are hermetic — they pass whether or not LITE itself is running
    (a live LITE holds the real mutex and the real port 51477, neither of
    which these tests touch)."""

    def setUp(self):
        self._orig_mutex_name = main._SINGLE_INSTANCE_MUTEX_NAME
        main._SINGLE_INSTANCE_MUTEX_NAME = "Local\\LITE.SingleInstance.TestMutex"
        main._release_single_instance_lock()

    def tearDown(self):
        main._release_single_instance_lock()
        main._SINGLE_INSTANCE_MUTEX_NAME = self._orig_mutex_name


class WindowsMutexGuardTest(_MutexGuardTestBase):
    @unittest.skipUnless(os.name == "nt", "Windows named-mutex logic")
    def test_second_acquire_is_detected_as_duplicate(self):
        self.assertTrue(main._acquire_single_instance_lock())
        self.assertFalse(main._acquire_single_instance_lock())

    @unittest.skipUnless(os.name == "nt", "Windows named-mutex logic")
    def test_release_allows_reacquire(self):
        self.assertTrue(main._acquire_single_instance_lock())
        main._release_single_instance_lock()
        self.assertTrue(main._acquire_single_instance_lock())
        main._release_single_instance_lock()
        self.assertTrue(main._acquire_single_instance_lock())

    @unittest.skipUnless(os.name == "nt", "Windows named-mutex logic")
    def test_second_acquire_does_not_keep_duplicate_mutex_handle(self):
        self.assertTrue(main._acquire_single_instance_lock())
        held = main._single_instance_mutex
        self.assertFalse(main._acquire_single_instance_lock())
        # A duplicate must not overwrite the owner's handle with its own —
        # otherwise the real owner would lose its lock bookkeeping.
        self.assertEqual(main._single_instance_mutex, held)


class SocketRaiseChannelTest(_MutexGuardTestBase):
    @unittest.skipUnless(os.name == "nt", "Windows socket-fallback path")
    def test_start_succeeds_even_if_lock_port_is_busy(self):
        # The mutex is authoritative on Windows: if the raise-channel port is
        # held by another process, LITE must still start (without the channel)
        # instead of refusing to launch. Simulate by occupying the port first.
        import socket
        occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(occupier.close)
        try:
            occupier.bind(("127.0.0.1", main._SINGLE_INSTANCE_PORT))
            occupier.listen(1)
        except OSError:
            self.skipTest("port 51477 unexpectedly busy at OS level")
        self.assertTrue(main._acquire_single_instance_lock())
        self.assertIsNone(main._single_instance_socket)

    def test_listener_tolerates_missing_raise_channel(self):
        # No lock held -> no raise socket -> the listener must return
        # silently instead of crashing on a None socket.
        self.assertIsNone(main._single_instance_socket)
        main._start_single_instance_listener(lambda: None)

    def test_listener_starts_when_raise_channel_exists(self):
        self.assertTrue(main._acquire_single_instance_lock())
        self.assertIsNotNone(main._single_instance_socket)
        main._start_single_instance_listener(lambda: None)


class EscapeHatchTest(unittest.TestCase):
    def test_env_var_bypasses_guard(self):
        os.environ["LITE_ALLOW_MULTI_INSTANCE"] = "1"
        try:
            # Even with the real mutex possibly held by a live LITE, the
            # escape hatch must unconditionally allow acquisition.
            self.assertTrue(main._acquire_single_instance_lock())
        finally:
            os.environ.pop("LITE_ALLOW_MULTI_INSTANCE", None)
            main._release_single_instance_lock()


if __name__ == "__main__":
    unittest.main()

import os
import unittest

import numpy as np

os.environ.setdefault("LITE_ALLOW_MULTI_INSTANCE", "1")

from core.echo_canceller import FRAME_SAMPLES, MAX_REF_BUFFER, EchoCanceller, aec_available

SR = 16000


def _speechish(freqs, seconds, am_hz=2.5):
    """A crude stand-in for LITE's TTS voice: several tones with slow
    amplitude modulation — tonal enough to be a hard case for an AEC, so a
    pass here is a conservative estimate of real-speech performance."""
    t = np.arange(int(SR * seconds)) / SR
    sig = np.zeros_like(t)
    for i, f in enumerate(freqs):
        sig += (0.5 ** i) * np.sin(2 * np.pi * f * t)
    sig *= 0.6 + 0.4 * np.sin(2 * np.pi * am_hz * t)
    return sig / np.max(np.abs(sig))


def _band_energy(x, f0, bw=20.0):
    spec = np.abs(np.fft.rfft(x))
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    mask = (freqs >= f0 - bw) & (freqs <= f0 + bw)
    return float((spec[mask] ** 2).sum())


class EchoCancellerRobustnessTest(unittest.TestCase):
    def test_odd_and_empty_inputs_are_safe(self):
        if not aec_available():
            self.skipTest("pyaec echo canceller not available")
        aec = EchoCanceller()
        # Too small to fill one 20 ms frame → buffered, nothing out yet.
        self.assertEqual(aec.process_mic(np.zeros(50, dtype="<i2").tobytes()), b"")
        # Enough input now → at least one frame processed.
        out = aec.process_mic(np.zeros(600, dtype="<i2").tobytes())
        self.assertGreater(len(out), 0)
        self.assertEqual(len(out) % (FRAME_SAMPLES * 2), 0)
        # Reference FIFO stays bounded even with no mic consumption.
        big = np.zeros(40000, dtype="<i2").tobytes()  # > 2 s of samples
        aec.push_reference(big)
        self.assertLessEqual(len(aec._ref_fifo), MAX_REF_BUFFER * 2)

    def test_silence_reference_passes_near_end_through(self):
        if not aec_available():
            self.skipTest("pyaec echo canceller not available")
        aec = EchoCanceller()
        rng = np.random.default_rng(3)
        near = (rng.standard_normal(9600) * 3000).astype("<i2")
        out = np.frombuffer(aec.process_mic(near.tobytes()), dtype="<i2")
        # No far-end signal → there is nothing to cancel; the capture must
        # survive essentially intact (loose bound: preprocessors may trim a bit).
        in_energy = float((near.astype(np.float64) ** 2).sum())
        out_energy = float((out.astype(np.float64) ** 2).sum())
        self.assertGreater(out_energy, 0.5 * in_energy)


@unittest.skipUnless(aec_available(), "pyaec echo canceller not available")
class EchoSuppressionTest(unittest.TestCase):
    """The core proof that voice barge-in is safe: feed the canceller a
    synthetic "LITE speaking through speakers + leaking into the mic" signal
    and verify LITE's voice is strongly removed while a simultaneous user
    voice survives."""

    FAR_FREQS = (180.0, 240.0, 330.0, 700.0, 1100.0)  # LITE's "voice"
    USER_FREQS = (440.0, 659.0)                        # the real user speaking

    def _run(self):
        duration = 1.6
        n = int(SR * duration)
        far = _speechish(self.FAR_FREQS, duration)

        # Speaker → mic path: 30 ms delay, 0.6 gain, plus a small reverb tap.
        delay = int(0.030 * SR)
        echo = np.zeros(n)
        echo[delay:] = 0.6 * far[:-delay]
        echo[int(0.05 * SR):] += 0.15 * far[: n - int(0.05 * SR)]

        # The real user talks at the same time (double-talk — the hard case).
        # Speech-like: dynamic, amplitude-modulated tones — NOT a stationary
        # pure tone, which echo preprocessors legitimately treat as noise.
        user = 0.15 * _speechish(self.USER_FREQS, duration, am_hz=4.2)
        rng = np.random.default_rng(7)
        noise = 0.002 * rng.standard_normal(n)
        near = echo + user + noise

        aec = EchoCanceller()
        aec.push_reference((np.clip(far, -1, 1) * 32767).astype("<i2").tobytes())
        resid = np.frombuffer(
            aec.process_mic((np.clip(near, -1, 1) * 32767).astype("<i2").tobytes()),
            dtype="<i2",
        ).astype(np.float64) / 32767.0

        # Skip the adaptive-filter convergence window.
        skip = int(0.5 * SR)
        return near[skip:], resid[skip:]

    def test_far_end_suppressed_and_user_preserved(self):
        near, resid = self._run()
        self.assertEqual(len(near), len(resid))

        # 1) LITE's voice bands must be strongly attenuated (≥ 8 dB).
        in_e = sum(_band_energy(near, f) for f in self.FAR_FREQS)
        out_e = sum(_band_energy(resid, f) for f in self.FAR_FREQS)
        suppression_db = 10 * np.log10(in_e / max(out_e, 1e-12))
        self.assertGreaterEqual(suppression_db, 8.0, "echo not suppressed enough")

        # 2) The user's voice must survive (≥ 30% of its energy retained).
        u_in = sum(_band_energy(near, f) for f in self.USER_FREQS)
        u_out = sum(_band_energy(resid, f) for f in self.USER_FREQS)
        self.assertGreaterEqual(u_out / max(u_in, 1e-12), 0.3, "user voice attenuated")

    def test_suppression_reported_values(self):
        """Not an assertion — prints the measured numbers so tuning the
        thresholds later doesn't require rerunning the experiment by hand."""
        near, resid = self._run()
        in_e = sum(_band_energy(near, f) for f in self.FAR_FREQS)
        out_e = sum(_band_energy(resid, f) for f in self.FAR_FREQS)
        u_in = sum(_band_energy(near, f) for f in self.USER_FREQS)
        u_out = sum(_band_energy(resid, f) for f in self.USER_FREQS)
        print(
            f"[EchoTest] suppression = "
            f"{10 * np.log10(in_e / max(out_e, 1e-12)):.1f} dB, "
            f"user preservation = {u_out / max(u_in, 1e-12):.0%}"
        )


if __name__ == "__main__":
    unittest.main()

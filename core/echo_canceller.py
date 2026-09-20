"""Acoustic echo cancellation for LITE's voice pipeline.

Why this exists: LITE talks through the speakers and the laptop microphone
hears its own voice back. Without cancellation, Gemini's voice-activity
detector mistakes that echo for the user starting a new turn — LITE cuts
itself off mid-sentence and the session flip-flops between LISTENING and
SPEAKING. The simple fix (before this module existed) was to mute mic
capture while LITE speaks — which also killed voice barge-in (interrupting
LITE by talking over it).

This module restores true barge-in: it knows exactly what LITE is playing
(the "far-end"/reference signal — see LiteLive._play_audio) and subtracts
the echo path from the mic capture ("near-end") in real time, so the mic
can stay open while LITE speaks and Gemini only ever hears the real user.
Uses the Speex acoustic echo canceller via the `pyaec` wheel (a small Rust
binding with prebuilt Windows/macOS/Linux binaries — no compiler needed).
If the wheel is missing or fails, callers transparently fall back to the
mute-while-speaking behaviour, so this module is strictly optional.
"""

import threading

import numpy as np

try:
    from pyaec import Aec as _SpeexAec
    _IMPORT_ERROR = None
except Exception as _err:  # ImportError or a DLL load failure
    _SpeexAec = None
    _IMPORT_ERROR = _err

SAMPLE_RATE = 16000          # matches SEND_SAMPLE_RATE — mic frames arrive already resampled
FRAME_SAMPLES = 320          # 20 ms — speex adapts best with small, consistent frames
FILTER_SAMPLES = 4096        # 256 ms tail — covers speaker→mic path delay + room reverb
MAX_REF_BUFFER = SAMPLE_RATE * 2   # keep at most 2 s of un-consumed reference audio

# Speex's built-in post-filter (noise suppression / AGC) measurably eats the
# user's own voice during double-talk (measured 39% → 23% retention on a
# speech-like double-talk probe) for little gain — Gemini's VAD prefers
# unmasked user audio over denoised audio. Measured with
# tests/test_echo_cancellation.py; revisit only if residual echo creeps up.
ENABLE_PREPROCESS = False


def aec_available() -> bool:
    """True when the echo canceller can actually be created on this machine
    (pyaec installed AND its native library loads AND a handle allocates)."""
    if _SpeexAec is None:
        return False
    try:
        probe = _SpeexAec(FRAME_SAMPLES, FILTER_SAMPLES, SAMPLE_RATE, ENABLE_PREPROCESS)
        del probe  # __del__ releases the native handle
        return True
    except Exception:
        return False


def _as_int16(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<i2")


def _pack_int16(values) -> bytes:
    return np.asarray(values, dtype=np.int16).astype("<i2", copy=False).tobytes()


class EchoCanceller:
    """Real-time single-channel acoustic echo canceller.

    Data flow inside LITE:
      - LiteLive._play_audio pushes each batch it plays (downsampled to
        16 kHz) via push_reference() — the sound going OUT of the speakers.
      - The mic callback feeds each capture frame through process_mic() —
        what comes back has LITE's own voice removed; only the user remains.

    Frames are paired first-in-first-out: reference written at time T is
    matched against mic frames captured a little later (speaker buffering +
    propagation, typically 100-250 ms). That offset is roughly constant and
    far shorter than the 256 ms filter tail, so the adaptive filter absorbs
    it — no timestamp bookkeeping needed.
    """

    def __init__(self):
        if _SpeexAec is None:
            raise RuntimeError(f"pyaec unavailable: {_IMPORT_ERROR}")
        self._aec = _SpeexAec(FRAME_SAMPLES, FILTER_SAMPLES, SAMPLE_RATE, ENABLE_PREPROCESS)
        self._lock = threading.Lock()
        self._ref_fifo = bytearray()     # far-end samples awaiting pairing
        self._mic_buffer = bytearray()   # partial mic frame awaiting FRAME_SAMPLES

    def push_reference(self, samples_16k: bytes) -> None:
        """Queue played audio (16 kHz mono int16 LE) as the far-end signal.
        Called from the audio-out thread right after the batch is handed to
        the output stream."""
        with self._lock:
            self._ref_fifo.extend(samples_16k)
            max_bytes = MAX_REF_BUFFER * 2
            overflow = len(self._ref_fifo) - max_bytes
            if overflow > 0:
                # Mic paused (muted/tail window) while speech kept playing —
                # drop the oldest reference so the FIFO can't grow unbounded.
                del self._ref_fifo[:overflow]

    def process_mic(self, mic_16k: bytes) -> bytes:
        """Remove LITE's voice from one mic capture frame (16 kHz mono int16
        LE). Returns the cleaned samples — a few may be buffered internally
        to keep whole 20 ms frames for the canceller."""
        out = bytearray()
        with self._lock:
            self._mic_buffer.extend(mic_16k)
            frame_bytes = FRAME_SAMPLES * 2
            while len(self._mic_buffer) >= frame_bytes:
                near = bytes(self._mic_buffer[:frame_bytes])
                del self._mic_buffer[:frame_bytes]
                far = self._take_reference(frame_bytes)
                try:
                    cleaned = self._aec.cancel_echo(
                        list(_as_int16(near)), list(_as_int16(far))
                    )
                except Exception:
                    # Never lose the user's mic over a DSP hiccup — pass the
                    # raw frame through; the echo guard upstream still bounds
                    # any damage from unc cancelled echo.
                    cleaned = list(_as_int16(near))
                out.extend(_pack_int16(cleaned))
        return bytes(out)

    def _take_reference(self, nbytes: int) -> bytes:
        """Pop nbytes of reference, zero-padding when the speakers were
        silent (nothing to cancel → the near-end passes through)."""
        if len(self._ref_fifo) >= nbytes:
            far = bytes(self._ref_fifo[:nbytes])
            del self._ref_fifo[:nbytes]
            return far
        far = bytes(self._ref_fifo) + b"\x00" * (nbytes - len(self._ref_fifo))
        self._ref_fifo.clear()
        return far

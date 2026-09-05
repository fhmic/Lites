"""Audio device selection for LITE's microphone input."""

import sounddevice as sd
import numpy as np


def _default_device_index(direction: int) -> int | None:
    try:
        index = int(sd.default.device[direction])
    except (IndexError, TypeError, ValueError):
        try:
            index = int(sd.default.device)
        except (TypeError, ValueError):
            return None
    return index if index >= 0 else None


def resolve_input_device(sample_rate: int = 16000) -> int:
    return resolve_input_stream(sample_rate)[0]


def resolve_input_stream(sample_rate: int = 16000) -> tuple[int, int]:
    """Return a usable input device compatible with the requested sample rate."""
    devices = list(sd.query_devices())
    default_input = _default_device_index(0)

    # Keep capture on the laptop when Windows switches its default input to a
    # Bluetooth hands-free microphone after a headset connects.
    internal_terms = (
        "microphone array",
        "internal microphone",
        "built-in microphone",
        "realtek audio mic",
        "realtek microphone",
    )
    external_input_terms = (
        "bluetooth",
        "hands-free",
        "headset",
        "airpods",
        "buds",
    )
    # Some Intel SST drivers expose a generic array endpoint that opens
    # successfully but carries only a near-zero signal. Their dedicated DMIC
    # endpoints contain the actual microphone channel.
    dedicated_internal_terms = (
        "microphone array 1",
        "microphone array 2",
        "microphone array 3",
    )
    input_devices = [
        (index, device)
        for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
    ]
    candidates = []
    candidates.extend(
        index for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
        and any(term in device.get("name", "").lower() for term in dedicated_internal_terms)
    )
    if (
        default_input is not None
        and default_input < len(devices)
        and devices[default_input].get("max_input_channels", 0) > 0
        and any(term in devices[default_input].get("name", "").lower() for term in internal_terms)
    ):
        candidates.append(default_input)
    candidates.extend(
        index for index, device in enumerate(devices)
        if index not in candidates
        and device.get("max_input_channels", 0) > 0
        and any(term in device.get("name", "").lower() for term in internal_terms)
    )
    candidates.extend(
        index for index, device in input_devices
        if index not in candidates
        and not any(term in device.get("name", "").lower() for term in external_input_terms)
    )
    if (
        default_input is not None
        and default_input < len(devices)
        and default_input not in candidates
        and not any(
            term in devices[default_input].get("name", "").lower()
            for term in external_input_terms
        )
    ):
        candidates.append(default_input)
    candidates.extend(
        index for index, device in enumerate(devices)
        if index not in candidates and device.get("max_input_channels", 0) > 0
    )

    for index in candidates:
        device = devices[index]
        if device.get("max_input_channels", 0) < 1:
            continue
        try:
            sd.check_input_settings(
                device=index,
                channels=1,
                dtype="int16",
                samplerate=sample_rate,
            )
            return index, sample_rate
        except Exception:
            native_rate = int(device.get("default_samplerate", 0))
            if native_rate <= 0 or native_rate == sample_rate:
                continue
            try:
                sd.check_input_settings(
                    device=index,
                    channels=1,
                    dtype="int16",
                    samplerate=native_rate,
                )
            except Exception:
                continue
            return index, native_rate

    available = [
        f"{index}: {device.get('name', 'Unknown')}"
        for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
    ]
    details = "; ".join(available) or "none"
    raise RuntimeError(
        f"No microphone supports {sample_rate} Hz. Available input devices: {details}"
    )


def input_channel_index(device_index: int) -> int:
    """Return the active channel for known multi-channel Intel DMIC endpoints."""
    try:
        device = sd.query_devices(device_index)
        name = str(device.get("name", "")).lower()
        if any(term in name for term in ("microphone array 1", "microphone array 2")):
            return 1 if device.get("max_input_channels", 0) > 1 else 0
    except Exception:
        pass
    return 0


def resolve_output_device(sample_rate: int = 24000) -> int:
    """Return Windows' current default output device when it is compatible."""
    devices = list(sd.query_devices())
    default_output = _default_device_index(1)
    candidates = []
    if default_output is not None:
        candidates.append(default_output)
    candidates.extend(
        index for index, device in enumerate(devices)
        if index not in candidates and device.get("max_output_channels", 0) > 0
    )

    for index in candidates:
        device = devices[index]
        if device.get("max_output_channels", 0) < 1:
            continue
        try:
            sd.check_output_settings(
                device=index,
                channels=1,
                dtype="int16",
                samplerate=sample_rate,
            )
        except Exception:
            continue
        return index

    available = [
        f"{index}: {device.get('name', 'Unknown')}"
        for index, device in enumerate(devices)
        if device.get("max_output_channels", 0) > 0
    ]
    raise RuntimeError(
        f"No output device supports {sample_rate} Hz. Available output devices: "
        + ("; ".join(available) or "none")
    )


def input_device_name(device_index: int) -> str:
    """Return a display name without making device selection fail."""
    try:
        return str(sd.query_devices(device_index).get("name", "Unknown"))
    except Exception:
        return f"device {device_index}"


def output_device_name(device_index: int) -> str:
    """Return a display name without making device selection fail."""
    try:
        return str(sd.query_devices(device_index).get("name", "Unknown"))
    except Exception:
        return f"device {device_index}"


def resample_audio(samples, source_rate: int, target_rate: int):
    """Convert a mono block to target_rate without an extra audio package."""
    if source_rate == target_rate:
        return samples
    source = np.asarray(samples)
    target_length = max(1, round(len(source) * target_rate / source_rate))
    source_positions = np.linspace(0, len(source) - 1, len(source))
    target_positions = np.linspace(0, len(source) - 1, target_length)
    converted = np.interp(target_positions, source_positions, source)
    return converted.astype(source.dtype, copy=False)

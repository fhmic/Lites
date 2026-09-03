"""Audio device selection for LITE's microphone input."""

import sounddevice as sd


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
    input_devices = [
        (index, device)
        for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
    ]
    candidates = [
        index for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
        and any(term in device.get("name", "").lower() for term in internal_terms)
    ]
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
        except Exception:
            continue
        return index

    available = [
        f"{index}: {device.get('name', 'Unknown')}"
        for index, device in enumerate(devices)
        if device.get("max_input_channels", 0) > 0
    ]
    details = "; ".join(available) or "none"
    raise RuntimeError(
        f"No microphone supports {sample_rate} Hz. Available input devices: {details}"
    )


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

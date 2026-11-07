"""Audio device discovery."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    index: int
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float
    is_default_input: bool = False
    is_default_output: bool = False

    @property
    def kind(self) -> str:
        if self.max_input_channels and self.max_output_channels:
            return "duplex"
        if self.max_input_channels:
            return "input"
        return "output"


def list_devices() -> list[DeviceInfo]:
    import sounddevice as sd

    try:
        raw_devices = sd.query_devices()
        host_apis = sd.query_hostapis()
        default_input, default_output = sd.default.device
    except Exception as exc:  # noqa: BLE001 - PortAudio raises various errors
        raise RuntimeError(f"Не удалось получить аудиоустройства: {exc}") from exc

    devices: list[DeviceInfo] = []
    for index, device in enumerate(raw_devices):
        host_api_index = int(device.get("hostapi", 0))
        host_api = host_apis[host_api_index]["name"] if host_api_index < len(host_apis) else "?"
        devices.append(
            DeviceInfo(
                index=index,
                name=str(device["name"]),
                host_api=str(host_api),
                max_input_channels=int(device.get("max_input_channels", 0)),
                max_output_channels=int(device.get("max_output_channels", 0)),
                default_samplerate=float(device.get("default_samplerate", 0.0)),
                is_default_input=index == default_input,
                is_default_output=index == default_output,
            )
        )
    return devices


def find_device(value: str | int | None, kind: str = "input") -> int | None:
    """Resolve a config value (index, index-string, or name substring)."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    if value.isdigit():
        return int(value)

    needle = value.lower()
    candidates = list_devices()
    matching = [
        device
        for device in candidates
        if needle in device.name.lower()
        and (device.max_input_channels if kind == "input" else device.max_output_channels)
    ]
    if not matching:
        names = ", ".join(
            f"{d.index}:{d.name}"
            for d in candidates
            if (d.max_input_channels if kind == "input" else d.max_output_channels)
        )
        raise ValueError(f"Устройство «{value}» не найдено. Доступны: {names}")
    return matching[0].index


def default_input_device() -> int | None:
    import sounddevice as sd

    return sd.default.device[0] if sd.default.device[0] >= 0 else None


def default_output_device() -> int | None:
    import sounddevice as sd

    return sd.default.device[1] if sd.default.device[1] >= 0 else None

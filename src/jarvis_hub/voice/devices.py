"""Following the system's default audio devices, so connecting headphones moves Jarvis's voice and microphone to them.

PortAudio reads the default devices once, when it initializes, and its streams stay on them. `DeviceRoute` watches
macOS's defaults and, when they change, closes every stream, reinitializes PortAudio and opens them again.
"""

import asyncio
import ctypes
import ctypes.util
from collections.abc import Callable, Sequence
from typing import Any, Protocol

# The device IDs CoreAudio gives the default input and output; None where they cannot be read.
Defaults = tuple[int, int] | None


class Rerouted(Protocol):
    def detach(self) -> None:
        """Close the device stream, keeping everything else."""
        ...

    def attach(self) -> None:
        """Open a stream on the current default device."""
        ...


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def _code(name: bytes) -> int:
    return int.from_bytes(name, "big")


_SYSTEM_OBJECT = 1
_coreaudio: Any = None


def system_defaults() -> Defaults:
    """macOS's default input and output device IDs, read from CoreAudio in microseconds."""
    global _coreaudio
    if _coreaudio is None:
        path = ctypes.util.find_library("CoreAudio")
        if path is None:
            return None
        _coreaudio = ctypes.cdll.LoadLibrary(path)
    found: list[int] = []
    for selector in (b"dIn ", b"dOut"):
        address = _Address(_code(selector), _code(b"glob"), 0)
        device, size = ctypes.c_uint32(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_uint32))
        status = _coreaudio.AudioObjectGetPropertyData(
            _SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(device)
        )
        if status != 0:
            return None
        found.append(device.value)
    return found[0], found[1]


def reinitialize_portaudio() -> None:
    import sounddevice as sd  # pyright: ignore[reportMissingTypeStubs]

    sd_: Any = sd
    sd_._terminate()  # pyright: ignore[reportPrivateUsage]
    sd_._initialize()  # pyright: ignore[reportPrivateUsage]


def device_names() -> str:
    """The default input and output as PortAudio sees them, for the screen."""
    import sounddevice as sd  # pyright: ignore[reportMissingTypeStubs]

    sd_: Any = sd
    try:
        heard, played = (sd_.query_devices(kind=kind)["name"] for kind in ("input", "output"))
    except Exception as e:
        return f"unknown ({type(e).__name__}: {e})"
    return f"microphone {heard}, speaker {played}"


class DeviceRoute:
    """Polls the system defaults and moves `streams` onto them when they change."""

    def __init__(
        self,
        streams: Sequence[Rerouted],
        notice: Callable[[str], None],
        *,
        read: Callable[[], Defaults] = system_defaults,
        reinitialize: Callable[[], None] = reinitialize_portaudio,
        names: Callable[[], str] = device_names,
        poll_s: float = 1.0,
    ) -> None:
        self._streams = streams
        self._notice = notice
        self._read = read
        self._reinitialize = reinitialize
        self._names = names
        self._poll_s = poll_s
        self._current = read()

    def check(self) -> bool:
        """Moves the streams when the defaults changed since the last check; blocking, so run it off the loop."""
        latest = self._read()
        if latest is None or latest == self._current:
            return False
        self._current = latest
        for stream in self._streams:
            stream.detach()
        try:
            self._reinitialize()
        finally:
            for stream in self._streams:
                try:
                    stream.attach()
                except Exception as e:
                    self._notice(f"Could not open the new audio device: {type(e).__name__}: {e}")
        self._notice(f"Audio moved: {self._names()}.")
        return True

    async def follow(self) -> None:
        while True:
            await asyncio.sleep(self._poll_s)
            await asyncio.to_thread(self.check)

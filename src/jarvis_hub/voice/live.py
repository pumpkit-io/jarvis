"""Live mode inputs: each iPhone Client in Live mode is one more audio input of the voice floor (ADR 0031)."""

import time
from collections.abc import Callable
from typing import Any, Protocol

from jarvis_hub.voice.input import Capture, Engines, Listening, UplinkFrames
from jarvis_hub.voice.voice_mode import AudioInput, client_rows


class Floor(Protocol):
    def add_input(self, client: str, audio: AudioInput) -> None: ...

    def remove_input(self, client: str) -> None: ...


class LiveInput:
    """One socket's Live mode: what it uplinks is heard on the floor until it is closed."""

    def __init__(self, inputs: "LiveInputs", client: str, source: UplinkFrames, capture: Capture) -> None:
        self._inputs, self._client, self._source, self._capture = inputs, client, source, capture

    def feed(self, pcm: bytes) -> None:
        """Hears whole 10 ms frames of 16 kHz mono PCM16, dropped while the engines load."""
        self._source.feed(pcm)

    def close(self) -> None:
        self._inputs.close(self._client, self._capture)


class LiveInputs:
    """Opens a Capture on each Live mode socket's uplink, with its own engines from `load` and the Mac's Listening
    settings. Its rows name its Client, and it keeps no clips: the phone's audio is never written to disk."""

    def __init__(
        self,
        floor: Floor,
        load: Callable[[], Engines],
        settings: Listening,
        record: Callable[[dict[str, Any]], None],
        notice: Callable[[str], None],
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._floor = floor
        self._load = load
        self._settings = settings
        self._record = record
        self._notice = notice
        self._clock = clock
        self._open: dict[str, Capture] = {}

    def open(self, client: str) -> LiveInput:
        """Starts hearing `client`'s uplink, in place of any input an earlier socket of that Client left open."""
        source = UplinkFrames(self._clock)
        capture = Capture(self._load, self._settings, client_rows(self._record, client), self._notice, source=source,
                          clock=self._clock)
        if self._open.pop(client, None) is not None:
            self._floor.remove_input(client)
        self._floor.add_input(client, capture)
        self._open[client] = capture
        capture.start()
        return LiveInput(self, client, source, capture)

    def close(self, client: str, capture: Capture) -> None:
        if self._open.get(client) is capture:
            del self._open[client]
            self._floor.remove_input(client)

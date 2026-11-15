"""Following the system's default audio devices, with stand-ins for CoreAudio, PortAudio and the streams."""

from jarvis_hub.voice.devices import DeviceRoute, Defaults


class Recording:
    def __init__(self, name: str, log: list[str], *, fails: bool = False) -> None:
        self._name, self._log, self._fails = name, log, fails

    def detach(self) -> None:
        self._log.append(f"detach {self._name}")

    def attach(self) -> None:
        self._log.append(f"attach {self._name}")
        if self._fails:
            raise OSError("device gone")


def route(defaults: list[Defaults], log: list[str], notices: list[str], *, fails: bool = False) -> DeviceRoute:
    reads = iter(defaults)
    return DeviceRoute(
        [Recording("mic", log), Recording("speaker", log, fails=fails)], notices.append,
        read=lambda: next(reads), reinitialize=lambda: log.append("reinitialize"), names=lambda: "AirPods",
    )


def test_unchanged_defaults_leave_the_streams_alone() -> None:
    log: list[str] = []
    notices: list[str] = []
    assert not route([(1, 2), (1, 2)], log, notices).check()
    assert log == [] and notices == []


def test_new_defaults_close_every_stream_before_reinitializing_and_reopen_them() -> None:
    log: list[str] = []
    notices: list[str] = []
    following = route([(1, 2), (3, 4), (3, 4)], log, notices)
    assert following.check()
    assert log == ["detach mic", "detach speaker", "reinitialize", "attach mic", "attach speaker"]
    assert notices == ["Audio moved: AirPods."]
    assert not following.check()


def test_unreadable_defaults_change_nothing() -> None:
    log: list[str] = []
    notices: list[str] = []
    assert not route([(1, 2), None], log, notices).check()
    assert log == []


def test_a_stream_that_cannot_reopen_is_reported_and_the_others_still_open() -> None:
    log: list[str] = []
    notices: list[str] = []
    assert route([(1, 2), (1, 4)], log, notices, fails=True).check()
    assert log[-2:] == ["attach mic", "attach speaker"]
    assert notices[0] == "Could not open the new audio device: OSError: device gone"

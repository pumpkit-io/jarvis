from pathlib import Path
from types import ModuleType
import sys
import tempfile
from typing import Any

import pytest

from jarvis_hub.voice.tts import configure_espeak


def test_speech_setup_uses_installed_data_from_long_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / ('checkout-' * 25) / 'espeak-ng-data'
    source.mkdir(parents=True)
    (source / 'phontab').write_bytes(b'installed phonemes')
    (source / 'it_dict').write_bytes(b'installed Italian')
    for resource in ('phonindex', 'phondata', 'en_dict'):
        (source / resource).write_bytes(b'installed data')
    loader = ModuleType('espeakng_loader')
    setattr(loader, 'get_data_path', lambda: str(source))
    espeak = ModuleType('misaki.espeak')
    paths: list[str] = []

    class Wrapper:
        @staticmethod
        def set_data_path(path: str) -> None:
            paths.append(path)

    setattr(espeak, 'EspeakWrapper', Wrapper)
    misaki = ModuleType('misaki')
    setattr(misaki, 'espeak', espeak)
    monkeypatch.setitem(sys.modules, 'espeakng_loader', loader)
    monkeypatch.setitem(sys.modules, 'misaki', misaki)
    monkeypatch.setitem(sys.modules, 'misaki.espeak', espeak)
    configure_espeak()
    data = Path(paths[-1])
    assert len(str(data).encode()) < 140
    assert not data.is_symlink()
    assert (data / 'phontab').read_bytes() == b'installed phonemes'
    assert (data / 'it_dict').read_bytes() == b'installed Italian'


def test_speech_setup_missing_resources_has_repair_instruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = ModuleType('espeakng_loader')
    def missing() -> Any:
        raise RuntimeError('data path not exists')
    setattr(loader, 'get_data_path', missing)
    monkeypatch.setitem(sys.modules, 'espeakng_loader', loader)
    with pytest.raises(RuntimeError, match='uv sync --locked --reinstall-package espeakng-loader'):
        configure_espeak()


def test_short_speech_setup_keeps_installed_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix='jv-', dir='/tmp') as directory:
        source = Path(directory) / 'espeak-ng-data'
        source.mkdir()
        for resource in ('phontab', 'phonindex', 'phondata', 'en_dict', 'it_dict'):
            (source / resource).write_bytes(b'installed data')
        loader = ModuleType('espeakng_loader')
        setattr(loader, 'get_data_path', lambda: str(source))
        paths: list[str] = []
        class Wrapper:
            @staticmethod
            def set_data_path(path: str) -> None:
                paths.append(path)
        espeak = ModuleType('misaki.espeak')
        setattr(espeak, 'EspeakWrapper', Wrapper)
        misaki = ModuleType('misaki')
        setattr(misaki, 'espeak', espeak)
        monkeypatch.setitem(sys.modules, 'espeakng_loader', loader)
        monkeypatch.setitem(sys.modules, 'misaki', misaki)
        monkeypatch.setitem(sys.modules, 'misaki.espeak', espeak)
        configure_espeak()
        assert paths == [str(source)]

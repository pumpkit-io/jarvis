from pathlib import Path
import sys

import numpy as np
import pytest

from jarvis_hub.voice.apple_wake import AppleWake


def native_command(tmp_path: Path, transcript: str | list[str], *, missing: bool = False, slow: bool = False) -> list[str]:
    script = tmp_path / 'native.py'
    segments = transcript if isinstance(transcript, list) else [transcript]
    script.write_text(
        'import json,sys,time,wave\n'
        'if sys.argv[1] == "--check":\n'
        f' print(json.dumps({{"ready": {not missing!r}, "error": "assets_missing"}})); sys.exit({int(missing)})\n'
        'with wave.open(sys.argv[1]) as audio:\n'
        ' assert audio.getframerate() == 16000 and audio.getnchannels() == 1\n'
        ' assert audio.getnframes() == 16000\n'
        f'time.sleep({60 if slow else 0})\n'
        f'print(json.dumps({{"segments": {segments!r}}}))\n'
    )
    return [sys.executable, str(script)]


@pytest.mark.parametrize(('transcript', 'accepted'), [
    ('Hey Jarvis, check the build.', True), ('Hi Jarvis', True), ('Ehi Jarvis', True),
    ('Jarvis', True), ('a quoted Jarvis mention', True), ('Jervis', False), ('Jarvison', False), ('', False),
])
def test_native_idle_wake_uses_the_frozen_exact_word_predicate(tmp_path: Path, transcript: str, accepted: bool) -> None:
    wake = AppleWake(command=native_command(tmp_path, transcript))
    try:
        assert wake.recognizes(np.zeros(16000, np.float32)).accepted is accepted
    finally:
        wake.close()


def test_missing_native_assets_report_the_explicit_setup_action(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match='apple_wake --install'):
        AppleWake(command=native_command(tmp_path, '', missing=True))


@pytest.mark.parametrize(('text', 'phrase_only'), [('Hey Jarvis.', True), ('Jarvis, check CI.', False), ('a quoted Jarvis mention', False)])
def test_native_keyword_only_metadata_never_becomes_request_text(tmp_path: Path, text: str, phrase_only: bool) -> None:
    wake = AppleWake(command=native_command(tmp_path, text))
    try:
        result = wake.recognizes(np.zeros(16000, np.float32))
        assert result.phrase_only is phrase_only
    finally:
        wake.close()


def test_native_idle_wake_timeout_fails_closed_and_reaps_the_process(tmp_path: Path) -> None:
    wake = AppleWake(command=native_command(tmp_path, '', slow=True), timeout_s=.1)
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            wake.recognizes(np.zeros(16000, np.float32))
        with pytest.raises(RuntimeError, match='closed'):
            wake.recognizes(np.zeros(16000, np.float32))
    finally:
        wake.close()


@pytest.mark.parametrize('segments', [['Hey', 'Jarvis'], ['Hi', 'Jarvis'], ['Ehi', 'Jarvis'], ['Jarvis']])
@pytest.mark.parametrize('one_breath', [False, True])
def test_final_native_segments_preserve_wake_word_boundaries(tmp_path: Path, segments: list[str], one_breath: bool) -> None:
    wake = AppleWake(command=native_command(tmp_path, segments + (['inspect the current CI status.'] if one_breath else [])))
    try:
        result = wake.recognizes(np.zeros(16000, np.float32))
        assert result.accepted and result.phrase_only is not one_breath
    finally:
        wake.close()

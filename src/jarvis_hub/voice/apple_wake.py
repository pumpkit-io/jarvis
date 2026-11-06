"""Opt-in local SpeechTranscriber keyword recognition, isolated from the capture thread."""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from typing import Any, cast
import wave

import numpy as np

from jarvis_hub.voice.player import Samples
from jarvis_hub.voice.idle_wake import WakeMatch

SETUP = 'uv run python -m jarvis_hub.voice.apple_wake --install'
KEYWORD = re.compile(r'(?<!\w)(?:(?:hey|hi|ehi)[\s,.;:!?]+)?jarvis(?!\w)', re.IGNORECASE)


def bridge() -> Path:
    if platform.system() != 'Darwin' or platform.machine() != 'arm64' or int(platform.mac_ver()[0].split('.')[0]) < 26:
        raise RuntimeError('Apple wake requires Apple Silicon and macOS 26 or later. Select JARVIS_WAKE_ENGINE=openwakeword.')
    source = Path(__file__).with_name('apple_wake.swift')
    identity = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    cache = Path.home() / 'Library/Caches/jarvis/native'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / f'apple-wake-{identity}'
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=cache) as folder:
            built = Path(folder) / 'wake'
            try:
                subprocess.run(['swiftc', '-parse-as-library', '-target', 'arm64-apple-macos26.0', str(source), '-o', str(built)],
                               capture_output=True, check=True, timeout=60)
            except (OSError, subprocess.SubprocessError) as error:
                raise RuntimeError('Apple wake needs the macOS 26 SDK and Swift compiler. Install Xcode command-line tools, then run ' + SETUP) from error
            built.replace(target)
    return target


class AppleWake:
    def __init__(self, *, command: Sequence[str] | None = None, timeout_s: float = 10) -> None:
        self._command = tuple(command) if command is not None else (str(bridge()),)
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._closed = False
        response = self._request('--check')
        if response.get('ready') is not True:
            self.close()
            raise RuntimeError('Apple wake English (US) assets are unavailable. Run ' + SETUP)

    def recognizes(self, audio: Samples) -> WakeMatch:
        with tempfile.TemporaryDirectory(prefix='jarvis-idle-wake-') as folder:
            path = Path(folder) / 'utterance.wav'
            pcm = (np.clip(audio, -1, 32767 / 32768) * 32768).astype(np.int16)
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(pcm.tobytes())
            response = self._request(str(path))
        segments = response.get('segments')
        if not isinstance(segments, list) or not all(isinstance(segment, str) for segment in cast(list[object], segments)):
            self.close()
            raise RuntimeError('Apple wake returned an invalid result; restart Jarvis after running ' + SETUP)
        text = ' '.join(cast(list[str], segments))
        return WakeMatch(KEYWORD.search(text) is not None, KEYWORD.fullmatch(text.strip(' \t\r\n,.;:!?')) is not None)

    def _request(self, argument: str) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError('Apple wake is closed; restart Jarvis after checking its setup.')
            process = subprocess.Popen([*self._command, argument], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._process = process
        try:
            output, _ = process.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired as error:
            self.close()
            process.communicate()
            raise RuntimeError('Apple wake timed out and is closed; restart Jarvis after checking its setup.') from error
        finally:
            with self._lock:
                self._process = None
        if process.returncode != 0:
            self.close()
            raise RuntimeError('Apple wake is unavailable. Check installed English (US) assets with ' + SETUP)
        try:
            response = json.loads(output)
        except json.JSONDecodeError as error:
            self.close()
            raise RuntimeError('Apple wake returned an invalid result; restart Jarvis after checking its setup.') from error
        if not isinstance(response, dict):
            self.close()
            raise RuntimeError('Apple wake returned an invalid result; restart Jarvis after checking its setup.')
        return cast(dict[str, Any], response)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
        if process is not None and process.poll() is None:
            process.kill()


def main() -> None:
    parser = argparse.ArgumentParser(description='Check local Apple wake assets or explicitly install English (US). No microphone is opened.')
    parser.add_argument('--install', action='store_true', help='Explicitly download missing English (US) SpeechTranscriber assets; never evict other languages.')
    parser.add_argument('--check', action='store_true', help='Check installed assets without downloading or recognizing audio.')
    args = parser.parse_args()
    result = subprocess.run([str(bridge()), '--install' if args.install else '--check'], check=False)
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()

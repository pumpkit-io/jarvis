"""Offline voice status through the installed command, without starting audio or agents."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('headphones', [False, True])
@pytest.mark.parametrize('engine', ['openwakeword', 'apple-vad'])
def test_voice_status_reports_effective_wake_configuration_without_claiming_readiness(engine: str, headphones: bool) -> None:
    root = Path(__file__).parents[1]
    command = [str(Path(sys.executable).with_name('jarvis-hub')), *(['--headphones'] if headphones else []), 'voice-status']
    result = subprocess.run(command, cwd=root, env={**os.environ, 'JARVIS_WAKE_ENGINE': engine,
                            'JARVIS_AEC': 'webrtc', 'JARVIS_WAKE_THRESHOLD': '.5', 'TYPESAFE_API_KEY': 'PRIVATE-SENTINEL'},
                            capture_output=True, text=True, check=True)
    status = json.loads(result.stdout)
    assert status['wake']['engine'] == engine
    assert status['audio']['aec'] == ('off' if headphones else 'webrtc')
    assert status['readiness'] == 'not_checked'
    assert status['physical_wake_gate'] == 'unqualified'
    assert status['source']['checkout'] == str(root)
    assert len(status['source']['revision']) == 40
    assert 'PRIVATE-SENTINEL' not in result.stdout + result.stderr
    assert 'assets' in status['next_step'] if engine == 'apple-vad' else 'threshold' in status['next_step']

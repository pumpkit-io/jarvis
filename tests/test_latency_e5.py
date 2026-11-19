import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from tools.latency_e5 import run_group


async def test_forty_synthetic_samples_keep_missing_and_partial_outcomes_and_pace() -> None:
    now = 0.0
    starts: list[float] = []
    records: list[dict[str, Any]] = []

    async def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    async def measure(_path: Path) -> dict[str, Any]:
        nonlocal now
        if not starts:
            now += 10
        starts.append(now)
        index = len(starts)
        return {'audio_status': 'partial' if index == 2 else 'complete',
                'first_matching_clip_received': None if index == 1 else now + 3,
                'release_to_first_clip_seconds': None if index == 1 else 3.0,
                'handoff_to_first_clip_seconds': None if index == 1 else 2.0,
                'upload_duration_seconds': 1.0}

    for network in ('wifi', 'lte'):
        await run_group([Path('fake.wav')] * 20, network=network, route='synthetic route',
                        provenance='synthetic', measure=measure, write=records.append,
                        clock=lambda: now, sleep=sleep)
    assert len(records) == 40
    assert [record['sample'] for record in records] == list(range(1, 21)) * 2
    assert records[0]['first_matching_clip_received'] is None
    assert records[1]['audio_status'] == 'partial'
    assert all(record['provenance'] == 'synthetic' for record in records)
    assert all(starts[index + 1] - starts[index] >= 6.1 - 1e-9
               for index in range(19))


async def invoke(*args: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(sys.executable, 'tools/latency_e5.py', *args,
                                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    return process.returncode or 0, stdout.decode(), stderr.decode()


async def test_offline_executable_report_preserves_missing_denominators(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = []
    for network in ('wifi', 'lte'):
        for sample in range(1, 21):
            value = None if network == 'wifi' and sample == 20 else float(sample)
            records.append({'type': 'e5_sample', 'provenance': 'synthetic', 'network': network,
                            'sample': sample, 'route': 'fake route', 'canonical_completed': True,
                            'audio_status': 'partial' if sample == 1 else 'complete',
                            'first_matching_clip_received': value,
                            'release_to_first_clip_seconds': value,
                            'handoff_to_first_clip_seconds': None if value is None else value - .5,
                            'upload_duration_seconds': .5})
    source = tmp_path / 'synthetic.jsonl'
    source.write_text(''.join(json.dumps(record) + '\n' for record in records))
    code, stdout, stderr = await invoke('report', str(source))
    assert code == 0, stderr
    report = json.loads(stdout)
    assert report['provenance'] == 'synthetic'
    wifi = report['networks']['wifi']
    assert (wifi['sample_count'], wifi['first_clip_count'], wifi['completed_audio_count'],
            wifi['partial_audio_count'], wifi['missing_clip_count']) == (20, 19, 19, 1, 1)
    assert wifi['release_to_first_clip_seconds'] == {'count': 19, 'median': 10.0, 'min': 1.0, 'max': 19.0}
    assert wifi['handoff_to_first_clip_seconds'] == {'count': 19, 'median': 9.5, 'min': .5, 'max': 18.5}
    assert wifi['upload_duration_seconds'] == {'count': 20, 'median': .5, 'min': .5, 'max': .5}
    assert wifi['target_assessment'] == 'incomplete_20_sample_receipt_data'
    assert report['networks']['lte']['release_to_first_clip_seconds']['median'] == 10.5
    assert report['networks']['lte']['target_assessment'] == 'receipt_proxy_over_2.5_seconds'
    assert report['physical_playback_verified'] is False


async def test_run_requires_opt_in_before_reading_credentials_or_connecting(tmp_path: Path) -> None:
    code, _stdout, stderr = await invoke('run', '--network', 'wifi', '--route', 'test route',
                                        '--manifest', str(tmp_path / 'missing.json'),
                                        '--records', str(tmp_path / 'real.jsonl'))
    assert code != 0
    assert '--allow-model-usage' in stderr
    assert not (tmp_path / 'real.jsonl').exists()


async def test_runner_persists_uncertain_sample_then_stops_without_replacing_input() -> None:
    import pytest

    records: list[dict[str, Any]] = []
    calls = 0

    async def measure(_path: Path) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise TimeoutError('Lost result')

    with pytest.raises(ValueError, match='reconcile history'):
        await run_group([Path('fake.wav')] * 20, network='wifi', route='fake route',
                        provenance='synthetic', measure=measure, write=records.append)
    assert calls == 1
    assert len(records) == 1
    assert records[0]['canonical_completed'] is False
    assert records[0]['first_matching_clip_received'] is None
    assert records[0]['release_to_first_clip_seconds'] is None
    assert records[0]['audio_status'] == 'unavailable'
    assert records[0]['request_uncertain'] is True


async def test_offline_report_rejects_mixed_provenance_and_duplicate_samples(tmp_path: Path) -> None:
    record = {'type': 'e5_sample', 'provenance': 'synthetic', 'network': 'wifi', 'sample': 1, 'route': 'fake'}
    source = tmp_path / 'mixed.jsonl'
    source.write_text(json.dumps(record) + '\n' + json.dumps({**record, 'provenance': 'real', 'sample': 2}) + '\n')
    code, _stdout, stderr = await invoke('report', str(source))
    assert code != 0 and 'mix real and synthetic' in stderr
    source.write_text((json.dumps(record) + '\n') * 2)
    code, _stdout, stderr = await invoke('report', str(source))
    assert code != 0 and 'unique' in stderr


async def test_real_cli_validates_manifest_before_accessing_a_client(tmp_path: Path) -> None:
    manifest = tmp_path / 'invalid.json'
    manifest.write_text('["fake.wav"]')
    output = tmp_path / 'unused.jsonl'
    code, _stdout, stderr = await invoke('run', '--allow-model-usage', '--network', 'lte',
                                        '--route', 'synthetic invalid manifest; no connection',
                                        '--manifest', str(manifest), '--records', str(output))
    assert code != 0 and 'exactly 20 WAV paths' in stderr
    assert not output.exists()


async def test_empty_offline_report_keeps_unmeasured_statistics_null(tmp_path: Path) -> None:
    source = tmp_path / 'empty.jsonl'
    source.write_text('')
    code, stdout, stderr = await invoke('report', str(source))
    assert code == 0, stderr
    report = json.loads(stdout)
    assert report['provenance'] == 'unmeasured'
    assert report['networks']['lte']['release_to_first_clip_seconds'] == {
        'count': 0, 'median': None, 'min': None, 'max': None}
    assert report['networks']['lte']['target_assessment'] == 'incomplete_20_sample_receipt_data'

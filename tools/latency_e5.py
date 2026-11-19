#!/usr/bin/env python3
"""E5 runner: real runs are opt-in and spend model usage; reports are offline."""
from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
import json
from pathlib import Path
import time
from typing import Any, cast

Json = dict[str, Any]


async def run_group(paths: list[Path], *, network: str, route: str, provenance: str,
                    measure: Callable[[Path], Awaitable[Json]], write: Callable[[Json], None],
                    clock: Callable[[], float] = time.perf_counter,
                    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    if len(paths) != 20 or network not in ('wifi', 'lte') or not route.strip():
        raise ValueError('Supply exactly 20 utterances, wifi/lte and an actual route label.')
    previous: float | None = None
    for sample, path in enumerate(paths, 1):
        if previous is not None:
            await sleep(max(0, previous + 6.1 - clock()))
        try:
            record = await measure(path)
        except Exception as error:
            record = {'canonical_completed': False, 'audio_status': 'unavailable',
                      'audio_reason': type(error).__name__, 'request_uncertain': True,
                      'release_upload_start': None, 'body_handoff_end': None,
                      'first_matching_clip_received': None, 'upload_duration_seconds': None,
                      'release_to_first_clip_seconds': None, 'handoff_to_first_clip_seconds': None}
        previous = clock()
        write({**record, 'type': 'e5_sample', 'network': network, 'route': route,
               'provenance': provenance, 'sample': sample, 'utterance': path.name})
        if record.get('request_uncertain'):
            raise ValueError('Stopped after uncertain input; reconnect and reconcile history before another run.')


def summarize(records: list[Json]) -> Json:
    import math
    import statistics

    sources = {record['provenance'] for record in records}
    if len(sources) > 1 or sources - {'real', 'synthetic'}:
        raise ValueError('Do not mix real and synthetic records.')
    seen: set[tuple[str, int]] = set()
    for record in records:
        key = (record['network'], record['sample'])
        if record.get('type') != 'e5_sample' or key[0] not in ('wifi', 'lte') or key[1] not in range(1, 21) or key in seen:
            raise ValueError('Expected unique e5_sample records numbered 1..20 per network.')
        seen.add(key)
    networks: Json = {}
    for network in ('wifi', 'lte'):
        group = [record for record in records if record['network'] == network]
        result: Json = {
            'sample_count': len(group), 'planned_count': 20,
            'unrecorded_count': 20 - len(group),
            'first_clip_count': sum(record.get('first_matching_clip_received') is not None for record in group),
            'canonical_completed_count': sum(record.get('canonical_completed') is True for record in group),
            'completed_audio_count': sum(record.get('audio_status') == 'complete' for record in group),
            'partial_audio_count': sum(record.get('audio_status') == 'partial' for record in group),
            'unavailable_audio_count': sum(record.get('audio_status') == 'unavailable' for record in group),
            'not_requested_audio_count': sum(record.get('audio_status') == 'not_requested' for record in group),
            'uncertain_request_count': sum(record.get('request_uncertain') is True for record in group),
            'missing_clip_count': sum(record.get('first_matching_clip_received') is None for record in group),
            'routes': sorted({record['route'] for record in group}),
        }
        for metric in ('release_to_first_clip_seconds', 'handoff_to_first_clip_seconds', 'upload_duration_seconds'):
            values: list[float] = []
            for record in group:
                value = record.get(metric)
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise ValueError('Timing must be a finite number or null.')
                    values.append(float(value))
            result[metric] = {'count': len(values), 'median': statistics.median(values) if values else None,
                              'min': min(values) if values else None, 'max': max(values) if values else None}
        release = result['release_to_first_clip_seconds']
        result['target_assessment'] = ('incomplete_20_sample_receipt_data'
                                       if len(group) != 20 or result['first_clip_count'] != 20 or release['count'] != 20
                                       else 'receipt_proxy_under_2.5_seconds' if release['median'] <= 2.5
                                       else 'receipt_proxy_over_2.5_seconds')
        networks[network] = result
    return {'type': 'e5_report', 'provenance': next(iter(sources), 'unmeasured'), 'networks': networks,
            'physical_playback_verified': False,
            'upload_boundary': 'local_post_write_and_drain_not_hub_receipt',
            'playback_boundary': 'clip_received_not_physical_playback'}


def read_records(paths: list[Path]) -> list[Json]:
    return [json.loads(line) for path in paths for line in path.read_text().splitlines() if line.strip()]


async def real_run(args: argparse.Namespace) -> None:
    from datetime import datetime, timezone
    import hashlib
    import math
    import os
    import uuid

    if __package__:
        from .reference_client import ReferenceClient, read_wav
    else:
        from reference_client import ReferenceClient, read_wav

    manifest: object = json.loads(args.manifest.read_text())
    if not isinstance(manifest, list):
        raise ValueError('Manifest must be a JSON array of exactly 20 WAV paths.')
    entries = cast(list[object], manifest)
    if len(entries) != 20:
        raise ValueError('Manifest must be a JSON array of exactly 20 WAV paths.')
    if not args.route.strip() or not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError('Supply a nonempty route label and positive timeout.')
    paths: list[Path] = []
    for path in entries:
        if not isinstance(path, str):
            raise ValueError('Manifest must contain WAV path strings.')
        paths.append((Path(args.manifest).parent / path).resolve())
    hashes = {path: hashlib.sha256(read_wav(path)).hexdigest() for path in paths}
    run_id = uuid.uuid4().hex
    metadata = {'run_id': run_id, 'started_utc': datetime.now(timezone.utc).isoformat(),
                'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'reference_client_sha256': hashlib.sha256(Path(__file__).with_name('reference_client.py').read_bytes()).hexdigest()}
    descriptor = os.open(args.records, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as output:
        def write(record: Json) -> None:
            output.write(json.dumps({**metadata, **record}, allow_nan=False) + '\n')
            output.flush()
            os.fsync(output.fileno())

        async with AsyncExitStack() as stack:
            async with asyncio.timeout(args.timeout):
                client = await stack.enter_async_context(ReferenceClient(args.credentials, args.clips, display=lambda _frame: None))
            # Leave a full quiet rate window before this Client's first new upload.
            await asyncio.sleep(61)

            async def measure(path: Path) -> Json:
                from_client: Json = {}
                canonical_completed = False
                timing = None
                reply = None
                audio_requested = False
                audio_status, audio_reason = 'unavailable', None
                try:
                    async with asyncio.timeout(args.timeout):
                        result, timing = await client.upload(path)
                        reply = await client.reply(result['request_id'])
                        canonical_completed = True
                        audio_requested = client.audio_requested(reply)
                        await client.wait_audio(reply, requested=audio_requested)
                except Exception as error:
                    audio_reason = 'deadline' if isinstance(error, TimeoutError) else type(error).__name__
                if timing is not None:
                    from_client = timing.report(client)
                else:
                    from_client = {'release_upload_start': None, 'body_handoff_end': None,
                                   'first_matching_clip_received': None, 'upload_duration_seconds': None,
                                   'release_to_first_clip_seconds': None, 'handoff_to_first_clip_seconds': None}
                if reply is not None:
                    audio_status, audio_reason = client.audio_outcome(reply, requested=audio_requested,
                        first_clip=from_client['first_matching_clip_received'] is not None, failure=audio_reason)
                elif from_client['first_matching_clip_received'] is not None:
                    audio_status = 'partial'
                return {**from_client, 'canonical_completed': canonical_completed, 'audio_status': audio_status,
                        'audio_reason': audio_reason, 'request_uncertain': not canonical_completed,
                        'pcm_sha256': hashes[path], 'origin': client.origin}

            await run_group(paths, network=args.network, route=args.route, provenance='real',
                            measure=measure, write=write)
    print(json.dumps(summarize(read_records([args.records])), allow_nan=False))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='action', required=True)
    report = commands.add_parser('report', help='Offline JSONL statistics; never connects')
    report.add_argument('records', type=Path, nargs='+')
    run = commands.add_parser('run', help='20 real uploads on one operator-selected route; spends model usage')
    run.add_argument('--allow-model-usage', action='store_true')
    run.add_argument('--network', choices=('wifi', 'lte'), required=True)
    run.add_argument('--route', required=True, help='Actual Client machine/network route, supplied by the User')
    run.add_argument('--manifest', type=Path, required=True, help='JSON array of exactly 20 WAV paths')
    run.add_argument('--records', type=Path, required=True, help='New private JSONL file; existing files are refused')
    run.add_argument('--credentials', type=Path, default=Path.home() / '.config/jarvis-reference/credentials.json')
    run.add_argument('--clips', type=Path, default=Path.home() / '.local/share/jarvis-reference/e5-clips')
    run.add_argument('--timeout', type=float, default=120, help='Per-sample deadline, seconds')
    return result


def main() -> None:
    args = parser().parse_args()
    try:
        if args.action == 'report':
            print(json.dumps(summarize(read_records(args.records)), allow_nan=False))
            return
        if not args.allow_model_usage:
            raise ValueError('Real E5 runs spend model usage. Explicit --allow-model-usage is required.')
        asyncio.run(real_run(args))
    except (ValueError, OSError, KeyError, TypeError, TimeoutError) as error:
        import sys
        print(str(error) or 'Client setup timed out; no measurement completed.', file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()

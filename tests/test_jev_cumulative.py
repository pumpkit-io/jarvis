"""Jev's wire contract is exercised with an HTTP fake, never a provider call."""

import json
from typing import Any

import httpx

from jarvis_hub.voice.cumulative import Snapshot, Source
from jarvis_hub.voice.jev import JevAssessor


async def test_one_jev_request_asks_independent_questions_binds_sources_and_records_usage() -> None:
    requests: list[dict[str, Any]] = []
    rows: list[dict[str, object]] = []
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        answers: dict[str, Any] = {}
        for name, question in body['questions'].items():
            label = 'directed' if name.startswith('source_') else 'take' if name == 'floor' else 'unfinished'
            answers[name] = dict(type='choice', choice=label, confidence=.95,
                                 probabilities={choice: .9 if choice == label else .1 / (len(question['criteria']) - 1)
                                                for choice in question['criteria']})
        return httpx.Response(200, json=dict(model='jev-1.14.0', answers=answers,
                                           usage=dict(input_tokens=123, output_tokens=9)))
    assessor = JevAssessor('fake-key', notice=lambda message: None, record=rows.append,
                           transport=httpx.MockTransport(respond))
    snapshot = Snapshot('snapshot-1', (Source(4, 2, 'No, wait, I meant', 1, 2, 2),), 2.2,
                        'speaking', 'Check repository A', 'Checking.', ('request', 0))
    result = await assessor.assess_snapshot(snapshot)
    await assessor.close()
    assert result.snapshot_id == 'snapshot-1' and result.directed == ((4, 2),)
    assert result.floor and not result.ready and not result.voice_only
    assert len(requests) == 1 and requests[0]['model'] == 'jev-1.14.0'
    assert set(requests[0]['questions']) == {'source_4_2', 'floor', 'readiness'}
    assert rows[0]['input_tokens'] == 123 and rows[0]['output_tokens'] == 9
    assert 'No, wait' not in str(rows)


async def test_throttling_is_one_failed_observation_without_retry_or_negative_recognition() -> None:
    calls = 0
    notices: list[str] = []
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={'detail': 'private candidate text and fake-key'})
    assessor = JevAssessor('fake-key', notice=notices.append, record=lambda row: None,
                           transport=httpx.MockTransport(respond))
    result = await assessor.assess_snapshot(Snapshot('s', (Source(1, 1, 'Stop', 1, 2, 2),), 2,
                                                    'speaking', '', '', None))
    await assessor.close()
    assert calls == 1 and result.reason == 'throttled'
    assert not result.floor and not result.directed and not result.unrelated
    assert not any('private candidate' in notice or 'fake-key' in notice for notice in notices)


async def test_absent_credentials_make_no_http_request_and_report_setup() -> None:
    calls = 0
    notices: list[str] = []
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)
    assessor = JevAssessor('', notice=notices.append, record=lambda row: None, transport=httpx.MockTransport(respond))
    assessor.start()
    result = await assessor.assess_snapshot(Snapshot('s', (Source(1, 1, 'Stop', 1, 2, 2),), 2,
                                                    'speaking', '', '', None))
    await assessor.close()
    assert calls == 0 and result.reason == 'unavailable'
    assert any('TYPESAFE_API_KEY' in notice for notice in notices)

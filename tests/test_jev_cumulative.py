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
    assert set(requests[0]['questions']) == {'source_4_2_0_3', 'source_4_2_4_9', 'source_4_2_10_11', 'source_4_2_12_17', 'floor', 'readiness'}
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


async def test_jev_returns_grounded_mixed_speech_spans_from_word_decisions() -> None:
    from jarvis_hub.voice.cumulative import Selection

    raw = 'Lena, close the door. Jarvis, explain the build.'
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        answers: dict[str, Any] = {}
        for name, question in body['questions'].items():
            if name == 'floor':
                label = 'take'
            elif name == 'readiness':
                label = 'ready'
            else:
                parts = name.split('_')
                label = 'directed' if len(parts) == 5 and int(parts[3]) >= raw.index('Jarvis,') else 'unrelated'
            answers[name] = dict(type='choice', choice=label, confidence=.95,
                                 probabilities={choice: .9 if choice == label else .1 / (len(question['criteria']) - 1)
                                                for choice in question['criteria']})
        return httpx.Response(200, json=dict(model='jev-1.14.0', answers=answers, usage=dict(input_tokens=333, output_tokens=20)))
    assessor = JevAssessor('fake-key', notice=lambda message: None, record=lambda row: None,
                           transport=httpx.MockTransport(respond))
    result = await assessor.assess_snapshot(Snapshot('s', (Source(1, 1, raw, 1, 2, 2, True),), 3,
                                                    'speaking', '', '', None))
    await assessor.close()
    assert result.spans == (Selection(1, 1, raw.index('Jarvis,'), len(raw)),)
    assert not result.directed and not result.unrelated
    assert result.floor and result.ready


async def test_word_question_workload_is_bounded_and_overflow_does_not_send_truncated_text() -> None:
    counts: list[int] = []
    body_sizes: list[int] = []
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        counts.append(len(body['questions']))
        body_sizes.append(len(request.content))
        answers: dict[str, Any] = {}
        for name, question in body['questions'].items():
            label = 'keep' if name == 'floor' else 'unfinished' if name == 'readiness' else 'unrelated'
            answers[name] = dict(type='choice', choice=label, confidence=.95,
                                 probabilities={choice: .9 if choice == label else .1 / (len(question['criteria']) - 1)
                                                for choice in question['criteria']})
        return httpx.Response(200, json=dict(model='jev-1.14.0', answers=answers, usage=dict(input_tokens=1, output_tokens=1)))
    assessor = JevAssessor('fake-key', notice=lambda message: None, record=lambda row: None,
                           transport=httpx.MockTransport(respond))
    result = await assessor.assess_snapshot(Snapshot('s', (Source(1, 1, ' '.join(['word'] * 512), 1, 2, 2),),
                                                    3, 'speaking', '', '', None))
    assert result.reason == 'assessed' and counts == [514]
    assert body_sizes[0] < 300_000
    overflow = await assessor.assess_snapshot(Snapshot('overflow', (Source(1, 1, ' '.join(['word'] * 513), 1, 2, 2),),
                                                      3, 'speaking', '', '', None))
    await assessor.close()
    assert overflow.reason == 'context_limit' and counts == [514]
    assert not overflow.directed and not overflow.spans and not overflow.floor


async def test_retired_prefix_is_context_but_gets_no_new_admission_questions() -> None:
    names: list[str] = []
    raw = 'Lena close the door. Jarvis explain the build.'
    boundary = raw.index('Jarvis')
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        names.extend(name for name in body['questions'] if name.startswith('source_'))
        answers: dict[str, Any] = {}
        for name, question in body['questions'].items():
            label = 'keep' if name == 'floor' else 'ready' if name == 'readiness' else 'directed'
            answers[name] = dict(type='choice', choice=label, confidence=.95,
                                 probabilities={choice: .9 if choice == label else .1 / (len(question['criteria']) - 1)
                                                for choice in question['criteria']})
        return httpx.Response(200, json=dict(model='jev-1.14.0', answers=answers, usage=dict(input_tokens=123, output_tokens=9)))
    assessor = JevAssessor('fake-key', notice=lambda message: None, record=lambda row: None,
                           transport=httpx.MockTransport(respond))
    result = await assessor.assess_snapshot(Snapshot('s', (Source(1, 2, raw, 1, 2, 2, allowed_from=boundary),),
                                                    3, 'speaking', '', '', None))
    await assessor.close()
    assert names and all(int(name.split('_')[3]) >= boundary for name in names)
    assert result.directed == ((1, 2),) and result.ready

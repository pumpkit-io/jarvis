"""Locally admitted speech retains context until the Talker explicitly chooses Task scope."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from jarvis_hub.agents.backend import Refused

from tests.fakes import AwaitInterrupt, Gate, ResolveScope, Text, Tool, until
from tests.test_voice_mode import LocalRelation, MakeRig, rig_for as rig_for


async def test_a_local_new_request_hint_cannot_discard_the_verbatim_opening(rig_for: MakeRig) -> None:
    rig = await rig_for([AwaitInterrupt()], [Text('I will check that, sir.')], recognition=LocalRelation('new_request'))
    rig.audio.wake()
    rig.audio.say('Open an issue')
    await until(lambda: len(rig.user_messages()) == 1)
    rig.audio.say('about failed webhook deliveries')
    await until(lambda: len(rig.user_messages()) == 2)
    assert 'Open an issue about failed webhook deliveries' in rig.user_messages()[-1]


async def test_task_intent_is_held_while_the_admitted_talker_has_not_chosen_scope(
    rig_for: MakeRig, tmp_path: Path,
) -> None:
    path = tmp_path / 'state.json'
    choose = asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [Gate(choose), Text('A separate answer, sir.')],
        orchestrator=[[AwaitInterrupt()]], recognition=LocalRelation('new_request'), state_path=path,
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('A different question: how many tasks are running?')
    await until(lambda: len(rig.user_messages()) == 2)
    saved = json.loads(path.read_text())
    [task] = saved['tasks']
    assert task['request'] == 'Check build'
    assert task['revision'] == 0
    [scope] = saved['spoken_scopes']
    assert scope['pending'] is True
    assert scope['task_ids'] == ['T1']
    assert scope['segments'] == ['Check build', 'A different question: how many tasks are running?']
    choose.set()


async def test_only_talker_selected_tasks_change_and_old_tools_wait_for_scope(rig_for: MakeRig, tmp_path: Path) -> None:
    choose, report = asyncio.Event(), asyncio.Event()
    path = tmp_path / 'state.json'
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [Gate(choose), ResolveScope('revise_tasks', ('T1',)),
         Tool('delegate', {'request': 'Check build and tests', 'task_id': 'T1'}), Text('Updated, sir.')],
        orchestrator=[[Gate(report), Tool('report', {'task_id': 'T1', 'spoken': 'Old report', 'needs_user': False})], []],
        recognition=LocalRelation('directed'), state_path=path,
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('and tests')
    await until(lambda: len(rig.user_messages()) == 2)
    report.set()
    await rig.settle()
    assert rig.backend.session('orchestrator').tool_results == []
    assert json.loads(path.read_text())['tasks'][0]['request'] == 'Check build'
    choose.set()
    await until(lambda: len(rig.backend.session('orchestrator').received) == 2)
    old = rig.backend.session('orchestrator').tool_results[0]
    assert old.result == 'Refused: this Task revision was superseded.'
    assert any(result.name == 'resolve_scope' and 'Resolved' in str(result.result)
               for result in rig.backend.session('talker').tool_results)


async def test_an_explicit_new_topic_releases_existing_task_without_rewriting_it(rig_for: MakeRig) -> None:
    choose, report = asyncio.Event(), asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [Gate(choose), ResolveScope('new_request', from_segment=1), Text('One Task, sir.')],
        orchestrator=[[Gate(report), Tool('report', {'task_id': 'T1', 'spoken': 'Build checked', 'needs_user': False})]],
        recognition=LocalRelation('directed'),
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('A different question: how many tasks are running?')
    await until(lambda: len(rig.user_messages()) == 2)
    report.set()
    await rig.settle()
    assert rig.backend.session('orchestrator').tool_results == []
    choose.set()
    await until(lambda: bool(rig.backend.session('orchestrator').tool_results))
    assert rig.backend.session('orchestrator').tool_results[0].result == 'Reported to the User.'
    status = next(tool.handler for tool in rig.backend.session('talker').spec.tools if tool.name == 'status')
    assert 'Check build' in await status({})


@pytest.mark.parametrize('decision', ['new_request', 'revise_tasks'])
async def test_subagent_permission_waits_for_scope_then_rechecks_its_source_revision(rig_for: MakeRig, decision: str) -> None:
    choose, finish = asyncio.Event(), asyncio.Event()
    spawn = {'repo': 'pumpkit', 'title': 'Build check', 'brief': 'Check build',
             'model': 'sonnet', 'effort': 'low', 'writes': False}
    resolution = ResolveScope(decision, ('T1',) if decision == 'revise_tasks' else (),
                              1 if decision == 'new_request' else None)
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [Gate(choose), resolution, Text('Scope checked, sir.')],
        orchestrator=[[Tool('spawn_subagent', spawn)]], subagents={'Check build': [Gate(finish), Text('Done.')]},
        recognition=LocalRelation('directed'),
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('subagent')))
    rig.audio.say('including tests' if decision == 'revise_tasks' else 'A different question: what time is it?')
    await until(lambda: len(rig.user_messages()) == 2)
    lease = rig.backend.session('subagent').spec.before_tool
    assert lease is not None
    pending = asyncio.ensure_future(lease('mcp__inspect', {}))
    await rig.settle()
    assert not pending.done()
    choose.set()
    answer = await pending
    if decision == 'revise_tasks':
        assert isinstance(answer, Refused)
        assert 'superseded' in answer.reason
    else:
        assert answer is None
    finish.set()


async def test_pending_scope_recovery_uses_text_only_talker_before_old_task_work(
    rig_for: MakeRig, tmp_path: Path,
) -> None:
    from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, make_hub
    path = tmp_path / 'state.json'
    held = asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [Gate(held), Text('Waiting, sir.')], orchestrator=[[AwaitInterrupt()]],
        recognition=LocalRelation('directed'), state_path=path,
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('A different question: how many tasks are running?')
    await until(lambda: len(rig.user_messages()) == 2)
    restart = tmp_path / 'restart.json'
    restart.write_text(path.read_text())
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    backend = FakeBackend(clock, [[ResolveScope('new_request', from_segment=1), Text('One Task, sir.')]],
                          orchestrator=[[Tool('report', {'task_id': 'T1', 'spoken': 'Build checked', 'needs_user': False})]])
    hub = make_hub(backend, output, metrics, state_path=restart)
    try:
        await hub.start()
        await until(lambda: bool(backend.all_sessions('talker')) and len(backend.session('talker').received) > 1)
        await until(lambda: any(result.name == 'resolve_scope' for result in backend.session('talker').tool_results))
        resolution = next(result for result in backend.session('talker').tool_results if result.name == 'resolve_scope')
        assert 'Resolved' in str(resolution.result)
        assert '<pending_scope' in backend.session('talker').received[1]
        await until(lambda: bool(backend.all_sessions('orchestrator')) and bool(backend.session('orchestrator').received))
        assert 'Check build' in backend.session('orchestrator').received[0]
    finally:
        held.set()
        await hub.close()


async def test_hub_approval_action_rechecks_source_authority_after_target_read() -> None:
    from jarvis_hub.approvals import Approval, Approvals
    from tests.fakes import FakeClock, StubGh
    head = 'a' * 40
    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', head)})
    approvals = Approvals(org='pumpkit-io', runner=gh, clock=FakeClock(),
                          on_change=lambda: None, log=lambda entry: None)
    asked = await approvals.request('T1', {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': head})
    assert isinstance(asked, Approval)
    approvals.turn_began(None)
    approvals.turn_ended(completed=True, told=[asked.id], said=asked.question())
    approvals.turn_began('Yes.')
    approved = approvals.decide(asked.id)
    assert isinstance(approved, Approval)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0
    async def source() -> Refused | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        entered.set()
        await release.wait()
        return Refused('The spoken Task scope changed before dispatch.')
    pending = asyncio.create_task(approvals.carry_out(approved, authorize=source))
    await entered.wait()
    assert len(gh.reads) >= 2
    assert gh.actions == []
    release.set()
    answer = await pending
    assert 'scope changed' in answer
    assert gh.actions == []


async def test_typed_clarification_is_durable_without_rewriting_spoken_segments(rig_for: MakeRig, tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check repository A'}), AwaitInterrupt()],
        [Text('Which repository, sir?')],
        [ResolveScope('revise_tasks', ('T1',)),
         Tool('delegate', {'request': 'Check repository C', 'task_id': 'T1'}), Text('Updated, sir.')],
        orchestrator=[[AwaitInterrupt()], [AwaitInterrupt()]], recognition=LocalRelation('directed'), state_path=path,
    )
    rig.audio.wake()
    rig.audio.say('Check repository A')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('for repository B')
    await until(lambda: rig.state == 'follow_up')
    rig.typist.type('Actually use repository C.')
    await until(lambda: len(rig.backend.session('orchestrator').received) == 2)
    saved = json.loads(path.read_text())
    [scope] = saved['spoken_scopes']
    assert scope['segments'] == ['Check repository A', 'for repository B']
    assert scope['typed_context'] == ['Actually use repository C.']
    [task] = saved['tasks']
    assert 'Actually use repository C.' in task['request']
    assert 'Actually use repository C.' in task['words'][-1]


async def test_text_only_recovery_resolves_second_scope_and_delegates_its_words(
    rig_for: MakeRig, tmp_path: Path,
) -> None:
    from tests.fakes import FakeBackend, FakeClock, RecordingMetrics, RecordingOutput, make_hub
    path = tmp_path / 'state.json'
    release_first, hold_last = asyncio.Event(), asyncio.Event()
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [Text('Which change, sir?')],
        [Tool('delegate', {'request': 'Check docs'}), AwaitInterrupt()],
        [Gate(hold_last), Text('Waiting, sir.')],
        orchestrator=[[Gate(release_first)], [AwaitInterrupt()]], recognition=LocalRelation('directed'), state_path=path,
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')))
    rig.audio.say('today')
    await until(lambda: rig.state == 'follow_up')
    release_first.set()
    await rig.settle()
    rig.clock.advance(1)
    rig.audio.wake()
    rig.audio.say('Check docs')
    await until(lambda: len(rig.backend.session('orchestrator').received) == 2)
    rig.audio.say('tomorrow')
    await until(lambda: len([message for message in rig.backend.session('talker').received if '<user ' in message]) == 4)
    snapshot = json.loads(path.read_text())
    assert len([scope for scope in snapshot['spoken_scopes'] if scope['pending']]) == 2
    restart = tmp_path / 'restart.json'
    restart.write_text(json.dumps(snapshot))
    clock, output, metrics = FakeClock(), RecordingOutput(), RecordingMetrics()
    backend = FakeBackend(clock, [[ResolveScope('revise_tasks', ('T2',), scope_index=1),
                                  Tool('delegate', {'request': 'Check docs tomorrow', 'task_id': 'T2'}), Text('Updated, sir.')]],
                          orchestrator=[[AwaitInterrupt()]])
    hub = make_hub(backend, output, metrics, state_path=restart)
    try:
        await hub.start()
        await until(lambda: bool(backend.all_sessions('orchestrator')) and bool(backend.session('orchestrator').received))
        saved = json.loads(restart.read_text())
        tasks = {task['id']: task for task in saved['tasks']}
        assert tasks['T1']['request'] == 'Check build'
        assert tasks['T2']['request'] == 'Check docs tomorrow'
        assert tasks['T2']['words'][-1] == 'Check docs tomorrow'
        assert 'Check docs tomorrow' in backend.session('orchestrator').received[0]
    finally:
        hold_last.set()
        await hub.close()


async def test_missing_or_late_resolution_never_releases_old_approval_or_cancel_authority(
    rig_for: MakeRig, tmp_path: Path,
) -> None:
    choose = asyncio.Event()
    path = tmp_path / 'state.json'
    approval: dict[str, Any] = {'action': 'subagent_action', 'description': 'Publish release 42', 'grants': []}
    rig = await rig_for(
        [Tool('delegate', {'request': 'Publish release 42'}), AwaitInterrupt()],
        [Gate(choose), ResolveScope('preserve_tasks'), Text('Late, sir.')],
        orchestrator=[[Tool('report', {'task_id': 'T1', 'spoken': 'Publish release 42?',
                                      'needs_user': True, 'approval': approval}), AwaitInterrupt()]],
        recognition=LocalRelation('directed'), state_path=path,
    )
    rig.audio.wake()
    rig.audio.say('Publish release 42')
    await until(lambda: bool(rig.backend.all_sessions('orchestrator')) and bool(rig.backend.session('orchestrator').tool_results))
    rig.audio.say('actually release 43')
    await until(lambda: len(rig.user_messages()) == 2)
    tools = {tool.name: tool.handler for tool in rig.backend.session('talker').spec.tools}
    assert 'Resolve' in await tools['cancel']({'task_id': 'T1'})
    assert 'Resolve' in await tools['approve']({'approval_id': 'A1'})
    await rig.advance(31)
    saved = json.loads(path.read_text())
    [scope] = saved['spoken_scopes']
    assert scope['pending'] is True and scope['failed'] is True
    assert saved['tasks'][0]['request'] == 'Publish release 42'
    assert saved['approvals'][0]['state'] == 'pending'
    choose.set()
    await rig.settle()
    result = next(result for result in rig.backend.session('talker').tool_results if result.name == 'resolve_scope')
    assert 'stale' in str(result.result)
    assert json.loads(path.read_text())['spoken_scopes'][0]['pending'] is True


async def test_an_inherited_lease_cannot_bypass_a_scope_change_while_it_awaits(rig_for: MakeRig, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    entered, release, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    spawn = {'repo': 'pumpkit', 'title': 'Build check', 'brief': 'Check build',
             'model': 'sonnet', 'effort': 'low', 'writes': False}
    rig = await rig_for(
        [Tool('delegate', {'request': 'Check build'}), AwaitInterrupt()],
        [ResolveScope('revise_tasks', ('T1',)), Text('Updated, sir.')],
        orchestrator=[[Tool('spawn_subagent', spawn)]], subagents={'Check build': [Gate(finish), Text('Done')]},
        recognition=LocalRelation('directed'),
    )
    rig.audio.wake()
    rig.audio.say('Check build')
    await until(lambda: bool(rig.backend.all_sessions('subagent')))
    original = getattr(rig.hub, '_session_for')
    async def inherited(name: str, inputs: Any) -> Refused | None:
        entered.set()
        await release.wait()
        return None
    def with_inherited(subagent: Any) -> Any:
        return replace(original(subagent), before_tool=inherited)
    monkeypatch.setattr(rig.hub, '_session_for', with_inherited)
    subagent = getattr(rig.hub, '_subagents').get('S1')
    spec = getattr(rig.hub, '_subagent_session')(subagent)
    pending = asyncio.ensure_future(spec.before_tool('mcp__inspect', {}))
    await entered.wait()
    rig.audio.say('including tests')
    await until(lambda: any(result.name == 'resolve_scope' for result in rig.backend.session('talker').tool_results))
    release.set()
    refused = await pending
    assert isinstance(refused, Refused) and 'superseded' in refused.reason
    finish.set()


async def test_a_scope_wait_invalidates_the_approval_target_read_and_reads_it_again() -> None:
    from jarvis_hub.approvals import Approval, Approvals
    from tests.fakes import FakeClock, StubGh
    head = 'a' * 40
    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', head)})
    approvals = Approvals(org='pumpkit-io', runner=gh, clock=FakeClock(),
                          on_change=lambda: None, log=lambda entry: None)
    asked = await approvals.request('T1', {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': head})
    assert isinstance(asked, Approval)
    approvals.turn_began(None)
    approvals.turn_ended(completed=True, told=[asked.id], said=asked.question())
    approvals.turn_began('Yes.')
    approved = approvals.decide(asked.id)
    assert isinstance(approved, Approval)
    entered, release = asyncio.Event(), asyncio.Event()
    calls, version = 0, 0
    async def authorize() -> Refused | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await release.wait()
        return None
    pending = asyncio.create_task(approvals.carry_out(approved, authorize=authorize, authority_version=lambda: version))
    await entered.wait()
    version += 1
    gh.prs['pumpkit-io/pumpkit#42'] = ('OPEN', 'b' * 40)
    release.set()
    await pending
    assert approved.state == 'lapsed'
    assert len(gh.reads) >= 3
    assert gh.actions == []


async def test_scope_resolution_rejects_malformed_or_repeated_authority_without_mutating_work() -> None:
    from jarvis_hub.spoken import ScopeChoice, SpokenScopes
    from tests.fakes import FakeClock
    scopes = SpokenScopes(FakeClock(), change=lambda: None, notice=lambda text: None)
    try:
        scopes.admit('scope', 0, ('Open an issue',), 0)
        scopes.bind('scope', 'T1', 'Open issue')
        scope = scopes.admit('scope', 1, ('Open an issue', 'about callbacks'), 1)
        authority = scopes.activate()
        base: dict[str, Any] = {'scope_id': scope.id, 'revision': scope.revision, 'attempt': scope.attempt,
                                'disposition': 'revise_tasks', 'task_ids': ['T1']}
        assert isinstance(scopes.choose({**base, 'revision': True}, authority), Refused)
        assert isinstance(scopes.choose({**base, 'scope_id': []}, authority), Refused)
        assert isinstance(scopes.choose({**base, 'task_ids': ['T9']}, authority), Refused)
        assert scope.pending
        choice = scopes.choose(base, authority)
        assert isinstance(choice, ScopeChoice)
        scopes.commit(choice)
        assert isinstance(scopes.choose(base, authority), Refused)
    finally:
        await scopes.close()


async def test_recent_scope_history_is_bounded_without_dropping_pending_authority() -> None:
    from jarvis_hub.spoken import SpokenScopes
    from tests.fakes import FakeClock
    scopes = SpokenScopes(FakeClock(), change=lambda: None, notice=lambda text: None)
    try:
        scopes.admit('pending', 0, ('Open an issue',), 0)
        scopes.bind('pending', 'T1', 'Open issue')
        scopes.admit('pending', 1, ('Open an issue', 'about callbacks'), 1)
        for index in range(70):
            scopes.admit(f'request-{index}', 0, (f'Question {index}',), 2 + index)
            scopes.prune()
        assert len(scopes.dump(lambda value: value)) <= 65
        assert scopes.held('T1')
        assert scopes.get('pending') is not None
        assert scopes.get('request-69') is not None
    finally:
        await scopes.close()

"""Permission through the Hub; language interpretation is supplied by the fake Talker."""

import asyncio
import json
from pathlib import Path

import pytest

from tests.fakes import FakeClock, RecordingMetrics, RecordingOutput, StubGh, Text, Tool, Abort, Step, until
from tests.test_approvals import HEAD, TIP, a_session, approve, ask_merge, merge_argv, present, results

DEPLOY = (
    'Run sh scripts/ship.sh in pumpkit-ops: release v0.14.1 and deploy to pumpkit.io. '
    'Sends nothing to users; deletes no branches.'
)


async def test_a4_question_and_natural_approval_dispatch_once() -> None:
    session = a_session(
        FakeClock(), RecordingOutput(), RecordingMetrics(), StubGh(),
        [
            [Text('I will prepare the deployment.'), Tool('delegate', {'request': 'Prepare deployment'})],
            [Tool('present_approval', {'approval_id': 'A1'}), Text('Ready to deploy. Shall I go ahead?')],
            [Text('Deploying.'), approve()],
            [Text('Done.')],
        ],
        [[Tool('report', {'task_id': 'T1', 'spoken': 'Ready to deploy.', 'needs_user': True,
                          'approval': {'action': 'subagent_action', 'description': DEPLOY}})],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Done.', 'needs_user': False})]],
    )
    await session.start()
    await session.say('Prepare the deployment')
    await session.replied('Ready to deploy. Shall I go ahead?')
    await session.say('Yes, I approve.')
    await session.end()
    [decision] = results(session.backend, 'talker', 'approve')
    assert str(decision.result).startswith('Approved A1 with the User\'s words')
    messages = session.backend.session('orchestrator').received
    assert len([m for m in messages if 'status="approved"' in m]) == 1
    assert 'Yes, I approve.' in messages[1]


async def test_direct_instruction_authorizes_merge_before_an_approval_exists() -> None:

    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
    metrics = RecordingMetrics()
    session = a_session(
        FakeClock(), RecordingOutput(), metrics, gh,
        [[Text('I will merge it.'), Tool('delegate', {'request': 'Merge pumpkit 42',
            'authorization': {'scope': 'action', 'description': 'Merge pumpkit PR 42'}})],
         [Text('Merged.')]],
        [[Tool('report', {'task_id': 'T1', 'spoken': 'Merging.', 'needs_user': False,
                         'authorization_id': 'U1',
                         'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}})],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Merged.', 'needs_user': False})]],
    )
    await session.start()
    await session.say('Merge pumpkit PR 42')
    await session.replied('Merged.')
    await session.end()
    assert gh.actions == [merge_argv()]
    assert not results(session.backend, 'talker', 'approve')
    logs = [r for r in metrics.records if r.get('kind') == 'approval']
    assert logs[0]['words'] == 'Merge pumpkit PR 42'


async def test_task_permission_covers_successive_actions_and_is_visible_in_context() -> None:

    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)},
                branches={'pumpkit-io/pumpkit:feat/old-idea': TIP})
    session = a_session(
        FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('I will finish it.'), Tool('delegate', {'request': 'Merge and clean up',
             'authorization': {'scope': 'task', 'description': 'Merge PR 42 and remove its branch'}})],
         [Text('Finished.')]],
        [[Tool('report', {'task_id': 'T1', 'spoken': 'Merging.', 'needs_user': False,
                         'authorization_id': 'U1',
                         'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}})],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Cleaning up.', 'needs_user': False,
                         'authorization_id': 'U1', 'approval': {'action': 'delete_branch', 'repo': 'pumpkit',
                                                              'branch': 'feat/old-idea', 'sha': TIP}})],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Finished.', 'needs_user': False})]],
    )
    await session.start()
    await session.say('Merge PR 42 and clean up its branch unattended')
    await session.replied('Finished.')
    await session.end()
    assert gh.actions[0] == merge_argv()
    assert len(gh.actions) == 2
    assert 'pumpkit-io/pumpkit:feat/old-idea' not in gh.branches
    assert 'scope="task"' in session.backend.session('orchestrator').received[1]


async def test_session_permission_can_be_granted_and_revoked_without_delegating() -> None:

    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
    session = a_session(
        FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('I will work unattended this session.'), Tool('authorize', {'scope': 'session', 'description': 'Work unattended'})],
         [Text('I will ask again.'), Tool('revoke_authorization', {'authorization_id': 'U1'})],
         [Text('I will check it.'), Tool('delegate', {'request': 'Merge PR 42'})],
         [Text('Permission is needed.')]],
        [[Tool('report', {'task_id': 'T1', 'spoken': 'Merging.', 'needs_user': False,
                         'authorization_id': 'U1',
                         'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}}),
          Tool('report', {'task_id': 'T1', 'spoken': 'Permission is needed.', 'needs_user': True})]],
    )
    await session.start()
    await session.say('For this session work unattended')
    await session.say('Actually ask permission again')
    await session.say('Prepare a merge')
    await session.replied('Permission is needed.')
    await session.end()
    [granted] = results(session.backend, 'talker', 'authorize')
    assert 'U1' in str(granted.result)
    assert 'revoked' in str(results(session.backend, 'orchestrator', 'report')[0].result)
    assert gh.actions == []


async def test_task_permission_survives_restart_and_session_permission_does_not(tmp_path: Path) -> None:

    for scope in ('task', 'session'):
        path = tmp_path / f'{scope}.json'
        gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
        grant = {'scope': scope, 'description': 'Complete the merge unattended'}
        opening = ([Tool('authorize', grant), Tool('delegate', {'request': 'Merge 42'})] if scope == 'session' else
                   [Tool('delegate', {'request': 'Merge 42', 'authorization': grant})])
        first = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
            [[Text('I will prepare it.'), *opening], [Text('Waiting for the release window.')]],
            [[Tool('report', {'task_id': 'T1', 'spoken': 'Waiting for release window.', 'needs_user': True})]],
            state_path=path)
        await first.start()
        await first.say('Merge 42 unattended when ready')
        await first.replied('Waiting for the release window.')
        await first.end()
        second = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh, [[Text('Ready.')]],
            [[Tool('report', {'task_id': 'T1', 'spoken': 'Merging.', 'needs_user': False, 'authorization_id': 'U1',
                             'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}}),
              *([] if scope == 'task' else [Tool('report', {'task_id': 'T1', 'spoken': 'Permission needed.', 'needs_user': True})])],
             [Tool('report', {'task_id': 'T1', 'spoken': 'Merged.', 'needs_user': False})]], state_path=path)
        await second.start()
        await until(lambda: bool(results(second.backend, 'orchestrator', 'report')))
        await second.end()
        assert gh.actions == ([merge_argv()] if scope == 'task' else [])
        first_result = str(results(second.backend, 'orchestrator', 'report')[0].result)
        assert ('Authorized A1' in first_result) if scope == 'task' else ('Unknown Authorization' in first_result)


async def test_revocation_during_target_reads_prevents_dispatch() -> None:

    class PausedGh(StubGh):
        def __init__(self, pause: int) -> None:
            super().__init__(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
            self.pause = pause
            self.view_count = 0
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def __call__(self, argv: tuple[str, ...]) -> tuple[int, str]:
            result = await super().__call__(argv)
            if argv[:3] == ('gh', 'pr', 'view'):
                self.view_count += 1
                if self.view_count == self.pause:
                    self.entered.set()
                    await self.release.wait()
            return result

    for read in (1, 2):
        gh = PausedGh(read)
        session = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
            [[Text('I will merge it.'), Tool('delegate', {'request': 'Merge 42',
                'authorization': {'scope': 'action', 'description': 'Merge PR 42'}})],
             [Text('Permission withdrawn.'), Tool('revoke_authorization', {'authorization_id': 'U1'})]],
            [[Tool('report', {'task_id': 'T1', 'spoken': 'Merging.', 'needs_user': False, 'authorization_id': 'U1',
                             'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}})]])
        await session.start()
        await session.say('Merge 42')
        await until(gh.entered.is_set)
        await session.say('Stop, do not merge it')
        gh.release.set()
        await session.end()
        assert gh.actions == [], f'Revoked permission executed after target read {read}'


async def test_action_permission_is_spent_after_one_concrete_action() -> None:

    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)},
                branches={'pumpkit-io/pumpkit:feat/old-idea': TIP})
    session = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('Merging.'), Tool('delegate', {'request': 'Merge 42',
            'authorization': {'scope': 'action', 'description': 'Merge PR 42'}})], [Text('Permission needed.')]],
        [[Tool('report', {'task_id': 'T1', 'spoken': 'Merging.', 'needs_user': False, 'authorization_id': 'U1',
                         'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}})],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Deleting.', 'needs_user': False, 'authorization_id': 'U1',
                         'approval': {'action': 'delete_branch', 'repo': 'pumpkit', 'branch': 'feat/old-idea', 'sha': TIP}}),
          Tool('report', {'task_id': 'T1', 'spoken': 'Permission needed.', 'needs_user': True})]])
    await session.start()
    await session.say('Merge PR 42')
    await session.replied('Permission needed.')
    await session.end()
    assert len(gh.actions) == 1
    assert 'used' in str(results(session.backend, 'orchestrator', 'report')[1].result)


async def test_new_permission_wakes_a_task_already_waiting_on_approval() -> None:

    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
    session = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('I will prepare it.'), Tool('delegate', {'request': 'Prepare merge'})],
         [present(), Text('May I merge PR 42?')],
         [Text('I will finish it.'), Tool('authorize', {'scope': 'task', 'task_id': 'T1',
                                                     'description': 'Finish the merge unattended'})],
         [Text('Merged.')]],
        [[ask_merge()],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Merging.', 'needs_user': False, 'authorization_id': 'U1',
                         'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}})],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Merged.', 'needs_user': False})]])
    await session.start()
    await session.say('Prepare a merge')
    await session.replied('May I merge PR 42?')
    await session.say('Finish this task unattended without asking me again')
    await session.replied('Merged.')
    await session.end()
    assert gh.actions == [merge_argv()]
    assert not results(session.backend, 'talker', 'approve')


@pytest.mark.parametrize('heard', ['Yes, I approve.', 'That sounds right to me, go ahead.', 'Sì, approvo.',
                                   'Procedi pure con questa operazione.'])
async def test_hub_accepts_the_talkers_semantic_decision_without_a_word_list(heard: str) -> None:
    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
    metrics = RecordingMetrics()
    session = a_session(FakeClock(), RecordingOutput(), metrics, gh,
        [[Text('I will prepare it.'), Tool('delegate', {'request': 'Prepare merge'})],
         [present(), Text('May I merge PR 42?')], [Text('Merging.'), approve()], [Text('Merged.')]],
        [[ask_merge()], [Tool('report', {'task_id': 'T1', 'spoken': 'Merged.', 'needs_user': False})]])
    await session.start()
    await session.say('Prepare a merge')
    await session.replied('May I merge PR 42?')
    await session.say(heard)
    await session.end()
    assert gh.actions == [merge_argv()]
    assert next(r['words'] for r in metrics.records if r.get('kind') == 'approval') == heard


async def test_interrupted_presentation_cannot_authorize_an_action() -> None:
    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
    session = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('I will prepare it.'), Tool('delegate', {'request': 'Prepare merge'})],
         [present(), Text('May I'), Abort()], [Text('Let me clarify.'), approve()]], [[ask_merge()]])
    await session.start()
    await session.say('Prepare a merge')
    await session.replied('May I')
    await session.say('Yes')
    await session.end()
    assert 'has not been presented' in str(results(session.backend, 'talker', 'approve')[0].result)
    assert gh.actions == []


async def test_task_permission_cannot_be_used_on_another_task_and_ends_with_its_task() -> None:
    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
    session = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('I will investigate.'), Tool('delegate', {'request': 'Investigate',
                'authorization': {'scope': 'task', 'description': 'Finish this investigation unattended'}})],
         [Text('Investigation complete.')],
         [Text('I will prepare the merge.'), Tool('delegate', {'request': 'Prepare merge'})],
         [Text('Permission needed.')],
         [Text('Here is the status.'), Tool('status')]],
        [[Tool('report', {'task_id': 'T1', 'spoken': 'Investigation complete.', 'needs_user': False})],
         [Tool('report', {'task_id': 'T2', 'spoken': 'Merging.', 'needs_user': False, 'authorization_id': 'U1',
                         'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}}),
          Tool('report', {'task_id': 'T2', 'spoken': 'Permission needed.', 'needs_user': True})]])
    await session.start()
    await session.say('Investigate unattended')
    await session.replied('Investigation complete.')
    await session.say('Prepare a merge')
    await session.replied('Permission needed.')
    await session.say('Status')
    await session.end()
    assert 'ended' in str(results(session.backend, 'orchestrator', 'report')[1].result)
    assert 'id="U1"' not in str(results(session.backend, 'talker', 'status')[0].result)
    assert gh.actions == []


async def test_old_state_loads_pending_approvals_without_inventing_permission(tmp_path: Path) -> None:
    path = tmp_path / 'state.json'
    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
    first = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('Preparing.'), Tool('delegate', {'request': 'Prepare merge'})], [present(), Text('May I merge?')]],
        [[ask_merge()]], state_path=path)
    await first.start()
    await first.say('Prepare a merge')
    await first.replied('May I merge?')
    await first.end()
    saved = json.loads(path.read_text())
    del saved['authorizations']
    for approval in saved['approvals']:
        del approval['authorization_id']
    path.write_text(json.dumps(saved))
    second = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('Here is the status.'), Tool('status')]], [[]], state_path=path)
    await second.start()
    await until(lambda: bool(second.backend.all_sessions('orchestrator')))
    await second.say('Status')
    await second.end()
    status = str(results(second.backend, 'talker', 'status')[0].result)
    assert 'A1 pending' in status
    assert '<authorization ' not in status
    assert gh.actions == []


async def test_revoking_permission_blocks_an_already_started_subagents_next_tool() -> None:
    gh = StubGh()
    session = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('I will deploy.'), Tool('delegate', {'request': 'Deploy pumpkit',
            'authorization': {'scope': 'task', 'description': 'Deploy pumpkit unattended'}})],
         [Text('I have stopped the deployment.'), Tool('revoke_authorization', {'authorization_id': 'U1'})]],
        [[Tool('report', {'task_id': 'T1', 'spoken': 'Deploying.', 'needs_user': False, 'authorization_id': 'U1',
                         'approval': {'action': 'subagent_action', 'description': DEPLOY}})],
         [Tool('spawn_subagent', {'repo': 'pumpkit', 'title': 'Deploy', 'brief': 'Deploy pumpkit', 'model': 'haiku',
                                 'effort': 'low', 'approval_id': 'A1'})]])
    session.backend.subagent_close_gate = asyncio.Event()
    await session.start()
    await session.say('Deploy pumpkit unattended')
    await until(lambda: bool(session.backend.all_sessions('subagent')) and
                bool(session.backend.session('subagent').received))
    subagent = session.backend.session('subagent')
    lease = subagent.spec.before_tool
    assert lease is not None
    assert await lease('Bash', {'command': 'sh scripts/ship.sh'}) is None
    assert DEPLOY in subagent.received[0]
    await session.say('Stop the deployment')
    refusal = await lease('Bash', {'command': 'sh scripts/ship.sh'})
    assert refusal is not None and 'withdrawn' in refusal.reason
    session.backend.subagent_close_gate.set()
    await session.end()


@pytest.mark.parametrize('attempt_spawn', [False, True])
async def test_unstarted_approved_subagent_action_does_not_keep_the_task_working(attempt_spawn: bool) -> None:
    metrics = RecordingMetrics()
    spawn = Tool('spawn_subagent', {'repo': 'unknown-repo', 'title': 'Deploy', 'brief': 'Deploy',
                                  'model': 'haiku', 'effort': 'low', 'approval_id': 'A1'})
    status: list[Step] = [Text('Status.'), Tool('status')]
    session = a_session(FakeClock(), RecordingOutput(), metrics, StubGh(),
        [[Text('Deploying.'), Tool('delegate', {'request': 'Deploy',
            'authorization': {'scope': 'task', 'description': 'Deploy unattended'}})], status, status],
        [[Tool('report', {'task_id': 'T1', 'spoken': 'Deploying.', 'needs_user': False, 'authorization_id': 'U1',
                         'approval': {'action': 'subagent_action', 'description': DEPLOY}})],
         [spawn] if attempt_spawn else []])
    await session.start()
    await session.say('Deploy unattended')
    await until(lambda: len([r for r in metrics.records if r.get('role') == 'orchestrator']) == 2)
    await session.say('Status')
    await session.end()
    assert 'T1 failed' in str(results(session.backend, 'talker', 'status')[-1].result)
    assert '<authorization ' not in str(results(session.backend, 'talker', 'status')[-1].result)


async def test_new_permission_removes_a_queued_approval_question() -> None:
    from tests.test_approvals import AlwaysQuiet

    class HeldAnnouncements(AlwaysQuiet):
        async def quiet(self) -> None:
            await asyncio.Event().wait()

    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD)})
    session = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('Preparing.'), Tool('delegate', {'request': 'Prepare merge'})],
         [Text('Finishing.'), Tool('authorize', {'task_id': 'T1', 'scope': 'task', 'description': 'Finish unattended'})],
         [Text('Merged.')]],
        [[ask_merge()],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Merging.', 'needs_user': False, 'authorization_id': 'U1',
                         'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}})],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Merged.', 'needs_user': False})]], floor=HeldAnnouncements())
    await session.start()
    await session.say('Prepare a merge')
    await until(lambda: bool(results(session.backend, 'orchestrator', 'report')))
    await session.say('Finish this task unattended')
    await until(lambda: bool(gh.actions))
    await session.end()
    assert not any('approval="A1"' in message for message in session.backend.session('talker').received)
    assert gh.actions == [merge_argv()]


async def test_session_permission_covers_a_waiting_task_and_future_tasks() -> None:
    gh = StubGh(prs={'pumpkit-io/pumpkit#42': ('OPEN', HEAD), 'pumpkit-io/pumpkit#43': ('OPEN', HEAD)})
    session = a_session(FakeClock(), RecordingOutput(), RecordingMetrics(), gh,
        [[Text('Preparing.'), Tool('delegate', {'request': 'Prepare merge 42'})],
         [present(), Text('May I merge PR 42?')],
         [Text('I will work unattended.'), Tool('authorize', {'scope': 'session', 'description': 'Merge unattended this session'})],
         [Text('Merged 42.')],
         [Text('I will handle 43.'), Tool('delegate', {'request': 'Merge PR 43'})],
         [Text('Merged 43.')]],
        [[ask_merge()],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Merging 42.', 'needs_user': False, 'authorization_id': 'U1',
                         'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 42, 'sha': HEAD}})],
         [Tool('report', {'task_id': 'T1', 'spoken': 'Merged 42.', 'needs_user': False})],
         [Tool('report', {'task_id': 'T2', 'spoken': 'Merging 43.', 'needs_user': False, 'authorization_id': 'U1',
                         'approval': {'action': 'merge_pr', 'repo': 'pumpkit', 'pr': 43, 'sha': HEAD}})],
         [Tool('report', {'task_id': 'T2', 'spoken': 'Merged 43.', 'needs_user': False})]])
    await session.start()
    await session.say('Prepare merge 42')
    await session.replied('May I merge PR 42?')
    await session.say('For this session merge unattended without asking')
    await session.replied('Merged 42.')
    await session.say('Handle PR 43 too')
    await session.replied('Merged 43.')
    await session.end()
    assert [argv[3] for argv in gh.actions] == ['42', '43']
    assert not results(session.backend, 'talker', 'approve')

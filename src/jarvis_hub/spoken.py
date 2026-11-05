"""Durable Task scope choices for already locally admitted spoken revisions."""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from html import escape
from typing import Any, Literal, Protocol, cast

from jarvis_hub.agents.backend import Refused
from jarvis_hub.state import TimeShift

SCOPE_SECONDS = 30.0
Disposition = Literal['revise_tasks', 'new_request', 'preserve_tasks']


class Clock(Protocol):
    def now(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


@dataclass
class SpokenScope:
    id: str
    revision: int
    segments: tuple[str, ...]
    previous: int
    task_ids: tuple[str, ...]
    history_task_ids: tuple[str, ...]
    created: float
    deadline: float
    pending: bool
    semantic_start: int = 0
    attempt: int = 0
    failed: bool = False
    disposition: str = ''
    selected: tuple[str, ...] = ()
    typed_context: tuple[str, ...] = ()
    task_context: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def all_segments(self) -> tuple[str, ...]:
        return self.segments + self.typed_context

    @property
    def text(self) -> str:
        return ' '.join(self.all_segments).strip()

    @property
    def current_text(self) -> str:
        return ' '.join(self.all_segments[self.semantic_start:]).strip()

    def context(self) -> str:
        tasks = ','.join(self.task_ids) or 'none'
        pieces = [f'<pending_scope id="{self.id}" revision="{self.revision}" attempt="{self.attempt}" '
                  f'tasks="{tasks}" new_segments_from="{self.previous}" semantic_start="{self.semantic_start}">']
        pieces += [f'<task id="{task_id}">{escape(self.task_context.get(task_id, task_id), quote=False)}</task>'
                   for task_id in self.task_ids]
        pieces += [f'<segment index="{index}" source="{"speech" if index < len(self.segments) else "typed"}">{escape(text, quote=False)}</segment>'
                   for index, text in enumerate(self.all_segments)]
        return '\n'.join([*pieces, '</pending_scope>'])


@dataclass(frozen=True)
class ScopeChoice:
    scope: SpokenScope
    disposition: Disposition
    task_ids: tuple[str, ...]
    start: int


class SpokenScopes:
    def __init__(self, clock: Clock, *, change: Callable[[], None], notice: Callable[[str], None]) -> None:
        self._clock, self._change, self._notice = clock, change, notice
        self._records: dict[str, SpokenScope] = {}
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._versions: dict[str, int] = {}

    def get(self, request_id: str | None) -> SpokenScope | None:
        return self._records.get(request_id) if request_id is not None else None

    def pending(self) -> tuple[SpokenScope, ...]:
        return tuple(scope for scope in self._records.values() if scope.pending)

    def held(self, task_id: str) -> bool:
        return any(task_id in scope.task_ids for scope in self.pending())

    def version(self, task_id: str) -> int:
        return self._versions.get(task_id, 0)

    def _advance(self, task_ids: Iterable[str]) -> None:
        for task_id in task_ids:
            self._versions[task_id] = self.version(task_id) + 1

    def held_tasks(self) -> set[str]:
        return {task_id for scope in self.pending() for task_id in scope.task_ids}

    def admit(self, request_id: str, revision: int, segments: tuple[str, ...], arrived: float) -> SpokenScope:
        previous = self.get(request_id)
        if previous is not None and revision <= previous.revision:
            return previous
        if not segments or len(' '.join(segments)) > 6000:
            raise ValueError('Spoken scope exceeds its retained request limit.')
        if previous is not None and segments[:len(previous.segments)] != previous.segments:
            raise ValueError('A spoken revision must preserve its raw segment history.')
        scope = SpokenScope(request_id, revision, segments, len(previous.segments) if previous else 0,
                            previous.task_ids if previous else (), previous.history_task_ids if previous else (),
                            arrived, self._clock.now() + SCOPE_SECONDS, revision > 0,
                            semantic_start=previous.semantic_start if previous else 0,
                            task_context=dict(previous.task_context) if previous else {})
        self._records[request_id] = scope
        if previous is not None:
            previous.changed.set()
        if scope.pending:
            self._advance(scope.task_ids)
            self._arm(scope)
        self._change()
        return scope

    def bind(self, request_id: str | None, task_id: str, context: str = "") -> None:
        if (scope := self.get(request_id)) is not None:
            if context:
                scope.task_context[task_id] = context
            scope.task_ids = tuple(dict.fromkeys([*scope.task_ids, task_id]))
            scope.history_task_ids = tuple(dict.fromkeys([*scope.history_task_ids, task_id]))

    def typed(self, text: str) -> None:
        for scope in self.pending():
            scope.previous = len(scope.all_segments)
            scope.typed_context += (text,)
            scope.attempt += 1
            self._advance(scope.task_ids)
        self._change()

    def activate(self) -> dict[str, tuple[int, int]]:
        for scope in self.pending():
            scope.attempt += 1
            scope.failed = False
            scope.deadline = self._clock.now() + SCOPE_SECONDS
            scope.changed.clear()
            self._arm(scope)
        self._change()
        return {scope.id: (scope.revision, scope.attempt) for scope in self.pending()}

    def settled(self, authority: dict[str, tuple[int, int]]) -> tuple[SpokenScope, ...]:
        return tuple(scope for scope in self._records.values() if not scope.pending
                     and authority.get(scope.id) == (scope.revision, scope.attempt))

    def context(self) -> str:
        pending = self.pending()
        return '\n'.join(['<spoken_scopes>', *(scope.context() for scope in pending), '</spoken_scopes>']) if pending else ''

    def choose(self, args: dict[str, Any], authority: dict[str, tuple[int, int]]) -> ScopeChoice | Refused:
        request_id = args.get('scope_id')
        if not isinstance(request_id, str) or any(not isinstance(args.get(name), int) or isinstance(args.get(name), bool)
                                                  for name in ('revision', 'attempt')):
            return Refused('A scope decision needs its exact id and integer revision/attempt.')
        scope = self.get(request_id)
        if scope is None or not scope.pending:
            return Refused('This spoken scope is not pending; it cannot be resolved again.')
        token = (args.get('revision'), args.get('attempt'))
        if token != (scope.revision, scope.attempt) or authority.get(scope.id) != token or scope.failed:
            return Refused('This scope decision is stale. Use the current pending scope and attempt.')
        disposition = args.get('disposition')
        if disposition not in ('revise_tasks', 'new_request', 'preserve_tasks'):
            return Refused('disposition must be revise_tasks, new_request, or preserve_tasks.')
        raw = args.get('task_ids', [])
        if not isinstance(raw, list) or any(not isinstance(task_id, str) for task_id in cast(list[object], raw)):
            return Refused('task_ids must list the complete set of owned Tasks that change.')
        chosen = tuple(dict.fromkeys(cast(list[str], raw)))
        if any(task_id not in scope.task_ids for task_id in chosen):
            return Refused('Choose only Task IDs owned by this scope: ' + ', '.join(scope.task_ids))
        if (disposition == 'revise_tasks') != bool(chosen):
            return Refused('revise_tasks needs affected Task IDs; other dispositions must leave task_ids empty.')
        start = args.get('from_segment', scope.previous)
        if disposition == 'new_request':
            if not isinstance(start, int) or isinstance(start, bool) or not scope.previous <= start < len(scope.all_segments):
                return Refused('from_segment must name an observed new raw segment boundary.')
        else:
            if 'from_segment' in args:
                return Refused('Only a new_request decision may change the current raw segment boundary.')
            start = scope.semantic_start
        return ScopeChoice(scope, disposition, chosen, start)

    def commit(self, choice: ScopeChoice) -> None:
        scope = choice.scope
        self._advance(scope.task_ids)
        scope.pending, scope.failed = False, False
        scope.disposition, scope.selected = choice.disposition, choice.task_ids
        if choice.disposition == 'new_request':
            scope.semantic_start = choice.start
            scope.task_ids = ()
        timer = self._timers.pop(scope.id, None)
        if timer is not None:
            timer.cancel()
        scope.changed.set()
        self._change()

    async def wait(self, task_id: str) -> bool:
        while not self._closed:
            holders = [scope for scope in self.pending() if task_id in scope.task_ids]
            if not holders:
                return True
            scope = holders[0]
            if scope.failed:
                return False
            await scope.changed.wait()
        return False

    def fail(self, authority: dict[str, tuple[int, int]]) -> None:
        for scope in self.pending():
            if authority.get(scope.id) == (scope.revision, scope.attempt):
                self._failed(scope)

    def _failed(self, scope: SpokenScope) -> None:
        if not scope.failed:
            scope.failed = True
            scope.changed.set()
            self._notice('Spoken request needs scope clarification. Existing work remains held; name the affected Tasks.'
                         if scope.task_ids else 'Spoken request needs clarification about whether it continues the earlier request.')
            self._change()

    def _arm(self, scope: SpokenScope) -> None:
        if previous := self._timers.pop(scope.id, None):
            previous.cancel()
        async def expire() -> None:
            await self._clock.sleep(max(0.0, scope.deadline - self._clock.now()))
            if self.get(scope.id) is scope and scope.pending:
                self._failed(scope)
        self._timers[scope.id] = asyncio.create_task(expire())

    def prune(self) -> None:
        resolved = [scope.id for scope in self._records.values() if not scope.pending]
        removed = resolved[:-64]
        for request_id in removed:
            self._records.pop(request_id)
        if removed:
            self._change()

    def dump(self, to_wall: TimeShift) -> list[dict[str, Any]]:
        return [dict(id=scope.id, revision=scope.revision, segments=scope.segments, previous=scope.previous,
                     task_ids=scope.task_ids, history_task_ids=scope.history_task_ids, created=to_wall(scope.created),
                     deadline=to_wall(scope.deadline), pending=scope.pending, semantic_start=scope.semantic_start,
                     attempt=scope.attempt, failed=scope.failed, disposition=scope.disposition, selected=scope.selected,
                     task_context=scope.task_context, typed_context=scope.typed_context)
                for scope in self._records.values()]

    def restore(self, records: Iterable[dict[str, Any]], from_wall: TimeShift) -> None:
        for record in records:
            values = dict(record)
            values.setdefault('typed_context', ())
            for name in ('segments', 'typed_context', 'task_ids', 'history_task_ids', 'selected'):
                values[name] = tuple(values[name])
            values['created'], values['deadline'] = from_wall(values['created']), from_wall(values['deadline'])
            scope = SpokenScope(**values)
            if not scope.segments or len(' '.join(scope.segments)) > 6000 or not 0 <= scope.semantic_start < len(scope.all_segments):
                raise ValueError('Invalid retained spoken scope.')
            self._records[scope.id] = scope
            if scope.pending:
                self._arm(scope)

    async def close(self) -> None:
        self._closed = True
        for scope in self._records.values():
            scope.changed.set()
        tasks = list(self._timers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

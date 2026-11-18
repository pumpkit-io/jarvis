"""User permission interpreted by the Talker, with scope and provenance owned by the Hub."""

from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from typing import Any, Literal, cast, get_args

from jarvis_hub.agents.backend import Refused
from jarvis_hub.clients import TERMINAL
from jarvis_hub.state import Fields, TimeShift, numbered, one_of

AuthorizationScope = Literal["action", "task", "session"]
AuthorizationState = Literal["active", "used", "revoked", "ended"]


@dataclass
class Authorization:
    id: str
    scope: AuthorizationScope
    description: str
    words: str
    source: str
    task_id: str | None
    task_revision: int
    state: AuthorizationState = "active"
    approval_id: str | None = None
    client_id: str = TERMINAL


_SAVED = Fields(("id", "scope", "description", "words", "source", "task_id", "task_revision", "state", "approval_id", "client_id"))


class Authorizations:
    def __init__(self) -> None:
        self._all: dict[str, Authorization] = {}
        self._ids = 0

    @staticmethod
    def validate(spec: object, *, delegate: bool = False) -> Refused | None:
        if not isinstance(spec, dict):
            return Refused("authorization is an object with scope and description.")
        spec = cast(dict[str, Any], spec)
        scopes = ("action", "task") if delegate else get_args(AuthorizationScope)
        if spec.get("scope") not in scopes:
            return Refused("authorization.scope is one of " + ", ".join(scopes) + ".")
        if not isinstance(spec.get("description"), str) or not spec["description"].strip():
            return Refused("authorization.description states the permitted outcome, conditions and exclusions.")
        return None

    def issue(self, spec: dict[str, Any], *, words: str, source: str, task_id: str | None, revision: int, client_id: str = TERMINAL) -> Authorization:
        for existing in self._all.values():
            if (existing.source, existing.scope, existing.task_id, existing.description) == (
                source, spec["scope"], task_id, spec["description"].strip()
            ):
                return existing
        self._ids += 1
        authorization = Authorization(f"U{self._ids}", spec["scope"], spec["description"].strip(), words,
                                      source, task_id, revision, client_id=client_id)
        self._all[authorization.id] = authorization
        return authorization

    def get(self, authorization_id: object) -> Authorization | None:
        return self._all.get(str(authorization_id).upper())

    def check(self, authorization_id: object, task_id: str, revision: int, *, approval_id: str | None = None) -> Authorization | Refused:
        authorization = self.get(authorization_id)
        if authorization is None:
            return Refused(f"Unknown Authorization {authorization_id}.")
        reusable = authorization.state == "used" and authorization.approval_id == approval_id and approval_id is not None
        if authorization.state != "active" and not reusable:
            return Refused(f"{authorization.id} is {authorization.state}.")
        if authorization.scope != "session" and (authorization.task_id, authorization.task_revision) != (task_id, revision):
            return Refused(f"{authorization.id} does not cover this Task revision.")
        return authorization

    def bind(self, authorization: Authorization, approval_id: str) -> None:
        if authorization.scope == "action":
            authorization.state, authorization.approval_id = "used", approval_id

    def revoke(self, authorization_id: object) -> Authorization | Refused:
        authorization = self.get(authorization_id)
        if authorization is None:
            return Refused(f"Unknown Authorization {authorization_id}.")
        authorization.state = "revoked"
        return authorization

    def end_task(self, task_id: str) -> None:
        for authorization in self._all.values():
            if authorization.task_id == task_id and authorization.state in ("active", "used"):
                authorization.state = "ended"

    def settle(self, is_open: Callable[[str], bool]) -> None:
        for authorization in self._all.values():
            if authorization.task_id is not None and not is_open(authorization.task_id):
                self.end_task(authorization.task_id)

    def revise(self, task_id: str, revision: int, *, preserve: bool) -> None:
        for authorization in self._all.values():
            if authorization.task_id == task_id and authorization.state == "active":
                if preserve:
                    authorization.task_revision = revision
                else:
                    authorization.state = "revoked"

    def context(self, task_id: str | None = None) -> str:
        selected = [a for a in self._all.values() if a.state == "active" and
                    (task_id is None or a.scope == "session" or a.task_id == task_id)]
        entries = [f'<authorization id="{a.id}" scope="{a.scope}" task="{a.task_id or ""}">'
                   f'{escape(a.description)}\nUser words: {escape(a.words)}</authorization>' for a in selected]
        return "\n".join(entries)

    def dump(self, task_ids: set[str], to_wall: TimeShift) -> list[dict[str, Any]]:
        return [_SAVED.dump(a, to_wall) for a in self._all.values() if a.scope != "session" and a.task_id in task_ids]

    @staticmethod
    def load(records: list[dict[str, Any]], from_wall: TimeShift) -> list[Authorization]:
        loaded: list[Authorization] = []
        for record in records:
            authorization = Authorization(**_SAVED.load({"client_id": TERMINAL, **record}, from_wall))
            numbered(authorization.id, "U")
            one_of(authorization.scope, ("action", "task"))
            one_of(authorization.state, get_args(AuthorizationState))
            numbered(authorization.task_id, "T")
            if (not authorization.words.strip() or not authorization.description.strip() or not authorization.source
                    or type(authorization.client_id) is not str or not authorization.client_id
                    or type(authorization.task_revision) is not int or authorization.task_revision < 0):
                raise ValueError("Invalid Authorization provenance or Task revision")
            loaded.append(authorization)
        return loaded

    def restore(self, authorizations: list[Authorization]) -> None:
        for authorization in authorizations:
            self._all[authorization.id] = authorization
            self._ids = max(self._ids, numbered(authorization.id, "U"))

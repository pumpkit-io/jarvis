from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

TERMINAL = "terminal"
ClientKind = Literal["terminal", "iphone", "reference"]


@dataclass(frozen=True)
class Client:
    id: str
    kind: ClientKind
    connected: bool = False
    can_show_text: bool = True
    can_speak: bool = False
    silence: bool = False


class Clients:
    def __init__(self, *, terminal_speech: bool, on_focus: Callable[[], None]) -> None:
        self._clients = {TERMINAL: Client(TERMINAL, "terminal", connected=True, can_speak=terminal_speech)}
        self._focus = TERMINAL
        self._on_focus = on_focus

    @property
    def focus(self) -> str:
        return self._focus

    def get(self, client_id: str) -> Client | None:
        return self._clients.get(client_id)

    def register(self, client: Client) -> None:
        if not client.id:
            raise ValueError("A Client needs an id")
        self._clients[client.id] = client

    def disconnect(self, client_id: str) -> None:
        client = self._clients[client_id]
        self._clients[client_id] = replace(client, connected=False)

    def take_focus(self, client_id: str) -> None:
        if client_id not in self._clients:
            raise ValueError(f"Unknown Client {client_id}")
        if self._focus != client_id:
            self._focus = client_id
            self._on_focus()

    def restore_focus(self, client_id: str) -> None:
        if not client_id:
            raise ValueError("Focus needs a Client id")
        self._focus = client_id

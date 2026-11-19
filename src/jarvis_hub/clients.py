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
    talking: bool = False
    playing: bool = False


class Clients:
    def __init__(self, *, terminal_speech: bool, on_focus: Callable[[], None], on_change: Callable[[], None] = lambda: None,
                 save_silence: Callable[[str, bool], None] | None = None) -> None:
        self._clients = {TERMINAL: Client(TERMINAL, "terminal", connected=True, can_speak=terminal_speech)}
        self._focus = TERMINAL
        self._on_focus = on_focus
        self._on_change = on_change
        self._save_silence: Callable[[str, bool], None] = save_silence or (lambda client_id, silence: None)

    @property
    def focus(self) -> str:
        return self._focus

    def all(self) -> tuple[Client, ...]:
        return tuple(self._clients.values())

    def get(self, client_id: str) -> Client | None:
        return self._clients.get(client_id)

    def register(self, client: Client) -> None:
        if not client.id:
            raise ValueError("A Client needs an id")
        previous = self._clients.get(client.id)
        if previous is None or previous.silence != client.silence:
            # Persist before change callbacks can release held news.
            self._save_silence(client.id, client.silence)
        self._clients[client.id] = client
        self._on_change()

    def set_silence(self, client_id: str, silence: bool) -> None:
        self.register(replace(self._clients[client_id], silence=silence))

    def disconnect(self, client_id: str) -> None:
        client = self._clients[client_id]
        self._clients[client_id] = replace(client, connected=False, talking=False, playing=False)
        self._on_change()

    def take_focus(self, client_id: str) -> None:
        if client_id not in self._clients:
            raise ValueError(f"Unknown Client {client_id}")
        if self._focus != client_id:
            self._focus = client_id
            self._on_focus()
            self._on_change()

    def restore_focus(self, client_id: str) -> None:
        if not client_id:
            raise ValueError("Focus needs a Client id")
        self._focus = client_id

    @property
    def remote_activity(self) -> bool:
        return any(client.id != TERMINAL and client.connected and (client.talking or client.playing)
                   for client in self._clients.values())

    def set_talking(self, client_id: str, talking: bool) -> None:
        self._clients[client_id] = replace(self._clients[client_id], talking=talking)
        self._on_change()

    def set_playing(self, client_id: str, playing: bool) -> None:
        self._clients[client_id] = replace(self._clients[client_id], playing=playing)
        self._on_change()

"""Terminal line editing and an immediate local voice stop; pipes retain line input."""

import asyncio
import io
import threading
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import ExitStack
from types import TracebackType
from typing import Self, TextIO, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import Input, create_input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.output import DummyOutput, Output, create_output
from prompt_toolkit.patch_stdout import StdoutProxy

from jarvis_hub.hub import Clock, Line

PROMPT = '› '


async def typed_lines(stdin: TextIO, clock: Clock) -> AsyncIterator[Line]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Line | None] = asyncio.Queue()
    if isinstance(stdin, io.TextIOWrapper):
        stdin.reconfigure(errors='replace')

    def read() -> None:
        try:
            for line in stdin:
                loop.call_soon_threadsafe(queue.put_nowait, Line(line.rstrip('\n'), clock.now()))
        except OSError:
            pass  # A lost terminal ends input without stopping background work.
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    # A blocked pipe read must not keep the process alive after shutdown.
    threading.Thread(target=read, daemon=True).start()
    while (line := await queue.get()) is not None:
        yield line


class TerminalInput:
    def __init__(self, stdin: TextIO, stdout: TextIO, stderr: TextIO, clock: Clock, *,
                 input: Input | None = None, output: Output | None = None) -> None:
        self._stdin, self.stdout, self.stderr, self._clock = stdin, stdout, stderr, clock
        self.interactive = input is not None or stdin.isatty()
        self._input, self._output = input, output
        self._stack = ExitStack()
        self._session: PromptSession[str] | None = None
        self._stop: Callable[[], None] = lambda: None

    def __enter__(self) -> Self:
        if not self.interactive:
            return self
        terminal_input = self._input
        if terminal_input is None:
            terminal_input = create_input(stdin=self._stdin)
            self._stack.callback(terminal_input.close)
        display = self.stdout if self.stdout.isatty() else self.stderr
        terminal_output = self._output or (create_output(stdout=display) if display.isatty() else DummyOutput())
        self._stack.enter_context(create_app_session(input=terminal_input, output=terminal_output))
        proxy = StdoutProxy(sleep_between_writes=0, raw=True)
        self._stack.callback(proxy.close)
        bindings = KeyBindings()

        @bindings.add('c-g', eager=True)
        def stop(event: KeyPressEvent) -> None:
            self._stop()

        self._session = PromptSession(PROMPT, input=terminal_input, output=terminal_output, key_bindings=bindings)
        if self.stdout.isatty():
            self.stdout = cast(TextIO, proxy)
        if self.stderr.isatty():
            self.stderr = cast(TextIO, proxy)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self._stack.close()

    async def lines(self, stop: Callable[[], None]) -> AsyncGenerator[Line, None]:
        self._stop = stop
        session = self._session
        if session is None:
            async for line in typed_lines(self._stdin, self._clock):
                yield line
            return
        while True:
            try:
                text = await session.prompt_async()
            except EOFError:
                return
            yield Line(text, self._clock.now())

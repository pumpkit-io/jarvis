"""Immediate terminal controls preserve the editable request buffer."""

import asyncio
import io

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from jarvis_hub.terminal_input import TerminalInput
from tests.fakes import FakeClock, until


async def test_ctrl_g_requests_stop_before_enter_and_preserves_the_typed_buffer() -> None:
    clock = FakeClock()
    stops: list[float] = []
    with create_pipe_input() as pipe:
        with TerminalInput(io.StringIO(), io.StringIO(), io.StringIO(), clock, input=pipe, output=DummyOutput()) as terminal:
            lines = terminal.lines(lambda: stops.append(clock.now()))
            reading = asyncio.ensure_future(anext(lines))
            pipe.send_text('Show the status')
            pipe.send_bytes(b'\x07')
            await until(lambda: len(stops) == 1)
            assert not reading.done()
            pipe.send_text(' instead\r')
            line = await asyncio.wait_for(reading, timeout=1)
            assert line.text == 'Show the status instead'
            await lines.aclose()


async def test_stop_preserves_unicode_and_normal_line_editing() -> None:
    stops: list[bool] = []
    with create_pipe_input() as pipe:
        with TerminalInput(io.StringIO(), io.StringIO(), io.StringIO(), FakeClock(), input=pipe, output=DummyOutput()) as terminal:
            lines = terminal.lines(lambda: stops.append(True))
            reading = asyncio.ensure_future(anext(lines))
            pipe.send_text('statuX')
            pipe.send_bytes(b'\x7f')
            pipe.send_text('s')
            pipe.send_bytes(b'\x01')
            pipe.send_text('Mostra ')
            pipe.send_bytes(b'\x05')
            pipe.send_text(' perché')
            pipe.send_bytes(b'\x07')
            await until(lambda: bool(stops))
            pipe.send_text('\r')
            assert (await asyncio.wait_for(reading, timeout=1)).text == 'Mostra status perché'
            await lines.aclose()


async def test_piped_input_keeps_the_existing_line_protocol() -> None:
    stops: list[bool] = []
    source = io.StringIO('first\nsecond\n')
    output = io.StringIO()
    with TerminalInput(source, output, io.StringIO(), FakeClock()) as terminal:
        assert not terminal.interactive and terminal.stdout is output
        lines = [line.text async for line in terminal.lines(lambda: stops.append(True))]
    assert lines == ['first', 'second'] and not stops

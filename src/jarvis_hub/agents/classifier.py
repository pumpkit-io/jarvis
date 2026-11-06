"""A tool-free subscription classifier isolated from every conversational role."""

import asyncio
from collections.abc import Callable, Mapping
import json
import multiprocessing
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from jarvis_hub.voice.admission import ClassifierFailure


async def _deny_tool(_input: Any, _tool_use_id: str | None, _context: Any) -> Any:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "The admission classifier has no tool authority."}}


def classifier_options(cli: str, model: str, prompt: str, cwd: Path) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cli_path=cli, cwd=cwd, model=model, effort="low", thinking={"type": "disabled"},
        system_prompt=prompt, tools=[], allowed_tools=[], skills=[], mcp_servers={}, strict_mcp_config=True,
        setting_sources=[], permission_mode="dontAsk", max_turns=1,
        hooks={"PreToolUse": [HookMatcher(matcher=".*", hooks=[_deny_tool], on_failure="block")]},
        include_partial_messages=False, max_buffer_size=32768, enable_file_checkpointing=False,
        extra_args={"no-session-persistence": None, "safe-mode": None, "disable-slash-commands": None,
                    "no-chrome": None, "prompt-suggestions": "false", "permission-prompts": "none"},
        env={"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128", "CLAUDE_CODE_MAX_RETRIES": "0",
             "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0"}, stderr=lambda _line: None,
    )


def subscription_environment(environ: Mapping[str, str]) -> dict[str, str]:
    if any(key.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_")) or key in (
        "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY_HELPER", "CLAUDE_CODE_BASE_URL"
    ) for key in environ):
        raise ClassifierFailure("subscription_required")
    keep = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
    return {**{key: environ[key] for key in keep if key in environ},
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"}


def _check_subscription(cli: str, cwd: str) -> None:
    completed = subprocess.run([cli, "--safe-mode", "--setting-sources=", "auth", "status", "--json"],
                               cwd=cwd, capture_output=True, timeout=15)
    if completed.returncode != 0 or len(completed.stdout) > 32768:
        raise ClassifierFailure("subscription_required")
    data = json.loads(completed.stdout)
    if not (data.get("loggedIn") is True and data.get("authMethod") == "claude.ai"
            and data.get("apiProvider") == "firstParty" and not data.get("apiKeySource")
            and data.get("subscriptionType") in ("pro", "max", "team", "enterprise")):
        raise ClassifierFailure("subscription_required")


async def _classify(cli: str, model: str, prompt: str, payload: str, failed: Callable[[str], None]) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, RateLimitEvent, ResultError, ResultMessage, ServerToolUseBlock, SystemMessage, TextBlock, ToolUseBlock
    with tempfile.TemporaryDirectory(prefix="jarvis-admission-") as directory:
        client = ClaudeSDKClient(classifier_options(cli, model, prompt, Path(directory)))
        failure: ClassifierFailure | None = None
        raw, initialized, completed = "", False, False
        try:
            await client.connect()
            await client.query(payload)
            async for message in client.receive_response():
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    if message.data.get("tools") or message.data.get("mcp_servers") or message.data.get("apiKeySource") != "none":
                        raise ClassifierFailure()
                    initialized = True
                if isinstance(message, RateLimitEvent) and message.rate_limit_info.status == "rejected":
                    raise ClassifierFailure("rate_limited")
                if isinstance(message, AssistantMessage):
                    if message.error:
                        raise ClassifierFailure("rate_limited" if message.error == "rate_limit" else "unavailable")
                    for block in message.content:
                        if isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                            raise ClassifierFailure()
                        if isinstance(block, TextBlock):
                            raw += block.text
                            if len(raw.encode("utf-8")) > 2048:
                                raise ClassifierFailure()
                if isinstance(message, ResultMessage):
                    if message.is_error or message.subtype != "success" or message.num_turns != 1:
                        raise ClassifierFailure("rate_limited" if message.api_error_status == 429 else "unavailable")
                    completed = True
                    break
            if not initialized or not completed:
                raise ClassifierFailure()
        except ClassifierFailure as error:
            failure = error
            failed(error.reason)
        except ResultError as error:
            failure = ClassifierFailure("rate_limited" if error.api_error_status == 429 else "unavailable")
            failed(failure.reason)
        except Exception:
            failure = ClassifierFailure()
            failed(failure.reason)
        finally:
            try:
                await client.disconnect()
            except Exception:
                if failure is None:
                    failure = ClassifierFailure()
                    failed(failure.reason)
        if failure is not None:
            raise failure
        return raw


def _serve(connection: Connection, cli: str, model: str, prompt: str, environment: dict[str, str]) -> None:
    try:
        os.setsid()
        os.environ.clear()
        os.environ.update(environment)
        with tempfile.TemporaryDirectory(prefix="jarvis-admission-auth-") as directory:
            _check_subscription(cli, directory)
        connection.send(("ready", ""))
        while True:
            payload = connection.recv()
            reported = False
            def failed(reason: str) -> None:
                nonlocal reported
                if not reported:
                    connection.send(("error", reason))
                    reported = True
            try:
                raw = asyncio.run(_classify(cli, model, prompt, payload, failed))
                connection.send(("result", raw))
            except ClassifierFailure as error:
                failed(error.reason)
            except Exception:
                failed("unavailable")
    except ClassifierFailure as error:
        connection.send(("error", error.reason))
    except (EOFError, BrokenPipeError):
        pass
    except Exception:
        try:
            connection.send(("error", "unavailable"))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


class ClaudeClassifier:
    provider = "claude"

    def __init__(self, cli: str, model: str, prompt: str) -> None:
        self.model = model
        self._cli, self._prompt = cli, prompt
        self._process: BaseProcess | None = None
        self._connection: Connection | None = None
        self._group: int | None = None
        self._stop_lock = asyncio.Lock()

    async def _receive(self) -> tuple[str, str]:
        while self._connection is not None and not self._connection.poll():
            if self._process is None or not self._process.is_alive():
                raise ClassifierFailure()
            await asyncio.sleep(0.005)
        if self._connection is None:
            raise ClassifierFailure()
        try:
            kind, value = self._connection.recv()
        except (EOFError, OSError):
            raise ClassifierFailure() from None
        if kind == "error":
            raise ClassifierFailure(value)
        return kind, value

    async def prepare(self) -> None:
        if self._process is not None:
            return
        environment = subscription_environment(os.environ)
        context = multiprocessing.get_context("spawn")
        connection, child = context.Pipe()
        process = context.Process(target=_serve, args=(child, self._cli, self.model, self._prompt, environment), daemon=True)
        process.start()
        child.close()
        self._process, self._connection = process, connection
        try:
            async with asyncio.timeout(20):
                kind, _ = await self._receive()
            if kind != "ready" or process.pid is None or os.getpgid(process.pid) != process.pid:
                raise ClassifierFailure()
            self._group = process.pid
        except BaseException:
            await self.close()
            raise

    async def classify(self, payload: str) -> str:
        if self._process is None:
            await self.prepare()
        assert self._connection is not None
        try:
            self._connection.send(payload)
            kind, value = await self._receive()
            if kind != "result":
                raise ClassifierFailure()
            return value
        except asyncio.CancelledError:
            await self.close()
            raise

    async def close(self) -> None:
        async with self._stop_lock:
            process, connection = self._process, self._connection
            self._connection = None
            if connection is not None:
                connection.close()
            if process is None:
                return
            group = self._group
            if group is None and process.pid is not None:
                try:
                    group = process.pid if os.getpgid(process.pid) == process.pid else None
                except ProcessLookupError:
                    pass
            # Replacement waits for group disappearance, not merely successful signal delivery.
            if group is not None:
                try:
                    os.killpg(group, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            elif process.is_alive():
                process.terminate()
            await asyncio.to_thread(process.join, 0.2)
            if group is not None:
                try:
                    os.killpg(group, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            elif process.is_alive():
                process.kill()
            await asyncio.to_thread(process.join, 0.2)
            if process.is_alive():
                raise ClassifierFailure()
            if group is not None:
                deadline = time.monotonic() + 1.0
                while True:
                    try:
                        os.killpg(group, 0)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        pass
                    if time.monotonic() >= deadline:
                        raise ClassifierFailure()
                    await asyncio.sleep(0.01)
            process.close()
            self._process, self._group = None, None

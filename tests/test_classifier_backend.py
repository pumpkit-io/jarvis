"""The classifier's tool-free provider route is independent of conversational role sessions."""

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from jarvis_hub.agents.classifier import ClaudeClassifier, classifier_options
from jarvis_hub.voice.admission import ClassifierFailure, CloudAssessor
from jarvis_hub.voice.assessment import Candidate
from tests.fakes import until


def test_classifier_options_have_no_tools_settings_or_session_persistence(tmp_path: Path) -> None:
    options = classifier_options("/fake/claude", "sonnet", "Observe data only.", tmp_path)
    assert options.cli_path == "/fake/claude"
    assert options.tools == [] and options.allowed_tools == [] and options.skills == []
    assert options.mcp_servers == {} and options.strict_mcp_config
    assert options.setting_sources == [] and options.permission_mode == "dontAsk"
    assert options.cwd == tmp_path and options.max_turns == 1
    assert options.extra_args is not None and "no-session-persistence" in options.extra_args
    assert options.env is not None and options.env["CLAUDE_CODE_MAX_RETRIES"] == "0"
    assert options.hooks is not None and options.hooks["PreToolUse"][0].on_failure == "block"


def fake_cli(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    marker = tmp_path / "child.json"
    cli = tmp_path / "claude"
    cli.write_text(f'''#!{sys.executable}
import json,os,signal,sys,time
if "auth" in sys.argv:
    print(json.dumps(dict(loggedIn=True,authMethod="claude.ai",apiProvider="firstParty",subscriptionType="max")))
    sys.exit(0)
if "--version" in sys.argv or "-v" in sys.argv:
    print("2.1.318 (Claude Code)")
    sys.exit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
with open({str(marker)!r},"w") as f:
    json.dump(dict(pid=os.getpid(),parent=os.getppid(),group=os.getpgrp()),f)
for line in sys.stdin:
    item=json.loads(line)
    with open({str(marker)!r},"w") as f:
        json.dump(dict(pid=os.getpid(),parent=os.getppid(),group=os.getpgrp(),stage=item.get("type"),subtype=item.get("request",{{}}).get("subtype")),f)
    if item.get("type")=="control_request":
        print(json.dumps(dict(type="control_response",response=dict(subtype="success",request_id=item["request_id"],response=dict(hook_on_failure_supported=True,hooks_applied=True)))),flush=True)
    elif item.get("type")=="user":
        print(json.dumps(dict(type="system",subtype="init",tools={["Bash"] if mode == "tools" else []!r},mcp_servers=[],apiKeySource="none",model="fake")),flush=True)
        if {mode!r}=="stall":
            while True: time.sleep(1)
        print(json.dumps(dict(type="assistant",message=dict(model="fake",content=[dict(type="text",text='["floor"]')]))),flush=True)
        print(json.dumps(dict(type="result",subtype="success",is_error={mode == "rate"!r},api_error_status={429 if mode == "rate" else None!r},num_turns=1,duration_ms=1,duration_api_ms=1,session_id="fake",result='["floor"]')),flush=True)
        sys.exit(0)
''')
    cli.chmod(0o700)
    return cli, marker


async def test_real_sdk_transport_child_stays_in_owned_group_and_is_killed_after_deadline(tmp_path: Path) -> None:
    cli, marker = fake_cli(tmp_path, "stall")
    backend = ClaudeClassifier(str(cli), "fake", "Classify synthetic data only.")
    notices: list[str] = []
    assessor = CloudAssessor(backend, notice=notices.append)
    assessor.start()
    await until(lambda: assessor.ready, timeout=5)
    candidate = Candidate("c", "t", "Jarvis, continue.", "Explain", "", "thinking", True, ("Jarvis, continue.",))
    try:
        task = asyncio.create_task(assessor.assess(candidate, current=lambda: ("c", "t"), timeout_s=1))
        await until(marker.exists, timeout=3)
        child = json.loads(marker.read_text())
        assert child["group"] == child["parent"] and child["group"] != os.getpgrp()
        result = await task
        assert json.loads(marker.read_text())["stage"] == "user"
        assert result.reason == "timeout" and result.segment_actions is None
        assert result.elapsed_ms < 1300
        await assessor.close()
        def reaped() -> bool:
            try:
                os.kill(child["pid"], 0)
            except ProcessLookupError:
                return True
            return False
        await until(reaped, timeout=3)
    finally:
        await assessor.close()


async def test_subscription_provider_rejects_tools_in_actual_sdk_init(tmp_path: Path) -> None:
    cli, _ = fake_cli(tmp_path, "tools")
    backend = ClaudeClassifier(str(cli), "fake", "Classify synthetic data only.")
    try:
        await backend.prepare()
        with pytest.raises(ClassifierFailure):
            await asyncio.wait_for(backend.classify('{"observed_segments":["Jarvis, continue."]}'), timeout=1)
    finally:
        await backend.close()


async def test_billing_override_never_opens_a_classifier_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cli, marker = fake_cli(tmp_path, "success")
    backend = ClaudeClassifier(str(cli), "fake", "Classify synthetic data only.")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "PRIVATE_SECRET")
    with pytest.raises(ClassifierFailure, match="subscription_required"):
        await backend.prepare()
    assert not marker.exists()


async def test_prepared_subscription_adapter_performs_one_tool_free_fake_query(tmp_path: Path) -> None:
    cli, marker = fake_cli(tmp_path, "success")
    backend = ClaudeClassifier(str(cli), "fake", "Classify synthetic data only.")
    try:
        await backend.prepare()
        assert not marker.exists()
        raw = await asyncio.wait_for(backend.classify('{"observed_segments":["Jarvis, continue."]}'), timeout=2)
        assert raw == '["floor"]'
        assert json.loads(marker.read_text())["stage"] == "user"
    finally:
        await backend.close()


async def test_subscription_rate_limit_is_not_replaced_by_partial_floor_text(tmp_path: Path) -> None:
    cli, _ = fake_cli(tmp_path, "rate")
    backend = ClaudeClassifier(str(cli), "fake", "Classify synthetic data only.")
    notices: list[str] = []
    assessor = CloudAssessor(backend, notice=notices.append)
    assessor.start()
    await until(lambda: assessor.ready, timeout=5)
    candidate = Candidate("c", "t", "Jarvis, continue.", "Explain", "", "thinking", True, ("Jarvis, continue.",))
    try:
        result = await assessor.assess(candidate, current=lambda: ("c", "t"))
        assert result.reason == "rate_limited" and result.segment_actions is None
        assert not assessor.ready and any("usage-limited" in text for text in notices)
    finally:
        await assessor.close()

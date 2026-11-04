"""The public launcher resolves symlinks before handing flags to the Hub."""

import json
import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("flags", [[], ["--text"], ["--headphones", "--debug"], ["stats", "--bargein"]])
def test_launcher_starts_the_hub_from_its_repo(tmp_path: Path, linked: bool, flags: list[str]) -> None:
    root = Path(__file__).parents[1]
    binaries = tmp_path / "bin"
    binaries.mkdir()
    uv = binaries / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "print(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:]}))\n"
    )
    uv.chmod(0o755)
    claude = binaries / "claude"
    claude.write_text("#!/bin/sh\nexit 83\n")
    claude.chmod(0o755)
    launcher = root / "jarvis"
    if linked:
        intermediate = tmp_path / "relative-link"
        intermediate.symlink_to(os.path.relpath(launcher, tmp_path))
        launcher = binaries / "jarvis"
        launcher.symlink_to("../relative-link")
    result = subprocess.run(
        [str(launcher), *flags], cwd=tmp_path, text=True, capture_output=True,
        env={**os.environ, "PATH": f"{binaries}{os.pathsep}{os.environ['PATH']}"}, check=True,
    )
    assert json.loads(result.stdout) == {"cwd": str(root), "args": ["run", "jarvis-hub", *flags]}

"""dangerous-cmd-guard.sh 테스트 — 위험 명령과 --no-verify를 막고, 평범한 명령은 통과시킨다."""

import json
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).parent / "hooks" / "dangerous-cmd-guard.sh"


def _decision(command):
    r = subprocess.run([str(HOOK)], input=json.dumps({"tool_input": {"command": command}}),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    if not r.stdout.strip():
        return "allow"
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"]


@pytest.mark.parametrize("command", [
    "git commit --no-verify -m x",
    "git push --no-verify fork main",
    "git push --force fork main",
])
def test_dangerous_commands_are_denied(command):
    assert _decision(command) == "deny"


@pytest.mark.parametrize("command", [
    "git push fork main",
    "git commit -m x",
    "git commit -m x && git log -n 1",
    "git commit -m 'feat: add -n option'",
])
def test_normal_commands_pass(command):
    assert _decision(command) == "allow"

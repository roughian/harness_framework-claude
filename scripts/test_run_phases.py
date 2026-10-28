"""run_phases.py 테스트 — 지금 시작할 수 있는 독립 phase를 상한까지 worktree로 함께 띄운다."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import run_phases as rp  # noqa: E402


def _index(*phases):
    return {"phases": list(phases)}


def _phase(name, status="pending", depends_on=None):
    phase = {"dir": name, "status": status}
    if depends_on:
        phase["depends_on"] = depends_on
    return phase


class TestReadyPhases:
    def test_independent_phases_start_together(self):
        ready, _ = rp.ready_phases(_index(_phase("a"), _phase("b")), running=set(), limit=3)
        assert [p["dir"] for p in ready] == ["a", "b"]

    def test_phase_waits_for_unfinished_dependency(self):
        ready, waiting = rp.ready_phases(_index(_phase("a"), _phase("c", depends_on=["a"])), running=set(), limit=3)
        assert [p["dir"] for p in ready] == ["a"]
        assert "c: a 끝나기를 기다림" in waiting

    def test_phase_starts_after_dependency_completed(self):
        index = _index(_phase("a", "completed"), _phase("c", depends_on=["a"]))
        ready, _ = rp.ready_phases(index, running=set(), limit=3)
        assert [p["dir"] for p in ready] == ["c"]

    def test_finished_or_stuck_phases_are_skipped(self):
        index = _index(_phase("a", "completed"), _phase("b", "blocked"), _phase("e", "error"))
        assert rp.ready_phases(index, running=set(), limit=3)[0] == []

    def test_running_phase_is_not_started_again(self):
        ready, _ = rp.ready_phases(_index(_phase("a"), _phase("b")), running={"a"}, limit=3)
        assert [p["dir"] for p in ready] == ["b"]

    def test_running_phases_count_against_limit(self):
        ready, waiting = rp.ready_phases(_index(_phase("a"), _phase("b"), _phase("c")), running={"x"}, limit=2)
        assert [p["dir"] for p in ready] == ["a"]
        assert "c: 동시 실행 상한 2개 도달" in waiting


class TestRunningPhases:
    def test_reads_feat_branches_from_worktrees(self):
        out = ("worktree /repo\nHEAD 1\nbranch refs/heads/main\n\n"
               "worktree /wt-a\nHEAD 2\nbranch refs/heads/feat-a\n\n"
               "worktree /wt-x\nHEAD 3\nbranch refs/heads/other\n")
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with patch.object(rp.subprocess, "run", return_value=done):
            assert rp.running_phases(Path("/repo")) == {"a"}


class TestLaunch:
    def test_inside_herdr_runs_in_work_pane(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERDR_PANE_ID", "p1")
        run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with patch.object(rp.subprocess, "run", run), \
             patch.object(rp.herdr_layout, "split_pane", return_value="p9") as split:
            rp.launch(_phase("a"), tmp_path / "repo")
        wt = tmp_path / "wt-a"
        assert run.call_args_list[0].args[0] == ["git", "worktree", "add", str(wt), "-b", "feat-a"]
        split.assert_called_once_with(wt, label="phase:a")
        assert run.call_args_list[1].args[0] == ["herdr", "pane", "run", "p9", "python3 scripts/execute.py a"]

    def test_outside_herdr_runs_in_background_with_log(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with patch.object(rp.subprocess, "run", run), patch.object(rp.subprocess, "Popen") as popen:
            rp.launch(_phase("a"), tmp_path / "repo")
        wt = tmp_path / "wt-a"
        assert popen.call_args.args[0] == ["python3", "scripts/execute.py", "a"]
        assert popen.call_args.kwargs["cwd"] == wt
        assert Path(popen.call_args.kwargs["stdout"].name) == wt / "phases" / "a" / "run.log"

    def test_worktree_failure_stops_launch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERDR_PANE_ID", raising=False)
        failed = subprocess.CompletedProcess([], 128, "", "already exists")
        with patch.object(rp.subprocess, "run", return_value=failed), \
             patch.object(rp.subprocess, "Popen") as popen:
            with pytest.raises(RuntimeError, match="already exists"):
                rp.launch(_phase("a"), tmp_path / "repo")
        popen.assert_not_called()

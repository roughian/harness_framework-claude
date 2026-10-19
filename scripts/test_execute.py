"""
execute.py 리팩터링 안전망 테스트.
리팩터링 전후 동작이 동일한지 검증한다.
"""

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import execute as ex


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_review_cache(tmp_path, monkeypatch):
    """리뷰 통과 기록은 임시 폴더에 쓴다."""
    monkeypatch.setattr(ex.review, "PASSED_FILE", tmp_path / "passed")
    monkeypatch.setattr(ex.review, "RESULTS_DIR", tmp_path / "results")


@pytest.fixture
def tmp_project(tmp_path):
    """phases/, CLAUDE.md, docs/ 를 갖춘 임시 프로젝트 구조."""
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Rules\n- rule one\n- rule two")

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "arch.md").write_text("# Architecture\nSome content")
    (docs_dir / "guide.md").write_text("# Guide\nAnother doc")

    return tmp_path


@pytest.fixture
def phase_dir(tmp_project):
    """step 3개를 가진 phase 디렉토리."""
    d = tmp_project / "phases" / "0-mvp"
    d.mkdir()

    index = {
        "project": "TestProject",
        "phase": "mvp",
        "steps": [
            {"step": 0, "name": "setup", "status": "completed", "summary": "프로젝트 초기화 완료"},
            {"step": 1, "name": "core", "status": "completed", "summary": "핵심 로직 구현"},
            {"step": 2, "name": "ui", "status": "pending"},
        ],
    }
    (d / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    (d / "step2.md").write_text("# Step 2: UI\n\nUI를 구현하세요.")

    return d


@pytest.fixture
def top_index(tmp_project):
    """phases/index.json (top-level)."""
    top = {
        "phases": [
            {"dir": "0-mvp", "status": "pending"},
            {"dir": "1-polish", "status": "pending"},
        ]
    }
    p = tmp_project / "phases" / "index.json"
    p.write_text(json.dumps(top, indent=2))
    return p


@pytest.fixture
def executor(tmp_project, phase_dir):
    """테스트용 StepExecutor 인스턴스. git 호출은 별도 mock 필요."""
    with patch.object(ex, "ROOT", tmp_project):
        inst = ex.StepExecutor("0-mvp")
    # 내부 경로를 tmp_project 기준으로 재설정
    inst._root = str(tmp_project)
    inst._phases_dir = tmp_project / "phases"
    inst._phase_dir = phase_dir
    inst._phase_dir_name = "0-mvp"
    inst._index_file = phase_dir / "index.json"
    inst._top_index_file = tmp_project / "phases" / "index.json"
    return inst


# ---------------------------------------------------------------------------
# _stamp (= 이전 now_iso)
# ---------------------------------------------------------------------------

class TestStamp:
    def test_returns_kst_timestamp(self, executor):
        result = executor._stamp()
        assert "+0900" in result

    def test_format_is_iso(self, executor):
        result = executor._stamp()
        dt = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert dt.tzinfo is not None

    def test_is_current_time(self, executor):
        before = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0)
        result = executor._stamp()
        after = datetime.now(ex.StepExecutor.TZ).replace(microsecond=0) + timedelta(seconds=1)
        parsed = datetime.strptime(result, "%Y-%m-%dT%H:%M:%S%z")
        assert before <= parsed <= after


# ---------------------------------------------------------------------------
# _read_json / _write_json
# ---------------------------------------------------------------------------

class TestJsonHelpers:
    def test_roundtrip(self, tmp_path):
        data = {"key": "값", "nested": [1, 2, 3]}
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, data)
        loaded = ex.StepExecutor._read_json(p)
        assert loaded == data

    def test_save_ensures_ascii_false(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"한글": "테스트"})
        raw = p.read_text()
        assert "한글" in raw
        assert "\\u" not in raw

    def test_save_indented(self, tmp_path):
        p = tmp_path / "test.json"
        ex.StepExecutor._write_json(p, {"a": 1})
        raw = p.read_text()
        assert "\n" in raw

    def test_load_nonexistent_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ex.StepExecutor._read_json(tmp_path / "nope.json")


# ---------------------------------------------------------------------------
# 문서 주입 (CLAUDE.md @import로 대체됨)
# ---------------------------------------------------------------------------

class TestPreambleDocs:
    def test_docs_and_claude_md_not_injected(self, executor):
        result = executor._build_preamble("")
        assert "rule one" not in result
        assert "# Architecture" not in result

    def test_includes_lessons_rule(self, executor):
        assert "docs/LESSONS.md" in executor._build_preamble("")


# ---------------------------------------------------------------------------
# _build_step_context
# ---------------------------------------------------------------------------

class TestBuildStepContext:
    def test_includes_completed_with_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "Step 0 (setup): 프로젝트 초기화 완료" in result
        assert "Step 1 (core): 핵심 로직 구현" in result

    def test_excludes_pending(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert "ui" not in result

    def test_excludes_completed_without_summary(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        del index["steps"][0]["summary"]
        result = ex.StepExecutor._build_step_context(index)
        assert "setup" not in result
        assert "core" in result

    def test_empty_when_no_completed(self):
        index = {"steps": [{"step": 0, "name": "a", "status": "pending"}]}
        result = ex.StepExecutor._build_step_context(index)
        assert result == ""

    def test_has_header(self, phase_dir):
        index = json.loads((phase_dir / "index.json").read_text())
        result = ex.StepExecutor._build_step_context(index)
        assert result.startswith("## 이전 Step 산출물")


# ---------------------------------------------------------------------------
# _build_preamble
# ---------------------------------------------------------------------------

class TestBuildPreamble:
    def test_includes_project_name(self, executor):
        result = executor._build_preamble("")
        assert "TestProject" in result

    def test_includes_step_context(self, executor):
        ctx = "## 이전 Step 산출물\n\n- Step 0: done"
        result = executor._build_preamble(ctx)
        assert "이전 Step 산출물" in result

    def test_includes_commit_example(self, executor):
        result = executor._build_preamble("")
        assert "feat(mvp):" in result

    def test_includes_rules(self, executor):
        result = executor._build_preamble("")
        assert "작업 규칙" in result
        assert "AC" in result

    def test_no_retry_section_by_default(self, executor):
        result = executor._build_preamble("")
        assert "이전 시도 실패" not in result

    def test_retry_section_with_prev_error(self, executor):
        result = executor._build_preamble("", prev_error="타입 에러 발생")
        assert "이전 시도 실패" in result
        assert "타입 에러 발생" in result

    def test_includes_max_retries(self, executor):
        result = executor._build_preamble("")
        assert str(ex.StepExecutor.MAX_RETRIES) in result

    def test_includes_index_path(self, executor):
        result = executor._build_preamble("")
        assert "/phases/0-mvp/index.json" in result


# ---------------------------------------------------------------------------
# _update_top_index
# ---------------------------------------------------------------------------

class TestUpdateTopIndex:
    def test_completed(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "completed"
        assert "completed_at" in mvp

    def test_error(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("error")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "error"
        assert "failed_at" in mvp

    def test_blocked(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("blocked")
        data = json.loads(top_index.read_text())
        mvp = next(p for p in data["phases"] if p["dir"] == "0-mvp")
        assert mvp["status"] == "blocked"
        assert "blocked_at" in mvp

    def test_other_phases_unchanged(self, executor, top_index):
        executor._top_index_file = top_index
        executor._update_top_index("completed")
        data = json.loads(top_index.read_text())
        polish = next(p for p in data["phases"] if p["dir"] == "1-polish")
        assert polish["status"] == "pending"

    def test_nonexistent_dir_is_noop(self, executor, top_index):
        executor._top_index_file = top_index
        executor._phase_dir_name = "no-such-dir"
        original = json.loads(top_index.read_text())
        executor._update_top_index("completed")
        after = json.loads(top_index.read_text())
        for p_before, p_after in zip(original["phases"], after["phases"]):
            assert p_before["status"] == p_after["status"]

    def test_no_top_index_file(self, executor, tmp_path):
        executor._top_index_file = tmp_path / "nonexistent.json"
        executor._update_top_index("completed")  # should not raise


# ---------------------------------------------------------------------------
# _checkout_branch (mocked)
# ---------------------------------------------------------------------------

class TestRunGitSkipsReviewHooks:
    def test_git_calls_skip_git_review_hooks(self, executor):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            executor._run_git("commit", "-m", "x")
        assert run.call_args.kwargs["env"]["HARNESS_SKIP_GIT_REVIEW"] == "1"


class TestCheckoutBranch:
    def _mock_git(self, executor, responses):
        call_idx = {"i": 0}
        def fake_git(*args):
            idx = call_idx["i"]
            call_idx["i"] += 1
            if idx < len(responses):
                return responses[idx]
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

    def test_already_on_branch(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="feat-mvp\n", stderr=""),
        ])
        executor._checkout_branch()  # should return without checkout

    def test_branch_exists_checkout(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_branch_not_exists_create(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="not found"),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()

    def test_records_base_branch(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="develop\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()
        assert executor._base_branch == "develop"

    def test_checkout_fails_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=0, stdout="main\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="dirty tree"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1

    def test_no_git_exits(self, executor):
        self._mock_git(executor, [
            MagicMock(returncode=1, stdout="", stderr="not a git repo"),
        ])
        with pytest.raises(SystemExit) as exc_info:
            executor._checkout_branch()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _commit_step (mocked)
# ---------------------------------------------------------------------------

class TestCommitStep:
    def test_two_phase_commit(self, executor):
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_calls = [c for c in calls if c[0] == "commit"]
        assert len(commit_calls) == 2
        assert "feat(mvp):" in commit_calls[0][2]
        assert "chore(mvp):" in commit_calls[1][2]

    def test_no_code_changes_skips_feat_commit(self, executor):
        call_count = {"diff": 0}
        calls = []
        def fake_git(*args):
            calls.append(args)
            if args[:2] == ("diff", "--cached"):
                call_count["diff"] += 1
                if call_count["diff"] == 1:
                    return MagicMock(returncode=0)
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="", stderr="")
        executor._run_git = fake_git

        executor._commit_step(2, "ui")

        commit_msgs = [c[2] for c in calls if c[0] == "commit"]
        assert len(commit_msgs) == 1
        assert "chore" in commit_msgs[0]


# ---------------------------------------------------------------------------
# _invoke_claude (mocked)
# ---------------------------------------------------------------------------

class TestInvokeClaude:
    def test_invokes_claude_with_correct_args(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"result": "ok"}', stderr="")
        step = {"step": 2, "name": "ui"}
        preamble = "PREAMBLE\n"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            output = executor._invoke_claude(step, preamble)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--output-format" in cmd
        assert "PREAMBLE" in cmd[-1]
        assert "UI를 구현하세요" in cmd[-1]

    def test_appends_lessons_matching_step_file(self, executor):
        docs = Path(executor._root) / "docs"
        (docs / "LESSONS.md").write_text("- [2026-10-10] UI 규칙 [키: UI를 구현]\n- [2026-10-11] 무관 [키: db/]\n")
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="{}", stderr="")) as mock_run:
            executor._invoke_claude({"step": 2, "name": "ui"}, "preamble")
        prompt = mock_run.call_args[0][0][-1]
        assert "## 지켜야 할 LESSONS" in prompt and "UI 규칙" in prompt and "무관" not in prompt

    def test_no_lessons_section_without_match(self, executor):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="{}", stderr="")) as mock_run:
            executor._invoke_claude({"step": 2, "name": "ui"}, "preamble")
        assert "지켜야 할 LESSONS" not in mock_run.call_args[0][0][-1]

    def test_saves_output_json(self, executor):
        mock_result = MagicMock(returncode=0, stdout='{"ok": true}', stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result):
            executor._invoke_claude(step, "preamble")

        output_file = executor._phase_dir / "step2-output.json"
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["step"] == 2
        assert data["name"] == "ui"
        assert data["exitCode"] == 0

    def test_nonexistent_step_file_exits(self, executor):
        step = {"step": 99, "name": "nonexistent"}
        with pytest.raises(SystemExit) as exc_info:
            executor._invoke_claude(step, "preamble")
        assert exc_info.value.code == 1

    def test_timeout_from_config(self, executor):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        step = {"step": 2, "name": "ui"}

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude(step, "preamble")

        assert mock_run.call_args[1]["timeout"] == executor._config["timeouts_sec"]["step"]

    def test_uses_worker_model_and_effort(self, executor):
        mock_result = MagicMock(returncode=0, stdout="{}", stderr="")
        worker = {"cli": "claude", "model": "picked-model", "effort": "max"}
        executor._config["roles"]["worker"] = worker

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            executor._invoke_claude({"step": 2, "name": "ui"}, "preamble")

        cmd = mock_run.call_args[0][0]
        assert cmd[cmd.index("--model") + 1] == worker["model"]
        assert cmd[cmd.index("--effort") + 1] == worker["effort"]

    def test_empty_worker_model_uses_cli_default(self, executor):
        executor._config["roles"]["worker"] = {"cli": "claude", "model": "", "effort": "max"}
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="{}", stderr="")) as mock_run:
            executor._invoke_claude({"step": 2, "name": "ui"}, "preamble")
        assert "--model" not in mock_run.call_args[0][0]

    def test_timeout_expired_is_recorded_not_raised(self, executor):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1)):
            output = executor._invoke_claude({"step": 2, "name": "ui"}, "preamble")
        assert output["exitCode"] == -1
        assert "timeout" in output["stderr"]


# ---------------------------------------------------------------------------
# progress_indicator (= 이전 Spinner)
# ---------------------------------------------------------------------------

class TestProgressIndicator:
    def test_context_manager(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.15)
        assert pi.elapsed >= 0.1

    def test_elapsed_increases(self):
        import time
        with ex.progress_indicator("test") as pi:
            time.sleep(0.2)
        assert pi.elapsed > 0


# ---------------------------------------------------------------------------
# main() CLI 파싱 (mocked)
# ---------------------------------------------------------------------------

class TestMainCli:
    def test_no_args_exits(self):
        with patch("sys.argv", ["execute.py"]):
            with pytest.raises(SystemExit) as exc_info:
                ex.main()
            assert exc_info.value.code == 2  # argparse exits with 2

    def test_invalid_phase_dir_exits(self):
        with patch("sys.argv", ["execute.py", "nonexistent"]):
            with patch.object(ex, "ROOT", Path("/tmp/fake_nonexistent")):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1

    def test_no_review_flag(self):
        with patch("sys.argv", ["execute.py", "0-mvp", "--no-review"]):
            with patch.object(ex, "StepExecutor") as mock_cls:
                ex.main()
        assert mock_cls.call_args[1]["review_enabled"] is False

    def test_missing_index_exits(self, tmp_project):
        (tmp_project / "phases" / "empty").mkdir()
        with patch("sys.argv", ["execute.py", "empty"]):
            with patch.object(ex, "ROOT", tmp_project):
                with pytest.raises(SystemExit) as exc_info:
                    ex.main()
                assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _check_blockers (= 이전 main() error/blocked 체크)
# ---------------------------------------------------------------------------

class TestCheckBlockers:
    def _make_executor_with_steps(self, tmp_project, steps):
        d = tmp_project / "phases" / "test-phase"
        d.mkdir(exist_ok=True)
        index = {"project": "T", "phase": "test", "steps": steps}
        (d / "index.json").write_text(json.dumps(index))

        with patch.object(ex, "ROOT", tmp_project):
            inst = ex.StepExecutor.__new__(ex.StepExecutor)
        inst._root = str(tmp_project)
        inst._phases_dir = tmp_project / "phases"
        inst._phase_dir = d
        inst._phase_dir_name = "test-phase"
        inst._index_file = d / "index.json"
        inst._top_index_file = tmp_project / "phases" / "index.json"
        inst._phase_name = "test"
        inst._total = len(steps)
        return inst

    def test_error_step_exits_1(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "bad", "status": "error", "error_message": "fail"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 1

    def test_blocked_step_exits_2(self, tmp_project):
        steps = [
            {"step": 0, "name": "ok", "status": "completed"},
            {"step": 1, "name": "stuck", "status": "blocked", "blocked_reason": "API key"},
        ]
        inst = self._make_executor_with_steps(tmp_project, steps)
        with pytest.raises(SystemExit) as exc_info:
            inst._check_blockers()
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# 리뷰 게이트 / PROGRESS 기록 / push 정책
# ---------------------------------------------------------------------------

def _merged(blocking=0, level="low", score=10):
    findings = [
        {"title": "SQL injection", "body": "b", "priority": 0, "confidence": 0.9, "file": "src/a.ts",
         "line_start": 3, "line_end": 4, "pre_existing": False, "found_by": ["claude"]}
    ] * blocking
    return {
        "base": "main", "reviewers": ["claude", "codex"], "failed": {},
        "verdict": "patch is incorrect" if blocking else "patch is correct",
        "risk": {"score": score, "level": level, "axes": {}},
        "blocking": blocking, "findings": findings, "summaries": {}, "head": "h" * 40,
    }


@pytest.fixture
def git_log(executor):
    """_run_git 호출을 기록하고, 스테이징된 변경이 있는 것처럼 응답한다."""
    calls = []

    def fake_git(*args):
        calls.append(args)
        if args[:2] == ("diff", "--cached"):
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="abc1234\n", stderr="")

    executor._run_git = fake_git
    return calls


class TestReviewPhase:
    def test_clean_review_records_and_skips_fix(self, executor, git_log):
        executor._run_claude = MagicMock()
        with patch.object(ex.review, "run_review", return_value=_merged()):
            result = executor._review_phase()
        assert result["blocking"] == 0
        executor._run_claude.assert_not_called()
        index = json.loads(executor._index_file.read_text())
        assert index["review"] == {"risk_score": 10, "risk_level": "low", "blocking": 0, "rounds": 0}

    def test_blocking_fixed_then_rereviewed(self, executor, git_log):
        executor._run_claude = MagicMock()
        with patch.object(ex.review, "run_review", side_effect=[_merged(blocking=1), _merged()]):
            executor._review_phase()
        prompt = executor._run_claude.call_args[0][0]
        assert "SQL injection" in prompt
        commits = [c for c in git_log if c[0] == "commit"]
        assert any("fix(mvp)" in c[2] for c in commits)
        assert json.loads(executor._index_file.read_text())["review"]["rounds"] == 1

    def test_still_blocking_blocks_phase(self, executor, git_log, tmp_project):
        executor._run_claude = MagicMock()
        with patch.object(ex.review, "run_review", return_value=_merged(blocking=2)):
            with pytest.raises(SystemExit) as exc_info:
                executor._review_phase()
        assert exc_info.value.code == 2
        index = json.loads(executor._index_file.read_text())
        assert "P0/P1" in index["blocked_reason"]
        assert "blocked" in (tmp_project / "docs" / "PROGRESS.md").read_text()

    def test_all_reviewers_failed_blocks_phase(self, executor, git_log):
        with patch.object(ex.review, "run_review", side_effect=ex.review.ReviewError("x")):
            with pytest.raises(SystemExit) as exc_info:
                executor._review_phase()
        assert exc_info.value.code == 2


class TestProgress:
    def test_completed_entry_lists_steps_and_commit_range(self, executor, git_log, tmp_project):
        executor._record_progress("completed")
        text = (tmp_project / "docs" / "PROGRESS.md").read_text()
        assert "mvp — completed" in text
        assert "- step 0 setup: 프로젝트 초기화 완료" in text
        assert "main..abc1234" in text

    def test_includes_review_risk(self, executor, git_log, tmp_project):
        index = json.loads(executor._index_file.read_text())
        index["review"] = {"risk_score": 12, "risk_level": "low", "blocking": 0, "rounds": 1}
        executor._index_file.write_text(json.dumps(index))
        executor._record_progress("completed")
        assert "risk low 12" in (tmp_project / "docs" / "PROGRESS.md").read_text()

    def test_appends_not_overwrites(self, executor, git_log, tmp_project):
        executor._record_progress("blocked", "API key")
        executor._record_progress("completed")
        text = (tmp_project / "docs" / "PROGRESS.md").read_text()
        assert text.count("\n## ") == 2
        assert "blocked: API key" in text

    def test_commits_only_progress_file(self, executor, git_log):
        executor._record_progress("completed")
        commit = next(c for c in git_log if c[0] == "commit")
        assert commit[-2:] == ("--", "docs/PROGRESS.md")


class TestFinalizePush:
    def test_high_risk_skips_push(self, executor, git_log):
        executor._auto_push = True
        executor._finalize(_merged(level="high", score=40))
        assert not any(c[0] == "push" for c in git_log)

    def test_low_risk_pushes(self, executor, git_log):
        executor._auto_push = True
        executor._finalize(_merged())
        assert any(c[0] == "push" for c in git_log)

    def test_no_review_pushes(self, executor, git_log):
        executor._auto_push = True
        executor._finalize(None)
        assert any(c[0] == "push" for c in git_log)


class TestReviewBaseAndWorkDir:
    def test_first_run_persists_base_branch(self, executor):
        TestCheckoutBranch()._mock_git(executor, [
            MagicMock(returncode=0, stdout="develop\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="", stderr=""),
        ])
        executor._checkout_branch()
        assert json.loads(executor._index_file.read_text())["base_branch"] == "develop"

    def test_rerun_on_feature_branch_restores_base(self, executor):
        index = json.loads(executor._index_file.read_text())
        index["base_branch"] = "develop"
        executor._index_file.write_text(json.dumps(index))
        TestCheckoutBranch()._mock_git(executor, [MagicMock(returncode=0, stdout="feat-mvp\n", stderr="")])
        executor._checkout_branch()
        assert executor._base_branch == "develop"



class TestVerifyReview:
    def test_rereview_after_fix_is_verify_mode(self, executor, git_log):
        executor._run_claude = MagicMock()
        first = _merged(blocking=1)
        with patch.object(ex.review, "run_review", side_effect=[first, _merged()]) as mock_review:
            executor._review_phase()
        assert mock_review.call_args_list[0].kwargs["previous"] is None
        assert mock_review.call_args_list[1].kwargs["previous"] is first

    def test_rerun_after_blocked_starts_with_verify(self, executor, git_log):
        # blocked 뒤 재실행은 전체를 다시 보지 않고 지난 차단 지적만 검증한다.
        previous = _merged(blocking=1)
        (executor._phase_dir / "review.json").write_text(json.dumps(previous))
        with patch.object(ex.review, "run_review", return_value=_merged()) as mock_review:
            executor._review_phase()
        assert mock_review.call_args.kwargs["previous"] == previous

    def test_stops_after_max_rounds(self, executor, git_log):
        executor._run_claude = MagicMock()
        with patch.object(ex.review, "run_review", return_value=_merged(blocking=1)) as mock_review:
            with pytest.raises(SystemExit):
                executor._review_phase()
        assert mock_review.call_count == 1 + executor.MAX_REVIEW_FIX_ROUNDS

    def test_finalize_records_head_as_passed(self, executor, git_log):
        executor._finalize(_merged())
        assert ex.review.is_passed("abc1234", "abc1234")
        # PR 단계가 다시 리뷰하지 않도록 최종 커밋에 결과를 남긴다.
        assert ex.review.load_result("abc1234")["blocking"] == 0


class TestHeaderModelSelection:
    def _config(self, executor, selected_at, worker_model=""):
        executor._config = {
            "selection": {"selected_at": selected_at},
            "roles": {
                "worker": {"cli": "claude", "model": worker_model, "effort": "max"},
                "reviewers": [{"name": "codex", "cli": "codex", "model": "", "effort": "max"}],
            },
        }

    def test_prints_models_and_cli_default(self, executor, capsys):
        self._config(executor, "2026-10-10", worker_model="claude-sonnet-5-5")
        with patch.object(ex, "_today", return_value=ex.date(2026, 10, 11)):
            executor._print_header()
        out = capsys.readouterr().out
        assert "worker: claude-sonnet-5-5" in out
        assert "codex: CLI 기본" in out
        assert "2026-10-10" in out
        assert "WARN" not in out

    def test_warns_when_selection_stale(self, executor, capsys):
        self._config(executor, "2026-10-01")
        with patch.object(ex, "_today", return_value=ex.date(2026, 10, 10)):
            executor._print_header()
        assert "WARN" in capsys.readouterr().out

    def test_warns_when_never_selected(self, executor, capsys):
        self._config(executor, "")
        executor._print_header()
        assert "WARN" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 사용량 한도 대기
# ---------------------------------------------------------------------------

def _out(code=0, stdout="", stderr=""):
    return {"exitCode": code, "stdout": stdout, "stderr": stderr}


LIMITED = _out(1, stderr="Claude AI usage limit reached|1760000000")


class TestUsageLimit:
    @pytest.mark.parametrize("output,expected", [
        (LIMITED, True),
        (_out(0, json.dumps({"is_error": True, "result": "5-hour limit reached ∙ resets 3pm"})), True),
        # claude CLI 바이너리 문자열로 조립되는 구독 한도 문구(2026-10 확인).
        (_out(1, json.dumps({"is_error": True, "result": "You've hit your session limit · resets 3pm"})), True),
        (_out(1, json.dumps({"is_error": True, "result": "You've reached your Fable limit"})), True),
        (_out(1, stderr="You've hit your monthly spend limit"), True),
        (_out(1, stderr="You've hit your limit · resets 3pm"), True),
        (_out(1, stderr="You've hit your org's monthly spend limit"), True),
        (_out(1, stderr="You're out of usage credits · resets 3pm"), True),
        (_out(1, stderr="You're out of extra usage"), True),
        # 성공한 응답 본문에 '한도' 단어가 있어도 한도가 아니다(예: rate limit 미들웨어를 구현함).
        (_out(0, json.dumps({"is_error": False, "result": "implemented rate limit middleware"})), False),
        (_out(-1, stderr="timeout after 3600s"), False),
    ])
    def test_detects_limit_only_on_error(self, output, expected):
        assert ex.is_usage_limited(output) is expected

    def _run(self, executor, outputs, limit_wait=21600):
        executor._config["timeouts_sec"]["limit_wait"] = limit_wait
        with patch.object(executor, "_invoke_claude", side_effect=outputs) as inv, \
             patch.object(ex.time, "sleep") as sleep:
            result = executor._invoke_until_unlimited({"step": 2, "name": "ui"}, "p")
        return result, inv.call_count, [c.args[0] for c in sleep.call_args_list]

    def test_waits_with_doubling_backoff_capped_at_30_minutes(self, executor):
        ok = _out(0, "{}")
        result, calls, sleeps = self._run(executor, [LIMITED] * 5 + [ok])
        assert result is ok and calls == 6
        assert sleeps == [300, 600, 1200, 1800, 1800]

    def test_gives_up_when_total_wait_reaches_limit_wait(self, executor):
        result, calls, sleeps = self._run(executor, [LIMITED] * 5, limit_wait=900)
        assert result is LIMITED and calls == 3 and sleeps == [300, 600]


# ---------------------------------------------------------------------------
# 고정 AC: execute.py가 AC를 직접 돌리고, 오케스트레이터가 쓴 AC 파일이 그대로인지 본다
# ---------------------------------------------------------------------------

AC_STEP = {"step": 2, "name": "ui", "ac": "pytest tests/ac", "ac_files": ["tests/ac/test_ui.py"]}


def _git(diff_code=0, checkout_code=0, cat_code=0, head="head1"):
    codes = {("diff", "--quiet"): diff_code, ("checkout",): checkout_code, ("cat-file",): cat_code}
    def run(*args):
        code = codes.get(args[:2], codes.get(args[:1], 0))
        return subprocess.CompletedProcess(["git", *args], code, head + "\n" if args[0] == "rev-parse" else "", "")
    return MagicMock(side_effect=run)


def _ac_run(code=0, out="ok"):
    return patch.object(ex.subprocess, "run", return_value=subprocess.CompletedProcess("ac", code, out, ""))


def _set_status(phase_dir, **fields):
    index = json.loads((phase_dir / "index.json").read_text())
    index["steps"][2].update(fields)
    (phase_dir / "index.json").write_text(json.dumps(index))


class TestFixedAc:
    def test_steps_without_ac_are_not_checked(self, executor):
        executor._run_git = _git()
        assert executor._verify_ac({"step": 2, "name": "ui"}, "start") is None
        executor._run_git.assert_not_called()

    def test_changed_ac_file_is_restored_and_fails(self, executor):
        executor._run_git = _git(diff_code=1)
        with _ac_run():
            err = executor._verify_ac(AC_STEP, "start")
        assert "tests/ac/test_ui.py" in err
        executor._run_git.assert_any_call("checkout", "start", "--", "tests/ac/test_ui.py")

    def test_failing_ac_reports_its_output(self, executor):
        executor._run_git = _git()
        with _ac_run(1, "FAILED test_ui"):
            assert "FAILED test_ui" in executor._verify_ac(AC_STEP, "start")

    def test_worker_claims_done_but_ac_fails_is_not_completed(self, executor, phase_dir):
        _set_status(phase_dir, status="completed", summary="done")
        executor._run_git = _git()
        with _ac_run(1, "FAILED test_ui"):
            status, err = executor._judge_attempt(AC_STEP, "start")
        assert status != "completed" and "FAILED test_ui" in err

    def test_timed_out_step_whose_ac_passes_is_completed(self, executor, phase_dir):
        # worker가 시간 초과로 status를 못 바꿨어도 AC가 통과하면 그 작업은 끝난 것이다(체크포인트).
        executor._run_git = _git()
        with _ac_run(0):
            status, _ = executor._judge_attempt(AC_STEP, "start")
        step = json.loads((phase_dir / "index.json").read_text())["steps"][2]
        assert status == "completed" and step["status"] == "completed" and step["summary"]

    def test_worker_error_is_kept_even_if_ac_passes(self, executor, phase_dir):
        _set_status(phase_dir, status="error", error_message="arch rule broken")
        executor._run_git = _git()
        with _ac_run(0) as run:
            status, err = executor._judge_attempt(AC_STEP, "start")
        assert status == "error" and err == "arch rule broken"
        run.assert_not_called()

    def test_ac_base_is_fixed_at_first_run(self, executor, phase_dir):
        # 실행마다 HEAD를 기준으로 잡으면, 앞선 실행에서 커밋된 AC 수정이 새 기준이 되어 검출되지 않는다.
        executor._run_git = _git(head="first")
        assert executor._ac_base() == "first"
        executor._run_git = _git(head="later")
        assert executor._ac_base() == "first"
        assert json.loads((phase_dir / "index.json").read_text())["ac_base"] == "first"

    def test_edited_ac_is_restored_even_when_worker_reports_error(self, executor, phase_dir):
        # error 분기의 커밋이 고친 AC 파일을 담지 않도록 status와 관계없이 먼저 되돌린다.
        _set_status(phase_dir, status="error", error_message="x")
        executor._run_git = _git(diff_code=1)
        with _ac_run(0):
            status, _ = executor._judge_attempt(AC_STEP, "start")
        assert status == "error"
        executor._run_git.assert_any_call("checkout", "start", "--", "tests/ac/test_ui.py")

    def test_failed_restore_is_reported(self, executor):
        executor._run_git = _git(diff_code=1, checkout_code=1)
        with _ac_run():
            assert "되돌리지 못했다" in executor._verify_ac(AC_STEP, "start")

    def test_uncommitted_ac_file_blocks_before_running_worker(self, executor, phase_dir):
        executor._run_git = _git(cat_code=1)
        with patch.object(executor, "_invoke_until_unlimited") as invoke, \
             patch.object(executor, "_record_progress"), pytest.raises(SystemExit) as exc:
            executor._execute_single_step(AC_STEP)
        assert exc.value.code == 2 and not invoke.called
        step = json.loads((phase_dir / "index.json").read_text())["steps"][2]
        assert step["status"] == "blocked" and "tests/ac/test_ui.py" in step["blocked_reason"]

    def test_prompt_lists_fixed_ac_files(self, executor, phase_dir):
        (phase_dir / "step2.md").write_text("# Step 2")
        with patch.object(executor, "_run_claude", return_value=subprocess.CompletedProcess("c", 0, "{}", "")) as rc:
            executor._invoke_claude(AC_STEP, "preamble")
        assert "tests/ac/test_ui.py" in rc.call_args.args[0]


class TestExecuteSingleStepWiring:
    def test_limit_that_never_clears_blocks_the_step(self, executor, phase_dir):
        executor._run_git = _git()
        with patch.object(executor, "_invoke_until_unlimited", return_value=LIMITED), \
             patch.object(executor, "_record_progress"), pytest.raises(SystemExit) as exc:
            executor._execute_single_step({"step": 2, "name": "ui"})
        assert exc.value.code == 2
        assert json.loads((phase_dir / "index.json").read_text())["steps"][2]["status"] == "blocked"

    def test_ac_failure_feeds_next_attempt(self, executor, phase_dir):
        executor._run_git = _git()
        seen = []
        def invoke(step, preamble):
            seen.append(preamble)
            _set_status(phase_dir, status="completed", summary="done")
            return _out(0, "{}")
        with patch.object(executor, "_invoke_until_unlimited", side_effect=invoke), \
             _ac_run(1, "FAILED test_ui"), patch.object(executor, "_record_progress"), pytest.raises(SystemExit):
            executor._execute_single_step(AC_STEP)
        assert len(seen) == ex.StepExecutor.MAX_RETRIES and "FAILED test_ui" in seen[1]
        assert json.loads((phase_dir / "index.json").read_text())["steps"][2]["status"] == "error"

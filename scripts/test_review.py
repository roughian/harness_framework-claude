"""review.py 테스트 — 프롬프트, 리뷰어 명령, herdr/로컬 병렬 실행, 결과 병합."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import review as rv


def _finding(file="src/a.ts", start=10, end=12, priority=1, pre_existing=False, title="bug"):
    return {
        "title": title, "body": "desc", "priority": priority, "confidence": 0.8,
        "file": file, "line_start": start, "line_end": end, "pre_existing": pre_existing,
    }


def _result(findings=None, score=10, verdict="patch is correct"):
    return {
        "findings": findings or [],
        "risk": {"score": score, "axes": {"security": 0, "scope": score, "breaking": 0, "tests": 0, "migration": 0}},
        "verdict": verdict,
        "summary": "ok",
    }


CLAUDE_REVIEWER = {"name": "claude", "cli": "claude", "model": "claude-opus-5-5", "effort": "max"}
CODEX_REVIEWER = {"name": "codex", "cli": "codex", "model": "", "effort": "max"}


# ---------------------------------------------------------------------------
# risk_level
# ---------------------------------------------------------------------------

class TestRiskLevel:
    @pytest.mark.parametrize("score,level", [
        (0, "low"), (15, "low"), (16, "medium"), (35, "medium"),
        (36, "high"), (60, "high"), (61, "critical"), (100, "critical"),
    ])
    def test_boundaries(self, score, level):
        assert rv.risk_level(score) == level


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_includes_base_diff_command(self):
        assert "git diff main" in rv.build_prompt("main")

    @pytest.mark.parametrize("doc", ["REVIEW.md", "CLAUDE.md", "docs/LESSONS.md", "docs/ARCHITECTURE.md", "docs/ADR.md"])
    def test_points_to_rule_docs(self, doc):
        assert doc in rv.build_prompt("main")

    def test_frames_reviewer_as_other_engineer(self):
        assert "다른 엔지니어" in rv.build_prompt("main")


# ---------------------------------------------------------------------------
# reviewer_command
# ---------------------------------------------------------------------------

class TestReviewerCommand:
    def test_claude_is_read_only_and_structured(self, tmp_path):
        cmd = rv.reviewer_command(CLAUDE_REVIEWER, tmp_path / "p.md", tmp_path / "r.json")
        assert cmd.startswith("claude -p")
        assert "--model claude-opus-5-5" in cmd and "--effort max" in cmd
        assert "--json-schema" in cmd
        assert "--allowedTools" in cmd
        assert "dangerously" not in cmd

    def test_codex_uses_read_only_sandbox_and_schema(self, tmp_path):
        cmd = rv.reviewer_command(CODEX_REVIEWER, tmp_path / "p.md", tmp_path / "r.json")
        assert cmd.startswith("codex exec")
        assert "--sandbox read-only" in cmd
        assert "--output-schema" in cmd
        assert "-o" in cmd
        assert "model_reasoning_effort" in cmd


# ---------------------------------------------------------------------------
# parse_raw / validate
# ---------------------------------------------------------------------------

class TestParseRaw:
    def test_claude_structured_output(self):
        envelope = {"type": "result", "structured_output": _result()}
        assert rv.parse_raw("claude", json.dumps(envelope))["verdict"] == "patch is correct"

    def test_claude_result_text_fallback(self):
        envelope = {"type": "result", "result": "```json\n" + json.dumps(_result()) + "\n```"}
        assert rv.parse_raw("claude", json.dumps(envelope))["summary"] == "ok"

    def test_codex_plain_json(self):
        assert rv.parse_raw("codex", json.dumps(_result()))["summary"] == "ok"

    def test_missing_key_raises(self):
        bad = _result()
        del bad["risk"]
        with pytest.raises(ValueError, match="risk"):
            rv.parse_raw("codex", json.dumps(bad))

    def test_invalid_priority_raises(self):
        with pytest.raises(ValueError, match="priority"):
            rv.parse_raw("codex", json.dumps(_result([_finding(priority=7)])))


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

class TestMerge:
    def test_overlapping_findings_merged_with_found_by(self):
        merged = rv.merge({
            "claude": _result([_finding(start=10, end=12, priority=2)]),
            "codex": _result([_finding(start=11, end=15, priority=1)]),
        }, failed={})
        assert len(merged["findings"]) == 1
        f = merged["findings"][0]
        assert f["found_by"] == ["claude", "codex"]
        assert f["priority"] == 1

    def test_different_files_not_merged(self):
        merged = rv.merge({
            "claude": _result([_finding(file="a.ts")]),
            "codex": _result([_finding(file="b.ts")]),
        }, failed={})
        assert len(merged["findings"]) == 2

    def test_risk_is_max_and_level_recomputed(self):
        merged = rv.merge({"claude": _result(score=12), "codex": _result(score=40)}, failed={})
        assert merged["risk"]["score"] == 40
        assert merged["risk"]["level"] == "high"

    def test_blocking_excludes_pre_existing_and_low_priority(self):
        merged = rv.merge({"claude": _result([
            _finding(file="a", priority=0),
            _finding(file="b", priority=1, pre_existing=True),
            _finding(file="c", priority=2),
        ])}, failed={})
        assert merged["blocking"] == 1

    def test_findings_sorted_by_priority(self):
        merged = rv.merge({"claude": _result([_finding(file="a", priority=3), _finding(file="b", priority=0)])}, failed={})
        assert [f["priority"] for f in merged["findings"]] == [0, 3]

    def test_nits_capped(self):
        nits = [_finding(file=f"f{i}", priority=3) for i in range(8)]
        merged = rv.merge({"claude": _result(nits)}, failed={})
        assert len(merged["findings"]) == rv.MAX_NITS

    def test_any_incorrect_verdict_wins(self):
        merged = rv.merge({"claude": _result(), "codex": _result(verdict="patch is incorrect")}, failed={})
        assert merged["verdict"] == "patch is incorrect"

    def test_failed_reviewers_recorded(self):
        merged = rv.merge({"claude": _result()}, failed={"codex": "timeout"})
        assert merged["failed"] == {"codex": "timeout"}
        assert merged["reviewers"] == ["claude"]


# ---------------------------------------------------------------------------
# 실행 모드
# ---------------------------------------------------------------------------

class TestUseHerdr:
    def test_true_inside_herdr(self, monkeypatch):
        monkeypatch.setenv("HERDR_ENV", "1")
        with patch("shutil.which", return_value="/bin/herdr"):
            assert rv.use_herdr(allow=True)

    def test_false_outside_herdr(self, monkeypatch):
        monkeypatch.delenv("HERDR_ENV", raising=False)
        assert not rv.use_herdr(allow=True)

    def test_false_when_disabled(self, monkeypatch):
        monkeypatch.setenv("HERDR_ENV", "1")
        with patch("shutil.which", return_value="/bin/herdr"):
            assert not rv.use_herdr(allow=False)


class TestRunInHerdr:
    def test_starts_all_before_waiting_and_closes_own_panes(self):
        calls = []
        split_ids = iter(["w1:p7", "w1:p8"])

        def fake_run(cmd, **kwargs):
            calls.append(cmd[1:3])
            out = ""
            if cmd[1:3] == ["pane", "split"]:
                out = json.dumps({"result": {"pane": {"pane_id": next(split_ids)}}})
            return MagicMock(returncode=0, stdout=out, stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            errors = rv.run_in_herdr({"claude": "cmd1", "codex": "cmd2"}, timeout_sec=10)

        actions = [c[1] for c in calls]
        assert actions == ["split", "run", "split", "run", "wait-output", "wait-output", "close", "close"]
        assert errors == {}

    def test_done_marker_not_matched_by_command_echo(self):
        sent = []

        def fake_run(cmd, **kwargs):
            if cmd[1:3] == ["pane", "run"]:
                sent.append(cmd[4])
            if cmd[1:3] == ["pane", "wait-output"]:
                sent.append(cmd[cmd.index("--match") + 1])
            out = json.dumps({"result": {"pane": {"pane_id": "w1:p9"}}})
            return MagicMock(returncode=0, stdout=out, stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            rv.run_in_herdr({"claude": "cmd1"}, timeout_sec=10)

        command_text, marker = sent
        assert marker not in command_text

    def test_wait_failure_reported_per_reviewer(self):
        def fake_run(cmd, **kwargs):
            if cmd[1:3] == ["pane", "wait-output"]:
                return MagicMock(returncode=1, stdout="", stderr='{"error":"timeout"}')
            out = json.dumps({"result": {"pane": {"pane_id": "w1:p9"}}})
            return MagicMock(returncode=0, stdout=out, stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            errors = rv.run_in_herdr({"codex": "cmd"}, timeout_sec=10)
        assert "codex" in errors


class TestRunLocal:
    def test_timeout_reported_per_reviewer(self):
        def fake_run(cmd, **kwargs):
            if cmd == "slow":
                raise subprocess.TimeoutExpired(cmd, 1)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            errors = rv.run_local({"claude": "fast", "codex": "slow"}, timeout_sec=1)
        assert list(errors) == ["codex"]


# ---------------------------------------------------------------------------
# run_review (end-to-end, 실행부 mock)
# ---------------------------------------------------------------------------

class TestRunReview:
    @pytest.fixture
    def config(self):
        return {
            "roles": {"reviewers": [CLAUDE_REVIEWER, CODEX_REVIEWER]},
            "timeouts_sec": {"step": 10, "review": 10},
        }

    def _fake_runner(self, raws):
        def runner(commands, timeout_sec):
            for name, (path, text) in raws.items():
                if text is not None:
                    path.write_text(text)
            return {}
        return runner

    def test_one_reviewer_failing_still_merges(self, tmp_path, config):
        work = tmp_path / ".review"
        raws = {
            "claude": (work / "review-claude.raw.json", json.dumps({"structured_output": _result([_finding(priority=0)])})),
            "codex": (work / "review-codex.raw.json", None),
        }
        out = tmp_path / "review.json"
        with patch.object(rv, "run_local", side_effect=self._fake_runner(raws)):
            merged = rv.run_review("main", out, config=config, work_dir=work, allow_herdr=False)
        assert merged["blocking"] == 1
        assert "codex" in merged["failed"]
        assert json.loads(out.read_text())["blocking"] == 1

    def test_all_failed_raises(self, tmp_path, config):
        work = tmp_path / ".review"
        with patch.object(rv, "run_local", return_value={}):
            with pytest.raises(rv.ReviewError):
                rv.run_review("main", tmp_path / "r.json", config=config, work_dir=work, allow_herdr=False)

    def test_reviewer_filter(self, tmp_path, config):
        work = tmp_path / ".review"
        seen = {}

        def runner(commands, timeout_sec):
            seen.update(commands)
            (work / "review-codex.raw.json").write_text(json.dumps(_result()))
            return {}

        with patch.object(rv, "run_local", side_effect=runner):
            rv.run_review("main", tmp_path / "r.json", config=config, work_dir=work,
                          allow_herdr=False, only=["codex"])
        assert list(seen) == ["codex"]


class TestFixPrompt:
    def test_lists_blocking_findings_and_lessons_rule(self):
        merged = rv.merge({"claude": _result([
            _finding(file="a.ts", priority=0, title="SQL injection"),
            _finding(file="b.ts", priority=3, title="naming nit"),
        ])}, failed={})
        prompt = rv.build_fix_prompt(merged, project="P")
        assert "SQL injection" in prompt and "a.ts:10" in prompt
        assert "naming nit" not in prompt
        assert "docs/LESSONS.md" in prompt


class TestReviewFindingsRegression:
    """교차 리뷰 스모크에서 나온 지적의 회귀 테스트."""

    def test_new_finding_overlapping_pre_existing_stays_blocking(self):
        merged = rv.merge({
            "claude": _result([_finding(priority=1, pre_existing=True)]),
            "codex": _result([_finding(priority=1, pre_existing=False)]),
        }, failed={})
        assert merged["findings"][0]["pre_existing"] is False
        assert merged["blocking"] == 1

    def test_unknown_reviewer_name_raises(self, tmp_path):
        config = {"roles": {"reviewers": [CLAUDE_REVIEWER]}, "timeouts_sec": {"step": 1, "review": 1}}
        with pytest.raises(rv.ReviewError, match="nope"):
            rv.run_review("main", tmp_path / "r.json", config=config, work_dir=tmp_path,
                          allow_herdr=False, only=["nope"])

    def test_runner_error_marks_reviewer_failed_even_with_raw(self, tmp_path):
        config = {"roles": {"reviewers": [CLAUDE_REVIEWER, CODEX_REVIEWER]}, "timeouts_sec": {"step": 1, "review": 1}}

        def runner(commands, timeout_sec):
            (tmp_path / "review-claude.raw.json").write_text(json.dumps({"structured_output": _result()}))
            (tmp_path / "review-codex.raw.json").write_text(json.dumps(_result()))
            return {"codex": "exit 1: auth error"}

        with patch.object(rv, "run_local", side_effect=runner):
            merged = rv.run_review("main", tmp_path / "r.json", config=config, work_dir=tmp_path, allow_herdr=False)
        assert merged["reviewers"] == ["claude"]
        assert merged["failed"] == {"codex": "exit 1: auth error"}

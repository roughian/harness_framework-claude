"""review.py 테스트 — 프롬프트, 리뷰어 선택, 병렬 실행, 결과 병합, 검증 리뷰, 상태·캐시."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import review as rv

REAL_REVIEW_LESSONS = rv.review_lessons  # autouse가 막아 두므로 TestReviewLessons가 원본을 쓴다


def _finding(file="src/a.ts", start=10, end=12, priority=1, pre_existing=False, title="bug",
             confirmed=True, repro="npm test -- a"):
    return {
        "title": title, "body": "desc", "priority": priority, "confidence": 0.8,
        "file": file, "line_start": start, "line_end": end, "pre_existing": pre_existing,
        "repro": repro, "confirmed": confirmed,
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
HEAD_SHA = "f" * 40


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """상태·캐시 파일은 임시 폴더로, git 조회는 고정값으로."""
    monkeypatch.setattr(rv, "REVIEW_DIR", tmp_path / "review-dir")
    monkeypatch.setattr(rv, "STATUS_FILE", tmp_path / "review-dir" / "status.json")
    monkeypatch.setattr(rv, "PASSED_FILE", tmp_path / "review-dir" / "passed")
    monkeypatch.setattr(rv, "RESULTS_DIR", tmp_path / "review-dir" / "results")
    monkeypatch.setattr(rv, "resolve_commit", lambda ref: HEAD_SHA)
    monkeypatch.setattr(rv, "is_worktree_clean", lambda: True)
    monkeypatch.setattr(rv, "changed_lines", lambda diff_range: {"src/big.ts": 500})
    monkeypatch.setattr(rv, "merge_base", lambda a, b: "b" * 40)
    monkeypatch.setattr(rv, "commit_subjects", lambda base, head: [])
    monkeypatch.setattr(rv, "LESSONS_FILE", tmp_path / "LESSONS.md")
    monkeypatch.setattr(rv, "review_lessons", lambda base, head: [])


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

    def test_asks_for_repro_and_confirmation(self):
        prompt = rv.build_prompt("main")
        assert "repro" in prompt and "confirmed" in prompt

    def test_head_reviews_range(self):
        assert "git diff abc...def" in rv.build_prompt("abc", head="def")

    def test_head_reads_files_from_snapshot_not_worktree(self):
        assert "git show def:<경로>" in rv.build_prompt("abc", head="def")
        assert "git show" not in rv.build_prompt("main").split("확인하라.")[1].split("\n")[0]

    def test_states_intent_from_commit_subjects(self):
        prompt = rv.build_prompt("main", intent=["feat: add retry", "fix: keep timeout"])
        assert "## 이 변경의 의도" in prompt and "- feat: add retry" in prompt

    def test_omits_intent_without_commits(self):
        assert "## 이 변경의 의도" not in rv.build_prompt("main")

    def test_injects_relevant_lessons(self):
        prompt = rv.build_prompt("main", lessons=["- [2026-10-10] 훅 규칙 [키: scripts/hooks]"])
        assert "## 이 변경과 관련된 LESSONS" in prompt and "훅 규칙" in prompt
        assert "grep" not in prompt

    def test_run_review_passes_intent_and_lessons(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rv, "review_lessons", lambda base, head: ["- [2026-10-10] 큰 파일 규칙 [키: src/big]"])
        monkeypatch.setattr(rv, "commit_subjects", lambda base, head: ["feat: big change"])
        seen = {}
        def fake_run(reviewers, prompt, timeout, work_dir):
            seen["prompt"] = prompt
            return {"codex": _result()}, {}
        monkeypatch.setattr(rv, "_run_reviewers", fake_run)
        config = {"roles": {"reviewers": [CODEX_REVIEWER]}, "timeouts_sec": {"step": 1, "review": 1}}
        rv.run_review("main", tmp_path / "out.json", config=config, head=HEAD_SHA)
        assert "feat: big change" in seen["prompt"] and "큰 파일 규칙" in seen["prompt"]


# ---------------------------------------------------------------------------
# relevant_lessons
# ---------------------------------------------------------------------------

class TestRelevantLessons:
    LINES = ("# LESSONS\n<!-- - [YYYY-MM-DD] 예시 [키: scripts] -->\n"
             "- [2026-10-10] 훅 규칙 [키: scripts/hooks, .claude/settings]\n"
             "- [2026-10-12] gh 규칙 [키: gh api]\n"
             "- [2026-10-13] 키 없는 규칙\n")

    def _lessons(self, tmp_path, texts):
        path = tmp_path / "LESSONS.md"
        path.write_text(self.LINES)
        return rv.relevant_lessons(texts, path)

    def test_returns_lines_whose_key_appears(self, tmp_path):
        assert self._lessons(tmp_path, ["scripts/hooks/guard.sh"]) == [
            "- [2026-10-10] 훅 규칙 [키: scripts/hooks, .claude/settings]"]

    def test_any_key_in_any_text_matches(self, tmp_path):
        found = self._lessons(tmp_path, ["src/a.py", "`gh api --paginate`를 쓴다"])
        assert found == ["- [2026-10-12] gh 규칙 [키: gh api]"]

    def test_unrelated_and_keyless_lines_are_skipped(self, tmp_path):
        assert self._lessons(tmp_path, ["src/app.ts 키 없는 규칙"]) == []

    def test_missing_file_is_empty(self, tmp_path):
        assert rv.relevant_lessons(["scripts/hooks/x"], tmp_path / "none.md") == []


class TestReviewLessons:
    WORKTREE = "- [2026-10-10] 작업 트리 규칙 [키: scripts/hooks]\n"
    AT_HEAD = "- [2026-10-10] 커밋 규칙 [키: scripts/hooks]\n- [2026-10-12] gh 규칙 [키: gh api]\n"

    @pytest.fixture
    def fake_git(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rv, "review_lessons", REAL_REVIEW_LESSONS)
        (tmp_path / "LESSONS.md").write_text(self.WORKTREE + "- [2026-10-12] gh 규칙 [키: gh api]\n")
        outputs = {"show": self.AT_HEAD, "diff": "+++ b/scripts/hooks/x.sh\n+echo hi\n", "ls-files": ""}
        monkeypatch.setattr(rv, "_git", lambda *args: outputs[args[0]])
        return outputs

    def test_commit_review_reads_lessons_at_head(self, fake_git):
        assert rv.review_lessons("main", "def") == ["- [2026-10-10] 커밋 규칙 [키: scripts/hooks]"]

    def test_word_key_matches_diff_content(self, fake_git):
        fake_git["diff"] = "+++ b/src/a.py\n+run(['gh api', '--paginate'])\n"
        assert rv.review_lessons("main", "def") == ["- [2026-10-12] gh 규칙 [키: gh api]"]

    def test_worktree_review_matches_untracked_new_files(self, fake_git):
        fake_git["diff"] = ""
        fake_git["ls-files"] = "scripts/hooks/new.sh\n"
        assert rv.review_lessons("main", None) == ["- [2026-10-10] 작업 트리 규칙 [키: scripts/hooks]"]


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

    def test_claude_denies_tools_outside_allow_list(self, tmp_path):
        # 사용자 설정이 auto 모드면 --allowedTools 밖의 명령(pytest, rm)도 승인된다. 쓰기 도구는 아예 빼고 나머지는 거부한다.
        cmd = rv.reviewer_command(CLAUDE_REVIEWER, tmp_path / "p.md", tmp_path / "r.json")
        assert "--permission-mode dontAsk" in cmd
        assert "--tools Read,Grep,Glob,Bash" in cmd

    def test_tool_names_come_from_allow_list(self):
        # --tools와 --allowedTools가 따로 적히면 한쪽만 고쳐 어긋난다. 허용 목록에서 이름만 뽑는다.
        assert rv.tool_names("Read,Bash(git diff:*),Bash(git log:*),Grep") == "Read,Bash,Grep"

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

    @pytest.mark.parametrize("key", ["repro", "confirmed"])
    def test_missing_new_finding_key_raises(self, key):
        finding = _finding()
        del finding[key]
        with pytest.raises(ValueError, match=key):
            rv.parse_raw("codex", json.dumps(_result([finding])))

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

    def test_unconfirmed_p1_is_not_blocking(self):
        merged = rv.merge({"claude": _result([_finding(priority=1, confirmed=False)])}, failed={})
        assert merged["blocking"] == 0
        assert len(merged["findings"]) == 1

    def test_unconfirmed_p0_still_blocks(self):
        # confirmed는 모델이 스스로 매긴다. 읽기 전용이라 확인하지 못한 P0를 통과시키면 안 된다.
        merged = rv.merge({"claude": _result([_finding(priority=0, confirmed=False)])}, failed={})
        assert merged["blocking"] == 1

    def test_unconfirmed_p1_agreed_by_two_reviewers_blocks(self):
        merged = rv.merge({
            "claude": _result([_finding(priority=1, confirmed=False)]),
            "codex": _result([_finding(priority=1, confirmed=False)]),
        }, failed={})
        assert merged["findings"][0]["agreed_by"] == ["claude", "codex"]
        assert merged["blocking"] == 1

    def test_confirmed_by_any_reviewer_wins(self):
        merged = rv.merge({
            "claude": _result([_finding(confirmed=False, repro="")]),
            "codex": _result([_finding(confirmed=True, repro="run x")]),
        }, failed={})
        f = merged["findings"][0]
        assert f["confirmed"] is True and f["repro"] == "run x"
        assert merged["blocking"] == 1

    def test_unconfirmed_higher_priority_not_upgraded_by_confirmed_lower(self):
        merged = rv.merge({
            "claude": _result([_finding(priority=2, confirmed=True)]),
            "codex": _result([_finding(priority=1, confirmed=False)]),
        }, failed={})
        f = merged["findings"][0]
        assert f["priority"] == 1 and f["confirmed"] is False
        assert merged["blocking"] == 0

    def test_confirmed_same_priority_stays_confirmed(self):
        merged = rv.merge({
            "claude": _result([_finding(priority=1, confirmed=False)]),
            "codex": _result([_finding(priority=1, confirmed=True)]),
        }, failed={})
        assert merged["blocking"] == 1

    def test_legacy_finding_without_confirmed_counts_as_confirmed(self):
        legacy = _finding()
        del legacy["confirmed"]
        assert rv.blocking_findings({"findings": [legacy]}) == [legacy]

    def test_summary_marks_unconfirmed(self):
        merged = rv.merge({"claude": _result([_finding(confirmed=False)])}, failed={})
        assert "(미확인)" in rv.format_summary(merged)

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
            merged = rv.run_review("main", out, config=config, work_dir=work)
        assert merged["blocking"] == 1
        assert "codex" in merged["failed"]
        assert json.loads(out.read_text())["blocking"] == 1

    def test_all_failed_raises(self, tmp_path, config):
        work = tmp_path / ".review"
        with patch.object(rv, "run_local", return_value={}):
            with pytest.raises(rv.ReviewError):
                rv.run_review("main", tmp_path / "r.json", config=config, work_dir=work)

    def test_reviewer_filter(self, tmp_path, config):
        work = tmp_path / ".review"
        seen = {}

        def runner(commands, timeout_sec):
            seen.update(commands)
            (work / "review-codex.raw.json").write_text(json.dumps(_result()))
            return {}

        with patch.object(rv, "run_local", side_effect=runner):
            rv.run_review("main", tmp_path / "r.json", config=config, work_dir=work, only=["codex"])
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
        assert "docs/LESSONS.md" in prompt and "[키:" in prompt

    def test_includes_repro(self):
        merged = rv.merge({"claude": _result([_finding(priority=0, repro="curl /x?id=1'")])}, failed={})
        assert "curl /x?id=1'" in rv.build_fix_prompt(merged, project="P")


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
            rv.run_review("main", tmp_path / "r.json", config=config, work_dir=tmp_path, only=["nope"])

    def test_runner_error_marks_reviewer_failed_even_with_raw(self, tmp_path):
        config = {"roles": {"reviewers": [CLAUDE_REVIEWER, CODEX_REVIEWER]}, "timeouts_sec": {"step": 1, "review": 1}}

        def runner(commands, timeout_sec):
            (tmp_path / "review-claude.raw.json").write_text(json.dumps({"structured_output": _result()}))
            (tmp_path / "review-codex.raw.json").write_text(json.dumps(_result()))
            return {"codex": "exit 1: auth error"}

        with patch.object(rv, "run_local", side_effect=runner):
            merged = rv.run_review("main", tmp_path / "r.json", config=config, work_dir=tmp_path)
        assert merged["reviewers"] == ["claude"]
        assert merged["failed"] == {"codex": "exit 1: auth error"}


# ---------------------------------------------------------------------------
# 리뷰어 선택 (위험 등급)
# ---------------------------------------------------------------------------

class TestChooseReviewers:
    CONFIG = {"roles": {"reviewers": [CLAUDE_REVIEWER, CODEX_REVIEWER]}}

    def test_small_change_uses_single_reviewer(self):
        assert rv.choose_reviewers(self.CONFIG, {"src/a.ts": 40, "src/b.ts": 30}) == ["claude"]

    def test_large_change_uses_cross_review(self):
        assert rv.choose_reviewers(self.CONFIG, {"src/a.ts": 101}) == ["claude", "codex"]

    @pytest.mark.parametrize("path", ["src/auth/login.ts", ".claude/settings.json", "db/migrations/1.sql"])
    def test_sensitive_path_always_cross_reviewed(self, path):
        assert rv.choose_reviewers(self.CONFIG, {path: 1}) == ["claude", "codex"]

    def test_generated_files_do_not_count(self):
        assert rv.choose_reviewers(self.CONFIG, {"package-lock.json": 5000, "src/a.ts": 10}) == ["claude"]

    def test_threshold_from_config(self):
        config = {**self.CONFIG, "review": {"single_reviewer_max_lines": 5}}
        assert rv.choose_reviewers(config, {"src/a.ts": 10}) == ["claude", "codex"]


# ---------------------------------------------------------------------------
# 검증 리뷰 · 상태 · 캐시
# ---------------------------------------------------------------------------

def _writer(work, results):
    """run_local 대역: 리뷰어별 raw 결과를 쓰고, 실행된 명령을 기록한다."""
    seen = {}

    def runner(commands, timeout_sec):
        seen.update(commands)
        for name in commands:
            if name in results:
                (work / f"review-{name}.raw.json").write_text(json.dumps(results[name]))
        return {}
    return runner, seen


class TestVerifyReview:
    CONFIG = {"roles": {"reviewers": [CLAUDE_REVIEWER, CODEX_REVIEWER]}, "timeouts_sec": {"step": 1, "review": 1}}

    def _previous(self):
        merged = rv.merge({"codex": _result([_finding(priority=1, title="race in cache")], score=40)}, failed={})
        return {"base": "main", "head": "a" * 40, **merged}

    def test_prompt_has_previous_findings_and_fix_diff_only(self):
        prompt = rv.build_verify_prompt(self._previous())
        assert "race in cache" in prompt
        assert f"git diff {'a' * 40}" in prompt
        assert "전체를 다시 리뷰하지 마라" in prompt

    def test_rounds_count_up_from_first_review(self, tmp_path):
        work = tmp_path / "w"
        work.mkdir()
        runner, _ = _writer(work, {"codex": _result(), "claude": _result()})
        with patch.object(rv, "run_local", side_effect=runner):
            first = rv.run_review("main", tmp_path / "r.json", config=self.CONFIG, work_dir=work)
            again = rv.run_review("main", tmp_path / "r.json", config=self.CONFIG, work_dir=work,
                                  previous={**self._previous(), "round": 1})
        assert (first["round"], again["round"]) == (0, 2)

    def test_only_reviewers_that_found_blocking_issues_rerun(self, tmp_path):
        work = tmp_path / "w"
        work.mkdir()
        runner, seen = _writer(work, {"codex": {**_result(), "resolved": [1]}})
        with patch.object(rv, "run_local", side_effect=runner):
            merged = rv.run_review("main", tmp_path / "r.json", config=self.CONFIG, work_dir=work,
                                   previous=self._previous())
        assert list(seen) == ["codex"]
        assert merged["mode"] == "verify" and merged["blocking"] == 0
        assert "race in cache" in (work / "review-prompt.md").read_text()

    def test_verify_keeps_first_review_risk(self, tmp_path):
        work = tmp_path / "w"
        work.mkdir()
        runner, _ = _writer(work, {"codex": _result(score=5)})
        with patch.object(rv, "run_local", side_effect=runner):
            merged = rv.run_review("main", tmp_path / "r.json", config=self.CONFIG, work_dir=work,
                                   previous=self._previous())
        assert merged["risk"]["score"] == 40

    def test_unresolved_previous_finding_is_carried_even_if_omitted(self, tmp_path):
        # 검증 리뷰어가 남은 지적을 다시 적지 않아도, 해결로 확인하지 않았으면 차단이 이어진다.
        work = tmp_path / "w"
        work.mkdir()
        runner, _ = _writer(work, {"codex": {**_result(), "resolved": []}})
        with patch.object(rv, "run_local", side_effect=runner):
            merged = rv.run_review("main", tmp_path / "r.json", config=self.CONFIG, work_dir=work,
                                   previous=self._previous())
        assert merged["blocking"] == 1
        assert merged["findings"][0]["title"] == "race in cache" and merged["findings"][0]["carried"]

    def test_reported_again_with_weaker_flags_still_blocks(self):
        # 남은 지적을 미확인·기존 문제로 다시 보고해도 원래 차단 속성이 유지된다.
        weaker = _finding(priority=1, confirmed=False, pre_existing=True, title="race (again)")
        merged = rv.carry_unresolved(
            rv.merge({"codex": {**_result([weaker]), "resolved": []}}, failed={}),
            self._previous(), {"codex": {"resolved": []}})
        assert merged["blocking"] == 1
        assert [f["title"] for f in merged["findings"]] == ["race in cache"]

    def test_new_blocking_finding_at_same_place_is_kept(self):
        # 남은 지적과 겹치는 자리의 새 차단 지적은 버리지 않는다.
        new_issue = _finding(priority=0, title="new injection")
        merged = rv.carry_unresolved(
            rv.merge({"codex": {**_result([new_issue]), "resolved": []}}, failed={}),
            self._previous(), {"codex": {"resolved": []}})
        assert sorted(f["title"] for f in merged["findings"]) == ["new injection", "race in cache"]
        assert merged["blocking"] == 2

    def test_verify_prompt_with_head_compares_commits_not_worktree(self):
        prompt = rv.build_verify_prompt(self._previous(), head="c" * 40)
        assert f"git diff {'a' * 40}..{'c' * 40}" in prompt
        assert f"git show {'c' * 40}:<경로>" in prompt

    def test_verify_prompt_numbers_previous_findings(self):
        assert "[1] P1" in rv.build_verify_prompt(self._previous())


class TestStatusAndCache:
    CONFIG = {"roles": {"reviewers": [CLAUDE_REVIEWER, CODEX_REVIEWER]}, "timeouts_sec": {"step": 1, "review": 1}}

    def test_passing_review_records_head_and_status(self, tmp_path):
        work = tmp_path / "w"
        work.mkdir()
        runner, _ = _writer(work, {"claude": {"structured_output": _result()}, "codex": _result()})
        with patch.object(rv, "run_local", side_effect=runner):
            merged = rv.run_review("main", tmp_path / "r.json", config=self.CONFIG, work_dir=work)
        assert merged["head"] == HEAD_SHA
        assert rv.is_passed("b" * 40, HEAD_SHA)
        assert rv.load_result(HEAD_SHA)["blocking"] == 0
        status = json.loads(rv.STATUS_FILE.read_text())
        assert status["state"] == "done" and status["mode"] == "first" and status["blocking"] == 0

    def test_blocking_review_is_not_cached(self, tmp_path):
        work = tmp_path / "w"
        work.mkdir()
        runner, _ = _writer(work, {"codex": _result([_finding(priority=0)])})
        with patch.object(rv, "run_local", side_effect=runner):
            rv.run_review("main", tmp_path / "r.json", config=self.CONFIG, work_dir=work, only=["codex"])
        assert not rv.is_passed("b" * 40, HEAD_SHA)

    def test_dirty_worktree_is_not_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rv, "is_worktree_clean", lambda: False)
        work = tmp_path / "w"
        work.mkdir()
        runner, _ = _writer(work, {"codex": _result()})
        with patch.object(rv, "run_local", side_effect=runner):
            rv.run_review("main", tmp_path / "r.json", config=self.CONFIG, work_dir=work, only=["codex"])
        assert not rv.is_passed("b" * 40, HEAD_SHA)
        # 커밋 안 된 변경이 섞인 결과를 HEAD 커밋의 리뷰로 저장하면 PR 단계가 그 커밋의 결과로 믿는다
        assert rv.load_result(HEAD_SHA) is None

    def test_own_work_dir_is_removed(self, tmp_path):
        def runner(commands, timeout_sec):
            for name, command in commands.items():
                raw = command.split("-o ")[1].split(" ")[0]
                Path(raw).write_text(json.dumps(_result()))
            return {}
        with patch.object(rv, "run_local", side_effect=runner):
            rv.run_review("main", tmp_path / "r.json", config=self.CONFIG, only=["codex"])
        assert [p.name for p in rv.REVIEW_DIR.iterdir() if p.name.startswith("run-")] == []


class TestFixPromptSameKind:
    def test_asks_to_fix_every_place_of_same_kind(self):
        merged = rv.merge({"claude": _result([_finding(priority=0)])}, failed={})
        assert "같은 종류의 문제가 있는 모든 자리" in rv.build_fix_prompt(merged, project="P")


class TestGit:
    def test_non_utf8_diff_does_not_crash(self, tmp_path, monkeypatch):
        run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
        run("init", "-q")
        run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        (tmp_path / "latin1.txt").write_bytes(b"caf\xe9\n")
        run("add", "latin1.txt")
        monkeypatch.setattr(rv, "ROOT", tmp_path)
        assert "latin1.txt" in rv._git("diff", "--cached")

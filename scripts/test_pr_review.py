"""pr_review.py 테스트 — 리뷰 엔진(review.run_review)·gh·git 호출은 전부 mock한다."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import pr_review as pr
import review as rv

GH_CONFIG = {"model": "m-1", "effort": "high", "reviewers": ["alice", "bob"],
             "seniors": ["sen"], "notify": ["cto"]}
ENV = {"OPENAI_API_KEY": "k", "PR_NUMBER": "7", "BASE_REF": "main", "HEAD_REPO": "o/r", "REPO": "o/r"}
ADR_BODY = "배경 설명 docs/ADR.md 참고 ADR-003"


def _finding(priority=1, confirmed=True, pre_existing=False, line=3):
    return {"title": "bug", "body": "desc", "priority": priority, "confidence": 0.9, "file": "a.py",
            "line_start": line, "line_end": line, "pre_existing": pre_existing,
            "repro": "pytest a", "confirmed": confirmed}


def _result(findings=None, score=10, verdict="patch is correct"):
    return {"findings": findings or [],
            "risk": {"score": score, "axes": {"security": score, "scope": 0, "breaking": 0, "tests": 0, "migration": 0}},
            "verdict": verdict, "summary": "요약"}


def _merged(findings=None, score=10):
    return rv.merge({"openai": _result(findings, score)}, failed={})


class FakeGh:
    """gh 호출을 기록하고 인자 패턴별로 응답을 돌려준다."""

    def __init__(self, comments=None, pr_info=None):
        self.calls = []
        self.comments = comments or []
        self.pr_info = pr_info or {"body": ADR_BODY, "isDraft": False, "labels": []}

    def __call__(self, args, check=True):
        self.calls.append(list(args))
        if args[:2] == ["pr", "view"]:
            return json.dumps(self.pr_info)
        if args[0] == "api" and "--paginate" in args:
            # --jq 필터 결과처럼 마커로 시작하는 코멘트 id를 한 줄씩 돌려준다.
            assert "--jq" in args
            return "".join(f"{c['id']}\n" for c in self.comments if c["body"].startswith(pr.MARKER))
        return ""

    def joined(self):
        return [" ".join(c) for c in self.calls]


def _patched(gh, engine, config=GH_CONFIG):
    return [patch.object(pr, "gh", gh), patch.object(pr, "diff_stats", return_value=(1, 10)),
            patch.object(pr, "npm_audit_summary", return_value=""),
            patch.object(pr, "load_github_config", return_value=config),
            patch.object(pr, "run_engine", **engine)]


def _run(score_findings, env=None, gh=None, pr_body=ADR_BODY, score=10, config=GH_CONFIG):
    gh = gh or FakeGh(pr_info={"body": pr_body, "isDraft": False, "labels": []})
    patches = _patched(gh, {"return_value": _merged(score_findings, score)}, config)
    with patches[0], patches[1], patches[2], patches[3], patches[4] as call:
        code = pr.main([], env=env or ENV)
    return code, gh, call


@pytest.mark.parametrize("files,lines,expected", [
    (0, 0, 0), (5, 100, 4), (1, 10, 1), (30, 10, 20), (1, 2001, 20), (100, 5000, 20),
])
def test_scope_score_boundaries(files, lines, expected):
    assert pr.scope_score(files, lines) == expected


def test_scope_score_is_monotonic_between_bounds():
    assert 4 < pr.scope_score(15, 500) < 20
    assert pr.scope_score(10, 500) <= pr.scope_score(20, 500)


def test_apply_scope_overrides_model_value_and_recomputes_total():
    result = _result(score=50)
    result["risk"]["axes"]["scope"] = 20
    out = pr.apply_scope(result, files=0, lines=0)
    assert out["risk"]["axes"]["scope"] == 0
    assert out["risk"]["score"] == sum(out["risk"]["axes"].values())


@pytest.mark.parametrize("score,label,reviewer,auto_off,extra,notify", [
    (10, "risk:low", [], False, [], []),
    (20, "risk:medium", ["alice"], True, [], []),
    (50, "risk:high", ["sen"], True, ["security", "architecture"], []),
    (80, "risk:critical", ["sen"], True, ["security", "architecture"], ["cto"]),
])
def test_plan_actions_per_level(score, label, reviewer, auto_off, extra, notify):
    merged = _merged(score=score)
    plan = pr.plan_actions(merged["risk"]["level"], merged, ADR_BODY, GH_CONFIG)
    assert plan["label"] == label
    assert plan["reviewers"] == reviewer
    assert plan["disable_auto_merge"] is auto_off
    assert plan["extra_reviews"] == extra
    assert plan["notify"] == notify
    assert plan["fail"] is False


def test_plan_low_marks_auto_merge_candidate():
    merged = _merged(score=5)
    assert pr.plan_actions("low", merged, "", GH_CONFIG)["auto_merge_candidate"] is True


def test_plan_reviewers_unset_is_reported():
    merged = _merged(score=20)
    plan = pr.plan_actions("medium", merged, "", {})
    assert plan["reviewers"] == [] and plan["reviewer_missing"] is True


def test_plan_critical_without_adr_link_fails():
    merged = _merged(score=80)
    plan = pr.plan_actions("critical", merged, "그냥 변경", GH_CONFIG)
    assert plan["adr_missing"] is True and plan["fail"] is True


def test_plan_critical_with_adr_link_passes():
    merged = _merged(score=80)
    plan = pr.plan_actions("critical", merged, ADR_BODY, GH_CONFIG)
    assert plan["adr_missing"] is False and plan["fail"] is False


def test_plan_confirmed_p1_fails_unconfirmed_does_not():
    assert pr.plan_actions("low", _merged([_finding(1, True)]), "", GH_CONFIG)["fail"] is True
    assert pr.plan_actions("low", _merged([_finding(1, False)]), "", GH_CONFIG)["fail"] is False


def test_upsert_comment_patches_existing_marker():
    gh = FakeGh(comments=[{"id": 11, "body": "other"}, {"id": 22, "body": pr.MARKER + "\nold"}])
    with patch.object(pr, "gh", gh):
        pr.upsert_comment("o/r", 7, pr.MARKER + "\nnew")
    assert any("-X PATCH repos/o/r/issues/comments/22" in c for c in gh.joined())


def test_critical_without_notify_or_senior_flags_missing():
    merged = _merged([], score=80)
    assert pr.plan_actions("critical", merged, ADR_BODY, {"notify": [], "seniors": ["s"]})["reviewer_missing"] is True
    assert pr.plan_actions("critical", merged, ADR_BODY, {"notify": ["lead"]})["reviewer_missing"] is True
    assert pr.plan_actions("critical", merged, ADR_BODY,
                           {"notify": ["lead"], "seniors": ["s"]})["reviewer_missing"] is False


def test_blocking_extra_review_fails_check():
    # high 등급의 보안 집중 리뷰가 찾은 확인된 P0도 체크를 실패시켜야 한다.
    merged = _merged([], score=50)
    plan = pr.plan_actions("high", merged, ADR_BODY, GH_CONFIG)
    pr.apply_extras(merged, plan, {"security": _merged([_finding(0, True)])})
    assert plan["fail"] is True and merged["blocking"] == 1


def test_high_extra_review_blocking_exits_one():
    results = [_merged([], 50), _merged([_finding(0, True)], 50), _merged([], 50)]
    patches = _patched(FakeGh(), {"side_effect": results})
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        assert pr.main([], env=ENV) == 1


def test_review_failure_posts_comment_and_exits_two():
    gh = FakeGh()
    patches = _patched(gh, {"side_effect": rv.ReviewError("모든 리뷰어 실패: codex auth")})
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        assert pr.main([], env=ENV) == 2
    assert any("issues/7/comments" in c and "codex auth" in c for c in gh.joined())


def test_engine_reviews_pr_range_with_codex_only(monkeypatch):
    base = {"roles": {"worker": {}, "reviewers": [
        {"name": "claude", "cli": "claude", "model": "o", "effort": "max"},
        {"name": "codex", "cli": "codex", "model": "g", "effort": "max"}]}, "timeouts_sec": {"review": 1}}
    monkeypatch.setattr(pr.config, "load_config", lambda: base)
    with patch.object(pr.review, "run_review", return_value="merged") as run:
        assert pr.run_engine("main", {"model": "gpt-x", "effort": ""}) == "merged"
    args, kwargs = run.call_args
    assert args[0] == "origin/main" and kwargs["head"] == "HEAD" and kwargs["only"] == ["codex"]
    (codex,) = kwargs["config"]["roles"]["reviewers"]
    assert codex["model"] == "gpt-x" and codex["effort"] == "max"


def test_missing_codex_reviewer_is_clear_error(monkeypatch):
    base = {"roles": {"reviewers": [{"name": "claude", "cli": "claude"}]}}
    monkeypatch.setattr(pr.config, "load_config", lambda: base)
    with pytest.raises(pr.PrReviewError, match="codex"):
        pr.codex_config({})


def test_upsert_comment_creates_when_missing():
    gh = FakeGh(comments=[{"id": 11, "body": "other"}])
    with patch.object(pr, "gh", gh):
        pr.upsert_comment("o/r", 7, pr.MARKER + "\nnew")
    assert any("-X POST repos/o/r/issues/7/comments" in c for c in gh.joined())
    assert not any("PATCH" in c for c in gh.joined())


def test_fork_pr_skips_openai_and_exits_zero():
    gh = FakeGh()
    env = {**ENV, "HEAD_REPO": "fork/r"}
    code, gh, call = _run([], env=env, gh=gh)
    assert code == 0
    call.assert_not_called()
    assert any("issues/7/comments" in c for c in gh.joined())


def test_missing_api_key_fails_without_reviewing():
    # required check가 리뷰 없이 통과로 보이지 않게 실패로 끝낸다.
    env = {k: v for k, v in ENV.items() if k != "OPENAI_API_KEY"}
    code, _, call = _run([], env=env)
    assert code == 1
    call.assert_not_called()


def test_confirmed_p1_exits_one():
    code, _, _ = _run([_finding(1, True)])
    assert code == 1


def test_unconfirmed_p1_exits_zero():
    code, _, _ = _run([_finding(1, False)])
    assert code == 0


def test_critical_without_adr_exits_one_and_disables_auto_merge():
    code, gh, _ = _run([], score=80, pr_body="없음")
    assert code == 1
    assert any("--disable-auto" in c for c in gh.joined())


def test_high_runs_two_extra_focused_reviews():
    code, _, call = _run([], score=50)
    assert call.call_count == 3
    assert code == 0


def test_comment_starts_with_marker_and_orders_sections():
    merged = _merged([_finding(1, True)], score=80)
    plan = pr.plan_actions("critical", merged, "", {})
    body = pr.build_comment(merged, plan, extras={}, stats=(3, 40))
    assert body.startswith(pr.MARKER)
    assert body.index("## 결정 필요") < body.index("## 요약") < body.index("## 지적")
    assert "리뷰어 미설정" in body


def test_dry_run_makes_no_external_calls(tmp_path, capsys):
    result_file = tmp_path / "r.json"
    result_file.write_text(json.dumps(_result([_finding(2)], score=20)), encoding="utf-8")
    with patch.object(pr, "gh") as gh, patch.object(pr, "run_engine") as call, \
         patch.object(pr, "diff_stats", return_value=(1, 5)), \
         patch.object(pr, "load_github_config", return_value=GH_CONFIG):
        code = pr.main(["--dry-run", "--result-file", str(result_file)], env={"BASE_REF": "main"})
    gh.assert_not_called()
    call.assert_not_called()
    out = capsys.readouterr().out
    assert code == 0 and pr.MARKER in out and "risk:medium" in out

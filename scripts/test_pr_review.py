"""pr_review.py 테스트 — gh·git·리뷰 엔진은 전부 mock한다."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import pr_review as pr
import review as rv

GH_CONFIG = {"reviewers": ["alice", "bob"], "seniors": ["sen"], "notify": ["cto"]}
ADR_BODY = "배경 설명 docs/ADR.md 참고 ADR-003"
HEAD = "h" * 40


def _finding(priority=1, confirmed=True, pre_existing=False, line=3):
    return {"title": "bug", "body": "desc", "priority": priority, "confidence": 0.9, "file": "a.py",
            "line_start": line, "line_end": line, "pre_existing": pre_existing,
            "repro": "pytest a", "confirmed": confirmed}


def _merged(findings=None, score=10):
    result = {"findings": findings or [],
              "risk": {"score": score, "axes": {"security": score, "scope": 0, "breaking": 0, "tests": 0,
                                                "migration": 0}},
              "verdict": "patch is correct", "summary": "요약"}
    return rv.merge({"codex": result}, failed={})


class FakeGh:
    """gh 호출을 기록하고 인자 패턴별로 응답을 돌려준다."""

    def __init__(self, comments=None, body=ADR_BODY):
        self.calls = []
        self.comments = comments or []
        self.pr_info = {"body": body, "labels": [{"name": "risk:low"}], "headRefOid": HEAD, "baseRefName": "main"}

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


def _finish(result, body=ADR_BODY, config=GH_CONFIG, lines=None):
    gh = FakeGh(body=body)
    with patch.object(pr, "gh", gh), \
         patch.object(pr.review, "changed_lines", return_value=lines or {"a.py": 10}), \
         patch.object(pr, "load_github_config", return_value=config), \
         patch.object(pr, "remote_for", return_value="fork"), \
         patch.object(pr, "fetch_base"), \
         patch.object(pr, "review_for", return_value=result) as review_for:
        code = pr.finish_pr("o/r", 7)
    return code, gh, review_for


def _status(gh):
    (call,) = [c for c in gh.calls if c[:4] == ["api", "-X", "POST", f"repos/o/r/statuses/{HEAD}"]]
    return dict(arg.split("=", 1) for arg in call[4:] if "=" in arg)


# ---------- 결과 재사용 ----------

def _archive(tmp_path, monkeypatch, base):
    monkeypatch.setattr(rv, "RESULTS_DIR", tmp_path)
    rv.archive_result({**_merged(), "base": base}, HEAD)
    # merge_base(x, HEAD): PR 기준(origin/main)과 첫 push의 기준은 "m", 그 밖(이전 push 끝)은 자기 자신
    monkeypatch.setattr(rv, "merge_base", lambda a, b: "m" if a in ("origin/main", "m") else a)


def test_reuses_review_recorded_at_push(tmp_path, monkeypatch):
    _archive(tmp_path, monkeypatch, base="m")
    with patch.object(rv, "run_review") as run:
        assert pr.review_for(HEAD, "main")["blocking"] == 0
    run.assert_not_called()


def test_record_of_later_push_only_is_not_reused(tmp_path, monkeypatch):
    # 이미 있는 브랜치에 push하면 그 push분만 리뷰된다. PR 전체 위험이 빠지므로 PR 범위를 다시 리뷰한다.
    _archive(tmp_path, monkeypatch, base="p" * 40)
    with patch.object(rv, "run_review", return_value="fresh") as run:
        assert pr.review_for(HEAD, "main") == "fresh"
    assert run.call_args.args[0] == "origin/main"


def test_pr_base_uses_given_remote(tmp_path, monkeypatch):
    # fork에 연 PR은 origin(원본 저장소)이 아니라 그 저장소를 가리키는 원격의 기준 브랜치와 비교한다.
    monkeypatch.setattr(rv, "RESULTS_DIR", tmp_path)
    with patch.object(rv, "run_review", return_value="fresh") as run:
        pr.review_for(HEAD, "main", remote="fork")
    assert run.call_args.args[0] == "fork/main"


@pytest.mark.parametrize("url", ["https://github.com/o/r.git", "git@github.com:o/r.git", "https://github.com/o/r"])
def test_remote_for_finds_remote_pointing_at_repo(url):
    listing = f"origin\thttps://github.com/up/r.git (fetch)\nfork\t{url} (fetch)\n"
    with patch.object(pr.review, "_git", return_value=listing):
        assert pr.remote_for("o/r") == "fork"


def test_remote_for_unknown_repo_is_an_error():
    with patch.object(pr.review, "_git", return_value="origin\thttps://github.com/up/r.git (fetch)\n"):
        with pytest.raises(pr.PrReviewError):
            pr.remote_for("o/r")


def test_reviews_now_only_when_no_record(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "RESULTS_DIR", tmp_path)
    with patch.object(rv, "run_review", return_value="fresh") as run:
        assert pr.review_for(HEAD, "main") == "fresh"
    assert run.call_args.args[0] == "origin/main" and run.call_args.kwargs["head"] == HEAD


# ---------- 상태 체크·코멘트·라벨 ----------

def test_clean_review_sets_success_status_and_label():
    code, gh, _ = _finish(_merged())
    assert code == 0
    assert _status(gh) == {"state": "success", "context": "AI Review", "description": "통과 — risk low (11)"}
    assert any("--add-label risk:low" in c for c in gh.joined())


def test_blocking_finding_sets_failure_status():
    code, gh, _ = _finish(_merged([_finding(1, True)]))
    assert code == 1
    assert _status(gh)["state"] == "failure" and "차단 지적 1건" in _status(gh)["description"]


def test_critical_without_adr_fails_and_disables_auto_merge():
    code, gh, _ = _finish(_merged(score=80), body="없음")
    assert code == 1
    assert _status(gh)["description"] == "critical: RFC/ADR 링크 필요"
    assert any("--disable-auto" in c for c in gh.joined())


def test_stale_risk_label_removed():
    _, gh, _ = _finish(_merged(score=20))
    assert any("--add-label risk:medium" in c and "--remove-label risk:low" in c for c in gh.joined())


def test_upsert_comment_patches_existing_marker():
    gh = FakeGh(comments=[{"id": 11, "body": "other"}, {"id": 22, "body": pr.MARKER + "\nold"}])
    with patch.object(pr, "gh", gh):
        pr.upsert_comment("o/r", 7, pr.MARKER + "\nnew")
    assert any("-X PATCH repos/o/r/issues/comments/22" in c for c in gh.joined())


def test_upsert_comment_creates_when_missing():
    gh = FakeGh(comments=[{"id": 11, "body": "other"}])
    with patch.object(pr, "gh", gh):
        pr.upsert_comment("o/r", 7, pr.MARKER + "\nnew")
    assert any("-X POST repos/o/r/issues/7/comments" in c for c in gh.joined())


def test_comment_orders_decisions_first():
    merged = _merged([_finding(1, True)], score=80)
    plan = pr.plan_actions("critical", merged, "", {})
    body = pr.build_comment(merged, plan, stats=(3, 40))
    assert body.startswith(pr.MARKER)
    assert body.index("## 결정 필요") < body.index("## 요약") < body.index("## 지적")


# ---------- 점수·계획 ----------

def test_scope_score_is_monotonic_between_bounds():
    assert 4 < pr.scope_score(15, 500) < 20
    assert pr.scope_score(10, 500) <= pr.scope_score(20, 500)


@pytest.mark.parametrize("files,lines,expected", [
    (0, 0, 0), (5, 100, 4), (1, 10, 1), (30, 10, 20), (1, 2001, 20), (100, 5000, 20),
])
def test_scope_score_boundaries(files, lines, expected):
    assert pr.scope_score(files, lines) == expected


def test_apply_scope_overrides_model_value_and_recomputes_level():
    out = pr.apply_scope(_merged(score=50), files=0, lines=0)
    assert out["risk"]["axes"]["scope"] == 0
    assert out["risk"]["score"] == sum(out["risk"]["axes"].values())
    assert out["risk"]["level"] == "high"


@pytest.mark.parametrize("level,reviewers,auto_off,notify", [
    ("low", [], False, []),
    ("medium", ["alice"], True, []),
    ("high", ["sen"], True, []),
    ("critical", ["sen"], True, ["cto"]),
])
def test_plan_actions_per_level(level, reviewers, auto_off, notify):
    plan = pr.plan_actions(level, _merged(), ADR_BODY, GH_CONFIG)
    assert plan["label"] == f"risk:{level}"
    assert plan["reviewers"] == reviewers and plan["notify"] == notify
    assert plan["disable_auto_merge"] is auto_off and plan["fail"] is False
    assert plan["auto_merge_candidate"] is (level == "low")


def test_solo_developer_without_reviewers_has_no_decision_noise():
    # 혼자 개발하면 github.* 를 비워 둔다. 그때 "리뷰어 미설정" 같은 결정 항목을 만들지 않는다.
    merged = _merged(score=50)
    plan = pr.plan_actions("high", merged, ADR_BODY, {})
    assert plan["reviewers"] == [] and pr._decisions(merged, plan) == []


def test_unconfirmed_p1_does_not_fail():
    assert pr.plan_actions("low", _merged([_finding(1, False)]), "", GH_CONFIG)["fail"] is False


def test_main_reports_gh_error_as_exit_two(capsys):
    with patch.object(pr, "gh", side_effect=pr.PrReviewError("gh auth")):
        assert pr.main(["--pr", "7", "--repo", "o/r"]) == 2
    assert "gh auth" in capsys.readouterr().err

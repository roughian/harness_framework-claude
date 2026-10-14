"""git_review_gate.py 테스트 — pre-push 훅이 push 범위를 review.py로 리뷰해 차단 여부를 정한다."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import git_review_gate as gate  # noqa: E402

ZERO = "0" * 40
LOCAL = "a" * 40
REMOTE = "b" * 40
TAG_OBJECT = "e" * 40


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.delenv(gate.SKIP_ENV, raising=False)
    monkeypatch.setattr(gate.review, "PASSED_FILE", tmp_path / "passed")


def _fake_run(review_exit=0, merge_bases=None):
    """merge_bases: 기준 브랜치별 merge-base 결과(없으면 실패). 지정하지 않으면 모든 기준에 'c'*40."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-parse"]:
            sha = cmd[2].split("^")[0]
            return MagicMock(returncode=0, stdout=(LOCAL if sha == TAG_OBJECT else sha) + "\n", stderr="")
        if cmd[:2] == ["git", "merge-base"]:
            found = "c" * 40 if merge_bases is None else merge_bases.get(cmd[-1])
            return MagicMock(returncode=0 if found else 1, stdout=(found or "") + "\n", stderr="")
        return MagicMock(returncode=review_exit, stdout="", stderr="")

    return run, calls


def _review_calls(calls):
    return [c for c in calls if "review.py" in " ".join(c)]


def _push(refs, **kwargs):
    run, calls = _fake_run(**kwargs)
    with patch("subprocess.run", side_effect=run):
        code = gate.pre_push(refs)
    return code, _review_calls(calls)


def test_skip_env_bypasses_review(monkeypatch):
    monkeypatch.setenv(gate.SKIP_ENV, "1")
    assert _push(f"refs/heads/x {LOCAL} refs/heads/x {REMOTE}\n") == (0, [])


def test_existing_branch_reviews_remote_to_local():
    code, (review,) = _push(f"refs/heads/x {LOCAL} refs/heads/x {REMOTE}\n")
    assert code == 0
    assert review[review.index("--base") + 1] == REMOTE
    assert review[review.index("--head") + 1] == LOCAL


def test_already_passed_commit_is_not_reviewed_again():
    # execute.py 게이트나 이전 push가 통과시킨 커밋은 다시 리뷰하지 않는다.
    gate.review.record_passed(LOCAL)
    assert _push(f"refs/heads/x {LOCAL} refs/heads/x {REMOTE}\n") == (0, [])


def test_annotated_tag_is_resolved_to_its_commit():
    gate.review.record_passed(LOCAL)
    assert _push(f"refs/tags/v1 {TAG_OBJECT} refs/tags/v1 {ZERO}\n") == (0, [])


def test_new_branch_uses_merge_base_with_main():
    _, (review,) = _push(f"refs/heads/x {LOCAL} refs/heads/x {ZERO}\n")
    assert review[review.index("--base") + 1] == "c" * 40


def test_new_branch_falls_back_to_origin_main():
    # 로컬 main이 없는 클론
    _, (review,) = _push(f"refs/heads/x {LOCAL} refs/heads/x {ZERO}\n", merge_bases={"origin/main": "d" * 40})
    assert review[review.index("--base") + 1] == "d" * 40


def test_new_branch_without_any_base_blocks():
    code, reviews = _push(f"refs/heads/x {LOCAL} refs/heads/x {ZERO}\n", merge_bases={})
    assert code != 0 and reviews == []


def test_branch_delete_is_not_reviewed():
    assert _push(f"(delete) {ZERO} refs/heads/x {REMOTE}\n") == (0, [])


@pytest.mark.parametrize("review_exit", [1, 3])
def test_blocking_or_failed_review_blocks_push(review_exit):
    code, _ = _push(f"refs/heads/x {LOCAL} refs/heads/x {REMOTE}\n", review_exit=review_exit)
    assert code != 0

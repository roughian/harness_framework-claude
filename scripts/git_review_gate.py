#!/usr/bin/env python3
"""
git pre-push 리뷰 게이트 — .githooks/pre-push가 부른다.

push 범위를 review.py로 리뷰하고, 차단 지적이 있거나(exit 3, 규칙은 REVIEW.md) 리뷰어가 모두 실패하면(exit 1) 막는다.
이미 통과한 범위(.review/passed, execute.py 게이트나 이전 push가 기록)는 다시 리뷰하지 않는다.
직전 push 리뷰가 같은 범위에서 막혔다면 전체를 다시 보지 않고 그 지적과 수정분만 검증한다.
로컬 게이트는 실수로 리뷰를 건너뛰는 것을 막는 장치다. 진짜 강제는 required로 건 상태 체크 AI Review가 한다.

Usage:
    python3 scripts/git_review_gate.py pre-push < <git이 주는 ref 목록>
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import review

ROOT = Path(__file__).resolve().parent.parent
REVIEW_SCRIPT = ROOT / "scripts" / "review.py"
# execute.py는 자체 리뷰 게이트가 있어 이 값으로 건너뛴다.
SKIP_ENV = "HARNESS_SKIP_GIT_REVIEW"
# 새 브랜치 push의 리뷰 기준. 로컬 main이 없는 클론도 있어 원격 추적 브랜치까지 본다.
BASE_CANDIDATES = ("main", "origin/main")
ZERO_SHA = "0" * 40
EXIT_BLOCK = 1


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def _commit_of(sha: str) -> str:
    """annotated tag는 가리키는 커밋으로 푼다."""
    r = _git("rev-parse", f"{sha}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else sha


def _push_base(local_sha: str, remote_sha: str) -> str:
    """리뷰 범위의 시작점. 찾지 못하면 빈 문자열."""
    if remote_sha != ZERO_SHA:
        return _commit_of(remote_sha)
    for candidate in BASE_CANDIDATES:
        r = _git("merge-base", local_sha, candidate)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return ""


def _previous_review(base: str, head: str):
    """같은 범위에서 막힌 직전 리뷰 결과 경로. 있으면 검증 리뷰로 돈다."""
    if not review.DEFAULT_OUT.exists():
        return None
    try:
        previous = json.loads(review.DEFAULT_OUT.read_text(encoding="utf-8"))
    except ValueError:
        return None
    is_same_range = previous.get("base") == base and previous.get("head")
    if previous.get("blocking") and is_same_range and review.is_ancestor(previous["head"], head):
        return review.DEFAULT_OUT
    return None


def _review(base: str, head: str) -> int:
    """review.py를 실행하고, 차단해야 하면 사유를 출력한 뒤 EXIT_BLOCK을 돌려준다."""
    cmd = [sys.executable, str(REVIEW_SCRIPT), "--base", base, "--head", head]
    previous = _previous_review(base, head)
    if previous:
        # review.py가 결과를 같은 파일에 다시 쓰므로 이전 결과를 따로 복사해 넘긴다.
        snapshot = previous.with_name("previous.json")
        snapshot.write_text(previous.read_text(encoding="utf-8"), encoding="utf-8")
        cmd += ["--previous", str(snapshot)]
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode == 0:
        return 0
    reason = "차단 지적" if r.returncode == review.EXIT_BLOCKING else "리뷰 실행 실패"
    print(f"\n[review gate] {reason}으로 push를 막았습니다. .review/latest.json을 확인하세요.", file=sys.stderr)
    return EXIT_BLOCK


def pre_push(refs: str) -> int:
    """refs는 git pre-push stdin 형식: '<local ref> <local sha> <remote ref> <remote sha>' 줄 목록."""
    if os.environ.get(SKIP_ENV) == "1":
        return 0
    for line in refs.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[1] == ZERO_SHA:
            continue
        local_sha = _commit_of(parts[1])
        base = _push_base(local_sha, parts[3])
        if not base:
            # 리뷰 범위를 정하지 못했다고 통과시키면 게이트가 열린다.
            print(f"[review gate] {parts[0]}의 리뷰 기준을 찾지 못해 막았습니다"
                  f"({' / '.join(BASE_CANDIDATES)}와의 merge-base 없음). 기준 브랜치를 fetch한 뒤 다시 push하세요.",
                  file=sys.stderr)
            return EXIT_BLOCK
        if base == local_sha or review.is_passed(base, local_sha):
            continue
        if _review(base, local_sha):
            return EXIT_BLOCK
    return 0


def main() -> None:
    hook = sys.argv[1] if len(sys.argv) > 1 else ""
    if hook == "pre-push":
        sys.exit(pre_push(sys.stdin.read()))
    print(f"알 수 없는 훅: {hook!r} (pre-push)", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()

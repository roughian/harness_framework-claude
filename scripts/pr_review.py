#!/usr/bin/env python3
"""
PR 리뷰 마무리 — push 때 이미 끝난 리뷰 결과(.review/results/<커밋>.json)를 PR에 붙인다.

리뷰를 다시 돌리지 않는다. codex는 이 Mac의 OAuth 로그인으로 pre-push에서 이미 돌았기 때문이다.
결과가 없을 때만(리뷰 없이 올라간 커밋) 지금 리뷰한다.

    위험도 라벨(risk:*), 사람 리뷰어 지정(설정했을 때만), auto-merge 끄기, 요약 코멘트 1개,
    GitHub 상태 체크 "AI Review"(success/failure) — 이 체크를 required로 걸면 통과 전에는 merge할 수 없다.

Usage:
    python3 scripts/pr_review.py --pr 12 [--repo owner/name]
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import review  # noqa: E402

MARKER = "<!-- harness-ai-review -->"
STATUS_CONTEXT = "AI Review"
MAX_STATUS_DESCRIPTION = 140
MAX_COMMENT_CHARS = 60_000
SMALL_FILES, SMALL_LINES, SMALL_MAX_SCORE = 5, 100, 4
LARGE_FILES, LARGE_LINES, MAX_SCOPE_SCORE = 30, 2000, 20
ADR_PATTERN = re.compile(r"docs/ADR|ADR-\d+|RFC.*https?://|https?://\S*RFC", re.IGNORECASE | re.DOTALL)
LABEL_COLORS = {"low": "0e8a16", "medium": "fbca04", "high": "d93f0b", "critical": "b60205"}
AXES_ORDER = ("security", "scope", "breaking", "tests", "migration")


class PrReviewError(Exception):
    """PR 정보를 읽거나 쓰지 못함."""


# ---------- 외부 호출(테스트에서 mock) ----------

def gh(args, check=True) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise PrReviewError(f"gh {' '.join(args[:3])} 실패: {proc.stderr.strip()}")
    return proc.stdout


def load_github_config() -> dict:
    try:
        return config.load_config().get("github", {})
    except config.ConfigError:
        return {}


def review_for(head: str, base_ref: str) -> dict:
    """push 때 남긴 이 커밋의 리뷰 결과. 없으면 지금 리뷰한다."""
    result = review.load_result(head)
    if result is not None:
        return result
    print(f"  {head[:7]}의 리뷰 기록이 없어 지금 리뷰합니다.")
    return review.run_review(f"origin/{base_ref}", review.DEFAULT_OUT, head=head)


# ---------- 점수·계획 (순수 함수) ----------

def scope_score(files: int, lines: int) -> int:
    if files <= SMALL_FILES and lines <= SMALL_LINES:
        ratio = max(files / SMALL_FILES, lines / SMALL_LINES)
        return round(SMALL_MAX_SCORE * ratio)
    if files >= LARGE_FILES or lines > LARGE_LINES:
        return MAX_SCOPE_SCORE
    ratio = max((files - SMALL_FILES) / (LARGE_FILES - SMALL_FILES),
                (lines - SMALL_LINES) / (LARGE_LINES - SMALL_LINES))
    return round(SMALL_MAX_SCORE + (MAX_SCOPE_SCORE - SMALL_MAX_SCORE) * max(ratio, 0))


def apply_scope(result: dict, files: int, lines: int) -> dict:
    """scope 축은 모델 대신 PR 전체 diff 통계로 정한다(결정적이고 비용이 없다)."""
    axes = {**result["risk"]["axes"], "scope": scope_score(files, lines)}
    score = sum(axes.values())
    return {**result, "risk": {"score": score, "axes": axes, "level": review.risk_level(score)}}


def has_adr_link(pr_body: str) -> bool:
    return bool(ADR_PATTERN.search(pr_body or ""))


def plan_actions(level: str, merged: dict, pr_body: str, gh_config: dict) -> dict:
    """REVIEW.md 등급별 액션. 사람 리뷰어·알림은 설정했을 때만 한다(혼자 개발하면 비워 둔다)."""
    pool = {"medium": "reviewers", "high": "seniors", "critical": "seniors"}.get(level)
    is_critical = level == "critical"
    plan = {
        "label": f"risk:{level}",
        "reviewers": list(gh_config.get(pool, [])[:1]) if pool else [],
        "notify": list(gh_config.get("notify", [])) if is_critical else [],
        "disable_auto_merge": level != "low",
        "auto_merge_candidate": level == "low",
        "adr_missing": is_critical and not has_adr_link(pr_body),
    }
    plan["fail"] = bool(review.blocking_findings(merged)) or plan["adr_missing"]
    return plan


# ---------- 코멘트·상태 ----------

def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def findings_table(findings: list) -> str:
    if not findings:
        return "지적 없음"
    rows = ["| 등급 | 위치 | 문제 | 재현 | 확인 여부 |", "|---|---|---|---|---|"]
    for f in findings:
        tag = " (기존)" if f["pre_existing"] else ""
        status = "확인됨" if f.get("confirmed", True) else "미확인"
        rows.append(f"| P{f['priority']}{tag} | {_cell(f['file'])}:{f['line_start']} | "
                    f"{_cell(f['title'])} | {_cell(f.get('repro') or '-')} | {status} |")
    return "\n".join(rows)


def _decisions(merged: dict, plan: dict) -> list:
    items = [f"차단 지적 {merged['blocking']}건: 고친 뒤 다시 push하면 고친 부분만 검증한다."] if merged["blocking"] else []
    if plan["adr_missing"]:
        items.append("critical 변경: PR 본문에 RFC 또는 ADR 링크(docs/ADR, ADR-숫자, RFC+URL)를 넣고 이 명령을 다시 실행하라.")
    if plan["notify"]:
        items.append("알림: " + " ".join(f"@{u}" for u in plan["notify"]))
    return items


def build_comment(merged: dict, plan: dict, stats: tuple) -> str:
    risk = merged["risk"]
    axes = " / ".join(f"{k} {risk['axes'].get(k, 0)}" for k in AXES_ORDER)
    out = [MARKER, "# AI 리뷰", "", "## 결정 필요"]
    out += [f"- {d}" for d in _decisions(merged, plan)] or ["- 없음"]
    out += ["", "## 요약", f"- verdict: {merged['verdict']}", f"- risk: {risk['level']} ({risk['score']}) — {axes}",
            f"- 조치: 라벨 `{plan['label']}`" + (f", 리뷰어 {', '.join(plan['reviewers'])}" if plan["reviewers"] else "")
            + (", auto-merge 끔" if plan["disable_auto_merge"] else ""),
            f"- 변경 규모: 파일 {stats[0]}개, {stats[1]}줄"]
    if plan["auto_merge_candidate"]:
        out.append("- 1명 승인 시 auto-merge 후보")
    out += ["", *merged["summaries"].values(), "", "## 지적", findings_table(merged["findings"])]
    return "\n".join(out)[:MAX_COMMENT_CHARS]


def status_description(merged: dict, plan: dict) -> str:
    if merged["blocking"]:
        text = f"차단 지적 {merged['blocking']}건"
    elif plan["adr_missing"]:
        text = "critical: RFC/ADR 링크 필요"
    else:
        text = f"통과 — risk {merged['risk']['level']} ({merged['risk']['score']})"
    return text[:MAX_STATUS_DESCRIPTION]


def upsert_comment(repo: str, number: int, body: str):
    # --paginate는 페이지마다 배열을 따로 출력하므로 jq로 id만 한 줄씩 뽑는다.
    ids = gh(["api", "--paginate", f"repos/{repo}/issues/{number}/comments",
              "--jq", f'.[] | select(.body | startswith("{MARKER}")) | .id']).split()
    if ids:
        gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{ids[0]}", "-f", f"body={body}"])
    else:
        gh(["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"])


def set_status(repo: str, head: str, merged: dict, plan: dict):
    """required check로 걸 수 있는 커밋 상태. 실패면 merge 버튼이 잠긴다."""
    gh(["api", "-X", "POST", f"repos/{repo}/statuses/{head}",
        "-f", f"state={'failure' if plan['fail'] else 'success'}",
        "-f", f"context={STATUS_CONTEXT}", "-f", f"description={status_description(merged, plan)}"])


def apply_actions(repo: str, number: int, info: dict, plan: dict):
    label = plan["label"]
    gh(["label", "create", label, "--color", LABEL_COLORS[label.split(":", 1)[1]], "--force", "-R", repo], check=False)
    stale = [lb["name"] for lb in info.get("labels", []) if lb["name"].startswith("risk:") and lb["name"] != label]
    args = ["pr", "edit", str(number), "-R", repo, "--add-label", label]
    for name in stale:
        args += ["--remove-label", name]
    gh(args, check=False)
    for user in plan["reviewers"]:
        gh(["pr", "edit", str(number), "-R", repo, "--add-reviewer", user], check=False)
    if plan["disable_auto_merge"]:
        gh(["pr", "merge", str(number), "-R", repo, "--disable-auto"], check=False)


# ---------- 실행 ----------

def finish_pr(repo: str, number: int) -> int:
    info = json.loads(gh(["pr", "view", str(number), "-R", repo, "--json", "body,labels,headRefOid,baseRefName"]))
    head, base = info["headRefOid"], info["baseRefName"]
    lines = review.changed_lines(f"origin/{base}...{head}")
    stats = (len(lines), sum(lines.values()))
    merged = apply_scope(review_for(head, base), *stats)
    plan = plan_actions(merged["risk"]["level"], merged, info.get("body") or "", load_github_config())
    apply_actions(repo, number, info, plan)
    upsert_comment(repo, number, build_comment(merged, plan, stats))
    set_status(repo, head, merged, plan)
    print(f"  PR #{number}: {status_description(merged, plan)} (라벨 {plan['label']})")
    return 1 if plan["fail"] else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="push 때 끝난 리뷰 결과를 PR에 붙인다")
    parser.add_argument("--pr", type=int, required=True, help="PR 번호")
    parser.add_argument("--repo", help="owner/name (기본: gh 기본 저장소)")
    args = parser.parse_args(argv)
    try:
        repo = args.repo or gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).strip()
        return finish_pr(repo, args.pr)
    except (PrReviewError, review.ReviewError, config.ConfigError) as err:
        print(f"PR 리뷰 실패: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

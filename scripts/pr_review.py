"""GitHub PR 자동 AI 리뷰 — 로컬과 같은 리뷰 엔진(review.py, codex 리뷰어)으로 리뷰하고
위험도 등급별로 라벨·리뷰어·코멘트를 처리한다.

표준 라이브러리, `gh` CLI, `codex` CLI만 쓴다. 로컬 검증은 `--dry-run --result-file <json>`.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import review  # noqa: E402

MARKER = "<!-- harness-ai-review -->"
OUT_FILE = review.REVIEW_DIR / "pr.json"
MAX_COMMENT_CHARS = 60_000
SMALL_FILES, SMALL_LINES, SMALL_MAX_SCORE = 5, 100, 4
LARGE_FILES, LARGE_LINES, MAX_SCOPE_SCORE = 30, 2000, 20
AUDIT_SEVERITIES = ("high", "critical")
EXTRA_FOCUS = ("security", "architecture")
ADR_PATTERN = re.compile(r"docs/ADR|ADR-\d+|RFC.*https?://|https?://\S*RFC", re.IGNORECASE | re.DOTALL)
LABEL_COLORS = {"low": "0e8a16", "medium": "fbca04", "high": "d93f0b", "critical": "b60205"}
AXES_ORDER = ("security", "scope", "breaking", "tests", "migration")
FOCUS_HINT = {
    "security": "보안(인젝션, 인증·인가, 비밀값, 의존성 취약점)만 집중해서 본다.",
    "architecture": "아키텍처(docs/ARCHITECTURE.md, docs/ADR.md 위반, 계층·의존 방향, breaking change)만 집중해서 본다.",
}


class PrReviewError(Exception):
    """리뷰를 진행할 수 없는 설정·응답 오류."""


# ---------- 외부 호출(테스트에서 mock) ----------

def gh(args, check=True) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise PrReviewError(f"gh {' '.join(args[:3])} 실패: {proc.stderr.strip()}")
    return proc.stdout


def diff_stats(base_ref: str) -> tuple:
    """(변경 파일 수, 변경 줄 수). 바이너리 파일은 줄 수 0으로 센다."""
    lines = review.changed_lines(f"origin/{base_ref}...HEAD")
    return len(lines), sum(lines.values())


def npm_audit_summary() -> str:
    """package-lock.json이 있으면 high/critical 취약점 개수 요약. 실패해도 빈 문자열."""
    if not (review.ROOT / "package-lock.json").exists():
        return ""
    try:
        proc = subprocess.run(["npm", "audit", "--json"], capture_output=True, text=True,
                              cwd=review.ROOT, timeout=120)
        counts = json.loads(proc.stdout)["metadata"]["vulnerabilities"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return ""
    return "npm audit: " + ", ".join(f"{s} {counts.get(s, 0)}건" for s in AUDIT_SEVERITIES)


def load_github_config() -> dict:
    try:
        return config.load_config().get("github", {})
    except config.ConfigError:
        return {}


def codex_config(gh_config: dict) -> tuple:
    """(리뷰 설정, codex 리뷰어 이름). github.model·effort가 있으면 codex 리뷰어 설정을 덮어쓴다."""
    base = config.load_config()
    codex = next((r for r in base["roles"]["reviewers"] if r["cli"] == "codex"), None)
    if codex is None:
        raise PrReviewError("harness.config.json roles.reviewers에 codex 리뷰어가 없다.")
    overrides = {k: gh_config[k] for k in ("model", "effort") if gh_config.get(k)}
    return {**base, "roles": {**base["roles"], "reviewers": [{**codex, **overrides}]}}, codex["name"]


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
    """scope 축은 모델 대신 diff 통계로 정한다(결정적이고 비용이 없다)."""
    axes = {**result["risk"]["axes"], "scope": scope_score(files, lines)}
    score = sum(axes.values())
    return {**result, "risk": {"score": score, "axes": axes, "level": review.risk_level(score)}}


def has_adr_link(pr_body: str) -> bool:
    return bool(ADR_PATTERN.search(pr_body or ""))


def _first(names) -> list:
    return list(names[:1])


def plan_actions(level: str, merged: dict, pr_body: str, gh_config: dict) -> dict:
    plan = {"label": f"risk:{level}", "reviewers": [], "reviewer_missing": False,
            "disable_auto_merge": level != "low", "extra_reviews": [], "notify": [],
            "auto_merge_candidate": level == "low", "adr_missing": False}
    # critical은 high의 조치(시니어·집중 리뷰)에 알림과 RFC/ADR 요구를 더한다.
    pool = {"medium": "reviewers", "high": "seniors", "critical": "seniors"}.get(level)
    if pool:
        plan["reviewers"] = _first(gh_config.get(pool, []))
        plan["reviewer_missing"] = not plan["reviewers"]
    if level in ("high", "critical"):
        plan["extra_reviews"] = list(EXTRA_FOCUS)
    if level == "critical":
        plan["notify"] = list(gh_config.get("notify", []))
        plan["reviewer_missing"] = plan["reviewer_missing"] or not plan["notify"]
        plan["adr_missing"] = not has_adr_link(pr_body)
    plan["fail"] = bool(review.blocking_findings(merged)) or plan["adr_missing"]
    return plan


def apply_extras(merged: dict, plan: dict, extras: dict):
    """집중 리뷰의 차단 지적도 차단 건수와 체크 실패에 반영한다."""
    extra_blocking = sum(len(review.blocking_findings(r)) for r in extras.values())
    merged["blocking"] += extra_blocking
    plan["fail"] = plan["fail"] or extra_blocking > 0


# ---------- 코멘트 ----------

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
    items = [f"차단 지적 {merged['blocking']}건: 차단 지적(REVIEW.md 차단 규칙)을 수정해야 머지할 수 있다."] if merged["blocking"] else []
    if plan["adr_missing"]:
        items.append("critical 변경: PR 본문에 RFC 또는 ADR 링크(docs/ADR, ADR-숫자, RFC+URL)가 필요하다.")
    if plan["reviewer_missing"]:
        items.append("리뷰어 미설정: harness.config.json의 github.reviewers/seniors/notify를 채워라.")
    if plan["notify"]:
        items.append("알림: " + " ".join(f"@{u}" for u in plan["notify"]))
    return items


def _risk_lines(merged: dict, plan: dict) -> list:
    risk = merged["risk"]
    axes = " / ".join(f"{k} {risk['axes'][k]}" for k in AXES_ORDER)
    lines = [f"- verdict: {merged['verdict']}",
             f"- risk: {risk['level']} ({risk['score']}) — {axes}",
             f"- 조치: 라벨 `{plan['label']}`" + (f", 리뷰어 {', '.join(plan['reviewers'])}" if plan["reviewers"] else "")
             + (", auto-merge 끔" if plan["disable_auto_merge"] else "")]
    if plan["auto_merge_candidate"]:
        lines.append("- 1명 승인 시 auto-merge 후보")
    return lines


def build_comment(merged: dict, plan: dict, extras: dict, stats: tuple) -> str:
    out = [MARKER, "# AI 리뷰", "", "## 결정 필요"]
    decisions = _decisions(merged, plan)
    out += [f"- {d}" for d in decisions] or ["- 없음"]
    out += ["", "## 요약", *_risk_lines(merged, plan), f"- 변경 규모: 파일 {stats[0]}개, {stats[1]}줄"]
    out += ["", *merged["summaries"].values(), "", "## 지적", findings_table(merged["findings"])]
    for focus, result in extras.items():
        out += ["", f"## {focus} 집중 리뷰", *result["summaries"].values(), "", findings_table(result["findings"])]
    return "\n".join(out)[:MAX_COMMENT_CHARS]


def skip_comment(reason: str) -> str:
    return f"{MARKER}\n# AI 리뷰\n\n## 결정 필요\n- 자동 리뷰를 건너뛰었다: {reason}"


def upsert_comment(repo: str, number: int, body: str):
    # --paginate는 페이지마다 배열을 따로 출력하므로 jq로 id만 한 줄씩 뽑는다.
    ids = gh(["api", "--paginate", f"repos/{repo}/issues/{number}/comments",
              "--jq", f'.[] | select(.body | startswith("{MARKER}")) | .id']).split()
    if ids:
        gh(["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{ids[0]}", "-f", f"body={body}"])
    else:
        gh(["api", "-X", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"])


# ---------- 액션 실행 ----------

def sync_labels(repo: str, number: int, info: dict, plan: dict):
    label = plan["label"]
    color = LABEL_COLORS[label.split(":", 1)[1]]
    gh(["label", "create", label, "--color", color, "--force", "-R", repo], check=False)
    stale = [lb["name"] for lb in info.get("labels", []) if lb["name"].startswith("risk:") and lb["name"] != label]
    args = ["pr", "edit", str(number), "-R", repo, "--add-label", label]
    for name in stale:
        args += ["--remove-label", name]
    gh(args, check=False)


def apply_actions(repo: str, number: int, info: dict, plan: dict):
    sync_labels(repo, number, info, plan)
    for user in plan["reviewers"]:
        gh(["pr", "edit", str(number), "-R", repo, "--add-reviewer", user], check=False)
    if plan["disable_auto_merge"]:
        gh(["pr", "merge", str(number), "-R", repo, "--disable-auto"], check=False)


# ---------- 실행 ----------

def _skip_reason(env: dict):
    if env.get("HEAD_REPO") != env.get("REPO"):
        return "포크 PR은 시크릿에 접근할 수 없어 리뷰하지 않는다."
    if not env.get("OPENAI_API_KEY"):
        return "OPENAI_API_KEY가 설정되지 않았다."
    return None


def run_engine(base: str, gh_config: dict, *, focus: str = "", context: str = "") -> dict:
    """로컬과 같은 review.py 엔진으로 PR 범위(origin/<base>...HEAD)를 codex 리뷰어 하나로 리뷰한다."""
    review_config, name = codex_config(gh_config)
    return review.run_review(f"origin/{base}", OUT_FILE, config=review_config, only=[name], head="HEAD",
                             focus=focus, context=context)


def _model_result(args, gh_config: dict, base: str, audit: str) -> dict:
    if args.dry_run:
        result = review._validate(json.loads(Path(args.result_file).read_text(encoding="utf-8")))
        return review.merge({"codex": result}, failed={})
    return run_engine(base, gh_config, context=audit)


def _review(args, env: dict, repo: str, number: int) -> int:
    base, gh_config = env["BASE_REF"], load_github_config()
    info = ({"body": env.get("PR_BODY", ""), "labels": []} if args.dry_run
            else json.loads(gh(["pr", "view", str(number), "-R", repo, "--json", "body,isDraft,labels"])))
    stats = diff_stats(base)
    audit = npm_audit_summary()
    merged = apply_scope(_model_result(args, gh_config, base, audit), *stats)
    plan = plan_actions(merged["risk"]["level"], merged, info.get("body") or "", gh_config)
    extras = {} if args.dry_run else {
        focus: run_engine(base, gh_config, focus=FOCUS_HINT[focus], context=audit) for focus in plan["extra_reviews"]}
    apply_extras(merged, plan, extras)
    body = build_comment(merged, plan, extras, stats)
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2), body, sep="\n\n")
    else:
        apply_actions(repo, number, info, plan)
        upsert_comment(repo, number, body)
    return 1 if plan["fail"] else 0


def main(argv=None, env=None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="PR 자동 AI 리뷰")
    parser.add_argument("--dry-run", action="store_true", help="gh·리뷰어를 호출하지 않고 계획만 출력")
    parser.add_argument("--result-file", help="dry-run에서 쓸 모델 결과 JSON")
    args = parser.parse_args(argv)
    if args.dry_run and not args.result_file:
        parser.error("--dry-run에는 --result-file이 필요하다")
    repo, number = env.get("REPO", ""), int(env.get("PR_NUMBER", "0") or 0)
    reason = None if args.dry_run else _skip_reason(env)
    if reason:
        try:
            upsert_comment(repo, number, skip_comment(reason))
        except PrReviewError as err:
            # 포크 PR의 GITHUB_TOKEN은 읽기 전용이라 코멘트를 못 쓸 수 있다. 건너뛰기는 실패가 아니다.
            print(f"건너뜀({reason}), 코멘트 실패: {err}", file=sys.stderr)
        return 0
    try:
        return _review(args, env, repo, number)
    except (PrReviewError, review.ReviewError, config.ConfigError) as err:
        print(f"PR 리뷰 실패: {err}", file=sys.stderr)
        if not args.dry_run:
            _post_quietly(repo, number, f"{MARKER}\n# AI 리뷰\n\n## 결정 필요\n- 자동 리뷰 실패: {err}")
        return 2


def _post_quietly(repo: str, number: int, body: str):
    try:
        upsert_comment(repo, number, body)
    except PrReviewError as err:
        print(f"코멘트 실패: {err}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())

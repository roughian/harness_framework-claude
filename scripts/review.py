#!/usr/bin/env python3
"""
교차 코드 리뷰 — harness.config.json의 리뷰어(Claude, Codex …)를 병렬로 돌려 결과를 병합한다.

herdr 안에서 실행하면 리뷰어마다 pane을 띄워 진행 상황을 보여 주고,
아니면 로컬 subprocess로 병렬 실행한다.

Usage:
    python3 scripts/review.py [--base main] [--out PATH] [--reviewers claude,codex] [--no-herdr]

Exit code: 0 차단 이슈 없음 / 3 P0·P1 있음 / 1 리뷰어 전원 실패
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import config as cfg

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "scripts" / "review_schema.json"
DEFAULT_WORK_DIR = ROOT / ".review" / "cli"
DEFAULT_OUT = ROOT / ".review" / "latest.json"

DONE_MARKER = "HARNESS_REVIEW_DONE"
BLOCKING_MAX_PRIORITY = 1
MAX_NITS = 5
NIT_PRIORITY = 3
RISK_LEVELS = ((15, "low"), (35, "medium"), (60, "high"), (100, "critical"))
CLAUDE_READ_ONLY_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git status:*),Bash(git show:*)"
REQUIRED_KEYS = ("findings", "risk", "verdict", "summary")
FINDING_KEYS = ("title", "body", "priority", "confidence", "file", "line_start", "line_end", "pre_existing")

EXIT_BLOCKING = 3
EXIT_ALL_FAILED = 1


class ReviewError(Exception):
    """모든 리뷰어가 실패해 결과를 만들 수 없음."""


def risk_level(score: int) -> str:
    for upper, level in RISK_LEVELS:
        if score <= upper:
            return level
    return RISK_LEVELS[-1][1]


def build_prompt(base: str) -> str:
    return (
        "너는 다른 엔지니어가 만든 코드 변경을 리뷰한다. 코드를 수정하지 마라.\n\n"
        "1. 먼저 다음 문서를 읽어라: REVIEW.md(리뷰 기준), CLAUDE.md, docs/LESSONS.md, "
        "docs/ARCHITECTURE.md, docs/ADR.md\n"
        f"2. `git diff {base}`와 `git status --short`로 변경을 확인하라(커밋 안 된 변경 포함). "
        "필요하면 변경 파일 전체를 읽어 맥락을 확인하라.\n"
        "3. REVIEW.md의 심각도(P0-P3), 체크리스트, Risk Score 기준을 그대로 적용하라.\n"
        "4. 이 diff가 새로 만든 문제만 지적하라. 기존 문제는 pre_existing=true로 표시하라.\n"
        "5. 결과는 주어진 JSON 스키마 형식 하나로만 출력하라.\n"
    )


def reviewer_command(reviewer: dict, prompt_file: Path, raw_file: Path) -> str:
    """리뷰어 하나를 읽기 전용으로 실행하는 셸 명령. herdr pane과 로컬 실행이 같은 명령을 쓴다."""
    q = shlex.quote
    if reviewer["cli"] == "claude":
        args = " ".join(q(a) for a in cfg.claude_args(reviewer))
        return (
            f"claude -p {args} --output-format json "
            f"--json-schema \"$(cat {q(str(SCHEMA_FILE))})\" "
            f"--allowedTools {q(CLAUDE_READ_ONLY_TOOLS)} "
            f"< {q(str(prompt_file))} > {q(str(raw_file))}"
        )
    args = " ".join(q(a) for a in cfg.codex_args(reviewer))
    return (
        f"codex exec --sandbox read-only {args} "
        f"--output-schema {q(str(SCHEMA_FILE))} -o {q(str(raw_file))} - "
        f"< {q(str(prompt_file))}"
    )


def _extract_json(text: str) -> dict:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    return json.loads(fenced.group(1) if fenced else text)


def _validate(result: dict) -> dict:
    missing = [k for k in REQUIRED_KEYS if k not in result]
    if missing:
        raise ValueError(f"리뷰 결과에 필수 키 누락: {missing}")
    if not isinstance(result["risk"].get("score"), int):
        raise ValueError("risk.score가 정수가 아님")
    for f in result["findings"]:
        absent = [k for k in FINDING_KEYS if k not in f]
        if absent:
            raise ValueError(f"finding 필수 키 누락: {absent}")
        if f["priority"] not in range(NIT_PRIORITY + 1):
            raise ValueError(f"잘못된 priority: {f['priority']}")
    return result


def parse_raw(cli: str, text: str) -> dict:
    data = json.loads(text) if cli == "claude" else _extract_json(text)
    if cli == "claude":
        data = data.get("structured_output") or _extract_json(data.get("result", ""))
    return _validate(data)


def use_herdr(allow: bool) -> bool:
    return allow and os.environ.get("HERDR_ENV") == "1" and shutil.which("herdr") is not None


def _herdr(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["herdr", *args], capture_output=True, text=True)


def _start_in_pane(name: str, command: str) -> str:
    split = _herdr("pane", "split", "--current", "--direction", "right", "--no-focus", "--cwd", str(ROOT))
    if split.returncode != 0:
        raise RuntimeError(f"pane split 실패: {split.stderr.strip()}")
    pane = json.loads(split.stdout)["result"]["pane"]["pane_id"]
    # 명령 텍스트 자체에는 "MARKER:name"이 나타나지 않게 printf로 조립한다(에코 오탐 방지).
    _herdr("pane", "run", pane, f"{command}; printf '%s:%s\\n' {DONE_MARKER} {name}")
    return pane


def run_in_herdr(commands: dict, timeout_sec: int) -> dict:
    """리뷰어마다 pane을 띄워 모두 먼저 시작한 뒤 완료 마커를 기다린다. 반환: {name: 에러}"""
    panes, errors = {}, {}
    try:
        for name, command in commands.items():
            try:
                panes[name] = _start_in_pane(name, command)
            except RuntimeError as err:
                errors[name] = str(err)
        for name, pane in panes.items():
            r = _herdr("pane", "wait-output", pane, "--match", f"{DONE_MARKER}:{name}",
                       "--source", "recent", "--timeout", str(timeout_sec * 1000))
            if r.returncode != 0:
                errors[name] = f"wait-output 실패: {r.stderr.strip()}"
    finally:
        for pane in panes.values():
            _herdr("pane", "close", pane)
    return errors


def run_local(commands: dict, timeout_sec: int) -> dict:
    def _run(command):
        return subprocess.run(command, shell=True, cwd=ROOT, capture_output=True,
                              text=True, timeout=timeout_sec)

    errors = {}
    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        futures = {name: pool.submit(_run, command) for name, command in commands.items()}
        for name, future in futures.items():
            try:
                r = future.result()
                if r.returncode != 0:
                    errors[name] = f"exit {r.returncode}: {r.stderr.strip()[-300:]}"
            except subprocess.TimeoutExpired:
                errors[name] = f"timeout {timeout_sec}s"
    return errors


def _overlaps(a: dict, b: dict) -> bool:
    return a["file"] == b["file"] and a["line_start"] <= b["line_end"] and b["line_start"] <= a["line_end"]


def _merge_findings(results: dict) -> list:
    merged = []
    for name, result in results.items():
        for finding in result["findings"]:
            match = next((m for m in merged if _overlaps(m, finding)), None)
            if match is None:
                merged.append({**finding, "found_by": [name]})
                continue
            match["priority"] = min(match["priority"], finding["priority"])
            # 한쪽이라도 신규 문제로 봤으면 신규로 둔다(차단 판정에서 빠지지 않게).
            match["pre_existing"] = match["pre_existing"] and finding["pre_existing"]
            if name not in match["found_by"]:
                match["found_by"].append(name)
    merged.sort(key=lambda f: (f["priority"], f["file"], f["line_start"]))
    nits = [f for f in merged if f["priority"] == NIT_PRIORITY]
    return [f for f in merged if f["priority"] != NIT_PRIORITY] + nits[:MAX_NITS]


def _is_blocking(finding: dict) -> bool:
    return finding["priority"] <= BLOCKING_MAX_PRIORITY and not finding["pre_existing"]


def merge(results: dict, failed: dict) -> dict:
    riskiest = max(results.values(), key=lambda r: r["risk"]["score"])
    findings = _merge_findings(results)
    is_incorrect = any(r["verdict"] == "patch is incorrect" for r in results.values())
    return {
        "reviewers": list(results),
        "failed": failed,
        "verdict": "patch is incorrect" if is_incorrect else "patch is correct",
        "risk": {**riskiest["risk"], "level": risk_level(riskiest["risk"]["score"])},
        "blocking": sum(1 for f in findings if _is_blocking(f)),
        "findings": findings,
        "summaries": {name: r["summary"] for name, r in results.items()},
    }


def _collect(raw_files: dict, reviewers: dict, runner_errors: dict) -> tuple:
    results, failed = {}, {}
    for name, raw in raw_files.items():
        if name in runner_errors:
            failed[name] = runner_errors[name]
            continue
        try:
            results[name] = parse_raw(reviewers[name]["cli"], raw.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, AttributeError) as err:
            failed[name] = f"결과 파싱 실패: {err}"
    return results, failed


def run_review(base: str, out: Path, *, config: Optional[dict] = None, work_dir: Optional[Path] = None,
               allow_herdr: bool = True, only: Optional[list] = None) -> dict:
    config = config or cfg.load_config()
    work_dir = work_dir or DEFAULT_WORK_DIR
    work_dir.mkdir(parents=True, exist_ok=True)
    reviewers = {r["name"]: r for r in config["roles"]["reviewers"] if not only or r["name"] in only}
    unknown = set(only or []) - set(reviewers)
    if unknown or not reviewers:
        raise ReviewError(f"설정에 없는 리뷰어: {sorted(unknown)}")

    prompt_file = work_dir / "review-prompt.md"
    prompt_file.write_text(build_prompt(base), encoding="utf-8")
    raw_files = {name: work_dir / f"review-{name}.raw.json" for name in reviewers}
    for raw in raw_files.values():
        raw.unlink(missing_ok=True)
    commands = {name: reviewer_command(r, prompt_file, raw_files[name]) for name, r in reviewers.items()}

    runner = run_in_herdr if use_herdr(allow_herdr) else run_local
    runner_errors = runner(commands, config["timeouts_sec"]["review"])
    results, failed = _collect(raw_files, reviewers, runner_errors)
    if not results:
        raise ReviewError(f"모든 리뷰어 실패: {failed}")

    merged = {"base": base, **merge(results, failed)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    return merged


def blocking_findings(merged: dict) -> list:
    return [f for f in merged["findings"] if _is_blocking(f)]


def build_fix_prompt(merged: dict, project: str) -> str:
    items = "\n".join(
        f"- [P{f['priority']}] {f['file']}:{f['line_start']} {f['title']} — {f['body']}"
        for f in blocking_findings(merged)
    )
    return (
        f"당신은 {project} 프로젝트의 개발자입니다. 코드 리뷰에서 아래 차단 이슈(P0/P1)가 나왔습니다.\n\n"
        f"## 리뷰 지적사항\n\n{items}\n\n"
        "## 작업 규칙\n\n"
        "1. 지적된 문제만 고쳐라. 다른 리팩터링이나 기능 추가를 하지 마라.\n"
        "2. 각 수정에는 문제를 재현하는 테스트를 먼저 추가하라. 기존 테스트를 깨뜨리지 마라.\n"
        "3. 각 지적의 원인을 규칙 한 줄로 docs/LESSONS.md에 추가하라. "
        "형식: `- [YYYY-MM-DD] 규칙. 원인: … (출처: review)`. 같은 규칙이 있으면 그 줄을 갱신하라.\n"
        "4. 커밋하지 마라. 실행기가 커밋한다.\n"
    )


def format_summary(merged: dict) -> str:
    risk = merged["risk"]
    lines = [
        f"## 리뷰 결과 — {merged['verdict']} | risk {risk['level']} ({risk['score']}) | 차단 {merged['blocking']}건",
        f"리뷰어: {', '.join(merged['reviewers'])}" + (f" | 실패: {merged['failed']}" if merged["failed"] else ""),
        "",
    ]
    for f in merged["findings"]:
        tag = " (기존)" if f["pre_existing"] else ""
        lines.append(f"- P{f['priority']}{tag} {f['file']}:{f['line_start']} {f['title']} [{'+'.join(f['found_by'])}]")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Harness 교차 코드 리뷰")
    parser.add_argument("--base", default="main", help="비교 기준 브랜치/커밋 (기본 main)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="병합 결과 JSON 경로")
    parser.add_argument("--reviewers", help="쉼표로 구분한 리뷰어 이름 (기본: config 전체)")
    parser.add_argument("--no-herdr", action="store_true", help="herdr pane 대신 로컬 병렬 실행")
    args = parser.parse_args()

    only = args.reviewers.split(",") if args.reviewers else None
    try:
        merged = run_review(args.base, args.out, allow_herdr=not args.no_herdr, only=only)
    except (ReviewError, cfg.ConfigError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(EXIT_ALL_FAILED)
    print(format_summary(merged))
    print(f"\n결과: {args.out}")
    sys.exit(EXIT_BLOCKING if merged["blocking"] else 0)


if __name__ == "__main__":
    main()

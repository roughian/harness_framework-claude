#!/usr/bin/env python3
"""
교차 코드 리뷰 — harness.config.json의 리뷰어(Claude, Codex …)를 병렬 subprocess로 돌려 결과를 병합한다.

    첫 리뷰: 변경이 작고 민감 경로가 아니면 리뷰어 1명, 아니면 전원(교차)
    검증 리뷰(previous 지정): 이전 차단 지적 + 그 뒤의 수정 diff만 본다. 지적한 리뷰어만 다시 돈다.

실행 중 상태는 .review/status.json에, 통과한 커밋은 .review/passed에 남긴다(리캡·pre-push 캐시용).

Usage:
    python3 scripts/review.py [--base main] [--out PATH] [--reviewers claude,codex] [--head SHA]

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
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional

import config as cfg

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "scripts" / "review_schema.json"
REVIEW_DIR = ROOT / ".review"
DEFAULT_OUT = REVIEW_DIR / "latest.json"
STATUS_FILE = REVIEW_DIR / "status.json"
PASSED_FILE = REVIEW_DIR / "passed"
# 커밋별 리뷰 결과. PR 단계(pr_review.py)가 다시 리뷰하지 않고 이 결과를 쓴다.
RESULTS_DIR = REVIEW_DIR / "results"
LESSONS_FILE = ROOT / "docs" / "LESSONS.md"
LESSON_KEYS = re.compile(r"\[키:\s*([^\]]+)\]\s*$")
MAX_INTENT_COMMITS = 20

BLOCKING_MAX_PRIORITY = 1
# 확인되지 않은 P1도 이만큼의 리뷰어가 같은 등급으로 지적하면 차단한다.
MIN_AGREEING_REVIEWERS = 2
MAX_NITS = 5
# 검증 리뷰 상한. 넘어도 남은 차단 지적은 사람이 정한다(execute.py·pre-push 공통).
MAX_VERIFY_ROUNDS = 2
NIT_PRIORITY = 3
RISK_LEVELS = ((15, "low"), (35, "medium"), (60, "high"), (100, "critical"))
CLAUDE_READ_ONLY_TOOLS = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git status:*),Bash(git show:*)"
REQUIRED_KEYS = ("findings", "risk", "verdict", "summary")
FINDING_KEYS = ("title", "body", "priority", "confidence", "file", "line_start", "line_end", "pre_existing",
                "repro", "confirmed")
# 리뷰어 수 판정에서 빼는 생성물·lock 파일
GENERATED_PATTERNS = ("*.lock", "*-lock.json", "*-lock.yaml", "dist/*", "build/*", "*.min.js", "*.snap")
DEFAULT_SINGLE_REVIEWER_MAX_LINES = 100
DEFAULT_FULL_REVIEW_PATHS = ("auth", "crypto", "security", "migration", ".claude/", ".githooks/", "scripts/hooks/")

EXIT_BLOCKING = 3
EXIT_ALL_FAILED = 1


class ReviewError(Exception):
    """모든 리뷰어가 실패해 결과를 만들 수 없음."""


def risk_level(score: int) -> str:
    for upper, level in RISK_LEVELS:
        if score <= upper:
            return level
    return RISK_LEVELS[-1][1]


# ---------- git ----------

def _git(*args) -> str:
    # diff 본문에는 UTF-8이 아닌 파일이 섞일 수 있다. 깨진 바이트는 대체 문자로 읽는다.
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise ReviewError(f"git {' '.join(args[:2])} 실패: {r.stderr.strip()}")
    return r.stdout


def resolve_commit(ref: str) -> str:
    """ref(브랜치·태그·SHA)가 가리키는 커밋 SHA. annotated tag도 커밋으로 푼다."""
    return _git("rev-parse", f"{ref}^{{commit}}").strip()


def merge_base(a: str, b: str) -> str:
    return _git("merge-base", a, b).strip()


def is_worktree_clean() -> bool:
    return not _git("status", "--porcelain").strip()


def commit_subjects(base: str, head: Optional[str]) -> list:
    """리뷰 범위의 커밋 제목(최신순). 리뷰어에게 변경 의도로 준다."""
    out = _git("log", f"-n{MAX_INTENT_COMMITS}", "--format=%s", f"{base}..{head or 'HEAD'}")
    return [line for line in out.splitlines() if line.strip()]


def review_range(base: str, head: Optional[str]) -> str:
    """리뷰 대상 diff 범위. head가 없으면 작업 트리까지."""
    return f"{base}...{head}" if head else base


def changed_lines(diff_range: str) -> dict:
    """{경로: 변경 줄 수}. 바이너리는 0줄."""
    lines = {}
    for row in _git("diff", "--numstat", diff_range).splitlines():
        added, deleted, path = row.split("\t", 2)
        lines[path] = sum(int(n) for n in (added, deleted) if n.isdigit())
    return lines


# ---------- LESSONS ----------

def match_lessons(lessons_text: str, texts: list) -> list:
    """`[키: a, b]`의 키 하나라도 texts 어딘가에 들어 있는 LESSONS 줄. 손으로 옮겨 적지 않아도 전달되게 한다."""
    found = []
    for line in lessons_text.splitlines():
        match = LESSON_KEYS.search(line) if line.startswith("- [") else None
        keys = [k.strip() for k in match.group(1).split(",") if k.strip()] if match else []
        if any(k in text for k in keys for text in texts):
            found.append(line)
    return found


def relevant_lessons(texts: list, path: Optional[Path] = None) -> list:
    path = path or LESSONS_FILE
    return match_lessons(path.read_text(encoding="utf-8"), texts) if path.exists() else []


def review_lessons(base: str, head: Optional[str]) -> list:
    """리뷰 대상과 관련된 LESSONS 줄. 커밋 리뷰는 그 커밋의 LESSONS를, 작업 트리 리뷰는 새 파일 경로까지 본다."""
    texts = [_git("diff", review_range(base, head))]
    if head is None:
        texts.append(_git("ls-files", "--others", "--exclude-standard"))
        return relevant_lessons(texts)
    try:
        lessons_text = _git("show", f"{head}:docs/LESSONS.md")
    except ReviewError:  # 그 커밋에 LESSONS가 없으면 전달할 규칙도 없다
        return []
    return match_lessons(lessons_text, texts)


def lessons_section(heading: str, lessons: list) -> str:
    return f"## {heading}\n\n" + "\n".join(lessons) + "\n\n" if lessons else ""


# ---------- 리뷰어 선택 ----------

def choose_reviewers(config: dict, lines_by_path: dict) -> list:
    """작고 민감하지 않은 변경은 첫 리뷰어 1명, 나머지는 전원(교차). Cloudflare 위험 등급을 단순화했다."""
    names = [r["name"] for r in config["roles"]["reviewers"]]
    rules = config.get("review", {})
    max_lines = rules.get("single_reviewer_max_lines", DEFAULT_SINGLE_REVIEWER_MAX_LINES)
    sensitive = rules.get("full_review_paths", DEFAULT_FULL_REVIEW_PATHS)
    counted = {p: n for p, n in lines_by_path.items() if not any(fnmatch(p, g) for g in GENERATED_PATTERNS)}
    is_sensitive = any(s in p for p in counted for s in sensitive)
    if sum(counted.values()) <= max_lines and not is_sensitive:
        return names[:1]
    return names


# ---------- 프롬프트 ----------

def build_prompt(base: str, *, head: Optional[str] = None, intent: tuple = (), lessons: tuple = ()) -> str:
    diff = f"git diff {base}...{head}" if head else f"git diff {base}"
    read_hint = f" 작업 트리는 리뷰 대상과 다를 수 있으니 파일은 `git show {head}:<경로>`로 읽어라." if head else ""
    intent_part = ("## 이 변경의 의도 (커밋 제목)\n\n" + "\n".join(f"- {s}" for s in intent) +
                   "\n\n의도 자체는 따지지 말고, 코드가 그 의도를 달성했는지와 의도 밖의 변경을 했는지 보라.\n") if intent else ""
    lessons_part = lessons_section("이 변경과 관련된 LESSONS (재발하면 P1)", list(lessons))
    parts = [
        "너는 다른 엔지니어가 만든 코드 변경을 리뷰한다. 코드를 수정하지 마라.\n",
        intent_part + lessons_part +
        "1. REVIEW.md(리뷰 기준, 특히 '지적하지 말 것')와 CLAUDE.md를 읽어라. docs/LESSONS.md 중 이 변경과 관련된 줄은 "
        "위에 붙였다. 구조나 기술 스택을 바꾸는 변경이면 docs/ARCHITECTURE.md와 docs/ADR.md도 읽어라.",
        f"2. `{diff}`" + ("" if head else "와 `git status --short`(아직 커밋하지 않은 새 파일)") +
        f"로 변경을 확인하라. 필요하면 변경 파일 전체를 읽어 맥락을 확인하라.{read_hint}",
        "3. REVIEW.md의 심각도(P0-P3), 체크리스트, Risk Score 기준을 그대로 적용하라.",
        "4. 이 diff가 새로 만든 문제만 지적하라. 기존 문제는 pre_existing=true로 표시하라.",
        "5. 머지를 막을 문제를 먼저 찾아라. 지적마다 왜 틀렸는지(body)와 실패를 보이는 방법(repro: 테스트·명령·입력)을 적어라.",
        "6. 실행으로 실패를 봤거나, 호출부부터 실패 지점까지 경로를 줄 단위로 따라가 보인 지적만 confirmed=true로 하라. "
        "줄만 가리킨 지적은 confirmed=false로 두고, summary에 어디까지 확인했는지 적어라.",
        "7. 결과는 주어진 JSON 스키마 형식 하나로만 출력하라. 첫 리뷰이므로 resolved는 빈 배열로 둔다.",
    ]
    return "\n".join(parts) + "\n"


def build_verify_prompt(previous: dict, head: Optional[str] = None) -> str:
    """검증 리뷰: 이전 차단 지적이 고쳐졌는지, 수정이 새 문제를 만들었는지만 본다."""
    items = "\n".join(
        f"[{n}] P{f['priority']} {f['file']}:{f['line_start']}-{f['line_end']} {f['title']} — {f['body']}"
        + (f" (재현: {f['repro']})" if f.get("repro") else "")
        for n, f in enumerate(blocking_findings(previous), 1)
    )
    if head:  # pre-push: push할 커밋끼리 비교한다
        diff_step = (f"2. `git diff {previous['head']}..{head}`로 그 뒤의 수정만 확인하라. "
                     f"작업 트리는 리뷰 대상과 다를 수 있으니 파일은 `git show {head}:<경로>`로 읽어라.\n")
    else:
        diff_step = f"2. `git diff {previous['head']}`로 그 뒤의 수정만 확인하라.\n"
    return (
        "너는 이전 리뷰의 차단 지적이 고쳐졌는지 확인한다. 코드를 수정하지 마라. 전체를 다시 리뷰하지 마라.\n\n"
        f"## 이전 차단 지적\n\n{items}\n\n"
        "1. REVIEW.md(특히 '지적하지 말 것')를 읽어라.\n"
        f"{diff_step}"
        "3. 고쳐졌음을 확인한 이전 지적의 번호만 resolved에 넣어라. 확인하지 못했거나 남은 지적은 resolved에 넣지 마라"
        "(넣지 않은 지적은 그대로 차단으로 이어진다). 남은 지적은 같은 위치·등급으로 findings에도 다시 넣어라.\n"
        "4. 위 수정 diff가 새로 만든 P0/P1만 새 지적으로 추가하라. 수정 diff 밖의 문제는 지적하지 마라.\n"
        "5. 결과는 주어진 JSON 스키마 형식 하나로만 출력하라.\n"
    )


def reviewer_command(reviewer: dict, prompt_file: Path, raw_file: Path) -> str:
    """리뷰어 하나를 읽기 전용으로 실행하는 셸 명령."""
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


# ---------- 결과 파싱·병합 ----------

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


def run_local(commands: dict, timeout_sec: int) -> dict:
    """리뷰어 명령을 병렬 subprocess로 실행한다. 반환: {name: 에러}"""
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
                merged.append({**finding, "found_by": [name], "agreed_by": [name]})
                continue
            priority = min(match["priority"], finding["priority"])
            # agreed_by: 합친 등급과 같은 등급으로 지적한 리뷰어
            if finding["priority"] < match["priority"]:
                match["agreed_by"] = [name]
            elif finding["priority"] == match["priority"] and name not in match["agreed_by"]:
                match["agreed_by"].append(name)
            # 합친 등급과 같은 등급으로 직접 확인한 리뷰어가 있을 때만 확인된 지적이다.
            # (미확인 P1 + 확인된 P2가 "확인된 P1"로 올라가 차단되지 않게)
            match["confirmed"] = any(
                f.get("confirmed", True) and f["priority"] == priority for f in (match, finding))
            match["priority"] = priority
            # 한쪽이라도 신규 문제로 봤으면 신규로 둔다(차단 판정에서 빠지지 않게).
            match["pre_existing"] = match["pre_existing"] and finding["pre_existing"]
            match["repro"] = match.get("repro") or finding.get("repro", "")
            if name not in match["found_by"]:
                match["found_by"].append(name)
    merged.sort(key=lambda f: (f["priority"], f["file"], f["line_start"]))
    nits = [f for f in merged if f["priority"] == NIT_PRIORITY]
    return [f for f in merged if f["priority"] != NIT_PRIORITY] + nits[:MAX_NITS]


def _is_blocking(finding: dict) -> bool:
    """신규 P0는 항상, 신규 P1은 확인됐거나 리뷰어 2명 이상이 같은 등급으로 지적했을 때 차단한다.

    confirmed는 모델이 스스로 매기므로, 그것만으로 P0를 풀면 읽기 전용 리뷰어가 확인하지 못한 심각한 문제가 통과한다.
    confirmed가 없는 이전 결과는 확인된 것으로 본다.
    """
    if finding["priority"] > BLOCKING_MAX_PRIORITY or finding["pre_existing"]:
        return False
    is_agreed = len(finding.get("agreed_by", [])) >= MIN_AGREEING_REVIEWERS
    return finding["priority"] == 0 or finding.get("confirmed", True) or is_agreed


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


# ---------- 상태·캐시 ----------

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write_json_atomic(path: Path, data: dict):
    # 리캡이 쓰는 도중의 파일을 읽지 않도록 임시 파일에 쓰고 바꿔치기한다.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def write_status(status: dict):
    """리캡이 읽는 진행 상태. state: running/done, mode: first/verify."""
    _write_json_atomic(STATUS_FILE, status)


def is_ancestor(ancestor: str, descendant: str) -> bool:
    r = subprocess.run(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=ROOT, capture_output=True)
    return r.returncode == 0


def is_passed(base: str, head: str) -> bool:
    """base..head 범위가 이미 통과한 리뷰 범위 안에 있는가. 기록은 '리뷰 기준 커밋 head' 한 줄씩이다."""
    if not PASSED_FILE.exists():
        return False
    for line in PASSED_FILE.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == head and (parts[0] == base or is_ancestor(parts[0], base)):
            return True
    return False


def archive_result(result: dict, head: str):
    _write_json_atomic(RESULTS_DIR / f"{head}.json", result)


def load_result(head: str) -> Optional[dict]:
    path = RESULTS_DIR / f"{head}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def record_passed(base: str, head: str):
    if not is_passed(base, head):
        PASSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        with PASSED_FILE.open("a") as f:
            f.write(f"{base} {head}\n")


# ---------- 실행 ----------

def _select(config: dict, base: str, head: Optional[str], previous: Optional[dict], only: Optional[list]) -> list:
    """이번 리뷰를 돌릴 리뷰어 이름."""
    if only:
        return only
    if previous:
        # 검증 리뷰는 차단 지적을 낸 리뷰어만 다시 돈다.
        names = [r["name"] for r in config["roles"]["reviewers"]]
        found = {n for f in blocking_findings(previous) for n in f.get("found_by", [])}
        return [n for n in names if n in found] or names
    return choose_reviewers(config, changed_lines(review_range(base, head)))


def carry_unresolved(merged: dict, previous: dict, results: dict) -> dict:
    """검증 리뷰어가 모두 해결로 확인하지 않은 이전 차단 지적을 결과에 이어 붙인다.

    리뷰어가 남은 지적을 다시 적는 것을 빠뜨려도 미수정 P0/P1이 통과하지 않게 한다.
    """
    resolved_sets = [set(r.get("resolved", [])) for r in results.values()]
    resolved = set.intersection(*resolved_sets) if resolved_sets else set()
    unresolved = [f for n, f in enumerate(blocking_findings(previous), 1) if n not in resolved]
    # 남은 지적은 원래 속성 그대로 둔다. 같은 자리를 미확인·기존 문제로 약하게 다시 보고한 것만 버리고,
    # 같은 자리의 새 차단 지적은 남긴다.
    findings = [m for m in merged["findings"]
                if _is_blocking(m) or not any(_overlaps(f, m) for f in unresolved)]
    findings += [{**f, "carried": True} for f in unresolved]
    findings.sort(key=lambda f: (f["priority"], f["file"], f["line_start"]))
    return {**merged, "findings": findings, "blocking": sum(1 for f in findings if _is_blocking(f))}


def _run_reviewers(reviewers: dict, prompt: str, timeout_sec: int, work_dir: Optional[Path]) -> tuple:
    """리뷰어들을 돌려 (results, failed)를 돌려준다.

    work_dir를 주지 않으면 실행마다 새 폴더를 만들고 끝나면 지운다.
    동시에 도는 리뷰(백그라운드 push 등)가 서로의 파일을 덮지 않게 하기 위해서다.
    """
    own_dir = work_dir is None
    if own_dir:
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix="run-", dir=REVIEW_DIR))
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = work_dir / "review-prompt.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        raw_files = {name: work_dir / f"review-{name}.raw.json" for name in reviewers}
        for raw in raw_files.values():
            raw.unlink(missing_ok=True)
        commands = {name: reviewer_command(r, prompt_file, raw_files[name]) for name, r in reviewers.items()}
        return _collect(raw_files, reviewers, run_local(commands, timeout_sec))
    finally:
        if own_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


def run_review(base: str, out: Path, *, config: Optional[dict] = None, work_dir: Optional[Path] = None,
               only: Optional[list] = None, head: Optional[str] = None, previous: Optional[dict] = None) -> dict:
    """리뷰를 돌려 병합 결과를 out에 쓰고 돌려준다. previous가 있으면 검증 리뷰다."""
    config = config or cfg.load_config()
    selected = _select(config, base, head, previous, only)
    reviewers = {r["name"]: r for r in config["roles"]["reviewers"] if r["name"] in selected}
    unknown = set(selected) - set(reviewers)
    if unknown or not reviewers:
        raise ReviewError(f"설정에 없는 리뷰어: {sorted(unknown)}")

    reviewed_head = resolve_commit(head or "HEAD")
    mode = "verify" if previous else "first"
    status = {"state": "running", "mode": mode, "reviewers": list(reviewers), "started_at": _now()}
    write_status(status)
    if previous:
        prompt = build_verify_prompt(previous, head)
    else:
        prompt = build_prompt(base, head=head, intent=tuple(commit_subjects(base, head)),
                              lessons=tuple(review_lessons(base, head)))
    results, failed = _run_reviewers(reviewers, prompt, config["timeouts_sec"]["review"], work_dir)
    if not results:
        write_status({**status, "state": "failed", "finished_at": _now()})
        raise ReviewError(f"모든 리뷰어 실패: {failed}")

    round_no = previous.get("round", 0) + 1 if previous else 0
    merged = {"base": base, "head": reviewed_head, "mode": mode, "round": round_no, **merge(results, failed)}
    if previous:
        merged = carry_unresolved(merged, previous, results)
        if previous["risk"]["score"] > merged["risk"]["score"]:
            merged["risk"] = previous["risk"]  # 검증 리뷰는 수정분만 보므로 위험도는 첫 리뷰 값을 유지한다
    _write_json_atomic(out, merged)
    write_status({**status, "state": "done", "finished_at": _now(), "blocking": merged["blocking"]})
    # 커밋되지 않은 변경이 섞인 리뷰는 커밋의 기록으로 남기지 않는다(PR 단계·pre-push가 그 커밋의 결과로 믿는다).
    if head or is_worktree_clean():
        archive_result(merged, reviewed_head)
        if not merged["blocking"]:
            record_passed(merge_base(base, reviewed_head), reviewed_head)
    return merged


def blocking_findings(merged: dict) -> list:
    return [f for f in merged["findings"] if _is_blocking(f)]


def build_fix_prompt(merged: dict, project: str) -> str:
    items = "\n".join(
        f"- [P{f['priority']}] {f['file']}:{f['line_start']} {f['title']} — {f['body']}"
        + (f" (재현: {f['repro']})" if f.get("repro") else "")
        for f in blocking_findings(merged)
    )
    return (
        f"당신은 {project} 프로젝트의 개발자입니다. 코드 리뷰에서 아래 차단 이슈(P0/P1)가 나왔습니다.\n\n"
        f"## 리뷰 지적사항\n\n{items}\n\n"
        "## 작업 규칙\n\n"
        "1. 지적된 문제만 고쳐라. 다른 리팩터링이나 기능 추가를 하지 마라.\n"
        "2. 지적된 자리만이 아니라 같은 종류의 문제가 있는 모든 자리를 찾아 함께 고쳐라. "
        "한 자리만 고치면 다음 리뷰가 같은 문제를 다른 자리에서 다시 찾는다.\n"
        "3. 각 수정에는 문제를 재현하는 테스트를 먼저 추가하라. 기존 테스트를 깨뜨리지 마라.\n"
        "4. 각 지적의 원인을 규칙 한 줄(300자 이하)로 docs/LESSONS.md에 추가하라. "
        "형식: `- [YYYY-MM-DD] 규칙. 원인: … (출처: review) [키: 관련 경로나 단어, …]`. 같은 규칙이 있으면 새 줄을 붙이지 말고 그 줄을 고쳐라.\n"
        "5. 커밋하지 마라. 실행기가 커밋한다.\n"
    )


def format_summary(merged: dict) -> str:
    risk = merged["risk"]
    mode = " (검증 리뷰)" if merged.get("mode") == "verify" else ""
    lines = [
        f"## 리뷰 결과{mode} — {merged['verdict']} | risk {risk['level']} ({risk['score']}) | 차단 {merged['blocking']}건",
        f"리뷰어: {', '.join(merged['reviewers'])}" + (f" | 실패: {merged['failed']}" if merged["failed"] else ""),
        "",
    ]
    for f in merged["findings"]:
        tag = (" (기존)" if f["pre_existing"] else "") + ("" if f.get("confirmed", True) else " (미확인)")
        lines.append(f"- P{f['priority']}{tag} {f['file']}:{f['line_start']} {f['title']} [{'+'.join(f['found_by'])}]")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Harness 교차 코드 리뷰")
    parser.add_argument("--base", default="main", help="비교 기준 브랜치/커밋 (기본 main)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="병합 결과 JSON 경로")
    parser.add_argument("--reviewers", help="쉼표로 구분한 리뷰어 이름 (기본: 변경 크기로 자동 선택)")
    parser.add_argument("--head", help="base...HEAD 커밋 범위만 리뷰 (pre-push)")
    parser.add_argument("--previous", type=Path, help="이 결과의 차단 지적만 검증한다(수정분만 리뷰)")
    args = parser.parse_args()

    only = args.reviewers.split(",") if args.reviewers else None
    previous = json.loads(args.previous.read_text(encoding="utf-8")) if args.previous else None
    try:
        merged = run_review(args.base, args.out, only=only, head=args.head, previous=previous)
    except (ReviewError, cfg.ConfigError) as err:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(EXIT_ALL_FAILED)
    print(format_summary(merged))
    print(f"\n결과: {args.out}")
    sys.exit(EXIT_BLOCKING if merged["blocking"] else 0)


if __name__ == "__main__":
    main()

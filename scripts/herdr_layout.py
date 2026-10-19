#!/usr/bin/env python3
"""
herdr 화면 배치 — 탭1은 사람이 보는 곳, 병렬 작업은 뒤쪽 탭에 모은다.

    탭1:      [오케스트레이션 | 리캡]          리캡 = 쉬운 설명(지금 무슨 일이), 사람이 정할 것, 작업 탭·pane 상태, phase 현황, 최근 리뷰 요약을 주기적으로 갱신
    작업 N:   작업 pane 최대 8개(2행×4열 격자). 다 차면 "작업 N+1" 탭을 만든다.

Usage:
    python3 scripts/herdr_layout.py setup                      # 현재 pane을 "오케스트레이션"으로, 옆에 리캡 pane
    python3 scripts/herdr_layout.py split [--cwd P] [--label L] # 작업 탭에 pane을 만들고 pane_id 출력
    python3 scripts/herdr_layout.py recap                      # 리캡 화면(리캡 pane에서 자동 실행)
"""

import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import recap_story

ORCHESTRATOR_LABEL = "오케스트레이션"
RECAP_LABEL = "리캡"
WORKER_TAB_PREFIX = "작업 "
MAX_PANES_PER_TAB = 8
RECAP_INTERVAL_SEC = 5
RECAP_MAX_FINDINGS = 5
BLOCKED_PANE_STATUS = "blocked"
DECISION_PHASE_STATUSES = ("blocked", "error")
# 터미널 셀은 세로가 가로의 약 2배라, 폭이 높이의 2배 이상이면 좌우로 나눠야 정사각형에 가깝다.
CELL_ASPECT = 2

ROOT = Path(__file__).resolve().parent.parent
PHASES_INDEX = ROOT / "phases" / "index.json"
REVIEW_STATUS = ROOT / ".review" / "status.json"
LESSONS_FILE = ROOT / "docs" / "LESSONS.md"


def _herdr(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["herdr", *args], capture_output=True, text=True)


def _herdr_json(*args) -> dict:
    r = _herdr(*args)
    if r.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args[:2])} 실패: {r.stderr.strip()}")
    # pane run 등은 성공해도 출력이 없다.
    return json.loads(r.stdout)["result"] if r.stdout.strip() else {}


def choose_grid_split(layout: dict) -> tuple:
    """탭 안에서 가장 큰 pane을 골라, 넓으면 좌우·높으면 위아래로 나눈다. 8개면 2행×4열이 된다."""
    target = max(layout["panes"], key=lambda p: (p["rect"]["width"] * p["rect"]["height"],
                                                -p["rect"]["y"], -p["rect"]["x"]))
    rect = target["rect"]
    direction = "right" if rect["width"] >= rect["height"] * CELL_ASPECT else "down"
    return target["pane_id"], direction


def _worker_tabs(tabs: list) -> list:
    return [t for t in tabs if t.get("label", "").startswith(WORKER_TAB_PREFIX)]


def pick_worker_tab(tabs: list):
    """자리가 남은 첫 작업 탭의 tab_id, 없으면 None."""
    for tab in sorted(_worker_tabs(tabs), key=lambda t: t.get("number", 0)):
        if tab.get("pane_count", 0) < MAX_PANES_PER_TAB:
            return tab["tab_id"]
    return None


def next_worker_label(tabs: list) -> str:
    return f"{WORKER_TAB_PREFIX}{len(_worker_tabs(tabs)) + 1}"


def _pane_name(pane: dict) -> str:
    return pane.get("label") or pane.get("terminal_title_stripped") or pane["pane_id"]


def latest_review(root: Path):
    """가장 최근에 바뀐 리뷰 결과(.review/latest.json, phases/*/review.json)의 (상대 경로, 내용). 없으면 None."""
    candidates = [root / ".review" / "latest.json", *root.glob("phases/*/review.json")]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        return None
    newest = max(existing, key=lambda p: p.stat().st_mtime)
    return str(newest.relative_to(root)), json.loads(newest.read_text(encoding="utf-8"))


def _render_review(source: str, review: dict) -> list:
    risk = review.get("risk", {})
    lines = ["", f"리뷰 ({source}):",
             f"  {review.get('verdict')} | risk {risk.get('level')} ({risk.get('score')}) | 차단 {review.get('blocking', 0)}건"]
    if review.get("failed"):
        lines.append(f"  실패: {', '.join(review['failed'])}")
    findings = review.get("findings", [])
    for f in findings[:RECAP_MAX_FINDINGS]:
        tag = ("(기존) " if f.get("pre_existing") else "") + ("" if f.get("confirmed", True) else "(미확인) ")
        lines.append(f"  P{f['priority']} {tag}{f['file']}:{f['line_start']} {f['title']} [{'+'.join(f.get('found_by', []))}]")
    if len(findings) > RECAP_MAX_FINDINGS:
        lines.append(f"  … 외 {len(findings) - RECAP_MAX_FINDINGS}건")
    return lines


def _needs_decision(panes: list, phases, review) -> list:
    """사람이 결정해야 하는 항목: 입력을 기다리는 pane, 멈춘 phase, 리뷰 차단, 실패한 리뷰어."""
    items = [f"  '{_pane_name(p)}' 창의 AI가 사람의 대답을 기다려요"
             for p in panes if p.get("agent_status") == BLOCKED_PANE_STATUS]
    for phase in (phases or {}).get("phases", []):
        if phase.get("status") in DECISION_PHASE_STATUSES:
            reason = "오류로 멈췄어요" if phase["status"] == "error" else "멈췄어요"
            items.append(f"  작업 '{phase.get('dir')}'이(가) {reason}")
    if review:
        source, result = review
        if result.get("blocking"):
            items.append(f"  꼭 고쳐야 할 문제 {result['blocking']}건이 남았어요 ({source})")
        if result.get("failed"):
            items.append(f"  검사관 {', '.join(result['failed'])}이(가) 실행되지 못했어요(설정·로그인 확인)")
    return items


def render_recap(tabs: list, panes: list, own_tab: str, phases, review=None, story=None, worktrees=()) -> str:
    """리캡 화면 텍스트. 쉬운 설명(story)과 사람이 정할 것을 맨 위에 두고, 오케스트레이터 탭(own_tab)을 뺀
    작업 탭별 pane 상태·phase 현황·최근 리뷰 요약을 보여준다."""
    lines = [f"리캡 {time.strftime('%H:%M:%S')}"]
    if story:
        lines += ["지금 무슨 일이 일어나고 있나요", *(f"  {line}" for line in story)]
    decisions = _needs_decision(panes, phases, review)
    decisions += [f"  [{w['name']}] {item.strip()}" for w in worktrees for item in _needs_decision([], w["phases"], w["review"])]
    if decisions:
        lines += [""] * bool(story) + ["⚠ 사람이 정해야 해요:", *decisions]
    lines += ["", "── 자세히 ──"]
    tab_labels = {t["tab_id"]: t.get("label", t["tab_id"]) for t in tabs}
    by_tab = {}
    for pane in panes:
        if pane.get("tab_id") != own_tab:
            by_tab.setdefault(pane.get("tab_id"), []).append(pane)

    if not by_tab:
        lines.append("병렬 작업 없음")
    for tab_id, tab_panes in by_tab.items():
        lines.append(f"[{tab_labels.get(tab_id, tab_id)}] {len(tab_panes)}/{MAX_PANES_PER_TAB}")
        for pane in tab_panes:
            lines.append(f"  {pane.get('agent_status', '-'):<8} {_pane_name(pane)}")

    if phases:
        lines += ["", "phases:"]
        for phase in phases.get("phases", []):
            lines.append(f"  {phase.get('status', '-'):<10} {phase.get('dir')}")
    if review:
        lines += _render_review(*review)
    return "\n".join(lines)


def _current_pane() -> dict:
    return _herdr_json("pane", "current")["pane"]


def _current_pane_id() -> str:
    return os.environ.get("HERDR_PANE_ID") or _current_pane()["pane_id"]


def _workspace_id() -> str:
    return os.environ.get("HERDR_WORKSPACE_ID") or _current_pane()["workspace_id"]


def _tab_id() -> str:
    return os.environ.get("HERDR_TAB_ID") or _current_pane()["tab_id"]


def _list_panes(workspace: str) -> list:
    return _herdr_json("pane", "list", "--workspace", workspace)["panes"]


def setup() -> None:
    """현재 pane을 "오케스트레이션"으로 바꾸고, 같은 탭에 리캡 pane이 없으면 오른쪽에 연다."""
    current = _current_pane_id()
    _herdr_json("pane", "rename", current, ORCHESTRATOR_LABEL)
    tab = _tab_id()
    if any(p.get("tab_id") == tab and p.get("label") == RECAP_LABEL for p in _list_panes(_workspace_id())):
        return
    recap = _herdr_json("pane", "split", current, "--direction", "right", "--no-focus",
                        "--cwd", str(ROOT))["pane"]["pane_id"]
    _herdr_json("pane", "rename", recap, RECAP_LABEL)
    _herdr_json("pane", "run", recap, f"python3 scripts/herdr_layout.py recap --tab {tab}")


def split_pane(cwd: Path, label: str = None) -> str:
    """자리가 남은 작업 탭에 pane을 만들고(없으면 탭 생성, 포커스는 그대로) pane_id를 돌려준다."""
    workspace = _workspace_id()
    cwd = Path(cwd).resolve()
    tabs = _herdr_json("tab", "list", "--workspace", workspace)["tabs"]
    tab = pick_worker_tab(tabs)
    if tab is None:
        pane = _herdr_json("tab", "create", "--workspace", workspace, "--cwd", str(cwd),
                           "--label", next_worker_label(tabs), "--no-focus")["root_pane"]["pane_id"]
    else:
        anchor = next(p["pane_id"] for p in _list_panes(workspace) if p.get("tab_id") == tab)
        layout = _herdr_json("pane", "layout", "--pane", anchor)["layout"]
        target, direction = choose_grid_split(layout)
        pane = _herdr_json("pane", "split", target, "--direction", direction,
                           "--no-focus", "--cwd", str(cwd))["pane"]["pane_id"]
    if label:
        _herdr_json("pane", "rename", pane, label)
    return pane


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _phase_story(root: Path, phases, review, now: datetime) -> list:
    """root(메인 폴더나 worktree)의 진행 중 phase index와 리뷰 진행 상태를 읽어 쉬운 설명 문장을 만든다."""
    active = next((p for p in (phases or {}).get("phases", []) if p.get("status") != "completed"),
                  ((phases or {}).get("phases") or [None])[-1])
    index = _read_json(root / "phases" / active["dir"] / "index.json") if active else None
    status = _read_json(REVIEW_STATUS if root == ROOT else root / ".review" / "status.json")
    return recap_story.explain(phases, index, review[1] if review else None, status, now)


def build_story(phases, review) -> list:
    """메인 폴더의 쉬운 설명. LESSONS가 너무 길면 경고를 붙인다."""
    story = _phase_story(ROOT, phases, review, datetime.now().astimezone())
    warning = recap_story.lessons_warning(LESSONS_FILE.read_text(encoding="utf-8")) if LESSONS_FILE.exists() else None
    return story + ([f"⚠ {warning}"] if warning else [])


def _branch_phase(path: Path, phases: dict, branch: str):
    """worktree 브랜치가 돌리는 phase. execute.py는 phase마다 feat-<phase 이름> 브랜치에서 돈다."""
    for phase in phases.get("phases", []):
        index = _read_json(path / "phases" / phase["dir"] / "index.json") or {}
        if branch == f"feat-{index.get('phase', phase['dir'])}":
            return phase
    return None


def load_worktrees(root: Path) -> list:
    """메인 폴더 밖에서 phase를 돌리는 git worktree: {name(브랜치), path, phases(그 phase만), review}.
    worktree에는 top index 전체가 복제되므로 브랜치로 그 worktree의 phase를 고른다. 맞는 phase가 없으면 뺀다."""
    r = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    out = []
    for block in r.stdout.strip().split("\n\n"):
        fields = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        path = Path(fields.get("worktree", ""))
        branch = fields.get("branch", "").removeprefix("refs/heads/")
        phases = _read_json(path / "phases" / "index.json")
        phase = _branch_phase(path, phases, branch) if phases and path.resolve() != root.resolve() else None
        if phase:
            out.append({"name": branch, "path": path, "phases": {"phases": [phase]}, "review": latest_review(path)})
    return out


def worktree_story(worktrees: list, now: datetime) -> list:
    """worktree마다 쉬운 설명을 [브랜치] 머리말을 붙여 이어 붙인다."""
    return [f"[{w['name']}] {line}" for w in worktrees for line in _phase_story(w["path"], w["phases"], w["review"], now)]


def recap_text(workspace: str, own_tab: str) -> str:
    """리캡 한 화면. 실패해도 리캡 pane이 죽지 않게 오류를 화면에 적고 다음 주기에 다시 시도한다."""
    try:
        tabs = _herdr_json("tab", "list", "--workspace", workspace)["tabs"]
        phases = _read_json(PHASES_INDEX)
        review = latest_review(ROOT)
        worktrees = load_worktrees(ROOT)
        story = build_story(phases, review) + worktree_story(worktrees, datetime.now().astimezone())
        return render_recap(tabs, _list_panes(workspace), own_tab, phases, review, story, worktrees)
    except Exception as err:  # noqa: BLE001 — 파일이 쓰이는 중이거나 herdr 일시 오류: 다음 주기에 회복된다
        return f"리캡 갱신 실패 ({type(err).__name__}: {err}). {RECAP_INTERVAL_SEC}초 뒤 다시 시도합니다."


def recap_loop(own_tab: str) -> None:
    workspace = _workspace_id()
    while True:
        print("\033[2J\033[H" + recap_text(workspace, own_tab), flush=True)
        time.sleep(RECAP_INTERVAL_SEC)


def main():
    parser = argparse.ArgumentParser(description="herdr 화면 배치")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help='현재 pane을 "오케스트레이션"으로 바꾸고 리캡 pane을 연다')
    split = sub.add_parser("split", help="작업 탭에 새 pane을 만든다")
    split.add_argument("--cwd", type=Path, default=Path.cwd())
    split.add_argument("--label", help="pane 이름(리캡에 표시)")
    recap = sub.add_parser("recap", help="리캡 화면을 갱신한다")
    recap.add_argument("--tab", help="제외할 오케스트레이터 탭 id")
    args = parser.parse_args()

    if args.cmd == "setup":
        setup()
    elif args.cmd == "split":
        print(split_pane(args.cwd, args.label))
    else:
        recap_loop(args.tab or _tab_id())


if __name__ == "__main__":
    main()

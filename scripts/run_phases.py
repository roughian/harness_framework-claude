#!/usr/bin/env python3
"""
독립 phase 병렬 실행 — 지금 시작할 수 있는 phase를 parallel.max_worktrees개까지 worktree로 함께 띄운다.

시작 조건: status가 pending이고, depends_on의 phase가 모두 completed(main에 merge됨)이며, 이미 돌고 있지 않다.
한 번 띄우고 끝난다. 앞선 phase를 merge한 뒤 다시 실행하면 다음 묶음이 시작된다.

Usage:
    python3 scripts/run_phases.py [--dry-run]
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

import config as cfg
import herdr_layout

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MAX_WORKTREES = 3


def ready_phases(index: dict, running: set, limit: int) -> tuple:
    """(지금 띄울 phase 목록, 기다리는 이유 목록). limit은 동시 실행 상한(실행 중 포함)."""
    status = {p["dir"]: p.get("status") for p in index.get("phases", [])}
    # merge가 끝난 phase의 worktree는 남아 있어도 돌고 있지 않다.
    running = {name for name in running if status.get(name) != "completed"}
    ready, waiting = [], []
    for phase in index.get("phases", []):
        name = phase["dir"]
        if status[name] != "pending" or name in running:
            continue
        pending_deps = [d for d in phase.get("depends_on", []) if status.get(d) != "completed"]
        if pending_deps:
            waiting.append(f"{name}: {', '.join(pending_deps)} 끝나기를 기다림")
        elif len(running) + len(ready) >= limit:
            waiting.append(f"{name}: 동시 실행 상한 {limit}개 도달")
        else:
            ready.append(phase)
    return ready, waiting


def running_phases(root: Path) -> set:
    """feat-<phase> 브랜치를 가진 worktree = 이미 띄운 phase(execute.py는 phase마다 이 브랜치에서 돈다)."""
    r = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=root, capture_output=True, text=True)
    branches = [line.removeprefix("branch refs/heads/") for line in r.stdout.splitlines() if line.startswith("branch ")]
    return {b.removeprefix("feat-") for b in branches if b.startswith("feat-")}


def _pin_review_base(index_file: Path, start: str) -> None:
    """리뷰 base를 시작 커밋으로 고정한다. execute.py는 feat 브랜치가 이미 checkout돼 있으면 index의
    base_branch를 쓰고, 없으면 "main"을 쓴다. 다른 phase가 먼저 merge되면 main이 움직여 그 변경이 이 phase의 삭제로 보인다."""
    if not index_file.exists():
        return
    index = json.loads(index_file.read_text(encoding="utf-8"))
    index.setdefault("base_branch", start)
    index_file.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")


def launch(phase: dict, root: Path) -> None:
    name = phase["dir"]
    wt = root.parent / f"wt-{name}"
    start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    r = subprocess.run(["git", "worktree", "add", str(wt), "-b", f"feat-{name}", start],
                       cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{name} worktree 생성 실패: {r.stderr.strip()}")
    _pin_review_base(wt / "phases" / name / "index.json", start)
    if os.environ.get("HERDR_PANE_ID"):
        pane = herdr_layout.split_pane(wt, label=f"phase:{name}")
        subprocess.run(["herdr", "pane", "run", pane, f"python3 scripts/execute.py {name}"], check=True)
        return
    log = wt / "phases" / name / "run.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(["python3", "scripts/execute.py", name], cwd=wt, stdout=log.open("w"), stderr=subprocess.STDOUT,
                     env={**os.environ, "PYTHONUNBUFFERED": "1"})
    print(f"  {name}: 백그라운드 실행, 로그 {log}")


def main():
    parser = argparse.ArgumentParser(description="독립 phase 병렬 실행")
    parser.add_argument("--dry-run", action="store_true", help="띄울 phase만 출력")
    args = parser.parse_args()

    index_file = ROOT / "phases" / "index.json"
    if not index_file.exists():
        raise SystemExit(f"{index_file} 없음 — /harness D절로 phase를 먼저 만든다")
    index = json.loads(index_file.read_text(encoding="utf-8"))
    limit = cfg.load_config().get("parallel", {}).get("max_worktrees", DEFAULT_MAX_WORKTREES)
    ready, waiting = ready_phases(index, running_phases(ROOT), limit)
    for phase in ready:
        print(f"시작: {phase['dir']} (../wt-{phase['dir']}, feat-{phase['dir']})")
        if not args.dry_run:
            launch(phase, ROOT)
    for line in waiting:
        print(f"대기: {line}")
    if not ready and not waiting:
        print("시작할 phase 없음")


if __name__ == "__main__":
    main()

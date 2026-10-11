#!/usr/bin/env python3
"""
herdr pane 배치 — 오케스트레이션 pane은 왼쪽 위 1/4을 유지하고, 새 pane은 나머지 영역을 위아래로 나눈다.

    1개: [오케스트레이션 | W1]
    2개: [오케스트레이션 | W1]
         [W2             |   ]
    3개+: 가장 큰 작업 pane을 위아래로(높이가 모자라면 좌우로) 나눈다.

Usage:
    python3 scripts/herdr_layout.py mark            # 현재 pane 이름을 "오케스트레이션"으로
    python3 scripts/herdr_layout.py split [--cwd P] # 규칙대로 새 pane을 만들고 pane_id 출력
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

ORCHESTRATOR_LABEL = "오케스트레이션"
MIN_PANE_ROWS = 10


def _herdr(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["herdr", *args], capture_output=True, text=True)


def _herdr_json(*args) -> dict:
    r = _herdr(*args)
    if r.returncode != 0:
        raise RuntimeError(f"herdr {' '.join(args[:2])} 실패: {r.stderr.strip()}")
    return json.loads(r.stdout)["result"]


def choose_split(layout: dict, anchor_id: str) -> tuple:
    """(나눌 pane_id, 방향)을 고른다. 오케스트레이션(anchor)이 1/4보다 크면 anchor부터 줄인다."""
    area = layout["area"]
    rects = {p["pane_id"]: p["rect"] for p in layout["panes"]}
    anchor = rects[anchor_id]
    others = {pid: r for pid, r in rects.items() if pid != anchor_id}

    if not others or anchor["width"] * 2 > area["width"] + 1:
        return anchor_id, "right"
    if anchor["height"] * 2 > area["height"] + 1:
        return anchor_id, "down"

    # 가장 큰 pane, 같으면 오른쪽 열·위쪽 우선
    target = max(others, key=lambda pid: (others[pid]["width"] * others[pid]["height"],
                                         others[pid]["x"], -others[pid]["y"]))
    direction = "down" if others[target]["height"] >= MIN_PANE_ROWS * 2 else "right"
    return target, direction


def find_anchor(panes: list, tab_id: str, current_id: str) -> str:
    """같은 탭의 "오케스트레이션" pane, 없으면 현재 pane."""
    for pane in panes:
        if pane.get("tab_id") == tab_id and pane.get("label") == ORCHESTRATOR_LABEL:
            return pane["pane_id"]
    return current_id


def _current_pane_id() -> str:
    return os.environ.get("HERDR_PANE_ID") or _herdr_json("pane", "current")["pane"]["pane_id"]


def mark_orchestrator() -> None:
    _herdr_json("pane", "rename", _current_pane_id(), ORCHESTRATOR_LABEL)


def split_pane(cwd: Path) -> str:
    """규칙대로 새 pane을 만들고(포커스는 그대로) pane_id를 돌려준다."""
    current = _current_pane_id()
    layout = _herdr_json("pane", "layout", "--pane", current)["layout"]
    list_args = ["pane", "list"]
    if os.environ.get("HERDR_WORKSPACE_ID"):
        list_args += ["--workspace", os.environ["HERDR_WORKSPACE_ID"]]
    panes = _herdr_json(*list_args)["panes"]

    anchor = find_anchor(panes, layout.get("tab_id"), current)
    if anchor not in {p["pane_id"] for p in layout["panes"]}:
        anchor = current
    target, direction = choose_split(layout, anchor)
    result = _herdr_json("pane", "split", target, "--direction", direction,
                         "--no-focus", "--cwd", str(cwd))
    return result["pane"]["pane_id"]


def main():
    parser = argparse.ArgumentParser(description="herdr pane 배치")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("mark", help='현재 pane 이름을 "오케스트레이션"으로 바꾼다')
    split = sub.add_parser("split", help="규칙대로 새 pane을 만든다")
    split.add_argument("--cwd", type=Path, default=Path.cwd())
    args = parser.parse_args()

    if args.cmd == "mark":
        mark_orchestrator()
    else:
        print(split_pane(args.cwd))


if __name__ == "__main__":
    main()

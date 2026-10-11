"""herdr_layout.py 테스트 — 오케스트레이션 pane은 왼쪽 위 1/4, 나머지는 위아래로 나눈다."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import herdr_layout as hl  # noqa: E402

AREA = {"x": 0, "y": 0, "width": 160, "height": 48}


def _pane(pane_id, x, y, width, height):
    return {"pane_id": pane_id, "rect": {"x": x, "y": y, "width": width, "height": height}}


def _layout(*panes):
    return {"area": AREA, "panes": list(panes)}


class TestChooseSplit:
    def test_first_pane_goes_right_of_orchestrator(self):
        layout = _layout(_pane("o", 0, 0, 160, 48))
        assert hl.choose_split(layout, "o") == ("o", "right")

    def test_second_pane_shrinks_orchestrator_to_top_left_quarter(self):
        layout = _layout(_pane("o", 0, 0, 80, 48), _pane("w1", 80, 0, 80, 48))
        assert hl.choose_split(layout, "o") == ("o", "down")

    def test_orchestrator_too_wide_is_split_right(self):
        layout = _layout(_pane("o", 0, 0, 160, 24), _pane("w1", 0, 24, 160, 24))
        assert hl.choose_split(layout, "o") == ("o", "right")

    def test_after_quarter_largest_worker_is_split_down(self):
        layout = _layout(
            _pane("o", 0, 0, 80, 24),
            _pane("w2", 0, 24, 80, 24),
            _pane("w1", 80, 0, 80, 48),
        )
        assert hl.choose_split(layout, "o") == ("w1", "down")

    def test_picks_largest_worker(self):
        layout = _layout(
            _pane("o", 0, 0, 80, 24),
            _pane("w2", 0, 24, 80, 24),
            _pane("w1", 80, 0, 80, 12),
            _pane("w3", 80, 12, 80, 12),
        )
        target, direction = hl.choose_split(layout, "o")
        assert (target, direction) == ("w2", "down")

    def test_split_right_when_no_pane_is_tall_enough(self):
        layout = _layout(
            _pane("o", 0, 0, 80, 24),
            _pane("w1", 80, 0, 80, 12),
            _pane("w2", 0, 24, 80, 12),
        )
        assert hl.choose_split(layout, "o")[1] == "right"


class TestFindAnchor:
    def test_prefers_labeled_pane_in_same_tab(self):
        panes = [
            {"pane_id": "a:p1", "tab_id": "a:t1", "label": hl.ORCHESTRATOR_LABEL},
            {"pane_id": "b:p1", "tab_id": "b:t1", "label": hl.ORCHESTRATOR_LABEL},
            {"pane_id": "b:p2", "tab_id": "b:t1"},
        ]
        assert hl.find_anchor(panes, tab_id="b:t1", current_id="b:p2") == "b:p1"

    def test_falls_back_to_current_pane(self):
        panes = [{"pane_id": "b:p2", "tab_id": "b:t1"}]
        assert hl.find_anchor(panes, tab_id="b:t1", current_id="b:p2") == "b:p2"


def _fake_herdr(layout, panes, new_id="w:p9"):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd[1:])
        if cmd[1:3] == ["pane", "layout"]:
            out = {"result": {"layout": {**layout, "tab_id": "w:t1"}}}
        elif cmd[1:3] == ["pane", "list"]:
            out = {"result": {"panes": panes}}
        elif cmd[1:3] == ["pane", "split"]:
            out = {"result": {"pane": {"pane_id": new_id}}}
        else:
            out = {"result": {}}
        return MagicMock(returncode=0, stdout=json.dumps(out), stderr="")

    return run, calls


class TestSplitPane:
    def test_splits_chosen_target_without_focus(self, monkeypatch):
        monkeypatch.setenv("HERDR_PANE_ID", "w:p1")
        monkeypatch.setenv("HERDR_WORKSPACE_ID", "w")
        layout = _layout(_pane("w:p1", 0, 0, 80, 48), _pane("w:p2", 80, 0, 80, 48))
        panes = [{"pane_id": "w:p1", "tab_id": "w:t1", "label": hl.ORCHESTRATOR_LABEL},
                 {"pane_id": "w:p2", "tab_id": "w:t1"}]
        run, calls = _fake_herdr(layout, panes)
        with patch("subprocess.run", side_effect=run):
            new_id = hl.split_pane(Path("/repo"))

        assert new_id == "w:p9"
        split = next(c for c in calls if c[:2] == ["pane", "split"])
        assert split[2] == "w:p1"
        assert split[split.index("--direction") + 1] == "down"
        assert "--no-focus" in split

    def test_split_failure_raises(self, monkeypatch):
        monkeypatch.setenv("HERDR_PANE_ID", "w:p1")
        layout = _layout(_pane("w:p1", 0, 0, 160, 48))

        def run(cmd, **kwargs):
            if cmd[1:3] == ["pane", "split"]:
                return MagicMock(returncode=1, stdout="", stderr="boom")
            return _fake_herdr(layout, [])[0](cmd)

        with patch("subprocess.run", side_effect=run):
            try:
                hl.split_pane(Path("/repo"))
            except RuntimeError as err:
                assert "boom" in str(err)
            else:
                raise AssertionError("RuntimeError expected")


class TestMarkOrchestrator:
    def test_renames_current_pane(self, monkeypatch):
        monkeypatch.setenv("HERDR_PANE_ID", "w:p1")
        run, calls = _fake_herdr(_layout(), [])
        with patch("subprocess.run", side_effect=run):
            hl.mark_orchestrator()
        assert ["pane", "rename", "w:p1", hl.ORCHESTRATOR_LABEL] in calls

"""herdr_layout.py 테스트 — 탭1은 오케스트레이션+리캡, 작업 pane은 "작업 N" 탭에 최대 8개씩 격자로."""

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


def _tab(tab_id, label, pane_count, number=1):
    return {"tab_id": tab_id, "label": label, "pane_count": pane_count, "number": number}


class TestChooseGridSplit:
    def test_wide_pane_is_split_right(self):
        assert hl.choose_grid_split(_layout(_pane("a", 0, 0, 160, 48))) == ("a", "right")

    def test_tall_pane_is_split_down(self):
        layout = _layout(_pane("a", 0, 0, 80, 48), _pane("b", 80, 0, 80, 48))
        assert hl.choose_grid_split(layout)[1] == "down"

    def test_picks_largest_pane(self):
        layout = _layout(
            _pane("a", 0, 0, 80, 24),
            _pane("b", 0, 24, 80, 24),
            _pane("c", 80, 0, 80, 48),
        )
        assert hl.choose_grid_split(layout) == ("c", "down")

    def test_eight_panes_form_a_grid(self):
        panes = [_pane("p0", 0, 0, 160, 48)]
        for n in range(1, hl.MAX_PANES_PER_TAB):
            target, direction = hl.choose_grid_split(_layout(*panes))
            rect = next(p["rect"] for p in panes if p["pane_id"] == target)
            x, y, w, h = rect["x"], rect["y"], rect["width"], rect["height"]
            if direction == "right":
                rect["width"] = w // 2
                panes.append(_pane(f"p{n}", x + w // 2, y, w - w // 2, h))
            else:
                rect["height"] = h // 2
                panes.append(_pane(f"p{n}", x, y + h // 2, w, h - h // 2))
        sizes = {(p["rect"]["width"], p["rect"]["height"]) for p in panes}
        assert sizes == {(40, 24)}


class TestPickWorkerTab:
    def test_reuses_worker_tab_with_room(self):
        tabs = [_tab("w:t1", "1", 2), _tab("w:t2", "작업 1", 3, 2)]
        assert hl.pick_worker_tab(tabs) == "w:t2"

    def test_skips_full_worker_tab(self):
        tabs = [_tab("w:t2", "작업 1", hl.MAX_PANES_PER_TAB, 2), _tab("w:t3", "작업 2", 1, 3)]
        assert hl.pick_worker_tab(tabs) == "w:t3"

    def test_none_when_all_full_or_missing(self):
        tabs = [_tab("w:t1", "1", 2), _tab("w:t2", "작업 1", hl.MAX_PANES_PER_TAB, 2)]
        assert hl.pick_worker_tab(tabs) is None

    def test_next_label_counts_worker_tabs(self):
        tabs = [_tab("w:t1", "1", 2), _tab("w:t2", "작업 1", 8, 2)]
        assert hl.next_worker_label(tabs) == "작업 2"


class TestLatestReview:
    def test_picks_most_recent_review_file(self, tmp_path):
        import os
        old = tmp_path / ".review" / "latest.json"
        new = tmp_path / "phases" / "0-mvp" / "review.json"
        for path, mtime in ((old, 100), (new, 200)):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"verdict": str(mtime)}))
            os.utime(path, (mtime, mtime))
        rel, review = hl.latest_review(tmp_path)
        assert rel == "phases/0-mvp/review.json" and review["verdict"] == "200"

    def test_none_without_reviews(self, tmp_path):
        assert hl.latest_review(tmp_path) is None


class TestRenderRecap:
    def test_lists_worker_panes_by_tab_and_skips_own_tab(self):
        tabs = [_tab("w:t1", "1", 2), _tab("w:t2", "작업 1", 2, 2)]
        panes = [
            {"pane_id": "w:p1", "tab_id": "w:t1", "label": hl.ORCHESTRATOR_LABEL, "agent_status": "working"},
            {"pane_id": "w:p3", "tab_id": "w:t2", "label": "리뷰:codex", "agent_status": "working"},
            {"pane_id": "w:p4", "tab_id": "w:t2", "terminal_title_stripped": "step 2", "agent_status": "idle"},
        ]
        text = hl.render_recap(tabs, panes, own_tab="w:t1", phases=None)
        assert "작업 1" in text and "리뷰:codex" in text and "step 2" in text
        assert hl.ORCHESTRATOR_LABEL not in text

    def test_shows_phase_status(self):
        phases = {"phases": [{"dir": "0-mvp", "status": "completed"}, {"dir": "1-api", "status": "pending"}]}
        text = hl.render_recap([], [], own_tab="w:t1", phases=phases)
        assert "0-mvp" in text and "completed" in text and "1-api" in text

    def test_shows_review_summary(self):
        review = {
            "verdict": "patch is incorrect", "risk": {"score": 40, "level": "high"}, "blocking": 1,
            "failed": {"codex": "timeout"},
            "findings": [{"priority": 1, "pre_existing": False, "file": "src/a.ts", "line_start": 3,
                          "title": "널 체크 누락", "found_by": ["claude", "codex"]}],
        }
        text = hl.render_recap([], [], own_tab="w:t1", phases=None, review=("phases/0-mvp/review.json", review))
        for part in ("phases/0-mvp/review.json", "patch is incorrect", "high", "차단 1", "codex",
                     "P1 src/a.ts:3 널 체크 누락", "claude+codex"):
            assert part in text

    def test_review_shows_at_most_five_findings(self):
        findings = [{"priority": 2, "pre_existing": False, "file": "f.py", "line_start": n,
                     "title": f"t{n}", "found_by": ["claude"]} for n in range(8)]
        review = {"verdict": "patch is correct", "risk": {"score": 5, "level": "low"}, "blocking": 0,
                  "failed": {}, "findings": findings}
        text = hl.render_recap([], [], own_tab="w:t1", phases=None, review=("r.json", review))
        assert "f.py:4 t4" in text and "f.py:5 t5" not in text and "외 3건" in text

    def test_needs_decision_section_comes_first(self):
        panes = [{"pane_id": "w:p3", "tab_id": "w:t2", "label": "phase:1-api", "agent_status": "blocked"}]
        phases = {"phases": [{"dir": "0-mvp", "status": "error"}, {"dir": "1-api", "status": "completed"}]}
        review = {"verdict": "patch is incorrect", "risk": {"score": 40, "level": "high"}, "blocking": 2,
                  "failed": {"codex": "timeout"}, "findings": []}
        text = hl.render_recap([_tab("w:t2", "작업 1", 1, 2)], panes, own_tab="w:t1", phases=phases,
                               review=("r.json", review))
        lines = text.splitlines()
        assert lines[1] == "⚠ 사람이 정해야 해요:"
        section = "\n".join(lines[1:lines.index("", 2)])
        for part in ("phase:1-api", "0-mvp", "오류로 멈췄어요", "문제 2건", "codex"):
            assert part in section
        assert "1-api" not in section.replace("phase:1-api", "")

    def test_no_decision_section_when_nothing_pending(self):
        phases = {"phases": [{"dir": "0-mvp", "status": "completed"}]}
        assert "사람이 정해야" not in hl.render_recap([], [], own_tab="w:t1", phases=phases)

    def test_story_comes_first(self):
        text = hl.render_recap([], [], own_tab="w:t1", phases=None, story=["아직 시작한 작업이 없어요."])
        lines = text.splitlines()
        assert lines[1] == "지금 무슨 일이 일어나고 있나요" and "아직 시작한 작업" in lines[2]

    def test_unconfirmed_finding_marked(self):
        review = {"verdict": "patch is correct", "risk": {"score": 5, "level": "low"}, "blocking": 0, "failed": {},
                  "findings": [{"priority": 1, "pre_existing": False, "confirmed": False, "file": "a.py",
                                "line_start": 1, "title": "t", "found_by": ["codex"]}]}
        assert "(미확인)" in hl.render_recap([], [], own_tab="w:t1", phases=None, review=("r.json", review))

    def test_empty_state_message(self):
        assert "병렬 작업 없음" in hl.render_recap([], [], own_tab="w:t1", phases=None)


def _fake_herdr(tabs, layout=None, panes=None):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd[1:])
        key = cmd[1:3]
        if key == ["tab", "list"]:
            out = {"tabs": tabs}
        elif key == ["tab", "create"]:
            out = {"tab": {"tab_id": "w:t9"}, "root_pane": {"pane_id": "w:p20"}}
        elif key == ["pane", "layout"]:
            out = {"layout": layout}
        elif key == ["pane", "split"]:
            out = {"pane": {"pane_id": "w:p21"}}
        elif key == ["pane", "list"]:
            out = {"panes": panes or []}
        else:
            out = {}
        return MagicMock(returncode=0, stdout=json.dumps({"result": out}), stderr="")

    return run, calls


class TestSplitPane:
    def _env(self, monkeypatch):
        monkeypatch.setenv("HERDR_PANE_ID", "w:p1")
        monkeypatch.setenv("HERDR_WORKSPACE_ID", "w")

    def test_creates_worker_tab_when_none_has_room(self, monkeypatch):
        self._env(monkeypatch)
        run, calls = _fake_herdr([_tab("w:t1", "1", 2)])
        with patch("subprocess.run", side_effect=run):
            new_id = hl.split_pane(Path("/repo"), label="리뷰:claude")

        assert new_id == "w:p20"
        create = next(c for c in calls if c[:2] == ["tab", "create"])
        assert create[create.index("--label") + 1] == "작업 1"
        assert "--no-focus" in create
        assert ["pane", "rename", "w:p20", "리뷰:claude"] in calls

    def test_splits_inside_worker_tab_with_room(self, monkeypatch):
        self._env(monkeypatch)
        tabs = [_tab("w:t1", "1", 2), _tab("w:t2", "작업 1", 1, 2)]
        layout = _layout(_pane("w:p5", 0, 0, 160, 48))
        panes = [{"pane_id": "w:p5", "tab_id": "w:t2"}]
        run, calls = _fake_herdr(tabs, layout, panes)
        with patch("subprocess.run", side_effect=run):
            new_id = hl.split_pane(Path("/repo"))

        assert new_id == "w:p21"
        split = next(c for c in calls if c[:2] == ["pane", "split"])
        assert split[2] == "w:p5" and "--no-focus" in split
        assert not any(c[:2] == ["tab", "create"] for c in calls)

    def test_failure_raises_with_stderr(self, monkeypatch):
        self._env(monkeypatch)

        def run(cmd, **kwargs):
            return MagicMock(returncode=1, stdout="", stderr="boom")

        with patch("subprocess.run", side_effect=run):
            try:
                hl.split_pane(Path("/repo"))
            except RuntimeError as err:
                assert "boom" in str(err)
            else:
                raise AssertionError("RuntimeError expected")


class TestSetup:
    def test_renames_and_opens_recap_once(self, monkeypatch):
        monkeypatch.setenv("HERDR_PANE_ID", "w:p1")
        monkeypatch.setenv("HERDR_WORKSPACE_ID", "w")
        monkeypatch.setenv("HERDR_TAB_ID", "w:t1")
        run, calls = _fake_herdr([], panes=[{"pane_id": "w:p1", "tab_id": "w:t1"}])
        with patch("subprocess.run", side_effect=run):
            hl.setup()

        assert ["pane", "rename", "w:p1", hl.ORCHESTRATOR_LABEL] in calls
        split = next(c for c in calls if c[:2] == ["pane", "split"])
        assert split[2] == "w:p1" and split[split.index("--direction") + 1] == "right"
        assert ["pane", "rename", "w:p21", hl.RECAP_LABEL] in calls
        assert any(c[:2] == ["pane", "run"] and "recap" in c[3] for c in calls)

    def test_skips_recap_when_already_open(self, monkeypatch):
        monkeypatch.setenv("HERDR_PANE_ID", "w:p1")
        monkeypatch.setenv("HERDR_WORKSPACE_ID", "w")
        monkeypatch.setenv("HERDR_TAB_ID", "w:t1")
        panes = [{"pane_id": "w:p1", "tab_id": "w:t1"},
                 {"pane_id": "w:p2", "tab_id": "w:t1", "label": hl.RECAP_LABEL}]
        run, calls = _fake_herdr([], panes=panes)
        with patch("subprocess.run", side_effect=run):
            hl.setup()

        assert not any(c[:2] == ["pane", "split"] for c in calls)


class TestRecapText:
    def test_error_is_shown_instead_of_killing_recap(self):
        # review.py가 latest.json을 쓰는 중이거나 herdr 호출이 잠깐 실패해도 리캡 pane은 살아 있어야 한다.
        with patch.object(hl, "_herdr_json", side_effect=RuntimeError("herdr down")):
            text = hl.recap_text("w1", "t1")
        assert "리캡 갱신 실패" in text and "herdr down" in text


class TestBuildStory:
    def test_reads_active_phase_status_and_lessons(self, tmp_path, monkeypatch):
        (tmp_path / "phases" / "1-api").mkdir(parents=True)
        (tmp_path / "phases" / "1-api" / "index.json").write_text(json.dumps(
            {"phase": "api", "steps": [{"step": 0, "name": "routes", "status": "pending"}]}))
        lessons = tmp_path / "LESSONS.md"
        lessons.write_text("\n".join(f"- 규칙 {n}" for n in range(60)))
        monkeypatch.setattr(hl, "ROOT", tmp_path)
        monkeypatch.setattr(hl, "REVIEW_STATUS", tmp_path / "status.json")
        monkeypatch.setattr(hl, "LESSONS_FILE", lessons)
        story = hl.build_story({"phases": [{"dir": "1-api", "status": "pending"}]}, None)
        assert "'routes'" in story[0]
        assert story[-1].startswith("⚠ 실수 기록장")


class TestWorktrees:
    """병렬 phase는 다른 worktree에서 돌고 상태 파일도 그쪽에 쓴다. 리캡 하나에 모아 보여 준다."""

    @staticmethod
    def _worktree(path, status="pending"):
        (path / "phases" / "1-api").mkdir(parents=True)
        (path / "phases" / "index.json").write_text(json.dumps({"phases": [{"dir": "1-api", "status": status}]}))
        (path / "phases" / "1-api" / "index.json").write_text(json.dumps(
            {"phase": "api", "blocked_reason": "review: 차단 1건",
             "steps": [{"step": 0, "name": "routes", "status": "pending"}]}))

    def _load(self, tmp_path, status="pending"):
        main, wt, bare = tmp_path / "main", tmp_path / "wt-api", tmp_path / "wt-empty"
        for p in (main, wt, bare):
            p.mkdir()
        self._worktree(wt, status)
        porcelain = (f"worktree {main}\nHEAD a\nbranch refs/heads/main\n\n"
                     f"worktree {wt}\nHEAD b\nbranch refs/heads/feat-api\n\n"
                     f"worktree {bare}\nHEAD c\ndetached\n\n")
        done = MagicMock(returncode=0, stdout=porcelain)
        with patch.object(hl.subprocess, "run", return_value=done):
            return hl.load_worktrees(main)

    def test_lists_other_worktrees_with_phases_by_branch(self, tmp_path):
        worktrees = self._load(tmp_path)
        assert [w["name"] for w in worktrees] == ["feat-api"]

    def test_story_shows_each_worktree_phase(self, tmp_path):
        story = hl.worktree_story(self._load(tmp_path), hl.datetime.now().astimezone())
        assert story[0].startswith("[feat-api]") and "'routes'" in story[0]

    def test_blocked_worktree_phase_needs_decision(self, tmp_path):
        text = hl.render_recap([], [], "t1", None, worktrees=self._load(tmp_path, status="blocked"))
        decisions = text[text.index("⚠ 사람이 정해야 해요"):text.index("── 자세히")]
        assert "[feat-api]" in decisions and "1-api" in decisions

    def test_worktree_shows_the_phase_its_branch_runs(self, tmp_path):
        # 병렬 worktree에는 top index 전체가 복제된다. 첫 미완료가 아니라 브랜치(feat-<phase>)의 phase를 보여 준다.
        main, wt, other = tmp_path / "main", tmp_path / "wt-ui", tmp_path / "wt-old"
        for p in (main, wt, other):
            p.mkdir()
            self._worktree(p)
            (p / "phases" / "2-ui").mkdir()
            (p / "phases" / "2-ui" / "index.json").write_text(json.dumps(
                {"phase": "ui", "steps": [{"step": 0, "name": "screens", "status": "pending"}]}))
            (p / "phases" / "index.json").write_text(json.dumps(
                {"phases": [{"dir": "1-api", "status": "blocked"}, {"dir": "2-ui", "status": "pending"}]}))
        porcelain = (f"worktree {main}\nbranch refs/heads/main\n\n"
                     f"worktree {wt}\nbranch refs/heads/feat-ui\n\n"
                     f"worktree {other}\nbranch refs/heads/spike\n\n")
        with patch.object(hl.subprocess, "run", return_value=MagicMock(returncode=0, stdout=porcelain)):
            worktrees = hl.load_worktrees(main)
        assert [w["name"] for w in worktrees] == ["feat-ui"]
        story = hl.worktree_story(worktrees, hl.datetime.now().astimezone())
        assert "'screens'" in story[0] and "'routes'" not in " ".join(story)
        # main에서 이미 보이는 1-api의 멈춤을 worktree 머리말로 중복해 내지 않는다.
        assert "1-api" not in hl.render_recap([], [], "t1", None, worktrees=worktrees)

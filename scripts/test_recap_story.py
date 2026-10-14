"""recap_story.py 테스트 — 진행 상태가 전문용어 없는 문장으로 바뀌는지 본다."""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import recap_story as rs  # noqa: E402

NOW = datetime.strptime("2026-10-14T12:20:00+0900", "%Y-%m-%dT%H:%M:%S%z")
PHASES = {"phases": [{"dir": "0-setup", "status": "completed"}, {"dir": "1-http-engine", "status": "pending"}]}


def _index(done, total=5, current_started="2026-10-14T12:08:00+0900", **extra):
    steps = []
    for n in range(total):
        step = {"step": n, "name": f"step-{n}", "status": "completed" if n < done else "pending",
                "started_at": "2026-10-14T10:00:00+0900"}
        if n < done:
            step["completed_at"] = "2026-10-14T12:10:00+0900"
        steps.append(step)
    if done < total:
        steps[done]["started_at"] = current_started
        steps[done]["name"] = "retry-policy"
    return {"phase": "http-engine", "steps": steps, **extra}


def _review(blocking=0, score=12, axes=None, title="파일 경로에 공백이 있으면 저장이 실패해요"):
    findings = [{"priority": 1, "pre_existing": False, "title": title}] * blocking
    return {"blocking": blocking, "findings": findings,
            "risk": {"score": score, "level": "low", "axes": axes or {}}}


def test_nothing_started():
    assert rs.explain(None, None, None, None, NOW) == ["아직 시작한 작업이 없어요."]


def test_progress_sentence_names_step_and_elapsed_time():
    (line,) = rs.explain(PHASES, _index(done=3), None, None, NOW)
    assert "5단계로 나눠" in line and "3단계까지 끝났고" in line
    assert "4단계 'retry-policy'" in line and "12분째" in line


def test_reviewing_with_two_reviewers_explains_cross_check():
    status = {"state": "running", "mode": "first", "reviewers": ["claude", "codex"],
              "started_at": "2026-10-14T12:12:00+0900"}
    lines = rs.explain(PHASES, _index(done=5), None, status, NOW)
    assert "모두 만들었어요" in lines[0]
    assert "AI 검사관 2명(claude, codex)" in lines[1] and "8분째" in lines[1]
    assert "놓친 문제" in lines[2]


def test_verify_review_says_only_fixes_are_rechecked():
    status = {"state": "running", "mode": "verify", "reviewers": ["codex"], "started_at": "2026-10-14T12:19:00+0900"}
    assert "고친 부분만" in rs.explain(PHASES, _index(done=5), None, status, NOW)[-1]


def test_blocking_findings_give_count_and_example():
    lines = rs.explain(PHASES, _index(done=5), _review(blocking=2), {"state": "done"}, NOW)
    assert "꼭 고쳐야 할 문제 2개" in lines[1] and "공백" in lines[1]
    assert "고친 부분만 다시 검사" in lines[2]


def test_passed_review_explains_risk_in_plain_words():
    review = _review(score=30, axes={"security": 25, "scope": 5})
    line = rs.explain(PHASES, _index(done=5), review, {"state": "done"}, NOW)[-1]
    assert "검사 통과" in line and "'낮음'(30점)" in line and "보안" in line


def test_completed_phase_reports_build_and_review_time():
    phases = {"phases": [{"dir": "1-http-engine", "status": "completed"}]}
    index = _index(done=5, completed_at="2026-10-14T12:35:00+0900")
    line = rs.explain(phases, index, _review(), None, NOW)[0]
    assert line.startswith("완료!") and "만들기 2시간 10분" in line and "검사 25분" in line


def test_blocked_phase_asks_for_a_person():
    phases = {"phases": [{"dir": "1-http-engine", "status": "blocked"}]}
    line = rs.explain(phases, _index(done=5, blocked_reason="API 키가 없어요"), None, None, NOW)[0]
    assert line.startswith("⚠") and "API 키가 없어요" in line and "사람" in line


def test_lessons_warning_only_when_too_long():
    assert rs.lessons_warning("# LESSONS\n- [2026-10-14] 짧은 규칙\n") is None
    assert "51줄" in rs.lessons_warning("\n".join(f"- 규칙 {n}" for n in range(51)))
    assert "너무 긴 줄 1개" in rs.lessons_warning("- " + "가" * 400)

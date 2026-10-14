"""
리캡의 "지금 무슨 일이 일어나고 있나요" 절 — 진행 상태를 전문용어 없이 문장으로 바꾼다.

입력은 모두 이미 읽어 둔 값(phase index, 리뷰 결과, 리뷰 진행 상태)이고, 화면에 쓸 줄 목록을 돌려준다.
"""

from datetime import datetime
from typing import Optional

LESSONS_MAX_LINES = 50
LESSONS_MAX_LINE_CHARS = 300
RISK_WORDS = {"low": "낮음", "medium": "보통", "high": "높음", "critical": "매우 높음"}
AXIS_WORDS = {
    "security": "보안과 관련된 코드가 바뀌었어요",
    "scope": "바뀐 양이 많아요",
    "breaking": "다른 코드가 쓰는 부분(공개 함수·설정 등)이 바뀌었어요",
    "tests": "테스트가 부족해요",
    "migration": "데이터 구조(DB 등)가 바뀌었어요",
}
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60


def _parse(stamp: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z") if stamp else None
    except ValueError:
        return datetime.fromisoformat(stamp)


def duration(start: Optional[str], end: Optional[datetime]) -> str:
    """'2시간 10분', '8분'처럼 사람이 읽는 경과 시간. 알 수 없으면 빈 문자열."""
    begin = _parse(start)
    if begin is None or end is None:
        return ""
    minutes = max(int((end - begin).total_seconds() // SECONDS_PER_MINUTE), 0)
    hours, minutes = divmod(minutes, MINUTES_PER_HOUR)
    return f"{hours}시간 {minutes}분" if hours else f"{minutes}분"


def _active_phase(phases: dict) -> Optional[dict]:
    items = phases.get("phases", [])
    return next((p for p in items if p.get("status") != "completed"), items[-1] if items else None)


def _risk_sentence(review: dict) -> str:
    risk = review.get("risk", {})
    axes = {k: v for k, v in risk.get("axes", {}).items() if v}
    reason = AXIS_WORDS.get(max(axes, key=axes.get), "") if axes else "바뀐 범위가 작고 위험한 부분이 없어요"
    return f"위험도 '{RISK_WORDS.get(risk.get('level'), risk.get('level'))}'({risk.get('score')}점): {reason}."


def _review_sentences(review: Optional[dict], status: Optional[dict], now: datetime, in_phase: bool = True) -> list:
    if status and status.get("state") == "running":
        names = status.get("reviewers", [])
        line = f"다 만든 코드를 AI 검사관 {len(names)}명({', '.join(names)})이 살펴보는 중이에요"
        line += f"({duration(status.get('started_at'), now)}째)."
        if status.get("mode") == "verify":
            return [line, "지난번에 찾은 문제가 고쳐졌는지, 고친 부분만 다시 확인하고 있어요."]
        if len(names) > 1:
            return [line, "서로 다른 회사의 AI라서 한쪽이 놓친 문제를 다른 쪽이 잡아 줘요."]
        return [line, "바뀐 양이 적어서 검사관 1명이 봐요."]
    if not review:
        return []
    blocking = [f for f in review.get("findings", []) if f.get("priority", 3) <= 1 and not f.get("pre_existing")]
    if review.get("blocking"):
        example = f" 예: '{blocking[0]['title']}'." if blocking else ""
        found = f"검사관이 꼭 고쳐야 할 문제 {review['blocking']}개를 찾았어요.{example}"
        # phase 안에서는 AI가 고치고 다시 검사한다. push·/review 리뷰는 사람이 고친다.
        follow = "AI가 고치고 나면 고친 부분만 다시 검사해요(최대 2번)." if in_phase else "고친 뒤 다시 올리면 다시 검사해요."
        return [found, follow]
    return [f"검사 통과! {_risk_sentence(review)}"]


def _steps_sentence(index: dict, now: datetime) -> str:
    steps = index.get("steps", [])
    done = sum(1 for s in steps if s.get("status") == "completed")
    name = index.get("phase", "")
    current = next((s for s in steps if s.get("status") != "completed"), None)
    if current is None:
        return f"기능 '{name}'의 {len(steps)}단계를 모두 만들었어요."
    elapsed = duration(current.get("started_at"), now)
    doing = f"지금 {current['step'] + 1}단계 '{current['name']}'을(를) 만드는 중이에요" + (f"({elapsed}째)" if elapsed else "")
    return f"기능 '{name}'을(를) {len(steps)}단계로 나눠 만들고 있어요. {done}단계까지 끝났고, {doing}."


def _phase_sentences(phase: dict, index: dict, now: datetime) -> list:
    status = phase.get("status")
    steps = index.get("steps", [])
    if status == "completed":
        build = duration(steps[0].get("started_at"), _parse(steps[-1].get("completed_at"))) if steps else ""
        check = duration(steps[-1].get("completed_at"), _parse(index.get("completed_at"))) if steps else ""
        times = f" 걸린 시간: 만들기 {build or '-'}, 검사 {check or '-'}." if build or check else ""
        return [f"완료! 기능 '{index.get('phase', phase.get('dir'))}'을(를) {len(steps)}단계로 만들고 검사를 통과했어요.{times}"]
    if status == "blocked":
        return [f"⚠ 멈췄어요: {index.get('blocked_reason') or '이유가 기록되지 않았어요'}. 사람이 확인해야 해요."]
    if status == "error":
        failed = next((s for s in steps if s.get("status") == "error"), {})
        return [f"⚠ {failed.get('step', 0) + 1}단계에서 오류가 나서 멈췄어요: {failed.get('error_message', '')}"]
    return [_steps_sentence(index, now)]


def lessons_warning(text: str) -> Optional[str]:
    """LESSONS가 너무 길어지면 정리를 권한다(매 세션 비용이 커지고 규칙이 묻힌다)."""
    rules = [line for line in text.splitlines() if line.startswith("- ")]
    long_rules = [line for line in rules if len(line) > LESSONS_MAX_LINE_CHARS]
    if len(rules) <= LESSONS_MAX_LINES and not long_rules:
        return None
    return (f"실수 기록장(docs/LESSONS.md)이 너무 길어요(규칙 {len(rules)}줄, 너무 긴 줄 {len(long_rules)}개). "
            "합치거나 CLAUDE.md '함정'으로 옮겨 주세요.")


def explain(phases: Optional[dict], index: Optional[dict], review: Optional[dict], status: Optional[dict],
            now: datetime) -> list:
    """리캡 맨 위에 쓸 쉬운 설명 줄 목록."""
    phase = _active_phase(phases or {})
    if phase is None:
        # pre-push나 /review처럼 phase 밖에서 도는 리뷰도 보여 준다.
        return _review_sentences(review, status, now, in_phase=False) or ["아직 시작한 작업이 없어요."]
    lines = _phase_sentences(phase, index or {}, now)
    is_reviewing = bool(status and status.get("state") == "running")
    if phase.get("status") != "completed" or is_reviewing:
        lines += _review_sentences(review, status, now)
    elif review:
        lines.append(_risk_sentence(review))
    return lines

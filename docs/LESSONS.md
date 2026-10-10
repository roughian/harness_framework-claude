# LESSONS — 실수에서 얻은 규칙

<!--
매 세션 자동 로드된다(CLAUDE.md @import). 50줄 이하로 유지한다.
형식(1항목 1줄): - [YYYY-MM-DD] 규칙(하라/하지 마라). 원인: … (출처: phase/step | review | session)
예: - [2026-10-10] Claude Code 훅은 stdin JSON을 읽고 차단은 permissionDecision "deny" 또는 exit 2로 한다. 원인: $CLAUDE_TOOL_INPUT·exit 1로 가드가 무력했음 (출처: session)
같은 실수 2회 → CLAUDE.md CRITICAL로 승격 후 여기서 삭제.
-->

- [2026-10-10] Claude Code 훅은 stdin JSON(`.tool_input.*`)을 읽고, 차단은 `permissionDecision: "deny"` 또는 exit 2로 한다. 원인: 존재하지 않는 `$CLAUDE_TOOL_INPUT`과 exit 1(비차단)로 위험 명령 가드가 무력했음 (출처: session)
- [2026-10-10] `git add -A`로 커밋하는 코드를 넣기 전에 생성물(`__pycache__/`, 캐시, 작업 파일)이 .gitignore에 있는지 확인한다. 원인: 리뷰 수정 커밋에 __pycache__가 섞일 뻔함 (출처: review)
- [2026-10-10] 실행을 재개하는 상태(기준 브랜치 등)는 메모리 기본값이 아니라 index.json에 저장해 재실행 때 복원한다. 원인: 재실행 시 리뷰 base가 main으로 바뀜 (출처: review)

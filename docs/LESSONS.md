# LESSONS — 실수에서 얻은 규칙

<!--
설계할 때 오케스트레이터가 읽고, 관련 줄을 step 파일 금지사항에 옮긴다(매 세션 로드하지 않는다). 50줄 이하로 유지한다.
형식(1항목 1줄, 300자 이하, 1줄 1규칙): - [YYYY-MM-DD] 규칙(하라/하지 마라). 원인: … (출처: phase/step | review | session)
예: - [2026-10-10] Claude Code 훅은 stdin JSON을 읽고 차단은 permissionDecision "deny" 또는 exit 2로 한다. 원인: $CLAUDE_TOOL_INPUT·exit 1로 가드가 무력했음 (출처: session)
같은 실수 2회 → CLAUDE.md `## 함정`으로 옮기고(이유 포함) 여기서 삭제.
-->

- [2026-10-10] Claude Code 훅은 stdin JSON(`.tool_input.*`)을 읽고, 차단은 `permissionDecision: "deny"` 또는 exit 2로 한다. 원인: 존재하지 않는 `$CLAUDE_TOOL_INPUT`과 exit 1(비차단)로 위험 명령 가드가 무력했음 (출처: session)
- [2026-10-10] `git add -A`로 커밋하는 코드를 넣기 전에 생성물(`__pycache__/`, 캐시, 작업 파일)이 .gitignore에 있는지 확인한다. 원인: 리뷰 수정 커밋에 __pycache__가 섞일 뻔함 (출처: review)
- [2026-10-10] 실행을 재개하는 상태(기준 브랜치 등)는 메모리 기본값이 아니라 index.json에 저장해 재실행 때 복원한다. 원인: 재실행 시 리뷰 base가 main으로 바뀜 (출처: review)
- [2026-10-12] `gh api --paginate` 결과는 `--jq`로 한 줄씩 뽑아 쓴다. 원인: 페이지마다 배열이 따로 출력되어, 코멘트가 30개를 넘으면 `json.loads`가 실패함 (출처: session)
- [2026-10-13] push·phase를 막는 게이트를 넣을 때는 같은 명령을 쓰는 자동화 경로(execute.py push 등)에 건너뛰기 경로를 함께 둔다. 원인: 새 리뷰 게이트가 자동화 커밋을 막아 phase 실행이 실패할 뻔함 (출처: review)
- [2026-10-14] 로컬 리뷰 게이트는 실수로 건너뛰는 것만 막고(의도적 우회는 범위 밖, 진짜 강제는 PR check), 리뷰는 첫 리뷰 뒤 수정분만 검증한다. 원인: 우회 방지를 계속 보강하자 리뷰가 매번 새 지적 10건을 내며 수렴하지 않았고, 매번 전체 재리뷰로 phase 시간의 절반이 리뷰에 들었다 (출처: session)

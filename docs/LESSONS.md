# LESSONS — 실수에서 얻은 규칙

<!--
줄 끝 [키: …]가 step 파일이나 리뷰 diff의 경로·글에 들어 있으면 execute.py와 review.py가 그 줄을 worker·리뷰어 프롬프트에 자동으로 붙인다(매 세션 로드하지 않는다). 50줄 이하로 유지한다.
형식(1항목 1줄, 300자 이하, 1줄 1규칙): - [YYYY-MM-DD] 규칙(하라/하지 마라). 원인: … (출처: phase/step | review | session) [키: 관련 경로 앞부분이나 단어, …]
예: - [2026-10-10] Claude Code 훅은 stdin JSON을 읽고 차단은 permissionDecision "deny" 또는 exit 2로 한다. 원인: $CLAUDE_TOOL_INPUT·exit 1로 가드가 무력했음 (출처: session) [키: scripts/hooks]
같은 실수 2회 → 먼저 테스트·훅·검사로 막고 여기서 삭제. 막을 수 없는 판단 규칙만 CLAUDE.md `## 함정`으로 옮긴다.
-->

- [2026-10-10] Claude Code 훅은 stdin JSON(`.tool_input.*`)을 읽고, 차단은 `permissionDecision: "deny"` 또는 exit 2로 한다. 원인: 존재하지 않는 `$CLAUDE_TOOL_INPUT`과 exit 1(비차단)로 위험 명령 가드가 무력했음 (출처: session) [키: scripts/hooks, .claude/settings]
- [2026-10-10] `git add -A`로 커밋하는 코드를 넣기 전에 생성물(`__pycache__/`, 캐시, 작업 파일)이 .gitignore에 있는지 확인한다. 원인: 리뷰 수정 커밋에 __pycache__가 섞일 뻔함 (출처: review) [키: git add, .gitignore]
- [2026-10-19] 실행을 재개하거나 비교 기준이 되는 상태(리뷰 base, 고정 AC 기준 커밋 등)는 메모리나 실행 시점 HEAD가 아니라 phase 첫 실행 때 index.json에 setdefault로 저장해 재실행 때 복원한다. 원인: 재실행 시 리뷰 base가 main으로 바뀜, AC 기준을 실행마다 HEAD로 잡아 커밋된 AC 수정이 검출되지 않음, 병렬 worktree가 base를 고정하지 않아 먼저 merge된 phase가 역 diff로 보임 (출처: review ×3) [키: scripts/execute.py, scripts/run_phases.py, index.json]
- [2026-10-12] `gh api --paginate` 결과는 `--jq`로 한 줄씩 뽑아 쓴다. 원인: 페이지마다 배열이 따로 출력되어, 코멘트가 30개를 넘으면 `json.loads`가 실패함 (출처: session) [키: gh api, scripts/pr_review.py]
- [2026-10-13] push·phase를 막는 게이트를 넣을 때는 같은 명령을 쓰는 자동화 경로(execute.py push 등)에 건너뛰기 경로를 함께 둔다. 원인: 새 리뷰 게이트가 자동화 커밋을 막아 phase 실행이 실패할 뻔함 (출처: review) [키: .githooks/, scripts/git_review_gate.py]
- [2026-10-14] 로컬 리뷰 게이트는 실수로 건너뛰는 것만 막고(의도적 우회는 범위 밖, AI Review 체크는 표시용), 리뷰는 첫 리뷰 뒤 수정분만 검증한다. 원인: 우회 방지를 계속 보강하자 리뷰가 매번 새 지적 10건을 내며 수렴하지 않았고, 매번 전체 재리뷰로 phase 시간의 절반이 리뷰에 들었다 (출처: session) [키: scripts/review.py, scripts/git_review_gate.py, .githooks/]
- [2026-10-19] `claude -p`를 읽기 전용으로 돌릴 때는 `--allowedTools`만 믿지 말고 `--tools`(쓰기 도구 제거)와 `--permission-mode dontAsk`를 함께 준다. 원인: 사용자 설정이 auto 모드라 리뷰어가 허용 목록 밖의 pytest 실행·임시 폴더 생성과 삭제까지 시도함 (출처: session) [키: claude -p, allowedTools, scripts/review.py]
- [2026-10-19] git diff를 해시·비교할 때는 `text=True` 없이 bytes 그대로 다루고 `git patch-id`는 `--verbatim`으로 쓴다. 원인: 비 UTF-8 파일에서 UnicodeDecodeError로 push가 막히고, 기본 patch-id가 공백을 지워 들여쓰기만 바뀐 코드도 같은 diff로 봄 (출처: review) [키: patch-id, git diff, scripts/review.py]
- [2026-10-19] CLI 출력 문구를 판정할 때는 구조화된 필드(api_error_status 등)를 먼저 보고, 문구는 짐작한 예시가 아니라 실제 바이너리 문자열(`strings`)로 테스트한다. 원인: 한도 감지가 실제 문구 "You've hit your session limit"을 놓쳐 worker가 기다리지 않고 실패할 뻔함 (출처: session) [키: LIMIT_PATTERN, is_usage_limited]
- [2026-10-20] 다른 프로젝트의 기능을 옮길 때는 대화 기록이 아니라 원본 코드와 대조한다. 원인: 기록만 보고 만든 한도 대기가 끝낸 step 재실행·수정 세션 대기 누락을 빠뜨림 (출처: session) [키: _wait_out_limit, scripts/execute.py]
- [2026-10-28] '실행 중' 판정은 남은 흔적(worktree·브랜치·락 파일)만 세지 말고 완료 상태로 거른다. 원인: merge된 phase의 worktree가 동시 실행 상한을 계속 차지해 대기 phase가 시작되지 않음 (출처: review) [키: scripts/run_phases.py, worktree]

#!/bin/bash
# Dangerous Command Guard — PreToolUse[Bash]
# rm -rf, git push --force, git reset --hard, DROP TABLE, --no-verify(리뷰 훅 우회) 등 위험 명령어 차단.
# 로컬 게이트는 실수 방지용이다. 셸을 가진 행위자의 의도적 우회는 막지 않는다(진짜 강제는 required로 건 상태 체크 AI Review).

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ -z "$COMMAND" ]; then
  exit 0
fi

if echo "$COMMAND" | grep -qE 'rm\s+-rf|git\s+push\s+--force|git\s+reset\s+--hard|DROP\s+TABLE|git\s.*--no-verify'; then
  cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "위험한 명령어가 감지되었습니다. rm -rf, git push --force, git reset --hard, DROP TABLE, git --no-verify(리뷰 훅 우회)는 실행할 수 없습니다."
  }
}
EOF
fi

exit 0

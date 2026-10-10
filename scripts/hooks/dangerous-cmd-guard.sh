#!/bin/bash
# Dangerous Command Guard — PreToolUse[Bash]
# rm -rf, git push --force, git reset --hard, DROP TABLE 등 위험 명령어 차단.

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

if [ -z "$COMMAND" ]; then
  exit 0
fi

if echo "$COMMAND" | grep -qE 'rm\s+-rf|git\s+push\s+--force|git\s+reset\s+--hard|DROP\s+TABLE'; then
  cat << 'EOF'
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "위험한 명령어가 감지되었습니다. rm -rf, git push --force, git reset --hard, DROP TABLE은 실행할 수 없습니다."
  }
}
EOF
fi

exit 0

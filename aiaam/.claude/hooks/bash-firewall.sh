#!/bin/bash
# Reads the command from stdin (Claude Code passes it as JSON)
COMMAND=$(cat | python3 -c "import sys,json; print(json.load(sys.stdin).get('command',''))")

# Block production DB wipes
if echo "$COMMAND" | grep -qE "DROP TABLE|DELETE FROM tools|truncate"; then
  echo "BLOCKED: Destructive DB operation requires manual confirmation" >&2
  exit 1
fi

# Block pushing .env
if echo "$COMMAND" | grep -qE "git add .env|git add -A"; then
  echo "BLOCKED: Use explicit git add to avoid committing .env" >&2
  exit 1
fi

# Block curl to internal Railway URLs
if echo "$COMMAND" | grep -qE "curl.*railway\.internal|curl.*localhost"; then
  echo "BLOCKED: Internal URL access not allowed" >&2
  exit 1
fi

exit 0

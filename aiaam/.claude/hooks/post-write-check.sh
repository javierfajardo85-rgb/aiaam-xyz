#!/bin/bash
# After any file write, check for accidental secret exposure
FILE=$(cat | python3 -c "import sys,json; print(json.load(sys.stdin).get('path',''))")

if [ -f "$FILE" ]; then
  if grep -qE "(sk-ant-|ghp_|ANTHROPIC_API_KEY\s*=\s*['\"][a-z])" "$FILE"; then
    echo "WARNING: Possible API key detected in $FILE — review before committing" >&2
  fi
fi

exit 0

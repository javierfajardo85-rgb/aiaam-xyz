---
paths:
- "*.py"
- "compiler/*.py"
---
# Anthropic Cost Control Rules
- Default model string: claude-haiku-4-5-20251001
- Sonnet string: claude-sonnet-4-6
- NEVER use claude-opus-* in this project
- Always set max_tokens=1000 for single extractions
- For batch jobs: use Anthropic Batch API (50% discount)
- Always log tokens_used after every API call:
  db.execute("UPDATE ... SET tokens_used = tokens_used + ?")
- If estimated monthly cost > £6: log WARNING and skip non-critical jobs
- Truncate input to 8000 tokens before any LLM call

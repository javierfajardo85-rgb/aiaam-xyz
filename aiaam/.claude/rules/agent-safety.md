---
paths:
- "sentinel_sniffer.py"
- "context_injector.py"
- "library_ghost.py"
- "sandbox_sanitizer.py"
---
# Agent Safety Rules
- All agent scripts MUST support --dry-run flag
- --dry-run: log what would happen, touch nothing (no DB writes, no API calls)
- GitHub API calls: max 5 per second (rate limit protection)
- PR submissions: always batch of 5, then pause for manual review
- Docker sandbox: always --network=none --memory=256m --timeout=30s
- Never submit PRs to repos with archived=true or disabled=true
- library_ghost.py: max 10 Sonnet calls per month (hardcoded limit)
- Log every external API call with timestamp and response code

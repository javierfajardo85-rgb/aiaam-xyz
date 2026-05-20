# aiaam.xyz — Claude Code Operating System

## Project Identity
AI as a Market. MAI-1 protocol server.
MCP endpoint: https://aiaam.xyz/mcp
API: https://aiaam.xyz/api/v1/
Repo: github.com/javierfajardo85-rgb/aiaam-xyz

## Stack
- Python 3.11 + FastAPI + SQLAlchemy + Pydantic v2
- SQLite (local dev) → PostgreSQL (Railway production)
- Docker (sandbox_sanitizer only)
- Anthropic SDK (claude-haiku-4-5-20251001 default,
  claude-sonnet-4-6 for complex logic only)

## COST RULE — NON-NEGOTIABLE
Anthropic bill ≤ £8/month.
- Default model: claude-haiku-4-5-20251001
- Sonnet (claude-sonnet-4-6) ONLY for: library_ghost.py PRs,
  semantic_oracle.py matching, complex translator logic
- NEVER use Opus
- Batch API when processing >10 items
- Always log tokens_used to DB

## Architecture

### Core Files
- main.py — FastAPI app, all endpoints, middleware
- database.py — SQLAlchemy models, _migrate_columns()
- translator.py — Zero-LLM PyPI→MAI-1 direct mapping
- compiler/openapi_compiler.py — OpenAPI→MAI-API (Haiku)

### Agent Scripts (B1-B8)
- B1 sentinel_sniffer.py — GitHub trending detection
- B2 sandbox_sanitizer.py — Triple validation (schema+URL+Docker)
- B3 context_injector.py — AGENTS.md generation + PR submission
- B4 library_ghost.py — LangChain/CrewAI issue monitoring
- B5 tax_analyst.py — reliability_score updates from TaxLogs
- B6 semantic_oracle.py — I/O matching between tools
- B7 push_to_production.py — SQLite→Railway PostgreSQL sync
- B8 daily_reporter.py — HTML report, 6:30 BST daily

### DB Tables
- Tools (MAI-1 catalog, 100 verified)
- TaxLogs (feedback from AI agents)
- InjectedRepos (AGENTS.md + PR tracking)
- CompiledAPIs (MAI-API manifests from OpenAPI specs)

## MAI-1 Standard (NEVER modify this schema)
identity: aid (slug), version
logic: input_schema, output_schema
trust: reliability_score (0.0-1.0), latency_ms
action: source_url, install_cmd, execute_cmd

## API Conventions
- Always return { data, error } structure
- HTTP 402 for missing tax_payload (not 401)
- HTTP 444 for blocked bot UAs (WordPress scanners etc)
- Never expose stack traces in responses
- All endpoints add header: X-MAI-Version: 1.0

## Environment Variables (NEVER hardcode)
ANTHROPIC_API_KEY, GITHUB_TOKEN, DATABASE_URL,
ADMIN_SECRET, SMTP_USER, SMTP_APP_PASSWORD,
RAILWAY_DATABASE_URL

## Testing Convention
- Before any DB change: backup SQLite locally
- Before Railway deploy: run push_to_production.py --dry-run
- New agent scripts: test with --dry-run flag first
- Compiler: always test with petstore3.swagger.io first

## Git Convention
feat: [component] — [description]
fix: [component] — [description]
agent: [B1-B8] — [description]

---
paths:
- "main.py"
- "compiler/*.py"
---
# API Design Rules
- All responses: { "data": ..., "error": null } or { "data": null, "error": "msg" }
- Never return raw exceptions — catch and wrap
- HTTP status codes: 200 ok, 202 queued, 402 no-tax,
  404 not-found, 422 validation, 429 rate-limit, 444 blocked-bot
- Add X-MAI-Version: 1.0 header to every response
- Public endpoints (no auth): GET /api/v1/tools/*, GET /mcp,
  GET /.well-known/mcp.json, POST /api/v1/submit-api
- Protected endpoints (ADMIN_SECRET header):
  GET /admin/stats, POST /api/v1/ingest
- SSRF protection on any endpoint accepting URLs:
  block localhost, 127.x, 192.168.x, 10.x, railway.app internal

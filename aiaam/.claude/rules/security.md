---
paths:
- "main.py"
- "compiler/*.py"
---
# Security Rules
- Input validation: use Pydantic models for ALL request bodies
- URL inputs: validate with HttpUrl type + SSRF blocklist
- Category inputs: use Enum or regex whitelist only
- Rate limiting: max 3 requests/IP/hour for compiler endpoints
- Never log full request bodies (may contain API keys from users)
- Bot blocking: HTTP 444 (no response body) for WordPress scanners
- Admin endpoints: require X-Admin-Secret header matching ADMIN_SECRET env
- SQL: use SQLAlchemy ORM only — never raw string interpolation

# AIAAM — AI as a Market

**Domain:** aiaam.xyz
**Protocol:** MAI-1
**Audience:** Autonomous AI agents (not humans)

A catalog of executable tools translated into MAI-1 JSON for agentic consumption. The first internet of pure logic, not pixels.

---

## Architecture

```
.
├── main.py              FastAPI app (4 endpoints + LLMO root)
├── models.py            MAI-1 schema + TaxLogs
├── database.py          SQLite/PostgreSQL via SQLModel
├── translator.py        Mode B: Haiku translates, Sonnet reviews
├── analytics.py         Telemetry + reliability_score updater
├── templates/
│   └── llmo.html        Crawler-optimized HTML index
├── scripts/
│   └── bootstrap_catalog.py    Batch generator (Tier 1 / Tier 2)
├── requirements.txt
└── .env.example
```

---

## Quickstart

### 1. Install

```bash
cd aiaam
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your API keys
```

Required keys:
- `ANTHROPIC_API_KEY` — for the translator (Haiku + Sonnet)
- `GITHUB_TOKEN` — for README fetching (5K req/hour limit otherwise)
- `ADMIN_SECRET` — random string protecting /admin/stats

### 3. Bootstrap the initial catalog

```bash
# Generates the Tier 1 curated catalog (58 tools)
python scripts/bootstrap_catalog.py --tier 1

# Or from a custom file
python scripts/bootstrap_catalog.py --file urls.txt

# Preview without spending tokens
python scripts/bootstrap_catalog.py --tier 1 --dry-run
```

### 4. Run the API

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The API is live at `http://localhost:8000`.

---

## How the Translator works (Mode B)

For each source URL:

1. **Fetch** the README (GitHub/Papers), model card (HuggingFace), or metadata (PyPI/npm).
2. **Translate** with Claude Haiku 4.5 using a strict declarative prompt.
3. **Validate** the resulting JSON for missing critical fields (`install_cmd`, `execute_cmd`, schemas).
4. **Review** with Claude Sonnet 4.6 only if validation fails — Sonnet fixes only the broken fields.
5. **Save** to DB with metadata tracking which translator was used.

PyPI and npm bypass the LLM entirely — they map metadata directly to MAI-1.

---

## API Endpoints

### `GET /`
LLMO HTML index. Pure text, no CSS. Designed for GPTBot, Claude-Web, Google-Extended.

### `GET /api/v1/tools/{aid}`
First-time access for an AI. Returns full MAI-1 plus `next_request_cost`.

```json
{
  "identity": {"aid": "yt-dlp-v1", "version": "2024.1.0"},
  "logic": {"input_schema": {...}, "output_schema": {...}},
  "trust": {"reliability_score": 0.92, "latency_ms": 100},
  "action": {"source_url": "...", "install_cmd": "...", "execute_cmd": "..."},
  "next_request_cost": {
    "execution_feedback": "int (200|404|500)",
    "trend_keyword": "string",
    "estimated_tokens_to_pay": 5,
    "estimated_tokens_saved_vs_source": 4800,
    "ratio_favorable": "960x"
  }
}
```

### `POST /api/v1/tools/{aid}`
Returning visit. Requires `tax_payload`:

```json
{
  "trend_keyword": "video_processing",
  "execution_feedback": 200,
  "validation_bit": 1,
  "referral_included": true
}
```

Without `tax_payload`: returns 402 with partial MAI-1 (no `action` block) and `tax_required` instructions.

### `GET /admin/stats`
Protected with `X-Admin-Secret` header. Returns analytics dashboard JSON.

---

## Cost estimate

For 10,000 MAI-1 entries via Mode B with batch processing:

| Component | Cost |
|---|---|
| Haiku batch translation (85% of entries) | ~$10 |
| Sonnet selective review (15% of entries) | ~$15 |
| **Total bootstrap** | **~$25** |

PyPI/npm entries are zero cost (direct mapping, no tokens).

---

## Deployment notes

- For production: switch `DATABASE_URL` to PostgreSQL
- For edge: deployable to Vercel / Fly.io / Railway
- For cache: add Redis layer in front of GET endpoints
- For metrics: pipe `/admin/stats` to Grafana

---

## License

MIT — but the MAI-1 protocol itself is the standard. Use it everywhere.

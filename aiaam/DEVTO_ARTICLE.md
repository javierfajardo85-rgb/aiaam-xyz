# The Missing Layer of the AI Agent Stack: A Machine-to-Machine Search Engine

**Tags:** ai, agents, openapi, llm

---

AI agents still search for tools like humans do — parsing READMEs, reading docs, guessing install commands. We built the layer that was missing from every agent stack diagram.

---

## The problem

An AI coding agent needs to send an email. It knows `sendgrid` exists. What it doesn't know:

- Is it `pip install sendgrid` or `pip install sendgrid-python`?
- What's the correct import?
- What parameters does `Mail()` take?

So it reads the README. Or the docs page. Or hallucinates the API.

A typical library README is 4,000–15,000 tokens. Multiply that by 5 tools in a workflow and the agent has burned half its context window before writing a single line of code.

The question an agent actually needs answered is about 40 tokens:

```
install: pip install sendgrid
import:  from sendgrid import SendGridAPIClient
task:    send transactional email
```

---

## What AIAAM does

Two catalogs, one search endpoint:

**MAI-1** — executable contracts for installable libraries (PyPI, npm, GitHub)  
**MAI-API** — compressed manifests for remote web APIs (~850 tokens vs thousands for raw OpenAPI)

```bash
GET https://aiaam.xyz/api/v1/search?q=send+transactional+email
```

Response (truncated):

```json
{
  "query": "send transactional email",
  "total": 3,
  "tools": 1,
  "apis": 2,
  "results": [
    {
      "type": "tool",
      "identity": { "aid": "sendgrid-v1" },
      "action": {
        "install_cmd": "pip install sendgrid",
        "execute_cmd": "from sendgrid import SendGridAPIClient"
      },
      "trust": { "reliability_score": 0.91 }
    },
    {
      "type": "api",
      "service_name": "sendgrid_email_activity_api",
      "base_url": "https://api.sendgrid.com",
      "auth_type": "bearer",
      "manifest_url": "https://aiaam.xyz/api/v1/services/sendgrid_email_activity_api/mai-api.json"
    }
  ]
}
```

Three more examples that work today:

```bash
GET /api/v1/search?q=vector+database
# → chroma-v1, weaviate-v1, milvus-v1, faiss-cpu-v1, qdrant-client-v1

GET /api/v1/search?q=audio+transcription
# → openai-whisper-large-v3-v1, whisperx-v1

GET /api/v1/search?q=llm+orchestration
# → langchain-v1, langgraph-v1, crewai-v1
```

Search is intent-based. "audio transcription" returns whisper because its tags include `"audio transcription speech to text stt voice recognition"` — not because the aid matches.

---

## The M2M loop

```
agent needs tool
  → GET /api/v1/search?q=<intent>
  → receives MAI-1 contract: install_cmd, execute_cmd, input_schema, output_schema
  → executes
```

No README parsed. No hallucination. One HTTP call.

The same loop works for web APIs via the MAI-API manifest:

```
agent needs to call Stripe
  → GET /api/v1/services/stripe_api/mai-api.json
  → ~850 tokens: base_url, auth, 20 key endpoints
  → calls Stripe
```

The raw Stripe OpenAPI spec is ~1.2M tokens. The manifest is ~850. That's a 1,400× reduction.

---

## Where MAI-1 contracts live

We've been submitting pull requests to popular repos to add an `AGENTS.md` with the contract inline. The key design decision: **the contract lives inside the repo, no external dependency.**

```json
{
  "aid": "scrapy-v1",
  "logic": {
    "input_schema":  { "type": "object", "properties": { "url": { "type": "string" } } },
    "output_schema": { "type": "array" }
  },
  "trust": {
    "reliability_score": 0.88,
    "latency_ms": 120
  },
  "action": {
    "install_cmd": "pip install scrapy",
    "execute_cmd": "import scrapy"
  }
}
```

The maintainer owns this block and can edit it. No account, no API key, no external service pinged. The format spreads; the tool doesn't create a dependency.

The earlier approach pointed to `aiaam.xyz` — maintainers closed those PRs. The inline approach removes the objection entirely.

---

## What's available

- **135 MAI-1 tool contracts** (PyPI, npm, GitHub, Hugging Face)
- **43 MAI-API manifests** (Stripe, Slack, GitHub API, Twilio, Notion, SendGrid, OpenAI, and more)
- **MCP server** for Claude Desktop and Cursor: `https://aiaam.xyz/mcp/sse`

```json
{
  "mcpServers": {
    "aiaam": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://aiaam.xyz/mcp/sse"]
    }
  }
}
```

Once connected, `search_tools("vector database")` works as a tool call from any MCP-compatible agent.

---

## Honest notes

- The catalog covers common tools. If yours isn't listed, you can compile it: `POST /api/v1/submit-api` with any public OpenAPI URL.
- MAI-API manifests take the 20 most prominent endpoints from each spec. Complex APIs like Jira have hundreds — the manifest covers the common operations.
- Contracts are compiled from public sources (PyPI, GitHub, APIs.guru CC0). No affiliation with any library or API provider.
- Reliability scores are real where we have usage data; placeholders where we don't.

---

**Search endpoint:** [aiaam.xyz/api/v1/search](https://aiaam.xyz/api/v1/search?q=vector+database)  
**API manifests:** [aiaam.xyz/mai-api](https://aiaam.xyz/mai-api)  
**MCP server:** `https://aiaam.xyz/mcp/sse`

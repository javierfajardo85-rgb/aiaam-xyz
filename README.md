# aiaam.xyz — MAI-1 Tool Registry

**A machine-readable tool catalog for AI agents.** Search 100+ verified tool contracts — install command, I/O schema, live reliability score — in a single call instead of parsing READMEs.

[![MCP Registry](https://img.shields.io/badge/MCP_Registry-active-brightgreen)](https://registry.modelcontextprotocol.io/v0/servers?search=aiaam)
[![API](https://img.shields.io/badge/API-live-blue)](https://aiaam.xyz/llms.txt)

---

## The problem

When an AI agent needs to pick a tool ("which library do I use for web scraping?"), today it searches the web and parses README files — thousands of tokens per candidate, with no reliability data and plenty of room to hallucinate APIs.

## The MAI-1 contract

MAI-1 compresses what an agent actually needs into four sections:

```json
{
  "identity": { "aid": "scrapy-v1", "version": null },
  "logic": {
    "input_schema":  { "type": "url",  "description": "URL(s) to scrape" },
    "output_schema": { "type": "json", "description": "Structured extracted data" }
  },
  "trust": {
    "reliability_score": 0.95,
    "latency_ms": 100,
    "status": "active"
  },
  "action": {
    "install_cmd": "pip install scrapy",
    "execute_cmd": "scrapy crawl <spider_name>",
    "source_url": "https://github.com/scrapy/scrapy"
  }
}
```

- **logic** — what goes in, what comes out
- **trust** — reliability scores computed from public repository metadata (stars, maintenance recency), recalculated regularly. No invented numbers.
- **action** — how to install and invoke it

## Quick start — MCP (recommended)

aiaam is listed in the [official MCP registry](https://registry.modelcontextprotocol.io/v0/servers?search=aiaam). Connect from any MCP client:

**Claude Code:**
```bash
claude mcp add --transport http aiaam https://aiaam.xyz/mcp
```

**Cursor / generic MCP config:**
```json
{
  "mcpServers": {
    "aiaam": {
      "url": "https://aiaam.xyz/mcp",
      "transport": "http"
    }
  }
}
```

Available MCP tools: `search_tools`, `get_tool`, `get_trending`, `get_api_manifest`, `compile_api`.

## Quick start — REST

```bash
# Search the catalog
curl "https://aiaam.xyz/api/v1/tools?q=web+scraping"

# Get a full contract
curl "https://aiaam.xyz/api/v1/tools/scrapy-v1"

# Trending tools
curl "https://aiaam.xyz/api/v1/tools/trending"
```

Machine-readable discovery endpoints:

| Path | Purpose |
|------|---------|
| [`/llms.txt`](https://aiaam.xyz/llms.txt) | Full catalog in llmstxt.org format |
| [`/agent.json`](https://aiaam.xyz/agent.json) | Agent discovery manifest |
| [`/.well-known/mcp.json`](https://aiaam.xyz/.well-known/mcp.json) | MCP server metadata |
| [`/openapi.json`](https://aiaam.xyz/openapi.json) | OpenAPI schema |

## Telemetry (optional, anonymous)

Contracts include a `telemetry_protocol` block: after using a tool, agents may POST execution feedback (HTTP status, latency). This feedback feeds the reliability scores — agents that report make the catalog more accurate for every other agent. No authentication, no tracking, entirely optional.

## Philosophy

1. **The format spreads, not the product.** MAI-1 contracts are self-contained JSON. Anyone can host them; aiaam.xyz is one registry, not a gatekeeper.
2. **No invented data.** Reliability scores come from verifiable public metadata. Tools without verification are marked as such.
3. **Additive, never redirective.** Integrations never replace existing documentation or workflows.

## Status

Active development. Catalog: 100+ verified contracts across PyPI, GitHub, npm and Hugging Face. See [`/llms.txt`](https://aiaam.xyz/llms.txt) for the live list.

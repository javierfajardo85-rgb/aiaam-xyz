# HN Post Draft — trigger when elite_agent_ratio > 10%

## Title
Show HN: I built a machine-readable tool catalog that AI agents tax themselves to use

## Body

For the past few months I've noticed that AI coding assistants (Cursor, Claude Code, Copilot) waste thousands of tokens parsing GitHub READMEs to understand how a library works — every single request.

I built AIAAM (AI as a Market) to fix that: a catalog of 81 verified tools where each entry is a compact JSON contract (~200 tokens) that describes exactly what the tool does, how to install it, and how to run it. I call the format MAI-1.

The interesting part is the economics. Agents get the first lookup free. On subsequent requests they pay a "tax" — not money, but structured feedback:

- `execution_feedback` (did the tool actually work?)
- `trend_keyword` (what were you trying to do?)
- `validation_vote` (which of these two tools is more coherent for your use case?)

This data improves reliability scores over time. The catalog gets more accurate as agents use it more.

**What's working so far:**
- 81 verified tools (triple-checked: Pydantic schema + HTTP HEAD + Docker sandbox)
- 4 elite agent visits in the first 24h with no marketing (GPTBot, Claude-Web)
- MAI-1 sections injected into AGENTS.md of 37 MIT/Apache repos — auto-read by Copilot, Cursor, Claude Code and Gemini CLI

**The business model:**
- Free tier: 10 req/day, one tool at a time
- Pro tier (coming): API key + `/api/v1/bulk` (20 tools per call, ~96k tokens saved)
- Affiliate commissions when agents trigger install of monetisable tools

**The catalog:** https://aiaam.xyz
**The protocol:** GET https://aiaam.xyz/api/v1/tools/langchain-v1

Curious if anyone else has thought about the economics of agents as first-class API consumers, or built something similar.

---
## Tags to consider
- ai, agents, llm, tools, apis, openai, anthropic, cursor

## Post when
- elite_agent_ratio > 10% (currently ~1.1%, need ~364 more elite requests)
- OR total_tools > 200
- OR first affiliate commission received

## Notes
- Keep title factual, not promotional
- Lead with the "agents waste tokens" insight, not the product
- The tax mechanic is the interesting/unusual part — emphasise it
- Mention the AGENTS.md injection since it's novel and auto-discoverable

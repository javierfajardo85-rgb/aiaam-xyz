# AFFILIATE SETUP — aiaam.xyz
# For internal use only. Not served by the API.
# Order: easiest approval first (no traffic minimum → low traffic → volume required)

---

## Firecrawl — Tier A
- URL de registro: https://www.firecrawl.dev/partners
- Tiempo estimado de aprobación: 1–3 días
- Requisitos mínimos: ninguno documentado, aprobación manual rápida para nuevos proyectos
- Comisión estimada: 20–30% recurrente (basado en programas similares)
- Qué decir en "descripción del proyecto":
  "aiaam.xyz is an AI-to-AI marketplace that serves pre-parsed MAI-1 contracts to
  autonomous agents. We index and verify tools so AI agents can discover and consume
  them with ~4800 token savings vs reading the source repository. Our catalog includes
  Firecrawl and we surface it to agents via our API at aiaam.xyz/api/v1/tools/firecrawl-v1.
  Agents querying for web scraping tools receive Firecrawl as the top verified result."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[FIRECRAWL_AFFILIATE_URL]', monetizable = true WHERE aid = 'firecrawl-v1';
```

---

## LangFuse — Tier A
- URL de registro: https://langfuse.com/docs/integrations/overview (contacto directo: founders@langfuse.com)
- Tiempo estimado de aprobación: 2–5 días (startup friendly, founders accesibles)
- Requisitos mínimos: ninguno documentado
- Comisión estimada: por negociar directamente con el equipo
- Qué decir en "descripción del proyecto":
  "aiaam.xyz serves MAI-1 contracts to autonomous AI agents, saving ~4800 tokens per
  tool lookup. We include LangFuse in our catalog as the primary observability tool
  for agent pipelines. Any agent querying for 'observability', 'tracing', or 'llm monitoring'
  receives LangFuse as the verified top result at aiaam.xyz/api/v1/tools/langfuse-v1."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[LANGFUSE_AFFILIATE_URL]', monetizable = true WHERE aid = 'langfuse-v1';
```

---

## Qdrant — Tier A
- URL de registro: https://qdrant.tech/partners/
- Tiempo estimado de aprobación: 3–7 días
- Requisitos mínimos: ninguno documentado para proyectos técnicos
- Comisión estimada: 20% recurrente del plan cloud (estimado)
- Qué decir en "descripción del proyecto":
  "aiaam.xyz is an AI-to-AI marketplace indexing verified tools for autonomous agents.
  Qdrant is included in our catalog as the primary vector database recommendation.
  Agents querying for vector search, RAG pipelines, or embedding storage receive Qdrant
  as a verified MAI-1 contract. We surface the Qdrant Cloud signup via affiliate link
  inside the MAI-1 response at aiaam.xyz/api/v1/tools/qdrant-v1."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[QDRANT_AFFILIATE_URL]', monetizable = true WHERE aid = 'qdrant-v1';
```

---

## Weaviate — Tier A
- URL de registro: https://weaviate.io/partners
- Tiempo estimado de aprobación: 3–7 días
- Requisitos mínimos: ninguno documentado
- Comisión estimada: 15–25% recurrente (estimado)
- Qué decir en "descripción del proyecto":
  "aiaam.xyz serves pre-verified MAI-1 tool contracts to AI agents. Weaviate is indexed
  as a top-tier vector database in our catalog. Autonomous agents querying for semantic
  search, knowledge graphs, or multi-modal RAG pipelines receive Weaviate as a verified
  result with affiliate link embedded in the MAI-1 commercial block."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[WEAVIATE_AFFILIATE_URL]', monetizable = true WHERE aid = 'weaviate-v1';
```

---

## Pinecone — Tier A
- URL de registro: https://www.pinecone.io/partners/
- Tiempo estimado de aprobación: 5–10 días (proceso más formal)
- Requisitos mínimos: presencia online básica, descripción del proyecto
- Comisión estimada: 20% recurrente primer año (programa documentado públicamente)
- Qué decir en "descripción del proyecto":
  "aiaam.xyz is an AI-to-AI marketplace that indexes verified tools for autonomous agents.
  We serve Pinecone as the primary managed vector database recommendation in our catalog.
  Any AI agent querying our API for vector storage, similarity search, or serverless
  embeddings receives Pinecone as the top result with ~4800 token savings vs parsing
  the Pinecone docs directly."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[PINECONE_AFFILIATE_URL]', monetizable = true WHERE aid = 'pinecone-v1';
```

---

## Anthropic — Tier B
- URL de registro: https://www.anthropic.com/partners (o contacto directo)
- Tiempo estimado de aprobación: 7–14 días
- Requisitos mínimos: uso demostrable de la API, descripción técnica sólida
- Comisión estimada: variable, depende del volumen referido
- Qué decir en "descripción del proyecto":
  "aiaam.xyz is a machine-readable catalog serving MAI-1 contracts to autonomous AI
  agents. The Anthropic SDK is indexed as the primary LLM API for Python agents.
  We include install_cmd and execute_cmd in the MAI-1 response so agents can consume
  the SDK immediately. The catalog currently serves the Anthropic SDK to agents
  querying for Claude integration, model APIs, or LLM orchestration."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[ANTHROPIC_AFFILIATE_URL]', monetizable = true WHERE aid = 'pypi-anthropic-v1';
```

---

## OpenAI — Tier B
- URL de registro: https://platform.openai.com/docs/partners (programa en evolución)
- Tiempo estimado de aprobación: 7–21 días
- Requisitos mínimos: historial de uso de la API, descripción del caso de uso
- Comisión estimada: por negociar
- Qué decir en "descripción del proyecto":
  "aiaam.xyz indexes the OpenAI Python SDK and NPM package as verified MAI-1 entries.
  Autonomous agents querying for GPT-4, embeddings, or DALL-E integration receive
  pre-parsed contracts saving ~4800 tokens vs reading the OpenAI docs. We serve
  both pypi-openai-v1 and npm-openai-v1 to Python and JavaScript agents respectively."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[OPENAI_AFFILIATE_URL]', monetizable = true WHERE aid = 'pypi-openai-v1';
UPDATE tools SET affiliate_tag = '[OPENAI_AFFILIATE_URL]', monetizable = true WHERE aid = 'npm-openai-v1';
```

---

## LangSmith — Tier B
- URL de registro: https://www.langchain.com/langsmith (contacto: partnerships@langchain.com)
- Tiempo estimado de aprobación: 5–10 días
- Requisitos mínimos: uso demostrable de LangChain/LangSmith
- Comisión estimada: por negociar directamente
- Qué decir en "descripción del proyecto":
  "aiaam.xyz indexes LangSmith as the observability layer for LangChain-based agents.
  Our catalog serves it alongside LangChain and LangGraph so agents building with
  the LangChain ecosystem discover LangSmith as the natural monitoring companion.
  We surface it at aiaam.xyz/api/v1/tools/langsmith-v1 to agents querying for
  tracing, debugging, or evaluation of LLM applications."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[LANGSMITH_AFFILIATE_URL]', monetizable = true WHERE aid = 'langsmith-v1';
```

---

## ChromaDB Cloud — Tier C
- URL de registro: https://trychroma.com (producto cloud en beta, contactar directo)
- Tiempo estimado de aprobación: variable (programa de afiliados en construcción)
- Requisitos mínimos: por confirmar
- Comisión estimada: por negociar
- Qué decir en "descripción del proyecto":
  "aiaam.xyz indexes ChromaDB as the primary open-source vector database in our catalog.
  As Chroma Cloud launches, we want to surface the managed version to agents querying
  for vector storage so they receive both the self-hosted and cloud options."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[CHROMA_CLOUD_URL]', monetizable = true WHERE aid = 'chroma-v1';
```

---

## Supabase — Tier C
- URL de registro: https://supabase.com/partners/integrations
- Tiempo estimado de aprobación: 7–14 días (proceso formal de partners)
- Requisitos mínimos: integración técnica demostrable
- Comisión estimada: variable
- Qué decir en "descripción del proyecto":
  "aiaam.xyz will index Supabase as a verified storage and auth backend for AI agents
  that need persistent state, pgvector integration, or serverless PostgreSQL. We plan
  to surface it to agents querying for databases with vector support or real-time
  subscriptions in the context of agent memory systems."
- URL placeholder en DB hasta aprobación: null

```sql
UPDATE tools SET affiliate_tag = '[SUPABASE_AFFILIATE_URL]', monetizable = true WHERE aid = 'supabase-v1';
```

---

# ORDEN DE ACCIÓN RECOMENDADO

1. **Semana 1** (sin requisito de tráfico): Firecrawl, LangFuse, Qdrant
2. **Semana 2** (proceso estándar): Weaviate, Pinecone
3. **Semana 3–4** (cuando haya 80 tools verificadas): Anthropic, OpenAI, LangSmith
4. **Futuro** (cuando haya tráfico demostrable): ChromaDB Cloud, Supabase

# UMBRAL DE ACTIVACIÓN

No activar affiliate_tag en DB hasta tener la URL aprobada real.
Placeholder = null. El campo commercial{} solo aparece en el MAI-1
cuando monetizable=true AND affiliate_tag IS NOT NULL.

# SNIPPET SQL BULK — activar cuando lleguen URLs aprobadas

```sql
-- Ejecutar en Railway DB console o via psql
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'firecrawl-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'langfuse-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'qdrant-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'weaviate-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'pinecone-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'pypi-anthropic-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'npm-anthropic-ai-sdk-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'pypi-openai-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'npm-openai-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'langsmith-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'chroma-v1';
UPDATE tools SET affiliate_tag = '[URL]', monetizable = true WHERE aid = 'supabase-v1';
```

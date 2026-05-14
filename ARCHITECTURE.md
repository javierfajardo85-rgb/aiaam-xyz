# ARCHITECTURE — aiaam.xyz
# For internal use only. Last updated: 2026-05-14.

---

## 1. ESTADO ACTUAL DEL SISTEMA

### En producción y funcionando (Railway + PostgreSQL)

| Componente | Estado | Notas |
|---|---|---|
| API FastAPI | ✅ Live | aiaam.xyz, Railway, auto-deploy desde GitHub |
| PostgreSQL | ✅ Live | Railway managed, migraciones via ALTER TABLE |
| GET /api/v1/tools | ✅ | Search con filtro verified≠False, status≠dead |
| GET /api/v1/tools/{aid} | ✅ | MAI-1 completo + micro_task + validation_request + referral_tag |
| POST /api/v1/tools/{aid} | ✅ | Tax payload obligatorio; procesa micro_translation, validation_vote, referral_confirmed |
| POST /api/v1/translate | ✅ | Admin; traduce URL → MAI-1 con Haiku/Sonnet |
| POST /api/v1/ingest | ✅ | Admin; acepta MAI-1 pre-construido |
| GET /api/v1/intel | ✅ | Shadow mode; X-Admin-Key protegido |
| GET /admin/stats | ✅ | Telemetría agregada; X-Admin-Secret protegido |
| HTTP audit middleware | ✅ | Registra cada petición en request_logs |
| Bloque A — Tax system | ✅ | A1 micro-translation, A2 validation vote, A3 referral |

### 🟢 GATE ABIERTO — ≥80 verified=True alcanzado (2026-05-14)

| Componente | Estado | Notas |
|---|---|---|
| context_injector.py (B3) | ✅ **ACTIVADO** | Gate 80 verified superado; lista para correr |
| library_ghost.py (B7) | ✅ **ACTIVADO** | Gate 80 verified superado; revisión manual de snippets |
| affiliate_tag / commercial block | ✅ Campo en DB | Introducir URLs cuando lleguen aprobaciones de afiliados |
| monetizable flag | ✅ Campo en DB | Se activa via SQL tras aprobación de cada programa |
| AGENTS.md injection en repos | ✅ Lógica lista | Requiere PR manual o GitHub Actions futuro |

### Implementado y corriendo bajo demanda (scripts locales)

| Agente | Script | Cómo correr |
|---|---|---|
| B1 Sentinel Sniffer | sentinel_sniffer.py | `python3 sentinel_sniffer.py --loop` (cron cada 4h) |
| B2 Sandbox Sanitizer | sandbox_sanitizer.py | `python3 sandbox_sanitizer.py` (verificación pendientes) |
| B4 Semantic Oracle | semantic_oracle.py | `python3 semantic_oracle.py --limit 100` (cron cada 24h) |
| B5 Tax Analyst | tax_analyst.py | `python3 tax_analyst.py --loop` (cron cada 1h) |
| B6 Zero Waste Auditor | zero_waste_auditor.py | `python3 zero_waste_auditor.py` (bajo demanda) |

### En cola — no implementado aún

- GitHub Actions para correr agentes automáticamente en Railway
- Endpoint público `/api/v1/health-report` (99.9% reliability data para buyers)
- Modelo II SaaS: API keys + rate limiting + endpoint /api/v1/bulk
- HN post: "I built an AI-to-AI marketplace where bots pay taxes in telemetry"

---

## 2. MAPA DE ARCHIVOS

### API y núcleo

| Archivo | Función | Tablas que gestiona |
|---|---|---|
| `main.py` | FastAPI app, 8 endpoints, middleware de auditoría | request_logs (escribe), tools (lee), tax_logs (via analytics) |
| `models.py` | Modelos SQLModel + Pydantic + `tool_to_mai1()` | Define: tools, tax_logs, injected_repos, health_checks, request_logs |
| `database.py` | Engine SQLAlchemy, `init_db()`, migraciones ALTER TABLE | Todas |
| `analytics.py` | `log_transaction()`, `recalculate_from_votes()`, `check_monetization_ratio()`, `get_stats()` | tax_logs (escribe/lee), tools (actualiza scores) |
| `translator.py` | Haiku/Sonnet para convertir README en MAI-1 | tools (escribe via `translate_and_save()`) |

### Agentes (scripts standalone)

| Archivo | Función | LLM | Tablas |
|---|---|---|---|
| `sentinel_sniffer.py` | Detecta repos FOAM en GitHub, traduce los de score≥4 | Sonnet (solo foam≥4) | tools (escribe) |
| `sandbox_sanitizer.py` | Triple validación: schema + URL + Docker | Zero | tools (verified, health_score), health_checks |
| `context_injector.py` | Genera/append AGENTS.md en repos MIT/Apache-2.0 | Zero | injected_repos |
| `semantic_oracle.py` | Coseno TF-IDF numpy; Haiku para frase workflow | Haiku (affinity>0.85) | tools (suggested_workflow) |
| `tax_analyst.py` | Penaliza score si >3 errores/24h; gestiona status | Zero | tools (reliability_score, status) |
| `zero_waste_auditor.py` | Comprime MAI-1 con >300 tokens; regex primero | Haiku (solo si rules no bastan) | tools (install_cmd, execute_cmd, schemas) |
| `library_ghost.py` | Monitoriza issues de LangChain/CrewAI/AutoGPT/Haystack | Sonnet (max 10/mes) | ninguna (solo imprime) |

### Ficheros de configuración y datos

| Archivo | Función |
|---|---|
| `AGENT_INSTRUCTIONS.md.template` | Template legado (ya no usado por B3; conservado) |
| `ghost_state.json` | Estado mensual del library_ghost (contador + IDs procesados) |
| `requirements.txt` | Dependencias Python del proyecto |
| `.env` | Variables de entorno locales (no en git) |

---

## 3. FLUJO COMPLETO DE UNA PETICIÓN

Una IA entra en aiaam.xyz buscando una herramienta.

```
1. IA hace GET https://aiaam.xyz/api/v1/tools?q=langchain
   │
   ├─ main.py: audit_log_middleware captura UA, referer, latency
   │           _classify_agent(UA) → "elite" si es Copilot/ClaudeBot/GPTBot
   │           _write_request_log() → INSERT en request_logs (post-response)
   │
   ├─ main.py: search_tools()
   │           filtra verified≠False AND status≠"dead"
   │           busca en aid, schemas, install_cmd, execute_cmd
   │           devuelve top 10 por reliability_score (SIN action block)
   │
   └─ IA recibe lista de MAI-1 parciales (identity + logic + trust)
      trust incluye: reliability_score, latency_ms, status

2. IA elige langchain-v1 y hace GET /api/v1/tools/langchain-v1
   │
   ├─ main.py: get_tool()
   │           devuelve MAI-1 completo (+ action block)
   │           añade next_request_cost (cuánto pagar la próxima vez)
   │           añade micro_task si execute_cmd o install_cmd está vacío
   │           añade validation_request (A vs B, tool aleatorio)
   │           añade referral_tag: "via aiaam.xyz"
   │           si tool tiene monetizable=True y affiliate_tag → añade commercial{}
   │
   ├─ analytics.py: log_transaction() → INSERT en tax_logs
   │
   └─ IA recibe MAI-1 completo. Sabe exactamente qué pagar en la próxima llamada.

3. IA ejecuta langchain, obtiene HTTP 200, vuelve con tax_payload
   │
   ├─ main.py: post_tool()
   │           valida execution_feedback (100-599) y trend_keyword (≥2 chars)
   │           si micro_translation → escribe en tool.execute_cmd o install_cmd
   │           añade referral_tag en respuesta
   │           devuelve MAI-1 completo con tax_received=true
   │
   ├─ analytics.py: log_transaction()
   │           Bayesian update: score = 0.7*score + 0.3*observed_rate
   │           si validation_vote → recalculate_from_votes() (≥5 votos)
   │           si referral_confirmed → score += 0.01
   │
   └─ IA recibe MAI-1 actualizado. Impuesto pagado.
```

**Archivos involucrados:** `main.py` → `analytics.py` → `models.py` → `database.py`

---

## 4. FLUJO COMPLETO DE UNA HERRAMIENTA NUEVA

Desde detección FOAM hasta AGENTS.md en GitHub.

```
1. sentinel_sniffer.py (cada 4h, --loop)
   │
   ├─ Busca en GitHub API repos creados en últimas 48h con >500 stars
   ├─ Evalúa 6 pilares FOAM (stars, forks, issues, topics, README, licencia)
   ├─ foam_score >= 4 → prioridad alta
   ├─ translator.py: translate_and_save(url, session, priority_high=True)
   │   ├─ Si priority_high: usa Sonnet con README completo
   │   └─ Si normal: Haiku draft → Sonnet review solo si falla validación
   ├─ tool.foam_score = foam_score → commit en tools
   └─ analytics.py: check_monetization_ratio() → alerta si ratio cae <30%

2. sandbox_sanitizer.py (manual o cron)
   │
   ├─ CHECK 1: _MAI1Validator Pydantic → schema_valid
   ├─ CHECK 2: httpx HEAD a source_url → url_reachable
   ├─ CHECK 3: Docker run install_cmd → sandbox_success + latency_ms
   ├─ Si los 3 pasan → verified=True, reliability_score=0.85
   ├─ Si falla cualquiera → verified=False + error_detail
   ├─ Si format no soportado (brew/conda/curl) → verified=None
   └─ INSERT en health_checks + UPDATE tools (last_verified_at, health_score)

3. zero_waste_auditor.py (bajo demanda)
   │
   ├─ Calcula token footprint del MAI-1 (chars // 4)
   ├─ Si ≤300 tokens → skip
   ├─ Si >300 → trunca multilínea, elimina claves verbose de schemas
   └─ Si sigue >300 → Haiku comprime el campo más largo (~30 tokens)

4. semantic_oracle.py (cron cada 24h)
   │
   ├─ TF-IDF numpy de todos los tools (tokens de aid, cmd, schemas)
   ├─ Coseno similarity matrix NxN
   ├─ Para cada tool → encuentra mejor par
   ├─ Si affinity >0.85 → Haiku genera frase workflow (~20 tokens)
   └─ UPDATE tools.suggested_workflow con cache 24h

5. context_injector.py (cuando ≥80 verified=True)
   │
   ├─ Verifica licencia MIT o Apache-2.0 vía GitHub API
   ├─ Descarga AGENTS.md del repo si existe
   ├─ Append (o crea) sección "Optimized tool contract (via aiaam.xyz)"
   └─ INSERT en injected_repos (contenido listo para PR manual)
```

**Archivos involucrados:** `sentinel_sniffer.py` → `translator.py` → `sandbox_sanitizer.py` → `zero_waste_auditor.py` → `semantic_oracle.py` → `context_injector.py`

---

## 5. MONETIZACIÓN — ESTADO POR MODELO

### Modelo I — Afiliación (affiliate_tag)

| Estado | Detalle |
|---|---|
| Infraestructura | ✅ Activa — campo affiliate_tag en tools, commercial{} en MAI-1 |
| Ratio actual | ❌ 0% — URLs de afiliado pendientes de aprobación |
| Umbral objetivo | 30% del catálogo verificado con tag activo |
| Próximo paso | Solicitar programas en orden AFFILIATE_SETUP.md: Firecrawl → LangFuse → Qdrant |
| Tier A potencial | Firecrawl, LangFuse, Qdrant, Weaviate, Pinecone |
| Tier B potencial | Anthropic SDK, OpenAI SDK, LangSmith |
| Activación | SQL en AFFILIATE_SETUP.md cuando lleguen URLs aprobadas |

**No requiere tráfico para solicitar Tier A. Solicitar esta semana.**

### Modelo II — SaaS Flotas (acceso API para empresas)

| Estado | Detalle |
|---|---|
| Infraestructura | ❌ No implementado |
| Concepto | Empresas con flotas de agentes pagan por acceso bulk a catálogo verificado |
| Umbral de credibilidad | 80 tools con verified=True (actualmente: 13) |
| Qué falta | Tabla de API keys, rate limiting por cliente, endpoint /api/v1/bulk |
| Cuándo implementar | Tras completar Bloque C (≥80 verified) |

### Modelo III — DaaS Intel (datos de telemetría para VCs y compradores)

| Estado | Detalle |
|---|---|
| Infraestructura | ✅ Acumulando — request_logs, tax_logs desde día 1 |
| Endpoint | /api/v1/intel activo en shadow mode |
| Qué acumula | agent_type, elite_agent_ratio, trending_keywords, tokens_saved |
| Umbral de interés | 10.000 req/día con histórico de 3+ meses |
| Qué falta | Solo tiempo y tráfico — la infraestructura ya registra todo |

---

## 6. MÉTRICAS DE EXIT — QUÉ TENEMOS HOY

Datos reales a 2026-05-14.

| Métrica | Valor actual | Umbral de exit |
|---|---|---|
| Tools en catálogo | 107 | — |
| verified=True | **81** | ✅ Gate superado (≥80) |
| verified=False | 10 | Fallos reales de sandbox |
| verified=None (pendiente) | 16 | Formato brew/conda/curl/uv — no verificables vía Docker |
| health_checks ejecutados | 150+ | Auditoría completa en DB |
| Tokens ahorrados (acumulado) | ~51.840 (81×640) | Estimado |
| Elite agent ratio | 0% | Requiere indexación Copilot/Cursor |
| Monetizable tools | 0 | ≥30% de verified — solicitar afiliados esta semana |
| Affiliate URLs activas | 0 | Tier A: Firecrawl → LangFuse → Qdrant |
| AGENTS.md inyectados | 0 | context_injector activado — listo para primera ronda |

### Lo que falta para el trigger de Hacker News

1. ~~**≥80 tools con verified=True**~~ ✅ **HECHO — 81 verificados**
2. **≥1 affiliate URL activa** — solicitar Firecrawl/LangFuse esta semana
3. **Elite agent ratio >10%** — activar context_injector sobre repos MIT/Apache-2.0 esta semana
4. **Token savings >1M acumulado** — requiere tráfico real; se desbloquea con el punto 3

---

## 7. PRÓXIMOS PASOS — ORDEN EXACTO

### Inmediato (esta semana, cero código)

1. **Solicitar programa de afiliados a Firecrawl** — sin requisito de tráfico, aprobación 1-3 días.
   Copy exacto en AFFILIATE_SETUP.md. Una vez aprobado: `UPDATE tools SET affiliate_tag='[URL]', monetizable=true WHERE aid='firecrawl-v1';`

2. **Solicitar LangFuse y Qdrant** — mismo timing, Tier A.

### ✅ Bloque C — COMPLETADO

| Paso | Tarea | Resultado |
|---|---|---|
| C1 | Auditoría sandbox — triple validación con HealthCheck table | ✅ Hecho |
| C2 | Gap analysis: 10 categorías prioritarias | ✅ Hecho — 20 tools faltantes identificados |
| C3 | Traducir 19 tools vía Haiku, inyectar asyncio-v1 | ✅ Hecho — 77 total |
| C4 | 30 herramientas lightweight + fixes npm/mongo | ✅ Hecho — 107 total, **81 verified** |

### Bloque D — inmediato

| Paso | Tarea | Coste estimado |
|---|---|---|
| D1 | **Solicitar afiliados Tier A** — Firecrawl, LangFuse, Qdrant (sin tráfico req.) | Zero |
| D2 | **Primera ronda context_injector** — repos MIT/Apache-2.0 del catálogo | Zero LLM |
| D3 | **Primera ronda library_ghost** — snippets LangChain/CrewAI/AutoGPT/Haystack | Max 10×Sonnet ~$0.03 |
| D4 | Implementar Modelo II SaaS (API keys + rate limiting + /api/v1/bulk) | Zero LLM |
| D5 | Cron jobs en Railway para B1/B4/B5 automáticos | Zero LLM |
| D6 | HN post: "I built an AI-to-AI marketplace where bots pay taxes in telemetry" | — |

### Coste Anthropic estimado en estado estacionario

| Agente | Coste/mes estimado |
|---|---|
| Sentinel (foam≥4, ~5 repos/semana) | ~$0.20 |
| Semantic Oracle (affinity>0.85, ~5% del catálogo) | ~$0.05 |
| Zero Waste Auditor (reglas primero) | ~$0.02 |
| Library Ghost (max 10 snippets) | ~$0.30 |
| Translator (nuevas herramientas, Haiku) | ~$0.10 |
| **Total mensual** | **~$0.67** |

Presupuesto máximo declarado: £8/mes. Margen actual: **>91%**.

"""
AIAAM Semantic Oracle — Agente B4
Calcula afinidad entre herramientas con coseno (numpy, sin LLM).
Solo llama a Haiku cuando afinidad > 0.85 para generar una frase
de suggested_workflow (~20 tokens por par).

Principio de coste máximo:
- Coseno y vectorización: numpy puro, £0
- Haiku: SOLO si afinidad > AFFINITY_THRESHOLD (0.85), ~20 tokens
- Cache 24h: si suggested_workflow fue calculado hace <24h, no recalcula
- Top 100 herramientas por total_requests

Uso:
    python3 semantic_oracle.py             # corre sobre top 100
    python3 semantic_oracle.py --limit 20  # solo top N
    python3 semantic_oracle.py --aid yt-dlp-v1  # recalcula uno
    python3 semantic_oracle.py --dry-run   # no escribe en DB ni llama LLM
"""

import os
import re
import sys
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
from anthropic import Anthropic
from sqlmodel import Session, select
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import engine, init_db
from models import Tool

load_dotenv()

ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY")
AFFINITY_THRESHOLD   = 0.85   # por debajo de este umbral: no se llama a LLM
CACHE_HOURS          = 24     # horas de validez del suggested_workflow en DB
TOP_N_DEFAULT        = 100    # herramientas a procesar por defecto
MODEL_HAIKU          = os.getenv("TRANSLATOR_MODEL_PRIMARY", "claude-haiku-4-5-20251001")

client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# =====================================================================
# VECTORIZACIÓN — TF-IDF simplificado, numpy puro, sin sklearn
# =====================================================================

_STOPWORDS = {"install", "pip", "npm", "run", "use", "the", "a", "an", "and", "or",
              "to", "of", "in", "for", "with", "v1", "v2", "v3", "lib", "api"}


def _tokenize(tool: Tool) -> list[str]:
    """Extrae tokens representativos del tool. Sin LLM."""
    parts = [
        tool.aid or "",
        tool.source_platform or "",
        (tool.install_cmd or "").lower(),
        (tool.execute_cmd or "").lower(),
        str(tool.input_schema.get("type", "") if tool.input_schema else ""),
        str(tool.output_schema.get("type", "") if tool.output_schema else ""),
        " ".join(tool.input_schema.get("format", []) if tool.input_schema else []),
        " ".join(tool.output_schema.get("format", []) if tool.output_schema else []),
    ]
    raw = " ".join(parts)
    tokens = re.findall(r"[a-z0-9]+", raw.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]


def build_tfidf_matrix(tools: list[Tool]) -> tuple[np.ndarray, list[str]]:
    """
    Construye matriz TF-IDF (n_tools × vocab_size) con numpy puro.
    Devuelve (matrix_normalizada, vocabulary).
    """
    # 1. Tokenización
    tokenized = [_tokenize(t) for t in tools]

    # 2. Vocabulario
    vocab = sorted({tok for doc in tokenized for tok in doc})
    vocab_idx = {w: i for i, w in enumerate(vocab)}
    V = len(vocab)
    N = len(tools)

    # 3. TF (term frequency)
    tf = np.zeros((N, V), dtype=np.float32)
    for i, tokens in enumerate(tokenized):
        for tok in tokens:
            tf[i, vocab_idx[tok]] += 1
        if tokens:
            tf[i] /= len(tokens)

    # 4. IDF (inverse document frequency)
    df = np.count_nonzero(tf, axis=0).astype(np.float32)
    idf = np.log((N + 1) / (df + 1)) + 1.0

    tfidf = tf * idf

    # 5. Normalización L2 por fila
    norms = np.linalg.norm(tfidf, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return tfidf / norms, vocab


def cosine_similarity_matrix(matrix: np.ndarray) -> np.ndarray:
    """Devuelve matriz n×n de similitudes coseno. Las filas ya están normalizadas."""
    return matrix @ matrix.T


# =====================================================================
# LLM — Haiku solo si afinidad > AFFINITY_THRESHOLD (~20 tokens)
# =====================================================================

def generate_workflow_sentence(tool_a: Tool, tool_b: Tool) -> Optional[str]:
    """
    Genera UNA frase de suggested_workflow con Haiku.
    Solo se llama si afinidad > AFFINITY_THRESHOLD.
    ~20 tokens. Coste aprox £0.000015 por llamada.
    """
    if not client:
        return None
    prompt = (
        f"In one short sentence (max 15 words), describe a practical workflow "
        f"combining '{tool_a.aid}' and '{tool_b.aid}'. "
        f"Focus on what the combined tools achieve together. "
        f"Output ONLY the sentence, no explanation."
    )
    try:
        resp = client.messages.create(
            model=MODEL_HAIKU,
            max_tokens=40,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as exc:
        print(f"    [haiku] error: {exc}")
        return None


# =====================================================================
# CACHE CHECK
# =====================================================================

def _is_cached(tool: Tool) -> bool:
    """True si suggested_workflow fue calculado hace menos de CACHE_HOURS."""
    wf = tool.suggested_workflow
    if not wf or "computed_at" not in wf:
        return False
    try:
        computed = datetime.fromisoformat(wf["computed_at"])
        if computed.tzinfo is None:
            computed = computed.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - computed
        return age < timedelta(hours=CACHE_HOURS)
    except Exception:
        return False


# =====================================================================
# CORE — procesa un tool
# =====================================================================

def compute_oracle_for_tool(
    tool: Tool,
    tools: list[Tool],
    sim_row: np.ndarray,
    session: Session,
    dry_run: bool = False,
) -> Optional[dict]:
    """
    Para un tool dado, busca el par más afín y decide si llamar a Haiku.
    Actualiza tool.suggested_workflow en DB. Devuelve el dict o None.
    """
    if _is_cached(tool):
        print(f"    CACHE HIT — skipping")
        return tool.suggested_workflow

    # Índice del tool en la lista
    try:
        idx = tools.index(tool)
    except ValueError:
        return None

    # Mejor par (excluye a sí mismo)
    row = sim_row.copy()
    row[idx] = -1.0
    best_idx = int(np.argmax(row))
    best_score = float(row[best_idx])
    best_tool = tools[best_idx]

    print(f"    best pair: {best_tool.aid}  affinity={best_score:.3f}")

    workflow_text = None
    if best_score >= AFFINITY_THRESHOLD:
        if dry_run:
            print(f"    DRY — would call Haiku (affinity={best_score:.3f} >= {AFFINITY_THRESHOLD})")
        else:
            print(f"    HAIKU — generating workflow sentence...")
            workflow_text = generate_workflow_sentence(tool, best_tool)
            print(f"    → \"{workflow_text}\"")
    else:
        print(f"    NO LLM — affinity below threshold ({best_score:.3f} < {AFFINITY_THRESHOLD})")

    result = {
        "paired_with": best_tool.aid,
        "affinity_score": round(best_score, 4),
        "workflow": workflow_text,
        "computed_at": datetime.utcnow().isoformat(),
    }

    if not dry_run:
        tool.suggested_workflow = result
        tool.updated_at = datetime.utcnow()
        session.add(tool)
        session.commit()

    return result


# =====================================================================
# MAIN RUN
# =====================================================================

def run(
    aid: Optional[str] = None,
    limit: int = TOP_N_DEFAULT,
    dry_run: bool = False,
) -> dict:
    """
    Corre el oracle sobre los top N tools por total_requests.
    Si aid se especifica, procesa solo ese tool (ignora cache).
    """
    init_db()
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\n[oracle] {ts} — computing semantic affinities...")

    results = {"computed": 0, "cached": 0, "haiku_calls": 0}

    with Session(engine) as session:
        if aid:
            target = session.get(Tool, aid)
            if not target:
                print(f"[oracle] ERROR: aid '{aid}' not found")
                return results
            # Carga top N para calcular similitudes
            top_tools = session.exec(
                select(Tool).order_by(Tool.total_requests.desc()).limit(limit)
            ).all()
            if target not in top_tools:
                top_tools = [target] + list(top_tools)
            process_list = [target]
        else:
            top_tools = session.exec(
                select(Tool).order_by(Tool.total_requests.desc()).limit(limit)
            ).all()
            process_list = list(top_tools)

        if len(top_tools) < 2:
            print(f"[oracle] Catálogo insuficiente (<2 herramientas). Nada que computar.")
            return results

        print(f"[oracle] vectorizando {len(top_tools)} herramientas...")
        matrix, vocab = build_tfidf_matrix(list(top_tools))
        sim_matrix = cosine_similarity_matrix(matrix)
        print(f"[oracle] vocabulario: {len(vocab)} términos · matriz: {sim_matrix.shape}\n")

        for tool in process_list:
            print(f"  → {tool.aid}")
            if _is_cached(tool) and not aid:
                print(f"    CACHE HIT — skipping")
                results["cached"] += 1
                continue

            try:
                tool_idx = list(top_tools).index(tool)
            except ValueError:
                continue

            wf = compute_oracle_for_tool(
                tool=tool,
                tools=list(top_tools),
                sim_row=sim_matrix[tool_idx],
                session=session,
                dry_run=dry_run,
            )

            if wf:
                results["computed"] += 1
                if wf.get("workflow"):
                    results["haiku_calls"] += 1

    print(
        f"\n[oracle] done — computed={results['computed']} "
        f"cached={results['cached']} haiku_calls={results['haiku_calls']}"
    )
    return results


# =====================================================================
# CLI
# =====================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM Semantic Oracle")
    parser.add_argument("--aid",     type=str, help="Procesar un tool específico")
    parser.add_argument("--limit",   type=int, default=TOP_N_DEFAULT,
                        help=f"Top N tools a procesar (default {TOP_N_DEFAULT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview sin escribir en DB ni llamar a Haiku")
    args = parser.parse_args()

    run(aid=args.aid, limit=args.limit, dry_run=args.dry_run)

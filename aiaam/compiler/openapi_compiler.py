"""
AIAAM OpenAPI Compiler Engine
Fetches a public OpenAPI/Swagger spec and compresses it into a MAI-API
manifest via claude-haiku-4-5-20251001. Zero prose — pure JSON output.

Cost control:
  - Input truncated to MAX_INPUT_TOKENS (8000) before sending to Haiku
  - If spec exceeds limit, only the first MAX_INTENTS paths are compiled
  - Emits a warning if per-call cost pushes estimated monthly spend > £6

Usage (standalone test):
  python3 compiler/openapi_compiler.py <openapi_url>
  python3 compiler/openapi_compiler.py  # uses petstore default
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

import httpx
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

# Try explicit path first (repo root), then cwd fallback, both with override=True
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=True)
else:
    load_dotenv(override=True)

# ── Constants ────────────────────────────────────────────────────────
HAIKU_MODEL        = "claude-haiku-4-5-20251001"
MAX_INPUT_TOKENS   = 8000
MAX_INTENTS        = 20

# Cost estimates (USD) — Haiku pricing
_HAIKU_IN_COST_PER_TOK  = 0.80  / 1_000_000   # $0.80 per million input tokens
_HAIKU_OUT_COST_PER_TOK = 4.00  / 1_000_000   # $4.00 per million output tokens
_GBP_PER_USD            = 0.787                # approximate
_MONTHLY_WARN_GBP       = 6.0

# Assumed ~30 compilations/month for warning calculation
_ASSUMED_MONTHLY_COMPILATIONS = 30

SYSTEM_PROMPT = """\
You are a semantic compression engine for AI agents.
Input: a raw OpenAPI/Swagger specification.
Output: ONLY a valid JSON object. No prose. No markdown. No explanation.
Compress the spec into this exact structure:
{
  "service": "string",
  "base_url": "string",
  "auth": {"type": "apikey|bearer|none", "header": "string"},
  "intents": [
    {
      "id": "snake_case_intent_name",
      "method": "GET|POST|PUT|DELETE",
      "path": "string",
      "params": {"param_name": "type"},
      "returns": "description_under_10_words"
    }
  ]
}
Rules:
- Maximum 20 intents (most important endpoints only)
- param types: string, number, boolean, array only
- Strip ALL human descriptions longer than 10 words
- No null values — omit optional fields entirely
- Output must be parseable by JSON.parse() with zero modifications\
"""


# ── Token helpers ────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 chars per token (standard rule of thumb)."""
    return len(text) // 4


def _cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * _HAIKU_IN_COST_PER_TOK +
            output_tokens * _HAIKU_OUT_COST_PER_TOK)


def _warn_if_over_budget(input_tokens: int, output_tokens: int) -> None:
    call_cost_gbp = _cost_usd(input_tokens, output_tokens) * _GBP_PER_USD
    monthly_est   = call_cost_gbp * _ASSUMED_MONTHLY_COMPILATIONS
    if monthly_est > _MONTHLY_WARN_GBP:
        print(
            f"[compiler] ⚠ COST WARNING: this call = £{call_cost_gbp:.4f} · "
            f"est. monthly ({_ASSUMED_MONTHLY_COMPILATIONS} calls) = "
            f"£{monthly_est:.2f} > £{_MONTHLY_WARN_GBP} budget",
            file=sys.stderr,
        )


# ── Spec fetch & parse ───────────────────────────────────────────────

def fetch_spec(url: str) -> dict:
    """Download and parse a JSON or YAML OpenAPI spec. Raises on failure."""
    r = httpx.get(url, follow_redirects=True, timeout=15)
    r.raise_for_status()
    content_type = r.headers.get("content-type", "")
    text = r.text

    if "yaml" in content_type or url.endswith((".yaml", ".yml")):
        return yaml.safe_load(text)
    try:
        return r.json()
    except Exception:
        return yaml.safe_load(text)   # fallback: some servers send YAML with wrong CT


# ── Spec truncation ──────────────────────────────────────────────────

def _truncate_spec(spec: dict) -> tuple[dict, bool]:
    """
    If the JSON representation exceeds MAX_INPUT_TOKENS, reduce the spec
    to its first MAX_INTENTS paths. Returns (spec, was_truncated).
    """
    spec_text = json.dumps(spec)
    if _estimate_tokens(spec_text) <= MAX_INPUT_TOKENS:
        return spec, False

    paths     = spec.get("paths", {})
    top_paths = dict(list(paths.items())[:MAX_INTENTS])
    truncated = {
        "openapi": spec.get("openapi", spec.get("swagger", "3.0.0")),
        "info":    spec.get("info", {}),
        "servers": spec.get("servers", []),
        "paths":   top_paths,
    }
    return truncated, True


# ── Core compilation ─────────────────────────────────────────────────

def _repair_truncated_json(raw: str) -> Optional[dict]:
    """
    Haiku sometimes hits max_tokens mid-JSON, leaving open arrays/objects.
    Strategy: truncate the intents array at the last fully-closed intent,
    then close all open structures. Returns parsed dict or None on failure.
    """
    # Find the last complete intent object: ends with }
    # Walk backwards from end, try to close open braces/brackets
    text = raw.rstrip()

    # Count open braces/brackets to figure out what needs closing
    depth_brace   = text.count("{") - text.count("}")
    depth_bracket = text.count("[") - text.count("]")

    # Remove any trailing partial object (find last complete },)
    # by truncating at the last "}" before any trailing incomplete tokens
    last_complete = text.rfind("},")
    if last_complete == -1:
        last_complete = text.rfind("}")
    if last_complete == -1:
        return None

    candidate = text[: last_complete + 1]  # up to and including the last "}"

    # Close remaining open brackets and braces
    depth_brace   = candidate.count("{") - candidate.count("}")
    depth_bracket = candidate.count("[") - candidate.count("]")

    closing = "]" * depth_bracket + "}" * depth_brace
    repaired = candidate + closing

    try:
        result = json.loads(repaired)
        print("[compiler] ⚠ JSON repaired (was truncated at max_tokens)", file=sys.stderr)
        return result
    except json.JSONDecodeError:
        return None


def compile_spec(spec: dict, source_url: str = "") -> dict:
    """
    Send the (possibly truncated) spec to Haiku and return:
    {
        "manifest":       dict,   # the mai-api.json
        "tokens_used":    int,    # input + output tokens
        "was_truncated":  bool,
        "source_url":     str,
    }
    Raises ValueError if Haiku response is not valid JSON.
    """
    truncated_spec, was_truncated = _truncate_spec(spec)
    if was_truncated:
        path_count = len(truncated_spec.get("paths", {}))
        print(
            f"[compiler] spec truncated → first {path_count} paths "
            f"(original estimated >{MAX_INPUT_TOKENS} tokens)",
            file=sys.stderr,
        )

    spec_text = json.dumps(truncated_spec, indent=None)   # compact for token savings
    input_tok_estimate = _estimate_tokens(spec_text)
    print(
        f"[compiler] sending ~{input_tok_estimate} estimated tokens to {HAIKU_MODEL}",
        file=sys.stderr,
    )

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set in environment")

    client   = Anthropic(api_key=api_key)
    response = client.messages.create(
        model      = HAIKU_MODEL,
        max_tokens = 4096,
        system     = SYSTEM_PROMPT,
        messages   = [{"role": "user", "content": spec_text}],
    )

    raw_text = response.content[0].text.strip()
    in_toks  = response.usage.input_tokens
    out_toks = response.usage.output_tokens
    total    = in_toks + out_toks

    print(
        f"[compiler] Haiku used {in_toks} input + {out_toks} output = {total} tokens "
        f"(cost ≈ £{_cost_usd(in_toks, out_toks) * _GBP_PER_USD:.4f})",
        file=sys.stderr,
    )
    _warn_if_over_budget(in_toks, out_toks)

    # Strip accidental markdown fences if model adds them
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        raw_text = "\n".join(
            l for l in lines if not l.startswith("```")
        ).strip()

    try:
        manifest = json.loads(raw_text)
    except json.JSONDecodeError:
        # Haiku hit max_tokens mid-JSON — repair by closing open structures
        manifest = _repair_truncated_json(raw_text)
        if manifest is None:
            raise ValueError(
                f"Haiku returned non-JSON output. "
                f"First 200 chars: {raw_text[:200]!r}"
            )

    return {
        "manifest":      manifest,
        "tokens_used":   total,
        "was_truncated": was_truncated,
        "source_url":    source_url,
    }


def compile_from_url(url: str) -> dict:
    """Convenience: fetch + compile in one call."""
    spec = fetch_spec(url)
    return compile_spec(spec, source_url=url)


def save_to_db(result: dict, category: str) -> "CompiledAPI":
    """
    Persist a compiled manifest to the compiled_apis table.
    Upserts on service_name (re-compilation updates the record).
    Returns the saved CompiledAPI instance.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sqlmodel import Session, select
    from database import engine, init_db
    from models import CompiledAPI

    init_db()

    manifest     = result["manifest"]
    service_name = manifest.get("service", "unknown").lower().replace(" ", "_")

    with Session(engine) as session:
        existing = session.exec(
            select(CompiledAPI).where(CompiledAPI.service_name == service_name)
        ).first()

        if existing:
            existing.manifest      = manifest
            existing.tokens_used   = result["tokens_used"]
            existing.source_url    = result["source_url"]
            existing.category      = category
            existing.compiled_at   = __import__("datetime").datetime.utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
            return existing

        record = CompiledAPI(
            service_name      = service_name,
            category          = category,
            source_url        = result["source_url"],
            manifest          = manifest,
            tokens_used       = result["tokens_used"],
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record


# ── CLI test runner ──────────────────────────────────────────────────

if __name__ == "__main__":
    test_url = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "https://petstore3.swagger.io/api/v3/openapi.json"
    )
    print(f"\n[compiler] compiling: {test_url}\n", file=sys.stderr)
    result = compile_from_url(test_url)
    print("\n=== COMPILED mai-api.json ===")
    print(json.dumps(result["manifest"], indent=2))
    print(f"\n=== METADATA ===")
    print(f"tokens_used   : {result['tokens_used']}")
    print(f"was_truncated : {result['was_truncated']}")
    print(f"source_url    : {result['source_url']}")

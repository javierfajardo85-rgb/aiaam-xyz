"""
AIAAM Translator - Mode B (Selective Review)

Pipeline:
1. Fetch README/model card/package metadata from source
2. Haiku 4.5 produces MAI-1 draft
3. Validator inspects critical fields
4. If any critical field is null/malformed → Sonnet 4.6 fixes ONLY those fields
5. Final MAI-1 saved to DB

Sources supported:
- github.com → translate_readme (LLM)
- huggingface.co → translate_model_card (LLM, variant prompt)
- pypi.org → translate_package_metadata (direct mapping, no LLM)
- npmjs.com → translate_package_metadata (direct mapping, no LLM)
"""
import os
import json
import re
import time
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

from models import Tool

load_dotenv(override=True)

# Configuration
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
HF_TOKEN = os.getenv("HF_TOKEN")
MODEL_PRIMARY = os.getenv("TRANSLATOR_MODEL_PRIMARY", "claude-haiku-4-5-20251001")
MODEL_REVIEWER = os.getenv("TRANSLATOR_MODEL_REVIEWER", "claude-sonnet-4-6")

client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


# =====================================================================
# PROMPTS — Strict JSON output, declarative, deterministic
# =====================================================================

PROMPT_GITHUB_README = """ROLE: JSON converter. Input: README sections. Output: MAI-1 object.

STRICT RULES:
- Output ONLY a single valid JSON object. No markdown. No explanation. No code fences.
- NEVER invent data. If a field cannot be determined from the text, use null. No exceptions.
- aid: lowercase slug from repo name + "-v1". Example: "yt-dlp" -> "yt-dlp-v1"
- version: look ONLY for explicit version strings in badge markdown or release headers
  (e.g. "v2.1.0", "2024.1"). Do NOT extract from import statements or code. null if absent.
- input_schema / output_schema: object with "type" and optional "format".
  type — ONLY one of: "url" "file" "string" "json" "stream" "image" "audio"
  format — array of max 5 concrete file extensions (e.g. ["mp4","wav","json","png"]).
  NEVER use narrative phrases ("thousands of sites", "various", "many", etc.).
  NEVER use adjectives or descriptions. Omit format entirely if no concrete extensions exist.
- reliability_score: always 0.75. Do not change this value.
- latency_ms: 100 (CLI), 500 (API call), 800 (ML model), null (unclear). No other values.
- source_url: exact GitHub URL provided. Do not modify.
- install_cmd: copy the EXACT shortest install command from the Installation section.
  Accepted forms: pip install X, npm install X, docker run ..., brew install X.
  Do NOT modify the command. null if absent.
- execute_cmd: SINGLE LINE, max 120 chars. Extract from Usage or Quickstart section ONLY.
  Never from Development, Contributing, or Build sections.
  Replace all user-specific values with {placeholder} variables.
  Join multi-step commands with " && ". NEVER use \\n.
  NEVER invent model names, endpoints, or API keys. null if absent.

EXAMPLE (inline, one JSON object):
{"aid":"yt-dlp-v1","version":null,"input_schema":{"type":"url","format":["youtube","mp4"]},"output_schema":{"type":"file","format":["mp4","mp3"]},"reliability_score":0.75,"latency_ms":100,"source_url":"https://github.com/yt-dlp/yt-dlp","install_cmd":"pip install yt-dlp","execute_cmd":"yt-dlp {url}"}

REPO URL: {source_url}
README SECTIONS:
{readme_content}"""


PROMPT_HF_MODEL_CARD = """ROLE: JSON converter. Input: HuggingFace model card. Output: MAI-1 object.

STRICT RULES:
- Output ONLY a single valid JSON object. No markdown. No explanation. No code fences.
- NEVER invent data. If a field cannot be determined, use null. No exceptions.
- aid: lowercase slug from model_id (replace "/" with "-") + "-v1".
  Example: "openai/whisper-large" -> "openai-whisper-large-v1"
- version: model version if explicitly stated, otherwise null. Do not infer.
- input_schema / output_schema: object with "type" and optional "format".
  type — ONLY one of: "url" "file" "string" "json" "stream" "image" "audio"
  format — array of max 5 concrete file extensions (e.g. ["mp3","wav","flac"]).
  NEVER use narrative phrases. Omit format if no concrete extensions exist.
- reliability_score: 0.75 (default, do not change).
- latency_ms: 800 (all ML models default).
- source_url: full HuggingFace URL provided. Do not modify.
- install_cmd: "pip install transformers" unless a specific library is required.
- execute_cmd: pipeline call format, SINGLE LINE, max 120 chars.
  Use the exact model_id as a literal string (not a placeholder).
  Replace task-specific inputs with {placeholder} variables.

EXAMPLE (inline):
{"aid":"openai-whisper-large-v3-v1","version":"v3","input_schema":{"type":"audio","format":["mp3","wav","flac"]},"output_schema":{"type":"string"},"reliability_score":0.75,"latency_ms":800,"source_url":"https://huggingface.co/openai/whisper-large-v3","install_cmd":"pip install transformers","execute_cmd":"pipeline('automatic-speech-recognition', model='openai/whisper-large-v3')('{audio_path}')"}

MODEL URL: {source_url}
MODEL CARD:
{model_card}"""


PROMPT_REVIEWER = """ROLE: MAI-1 quality reviewer. Fix ONLY the broken fields.

You will receive:
1. The original source content (README or model card)
2. A draft MAI-1 generated by a smaller model

TASK:
- Identify fields that are null, malformed, or clearly wrong.
- Fix ONLY those fields. Keep correct fields untouched.
- Return the complete corrected MAI-1 JSON.
- Output ONLY valid JSON. No markdown. No explanation.

CRITICAL FIELDS (must not be null if information exists in source):
- install_cmd
- execute_cmd
- input_schema
- output_schema

ORIGINAL SOURCE:
{source_content}

DRAFT MAI-1:
{draft_mai1}"""


# =====================================================================
# SOURCE FETCHERS — Get raw content from each platform
# =====================================================================

def fetch_github_readme(repo_url: str) -> Optional[str]:
    """Fetch README content from a GitHub repo URL via the GitHub API."""
    parsed = urlparse(repo_url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]

    headers = {"Accept": "application/vnd.github.v3.raw"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    # Primary: GitHub API readme endpoint (works without raw.githubusercontent.com)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    resp = httpx.get(api_url, headers=headers, timeout=30.0, follow_redirects=True)
    if resp.status_code == 200:
        content = resp.text
        if len(content) > 50:
            return content[:8000]

    # Fallback: raw.githubusercontent.com
    raw_headers = {"Accept": "application/vnd.github.v3.raw"}
    if GITHUB_TOKEN:
        raw_headers["Authorization"] = f"token {GITHUB_TOKEN}"
    for branch in ["main", "master"]:
        for filename in ["README.md", "README.rst", "README.txt", "readme.md"]:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{filename}"
            try:
                resp = httpx.get(url, headers=raw_headers, timeout=30.0, follow_redirects=True)
                if resp.status_code == 200 and len(resp.text) > 50:
                    return resp.text[:8000]
            except Exception:
                continue
    return None


def fetch_hf_model_card(model_url: str) -> Optional[str]:
    """Fetch HuggingFace model card via API."""
    parsed = urlparse(model_url)
    parts = parsed.path.strip("/").split("/")
    if len(parts) < 2:
        return None
    model_id = "/".join(parts[:2])

    headers = {}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"

    url = f"https://huggingface.co/api/models/{model_id}"
    try:
        resp = httpx.get(url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            card = data.get("cardData", {})
            description = json.dumps(card)[:8000]
            return f"Model ID: {model_id}\nMetadata: {description}"
    except Exception:
        return None
    return None


def fetch_pypi_metadata(package_url: str) -> Optional[dict]:
    """Fetch PyPI package metadata via JSON API."""
    parsed = urlparse(package_url)
    parts = parsed.path.strip("/").split("/")
    if "project" not in parts:
        return None
    idx = parts.index("project")
    if idx + 1 >= len(parts):
        return None
    package = parts[idx + 1]

    url = f"https://pypi.org/pypi/{package}/json"
    try:
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        return None
    return None


def fetch_npm_metadata(package_url: str) -> Optional[dict]:
    """Fetch npm package metadata via JSON API. Handles scoped packages (@scope/name)."""
    parsed = urlparse(package_url)
    parts = parsed.path.strip("/").split("/")
    if "package" not in parts:
        return None
    idx = parts.index("package")
    if idx + 1 >= len(parts):
        return None

    # Scoped package: @scope/name → parts[idx+1]="@scope", parts[idx+2]="name"
    if parts[idx + 1].startswith("@") and idx + 2 < len(parts):
        package = f"{parts[idx + 1]}/{parts[idx + 2]}"
    else:
        package = parts[idx + 1]

    url = f"https://registry.npmjs.org/{package}"
    try:
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        return None
    return None


# =====================================================================
# README SECTION EXTRACTOR — Reduces LLM input ~15k → ~3k tokens
# =====================================================================

# Keywords that identify sections worth sending to the LLM.
# Only these contain install commands and usage examples.
CRITICAL_SECTIONS = {
    "installation", "install", "setup", "getting started", "get started",
    "usage", "quickstart", "quick start", "quick-start",
    "examples", "example", "how to use", "how to run",
    "requirements", "prerequisites",
}


def extract_critical_sections(readme_text: str) -> str:
    """
    Extract only Installation + Usage sections from a README.
    Reduces LLM input from ~15,000 chars to ~3,000.

    If no structured headers are found (e.g. README has no ## sections),
    falls back to the first 3,000 characters of the raw text.
    """
    lines = readme_text.split("\n")
    sections: dict = {}
    current_key: Optional[str] = None
    current_lines: list = []

    for line in lines:
        header = re.match(r"^#{1,4}\s+(.+)", line)
        if header:
            # Flush previous section
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = header.group(1).strip().lower()
            current_lines = [line]
        elif current_key is not None:
            current_lines.append(line)

    # Flush last section
    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    # Collect only critical sections (cap each at 1,500 chars)
    extracted: list = []
    for title, body in sections.items():
        if any(kw in title for kw in CRITICAL_SECTIONS):
            extracted.append(body[:1500])

    if extracted:
        combined = "\n\n".join(extracted)
        # Only use extracted content if it's substantial enough for the LLM
        if len(combined) >= 150:
            return combined[:3000]

    # Fallback: no sections found OR extracted content too short (e.g. emoji headers
    # with minimal content, READMEs with non-standard structure)
    return readme_text[:3000]


# =====================================================================
# RELIABILITY CALCULATOR — Dynamic initial score from README signals
# =====================================================================

def calculate_initial_reliability(readme_text: str) -> float:
    """
    Score a README's quality signals to set an honest initial reliability_score.

    Scoring:
      base                          +0.50
      has code blocks (```)         +0.20
      has installation section      +0.15
      has usage / examples section  +0.10
      README < 500 chars            -0.30  (stub / empty README)
    Cap: 0.85  (real score only rises with actual execution feedback)
    """
    score = 0.50
    lower = readme_text.lower()

    if "```" in readme_text:
        score += 0.20

    if re.search(r"#{1,4}\s*(install|setup|getting.?started)", lower):
        score += 0.15

    if re.search(r"#{1,4}\s*(usage|quickstart|quick.?start|example)", lower):
        score += 0.10

    if len(readme_text) < 500:
        score -= 0.30

    return round(min(score, 0.85), 2)


# =====================================================================
# DIRECT MAPPERS — No LLM needed for PyPI and npm
# =====================================================================

# Curated execute_cmd / schema patterns for well-known PyPI packages.
# Agents need actionable commands, not bare imports.
_PYPI_KNOWN: dict = {
    "requests": {
        "input_schema":  {"type": "url", "format": "http|https"},
        "output_schema": {"type": "string", "format": "html|text|json"},
        "execute_cmd":   "requests.get('{url}').text",
        "latency_ms":    100,
    },
    "httpx": {
        "input_schema":  {"type": "url", "format": "http|https"},
        "output_schema": {"type": "string", "format": "html|text|json"},
        "execute_cmd":   "httpx.get('{url}').text",
        "latency_ms":    100,
    },
    "beautifulsoup4": {
        "input_schema":  {"type": "string", "format": "html|xml"},
        "output_schema": {"type": "string"},
        "execute_cmd":   "BeautifulSoup('{html}', 'html.parser').get_text()",
        "latency_ms":    100,
    },
    "pandas": {
        "input_schema":  {"type": "file", "format": ["csv", "json", "xlsx"]},
        "output_schema": {"type": "json", "format": "dataframe"},
        "execute_cmd":   "pd.read_csv('{file}')",
        "latency_ms":    100,
    },
    "numpy": {
        "input_schema":  {"type": "json", "format": "array|matrix"},
        "output_schema": {"type": "json", "format": "ndarray"},
        "execute_cmd":   "np.array({data})",
        "latency_ms":    100,
    },
    "fastapi": {
        "input_schema":  {"type": "json", "format": "http_request"},
        "output_schema": {"type": "json", "format": "http_response"},
        "execute_cmd":   "app = FastAPI(); @app.get('{path}') def handler(): return {response}",
        "latency_ms":    100,
    },
    "pydantic": {
        "input_schema":  {"type": "json"},
        "output_schema": {"type": "json"},
        "execute_cmd":   "class Model(BaseModel): field: {type}\nModel(**{data})",
        "latency_ms":    100,
    },
    "anthropic": {
        "input_schema":  {"type": "string", "format": "prompt"},
        "output_schema": {"type": "string"},
        "execute_cmd":   "Anthropic().messages.create(model='{model}', max_tokens=1024, messages=[{'role':'user','content':'{prompt}'}]).content[0].text",
        "latency_ms":    800,
    },
    "openai": {
        "input_schema":  {"type": "string", "format": "prompt"},
        "output_schema": {"type": "json"},
        "execute_cmd":   "OpenAI().chat.completions.create(model='{model}', messages=[{'role':'user','content':'{prompt}'}]).choices[0].message.content",
        "latency_ms":    800,
    },
    "transformers": {
        "input_schema":  {"type": "string", "format": "text|audio|image"},
        "output_schema": {"type": "json", "format": "model_output"},
        "execute_cmd":   "pipeline('{task}', model='{model}')('{input}')",
        "latency_ms":    800,
    },
}

# Curated patterns for well-known npm packages.
_NPM_KNOWN: dict = {
    "@anthropic-ai/sdk": {
        "input_schema":  {"type": "string", "format": "prompt"},
        "output_schema": {"type": "string"},
        "execute_cmd":   "new Anthropic().messages.create({model:'{model}',max_tokens:1024,messages:[{role:'user',content:'{prompt}'}]})",
        "latency_ms":    800,
    },
    "openai": {
        "input_schema":  {"type": "string", "format": "prompt"},
        "output_schema": {"type": "json"},
        "execute_cmd":   "new OpenAI().chat.completions.create({model:'{model}',messages:[{role:'user',content:'{prompt}'}]})",
        "latency_ms":    800,
    },
    "langchain": {
        "input_schema":  {"type": "string"},
        "output_schema": {"type": "string"},
        "execute_cmd":   "new LLMChain({llm:'{llm}',prompt:'{prompt}'}).call({input:'{input}'})",
        "latency_ms":    800,
    },
    "playwright": {
        "input_schema":  {"type": "url", "format": "http|https"},
        "output_schema": {"type": "string", "format": "html|screenshot"},
        "execute_cmd":   "const b = await chromium.launch(); const p = await b.newPage(); await p.goto('{url}'); await b.close()",
        "latency_ms":    500,
    },
}


def map_pypi_to_mai1(metadata: dict, source_url: str) -> dict:
    """Direct mapping PyPI JSON metadata → MAI-1. Zero tokens used."""
    info = metadata.get("info", {})
    name = info.get("name", "unknown").lower()
    known = _PYPI_KNOWN.get(name, {})
    docs_url = (info.get("project_urls") or {}).get("Documentation") or info.get("home_page", "")
    fallback_cmd = f"python -c \"import {name.replace('-', '_')}\"  # docs: {docs_url}" if docs_url else f"python -c \"import {name.replace('-', '_')}\""
    return {
        "aid": f"pypi-{name}-v1",
        "version": info.get("version"),
        "input_schema":  known.get("input_schema",  {"type": "string"}),
        "output_schema": known.get("output_schema", {"type": "json"}),
        "reliability_score": 0.75,
        "latency_ms": known.get("latency_ms", 500),
        "source_url": source_url,
        "install_cmd": f"pip install {name}",
        "execute_cmd": known.get("execute_cmd", fallback_cmd),
    }


def map_npm_to_mai1(metadata: dict, source_url: str) -> dict:
    """Direct mapping npm JSON metadata → MAI-1. Zero tokens used."""
    name = metadata.get("name", "unknown")
    version = metadata.get("dist-tags", {}).get("latest")
    aid_name = name.replace("@", "").replace("/", "-").lower()
    known = _NPM_KNOWN.get(name, {})
    return {
        "aid": f"npm-{aid_name}-v1",
        "version": version,
        "input_schema":  known.get("input_schema",  {"type": "string"}),
        "output_schema": known.get("output_schema", {"type": "json"}),
        "reliability_score": 0.75,
        "latency_ms": known.get("latency_ms", 500),
        "source_url": source_url,
        "install_cmd": f"npm install {name}",
        "execute_cmd": known.get("execute_cmd", f"const mod = require('{name}'); // see: {metadata.get('homepage', '')}"),
    }


# =====================================================================
# LLM CALLS — Haiku translation + Sonnet review
# =====================================================================

def _extract_json(text: str) -> Optional[dict]:
    """Extract JSON object from LLM response, stripping any markdown."""
    # Remove markdown fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    # Find first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def translate_with_haiku(prompt: str) -> Optional[dict]:
    """Call Haiku to generate MAI-1 draft."""
    if not client:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    response = client.messages.create(
        model=MODEL_PRIMARY,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text
    return _extract_json(text)


def review_with_sonnet(source_content: str, draft: dict) -> Optional[dict]:
    """Call Sonnet to fix problematic fields in a draft MAI-1."""
    if not client:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    prompt = PROMPT_REVIEWER.format(
        source_content=source_content[:6000],
        draft_mai1=json.dumps(draft, indent=2),
    )
    response = client.messages.create(
        model=MODEL_REVIEWER,
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text
    return _extract_json(text)


# =====================================================================
# RELIABILITY NORMALIZER — Defensive guard before validate_mai1
# =====================================================================

def _normalize_reliability(mai1: Optional[dict]) -> None:
    """
    Ensure reliability_score is a valid float in [0, 1].
    Mutates in-place. No-op if mai1 is None.
    We override the value in translate() anyway; this just prevents
    validate_mai1 from failing on a bad LLM-generated score.
    """
    if mai1 is None:
        return
    score = mai1.get("reliability_score")
    if not isinstance(score, (int, float)) or not (0.0 <= float(score) <= 1.0):
        mai1["reliability_score"] = 0.75


# =====================================================================
# VALIDATOR — Detects when Sonnet review is needed
# =====================================================================

def validate_mai1(mai1: Optional[dict]) -> Tuple[bool, str]:
    """Returns (is_valid, reason). Trigger Sonnet review if False."""
    if mai1 is None:
        return False, "null_response"

    # Critical fields that block AI execution if missing
    critical = ["aid", "input_schema", "output_schema", "install_cmd", "execute_cmd"]
    for field in critical:
        if mai1.get(field) is None:
            return False, f"missing_{field}"

    # Schema must be objects
    if not isinstance(mai1.get("input_schema"), dict):
        return False, "malformed_input_schema"
    if not isinstance(mai1.get("output_schema"), dict):
        return False, "malformed_output_schema"

    # Reliability score sanity
    score = mai1.get("reliability_score", 0.75)
    if not isinstance(score, (int, float)) or not (0.0 <= score <= 1.0):
        return False, "invalid_reliability_score"

    return True, "ok"


# =====================================================================
# MAIN TRANSLATOR — Routes by source platform
# =====================================================================

def translate(source_url: str) -> Tuple[Optional[dict], str]:
    """
    Translate a source URL to a MAI-1 JSON object.

    Returns: (mai1_dict, translator_used)
    translator_used: "haiku" | "sonnet" | "mapped" | "failed"
    """
    domain = urlparse(source_url).netloc.lower()

    # ===== PyPI: direct mapping, no LLM =====
    if "pypi.org" in domain:
        metadata = fetch_pypi_metadata(source_url)
        if metadata:
            return map_pypi_to_mai1(metadata, source_url), "mapped"
        return None, "failed"

    # ===== npm: direct mapping, no LLM =====
    if "npmjs.com" in domain:
        metadata = fetch_npm_metadata(source_url)
        if metadata:
            return map_npm_to_mai1(metadata, source_url), "mapped"
        return None, "failed"

    # ===== GitHub / Papers with Code: README + LLM =====
    if "github.com" in domain or "paperswithcode.com" in domain:
        readme = fetch_github_readme(source_url)
        if not readme:
            return None, "failed"

        # Strategy 1: extract only critical sections → smaller, focused LLM input
        readme_for_llm = extract_critical_sections(readme)
        # Strategy 3: dynamic reliability from full README quality signals
        reliability = calculate_initial_reliability(readme)

        prompt = PROMPT_GITHUB_README.replace("{source_url}", source_url).replace(
            "{readme_content}", readme_for_llm
        )
        draft = translate_with_haiku(prompt)
        _normalize_reliability(draft)  # guard before validation
        is_valid, reason = validate_mai1(draft)
        if is_valid:
            draft["reliability_score"] = reliability  # override with computed value
            return draft, "haiku"
        # Escalate to Sonnet — give full README for richer context
        print(f"[translator] Haiku draft failed validation ({reason}). Escalating to Sonnet.")
        fixed = review_with_sonnet(readme[:6000], draft or {"source_url": source_url})
        _normalize_reliability(fixed)
        is_valid, _ = validate_mai1(fixed)
        if is_valid:
            fixed["reliability_score"] = reliability
            return fixed, "sonnet"
        return None, "failed"

    # ===== HuggingFace: model card + LLM =====
    if "huggingface.co" in domain:
        card = fetch_hf_model_card(source_url)
        if not card:
            return None, "failed"

        # Strategy 3: dynamic reliability from card quality signals
        reliability = calculate_initial_reliability(card)

        prompt = PROMPT_HF_MODEL_CARD.replace("{source_url}", source_url).replace(
            "{model_card}", card
        )
        draft = translate_with_haiku(prompt)
        _normalize_reliability(draft)
        is_valid, reason = validate_mai1(draft)
        if is_valid:
            draft["reliability_score"] = reliability
            return draft, "haiku"
        print(f"[translator] HF draft failed ({reason}). Escalating to Sonnet.")
        fixed = review_with_sonnet(card, draft or {"source_url": source_url})
        _normalize_reliability(fixed)
        is_valid, _ = validate_mai1(fixed)
        if is_valid:
            fixed["reliability_score"] = reliability
            return fixed, "sonnet"
        return None, "failed"

    return None, "failed"


def translate_and_save(source_url: str, session) -> Optional[Tool]:
    """Translate and persist to DB. Returns the Tool or None on failure."""
    mai1, translator = translate(source_url)
    if not mai1:
        return None

    # Determine source platform
    domain = urlparse(source_url).netloc.lower()
    if "github" in domain:
        platform = "github"
    elif "huggingface" in domain:
        platform = "huggingface"
    elif "pypi" in domain:
        platform = "pypi"
    elif "npmjs" in domain:
        platform = "npm"
    elif "paperswithcode" in domain:
        platform = "paperswithcode"
    else:
        platform = "other"

    tool = Tool(
        aid=mai1["aid"],
        version=mai1.get("version"),
        input_schema=mai1["input_schema"],
        output_schema=mai1["output_schema"],
        reliability_score=mai1.get("reliability_score", 0.75),
        latency_ms=mai1.get("latency_ms"),
        source_url=mai1["source_url"],
        install_cmd=mai1.get("install_cmd"),
        execute_cmd=mai1.get("execute_cmd"),
        source_platform=platform,
        translator_used=translator,
    )
    try:
        session.merge(tool)
        session.commit()
    except Exception as exc:
        session.rollback()
        raise RuntimeError(f"DB save failed for aid={tool.aid}: {exc}") from exc
    return tool


if __name__ == "__main__":
    # Quick test (requires API key)
    test_urls = [
        "https://github.com/yt-dlp/yt-dlp",
        "https://pypi.org/project/requests/",
    ]
    for url in test_urls:
        print(f"\nTranslating: {url}")
        mai1, translator = translate(url)
        print(f"Translator used: {translator}")
        if mai1:
            print(json.dumps(mai1, indent=2))

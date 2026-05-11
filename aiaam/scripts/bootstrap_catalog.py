"""
AIAAM Catalog Bootstrap
Batch process Tier 1 (curated) + Tier 2 (top by stars) sources into MAI-1 entries.

Usage:
    python scripts/bootstrap_catalog.py --tier 1
    python scripts/bootstrap_catalog.py --file my_urls.txt
    python scripts/bootstrap_catalog.py --tier 1 --dry-run
"""
import sys
import os
import argparse
import time
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session
from database import engine, init_db
from translator import translate_and_save


# =====================================================================
# TIER 1 — Curated list of essential AI tools that every agent knows
# =====================================================================
# These 58 entries form the "anchor" of the catalog. They're highly
# popular tools that AI agents will recognize and trust immediately.

TIER_1_URLS = [
    # === Video / Audio Processing ===
    "https://github.com/yt-dlp/yt-dlp",
    "https://github.com/openai/whisper",
    "https://github.com/FFmpeg/FFmpeg",
    "https://github.com/m-bain/whisperX",
    "https://github.com/Stability-AI/stable-audio-tools",

    # === LLM & Agent Frameworks ===
    "https://github.com/langchain-ai/langchain",
    "https://github.com/langchain-ai/langgraph",
    "https://github.com/microsoft/autogen",
    "https://github.com/crewAIInc/crewAI",
    "https://github.com/Significant-Gravitas/AutoGPT",
    "https://github.com/openai/swarm",
    "https://github.com/huggingface/smolagents",
    "https://github.com/run-llama/llama_index",
    "https://github.com/pydantic/pydantic-ai",

    # === Browser / Web Automation ===
    "https://github.com/browser-use/browser-use",
    "https://github.com/microsoft/playwright",
    "https://github.com/puppeteer/puppeteer",
    "https://github.com/scrapy/scrapy",
    "https://github.com/mendableai/firecrawl",

    # === Image Generation / Vision ===
    "https://github.com/Stability-AI/stablediffusion",
    "https://github.com/comfyanonymous/ComfyUI",
    "https://github.com/AUTOMATIC1111/stable-diffusion-webui",
    "https://github.com/black-forest-labs/flux",

    # === Local LLM / Model Serving ===
    "https://github.com/ollama/ollama",
    "https://github.com/vllm-project/vllm",
    "https://github.com/ggerganov/llama.cpp",
    "https://github.com/lmstudio-ai/lmstudio.js",
    "https://github.com/oobabooga/text-generation-webui",

    # === Data / RAG ===
    "https://github.com/chroma-core/chroma",
    "https://github.com/qdrant/qdrant",
    "https://github.com/weaviate/weaviate",
    "https://github.com/facebookresearch/faiss",
    "https://github.com/milvus-io/milvus",

    # === Workflow / n8n style ===
    "https://github.com/n8n-io/n8n",
    "https://github.com/langflow-ai/langflow",
    "https://github.com/FlowiseAI/Flowise",
    "https://github.com/langgenius/dify",

    # === Memory / Persistence ===
    "https://github.com/mem0ai/mem0",
    "https://github.com/letta-ai/letta",

    # === Useful Python utilities ===
    "https://pypi.org/project/requests/",
    "https://pypi.org/project/httpx/",
    "https://pypi.org/project/beautifulsoup4/",
    "https://pypi.org/project/pandas/",
    "https://pypi.org/project/numpy/",
    "https://pypi.org/project/fastapi/",
    "https://pypi.org/project/pydantic/",
    "https://pypi.org/project/anthropic/",
    "https://pypi.org/project/openai/",
    "https://pypi.org/project/transformers/",

    # === HuggingFace top models ===
    "https://huggingface.co/openai/whisper-large-v3",
    "https://huggingface.co/microsoft/Phi-3-mini-4k-instruct",
    "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
    "https://huggingface.co/stabilityai/stable-diffusion-3-medium",
    "https://huggingface.co/Qwen/Qwen2.5-Coder-32B-Instruct",

    # === npm essentials for JS agents ===
    "https://www.npmjs.com/package/@anthropic-ai/sdk",
    "https://www.npmjs.com/package/openai",
    "https://www.npmjs.com/package/langchain",
    "https://www.npmjs.com/package/playwright",
]


def bootstrap(urls: list, dry_run: bool = False, delay_seconds: float = 1.0):
    """Translate and save a list of URLs into MAI-1 entries."""
    if not dry_run:
        init_db()

    total = len(urls)
    success = 0
    failures = []

    print(f"\n=== AIAAM CATALOG BOOTSTRAP ===")
    print(f"URLs to process: {total}")
    print(f"Dry run: {dry_run}\n")

    if dry_run:
        for i, url in enumerate(urls, 1):
            print(f"  [{i}/{total}] would translate: {url}")
        return

    with Session(engine) as session:
        for i, url in enumerate(urls, 1):
            print(f"[{i}/{total}] {url}")
            try:
                tool = translate_and_save(url, session)
                if tool:
                    success += 1
                    print(f"   OK   aid={tool.aid} translator={tool.translator_used}")
                else:
                    failures.append(url)
                    print(f"   FAIL  translation returned None")
            except Exception as e:
                failures.append(url)
                print(f"   ERROR  {e}")
            time.sleep(delay_seconds)  # be polite to APIs

    print(f"\n=== DONE ===")
    print(f"Success: {success}/{total}")
    print(f"Failed:  {len(failures)}/{total}")
    if failures:
        print("\nFailed URLs:")
        for url in failures:
            print(f"  - {url}")


def load_urls_from_file(filepath: str) -> list:
    """Load URLs from a text file (one per line, # for comments)."""
    urls = []
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AIAAM catalog bootstrap")
    parser.add_argument("--tier", type=int, choices=[1], default=None,
                        help="Run a predefined tier (1 = curated essentials)")
    parser.add_argument("--file", type=str, default=None,
                        help="Path to a file with URLs (one per line)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List URLs without translating")
    parser.add_argument("--delay", type=float, default=1.0,
                        help="Seconds between requests (default 1.0)")
    args = parser.parse_args()

    if args.tier == 1:
        urls = TIER_1_URLS
    elif args.file:
        urls = load_urls_from_file(args.file)
    else:
        print("ERROR: Provide --tier 1 or --file <path>")
        sys.exit(1)

    bootstrap(urls, dry_run=args.dry_run, delay_seconds=args.delay)

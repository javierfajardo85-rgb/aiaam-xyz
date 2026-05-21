"""Reintenta las 3 APIs que fallaron por JSON truncado."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

APIS = [
    ("https://api.apis.guru/v2/specs/twilio.com/api/1.42.0/openapi.json",     "communication", "twilio"),
    ("https://api.apis.guru/v2/specs/telegram.org/5.0.0/openapi.json",        "communication", "telegram"),
    ("https://api.apis.guru/v2/specs/googleapis.com/youtube/v3/openapi.json", "google",        "youtube"),
]

IN_COST = 0.80 / 1_000_000
OUT_COST = 4.00 / 1_000_000
GBP_USD = 0.787

from compiler.openapi_compiler import compile_from_url, save_to_db

total = 0.0
for i, (url, cat, label) in enumerate(APIS, 1):
    print(f"\n[{i}/3] {label}")
    try:
        result = compile_from_url(url)
        save_to_db(result, category=cat)
        tok = result["tokens_used"]
        cost = (int(tok*.75)*IN_COST + int(tok*.25)*OUT_COST) * GBP_USD
        total += cost
        intents = len(result["manifest"].get("intents", []))
        print(f"  ✅ {result['manifest'].get('service', label)} — {intents} intents — £{cost:.4f}")
    except Exception as e:
        print(f"  ❌ {e}")
    if i < len(APIS):
        time.sleep(1.5)

print(f"\nCoste reintentos: £{total:.4f}")

"""
Batch compiler — segunda tanda: 15 restantes + 3 reintentos.
Cap de seguridad: £0.45
"""

import sys, time, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

APIS = [
    # 3 reintentos (fallaron por JSON truncado)
    ("https://api.apis.guru/v2/specs/twilio.com/api/1.42.0/openapi.json",         "communication", "twilio"),
    ("https://api.apis.guru/v2/specs/telegram.org/5.0.0/openapi.json",            "communication", "telegram"),
    ("https://api.apis.guru/v2/specs/googleapis.com/youtube/v3/openapi.json",     "google",        "youtube"),

    # 12 restantes de la primera tanda
    ("https://api.apis.guru/v2/specs/asana.com/1.0/openapi.json",                 "productivity",  "asana"),
    ("https://api.apis.guru/v2/specs/trello.com/1.0/openapi.json",                "productivity",  "trello"),
    ("https://api.apis.guru/v2/specs/clickup.com/1.0.0/openapi.json",             "productivity",  "clickup"),
    ("https://api.apis.guru/v2/specs/box.com/2.0.0/openapi.json",                 "productivity",  "box"),
    ("https://api.apis.guru/v2/specs/okta.local/1.0.0/openapi.json",              "security",      "okta"),
    ("https://api.apis.guru/v2/specs/api.ebay.com/sell-account/v1.9.0/openapi.json","ecommerce",   "ebay"),
    ("https://api.apis.guru/v2/specs/spotify.com/1.0.0/openapi.json",             "media",         "spotify"),
    ("https://api.apis.guru/v2/specs/twitter.com/current/2.61/openapi.json",      "social",        "twitter"),
    ("https://api.apis.guru/v2/specs/giphy.com/1.0/openapi.json",                 "media",         "giphy"),
    ("https://api.apis.guru/v2/specs/medium.com/1.0/openapi.json",                "media",         "medium"),
    ("https://api.apis.guru/v2/specs/abstractapi.com/geolocation/1.0.0/openapi.json","data",       "abstractapi_geo"),
    ("https://api.apis.guru/v2/specs/nasa.gov/apod/1.0.0/openapi.json",           "data",          "nasa_apod"),
]

IN_COST      = 0.80 / 1_000_000
OUT_COST     = 4.00 / 1_000_000
GBP_USD      = 0.787
COST_CAP_GBP = 0.40

def main(dry_run=False):
    if dry_run:
        print(f"[batch-b] DRY-RUN — {len(APIS)} APIs\n")
        for i, (url, cat, label) in enumerate(APIS, 1):
            print(f"  {i:2d}. [{cat:15s}] {label}")
        return

    from compiler.openapi_compiler import compile_from_url, save_to_db

    total_cost = 0.0
    compiled = failed = 0

    print(f"[batch-b] Segunda tanda — {len(APIS)} APIs (cap £{COST_CAP_GBP})\n")

    for i, (url, category, label) in enumerate(APIS, 1):
        print(f"\n[{i:2d}/{len(APIS)}] {label} ({category})")
        try:
            result   = compile_from_url(url)
            record   = save_to_db(result, category=category)
            tok      = result["tokens_used"]
            cost     = (int(tok*.75)*IN_COST + int(tok*.25)*OUT_COST) * GBP_USD
            total_cost += cost
            service  = result["manifest"].get("service", label)
            intents  = len(result["manifest"].get("intents", []))
            trunc    = " [TRUNCATED]" if result["was_truncated"] else ""
            print(f"         ✅ {service} — {intents} intents — {tok} tok — £{cost:.4f}{trunc}")
            print(f"         💰 Acumulado: £{total_cost:.4f}")
            compiled += 1
        except Exception as exc:
            print(f"         ❌ ERROR: {exc}")
            failed += 1

        if total_cost >= COST_CAP_GBP:
            print(f"\n⛔ CAP ALCANZADO (£{total_cost:.4f})")
            break

        if i < len(APIS):
            time.sleep(1.5)

    print(f"\n{'═'*50}")
    print(f"  BATCH-B RESUMEN")
    print(f"{'═'*50}")
    print(f"  Compiladas : {compiled}")
    print(f"  Fallidas   : {failed}")
    print(f"  Coste      : £{total_cost:.4f}")
    print(f"{'═'*50}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)

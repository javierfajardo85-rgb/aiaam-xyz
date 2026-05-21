"""
Batch compiler — primera tanda de 50 APIs populares.
Compila cada spec via Haiku y persiste en compiled_apis.
Límite de seguridad: para si el coste acumulado supera £0.65.

Uso:
    python3 scripts/batch_compile_50.py           # compila todas
    python3 scripts/batch_compile_50.py --dry-run # muestra la lista sin compilar
"""

import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── 50 APIs objetivo ─────────────────────────────────────────────────
APIS = [
    # (spec_url, category, service_label)
    # Payments & Finance
    ("https://api.apis.guru/v2/specs/stripe.com/2022-11-15/openapi.json",         "payments",      "stripe"),
    ("https://api.apis.guru/v2/specs/squareup.com/2.0/openapi.json",              "payments",      "square"),
    ("https://api.apis.guru/v2/specs/plaid.com/2020-09-14_1.334.0/openapi.json",  "payments",      "plaid"),
    ("https://api.apis.guru/v2/specs/brex.io/2021.12/openapi.json",               "payments",      "brex"),
    ("https://api.apis.guru/v2/specs/xero.com/xero-identity/2.9.4/openapi.json",  "finance",       "xero"),

    # Communication & Messaging
    ("https://api.apis.guru/v2/specs/twilio.com/api/1.42.0/openapi.json",         "communication", "twilio"),
    ("https://api.apis.guru/v2/specs/sendgrid.com/1.0.0/openapi.json",            "communication", "sendgrid"),
    ("https://api.apis.guru/v2/specs/vonage.com/account/1.11.8/openapi.json",     "communication", "vonage"),
    ("https://api.apis.guru/v2/specs/telegram.org/5.0.0/openapi.json",            "communication", "telegram"),
    ("https://api.apis.guru/v2/specs/whatsapp.local/1.0/openapi.json",            "communication", "whatsapp"),
    ("https://api.apis.guru/v2/specs/slack.com/1.7.0/openapi.json",               "communication", "slack"),
    ("https://api.apis.guru/v2/specs/zoom.us/2.0.0/openapi.json",                 "communication", "zoom"),

    # Developer Tools & Cloud
    ("https://api.apis.guru/v2/specs/github.com/v0.1/openapi.json",               "devtools",      "github"),
    ("https://api.apis.guru/v2/specs/vercel.com/0.0.1/openapi.json",              "devtools",      "vercel"),
    ("https://api.apis.guru/v2/specs/digitalocean.com/2.0/openapi.json",          "devtools",      "digitalocean"),
    ("https://api.apis.guru/v2/specs/netlify.com/2.15.0/swagger.json",            "devtools",      "netlify"),
    ("https://api.apis.guru/v2/specs/circleci.com/v1/openapi.json",               "devtools",      "circleci"),
    ("https://api.apis.guru/v2/specs/snyk.io/1.0.0/openapi.json",                 "devtools",      "snyk"),
    ("https://api.apis.guru/v2/specs/launchdarkly.com/5.3.0/swagger.json",        "devtools",      "launchdarkly"),

    # Google APIs
    ("https://api.apis.guru/v2/specs/googleapis.com/gmail/v1/openapi.json",       "google",        "gmail"),
    ("https://api.apis.guru/v2/specs/googleapis.com/calendar/v3/openapi.json",    "google",        "google_calendar"),
    ("https://api.apis.guru/v2/specs/googleapis.com/drive/v3/openapi.json",       "google",        "google_drive"),
    ("https://api.apis.guru/v2/specs/googleapis.com/sheets/v4/openapi.json",      "google",        "google_sheets"),
    ("https://api.apis.guru/v2/specs/googleapis.com/youtube/v3/openapi.json",     "google",        "youtube"),
    ("https://api.apis.guru/v2/specs/googleapis.com/firebase/v1beta1/openapi.json","google",       "firebase"),

    # AI & Data
    ("https://api.apis.guru/v2/specs/openai.com/1.2.0/openapi.json",              "ai",            "openai"),

    # Productivity & Collaboration
    ("https://api.apis.guru/v2/specs/notion.com/1.0.0/openapi.json",              "productivity",  "notion"),
    ("https://api.apis.guru/v2/specs/atlassian.com/jira/1001.0.0-SNAPSHOT/openapi.json", "productivity", "jira"),
    ("https://api.apis.guru/v2/specs/asana.com/1.0/openapi.json",                 "productivity",  "asana"),
    ("https://api.apis.guru/v2/specs/trello.com/1.0/openapi.json",                "productivity",  "trello"),
    ("https://api.apis.guru/v2/specs/clickup.com/1.0.0/openapi.json",             "productivity",  "clickup"),
    ("https://api.apis.guru/v2/specs/box.com/2.0.0/openapi.json",                 "productivity",  "box"),

    # Security & Identity
    ("https://api.apis.guru/v2/specs/okta.local/1.0.0/openapi.json",              "security",      "okta"),

    # E-commerce & Marketplace
    ("https://api.apis.guru/v2/specs/api.ebay.com/sell-account/v1.9.0/openapi.json","ecommerce",   "ebay"),

    # Media & Social
    ("https://api.apis.guru/v2/specs/spotify.com/1.0.0/openapi.json",             "media",         "spotify"),
    ("https://api.apis.guru/v2/specs/twitter.com/current/2.61/openapi.json",      "social",        "twitter"),
    ("https://api.apis.guru/v2/specs/giphy.com/1.0/openapi.json",                 "media",         "giphy"),
    ("https://api.apis.guru/v2/specs/medium.com/1.0/openapi.json",                "media",         "medium"),

    # Data & Geolocation
    ("https://api.apis.guru/v2/specs/abstractapi.com/geolocation/1.0.0/openapi.json","data",       "abstractapi_geo"),
    ("https://api.apis.guru/v2/specs/nasa.gov/apod/1.0.0/openapi.json",           "data",          "nasa_apod"),

    # Already compiled — skip gracefully via upsert
    ("https://api.apis.guru/v2/specs/adyen.com/AccountService/6/openapi.json",    "payments",      "adyen_account"),
    ("https://api.apis.guru/v2/specs/1password.com/events/1.0.0/openapi.json",    "security",      "1password_events"),
    ("https://api.apis.guru/v2/specs/1password.local/connect/1.5.7/openapi.json", "security",      "1password_connect"),
]

# Cost tracking
IN_COST  = 0.80 / 1_000_000
OUT_COST = 4.00 / 1_000_000
GBP_USD  = 0.787
COST_CAP_GBP = 0.65   # hard stop


def main(dry_run: bool = False):
    if dry_run:
        print(f"[batch] DRY-RUN — {len(APIS)} APIs en cola\n")
        for i, (url, cat, label) in enumerate(APIS, 1):
            print(f"  {i:2d}. [{cat:15s}] {label} → {url[:60]}...")
        return

    from compiler.openapi_compiler import compile_from_url, save_to_db

    total_cost_gbp = 0.0
    compiled = 0
    skipped  = 0
    failed   = 0

    print(f"[batch] Iniciando compilación de {len(APIS)} APIs")
    print(f"[batch] Límite de seguridad: £{COST_CAP_GBP}\n")

    for i, (url, category, label) in enumerate(APIS, 1):
        print(f"\n[{i:2d}/{len(APIS)}] {label} ({category})")
        print(f"         {url[:70]}...")

        try:
            result = compile_from_url(url)
            record = save_to_db(result, category=category)

            tok    = result["tokens_used"]
            in_tok = int(tok * 0.75)
            out_tok= int(tok * 0.25)
            cost   = (in_tok * IN_COST + out_tok * OUT_COST) * GBP_USD
            total_cost_gbp += cost

            service = result["manifest"].get("service", label)
            intents = len(result["manifest"].get("intents", []))
            trunc   = " [TRUNCATED]" if result["was_truncated"] else ""
            print(f"         ✅ {service} — {intents} intents — {tok} tokens — £{cost:.4f}{trunc}")
            print(f"         💰 Acumulado: £{total_cost_gbp:.4f}")
            compiled += 1

        except Exception as exc:
            print(f"         ❌ ERROR: {exc}")
            failed += 1

        # Hard cost cap
        if total_cost_gbp >= COST_CAP_GBP:
            print(f"\n⛔ LÍMITE DE COSTE ALCANZADO (£{total_cost_gbp:.4f} ≥ £{COST_CAP_GBP})")
            break

        # Courtesy delay — avoid rate limits
        if i < len(APIS):
            time.sleep(1.5)

    print(f"\n{'═'*55}")
    print(f"  BATCH COMPILE — RESUMEN FINAL")
    print(f"{'═'*55}")
    print(f"  Compiladas   : {compiled}")
    print(f"  Fallidas     : {failed}")
    print(f"  Coste total  : £{total_cost_gbp:.4f}")
    print(f"  Coste/API    : £{total_cost_gbp/max(compiled,1):.4f}")
    print(f"{'═'*55}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)

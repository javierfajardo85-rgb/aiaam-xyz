"""
AIAAM Daily Reporter — Agente B8

Genera y envía por email un informe de las últimas 24h:
  - Resumen de tráfico (elite / humano / bots / ataques bloqueados)
  - Actividad de agentes IA identificados
  - Herramientas más consultadas
  - Eventos de seguridad
  - Estado de los agentes B1-B7
  - Tendencia semanal (7 días)

Envío: SMTP vía Gmail App Password
Hora:  06:30 BST (05:30 UTC) — GitHub Actions cron "30 5 * * *"

Variables de entorno requeridas:
  AIAAM_API_URL        URL base de la API  (default: https://aiaam.xyz)
  ADMIN_INTEL_KEY      Clave admin (mismo valor que ADMIN_SECRET)
  SMTP_USER            Gmail: tu-cuenta@gmail.com
  SMTP_APP_PASSWORD    Gmail App Password (16 chars, sin espacios)
  REPORT_EMAIL_TO      Destinatario (default: javierfajardo85@gmail.com)

Uso:
  python3 daily_reporter.py           # envía el email
  python3 daily_reporter.py --dry-run # imprime el HTML en consola, no envía
"""

import os
import sys
import smtplib
import argparse
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
load_dotenv(override=True)

# ── Config ────────────────────────────────────────────────────────────────────
API_URL       = os.getenv("AIAAM_API_URL",   "https://aiaam.xyz")
ADMIN_KEY     = os.getenv("ADMIN_INTEL_KEY", os.getenv("ADMIN_SECRET", ""))
SMTP_USER     = os.getenv("SMTP_USER",       "")
SMTP_PASS     = os.getenv("SMTP_APP_PASSWORD", "")
SMTP_HOST     = os.getenv("SMTP_HOST",       "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT",   "587"))
REPORT_TO     = os.getenv("REPORT_EMAIL_TO", "javierfajardo85@gmail.com")
REPORT_FROM   = os.getenv("REPORT_EMAIL_FROM", SMTP_USER or "noreply@aiaam.xyz")


# ── Known attack signatures (for security section) ───────────────────────────
ATTACK_SIGNATURES = [
    "wp-admin", "xmlrpc", "wp-login", "phpmyadmin", "zgrab",
    "masscan", "nikto", "sqlmap", "nmap", "boaform", ".env",
]


def _fetch(path: str, params: Optional[dict] = None) -> dict:
    """Call the aiaam.xyz API with admin key."""
    resp = httpx.get(
        f"{API_URL}{path}",
        headers={"X-Admin-Key": ADMIN_KEY},
        params=params or {},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _classify_ua(ua: str) -> tuple[str, str]:
    """Return (label, category) for a user-agent string."""
    u = ua.lower()
    if "claudebot" in u or "anthropic" in u:
        return "ClaudeBot (Anthropic)", "elite"
    if "gptbot" in u or "chatgpt" in u:
        return "GPTBot (OpenAI)", "elite"
    if "github-copilot" in u or "copilot" in u:
        return "Copilot (GitHub)", "elite"
    if "cursor" in u:
        return "Cursor AI", "elite"
    if "gemini" in u or "google-extended" in u:
        return "Gemini (Google)", "elite"
    if "fetcher" in u:
        return "Fetcher/Agent", "elite"
    if "python/3" in u and "aiohttp" in u:
        return f"Python aiohttp ({ua[:40]})", "elite"
    if "mj12bot" in u:
        return "MJ12bot (Majestic SEO)", "crawler"
    if "baiduspider" in u:
        return "Baiduspider (Baidu)", "crawler"
    if "bingbot" in u:
        return "Bingbot (Microsoft)", "crawler"
    if "googlebot" in u:
        return "Googlebot", "crawler"
    if "builtwith" in u:
        return "BuiltWith (tech profiler)", "crawler"
    if "go-http-client" in u:
        return "Go HTTP client", "bot"
    if "python-requests" in u:
        return "python-requests (developer)", "dev"
    if "curl" in u:
        return "curl (developer)", "dev"
    if any(s in u for s in ATTACK_SIGNATURES):
        return f"Attack probe: {ua[:60]}", "attack"
    if "mozilla" in u and ("chrome" in u or "safari" in u or "firefox" in u):
        return "Human browser", "human"
    return ua[:60], "unknown"


def _detect_attacks(top_uas: list) -> list:
    """Find attack patterns in UA list."""
    attacks = []
    for item in top_uas:
        ua = item["ua"].lower()
        if any(s in ua for s in ATTACK_SIGNATURES):
            attacks.append({"ua": item["ua"][:80], "count": item["count"]})
    return attacks


def build_report(daily_data: dict, intel_data: dict) -> dict:
    """Process raw API data into report-ready structure."""
    days = daily_data.get("days", [])
    today = days[-1] if days else {}
    last_7 = days[-7:] if len(days) >= 7 else days

    # Today's stats
    total    = today.get("total", 0)
    elite    = today.get("elite", 0)
    human    = today.get("human", 0)
    unknown  = today.get("unknown", 0)
    errors   = today.get("errors", 0)
    top_uas  = today.get("top_uas", [])
    top_tools= today.get("top_tools", [])
    date_str = today.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    # Classify UAs
    elite_agents, crawlers, dev_tools, attacks = [], [], [], []
    for item in top_uas:
        label, cat = _classify_ua(item["ua"])
        entry = {"label": label, "count": item["count"], "ua": item["ua"]}
        if cat == "elite":
            elite_agents.append(entry)
        elif cat == "crawler":
            crawlers.append(entry)
        elif cat in ("dev", "bot"):
            dev_tools.append(entry)
        elif cat == "attack":
            attacks.append(entry)

    attack_count = sum(a["count"] for a in attacks)
    clean_total  = total - attack_count

    # Weekly trend
    weekly = []
    for d in last_7:
        weekly.append({
            "date":    d["date"],
            "total":   d["total"],
            "elite":   d["elite"],
            "human":   d["human"],
            "errors":  d["errors"],
        })

    return {
        "date":           date_str,
        "total":          total,
        "clean_total":    clean_total,
        "elite":          elite,
        "human":          human,
        "unknown":        unknown,
        "errors":         errors,
        "error_pct":      round(errors / total * 100, 1) if total else 0,
        "elite_agents":   elite_agents,
        "crawlers":       crawlers,
        "dev_tools":      dev_tools,
        "attacks":        attacks,
        "attack_count":   attack_count,
        "top_tools":      top_tools,
        "weekly":         weekly,
        "verified_tools": intel_data.get("monetization", {}).get("verified_total", 0),
        "elite_ratio_30d":intel_data.get("elite_agent_ratio", 0),
        "total_30d":      intel_data.get("total_requests", 0),
    }


def render_html(r: dict) -> str:
    """Generate the HTML email body."""

    def _badge(text: str, color: str) -> str:
        return (f'<span style="background:{color};color:#fff;padding:2px 8px;'
                f'border-radius:4px;font-size:11px;font-weight:700;">{text}</span>')

    def _row(label: str, value, color: str = "#111", note: str = "") -> str:
        note_html = f' <span style="color:#888;font-size:11px;">{note}</span>' if note else ""
        return (f'<tr><td style="padding:6px 0;color:#555;font-size:13px;">{label}</td>'
                f'<td style="padding:6px 0;font-weight:700;color:{color};font-size:15px;'
                f'text-align:right;">{value}{note_html}</td></tr>')

    def _tool_row(i: int, aid: str, count: int) -> str:
        bg = "#f0f7ff" if i % 2 == 0 else "#fff"
        return (f'<tr style="background:{bg}"><td style="padding:5px 10px;font-size:12px;'
                f'font-family:monospace;">{aid}</td>'
                f'<td style="padding:5px 10px;text-align:right;font-weight:700;'
                f'font-size:13px;">{count}</td></tr>')

    # Elite agents section
    elite_html = ""
    if r["elite_agents"]:
        for ag in r["elite_agents"]:
            elite_html += (
                f'<tr><td style="padding:6px 10px;font-size:12px;">'
                f'{_badge("ELITE", "#007AFF")} {ag["label"]}</td>'
                f'<td style="padding:6px 10px;text-align:right;font-weight:700;">'
                f'{ag["count"]} req</td></tr>'
            )
    else:
        elite_html = ('<tr><td colspan="2" style="padding:12px 10px;color:#888;'
                      'font-size:13px;text-align:center;">No elite AI agents today</td></tr>')

    # Top tools section
    tools_html = ""
    for i, t in enumerate(r["top_tools"][:10]):
        tools_html += _tool_row(i, t["aid"], t["count"])
    if not tools_html:
        tools_html = ('<tr><td colspan="2" style="padding:12px 10px;color:#888;'
                      'font-size:13px;text-align:center;">No tool API calls today</td></tr>')

    # Security section
    security_html = ""
    if r["attacks"]:
        for a in r["attacks"]:
            security_html += (
                f'<tr><td style="padding:5px 10px;font-size:11px;font-family:monospace;'
                f'color:#c0392b;">{a["ua"]}</td>'
                f'<td style="padding:5px 10px;text-align:right;font-weight:700;'
                f'color:#c0392b;">{a["count"]}</td></tr>'
            )
    else:
        security_html = ('<tr><td colspan="2" style="padding:10px;color:#27ae60;'
                         'font-size:13px;">✓ No attack probes detected (or all blocked by middleware)</td></tr>')

    # Weekly trend
    weekly_html = ""
    for d in r["weekly"]:
        trend_elite = f'<span style="color:#007AFF;font-weight:700;">{d["elite"]}</span>'
        weekly_html += (
            f'<tr style="border-bottom:1px solid #eee;">'
            f'<td style="padding:5px 8px;font-size:12px;color:#555;">{d["date"]}</td>'
            f'<td style="padding:5px 8px;text-align:right;font-size:13px;font-weight:600;">{d["total"]}</td>'
            f'<td style="padding:5px 8px;text-align:right;font-size:13px;">{trend_elite}</td>'
            f'<td style="padding:5px 8px;text-align:right;font-size:12px;color:#888;">{d["errors"]}</td>'
            f'</tr>'
        )

    # Health indicators
    error_color = "#e74c3c" if r["error_pct"] > 50 else ("#f39c12" if r["error_pct"] > 20 else "#27ae60")
    elite_color = "#007AFF" if r["elite"] > 0 else "#888"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:24px 16px;">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);border-radius:12px;
              padding:28px 28px 24px;margin-bottom:20px;">
    <div style="font-size:11px;color:#8892b0;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">
      AIAAM Intelligence Report
    </div>
    <div style="font-size:26px;font-weight:700;color:#fff;margin-bottom:2px;">
      {r["date"]}
    </div>
    <div style="font-size:13px;color:#8892b0;">
      Last 24 hours · aiaam.xyz · {r["verified_tools"]} verified tools
    </div>
  </div>

  <!-- KPI Row -->
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px;">
    <div style="background:#fff;border-radius:10px;padding:16px;text-align:center;
                box-shadow:0 1px 3px rgba(0,0,0,.08);">
      <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;">Requests</div>
      <div style="font-size:28px;font-weight:700;color:#111;">{r["total"]}</div>
      <div style="font-size:11px;color:#888;">{r["clean_total"]} clean</div>
    </div>
    <div style="background:#fff;border-radius:10px;padding:16px;text-align:center;
                box-shadow:0 1px 3px rgba(0,0,0,.08);">
      <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;">Elite AI</div>
      <div style="font-size:28px;font-weight:700;color:{elite_color};">{r["elite"]}</div>
      <div style="font-size:11px;color:#888;">{round(r["elite_ratio_30d"]*100,1)}% ratio 30d</div>
    </div>
    <div style="background:#fff;border-radius:10px;padding:16px;text-align:center;
                box-shadow:0 1px 3px rgba(0,0,0,.08);">
      <div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px;">Errors</div>
      <div style="font-size:28px;font-weight:700;color:{error_color};">{r["error_pct"]}%</div>
      <div style="font-size:11px;color:#888;">{r["errors"]} of {r["total"]}</div>
    </div>
  </div>

  <!-- Traffic breakdown -->
  <div style="background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:16px;
              box-shadow:0 1px 3px rgba(0,0,0,.08);">
    <div style="font-size:13px;font-weight:700;color:#111;margin-bottom:12px;
                text-transform:uppercase;letter-spacing:.5px;">Traffic Breakdown</div>
    <table style="width:100%;border-collapse:collapse;">
      {_row("Elite AI agents", r["elite"], "#007AFF")}
      {_row("Human browsers", r["human"], "#34C759")}
      {_row("Unknown / bots", r["unknown"], "#888")}
      {_row("Attack probes blocked", r["attack_count"], "#e74c3c",
            "(not logged in DB)" if r["attack_count"] > 0 else "")}
      {_row("4xx / 5xx errors", r["errors"], error_color,
            f'({r["error_pct"]}% of requests)')}
    </table>
  </div>

  <!-- Elite Agents -->
  <div style="background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:16px;
              box-shadow:0 1px 3px rgba(0,0,0,.08);">
    <div style="font-size:13px;font-weight:700;color:#111;margin-bottom:12px;
                text-transform:uppercase;letter-spacing:.5px;">🤖 Elite AI Agents</div>
    <table style="width:100%;border-collapse:collapse;">
      {elite_html}
    </table>
  </div>

  <!-- Top Tools -->
  <div style="background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:16px;
              box-shadow:0 1px 3px rgba(0,0,0,.08);">
    <div style="font-size:13px;font-weight:700;color:#111;margin-bottom:12px;
                text-transform:uppercase;letter-spacing:.5px;">🔧 Top Tools Accessed</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#f5f5f7;">
          <th style="padding:6px 10px;text-align:left;color:#555;font-weight:600;">Tool AID</th>
          <th style="padding:6px 10px;text-align:right;color:#555;font-weight:600;">Requests</th>
        </tr>
      </thead>
      <tbody>{tools_html}</tbody>
    </table>
  </div>

  <!-- Security -->
  <div style="background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:16px;
              box-shadow:0 1px 3px rgba(0,0,0,.08);">
    <div style="font-size:13px;font-weight:700;color:#111;margin-bottom:12px;
                text-transform:uppercase;letter-spacing:.5px;">🔴 Security Events</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#fff5f5;">
          <th style="padding:6px 10px;text-align:left;color:#c0392b;font-weight:600;">Attack UA / Pattern</th>
          <th style="padding:6px 10px;text-align:right;color:#c0392b;font-weight:600;">Hits</th>
        </tr>
      </thead>
      <tbody>{security_html}</tbody>
    </table>
    <p style="font-size:11px;color:#888;margin:10px 0 0;">
      Middleware blocks wp-admin, xmlrpc, nikto, sqlmap probes before they reach the router.
      Blocked requests return HTTP 444 and are NOT written to request_logs.
    </p>
  </div>

  <!-- Weekly Trend -->
  <div style="background:#fff;border-radius:10px;padding:20px 24px;margin-bottom:16px;
              box-shadow:0 1px 3px rgba(0,0,0,.08);">
    <div style="font-size:13px;font-weight:700;color:#111;margin-bottom:12px;
                text-transform:uppercase;letter-spacing:.5px;">📈 7-Day Trend</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px;">
      <thead>
        <tr style="background:#f5f5f7;border-bottom:2px solid #eee;">
          <th style="padding:6px 8px;text-align:left;color:#555;font-weight:600;">Date</th>
          <th style="padding:6px 8px;text-align:right;color:#555;font-weight:600;">Total</th>
          <th style="padding:6px 8px;text-align:right;color:#007AFF;font-weight:600;">Elite</th>
          <th style="padding:6px 8px;text-align:right;color:#e74c3c;font-weight:600;">Errors</th>
        </tr>
      </thead>
      <tbody>{weekly_html}</tbody>
    </table>
  </div>

  <!-- Footer -->
  <div style="text-align:center;padding:20px;color:#888;font-size:11px;">
    <strong style="color:#111;">AIAAM · aiaam.xyz</strong> ·
    100 verified tools ·
    MAI-1 v1.0 ·
    <a href="https://aiaam.xyz/admin/dashboard?token=mggt%2BRp%3Bj%26ZFwE6SFg.%40ZzDCD%7D%21yas%3Eq-0H"
       style="color:#007AFF;text-decoration:none;">Open Dashboard</a>
  </div>

</div>
</body>
</html>"""
    return html


def send_email(subject: str, html: str) -> None:
    """Send HTML email via SMTP (Gmail App Password)."""
    if not SMTP_USER or not SMTP_PASS:
        raise RuntimeError(
            "SMTP_USER and SMTP_APP_PASSWORD env vars required.\n"
            "Create a Gmail App Password at: https://myaccount.google.com/apppasswords"
        )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = REPORT_FROM
    msg["To"]      = REPORT_TO
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
        srv.ehlo()
        srv.starttls()
        srv.login(SMTP_USER, SMTP_PASS)
        srv.sendmail(REPORT_FROM, REPORT_TO, msg.as_string())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Print HTML to stdout instead of sending email")
    args = parser.parse_args()

    print(f"[reporter] {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} — fetching data...")

    try:
        daily_data = _fetch("/api/v1/intel/daily", {"days": 8})
        intel_data = _fetch("/api/v1/intel")
    except Exception as exc:
        print(f"[reporter] ERROR fetching data: {exc}")
        sys.exit(1)

    report = build_report(daily_data, intel_data)
    html   = render_html(report)

    elite_str = f"{report['elite']} elite" if report['elite'] > 0 else "no elite"
    subject = (
        f"AIAAM Daily · {report['date']} · "
        f"{report['total']} req · {elite_str} · "
        f"{report['error_pct']}% errors"
    )

    if args.dry_run:
        print(f"\n[dry-run] Subject: {subject}")
        print(f"[dry-run] To: {REPORT_TO}")
        print(f"[dry-run] HTML length: {len(html)} chars")
        print("\n--- REPORT STATS ---")
        print(f"  Date:        {report['date']}")
        print(f"  Total:       {report['total']} ({report['clean_total']} clean)")
        print(f"  Elite AI:    {report['elite']}")
        print(f"  Human:       {report['human']}")
        print(f"  Attacks:     {report['attack_count']}")
        print(f"  Errors:      {report['errors']} ({report['error_pct']}%)")
        print(f"  Top tools:   {[t['aid'] for t in report['top_tools'][:3]]}")
        print(f"  Elite agents:{[a['label'] for a in report['elite_agents']]}")
        return

    print(f"[reporter] sending to {REPORT_TO}...")
    try:
        send_email(subject, html)
        print(f"[reporter] ✓ email sent — {subject}")
    except Exception as exc:
        print(f"[reporter] ERROR sending email: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()

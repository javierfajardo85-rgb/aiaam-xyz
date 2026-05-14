#!/usr/bin/env bash
# AIAAM Daily Agent Runner — ejecutar localmente o via cron
# Requiere: venv activado, Docker corriendo, .env con ANTHROPIC_API_KEY y GITHUB_TOKEN
#
# Uso:
#   ./run_daily.sh              # corre todos los agentes locales
#   ./run_daily.sh sentinel     # solo B1
#   ./run_daily.sh sanitizer    # solo B2
#   ./run_daily.sh injector     # solo B3
#   ./run_daily.sh ghost        # solo B4 (dry-run — snippets para envío manual)
#   ./run_daily.sh push         # solo B7 (sync a Railway)
#
# Para cron diario a las 06:00:
#   crontab -e
#   0 6 * * * cd /Users/tu_usuario/Desktop/AIAAM/aiaam && ./run_daily.sh >> logs/daily.log 2>&1

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$SCRIPT_DIR/venv/bin/python3"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

AGENT="${1:-all}"
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "AIAAM Daily Run — $TS — agent=$AGENT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

cd "$SCRIPT_DIR"

run_sentinel() {
    echo "[B1] Sentinel Sniffer..."
    $PYTHON sentinel_sniffer.py | tee "$LOG_DIR/sentinel_$(date +%Y%m%d).log"
}

run_sanitizer() {
    echo "[B2] Sandbox Sanitizer (pending tools only)..."
    $PYTHON sandbox_sanitizer.py | tee "$LOG_DIR/sanitizer_$(date +%Y%m%d).log"
}

run_injector() {
    echo "[B3] Context Injector..."
    $PYTHON context_injector.py | tee "$LOG_DIR/injector_$(date +%Y%m%d).log"
}

run_ghost() {
    echo "[B4] Library Ghost (dry-run — review snippets before posting)..."
    $PYTHON library_ghost.py --dry-run | tee "$LOG_DIR/ghost_$(date +%Y%m%d).log"
}

run_tax() {
    echo "[B5] Tax Analyst..."
    $PYTHON tax_analyst.py | tee "$LOG_DIR/tax_$(date +%Y%m%d).log"
}

run_push() {
    echo "[B7] Push to Production (verified only)..."
    $PYTHON push_to_production.py --only-verified | tee "$LOG_DIR/push_$(date +%Y%m%d).log"
}

case "$AGENT" in
    sentinel)  run_sentinel ;;
    sanitizer) run_sanitizer ;;
    injector)  run_injector ;;
    ghost)     run_ghost ;;
    tax)       run_tax ;;
    push)      run_push ;;
    all)
        run_sentinel
        run_sanitizer
        run_injector
        run_ghost
        run_tax
        run_push
        ;;
    *)
        echo "Agente desconocido: $AGENT"
        echo "Usa: sentinel | sanitizer | injector | ghost | tax | push | all"
        exit 1
        ;;
esac

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "AIAAM Daily Run completado — $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

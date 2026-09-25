#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# production_soak_check.sh — Long-run reliability / "no-stuck" monitor for
# CodeGloFix Access Control.
#
# Samples the live system every N seconds and writes a timestamped report. Use it
# to prove the system stays up, bound to localhost, and does not leak memory or
# fill the disk over hours of running.
#
# Usage:
#   ./tools/production_soak_check.sh                 # 10 minutes, 30s interval
#   ./tools/production_soak_check.sh --hours 24      # 24-hour soak
#   ./tools/production_soak_check.sh --minutes 30
#   ./tools/production_soak_check.sh --seconds 0     # single sample (quick check)
#   ./tools/production_soak_check.sh --interval 15 --user auto
#
# Every sample records: service status, nginx status, live-view + admin HTTP codes,
# app memory (RSS), CPU%, disk usage, backend log size, port binding, health JSON.
#
# FAILS (exit 1) if: the service stops, endpoints return 5xx repeatedly, memory
# grows abnormally, the disk is nearly full, the app is NOT bound to 127.0.0.1,
# the log grows too large, or the health endpoint gets stuck.
#
# All curl calls use --max-time so the monitor itself can never hang.
# ──────────────────────────────────────────────────────────────────────────────
set -u

# ── Config / args ─────────────────────────────────────────────────────────────
INTERVAL=30
TOTAL=600                         # default 10 minutes
RUN_USER="$(id -un)"
LIVEVIEW_URL="https://localhost/"
ADMIN_URL="https://localhost/admin"
HEALTH_URL="https://localhost/api/health"
CURL_MAXTIME=5

# Fail thresholds (override via env if needed)
DISK_FAIL_PCT="${DISK_FAIL_PCT:-90}"
LOG_MAX_MB="${LOG_MAX_MB:-500}"
MEM_GROWTH_FACTOR="${MEM_GROWTH_FACTOR:-2.0}"     # fail if RSS > factor × baseline
MEM_FLOOR_MB="${MEM_FLOOR_MB:-300}"              # ignore growth below this (noise)
CONSEC_5XX_FAIL="${CONSEC_5XX_FAIL:-3}"
CONSEC_HEALTH_STUCK="${CONSEC_HEALTH_STUCK:-3}"

while [ $# -gt 0 ]; do
    case "$1" in
        --hours)    TOTAL=$(( ${2:-0} * 3600 )); shift 2 ;;
        --minutes)  TOTAL=$(( ${2:-0} * 60 ));   shift 2 ;;
        --seconds)  TOTAL="${2:-0}";             shift 2 ;;
        --interval) INTERVAL="${2:-30}";         shift 2 ;;
        --user)     RUN_USER="${2:-$RUN_USER}";  shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '2,30p'; exit 0 ;;
        *) echo "Unknown option: $1"; exit 2 ;;
    esac
done

SERVICE="codeglofix-access@${RUN_USER}.service"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
LOGDIR="$PROJECT/logs"
mkdir -p "$LOGDIR"
STAMP="$(date +%Y%m%d_%H%M%S)"
REPORT="$LOGDIR/production_soak_report_${STAMP}.txt"

# Backend log location (production vs dev)
if [ -f /var/log/codeglofix/backend.log ]; then APP_LOG=/var/log/codeglofix/backend.log
else APP_LOG="$PROJECT/codeglofix_backend.log"; fi

# ── Helpers ───────────────────────────────────────────────────────────────────
log() { echo "$*" | tee -a "$REPORT" ; }

http_code() { local c; c="$(curl -sk --max-time "$CURL_MAXTIME" -o /dev/null -w '%{http_code}' "$1" 2>/dev/null)"; echo "${c:-000}"; }

app_pid() { ss -ltnp 2>/dev/null | awk '/127.0.0.1:5000/ {print}' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2; }

bound_localhost_only() {   # 0 = good (only 127.0.0.1:5000), 1 = bad (0.0.0.0 etc.)
    if ss -ltn 2>/dev/null | grep -q '0.0.0.0:5000\|\*:5000\|\[::\]:5000'; then return 1; fi
    return 0
}

log_size_mb() { [ -f "$APP_LOG" ] && echo $(( $(stat -c%s "$APP_LOG" 2>/dev/null || echo 0) / 1048576 )) || echo 0; }

disk_pct() { df -P "$PROJECT" 2>/dev/null | awk 'NR==2{gsub("%","",$5);print $5}'; }

health_json() { curl -sk --max-time "$CURL_MAXTIME" "$HEALTH_URL" 2>/dev/null; }

# ── Header ────────────────────────────────────────────────────────────────────
: > "$REPORT"
log "════════════════════════════════════════════════════════════════"
log " CodeGloFix Access Control — Production Soak Report"
log " Started : $(date '+%Y-%m-%d %H:%M:%S %z')"
log " Service : $SERVICE"
log " Duration: ${TOTAL}s   Interval: ${INTERVAL}s"
log " Backend log: $APP_LOG"
log "════════════════════════════════════════════════════════════════"
log "$(printf '%-19s %-8s %-6s %-5s %-5s %-8s %-5s %-6s %-7s %s' \
        TIME SERVICE NGINX LIVEVW ADMIN RSS_MB CPU% DISK% LOG_MB BOUND)"

# ── Sampling loop ─────────────────────────────────────────────────────────────
FAIL=0; REASONS=""
baseline_rss=""; consec_5xx=0; consec_health_stuck=0; samples=0
add_fail() { FAIL=1; REASONS="$REASONS\n  - $1"; }

END=$(( $(date +%s) + TOTAL ))
while : ; do
    samples=$((samples+1))
    now_h="$(date '+%Y-%m-%d %H:%M:%S')"

    svc="$(systemctl is-active "$SERVICE" 2>/dev/null)"; svc="${svc:-unknown}"
    ngx="$(systemctl is-active nginx 2>/dev/null)"; ngx="${ngx:-unknown}"
    kc="$(http_code "$LIVEVIEW_URL")"
    ac="$(http_code "$ADMIN_URL")"
    pid="$(app_pid)"
    if [ -n "$pid" ]; then
        rss_kb="$(ps -o rss= -p "$pid" 2>/dev/null | tr -d ' ')"; rss_mb=$(( ${rss_kb:-0} / 1024 ))
        cpu="$(ps -o %cpu= -p "$pid" 2>/dev/null | tr -d ' ')"
    else rss_mb=0; cpu="-"; fi
    dpct="$(disk_pct)"; lmb="$(log_size_mb)"
    if bound_localhost_only; then bnd="local"; else bnd="EXPOSED"; fi
    hj="$(health_json)"

    log "$(printf '%-19s %-8s %-6s %-5s %-5s %-8s %-5s %-6s %-7s %s' \
        "$now_h" "$svc" "$ngx" "$kc" "$ac" "$rss_mb" "${cpu:- -}" "${dpct:- -}" "$lmb" "$bnd")"

    # ── Evaluate fail conditions ──
    [ "$svc" = "active" ] || add_fail "service '$SERVICE' not active (was: $svc)"
    [ "$bnd" = "local" ]  || add_fail "app bound to a non-local address (security)"

    case "$kc" in 500|502|503|504) consec_5xx=$((consec_5xx+1));; *) consec_5xx=0;; esac
    case "$ac" in 500|502|503|504) consec_5xx=$((consec_5xx+1));; esac
    [ "$consec_5xx" -ge "$CONSEC_5XX_FAIL" ] && add_fail "endpoints returned 5xx $consec_5xx× in a row"

    if echo "$hj" | grep -q '"ok"'; then consec_health_stuck=0
    else consec_health_stuck=$((consec_health_stuck+1)); fi
    [ "$consec_health_stuck" -ge "$CONSEC_HEALTH_STUCK" ] && add_fail "health endpoint stuck/unreadable $consec_health_stuck×"

    if [ -n "${dpct:-}" ] && [ "$dpct" -ge "$DISK_FAIL_PCT" ] 2>/dev/null; then add_fail "disk ${dpct}% ≥ ${DISK_FAIL_PCT}%"; fi
    if [ "$lmb" -ge "$LOG_MAX_MB" ] 2>/dev/null; then add_fail "backend log ${lmb}MB ≥ ${LOG_MAX_MB}MB"; fi

    if [ "$rss_mb" -gt 0 ]; then
        [ -z "$baseline_rss" ] && baseline_rss="$rss_mb"
        if [ "$rss_mb" -ge "$MEM_FLOOR_MB" ]; then
            grow=$(awk -v a="$rss_mb" -v b="$baseline_rss" -v f="$MEM_GROWTH_FACTOR" 'BEGIN{print (b>0 && a > b*f)?1:0}')
            [ "$grow" = "1" ] && add_fail "memory grew abnormally: ${rss_mb}MB vs baseline ${baseline_rss}MB (×$MEM_GROWTH_FACTOR)"
        fi
    fi

    now=$(date +%s); [ "$now" -ge "$END" ] && break
    rem=$((END-now)); s=$INTERVAL; [ "$s" -gt "$rem" ] && s="$rem"; [ "$s" -gt 0 ] && sleep "$s"
done

# ── Summary ───────────────────────────────────────────────────────────────────
log "────────────────────────────────────────────────────────────────"
log " Samples taken : $samples"
log " Baseline RSS  : ${baseline_rss:-n/a} MB"
log " Finished      : $(date '+%Y-%m-%d %H:%M:%S %z')"
if [ "$FAIL" -eq 0 ]; then
    log " RESULT: PASS — system stayed up, local-only, and within limits."
else
    log " RESULT: FAIL"
    log "$(printf '%b' "$REASONS")"
fi
log "════════════════════════════════════════════════════════════════"
log " Report saved: $REPORT"
exit "$FAIL"

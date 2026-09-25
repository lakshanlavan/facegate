#!/bin/sh
# codeglofix-health-check.sh — watchdog health probe for the CodeGloFix access
# service. Run periodically by codeglofix-health.service (via codeglofix-health.timer).
#
# Healthy ONLY when /api/health returns {"ok":true} (camera fresh). HTTP 200 with
# "ok":false (camera stale) OR an unreachable/hung app both count as unhealthy.
#
# Health logic lives in this standalone script (called directly from ExecStart=)
# to avoid systemd ExecStart quote/escape mangling of the match pattern. It
# retries a few times so a brief startup / camera warm-up does not trigger an
# unnecessary restart. Borrowed from the DigitFace reference; adapted to the
# CodeGloFix service names and port. No access/relay logic is touched.

HEALTH_URL="http://127.0.0.1:5000/api/health"
ATTEMPTS=3
SLEEP_BETWEEN=3

# Resolve the active templated instance (codeglofix-access@USER.service); fall
# back to the non-templated name for dev installs.
resolve_service() {
    svc="$(systemctl list-units --type=service --all --plain --no-legend \
           'codeglofix-access@*.service' 2>/dev/null | awk '{print $1}' | head -n1)"
    [ -n "$svc" ] || svc="codeglofix-access.service"
    echo "$svc"
}

is_healthy() {
    # No leading double-quote / no backslash escaping → immune to systemd quoting
    # AND matches the compact {"ok":true,...} JSON FastAPI emits.
    curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null | grep -q 'ok":true'
}

i=1
while [ "$i" -le "$ATTEMPTS" ]; do
    if is_healthy; then
        exit 0
    fi
    if [ "$i" -lt "$ATTEMPTS" ]; then
        sleep "$SLEEP_BETWEEN"
    fi
    i=$((i + 1))
done

SERVICE="$(resolve_service)"
echo "codeglofix-health-check: app unhealthy after ${ATTEMPTS} attempts — restarting ${SERVICE}"
systemctl reset-failed "$SERVICE" 2>/dev/null || true
systemctl restart "$SERVICE" || true
exit 0

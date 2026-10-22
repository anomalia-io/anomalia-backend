#!/usr/bin/env bash
#
# Servidor SMTP de prueba para Anomalia (spec 018).
#
# Levanta un receptor SMTP local (aiosmtpd) en el host y apunta el
# `system_settings` del gateway a él, de modo que las alertas enriquecidas por IA
# se puedan ver "aterrizar" sin depender de un servidor de correo real.
#
# Cómo alcanza el contenedor al host:
#   `docker-compose.yml.j2` mapea `extra_hosts: anomalia.local:<host-gateway>`
#   (Docker) o `host.containers.internal` (Podman). Por eso el host SMTP se
#   configura como `anomalia.local:<puerto>`.
#
# Uso:
#   scripts/smtp-test/run-test-smtp.sh          # arranca y queda en foreground
#   scripts/smtp-test/run-test-smtp.sh --reset  # solo restaura la config SMTP
#
# Para provocar un correo: crea un tenant con destinatarios (perfil
# `alerts_manager`) y dispara una alerta, o usa "Probar SMTP" en Ajustes del
# sistema. Cada mensaje recibido se imprime en pantalla y se agrega a
# `scripts/smtp-test/received.log` (override con SMTP_TEST_LOG).
#
set -euo pipefail

PORT="${SMTP_TEST_PORT:-1025}"
HOST_ALIAS="${SMTP_TEST_HOST_ALIAS:-anomalia.local}"
FROM_ADDR="${SMTP_TEST_FROM:-alerts@anomalia.local}"
PG_CONTAINER="${SMTP_TEST_PG_CONTAINER:-anomalia_postgres}"
PG_USER="${SMTP_TEST_PG_USER:-anomal_user}"
PG_DB="${SMTP_TEST_PG_DB:-anomal_db}"
LISTEN_ADDR="${SMTP_TEST_LISTEN:-0.0.0.0:${PORT}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SMTP_TEST_LOG:-${SCRIPT_DIR}/received.log}"

log() { printf '\033[1;34m[smtp-test]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[smtp-test] error:\033[0m %s\n' "$*" >&2; exit 1; }

detect_engine() {
    if command -v docker >/dev/null 2>&1; then echo docker
    elif command -v podman >/dev/null 2>&1; then echo podman
    else die "no se encontró docker ni podman (el gateway no está instalado aquí)"
    fi
}

psql_exec() {
    local engine="$1" sql="$2"
    "$engine" exec "$PG_CONTAINER" \
        psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -c "$sql"
}

reset_smtp_config() {
    local engine="$1"
    psql_exec "$engine" \
        "UPDATE system_settings SET smtp_host='', smtp_port=587, smtp_use_tls=true, smtp_from='', smtp_username='', smtp_password='', updated_at=NOW() WHERE id=1;" \
        >/dev/null
    log "config SMTP restaurada (valores vacíos)."
}

main() {
    local engine
    engine="$(detect_engine)"
    log "engine de contenedores detectado: $engine"

    if [[ "${1:-}" == "--reset" ]]; then
        reset_smtp_config "$engine"
        exit 0
    fi

    python3 -c "import aiosmtpd" 2>/dev/null \
        || die "falta aiosmtpd. Instálalo con: pip3 install --user aiosmtpd"

    if ! "$engine" ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
        die "no encuentro el contenedor '$PG_CONTAINER'. ¿Está el stack desplegado?"
    fi

    log "configurando system_settings → ${HOST_ALIAS}:${PORT} (sin TLS, sin auth)"
    psql_exec "$engine" \
        "UPDATE system_settings SET smtp_host='${HOST_ALIAS}', smtp_port=${PORT}, smtp_use_tls=false, smtp_from='${FROM_ADDR}', smtp_username='', smtp_password='', updated_at=NOW() WHERE id=1;" \
        >/dev/null

    log "receptor SMTP escuchando en ${LISTEN_ADDR}"
    log "dispara una alerta (o 'Probar SMTP') y verás el correo debajo."
    log "cada mensaje recibido se registra además en: ${LOG_FILE}"
    log "Ctrl+C para detener. Luego: scripts/smtp-test/run-test-smtp.sh --reset"

    printf '\n===== smtp-test iniciado %s — %s =====\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "${HOST_ALIAS}:${PORT}" >>"$LOG_FILE"

    # Foreground: imprime cada mensaje recibido en stdout y lo agrega al log.
    # `-u` fuerza stdout sin buffer para que el correo aparezca al instante.
    python3 -u -m aiosmtpd -n -l "$LISTEN_ADDR" 2>&1 | tee -a "$LOG_FILE"
}

main "$@"

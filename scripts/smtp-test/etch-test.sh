#!/usr/bin/env bash
#
# Prueba de ENVÍO real de email con Ethereal (spec 018).
#
# No forma parte del producto: sirve para verificar que el gateway envía correo
# por un SMTP real de internet (negocia STARTTLS y autentica), sin configurar un
# relay propio. Ethereal (https://ethereal.email) acepta el mensaje y lo deja
# visible en su bandeja web.
#
# Uso:
#   scripts/smtp-test/etch-test.sh [destino@correo]     # crea cuenta y prueba
#   scripts/smtp-test/etch-test.sh --reset              # solo restaura la config
#
# Reutilizar una cuenta existente (evita crear una nueva en cada corrida):
#   ETHEREAL_USER=xxx@ethereal.email ETHEREAL_PASS=yyy scripts/smtp-test/etch-test.sh
#
# Variables opcionales:
#   ETCH_PG_CONTAINER (anomalia_postgres), ETCH_GW_CONTAINER (anomalia_gw),
#   ETCH_PG_USER (anomal_user), ETCH_PG_DB (anomal_db).
#
set -euo pipefail

PG_CONTAINER="${ETCH_PG_CONTAINER:-anomalia_postgres}"
GW_CONTAINER="${ETCH_GW_CONTAINER:-anomalia_gw}"
PG_USER="${ETCH_PG_USER:-anomal_user}"
PG_DB="${ETCH_PG_DB:-anomal_db}"
TO_ADDR="${1:-dest@example.com}"

log() { printf '\033[1;34m[etch-test]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[etch-test] error:\033[0m %s\n' "$*" >&2; exit 1; }

detect_engine() {
    if command -v docker >/dev/null 2>&1; then echo docker
    elif command -v podman >/dev/null 2>&1; then echo podman
    else die "no se encontró docker ni podman"
    fi
}

psql_exec() {
    "$ENGINE" exec "$PG_CONTAINER" \
        psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -c "$1" >/dev/null
}

reset_smtp_config() {
    psql_exec "UPDATE system_settings SET smtp_host='', smtp_port=587, smtp_use_tls=true, smtp_username='', smtp_password='', smtp_from='', updated_at=NOW() WHERE id=1;"
    log "config SMTP restaurada (valores vacíos)."
}

jq_get() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

create_account() {
    local json
    log "creando cuenta de prueba en Ethereal..." >&2
    json="$(curl -sS -X POST https://api.nodemailer.com/user \
        -H 'Content-type: application/json' \
        -d '{"requestor":"anomalia","version":"1.0.0"}')" \
        || die "no se pudo contactar api.nodemailer.com"
    printf '%s' "$json" | jq_get "['user']" >/dev/null 2>&1 \
        || die "respuesta inesperada de Ethereal: $json"
    printf '%s' "$json"
}

main() {
    ENGINE="$(detect_engine)"

    if [[ "${1:-}" == "--reset" ]]; then
        reset_smtp_config
        exit 0
    fi

    if ! "$ENGINE" ps --format '{{.Names}}' | grep -qx "$PG_CONTAINER"; then
        die "no encuentro el contenedor '$PG_CONTAINER'. ¿Está el stack desplegado?"
    fi
    if ! "$ENGINE" ps --format '{{.Names}}' | grep -qx "$GW_CONTAINER"; then
        die "no encuentro el contenedor '$GW_CONTAINER'. ¿Está el stack desplegado?"
    fi

    local user pass host port
    if [[ -n "${ETHEREAL_USER:-}" && -n "${ETHEREAL_PASS:-}" ]]; then
        user="$ETHEREAL_USER"; pass="$ETHEREAL_PASS"
        host="smtp.ethereal.email"; port="587"
        log "usando cuenta Ethereal provista por entorno: $user"
    else
        local account
        account="$(create_account)"
        user="$(printf '%s' "$account" | jq_get "['user']")"
        pass="$(printf '%s' "$account" | jq_get "['pass']")"
        host="$(printf '%s' "$account" | jq_get "['smtp']['host']")"
        port="$(printf '%s' "$account" | jq_get "['smtp']['port']")"
    fi

    log "configurando system_settings → ${host}:${port} (TLS/STARTTLS, usuario ${user})"
    psql_exec "UPDATE system_settings SET smtp_host='${host}', smtp_port=${port}, smtp_use_tls=true, smtp_username='${user}', smtp_password='${pass}', smtp_from='${user}', updated_at=NOW() WHERE id=1;"

    log "enviando email de prueba (destino: ${TO_ADDR})..."
    "$ENGINE" exec -i "$GW_CONTAINER" python3 -c \
        "import asyncio;from app.mailer import send_email;print(asyncio.run(send_email('${TO_ADDR}','Test Anomalia','Correo de prueba enviado desde Anomalia.')))"

    reset_smtp_config

    printf '\n'
    log "listo. Revisá el correo en: https://ethereal.email/messages"
    log "  usuario: ${user}"
    log "  clave:   ${pass}"
    log "Si no aparece en la web, se puede leer por IMAP: imap.ethereal.email:993"
}

main "$@"

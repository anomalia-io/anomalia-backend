#!/usr/bin/env bash
#
# IdP OIDC de prueba para Anomalia (soluto/oidc-server-mock).
#
# Levanta un servidor OIDC mock en http://anomalia.local:8080 y permite
# probar el flujo SSO (authorization_code + PKCE) con/ sin claims de roles/grupos.
#
# Requisitos: docker o podman. El stack Anomalia debe estar desplegado (Postgres).
# Red: /etc/hosts debe contener '127.0.0.1 anomalia.local'. Dentro de los contenedores
# anomalia.local apunta a host-gateway (extra_hosts definido en docker-compose.yml.j2).
#
# Uso:
#   scripts/oidc-test/run-test-idp.sh                # arranca (foreground)
#   scripts/oidc-test/run-test-idp.sh --configure    # arranca + configura Anomalia
#   scripts/oidc-test/run-test-idp.sh --stop         # detiene contenedor
#   scripts/oidc-test/run-test-idp.sh --reset        # detiene + limpia
#   scripts/oidc-test/run-test-idp.sh --logs         # sigue logs
#   scripts/oidc-test/run-test-idp.sh --sin-perfiles # sin claims de roles/grupos
#   scripts/oidc-test/run-test-idp.sh --groups "viewer,tenant_manager"
#
set -euo pipefail

PORT="${OIDC_TEST_PORT:-8080}"
ISSUER="${OIDC_TEST_ISSUER:-http://anomalia.local:${PORT}}"
HOST_ALIAS="${OIDC_TEST_HOST_ALIAS:-anomalia.local}"
CONTAINER_NAME="${OIDC_TEST_CONTAINER:-anomalia_oidc_mock}"
IMAGE="${OIDC_TEST_IMAGE:-ghcr.io/soluto/oidc-server-mock:latest}"

PG_CONTAINER="${OIDC_TEST_PG_CONTAINER:-anomalia_postgres}"
PG_USER="${OIDC_TEST_PG_USER:-anomal_user}"
PG_DB="${OIDC_TEST_PG_DB:-anomal_db}"

CLIENT_ID="${OIDC_TEST_CLIENT_ID:-anomalia-test}"
CLIENT_SECRET="${OIDC_TEST_CLIENT_SECRET:-anomalia-test-secret}"
REDIRECT_URIS_JSON="${OIDC_TEST_REDIRECT_URIS:-[\"https://anomalia.local/api/v1/oidc/callback\",\"http://localhost:8000/api/v1/oidc/callback\",\"anomalia://callback\"]}"
ALLOWED_SCOPES_JSON="${OIDC_TEST_SCOPES:-[\"openid\",\"profile\",\"email\",\"groups\"]}"
IDENTITY_RESOURCES_JSON="${OIDC_TEST_IDENTITY_RESOURCES:-}"
if [[ -z "$IDENTITY_RESOURCES_JSON" ]]; then
    IDENTITY_RESOURCES_JSON='[{"Name":"groups","ClaimTypes":["groups"]}]'
fi

TEST_USER="${OIDC_TEST_USER:-test@anomalia.local}"
TEST_PASSWORD="${OIDC_TEST_PASSWORD:-testpass}"
TEST_NAME="${OIDC_TEST_NAME:-Test User}"
TEST_GROUPS_CSV="${OIDC_TEST_GROUPS:-viewer}"
NO_CLAIMS="${OIDC_TEST_NO_CLAIMS:-false}"

CONFIGURE_ANOMALIA="${OIDC_TEST_CONFIGURE:-false}"
AUTH_MODE_AFTER="${OIDC_TEST_AUTH_MODE:-hybrid}"
OIDC_ENABLED_AFTER="true"
OIDC_AUTO_PROVISION_AFTER="true"
OIDC_REQUIRE_EMAIL_VERIFIED_AFTER="${OIDC_TEST_REQUIRE_EMAIL_VERIFIED:-true}"
OIDC_USERNAME_CLAIM_AFTER="${OIDC_TEST_USERNAME_CLAIM:-email}"
CLIENT_AUTH_METHOD_AFTER="${OIDC_TEST_CLIENT_AUTH_METHOD:-client_secret_basic}"
PKCE_ENABLED_AFTER="true"
USE_DISCOVERY_AFTER="true"
SCOPE_STRING="${OIDC_TEST_SCOPE_STRING:-openid profile email groups}"
WEB_REDIRECT_URI="${OIDC_TEST_WEB_REDIRECT_URI:-https://anomalia.local/api/v1/oidc/callback}"
PROVIDER_NAME="${OIDC_TEST_PROVIDER_NAME:-Test OIDC Mock}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '\033[1;34m[oidc-test]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[oidc-test] error:\033[0m %s\n' "$*" >&2; exit 1; }

detect_engine() {
    if command -v docker >/dev/null 2>&1; then echo docker
    elif command -v podman >/dev/null 2>&1; then echo podman
    else die "no se encontró docker ni podman"
    fi
}

container_exists() { "$1" ps -a --format '{{.Names}}' | grep -qx "$2"; }
container_running() { "$1" ps --format '{{.Names}}' | grep -qx "$2"; }

psql_exec() {
    local engine="$1" sql="$2"
    "$engine" exec "$PG_CONTAINER" \
        psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -c "$sql" >/dev/null
}

build_user_claims_json() {
    local parts=()
    parts+=('{"Type":"email","Value":"'"$TEST_USER"'","ValueType":"string"}')
    parts+=('{"Type":"email_verified","Value":"true","ValueType":"json"}')
    parts+=('{"Type":"name","Value":"'"$TEST_NAME"'","ValueType":"string"}')
    parts+=('{"Type":"preferred_username","Value":"'"$TEST_USER"'","ValueType":"string"}')
    if [[ "$NO_CLAIMS" != "true" ]]; then
        local garr
        IFS=',' read -r -a garr <<< "${TEST_GROUPS_CSV//[[:space:]]/}"
        for g in "${garr[@]}"; do
            [[ -z "$g" ]] && continue
            parts+=('{"Type":"groups","Value":"'"$g"'","ValueType":"string"}')
        done
    fi
    local IFS=','
    echo "[${parts[*]}]"
}

stop_container() {
    local engine="$1"
    if container_running "$engine" "$CONTAINER_NAME"; then
        log "deteniendo $CONTAINER_NAME..."
        "$engine" stop "$CONTAINER_NAME" >/dev/null
    fi
    if container_exists "$engine" "$CONTAINER_NAME"; then
        log "eliminando $CONTAINER_NAME..."
        "$engine" rm "$CONTAINER_NAME" >/dev/null
    fi
}

follow_logs() {
    local engine="$1"
    container_running "$engine" "$CONTAINER_NAME" \
        || die "contenedor $CONTAINER_NAME no está corriendo"
    "$engine" logs -f "$CONTAINER_NAME"
}

cleanup_anomalia_db() {
    local engine="$1"
    if container_running "$engine" "$PG_CONTAINER"; then
        log "limpiando configuración OIDC en PostgreSQL..."
        psql_exec "$engine" "DELETE FROM oidc_providers WHERE name='${PROVIDER_NAME}';"
        psql_exec "$engine" "UPDATE system_settings SET oidc_enabled = false, auth_mode = 'local', default_oidc_provider_id = NULL WHERE id = 1;"
    fi
}

reset_all() {
    local engine="$1"
    stop_container "$engine"
    cleanup_anomalia_db "$engine"
    log "reset completado (contenedor eliminado y BD de Anomalia restaurada)"
}

run_container() {
    local engine="$1"
    stop_container "$engine"

    local user_claims_json clients_json users_json server_opts_json
    user_claims_json="$(build_user_claims_json)"
    clients_json=$(cat <<JSON
[
  {
    "ClientId": "${CLIENT_ID}",
    "ClientSecrets": ["${CLIENT_SECRET}"],
    "Description": "Anomalia OIDC test client",
    "AllowedGrantTypes": ["authorization_code"],
    "RequirePkce": true,
    "AllowAccessTokensViaBrowser": true,
    "RedirectUris": ${REDIRECT_URIS_JSON},
    "AllowedScopes": ${ALLOWED_SCOPES_JSON},
    "IdentityTokenLifetime": 3600,
    "AccessTokenLifetime": 3600,
    "RequireClientSecret": true,
    "AlwaysIncludeUserClaimsInIdToken": true,
    "RequireConsent": false
  }
]
JSON
)
    users_json=$(cat <<JSON
[
  {
    "SubjectId": "1",
    "Username": "${TEST_USER}",
    "Password": "${TEST_PASSWORD}",
    "Claims": ${user_claims_json}
  }
]
JSON
)
    server_opts_json=$(cat <<JSON
{
  "AccessTokenJwtType": "JWT",
  "Discovery": { "ShowKeySet": true },
  "Authentication": {
    "CookieSameSiteMode": "Lax",
    "CheckSessionCookieSameSiteMode": "Lax"
  }
}
JSON
)

    log "levantando IdP mock: ${IMAGE} en ${ISSUER} (puerto ${PORT})"
    "$engine" run -d \
        --name "$CONTAINER_NAME" \
        -p "0.0.0.0:${PORT}:80" \
        -e ASPNETCORE_ENVIRONMENT=Development \
        -e ASPNETCORE_URLS="http://+:80" \
        -e SERVER_OPTIONS_INLINE="${server_opts_json}" \
        -e CLIENTS_CONFIGURATION_INLINE="${clients_json}" \
        -e USERS_CONFIGURATION_INLINE="${users_json}" \
        -e IDENTITY_RESOURCES_INLINE="${IDENTITY_RESOURCES_JSON}" \
        -e LOGIN_OPTIONS_INLINE='{"AllowRememberLogin": true}' \
        "${IMAGE}" >/dev/null
}

wait_ready() {
    local engine="$1"
    log "esperando a que el IdP esté listo..."
    local url="http://127.0.0.1:${PORT}/.well-known/openid-configuration"
    for i in {1..60}; do
        if curl -fsS "$url" >/dev/null 2>&1; then
            log "IdP listo en ${ISSUER}"
            return 0
        fi
        sleep 1
    done
    die "timeout esperando a ${url}"
}

configure_anomalia() {
    local engine="$1"
    if ! container_running "$engine" "$PG_CONTAINER"; then
        die "Postgres $PG_CONTAINER no está corriendo. Despliega el stack primero."
    fi

    log "configurando Anomalia SSO en PostgreSQL (auth_mode=${AUTH_MODE_AFTER})"
    psql_exec "$engine" "$(cat <<SQL
UPDATE system_settings
SET
  oidc_enabled = ${OIDC_ENABLED_AFTER},
  auth_mode = '${AUTH_MODE_AFTER}',
  oidc_auto_provision = ${OIDC_AUTO_PROVISION_AFTER},
  oidc_require_email_verified = ${OIDC_REQUIRE_EMAIL_VERIFIED_AFTER},
  oidc_username_claim = '${OIDC_USERNAME_CLAIM_AFTER}',
  updated_at = NOW()
WHERE id = 1;
SQL
)"

    local exists pid
    exists=$("$engine" exec "$PG_CONTAINER" \
        psql -U "$PG_USER" -d "$PG_DB" -tAc \
        "SELECT 1 FROM oidc_providers WHERE issuer_url='${ISSUER}' AND client_id='${CLIENT_ID}' LIMIT 1;" 2>/dev/null | tr -d '[:space:]')

    if [[ "$exists" == "1" ]]; then
        psql_exec "$engine" "$(cat <<SQL
UPDATE oidc_providers
SET
  name = '${PROVIDER_NAME}',
  client_secret = '${CLIENT_SECRET}',
  redirect_uri = '${WEB_REDIRECT_URI}',
  is_active = true,
  discovery_url = '${ISSUER}/.well-known/openid-configuration',
  use_discovery = ${USE_DISCOVERY_AFTER},
  client_auth_method = '${CLIENT_AUTH_METHOD_AFTER}',
  pkce_enabled = ${PKCE_ENABLED_AFTER},
  scopes = '${SCOPE_STRING}'
WHERE issuer_url='${ISSUER}' AND client_id='${CLIENT_ID}';
SQL
)"
        pid=$("$engine" exec "$PG_CONTAINER" \
            psql -U "$PG_USER" -d "$PG_DB" -tAc \
            "SELECT id FROM oidc_providers WHERE issuer_url='${ISSUER}' AND client_id='${CLIENT_ID}';" 2>/dev/null | tr -d '[:space:]')
    else
        pid=$("$engine" exec "$PG_CONTAINER" \
            psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -tAc "$(cat <<SQL
INSERT INTO oidc_providers (
  name, client_id, client_secret, issuer_url, redirect_uri, is_active,
  discovery_url, use_discovery, client_auth_method, pkce_enabled,
  allowed_domains, default_roles, scopes
)
VALUES (
  '${PROVIDER_NAME}', '${CLIENT_ID}', '${CLIENT_SECRET}', '${ISSUER}',
  '${WEB_REDIRECT_URI}', true,
  '${ISSUER}/.well-known/openid-configuration', ${USE_DISCOVERY_AFTER},
  '${CLIENT_AUTH_METHOD_AFTER}', ${PKCE_ENABLED_AFTER},
  '', '', '${SCOPE_STRING}'
)
RETURNING id;
SQL
)" 2>/dev/null | tr -d '[:space:]')
    fi

    [[ -n "$pid" ]] && psql_exec "$engine" "UPDATE system_settings SET default_oidc_provider_id=$pid, updated_at=NOW() WHERE id=1;" >/dev/null
    log "Anomalia configurada: oidc_enabled=true, auth_mode=${AUTH_MODE_AFTER}, default_provider_id=${pid:-?}"
}

main() {
    local engine
    engine="$(detect_engine)"
    log "engine: $engine"

    local action=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --configure) CONFIGURE_ANOMALIA=true; shift ;;
            --no-claims|--sin-perfiles) NO_CLAIMS=true; shift ;;
            --groups)
                [[ $# -lt 2 ]] && die "--groups requiere un valor CSV"
                TEST_GROUPS_CSV="$2"; shift 2 ;;
            --user)
                [[ $# -lt 2 ]] && die "--user requiere email"
                TEST_USER="$2"; shift 2 ;;
            --password)
                [[ $# -lt 2 ]] && die "--password requiere valor"
                TEST_PASSWORD="$2"; shift 2 ;;
            --port)
                [[ $# -lt 2 ]] && die "--port requiere número"
                PORT="$2"; ISSUER="http://anomalia.local:${PORT}"; shift 2 ;;
            --issuer)
                [[ $# -lt 2 ]] && die "--issuer requiere URL"
                ISSUER="$2"; shift 2 ;;
            --client-id)
                [[ $# -lt 2 ]] && die "--client-id requiere valor"
                CLIENT_ID="$2"; shift 2 ;;
            --client-secret)
                [[ $# -lt 2 ]] && die "--client-secret requiere valor"
                CLIENT_SECRET="$2"; shift 2 ;;
            --require-email-verified)
                [[ $# -lt 2 ]] && die "--require-email-verified requiere true|false"
                OIDC_REQUIRE_EMAIL_VERIFIED_AFTER="$2"; shift 2 ;;
            --auth-mode)
                [[ $# -lt 2 ]] && die "--auth-mode requiere local|hybrid|oidc_only|local_only"
                AUTH_MODE_AFTER="$2"; shift 2 ;;
            --stop) action="stop"; shift ;;
            --reset) action="reset"; shift ;;
            --logs) action="logs"; shift ;;
            -h|--help) action="help"; shift ;;
            *) die "opción desconocida: $1 (usa --help)" ;;
        esac
    done

    case "$action" in
        stop) stop_container "$engine"; cleanup_anomalia_db "$engine"; exit 0 ;;
        reset) reset_all "$engine"; exit 0 ;;
        logs) follow_logs "$engine"; exit 0 ;;
        help)
            cat <<USAGE
Uso: scripts/oidc-test/run-test-idp.sh [opciones]

Opciones:
  --configure                Configura Anomalia en PostgreSQL (system_settings + oidc_providers)
  --no-claims, --sin-perfiles No envía claims de roles/grupos (solo identidad)
  --groups CSV               Grupos/roles a enviar (p.ej. viewer,tenant_manager) [def: viewer]
  --user EMAIL               Usuario de prueba [def: test@anomalia.local]
  --password PASS            Contraseña [def: testpass]
  --port N                   Puerto host [def: 8080]
  --issuer URL               Issuer (override) [def: http://anomalia.local:PORT]
  --client-id ID             Client ID [def: anomalia-test]
  --client-secret SEC       Client Secret [def: anomalia-test-secret]
  --require-email-verified   true|false [def: true]
  --auth-mode MODE           local|hybrid|oidc_only|local_only [def: hybrid]
  --stop                     Detiene y elimina contenedor
  --reset                    Igual que --stop (limpieza)
  --logs                     Sigue logs del mock
  -h, --help                 Ayuda

Variables de entorno: OIDC_TEST_PORT, OIDC_TEST_ISSUER, OIDC_TEST_GROUPS,
OIDC_TEST_NO_CLAIMS, OIDC_TEST_USER, OIDC_TEST_PASSWORD, OIDC_TEST_CONFIGURE,
OIDC_TEST_REQUIRE_EMAIL_VERIFIED, OIDC_TEST_AUTH_MODE, OIDC_TEST_CLIENT_AUTH_METHOD.
USAGE
            exit 0 ;;
    esac

    run_container "$engine"
    wait_ready "$engine"

    if [[ "$CONFIGURE_ANOMALIA" == "true" ]]; then
        configure_anomalia "$engine"
    fi

    log "============================================================"
    log "IdP OIDC mock arrancado"
    log "Issuer:        ${ISSUER}"
    log "Discovery:     ${ISSUER}/.well-known/openid-configuration"
    log "Login UI:      ${ISSUER}/Account/Login"
    log "Usuario:       ${TEST_USER} / ${TEST_PASSWORD}"
    log "Groups/claims: ${NO_CLAIMS} (CSV: ${TEST_GROUPS_CSV})"
    log "Client ID:     ${CLIENT_ID}"
    log "PKCE:          true | Client Auth: ${CLIENT_AUTH_METHOD_AFTER}"
    log "Redirect URIs: ${REDIRECT_URIS_JSON}"
    log "Configure:     ${CONFIGURE_ANOMALIA}"
    log "============================================================"
    log "Ctrl+C para detener. Luego: scripts/oidc-test/run-test-idp.sh --stop"
    follow_logs "$engine"
}

main "$@"

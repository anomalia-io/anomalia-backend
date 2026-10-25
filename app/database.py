import os
import time
import urllib.parse as urlparse
import psycopg2
from sqlalchemy.ext.declarative import declarative_base

# Declaración única de Base para SQLAlchemy
Base = declarative_base()

# connect_timeout acota el reintento de conexion y lock_timeout garantiza que
# ninguna espera de lock supere los 15s (spec 011 4.2): sin el, un DDL o una
# query encolado tras una transaccion larga espera infinitamente y, si corre
# dentro del event loop, congela uvicorn (web no responde con TCP abierto).
_CONNECT_TIMEOUT = 10
_CONNECT_OPTIONS = "-c lock_timeout=15s"


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")
    retries = 5
    while retries > 0:
        try:
            if database_url:
                parsed_url = urlparse.urlparse(database_url)
                return psycopg2.connect(
                    dbname=parsed_url.path[1:],
                    user=parsed_url.username,
                    password=parsed_url.password,
                    host=parsed_url.hostname,
                    port=parsed_url.port,
                    connect_timeout=_CONNECT_TIMEOUT,
                    options=_CONNECT_OPTIONS,
                )
            return psycopg2.connect(
                host=os.getenv("DB_HOST", "postgres"),
                database=os.getenv("DB_NAME", "anomal_db"),
                user=os.getenv("DB_USER", "anomal_user"),
                password=os.getenv("DB_PASSWORD", "anomal_password"),
                connect_timeout=_CONNECT_TIMEOUT,
                options=_CONNECT_OPTIONS,
            )
        except psycopg2.OperationalError as e:
            retries -= 1
            if retries == 0:
                raise e
            time.sleep(2)

def get_db():
    """Dependencia de FastAPI para obtener una sesión/conexión de base de datos."""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()

# Clave de advisory lock que serializa el DDL de init_db entre procesos:
# anomalia_gw y anomalia_gw_tls importan este modulo a la vez en el arranque.
# Exclusiva de init_db; el ejecutor unico de workers usa otra clave
# (`workers.supervisor.DEFAULT_LOCK_KEY` = 'ANOS'). No reutilizar 'ANOM' alli:
# el ejecutor retiene su lock toda la vida del proceso.
_INIT_DB_ADVISORY_LOCK = 0x414E4F4D  # 'ANOM'


def init_db(hash_password_func):
    """Crea el esquema y seed de forma idempotente.

    Serializado entre procesos con ``pg_advisory_lock`` y con retry ante
    ``lock_timeout`` (spec 011 4.2): un ``ALTER TABLE`` encolado tras una
    transaccion larga no debe abortar el arranque ni esperar infinitamente.
    """
    last_error = None
    for attempt in range(1, 6):
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT pg_advisory_lock(%s)", (_INIT_DB_ADVISORY_LOCK,))
            _init_db_ddl(conn, cursor, hash_password_func)
            return
        except psycopg2.OperationalError as e:
            msg = str(e).lower()
            if "lock timeout" not in msg and "deadlock detected" not in msg:
                conn.rollback()
                print(f"Error inicializando base de datos: {e}")
                return
            last_error = e
            print(f"init_db: espera de lock expirada (intento {attempt}/5): {e}")
            time.sleep(attempt * 2)
        except Exception as e:
            conn.rollback()
            print(f"Error inicializando base de datos: {e}")
            return
        finally:
            cursor.close()
            conn.close()
    raise last_error


def _init_db_ddl(conn, cursor, hash_password_func):
    """Cuerpo idempotente de DDL + seed de init_db (spec 011 4.2)."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(150) UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_active BOOLEAN DEFAULT TRUE,
            theme VARCHAR(50) DEFAULT 'theme-enterprise-blue',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS roles (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) UNIQUE NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            id SERIAL PRIMARY KEY,
            code VARCHAR(100) UNIQUE NOT NULL,
            name VARCHAR(100) NOT NULL,
            description TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) UNIQUE NOT NULL,
            slug VARCHAR(100) UNIQUE,
            display_name VARCHAR(150),
            type VARCHAR(50) DEFAULT 'metrics',
            account_id INTEGER,
            project_id INTEGER,
            environment VARCHAR(50),
            port INTEGER DEFAULT 8427,
            description TEXT,
            instance_id INTEGER,
            vmalert_node_id INTEGER,
            vmalert_port INTEGER,
            has_alerts BOOLEAN DEFAULT FALSE,
            placement_mode VARCHAR(10) DEFAULT 'manual' CHECK (placement_mode IN ('manual','auto')),
            vmauth_node_id INTEGER,
            vmauth_placement_mode VARCHAR(10) DEFAULT 'manual' CHECK (vmauth_placement_mode IN ('manual','auto')),
            status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('provisioning','active','deleting','error','deleted_cleanup')),
            org_id_upper VARCHAR(100) UNIQUE,
            is_audit BOOLEAN DEFAULT FALSE,
            is_internal BOOLEAN DEFAULT FALSE,
            deleted_at TIMESTAMP,
            deletion_confirmed_at TIMESTAMP,
            deletion_confirmed_by INTEGER,
            deletion_challenge_id VARCHAR(64),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tenant_datasources (
            tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
            kind VARCHAR(50) NOT NULL,
            read_url TEXT NOT NULL,
            scoping VARCHAR(20) NOT NULL DEFAULT 'header',
            account_id INTEGER,
            project_id INTEGER,
            org_id VARCHAR(255),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Sequences for auto-generating account_id and project_id
    cursor.execute("CREATE SEQUENCE IF NOT EXISTS tenant_account_id_seq START 1000;")
    cursor.execute("CREATE SEQUENCE IF NOT EXISTS tenant_project_id_seq START 1000;")

    cursor.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_tenants_account_project_active ON tenants(account_id, project_id) WHERE status='active' AND deleted_at IS NULL;""")

    # Tickets de un solo uso que authorizes el montaje de una consola de
    # telemetria (specs/010). tenant_id es NULL para el scope
    # 'alertmanager_global', la unica consola sin tenant (specs/011 5.7).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ui_tickets (
            id SERIAL PRIMARY KEY,
            ticket VARCHAR(128) UNIQUE NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
            scope VARCHAR(32) NOT NULL DEFAULT 'metrics',
            expires_at TIMESTAMP NOT NULL,
            consumed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tenant_vmalert_instances (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER UNIQUE NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            instance_id INTEGER NOT NULL,
            port INTEGER NOT NULL,
            service VARCHAR(20) DEFAULT 'vmalert',
            unit_name VARCHAR(150) NOT NULL,
            rules_path VARCHAR(255) NOT NULL,
            rules_yaml TEXT,
            status VARCHAR(20) DEFAULT 'deployed' CHECK (status IN ('creating','deploying','deployed','error','stopped','undeployed')),
            health VARCHAR(10) DEFAULT 'unknown' CHECK (health IN ('ok','degraded','down','unknown')),
            last_deployed_at TIMESTAMP,
            enabled BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_tvi_instance_port UNIQUE (instance_id, port)
        );
    """)

    # Migración idempotente para BD preexistentes (spec 011/012): la regla de
    # alerta por tenant vive en la BD como fuente de verdad (rules_yaml).
    cursor.execute(
        "ALTER TABLE tenant_vmalert_instances ADD COLUMN IF NOT EXISTS rules_yaml TEXT"
    )
    # spec 011 §4.7: tenants de control interno (METRICS/TRACES/PROFILES/AUDIT_LOGS)
    # con reglas canónicas inmutables por API.
    cursor.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_internal BOOLEAN DEFAULT FALSE"
    )
    # spec 011 §4.10: nodo vmauth donde se materializa la ruta del tenant y su
    # modo de placement (manual/auto), análogo a vmalert_node_id.
    cursor.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS vmauth_node_id INTEGER"
    )
    cursor.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS vmauth_placement_mode VARCHAR(10) DEFAULT 'manual'"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS port_mapping (
            id SERIAL PRIMARY KEY,
            instance_id INTEGER NOT NULL,
            tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
            service VARCHAR(20),
            host_port INTEGER NOT NULL,
            container_port INTEGER,
            proto VARCHAR(5) DEFAULT 'tcp',
            purpose VARCHAR(50),
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_deletions (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
            actor INTEGER REFERENCES users(id) ON DELETE SET NULL,
            reason TEXT,
            ip VARCHAR(45),
            user_agent VARCHAR(255),
            challenge_id VARCHAR(64) NOT NULL UNIQUE,
            confirm_step1_at TIMESTAMP,
            confirm_step2_at TIMESTAMP,
            outcome VARCHAR(15) DEFAULT 'pending' CHECK (outcome IN ('pending','completed','expired','cancelled')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # El rollback de un alta fallida tambien se audita (spec 011 §4.9). El CHECK
    # original no contemplaba 'rolled_back'; se recrea de forma idempotente.
    cursor.execute("ALTER TABLE audit_deletions DROP CONSTRAINT IF EXISTS audit_deletions_outcome_check;")
    cursor.execute(
        "ALTER TABLE audit_deletions ADD CONSTRAINT audit_deletions_outcome_check "
        "CHECK (outcome IN ('pending','completed','expired','cancelled','rolled_back'));"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_state (
            id VARCHAR(36) PRIMARY KEY,
            type VARCHAR(30),
            ref_id INTEGER,
            status VARCHAR(12) DEFAULT 'queued' CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
            phase VARCHAR(30),
            progress_pct INTEGER DEFAULT 0,
            logs_ref TEXT,
            error_code VARCHAR(50),
            result JSONB,
            created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            timeout_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_state_type_status ON job_state(type, status);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_state_ref_id ON job_state(ref_id);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_state_created_at ON job_state(created_at);")
    # spec 011 §4.11: logs_ref pasa a TEXT (antes VARCHAR(255)) para no truncar el
    # diagnostico de fallos; idempotente para volumenes ya creados.
    cursor.execute("ALTER TABLE job_state ALTER COLUMN logs_ref TYPE TEXT;")
    # spec 011 §4.11: extra-vars de entrada del job (components/skip_regen).
    cursor.execute("ALTER TABLE job_state ADD COLUMN IF NOT EXISTS payload JSONB;")
    # spec 016: pasos visibles del job ({name,status,detail}) para la UI.
    cursor.execute("ALTER TABLE job_state ADD COLUMN IF NOT EXISTS steps JSONB;")
    # spec 016 §2.4: latido del job en ejecución (recuperación de huérfanos).
    cursor.execute("ALTER TABLE job_state ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMP;")

    # spec 016: snapshot previo a un import de backup, para revertir BD + remoto.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backup_import_state (
            job_id VARCHAR(36) PRIMARY KEY,
            snapshot JSONB NOT NULL,
            preexisting_tenant_ids INTEGER[] NOT NULL DEFAULT '{}',
            imported_pks JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # spec 017: notificaciones genéricas por usuario (campana in-app + email).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            type VARCHAR(50),
            title VARCHAR(255) NOT NULL,
            body TEXT,
            job_id VARCHAR(36),
            link VARCHAR(255),
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, is_read);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at);")

    # spec 019: solicitudes de acceso (roles/tenants) con flujo de aprobación.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS access_requests (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            requested_roles JSONB NOT NULL DEFAULT '[]',
            requested_tenants JSONB NOT NULL DEFAULT '[]',
            reason TEXT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','rejected','cancelled')),
            reviewer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            review_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_access_requests_status ON access_requests(status);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_access_requests_user ON access_requests(user_id);")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_roles (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, role_id)
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS role_profiles (
            role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
            profile_id INTEGER REFERENCES profiles(id) ON DELETE CASCADE,
            PRIMARY KEY (role_id, profile_id)
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_tenants (
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, tenant_id)
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS role_tenants (
            role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
            tenant_id INTEGER REFERENCES tenants(id) ON DELETE CASCADE,
            PRIMARY KEY (role_id, tenant_id)
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS oidc_providers (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL,
            issuer_url TEXT NOT NULL,
            discovery_url TEXT,
            authorization_endpoint TEXT,
            token_endpoint TEXT,
            jwks_uri TEXT,
            userinfo_endpoint TEXT,
            end_session_endpoint TEXT,
            scopes TEXT DEFAULT 'openid profile email',
            use_discovery BOOLEAN DEFAULT TRUE,
            client_auth_method VARCHAR(50) DEFAULT 'client_secret_basic',
            pkce_enabled BOOLEAN DEFAULT TRUE,
            allowed_domains TEXT,
            default_roles TEXT DEFAULT '',
            client_id VARCHAR(255) NOT NULL,
            client_secret TEXT,
            redirect_uri TEXT,
            is_active BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # spec 020: migraciones idempotentes para proveedores OIDC preexistentes.
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS discovery_url TEXT")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS authorization_endpoint TEXT")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS token_endpoint TEXT")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS jwks_uri TEXT")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS userinfo_endpoint TEXT")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS end_session_endpoint TEXT")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS scopes TEXT DEFAULT 'openid profile email'")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS use_discovery BOOLEAN DEFAULT TRUE")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS client_auth_method VARCHAR(50) DEFAULT 'client_secret_basic'")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS pkce_enabled BOOLEAN DEFAULT TRUE")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS allowed_domains TEXT")
    cursor.execute("ALTER TABLE oidc_providers ADD COLUMN IF NOT EXISTS default_roles TEXT DEFAULT ''")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_settings (
            id SERIAL PRIMARY KEY,
            selected_provider VARCHAR(100) DEFAULT 'anomalia_ollama',
            zen_api_key TEXT DEFAULT '',
            gemini_api_key TEXT DEFAULT '',
            gemini_model VARCHAR(100) DEFAULT 'gemini-3.5-flash-lite',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("SELECT COUNT(*) FROM ai_settings;")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO ai_settings (selected_provider) VALUES ('anomalia_ollama');")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            id SERIAL PRIMARY KEY,
            smtp_host VARCHAR(255) DEFAULT '',
            smtp_port INTEGER DEFAULT 587,
            smtp_username VARCHAR(255) DEFAULT '',
            smtp_password TEXT DEFAULT '',
            smtp_use_tls BOOLEAN DEFAULT TRUE,
            smtp_from VARCHAR(255) DEFAULT '',
            proxy_url VARCHAR(500) DEFAULT '',
            proxy_enabled BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    cursor.execute("SELECT COUNT(*) FROM system_settings;")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO system_settings (smtp_host, smtp_port, smtp_use_tls, proxy_enabled)
            VALUES ('', 587, TRUE, FALSE);
        """)

    # spec 003/018: idioma del análisis IA de alertas (default 'en'; configurable
    # en el panel de Proveedor IA). Migración idempotente para BD existentes.
    cursor.execute("ALTER TABLE ai_settings ADD COLUMN IF NOT EXISTS analysis_language VARCHAR(5) DEFAULT 'en'")

    # spec 018: push móvil (FCM). La configuración vive solo en la BD, nunca en
    # el repositorio (constitución §4). Sin credenciales el envío es best-effort.
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS push_enabled BOOLEAN DEFAULT TRUE")
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS fcm_project_id VARCHAR(255) DEFAULT ''")
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS fcm_service_account_json TEXT DEFAULT ''")

    # spec 020: autenticación OIDC habilitable/deshabilitable y modos de login.
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS auth_mode VARCHAR(20) NOT NULL DEFAULT 'hybrid'")
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS oidc_enabled BOOLEAN NOT NULL DEFAULT FALSE")
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS default_oidc_provider_id INTEGER")
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS oidc_auto_provision BOOLEAN NOT NULL DEFAULT TRUE")
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS oidc_require_email_verified BOOLEAN NOT NULL DEFAULT TRUE")
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS oidc_username_claim VARCHAR(50) DEFAULT 'email'")
    cursor.execute("ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS oidc_groups_claim VARCHAR(50) DEFAULT 'groups'")

    # spec 018: destinatarios de alertas por tenant (X correos), gestionados desde
    # el panel Alerts (conf) por el perfil alerts_manager.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tenant_alert_recipients (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            email VARCHAR(255) NOT NULL,
            label VARCHAR(150) DEFAULT '',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (tenant_id, email)
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tenant_alert_recipients_tenant ON tenant_alert_recipients(tenant_id);")

    # spec 003/018: historial de alertas enriquecidas por IA. Se crea aquí
    # (además de init.sql.j2) para que un volumen creado solo por la app tenga la
    # tabla; las columnas nuevas son migraciones idempotentes.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alert_history (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL,
            fingerprint VARCHAR(255),
            alertname VARCHAR(255) NOT NULL,
            severity VARCHAR(50),
            summary TEXT,
            description TEXT,
            ai_analysis TEXT,
            status VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    for ddl in (
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS tenant_id INTEGER REFERENCES tenants(id) ON DELETE SET NULL",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(255)",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS labels JSONB",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS annotations JSONB",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS generator_url TEXT",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS ai_summary TEXT",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS ai_root_cause TEXT",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS ai_checks JSONB",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS severity_source VARCHAR(10) DEFAULT 'ai'",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS severity_override_by VARCHAR(150)",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS severity_override_at TIMESTAMP",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS notified_at TIMESTAMP",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS starts_at TIMESTAMP",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS ends_at TIMESTAMP",
        # spec 018: marca de fallo del análisis IA (kind: quota/transient/auth/...).
        # Presente = la tarjeta se muestra con los datos de Alertmanager y un aviso
        # de "IA no disponible", sin dejar el diagnóstico vacío.
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS ai_error VARCHAR(50)",
        # spec 018: resultado del envío del email de alerta a los destinatarios del
        # tenant ('sent' | 'failed' | 'not_configured' | 'skipped') y el motivo del
        # fallo. Se persiste en el mismo UPDATE que marca notified_at, para que el
        # "no me llegó el mail" sea auditable desde la tarjeta y no solo desde logs.
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS email_status VARCHAR(16)",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS email_error VARCHAR(200)",
        "ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    ):
        cursor.execute(ddl)
    # Dedup/upsert: una UNA fila ACTIVA por (tenant, fingerprint) — índice parcial
    # sobre status='firing'. Al resolver, la fila pasa a 'resolved' y sale del
    # índice, de modo que un re-disparo abre un nuevo episodio (nueva fila) sin
    # colisionar. Así el webhook puede cerrar la alerta activa (UPDATE) en vez de
    # duplicarla (INSERT) cuando AM envía status='resolved' (spec 018).
    # Se elimina el índice legado que incluía status en la clave: impediría tener
    # varias resoluciones del mismo fingerprint a lo largo del tiempo.
    cursor.execute("DROP INDEX IF EXISTS uq_alert_history_fingerprint_status")
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_history_active "
        "ON alert_history (tenant_id, fingerprint) WHERE status = 'firing'"
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_alert_history_status_created ON alert_history(status, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_alert_history_tenant ON alert_history(tenant_id)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_alert_history_ends_at ON alert_history(ends_at)"
    )

    # spec 018: estado del proveedor de IA (singleton). Permite avisar por banner
    # global + in-app cuando una IA cloud agota su cuota de tokens (Gemini/Zen).
    # El modelo local (Ollama) no tiene cuota.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_provider_status (
            id INTEGER PRIMARY KEY DEFAULT 1,
            provider VARCHAR(100) DEFAULT '',
            state VARCHAR(30) DEFAULT 'ok',
            kind VARCHAR(30) DEFAULT '',
            message TEXT DEFAULT '',
            consecutive_failures INTEGER DEFAULT 0,
            last_error_at TIMESTAMP,
            last_success_at TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute(
        "INSERT INTO ai_provider_status (id, state) VALUES (1, 'ok') ON CONFLICT (id) DO NOTHING"
    )

    # spec 017/018: dispositivos móviles para push (una fila por token FCM).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_devices (
            id SERIAL PRIMARY KEY,
            user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            fcm_token TEXT UNIQUE NOT NULL,
            platform VARCHAR(50) DEFAULT 'android',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("ALTER TABLE user_devices ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS infrastructure_nodes (
            id SERIAL PRIMARY KEY,
            hostname VARCHAR(150) UNIQUE NOT NULL,
            ip_address VARCHAR(50) NOT NULL,
            service_ip VARCHAR(50),
            component_type VARCHAR(50) DEFAULT 'auth',
            port INTEGER DEFAULT 8427,
            status VARCHAR(50) DEFAULT 'operational',
            description TEXT,
            roles VARCHAR(255),
            ports_pool JSONB,
            capacity_slots INTEGER DEFAULT 10,
            tenants_count_active INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Claves foráneas diferidas (spec 011). Los REFERENCES en línea no son
    # posibles: tenants se crea antes que users y las tablas del ciclo de
    # vida de vmalert antes que infrastructure_nodes. Idempotente.
    cursor.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tenants_instance_id') THEN
                ALTER TABLE tenants ADD CONSTRAINT fk_tenants_instance_id
                    FOREIGN KEY (instance_id) REFERENCES infrastructure_nodes(id) ON DELETE SET NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tenants_vmalert_node_id') THEN
                ALTER TABLE tenants ADD CONSTRAINT fk_tenants_vmalert_node_id
                    FOREIGN KEY (vmalert_node_id) REFERENCES infrastructure_nodes(id) ON DELETE SET NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tenants_vmauth_node_id') THEN
                ALTER TABLE tenants ADD CONSTRAINT fk_tenants_vmauth_node_id
                    FOREIGN KEY (vmauth_node_id) REFERENCES infrastructure_nodes(id) ON DELETE SET NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tenants_deletion_confirmed_by') THEN
                ALTER TABLE tenants ADD CONSTRAINT fk_tenants_deletion_confirmed_by
                    FOREIGN KEY (deletion_confirmed_by) REFERENCES users(id) ON DELETE SET NULL;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_tvi_instance_id') THEN
                ALTER TABLE tenant_vmalert_instances ADD CONSTRAINT fk_tvi_instance_id
                    FOREIGN KEY (instance_id) REFERENCES infrastructure_nodes(id) ON DELETE RESTRICT;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_portmap_instance_id') THEN
                ALTER TABLE port_mapping ADD CONSTRAINT fk_portmap_instance_id
                    FOREIGN KEY (instance_id) REFERENCES infrastructure_nodes(id) ON DELETE RESTRICT;
            END IF;
        END $$;
    """)

    cursor.execute("SELECT COUNT(*) FROM users;")
    if cursor.fetchone()[0] == 0:
        default_pass = hash_password_func("admin123")
        cursor.execute("""
            INSERT INTO users (username, password_hash, is_active, theme)
            VALUES (%s, %s, TRUE, 'theme-enterprise-blue')
        """, ("root@anomalia.local", default_pass))

    cursor.execute("""
        INSERT INTO roles (name, description) VALUES 
        ('admin', 'Administrador Global'),
        ('viewer', 'Visualizador de Alertas'),
        ('auditor', 'Auditor de Seguridad'),
        ('tenant_manager', 'Gestor de Tenants'),
        ('infra_manager', 'Gestor de Infraestructura'),
        ('user_manager', 'Gestor de Usuarios'),
        ('role_manager', 'Gestor de Roles')
        ON CONFLICT (name) DO NOTHING;
    """)

    cursor.execute("""
        INSERT INTO profiles (code, name, description) VALUES 
        ('admin', 'Admin Profile', 'Control absoluto del sistema'),
        ('users_manager', 'Users Manager Profile', 'Gestión de cuentas y usuarios'),
        ('roles_manager', 'Roles Manager Profile', 'Gestión de roles de seguridad'),
        ('profile_manager', 'Profile Manager Profile', 'Gestión de perfiles de acceso'),
        ('access_metrics', 'Metrics Profile', 'Perfil para recolección y consulta de métricas'),
        ('access_logs', 'Logs Profile', 'Perfil para auditoría y logs centralizados'),
        ('access_traces', 'Traces Profile', 'Perfil para trazabilidad distribuida'),
        ('access_Continuous_Profiling', 'Profiling Profile', 'Perfil para profiling continuo'),
        ('access_alerts', 'Alerts Profile', 'Perfil de visualización de alertas'),
        ('tenants_manager', 'Tenants Manager Profile', 'Gestión de tenants y namespaces'),
        ('infra_manager', 'Infra Management Profile', 'Gestión de nodos de infraestructura y ruteadores')
        ON CONFLICT (code) DO NOTHING;
    """)

    cursor.execute("SELECT COUNT(*) FROM user_roles;")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO user_roles (user_id, role_id) SELECT 1, id FROM roles WHERE name = 'admin' ON CONFLICT DO NOTHING;")

    cursor.execute("SELECT COUNT(*) FROM tenants;")
    if cursor.fetchone()[0] == 0:
        cursor.execute("""
            INSERT INTO tenants (name, type, account_id, project_id, environment, port, description, has_alerts, is_internal, placement_mode) VALUES 
            ('METRICS', 'metrics', 0, 0, 'default', 8400, 'Tenant para métricas de infraestructura (VictoriaMetrics - vminsert)', TRUE, TRUE, 'auto'),
            ('TRACES', 'traces', 0, 1, 'default', 8490, 'Tenant para trazas distribuidas OTLP (VictoriaTraces - vtinsert)', TRUE, TRUE, 'auto'),
            ('PROFILES', 'profiles', NULL, NULL, 'default', 4040, 'Tenant de perfilado continuo vía Pyroscope (X-Scope-OrgID: PROFILES)', TRUE, TRUE, 'auto'),
            ('AUDIT_LOGS', 'logs', 0, 3, 'default', 8480, 'Tenant para logs de auditoría (VictoriaLogs - vlinsert)', TRUE, TRUE, 'auto')
            ON CONFLICT (name) DO NOTHING;
        """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS oidc_sessions (
            id VARCHAR(64) PRIMARY KEY,
            state VARCHAR(64) NOT NULL,
            nonce VARCHAR(128) NOT NULL,
            code_verifier VARCHAR(128) NOT NULL,
            redirect_uri TEXT NOT NULL,
            client_id VARCHAR(255) NOT NULL,
            provider_id INTEGER REFERENCES oidc_providers(id) ON DELETE CASCADE,
            session_type VARCHAR(20) NOT NULL DEFAULT 'web' CHECK (session_type IN ('web','mobile')),
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_oidc_sessions_state ON oidc_sessions(state);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_oidc_sessions_expires ON oidc_sessions(expires_at);")

    conn.commit()

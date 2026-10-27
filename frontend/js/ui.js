import { api } from './api.js';
import { state, pollJob } from './state.js';

// Consolas de telemetría accesibles desde el panel principal.
//
// Cada entrada indica el contenedor donde se pinta, el perfil que la habilita
// y como se seleccionan los tenants. `scope` viaja al backend y es el que
// determina que perfil se exige para emitir el ticket (UI_SCOPE_PROFILE), de
// modo que METRICS puede abrir la vista de métricas sin tener la de dashboards.
//
// El <h3> de cada tarjeta es el nombre del tenant; `title` es el nombre de
// producto (Anomalia-Metrics, Anomalia-Parses, ...) que se muestra en el chip.
// `tab` es la pestaña del panel principal que gobierna esta consola: una pestaña
// solo se muestra si el usuario tiene el perfil Y al menos un tenant que haga
// match, así que no hay que ocultarlas a mano una por una.
const CONSOLES = [
    {
        scope: 'metrics', profile: 'access_metrics', container: 'metrics-tenants-container',
        tab: 'metrics',
        icon: 'fa-chart-pie', title: 'Anomalia-Metrics', button: 'Abrir VictoriaMetrics',
        description: 'Tenant de Métricas',
        empty: 'No hay tenants de métricas autorizados para esta sesión.',
        match: t => (t.type || '').toLowerCase().includes('metric') || t.metrics_url || !t.type
    },
    {
        scope: 'logs', profile: 'access_logs', container: 'logs-tenants-container',
        tab: 'logs',
        icon: 'fa-file-lines', title: 'Anomalia-Logs', button: 'Abrir VictoriaLogs',
        description: 'Tenant de Logs',
        empty: 'No hay tenants de logs autorizados para esta sesión.',
        // is_audit separa el tenant de auditoría, que también es de tipo LOGS:
        // sin esto aparecía duplicado en la pestaña Logs.
        match: t => !t.is_audit && ((t.type || '').toLowerCase().includes('log') || t.logs_url)
    },
    {
        scope: 'traces', profile: 'access_traces', container: 'traces-container',
        tab: 'traces',
        icon: 'fa-route', title: 'Anomalia-Traces', button: 'Abrir VictoriaTraces',
        description: 'Tenant de Trazas',
        empty: 'No hay tenants de trazas autorizados para esta sesión.',
        match: t => (t.type || '').toLowerCase() === 'traces'
    },
    {
        scope: 'profiling', profile: 'access_Continuous_Profiling', container: 'profiling-container',
        tab: 'profiling',
        icon: 'fa-microchip', title: 'Anomalia-Profiles', button: 'Abrir Pyroscope',
        description: 'Tenant de Perfilado Continuo',
        empty: 'No hay tenants de perfilado autorizados para esta sesión.',
        match: t => (t.type || '').toLowerCase() === 'profiles'
    },
    {
        scope: 'dashboards', profile: 'access_dasboards', container: 'dashboards-container',
        tab: 'dashboards',
        icon: 'fa-chart-line', title: 'Anomalia-Parses', button: 'Abrir Parses',
        description: 'Dashboards de observabilidad (Parses)',
        empty: 'No hay tenants de dashboards autorizados para esta sesión.',
        // El proxy de Parses ya abre todos los proyectos a los que el usuario tiene
        // acceso, asi que una unica tarjeta alcanza (no una por tenant METRICS).
        single: true,
        match: t => (t.type || '').toLowerCase().includes('metric') || t.metrics_url || !t.type
    },
    {
        // Auditoría es una pestaña del panel de administración, no del panel
        // principal: no lleva `tab` y su tarjeta conserva el nombre del tenant.
        scope: 'audit', profile: 'audit', container: 'audit-logs-container',
        icon: 'fa-clock-rotate-left', title: 'Anomalia-Audit', button: 'Abrir VictoriaLogs',
        description: 'Registros de auditoría del tenant',
        empty: 'No hay tenants de auditoría autorizados para esta sesión.',
        match: t => !!t.is_audit
    }
];

// Pestañas del panel principal que gobierna CONSOLES. `alerts` no está porque no
// es una consola de tenants: su contenido es estático y no depende de asignaciones.
const MANAGED_TABS = new Set(CONSOLES.map(cfg => cfg.tab).filter(Boolean));

// Auto-refresh del listado de consolas de alertas (spec 011 §5.4): tras un
// Controladores AbortController para las esperas únicas de la lista de alertas.
const alertsConfigControllers = {};

function cancelAlertsConfigWait(key) {
    if (alertsConfigControllers[key]) {
        alertsConfigControllers[key].abort();
        delete alertsConfigControllers[key];
    }
}

async function startAlertsConfigWait(key, mine, renderFn) {
    cancelAlertsConfigWait(key);
    const controller = new AbortController();
    alertsConfigControllers[key] = controller;
    try {
        await api.fetchAlertsTenants({ mine, waitDeployed: true, signal: controller.signal });
        renderFn();
    } catch (err) {
        if (err.name !== 'AbortError') {
            console.error('Error en espera de despliegue de alertas:', err);
        }
    } finally {
        if (alertsConfigControllers[key] === controller) {
            delete alertsConfigControllers[key];
        }
    }
}

// Etiqueta del job vmalert más reciente del tenant (deploy/redeploy/undeploy).
// Devuelve null si no hay job o si ya está en estado terminal.
function alertsTenantJobLabel(tenant) {
    const status = tenant.vmalert_job_status;
    if (status === 'queued') return 'En cola de despliegue';
    if (status === 'running') {
        const parts = [];
        if (tenant.vmalert_job_phase) parts.push(tenant.vmalert_job_phase);
        if (typeof tenant.vmalert_job_progress === 'number') {
            parts.push(`${tenant.vmalert_job_progress}%`);
        }
        return parts.length ? `Desplegando… ${parts.join(' · ')}` : 'Desplegando…';
    }
    return null;
}

function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
}

export const ui = {
    applyTheme(themeName) {
        document.documentElement.className = themeName;
    },

    toggleModal(modalId, show = null) {
        const modal = document.getElementById(modalId);
        if (!modal) return;
        const isHidden = modal.classList.contains('hidden');
        const shouldShow = show !== null ? show : isHidden;
        if (shouldShow) {
            modal.classList.remove('hidden');
            modal.classList.add('flex');
        } else {
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }
    },

    toggleAdminSidebar() {
        document.getElementById("admin-sidebar")?.classList.toggle("hidden");
    },

    returnToMainView() {
        document.getElementById('admin-views-container')?.classList.add('hidden');
        document.getElementById('main-tabs-container')?.classList.remove('hidden');
        this.switchTab('metrics');
    },

    switchTab(tabName) {
        // Si la pestaña pedida está gestionada por CONSOLES y quedó oculta (no hay
        // perfil o no hay tenant), se cae a la primera visible en lugar de dejar la
        // pantalla en blanco.
        if (MANAGED_TABS.has(tabName) && this.visibleTabs && !this.visibleTabs.has(tabName)) {
            const first = this.visibleTabs.values().next().value;
            if (first) tabName = first;
        }

        document.querySelectorAll('.tab-view').forEach(el => el.classList.add('hidden'));
        document.getElementById('admin-views-container')?.classList.add('hidden');
        document.getElementById('main-tabs-container')?.classList.remove('hidden');
        
        document.querySelectorAll('.tab-btn').forEach(btn => {
            btn.classList.remove('border-current', 'dynamic-text-accent');
            btn.classList.add('border-transparent', 'opacity-70');
        });

        const targetView = document.getElementById(`view-${tabName}`);
        if (targetView) targetView.classList.remove('hidden');
        
        const activeBtn = document.querySelector(`.tab-btn[data-tab="${tabName}"]`);
        if (activeBtn) {
            activeBtn.classList.remove('border-transparent', 'opacity-70');
            activeBtn.classList.add('border-current', 'dynamic-text-accent');
        }

        // Emit tab-changed event for AI assistant context switching
        window.dispatchEvent(new CustomEvent('tab-changed', { detail: { tab: tabName } }));

        // La pestaña Alertas muestra sólo el listado/bitácora y la búsqueda. La
        // administración de consolas (Alerts conf) vive en el panel de control.
        if (tabName === 'alerts') {
            this.renderAlerts();
            this.ensureAlertSearchTenants();
            this._wireAlertSearch();
        }
    },

    switchAdminTab(adminTabName) {
        cancelAlertsConfigWait('admin-alerts');
        document.querySelectorAll('.tab-view').forEach(el => el.classList.add('hidden'));
        document.getElementById('main-tabs-container')?.classList.add('hidden');
        
        document.getElementById('admin-views-container')?.classList.remove('hidden');
        document.querySelectorAll('.admin-tab-view').forEach(el => el.classList.add('hidden'));
        
        document.querySelectorAll('.admin-tab-btn').forEach(btn => {
            btn.classList.remove('dynamic-card', 'border-current');
            btn.classList.add('border-transparent');
        });

        const targetView = document.getElementById(`admin-view-${adminTabName}`);
        if (targetView) targetView.classList.remove('hidden');
        
        const activeBtn = document.getElementById(`admin-tab-btn-${adminTabName}`);
        if (activeBtn) activeBtn.classList.add('dynamic-card', 'border-current');
    },

    hasProfile(profile, roles = [], profiles = []) {
        return roles.includes('admin') || roles.includes(profile) || profiles.includes(profile);
    },

    renderLoading() {
        CONSOLES.forEach(cfg => {
            const container = document.getElementById(cfg.container);
            if (container) container.innerHTML = '<p class="text-xs opacity-75">Cargando consolas...</p>';
        });
    },

    // Banner global del estado del proveedor de IA (spec 018). Se muestra cuando
    // una IA cloud agota su cuota (quota_exhausted) o queda degradada; el usuario
    // puede ocultarlo temporalmente hasta que el estado cambie.
    renderAiStatusBanner(status = {}) {
        const banner = document.getElementById('ai-status-banner');
        if (!banner) return;
        const state = status.state || 'ok';
        const dismissedFor = this._aiBannerDismissedState;
        if (state === 'ok') {
            this._aiBannerDismissedState = null;
            banner.classList.add('hidden');
            banner.classList.remove('flex');
            return;
        }
        if (dismissedFor === state) {
            banner.classList.add('hidden');
            banner.classList.remove('flex');
            return;
        }
        const titleEl = document.getElementById('ai-status-banner-title');
        const messageEl = document.getElementById('ai-status-banner-message');
        if (titleEl) {
            titleEl.textContent = state === 'quota_exhausted'
                ? 'Cuota de IA agotada'
                : 'Proveedor de IA degradado';
        }
        if (messageEl) {
            messageEl.textContent = status.message
                || 'No se puede generar el análisis IA de las alertas. Se están mostrando los datos originales de Alertmanager.';
        }
        banner.classList.remove('hidden');
        banner.classList.add('flex');
    },

    dismissAiStatusBanner(status = {}) {
        this._aiBannerDismissedState = status.state || null;
        const banner = document.getElementById('ai-status-banner');
        if (banner) {
            banner.classList.add('hidden');
            banner.classList.remove('flex');
        }
    },

    renderTenants(tenants = [], roles = [], profiles = []) {
        const visibleTabs = new Set();

        CONSOLES.forEach(cfg => {
            const container = document.getElementById(cfg.container);
            if (!container) return;

            const allowed = this.hasProfile(cfg.profile, roles, profiles);
            const matched = allowed ? tenants.filter(cfg.match) : [];
            // Consolas con `single` (Parses) muestran una sola tarjeta aunque el
            // usuario tenga varios tenants que hagan match.
            const matches = cfg.single ? matched.slice(0, 1) : matched;

            // Regla única de visibilidad: perfil Y al menos un tenant.
            if (cfg.tab && allowed && matches.length) visibleTabs.add(cfg.tab);

            if (!allowed) {
                container.innerHTML =
                    `<p class="text-xs opacity-75">Requiere el perfil «${esc(cfg.profile)}».</p>`;
                return;
            }

            if (!matches.length) {
                container.innerHTML = `<p class="text-xs opacity-75">${esc(cfg.empty)}</p>`;
                return;
            }

            container.innerHTML = matches.map(t => `
                <div class="dynamic-card border rounded-2xl p-6 shadow-xl flex flex-col justify-between">
                    <div>
                        <div class="flex justify-between items-start mb-4">
                            <span class="p-3 dynamic-accent rounded-xl"><i class="fa-solid ${esc(cfg.icon)} text-lg"></i></span>
                            <span class="text-xs dynamic-card px-2 py-1 rounded border font-mono">${esc(cfg.title || t.type || '')}</span>
                        </div>
                        <h3 class="text-lg font-bold mb-2">${esc(t.name)}</h3>
                        <p class="text-xs opacity-75 mb-4">${esc(cfg.description)}</p>
                    </div>
                    <button type="button" class="btn-open-console w-full dynamic-accent text-center py-2.5 rounded-xl font-medium text-sm transition"
                            data-tenant-id="${esc(t.id)}" data-scope="${esc(cfg.scope)}" data-label="${esc(cfg.button)}">
                        ${esc(cfg.button)}
                    </button>
                </div>
            `).join('');
        });

        this.applyTabVisibility(visibleTabs);
    },

    // Re-render de las consolas de tenants tras un alta/baja sin recargar el navegador.
    // `renderTenants` ya reconstruye las tarjetas, recalcula la visibilidad de pestañas
    // y activa la pestaña correspondiente al volver a la home.
    async refreshUserTenants() {
        try {
            const tenants = await api.fetchUserTenants();
            this.renderTenants(tenants, state.currentUserRoles, state.currentUserProfiles);
        } catch (err) {
            console.error('Error al refrescar consolas de tenants:', err);
        }
    },

    applyTabVisibility(visibleTabs) {
        this.visibleTabs = visibleTabs;

        // La pestaña activa se lee ANTES de tocar nada: es la referencia para
        // decidir que seccion se deja abierta.
        const activeTab = document.querySelector('#main-tabs-container .tab-btn.border-current')
            ?.getAttribute('data-tab') || 'metrics';

        // Con el ABM abierto, el panel principal debe quedar oculto: si se
        // destapara una vista, las tarjetas de consolas aparecerían por encima
        // del ABM (bug al crear un tenant desde la gestión de tenants).
        const adminOpen = !document.getElementById('admin-views-container')
            ?.classList.contains('hidden');

        MANAGED_TABS.forEach(tab => {
            const show = visibleTabs.has(tab);
            document.getElementById(`tab-btn-${tab}`)?.classList.toggle('hidden', !show);
            // Disponibilidad y activacion son dos cosas distintas: sin la segunda
            // condicion renderTenants le quitaba `hidden` a todas las secciones
            // disponibles y se veian todas las tarjetas a la vez.
            if (!adminOpen) {
                document.getElementById(`view-${tab}`)
                    ?.classList.toggle('hidden', !show || tab !== activeTab);
            }
        });

        // Si la pestaña que estaba activa quedó oculta, se salta a la primera
        // visible para no dejar el área principal vacía. No se hace con el ABM
        // abierto: sacaría al usuario del panel de administración.
        if (!adminOpen && MANAGED_TABS.has(activeTab) && !visibleTabs.has(activeTab)) {
            const first = visibleTabs.values().next().value;
            if (first) this.switchTab(first);
        }
    },

    buildAdminSidebarMenu(roles, profiles = []) {
        const menuContainer = document.getElementById("admin-sidebar-menu");
        let html = '';
        
        const isAdmin = roles.includes('admin');
        const hasProfile = (prof) => isAdmin || profiles.includes(prof) || roles.includes(prof);

        if (isAdmin) {
            html += `<button data-admintab="general" id="admin-tab-btn-general" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-gear w-5 dynamic-text-accent"></i><span>General</span></button>`;
            html += `<button data-admintab="android" id="admin-tab-btn-android" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-brands fa-android w-5 dynamic-text-accent"></i><span>Android</span></button>`;
            html += `<button data-admintab="bkp" id="admin-tab-btn-bkp" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-database w-5 dynamic-text-accent"></i><span>BKP</span></button>`;
        }
        if (hasProfile('users_manager')) {
            html += `<button data-admintab="users" id="admin-tab-btn-users" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-users w-5 dynamic-text-accent"></i><span>Usuarios (Abm)</span></button>`;
        }
        if (hasProfile('tenants_manager')) {
            html += `<button data-admintab="tenants" id="admin-tab-btn-tenants" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-network-wired w-5 dynamic-text-accent"></i><span>Tenants (Abm)</span></button>`;
        }
        if (hasProfile('roles_manager')) {
            html += `<button data-admintab="roles" id="admin-tab-btn-roles" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-shield-halved w-5 dynamic-text-accent"></i><span>Roles (Abm)</span></button>`;
        }
        if (hasProfile('profile_manager')) {
            html += `<button data-admintab="profiles" id="admin-tab-btn-profiles" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-id-card w-5 dynamic-text-accent"></i><span>Perfiles (Abm)</span></button>`;
        }
        if (hasProfile('infra_manager')) {
            html += `<button data-admintab="infra" id="admin-tab-btn-infra" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-server w-5 dynamic-text-accent"></i><span>Infraestructura</span></button>`;
        }
        if (hasProfile('alerts_manager')) {
            html += `<button data-admintab="alerts" id="admin-tab-btn-alerts" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-gear w-5 dynamic-text-accent"></i><span>Alerts (conf)</span></button>`;
        }
        if (hasProfile('admin')) {
            html += `<button data-admintab="sso" id="admin-tab-btn-sso" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-key w-5 dynamic-text-accent"></i><span>SSO (OIDC)</span></button>`;
            html += `<button data-admintab="ai" id="admin-tab-btn-ai" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-brain w-5 dynamic-text-accent"></i><span>Proveedor IA</span></button>`;
        }
        if (hasProfile('approvers')) {
            html += `<button data-admintab="approvals" id="admin-tab-btn-approvals" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-clipboard-check w-5 dynamic-text-accent"></i><span>Aprobaciones</span></button>`;
        }
        if (hasProfile('audit')) {
            html += `<button data-admintab="audit" id="admin-tab-btn-audit" class="admin-tab-btn w-full text-left px-3 py-2.5 rounded-xl text-sm font-medium flex items-center space-x-3 transition dynamic-card border border-transparent"><i class="fa-solid fa-clock-rotate-left w-5 dynamic-text-accent"></i><span>Auditoria</span></button>`;
        }

        if (menuContainer) {
            menuContainer.innerHTML = html;
        }
    },

    // Consolas de administración de alertas: vmalert por tenant y Alertmanager
    // global. Viven únicamente en el panel de control (specs/011 §5.4). Ambas
    // exigen el perfil 'alerts_manager', que el backend vuelve a comprobar al
    // emitir el ticket y en cada request del proxy.
    async renderAdminAlertsConfig(options = {}) {
        const autoWait = options.autoWait !== false;
        const container = document.getElementById('admin-alerts-config-list');
        if (!container) return;

        cancelAlertsConfigWait('admin-alerts');

        const profiles = state.currentUserProfiles || [];
        if (!profiles.includes('alerts_manager')) {
            container.innerHTML =
                '<p class="text-xs opacity-75">Requiere el perfil alerts_manager para administrar las consolas de alertas.</p>';
            return;
        }

        container.innerHTML = '<p class="text-xs opacity-75">Cargando consolas de alertas...</p>';

        let tenants = [];
        try {
            tenants = await api.fetchAlertsTenants({ mine: true });
        } catch (err) {
            console.error('Error al cargar tenants para Admin Alerts (conf):', err);
            container.innerHTML =
                `<p class="text-xs text-red-400">No se pudo cargar el listado de tenants: ${esc(err.message || err)}</p>`;
            return;
        }

        const withInstance = tenants.filter((t) => t.status === 'active' && t.vmalert_deployed);
        const withRules = tenants.filter((t) => t.status !== 'deleted_cleanup');
        const needsWait = tenants.some(t => t.status === 'active' && t.has_alerts && !t.vmalert_deployed);

        let html = `
            <div class="dynamic-card border rounded-xl p-4 flex justify-between items-center">
                <div>
                    <h4 class="font-bold text-sm">Alertmanager (global)</h4>
                    <p class="text-xs opacity-75">Consola unica del stack, sin tenant</p>
                </div>
                <button id="btn-open-alertmanager-global-admin" class="text-xs dynamic-card border px-2 py-1 rounded">
                    Abrir
                </button>
            </div>
        `;

        if (withInstance.length === 0 && !needsWait) {
            html += `
                <p class="text-xs opacity-75">
                    Ningun tenant tiene una instancia vmalert desplegada. Provisiona el tenant
                    para que aparezca aqui su consola.
                </p>
            `;
        }

        for (const t of withRules) {
            const deployed = t.status === 'active' && t.vmalert_deployed;
            const jobLabel = alertsTenantJobLabel(t);
            let statusLine = '';
            if (!deployed) {
                const desc = jobLabel ? `${jobLabel} — se habilitará automáticamente.` : 'Pendiente de reconciliación — se habilitará automáticamente.';
                statusLine = `
                    <p class="text-xs opacity-75 mt-1">${esc(desc)}</p>
                    <div class="w-full h-2 rounded-full overflow-hidden dynamic-card border mt-2">
                        <div class="h-full w-1/2 dynamic-accent rounded-full animate-pulse"></div>
                    </div>
                `;
            }
            html += `
                <div class="dynamic-card border rounded-xl p-4 flex justify-between items-center">
                    <div>
                        <h4 class="font-bold text-sm">${esc(t.name)}</h4>
                        <p class="text-xs opacity-75">
                            slug: ${esc(t.slug || '-')} | org: ${esc(t.org_id_upper || '-')} |
                            estado: ${esc(t.status || '-')} |
                            nodo: ${esc(t.instance_id || '-')} | puerto: ${esc(t.vmalert_port || '-')}
                        </p>
                        ${statusLine}
                    </div>
                    <div class="flex items-center space-x-2">
                        <button class="btn-vmalert-rules-admin text-xs dynamic-card border px-2 py-1 rounded"
                                data-tenant-id="${esc(t.id)}" data-tenant-slug="${esc(t.slug || '')}"
                                data-tenant-internal="${esc(t.is_internal ? '1' : '0')}">
                            Reglas
                        </button>
                        <button class="btn-alert-recipients text-xs dynamic-card border px-2 py-1 rounded"
                                data-tenant-id="${esc(t.id)}" data-tenant-name="${esc(t.name)}">
                            Destinatarios
                        </button>
                        <button class="btn-vmalert-reload-admin text-xs dynamic-card border px-2 py-1 rounded"
                                data-tenant-id="${esc(t.id)}" ${deployed ? '' : 'disabled title="Sin instancia desplegada"'}>
                            Reload
                        </button>
                        <button class="btn-open-vmalert-tenant-admin text-xs dynamic-card border px-2 py-1 rounded"
                                data-tenant-id="${esc(t.id)}" ${deployed ? '' : 'disabled title="Sin instancia desplegada"'}>
                            Abrir vmalert
                        </button>
                    </div>
                </div>
            `;
        }

        if (needsWait) {
            html += `
                <div class="flex justify-between items-center pt-2">
                    <p class="text-xs opacity-75"><i class="fa-solid fa-spinner fa-spin mr-1"></i> Esperando despliegue automático de instancias pendientes...</p>
                    <button id="btn-retry-admin-alerts-wait" class="text-xs dynamic-card border px-3 py-1.5 rounded">Reintentar</button>
                </div>
            `;
        }

        container.innerHTML = html;

        if (needsWait && autoWait) {
            startAlertsConfigWait('admin-alerts', true, () => this.renderAdminAlertsConfig({ autoWait: false }));
        }

        document.getElementById('btn-retry-admin-alerts-wait')?.addEventListener('click', () => {
            this.renderAdminAlertsConfig({ autoWait: true });
        });

        document.getElementById('btn-open-alertmanager-global-admin')?.addEventListener('click', async () => {
            try {
                const url = await api.createAlertmanagerGlobalUiTicket();
                window.open(url, '_blank');
            } catch (err) {
                console.error('Error abriendo la consola global de Alertmanager:', err);
                alert('No se pudo abrir Alertmanager: ' + (err.message || err));
            }
        });

        container.querySelectorAll('.btn-open-vmalert-tenant-admin').forEach((btn) => {
            btn.addEventListener('click', async () => {
                const tenantId = btn.getAttribute('data-tenant-id');
                try {
                    const url = await api.createVmalertUiTicket(tenantId);
                    window.open(url, '_blank');
                } catch (err) {
                    console.error('Error abriendo la consola vmalert del tenant:', err);
                    alert('No se pudo abrir vmalert: ' + (err.message || err));
                }
            });
        });

        container.querySelectorAll('.btn-alert-recipients').forEach((btn) => {
            btn.addEventListener('click', () => {
                this.openAlertRecipients(btn.getAttribute('data-tenant-id'), btn.getAttribute('data-tenant-name'));
            });
        });
    },

    // --- Alertas enriquecidas por IA (spec 018) ---

    // Índice de alertas: tarjetas expandibles con el análisis IA. Exige el perfil
    // 'access_alerts'; el scope de tenants lo aplica el backend.
    async renderAlerts() {
        const container = document.getElementById('recent-alerts-list');
        if (!container) return;

        const profiles = state.currentUserProfiles || [];
        if (!profiles.includes('access_alerts') && !profiles.includes('admin')) {
            container.innerHTML =
                '<p class="text-xs opacity-75">Requiere el perfil access_alerts para ver las alertas.</p>';
            return;
        }

        container.innerHTML = '<p class="text-xs opacity-75">Cargando alertas...</p>';

        let items = [];
        try {
            items = await api.fetchRecentAlerts();
        } catch (err) {
            console.error('Error al cargar alertas recientes:', err);
            container.innerHTML =
                `<p class="text-xs text-red-400">No se pudieron cargar las alertas: ${esc(err.message || err)}</p>`;
            return;
        }
        this._recentAlerts = items;

        if (!items.length) {
            container.innerHTML = '<p class="text-xs opacity-75">No hay alertas recientes para tus tenants.</p>';
            return;
        }

        container.innerHTML = items.map((a) => this._alertCardHtml(a)).join('');
        this._wireAlertCards(container);
    },

    // --- Historial / buscador de alertas (spec 018) ---

    async ensureAlertSearchTenants() {
        const sel = document.getElementById('alert-search-tenant');
        if (!sel || this._alertSearchTenantsLoaded) return;
        try {
            const tenants = await api.fetchUserTenants();
            const list = Array.isArray(tenants) ? tenants : (tenants.tenants || []);
            sel.innerHTML = '<option value="">Todos los tenants</option>' + list.map(t =>
                `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('');
            this._alertSearchTenantsLoaded = true;
        } catch (err) {
            sel.innerHTML = '<option value="">Todos los tenants</option>';
        }
    },

    _wireAlertSearch() {
        if (this._alertSearchWired) return;
        this._alertSearchWired = true;
        document.getElementById('btn-search-alerts')?.addEventListener('click', () => this.searchAlertHistory(true));
        document.getElementById('btn-more-alerts')?.addEventListener('click', () => this.searchAlertHistory(false));
        document.getElementById('alert-search-status')?.addEventListener('change', () => this.searchAlertHistory(true));
        document.getElementById('alert-search-tenant')?.addEventListener('change', () => this.searchAlertHistory(true));
        document.getElementById('alert-search-input')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this.searchAlertHistory(true);
            }
        });
    },

    async searchAlertHistory(reset = true) {
        const results = document.getElementById('alert-history-results');
        if (!results) return;
        if (reset) {
            this._alertHistoryItems = [];
            this._alertSearchTotal = 0;
        }
        const q = document.getElementById('alert-search-input')?.value.trim();
        const status = document.getElementById('alert-search-status')?.value || null;
        const tenantId = document.getElementById('alert-search-tenant')?.value || null;
        results.innerHTML = '<p class="text-xs opacity-75">Buscando…</p>';
        try {
            const data = await api.fetchAlertHistory({
                q: q || null,
                status,
                tenant_id: tenantId ? Number(tenantId) : null,
                limit: 20,
                offset: reset ? 0 : this._alertHistoryItems.length,
            });
            const items = data.items || [];
            this._alertSearchTotal = data.total || 0;
            this._alertHistoryItems = reset ? items : this._alertHistoryItems.concat(items);
            this._renderAlertHistoryResults();
        } catch (err) {
            console.error('Error buscando historial de alertas:', err);
            results.innerHTML =
                `<p class="text-xs text-red-400">No se pudo buscar: ${esc(err.message || err)}</p>`;
        }
    },

    _renderAlertHistoryResults() {
        const results = document.getElementById('alert-history-results');
        const total = document.getElementById('alert-history-total');
        const moreBtn = document.getElementById('btn-more-alerts');
        if (!results) return;
        const items = this._alertHistoryItems || [];
        if (!items.length) {
            results.innerHTML = '<p class="text-xs opacity-60 text-center py-4">Sin resultados.</p>';
            if (total) total.textContent = '';
            if (moreBtn) moreBtn.classList.add('hidden');
            return;
        }
        results.innerHTML = items.map((a) => this._alertCardHtml(a)).join('');
        this._wireAlertCards(results);
        const shown = items.length;
        if (total) total.textContent = `${shown} de ${this._alertSearchTotal} alertas`;
        if (moreBtn) moreBtn.classList.toggle('hidden', shown >= this._alertSearchTotal);
    },

    _severityBadge(sev) {
        const cls = {
            critical: 'bg-red-500/20 text-red-400',
            high: 'bg-orange-500/20 text-orange-400',
            medium: 'bg-amber-500/20 text-amber-400',
            low: 'bg-blue-500/20 text-blue-400',
            info: 'bg-slate-500/20 text-slate-300'
        }[sev] || 'bg-slate-500/20 text-slate-300';
        return `<span class="inline-block px-2 py-1 ${cls} text-xs font-semibold rounded uppercase">${esc(sev || 'info')}</span>`;
    },

    _alertCardHtml(a) {
        const profiles = state.currentUserProfiles || [];
        const canManage = profiles.includes('alerts_manager') || profiles.includes('admin');
        const firing = (a.status || 'firing') === 'firing';
        const statusBadge = firing
            ? '<span class="inline-block px-2 py-1 bg-red-500/20 text-red-400 text-xs font-semibold rounded">Firing</span>'
            : '<span class="inline-block px-2 py-1 bg-emerald-500/20 text-emerald-400 text-xs font-semibold rounded">Resolved</span>';
        const summary = a.ai_summary || a.summary || '(sin resumen)';
        const checks = Array.isArray(a.ai_checks) ? a.ai_checks : [];
        const checksHtml = checks.length
            ? `<ul class="list-disc list-inside space-y-1 mt-1">${checks.map(c => `<li>${esc(c)}</li>`).join('')}</ul>`
            : '<p class="opacity-60">Sin opciones de revisión generadas.</p>';
        // Fallback visible (spec 018): el análisis IA falló, pero se conservan los
        // datos originales de Alertmanager. Se marca en la tarjeta.
        const aiErrorBadge = a.ai_error
            ? '<span class="inline-block px-2 py-1 bg-amber-500/20 text-amber-400 text-xs font-semibold rounded" title="La IA no pudo analizar esta alerta; se muestran los datos originales de Alertmanager.">IA no disponible</span>'
            : '';
        // Entrega de email auditable (spec 018): si el envío a los destinatarios
        // del tenant falló o no había SMTP, se marca en la tarjeta.
        const emailBadge = a.email_status === 'failed' || a.email_status === 'not_configured'
            ? `<span class="inline-block px-2 py-1 bg-red-500/20 text-red-400 text-xs font-semibold rounded" title="${esc(a.email_error || 'No se pudo enviar el email de la alerta a los destinatarios.')}">Email no enviado</span>`
            : '';
        const rootCause = a.ai_root_cause
            ? `<p class="text-xs"><span class="font-bold">Causa probable:</span> ${esc(a.ai_root_cause)}</p>`
            : '<p class="text-xs opacity-60">Sin causa raíz estimada (análisis pendiente o IA no disponible).</p>';
        const override = a.severity_override_by
            ? `<p class="text-xs opacity-60">Criticidad ajustada por ${esc(a.severity_override_by)}</p>`
            : '';
        const sevSelect = canManage
            ? `<select class="sev-override dynamic-input border rounded-lg px-2 py-1 text-xs cursor-pointer"
                       data-alert-id="${esc(a.id)}" data-current="${esc(a.severity || 'info')}">
                   ${['critical', 'high', 'medium', 'low', 'info'].map(s =>
                       `<option value="${s}" ${s === (a.severity || 'info') ? 'selected' : ''}>${s}</option>`).join('')}
               </select>`
            : '';
        const link = a.generator_url
            ? `<a href="${esc(a.generator_url)}" target="_blank" rel="noopener" class="text-xs dynamic-text-accent underline">Ver en vmalert</a>`
            : '';
        const time = a.starts_at ? new Date(a.starts_at).toLocaleString() : '';

        return `
            <div class="dynamic-card border rounded-xl overflow-hidden" data-alert-card="${esc(a.id)}">
                <div class="p-4 flex justify-between items-start cursor-pointer alert-card-header">
                    <div class="space-y-1">
                        <div class="flex items-center space-x-2 flex-wrap gap-y-1">
                            ${this._severityBadge(a.severity)}
                            ${statusBadge}
                            ${aiErrorBadge}
                            ${emailBadge}
                            <h4 class="font-bold text-sm">${esc(a.alertname || '(sin nombre)')}</h4>
                            <span class="text-xs opacity-60 font-mono">${esc(a.tenant_name || ('tenant ' + a.tenant_id))}</span>
                        </div>
                        <p class="text-sm">${esc(summary)}</p>
                    </div>
                    <i class="fa-solid fa-chevron-down opacity-60 transition-transform alert-card-chevron"></i>
                </div>
                <div class="hidden border-t dynamic-border p-4 space-y-3 bg-black/10 alert-card-body">
                    ${rootCause}
                    <div class="text-xs"><span class="font-bold">Opciones a revisar:</span>${checksHtml}</div>
                    ${a.description ? `<p class="text-xs opacity-75">${esc(a.description)}</p>` : ''}
                    <div class="flex items-center justify-between flex-wrap gap-2 pt-1">
                        <div class="flex items-center space-x-2">
                            ${sevSelect}
                            ${override}
                        </div>
                        <div class="flex items-center space-x-3">
                            <span class="text-xs opacity-60">${esc(time)}</span>
                            ${link}
                        </div>
                    </div>
                </div>
            </div>
        `;
    },

    _wireAlertCards(container) {
        container.querySelectorAll('.alert-card-header').forEach((header) => {
            header.addEventListener('click', () => {
                const body = header.parentElement.querySelector('.alert-card-body');
                const chevron = header.querySelector('.alert-card-chevron');
                body?.classList.toggle('hidden');
                chevron?.classList.toggle('rotate-180');
            });
        });

        container.querySelectorAll('.sev-override').forEach((sel) => {
            sel.addEventListener('change', async (e) => {
                e.stopPropagation();
                const alertId = sel.getAttribute('data-alert-id');
                const severity = sel.value;
                const previous = sel.getAttribute('data-current');
                if (severity === previous) return;
                try {
                    await api.overrideAlertSeverity(alertId, severity);
                    sel.setAttribute('data-current', severity);
                } catch (err) {
                    console.error('Error al ajustar criticidad:', err);
                    alert('No se pudo actualizar la criticidad: ' + (err.message || err));
                    sel.value = previous;
                }
            });
        });
    },

    // --- Destinatarios de alertas (spec 018, alerts_manager) ---

    async openAlertRecipients(tenantId, tenantName) {
        const modal = document.getElementById('modal-alert-recipients');
        if (!modal) return;
        document.getElementById('alert-recipients-tenant-id').value = tenantId;
        document.getElementById('alert-recipients-tenant').textContent = tenantName || tenantId;
        document.getElementById('alert-recipient-email').value = '';
        document.getElementById('alert-recipient-label').value = '';
        this._recipientsTenantId = tenantId;
        this.toggleModal('modal-alert-recipients', true);

        const addBtn = document.getElementById('btn-add-alert-recipient');
        if (addBtn && !addBtn.dataset.wired) {
            addBtn.dataset.wired = '1';
            addBtn.addEventListener('click', () => this.addAlertRecipient());
        }

        await this.renderAlertRecipients();
    },

    async renderAlertRecipients() {
        const list = document.getElementById('alert-recipients-list');
        const tenantId = this._recipientsTenantId;
        if (!list || !tenantId) return;
        list.innerHTML = '<p class="text-xs opacity-75">Cargando destinatarios...</p>';
        let recipients = [];
        try {
            recipients = await api.fetchAlertRecipients(tenantId);
        } catch (err) {
            list.innerHTML = `<p class="text-xs text-red-400">No se pudieron cargar: ${esc(err.message || err)}</p>`;
            return;
        }
        if (!recipients.length) {
            list.innerHTML = '<p class="text-xs opacity-75">Sin destinatarios configurados.</p>';
            return;
        }
        list.innerHTML = recipients.map((r) => `
            <div class="dynamic-card border rounded-xl p-3 flex justify-between items-center">
                <div class="text-sm">
                    <p class="font-mono">${esc(r.email)}</p>
                    <p class="text-xs opacity-60">
                        ${esc(r.label || 'sin etiqueta')} ·
                        <span class="${r.enabled ? 'text-emerald-400' : 'text-red-400'}">${r.enabled ? 'activo' : 'deshabilitado'}</span>
                    </p>
                </div>
                <div class="flex items-center space-x-2">
                    <button class="btn-toggle-recipient text-xs dynamic-card border px-2 py-1 rounded"
                            data-recipient-id="${esc(r.id)}" data-enabled="${r.enabled ? '1' : '0'}">
                        ${r.enabled ? 'Deshabilitar' : 'Habilitar'}
                    </button>
                    <button class="btn-del-recipient text-xs text-red-400 dynamic-card border px-2 py-1 rounded"
                            data-recipient-id="${esc(r.id)}">Eliminar</button>
                </div>
            </div>
        `).join('');

        list.querySelectorAll('.btn-toggle-recipient').forEach((btn) => {
            btn.addEventListener('click', () => {
                const id = btn.getAttribute('data-recipient-id');
                const enabled = btn.getAttribute('data-enabled') !== '1';
                this.updateAlertRecipient(id, { enabled });
            });
        });
        list.querySelectorAll('.btn-del-recipient').forEach((btn) => {
            btn.addEventListener('click', () => {
                if (confirm('¿Eliminar este destinatario?')) {
                    this.deleteAlertRecipient(btn.getAttribute('data-recipient-id'));
                }
            });
        });
    },

    async addAlertRecipient() {
        const tenantId = this._recipientsTenantId;
        const email = document.getElementById('alert-recipient-email')?.value.trim();
        const label = document.getElementById('alert-recipient-label')?.value.trim();
        if (!email) { alert('Ingresa un correo.'); return; }
        try {
            await api.addAlertRecipient(tenantId, { email, label, enabled: true });
            document.getElementById('alert-recipient-email').value = '';
            document.getElementById('alert-recipient-label').value = '';
            await this.renderAlertRecipients();
        } catch (err) {
            alert('No se pudo añadir: ' + (err.message || err));
        }
    },

    async updateAlertRecipient(recipientId, payload) {
        try {
            await api.updateAlertRecipient(this._recipientsTenantId, recipientId, payload);
            await this.renderAlertRecipients();
        } catch (err) {
            alert('No se pudo actualizar: ' + (err.message || err));
        }
    },

    async deleteAlertRecipient(recipientId) {
        try {
            await api.deleteAlertRecipient(this._recipientsTenantId, recipientId);
            await this.renderAlertRecipients();
        } catch (err) {
            alert('No se pudo eliminar: ' + (err.message || err));
        }
    }
};

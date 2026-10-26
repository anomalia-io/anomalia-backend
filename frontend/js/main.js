import { api } from './api.js';
import { ui } from './ui.js';
import { admin } from './admin.js';
import { state } from './state.js';
import { initNotifications } from './notifications.js';

window.admin = admin;

// Deep-link ?tab=... : las notificaciones guardan link="/?tab=alerts" (spec 018),
// el import usa "/?tab=bkp" (spec 017) y las aprobaciones "/?tab=approvals"
// (spec 019). Sin esto, el click recargaba la página y caía en el tab por
// defecto en vez del destino de la notificación.
function applyDeepLinkTab() {
    const tab = new URLSearchParams(window.location.search).get('tab');
    if (!tab) return;
    const validMainTabs = ['metrics', 'logs', 'traces', 'profiling', 'dashboards', 'alerts'];
    if (validMainTabs.includes(tab)) {
        ui.switchTab(tab);
    } else if (tab === 'bkp') {
        ui.switchAdminTab('bkp');
        admin.loadSystemConfig();
    } else if (tab === 'approvals') {
        ui.switchAdminTab('approvals');
        admin.loadApprovals();
    }
    window.history.replaceState({}, '', window.location.pathname);
}

document.addEventListener("DOMContentLoaded", async () => {
    const token = api.getToken();
    if (!token) {
        window.location.href = "/login";
        return;
    }

    let username = localStorage.getItem("username") || "Usuario";
    let role = localStorage.getItem("role") || "viewer";
    state.currentUserRoles = JSON.parse(localStorage.getItem("roles") || '["viewer"]');
    state.currentUserProfiles = JSON.parse(localStorage.getItem("profiles") || '[]');
    let userTheme = localStorage.getItem('user_theme') || 'theme-enterprise-blue';
    let userAvatarUrl = "/media/default.png";

    try {
        const userData = await api.fetchUserData();
        username = userData.username || username;
        role = userData.role || role;
        
        if (userData.roles) {
            state.currentUserRoles = userData.roles;
            localStorage.setItem('roles', JSON.stringify(state.currentUserRoles));
        }
        if (userData.profiles) {
            state.currentUserProfiles = userData.profiles;
            localStorage.setItem('profiles', JSON.stringify(state.currentUserProfiles));
        }
        if (userData.theme) {
            userTheme = userData.theme;
            localStorage.setItem('user_theme', userTheme);
        }
        if (userData.avatar_url) {
            userAvatarUrl = userData.avatar_url;
        }
    } catch (error) {
        console.error("Error sincronizando perfil:", error);
    }

    ui.applyTheme(userTheme);
    const themePref = document.getElementById('user-theme-preference');
    if (themePref) themePref.value = userTheme;

    // Centro de notificaciones in-app (spec 017 §5). Disponible para todo
    // usuario autenticado.
    initNotifications();

    // Banner global del estado del proveedor de IA (spec 018): consulta periódica
    // para reflejar el agotamiento/recuperación de cuota de una IA cloud.
    const refreshAiStatus = async () => {
        try {
            const status = await api.fetchAiStatus();
            ui.renderAiStatusBanner(status);
        } catch (err) {
            // Silencioso: el banner es informativo, no debe romper la UI.
        }
    };
    document.getElementById('ai-status-banner-close')?.addEventListener('click', async () => {
        try {
            ui.dismissAiStatusBanner(await api.fetchAiStatus());
        } catch (err) {
            ui.dismissAiStatusBanner({});
        }
    });
    refreshAiStatus();
    setInterval(refreshAiStatus, 60000);
    
    const displayName = document.getElementById("user-display-name");
    if (displayName) displayName.innerText = username;
    
    const displayRole = document.getElementById("user-display-role");
    if (displayRole) displayRole.innerText = `Rol: ${role} (${state.currentUserRoles.join(', ')})`;
    
    const modalUsername = document.getElementById("modal-username");
    if (modalUsername) modalUsername.innerText = username;
    
    const modalRoles = document.getElementById("modal-user-inherited-roles");
    if (modalRoles) modalRoles.innerText = `Roles asignados: ${state.currentUserRoles.join(' | ')} | Perfiles: ${state.currentUserProfiles.join(' | ') || 'ninguno'}`;

    const headerAvatar = document.getElementById("user-avatar");
    const modalAvatar = document.getElementById("modal-avatar-preview");
    const defaultAvatar = "/media/default.png";

    const setupAvatarImg = (imgEl, url) => {
        if (!imgEl) return;
        imgEl.src = url;
        imgEl.onerror = () => {
            imgEl.src = defaultAvatar;
        };
    };

    setupAvatarImg(headerAvatar, userAvatarUrl);
    setupAvatarImg(modalAvatar, userAvatarUrl);

    const avatarInput = document.getElementById("input-user-avatar-file");
    if (avatarInput) {
        avatarInput.addEventListener("change", (e) => {
            const file = e.target.files[0];
            if (file) {
                const reader = new FileReader();
                reader.onload = (event) => {
                    if (headerAvatar) headerAvatar.src = event.target.result;
                    if (modalAvatar) modalAvatar.src = event.target.result;
                };
                reader.readAsDataURL(file);
            }
        });
    }

    const hasPrivilegedAccess = state.currentUserRoles.includes("admin") || 
        state.currentUserRoles.some(r => ["user_manager", "tenant_manager", "role_manager", "profile_manager", "infra_manager"].includes(r)) ||
        state.currentUserProfiles.some(p => ["users_manager", "tenants_manager", "roles_manager", "profile_manager", "infra_manager", "admin", "approvers", "audit"].includes(p));

    if (hasPrivilegedAccess) {
        const sidebarToggle = document.getElementById("admin-sidebar-toggle-container");
        if (sidebarToggle) sidebarToggle.classList.remove("hidden");
        ui.buildAdminSidebarMenu(state.currentUserRoles, state.currentUserProfiles);
        admin.loadAll();
    }

    try {
        ui.renderLoading();
        const tenants = await api.fetchUserTenants();
        ui.renderTenants(tenants, state.currentUserRoles, state.currentUserProfiles);
    } catch (err) {
        console.error("Error al cargar tenants del usuario:", err);
        alert("No se pudieron cargar los tenants autorizados: " + (err.message || err));
    }

    // Deep-link desde notificaciones (/?tab=alerts, /?tab=bkp).
    applyDeepLinkTab();

    const profileBtn = document.getElementById('profile-menu-button');
    if (profileBtn) profileBtn.addEventListener('click', () => ui.toggleModal('profile-modal'));
    
    document.getElementById('btn-toggle-sidebar')?.addEventListener('click', () => ui.toggleAdminSidebar());
    document.getElementById('btn-close-sidebar')?.addEventListener('click', () => ui.toggleAdminSidebar());
    
    const returnMain = document.getElementById('btn-return-main');
    if (returnMain) returnMain.addEventListener('click', () => ui.returnToMainView());

    document.querySelectorAll('.close-modal-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const modalId = btn.getAttribute('data-modal');
            ui.toggleModal(modalId, false);
        });
    });

    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            ui.switchTab(btn.getAttribute('data-tab'));
        });
    });

    document.addEventListener('click', (e) => {
        const admBtn = e.target.closest('.admin-tab-btn');
        if (admBtn) {
            const tabName = admBtn.getAttribute('data-admintab');
            ui.switchAdminTab(tabName);
            if (tabName === 'users') admin.loadUsers();
            if (tabName === 'tenants') admin.loadTenantsAdmin();
            if (tabName === 'roles') admin.loadRolesAdmin();
            if (tabName === 'profiles') admin.loadProfilesAdmin();
            if (tabName === 'infra') admin.loadInfraNodes();
            if (tabName === 'general') admin.loadSystemConfig();
            if (tabName === 'bkp') admin.loadSystemConfig();
            if (tabName === 'ai') admin.loadAIConfig();
            if (tabName === 'sso') admin.loadSSO();
            if (tabName === 'approvals') admin.loadApprovals?.();
            if (tabName === 'alerts') ui.renderAdminAlertsConfig?.();
        }
    });

    document.addEventListener('click', async (e) => {
        const consoleBtn = e.target.closest('.btn-open-console');
        if (!consoleBtn || consoleBtn.disabled) return;

        const tenantId = Number(consoleBtn.dataset.tenantId);
        const scope = consoleBtn.dataset.scope;
        const label = consoleBtn.dataset.label || 'la consola';
        if (!tenantId || !scope) return;

        const original = consoleBtn.textContent;
        consoleBtn.disabled = true;
        consoleBtn.textContent = 'Abriendo...';
        try {
            // Se abre la pestana antes del await: el navegador solo permite
            // window.open() dentro del gesto del usuario, y un fetch la perderia.
            const tab = window.open('about:blank', '_blank');
            const url = await api.createUiTicket(tenantId, scope);
            if (tab) tab.location = url;
            else window.open(url, '_blank');
        } catch (err) {
            alert('No se pudo abrir ' + label + ': ' + (err.message || err));
        } finally {
            consoleBtn.disabled = false;
            consoleBtn.textContent = original;
        }
    });

    const saveProfileChanges = document.getElementById('btn-save-profile-changes');
    if (saveProfileChanges) {
        saveProfileChanges.addEventListener('click', async () => {
            const selectedTheme = document.getElementById('user-theme-preference').value;
            const newPassword = document.getElementById('input-new-password').value;
            const confirmPassword = document.getElementById('input-confirm-password').value;
            const avatarFile = document.getElementById('input-user-avatar-file')?.files[0];
            
            if (newPassword && newPassword !== confirmPassword) {
                alert("Las contraseñas no coinciden.");
                return;
            }

            localStorage.setItem('user_theme', selectedTheme);
            ui.applyTheme(selectedTheme);

            try {
                const formData = new FormData();
                formData.append('theme', selectedTheme);
                if (newPassword) formData.append('password', newPassword);
                if (avatarFile) formData.append('avatar', avatarFile);

                const token = api.getToken();
                const res = await fetch('/api/v1/auth/theme', {
                    method: 'PUT',
                    headers: { 'Authorization': `Bearer ${token}` },
                    body: formData
                });

                if (res.ok) {
                    const data = await res.json();
                    alert("¡Preferencias guardadas con éxito!");
                    
                    if (data.avatar_url) {
                        const timestamp = new Date().getTime();
                        if (headerAvatar) headerAvatar.src = `${data.avatar_url}?t=${timestamp}`;
                        if (modalAvatar) modalAvatar.src = `${data.avatar_url}?t=${timestamp}`;
                    }
                    ui.toggleModal('profile-modal', false);
                } else {
                    const err = await res.json();
                    alert("Error al actualizar perfil: " + (err.detail || "Error desconocido"));
                }
            } catch (error) {
                console.error(error);
                alert("Error de red al actualizar perfil.");
            }
        });
    }

    const logoutBtn = document.getElementById('btn-logout');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', () => {
            localStorage.clear();
            sessionStorage.clear();
            window.location.href = "/login";
        });
    }

    document.getElementById('btn-open-user-modal')?.addEventListener('click', () => admin.openUserModal());
    document.getElementById('btn-open-tenant-modal')?.addEventListener('click', () => admin.openTenantModal());
    document.getElementById('btn-open-role-modal')?.addEventListener('click', () => admin.openRoleModal());
    document.getElementById('btn-open-profile-modal')?.addEventListener('click', () => admin.openProfileModal());

    document.getElementById('btn-refresh-users')?.addEventListener('click', () => admin.loadUsers());
    document.getElementById('btn-refresh-tenants')?.addEventListener('click', () => admin.loadTenantsAdmin());
    document.getElementById('btn-refresh-roles')?.addEventListener('click', () => admin.loadRolesAdmin());
    document.getElementById('btn-refresh-profiles')?.addEventListener('click', () => admin.loadProfilesAdmin());
    document.getElementById('btn-refresh-infra')?.addEventListener('click', () => admin.loadInfraNodes());

    document.getElementById('btn-save-user')?.addEventListener('click', () => admin.saveUser());
    document.getElementById('btn-save-tenant')?.addEventListener('click', () => admin.saveTenant());

    // Hard-delete de tenant en 2 pasos (specs/011 §3 y F5).
    document.getElementById('btn-tenant-delete-step1')?.addEventListener('click', () => admin.tenantDeleteStep1());
    document.getElementById('btn-tenant-delete-step2')?.addEventListener('click', () => admin.tenantDeleteStep2());

    // Editor de reglas vmalert por tenant (specs/011 F5).
    document.getElementById('btn-vmalert-rules-save')?.addEventListener('click', () => admin.saveVmalertRules(false));
    document.getElementById('btn-vmalert-rules-reload')?.addEventListener('click', () => admin.saveVmalertRules(true));
    document.getElementById('btn-save-role')?.addEventListener('click', () => admin.saveRole());
    document.getElementById('btn-save-profile')?.addEventListener('click', () => admin.saveProfile());
    document.getElementById('btn-save-infra')?.addEventListener('click', () => admin.saveInfraNode());
    document.getElementById('btn-save-ai')?.addEventListener('click', () => admin.saveAIConfig());
    document.getElementById('btn-test-ai')?.addEventListener('click', () => admin.testAIProvider());
    document.getElementById('btn-save-sso')?.addEventListener('click', () => admin.saveSSO());
    document.getElementById('btn-refresh-alerts')?.addEventListener('click', () => ui.renderAlerts());

    // Configuracion General y Backup (specs/014).
    document.getElementById('btn-save-smtp')?.addEventListener('click', () => admin.saveSmtpConfig());
    document.getElementById('btn-test-smtp')?.addEventListener('click', () => admin.testSmtpConfig());
    document.getElementById('btn-save-proxy')?.addEventListener('click', () => admin.saveProxyConfig());
    document.getElementById('btn-save-push')?.addEventListener('click', () => admin.savePushConfig());
    document.getElementById('btn-test-push')?.addEventListener('click', () => admin.testPushConfig());
    document.getElementById('push-service-account-file')?.addEventListener('change', (event) => {
        const file = event.target.files?.[0];
        if (!file) return;
        const reader = new FileReader();
        reader.onload = () => {
            const textarea = document.getElementById('push-service-account-json');
            if (textarea) textarea.value = String(reader.result || '');
        };
        reader.readAsText(file);
    });
    document.getElementById('btn-export-bkp')?.addEventListener('click', () => admin.exportBackup());
    document.getElementById('btn-import-bkp')?.addEventListener('click', () => admin.importBackup());

    // Solicitudes de acceso / aprobaciones (spec 019).
    document.getElementById('btn-open-access-request')?.addEventListener('click', () => admin.openAccessRequestModal());
    document.getElementById('btn-submit-access-request')?.addEventListener('click', () => admin.submitAccessRequest());
    document.getElementById('btn-refresh-approvals')?.addEventListener('click', () => admin.loadApprovals());

    document.addEventListener('click', (e) => {
        if (e.target.closest('.btn-edit-user')) {
            const data = JSON.parse(e.target.closest('.btn-edit-user').getAttribute('data-edit-user'));
            admin.openUserModal(data);
        }
        if (e.target.closest('.btn-delete-user')) {
            admin.deleteUser(e.target.closest('.btn-delete-user').getAttribute('data-delete-user'));
        }
        if (e.target.closest('.btn-edit-tenant')) {
            const data = JSON.parse(e.target.closest('.btn-edit-tenant').getAttribute('data-edit-tenant'));
            admin.openTenantModal(data);
        }
        if (e.target.closest('.btn-delete-tenant')) {
            // Hard-delete en 2 pasos (specs/011 §3). El borrado simple e
            // inmediato quedo obsoleto: deja vmalert, reglas y puertos huerfanos.
            admin.hardDeleteTenant(e.target.closest('.btn-delete-tenant').getAttribute('data-delete-tenant'));
        }
        // Admin Alerts (conf): editor de reglas y reload de vmalert por tenant.
        // Se wirea por delegacion porque los botones se recrean en cada
        // renderAdminAlertsConfig() (vive en el panel de control, specs/011 §5.4).
        if (e.target.closest('.btn-vmalert-rules-admin')) {
            const btn = e.target.closest('.btn-vmalert-rules-admin');
            admin.openVmalertRulesModal(
                btn.getAttribute('data-tenant-id'),
                btn.getAttribute('data-tenant-slug'),
                btn.getAttribute('data-tenant-internal') === '1'
            );
        }
        if (e.target.closest('.btn-vmalert-reload-admin')) {
            const btn = e.target.closest('.btn-vmalert-reload-admin');
            admin.reloadVmalert(btn.getAttribute('data-tenant-id'), btn);
        }
        if (e.target.closest('.btn-edit-role')) {
            const data = JSON.parse(e.target.closest('.btn-edit-role').getAttribute('data-edit-role'));
            admin.openRoleModal(data);
        }
        if (e.target.closest('.btn-delete-role')) {
            admin.deleteRole(e.target.closest('.btn-delete-role').getAttribute('data-delete-role'));
        }
        if (e.target.closest('.btn-edit-profile')) {
            const data = JSON.parse(e.target.closest('.btn-edit-profile').getAttribute('data-edit-profile'));
            admin.openProfileModal(data);
        }
        if (e.target.closest('.btn-delete-profile')) {
            admin.deleteProfile(e.target.closest('.btn-delete-profile').getAttribute('data-delete-profile'));
        }
        if (e.target.closest('.btn-edit-infra')) {
            const data = JSON.parse(e.target.closest('.btn-edit-infra').getAttribute('data-edit-infra'));
            admin.openInfraModal(data);
        }
        if (e.target.closest('.btn-update-infra')) {
            const btnEl = e.target.closest('.btn-update-infra');
            const infraId = btnEl.getAttribute('data-update-infra');
            if (infraId) {
                admin.updateInfraNodeStatus(infraId);
            }
        }
        if (e.target.closest('.btn-delete-infra')) {
            admin.deleteInfraNode(e.target.closest('.btn-delete-infra').getAttribute('data-delete-infra'));
        }
        // Aprobaciones (spec 019): botones recreados en cada loadApprovals().
        if (e.target.closest('.btn-approve-request')) {
            admin.approveRequest(e.target.closest('.btn-approve-request').getAttribute('data-approve-id'));
        }
        if (e.target.closest('.btn-reject-request')) {
            admin.rejectRequest(e.target.closest('.btn-reject-request').getAttribute('data-reject-id'));
        }
        if (e.target.closest('.btn-cancel-request')) {
            admin.cancelRequest(e.target.closest('.btn-cancel-request').getAttribute('data-cancel-id'));
        }
    });

    // ============ AI Assistant Floating Panel ============
    (function() {
        const toggleBtn = document.getElementById('ai-assistant-toggle');
        const panel = document.getElementById('ai-assistant-panel');
        const closeBtn = document.getElementById('ai-assistant-close');
        const sendBtn = document.getElementById('ai-assistant-send');
        const input = document.getElementById('ai-assistant-input');
        const messagesContainer = document.getElementById('ai-assistant-messages');
        const contextEl = document.getElementById('ai-assistant-context');
        const hintEl = document.getElementById('ai-assistant-hint');
        const contextBtn = document.getElementById('ai-assistant-context-btn');
        const badge = document.getElementById('ai-assistant-badge');

        if (!toggleBtn || !panel) return;

        let isOpen = false;
        let currentContext = 'general';
        let unreadCount = 0;

        const CONTEXTS = {
            general: { label: 'Asistente General', hint: 'Pregunta lo que necesites', aiContext: 'parses_query' },
            metrics: { label: 'VictoriaMetrics', hint: 'PromQL, métricas, dashboards', aiContext: 'victoria_metrics_query' },
            logs: { label: 'VictoriaLogs', hint: 'LogsQL, búsqueda de logs', aiContext: 'victoria_logs_query' },
            traces: { label: 'VictoriaTraces', hint: 'Búsqueda de trazas, latencia', aiContext: 'victoria_traces_query' },
            profiling: { label: 'Pyroscope', hint: 'Profiling CPU/memoria, flamegraphs', aiContext: 'pyroscope_query' },
            dashboards: { label: 'Parses/Dashboards', hint: 'Dashboards, paneles, variables', aiContext: 'parses_query' },
            alerts: { label: 'Alertas', hint: 'Alertas activas y bitácora enriquecida por IA', aiContext: 'vmalert_rules' },
        };

        let currentTenantId = null;

        function openPanel() {
            panel.classList.remove('hidden');
            panel.classList.add('flex');
            toggleBtn.classList.add('hidden');
            document.getElementById('ai-assistant-input')?.focus();
        }

        function closePanel() {
            panel.classList.add('hidden');
            panel.classList.remove('flex');
            toggleBtn.classList.remove('hidden');
        }

        function setContext(ctx) {
            currentContext = ctx;
            const c = CONTEXTS[ctx] || CONTEXTS.general;
            const contextEl = document.getElementById('ai-assistant-context');
            const hintEl = document.getElementById('ai-assistant-hint');
            if (contextEl) contextEl.textContent = c.label;
            if (hintEl) hintEl.textContent = c.hint;
        }

        function addMessage(role, text) {
            const container = document.getElementById('ai-assistant-messages');
            if (!container) return;
            const div = document.createElement('div');
            div.className = `flex ${role === 'user' ? 'justify-end' : 'justify-start'}`;
            div.innerHTML = role === 'user'
                ? `<div class="bg-primary/20 text-primary rounded-xl px-3 py-2 max-w-[85%] text-xs">${text}</div>`
                : `<div class="dynamic-card border rounded-xl px-3 py-2 max-w-[85%] text-xs">${text}</div>`;
            container.appendChild(div);
            container.scrollTop = container.scrollHeight;
        }

        async function sendMessage() {
            const input = document.getElementById('ai-assistant-input');
            const text = input?.value?.trim();
            if (!text) return;

            addMessage('user', text);
            input.value = '';
            input.disabled = true;
            const sendBtn = document.getElementById('ai-assistant-send');
            if (sendBtn) sendBtn.disabled = true;

            try {
                const response = await api.aiChat({
                    context: CONTEXTS[currentContext]?.aiContext || 'parses_query',
                    messages: [{ role: 'user', content: text }],
                    tenant_id: currentTenantId
                });
                addMessage('assistant', response.response);
            } catch (err) {
                addMessage('assistant', `Error: ${err.message || err}`);
            } finally {
                if (input) {
                    input.disabled = false;
                    input.focus();
                }
                if (sendBtn) sendBtn.disabled = false;
            }
        }

        // Toggle button from header nav only
        const openAssistant = () => {
            const panel = document.getElementById('ai-assistant-panel');
            if (panel.classList.contains('hidden')) {
                panel.classList.remove('hidden');
                panel.classList.add('flex');
                document.getElementById('ai-assistant-input')?.focus();
            } else {
                panel.classList.add('hidden');
                panel.classList.remove('flex');
            }
        };

        document.getElementById('ai-assistant-toggle-nav')?.addEventListener('click', openAssistant);

        // Close button
        document.getElementById('ai-assistant-close')?.addEventListener('click', () => {
            const panel = document.getElementById('ai-assistant-panel');
            panel.classList.add('hidden');
            panel.classList.remove('flex');
        });

        // Minimize and Maximize controls
        const minimizeBtn = document.getElementById('ai-assistant-minimize');
        const maximizeBtn = document.getElementById('ai-assistant-maximize');
        const assistantCard = document.getElementById('ai-assistant-card');
        const assistantBody = document.getElementById('ai-assistant-body');

        let isMinimized = false;
        let isMaximized = false;

        minimizeBtn?.addEventListener('click', () => {
            isMinimized = !isMinimized;
            if (isMinimized) {
                assistantBody.style.display = 'none';
                assistantCard.style.height = 'auto';
                minimizeBtn.innerHTML = '<i class="fa-solid fa-window-maximize"></i>';
            } else {
                assistantBody.style.display = 'flex';
                assistantCard.style.height = '480px';
                minimizeBtn.innerHTML = '<i class="fa-solid fa-window-minimize"></i>';
            }
        });

        maximizeBtn?.addEventListener('click', () => {
            isMaximized = !isMaximized;
            if (isMaximized) {
                assistantCard.style.width = '90vw';
                assistantCard.style.height = '90vh';
                assistantCard.style.maxWidth = '1200px';
                maximizeBtn.innerHTML = '<i class="fa-solid fa-compress"></i>';
            } else {
                assistantCard.style.width = '380px';
                assistantCard.style.height = isMinimized ? 'auto' : '480px';
                assistantCard.style.maxWidth = 'none';
                maximizeBtn.innerHTML = '<i class="fa-solid fa-expand"></i>';
            }
        });

        // Send button
        document.getElementById('ai-assistant-send')?.addEventListener('click', sendMessage);

        // Enter key
        document.getElementById('ai-assistant-input')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });

        // Context switcher (simplified - could be expanded with dropdown)
        document.getElementById('ai-assistant-context-btn')?.addEventListener('click', () => {
            const contexts = Object.keys(CONTEXTS);
            const currentIdx = contexts.indexOf(currentContext);
            const nextIdx = (currentIdx + 1) % contexts.length;
            setContext(contexts[nextIdx]);
        });

        // Enter key to send
        document.getElementById('ai-assistant-input')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        });

        // Listen for tenant selection to update context
        document.addEventListener('tenant-selected', (e) => {
            currentTenantId = e.detail.tenantId;
            const badge = document.getElementById('ai-assistant-badge');
            if (badge) {
                badge.textContent = '●';
                badge.classList.remove('hidden');
            }
        });

        // Listen for console tab changes to auto-switch context
        document.addEventListener('tab-changed', (e) => {
            const tab = e.detail.tab;
            if (CONTEXTS[tab]) {
                setContext(tab);
            }
        });

        // Initialize
        setContext('general');

        // Always show toggle button for all authenticated users
        const toggleBtnEl = document.getElementById('ai-assistant-toggle');
        if (toggleBtnEl) toggleBtnEl.style.display = 'flex';

        // ============ GitBook Documentation Modal & Toggles ============
        const gitbookToggleNav = document.getElementById('gitbook-toggle-nav');
        const gitbookModal = document.getElementById('gitbook-modal');
        const gitbookClose = document.getElementById('gitbook-modal-close');

        gitbookToggleNav?.addEventListener('click', () => {
            if (gitbookModal) {
                gitbookModal.classList.remove('hidden');
                gitbookModal.classList.add('flex');
            }
        });

        gitbookClose?.addEventListener('click', () => {
            gitbookModal?.classList.add('hidden');
            gitbookModal?.classList.remove('flex');
        });

        gitbookModal?.addEventListener('click', (e) => {
            if (e.target === gitbookModal) {
                gitbookModal.classList.add('hidden');
                gitbookModal.classList.remove('flex');
            }
        });
    })();
    // ============ End AI Assistant ============
});

export const api = {
    getToken() {
        return localStorage.getItem("access_token") || sessionStorage.getItem("access_token");
    },

    async fetchUserData() {
        const token = this.getToken();
        const response = await fetch('/api/v1/auth/user', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (response.ok) return await response.json();
        throw new Error("No se pudo obtener el usuario");
    },

    async fetchCatalogs() {
        const token = this.getToken();
        const [rolesRes, tenantsRes, profilesRes] = await Promise.all([
            fetch('/api/v1/admin/roles', { headers: { 'Authorization': `Bearer ${token}` } }),
            fetch('/api/v1/admin/tenants', { headers: { 'Authorization': `Bearer ${token}` } }),
            fetch('/api/v1/admin/profiles', { headers: { 'Authorization': `Bearer ${token}` } })
        ]);
        return {
            roles: rolesRes.ok ? (await rolesRes.json()).roles || [] : [],
            tenants: tenantsRes.ok ? (await tenantsRes.json()).tenants || [] : [],
            profiles: profilesRes.ok ? (await profilesRes.json()).profiles || [] : []
        };
    },

    async fetchUserTenants() {
        const token = this.getToken();
        const res = await fetch('/api/v1/tenants/my-tenants', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (res.ok) {
            const data = await res.json();
            return Array.isArray(data) ? data : (data.tenants || []);
        }
        return [];
    },

    async createUiTicket(tenantId, scope) {
        const token = this.getToken();
        const res = await fetch('/api/v1/ui/ticket', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({ tenant_id: tenantId, scope })
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo abrir la consola de telemetría');
        }
        const data = await res.json();
        if (!data.url) throw new Error('El backend no devolvió una URL de consola');
        return data.url;
    },

    async updateThemeAndPass(theme, password) {
        const token = this.getToken();
        return await fetch('/api/v1/auth/theme', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({ theme, password: password || undefined })
        });
    },

    async getAIProviders() {
        const token = this.getToken();
        const res = await fetch('/api/v1/ai/providers', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo obtener el catálogo de proveedores IA');
        return await res.json();
    },

    async getAIConfig() {
        const token = this.getToken();
        const res = await fetch('/api/v1/ai/config', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo obtener la configuración de IA');
        return await res.json();
    },

    async saveAIConfig(payload) {
        const token = this.getToken();
        const res = await fetch('/api/v1/ai/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo guardar la configuración de IA');
        }
        return await res.json();
    },

    async testAIProvider(provider) {
        const token = this.getToken();
        const res = await fetch('/api/v1/ai/test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(provider ? { provider } : {})
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo probar el proveedor IA');
        }
        return await res.json();
    },

    async getGeminiModels() {
        const token = this.getToken();
        const res = await fetch('/api/v1/ai/gemini-models', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo obtener los modelos de Gemini');
        return await res.json();
    },

    // --- Consolas de administración de alertas (specs/011 §5.7) ---

    // Ticket de la consola vmalert del tenant. La instancia la resuelve el
    // backend por tenant; el frontend no envía node ni puerto.
    async createVmalertUiTicket(tenantId) {
        return this.createUiTicket(tenantId, 'vmalert');
    },

    // Ticket de la consola global de Alertmanager. Es la única consola sin
    // tenant, así que no se manda tenant_id.
    async createAlertmanagerGlobalUiTicket() {
        const token = this.getToken();
        const res = await fetch('/api/v1/ui/ticket', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({ scope: 'alertmanager_global' })
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo abrir la consola de Alertmanager');
        }
        const data = await res.json();
        if (!data.url) throw new Error('El backend no devolvió una URL de consola');
        return data.url;
    },

    // --- Ciclo de vida del tenant (specs/011 §3) ---

    async fetchAdminTenants() {
        const token = this.getToken();
        const res = await fetch('/api/v1/admin/tenants', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo obtener el listado de tenants');
        const data = await res.json();
        return data.tenants || [];
    },

    async fetchVmalertNodes() {
        const token = this.getToken();
        const res = await fetch('/api/v1/admin/infra/vmalert-nodes', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo obtener nodos vmalert');
        return res.json();
    },

    async fetchVmauthNodes() {
        const token = this.getToken();
        const res = await fetch('/api/v1/admin/infra/vmauth-nodes', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo obtener nodos vmauth');
        return res.json();
    },

    // Listado minimo para la tarjeta Alerts (conf). Gated por alerts_manager, no
    // por tenants_manager: quien administra alertas no tiene por que administrar
    // tenants, y este endpoint no expone account_id ni project_id.
    async fetchAlertsTenants(options = {}) {
        const token = this.getToken();
        const params = new URLSearchParams();
        if (options.mine) params.set('mine', 'true');
        if (options.waitDeployed) params.set('wait_deployed', 'true');
        const res = await fetch(`/api/v1/admin/alerts/tenants?${params.toString()}`, {
            headers: { 'Authorization': `Bearer ${token}` },
            signal: options.signal
        });
        if (!res.ok) throw new Error('No se pudo obtener el listado de tenants de alertas');
        const data = await res.json();
        return data.tenants || [];
    },

    // Paso 1 del hard-delete: genera el challenge de un solo uso (TTL 10 min).
    async deleteTenantConfirm1(tenantId, reason) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/tenants/${tenantId}/delete/confirm1`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({ reason: reason || null })
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo iniciar la eliminación del tenant');
        }
        return await res.json();
    },

    // Paso 2: confirm_text debe ser EXACTAMENTE el slug del tenant.
    async deleteTenantConfirm2(tenantId, challengeId, confirmText) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/tenants/${tenantId}/delete/confirm2`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({ challenge_id: challengeId, confirm_text: confirmText })
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo confirmar la eliminación del tenant');
        }
        return await res.json();
    },

    async fetchJob(jobId) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/jobs/${jobId}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) {
            const err = new Error('No se pudo consultar el estado del job');
            err.status = res.status;
            throw err;
        }
        return await res.json();
    },

    // Lista jobs recientes (spec 016 §2.6): permite reanudar el seguimiento de un
    // import en curso tras recargar la página.
    async fetchJobs({ type = null, status = null, limit = 20 } = {}) {
        const token = this.getToken();
        const params = new URLSearchParams();
        if (type) params.set('type', type);
        if (status) params.set('status', status);
        if (limit) params.set('limit', limit);
        const res = await fetch(`/api/v1/admin/jobs?${params.toString()}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo listar los jobs');
        return await res.json();
    },

    // Notificaciones del usuario (spec 017).
    async fetchNotifications({ unread = false, limit = 50 } = {}) {
        const token = this.getToken();
        const params = new URLSearchParams();
        if (unread) params.set('unread', 'true');
        params.set('limit', limit);
        const res = await fetch(`/api/v1/notifications?${params.toString()}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) {
            const err = new Error('No se pudieron cargar las notificaciones');
            err.status = res.status;
            throw err;
        }
        return await res.json();
    },

    async markNotificationRead(id) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/notifications/${id}/read`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo marcar la notificación');
        return await res.json();
    },

    async markAllNotificationsRead() {
        const token = this.getToken();
        const res = await fetch('/api/v1/notifications/read-all', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudieron marcar las notificaciones');
        return await res.json();
    },

    // Estado global del proveedor de IA (spec 018): alimenta el banner cuando una
    // IA cloud agota su cuota de tokens.
    async fetchAiStatus() {
        const token = this.getToken();
        const res = await fetch('/api/v1/ai/status', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo obtener el estado del proveedor de IA');
        return await res.json();
    },

    // --- Alertas enriquecidas por IA (spec 018) ---

    // Alertas recientes visibles para el usuario (access_alerts + acceso al
    // tenant). Alimenta la tarjeta expandible del index.
    async fetchRecentAlerts(limit = 50) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/alerts/recent?limit=${limit}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudieron cargar las alertas');
        const data = await res.json();
        return data.items || [];
    },

    // Historial con filtros y paginación.
    async fetchAlertHistory({ tenant_id = null, status = null, limit = 50, offset = 0, q = null } = {}) {
        const token = this.getToken();
        const params = new URLSearchParams();
        if (tenant_id) params.set('tenant_id', tenant_id);
        if (status) params.set('status', status);
        if (q) params.set('q', q);
        params.set('limit', limit);
        params.set('offset', offset);
        const res = await fetch(`/api/v1/alerts/history?${params.toString()}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo cargar el historial de alertas');
        return await res.json();
    },

    // Override manual de criticidad (perfil alerts_manager).
    async overrideAlertSeverity(alertId, severity) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/alerts/${alertId}/severity`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({ severity })
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo actualizar la criticidad');
        }
        return await res.json();
    },

    // --- Destinatarios de alertas por tenant (spec 018, alerts_manager) ---

    async fetchAlertRecipients(tenantId) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/alerts/tenants/${tenantId}/recipients`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudieron cargar los destinatarios');
        const data = await res.json();
        return data.recipients || [];
    },

    async addAlertRecipient(tenantId, payload) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/alerts/tenants/${tenantId}/recipients`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo añadir el destinatario');
        }
        return await res.json();
    },

    async updateAlertRecipient(tenantId, recipientId, payload) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/alerts/tenants/${tenantId}/recipients/${recipientId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo actualizar el destinatario');
        }
        return await res.json();
    },

    async deleteAlertRecipient(tenantId, recipientId) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/alerts/tenants/${tenantId}/recipients/${recipientId}`, {
            method: 'DELETE',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo eliminar el destinatario');
        }
        return await res.json();
    },

    async getTenantVmalertRules(tenantId) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/tenants/${tenantId}/vmalert/rules`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudieron leer las reglas del tenant');
        return await res.json();
    },

async saveTenantVmalertRules(tenantId, yamlText) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/tenants/${tenantId}/vmalert/rules`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({ yaml: yamlText })
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudieron guardar las reglas');
        }
        return await res.json();
    },

    async aiChat(payload) {
        const token = this.getToken();
        const res = await fetch('/api/v1/ai/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'Error en chat IA');
        }
        return await res.json();
    },

    // Operaciones largas: devuelven 202 + job_id. El polling lo hace state.js.
    async tenantVmalertAction(tenantId, action) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/admin/tenants/${tenantId}/vmalert/${action}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({})
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || `No se pudo ejecutar '${action}'`);
        }
        return await res.json();
    },

    // --- Configuracion General: SMTP, Proxy y Backup (specs/014) ---

    async getSystemConfig() {
        const token = this.getToken();
        const res = await fetch('/api/v1/system/config', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo obtener la configuración del sistema');
        return await res.json();
    },

    async getAuthSettings() {
        const res = await fetch('/api/v1/settings/auth');
        if (!res.ok) throw new Error('No se pudo obtener la configuración de autenticación');
        return await res.json();
    },

    async getSSOConfig() {
        const token = this.getToken();
        const res = await fetch('/api/v1/settings/sso', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo obtener la configuración SSO');
        }
        return await res.json();
    },

    async saveSSOConfig(payload) {
        const token = this.getToken();
        const res = await fetch('/api/v1/settings/sso', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo guardar la configuración SSO');
        }
        return await res.json();
    },

    async saveSmtpConfig(payload) {
        const token = this.getToken();
        const res = await fetch('/api/v1/system/smtp', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo guardar la configuración SMTP');
        }
        return await res.json();
    },

    async testSmtpConfig(to) {
        const token = this.getToken();
        const res = await fetch('/api/v1/system/smtp-test', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({ to })
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo enviar el email de prueba');
        }
        return await res.json();
    },

    async saveProxyConfig(payload) {
        const token = this.getToken();
        const res = await fetch('/api/v1/system/proxy', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo guardar la configuración de proxy');
        }
        return await res.json();
    },

    async exportBackup(password) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/system/backup/export?password=${encodeURIComponent(password)}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo exportar el backup');
        }
        return await res.blob();
    },

    async importBackup(file, password) {
        const token = this.getToken();
        const formData = new FormData();
        formData.append('file', file);
        formData.append('password', password);
        const res = await fetch('/api/v1/system/backup/import', {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` },
            body: formData
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo importar el backup');
        }
        return await res.json();
    },

    // --- Solicitudes de acceso / aprobaciones (spec 019) ---

    async fetchAccessCatalog() {
        const token = this.getToken();
        const res = await fetch('/api/v1/approvals/catalog', {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudo obtener el catálogo de accesos');
        return await res.json();
    },

    async fetchApprovals(status = null) {
        const token = this.getToken();
        const qs = status ? `?status_filter=${encodeURIComponent(status)}` : '';
        const res = await fetch(`/api/v1/approvals/requests${qs}`, {
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) throw new Error('No se pudieron cargar las solicitudes');
        return await res.json();
    },

    async createAccessRequest(payload) {
        const token = this.getToken();
        const res = await fetch('/api/v1/approvals/requests', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify(payload)
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo enviar la solicitud');
        }
        return await res.json();
    },

    async approveAccessRequest(id) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/approvals/requests/${id}/approve`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo aprobar la solicitud');
        }
        return await res.json();
    },

    async rejectAccessRequest(id, reviewNote) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/approvals/requests/${id}/reject`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
            body: JSON.stringify({ review_note: reviewNote || null })
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo rechazar la solicitud');
        }
        return await res.json();
    },

    async cancelAccessRequest(id) {
        const token = this.getToken();
        const res = await fetch(`/api/v1/approvals/requests/${id}/cancel`, {
            method: 'POST',
            headers: { 'Authorization': `Bearer ${token}` }
        });
        if (!res.ok) {
            let detail = '';
            try { detail = (await res.json()).detail || ''; } catch (_) { /* sin cuerpo */ }
            throw new Error(detail || 'No se pudo cancelar la solicitud');
        }
        return await res.json();
    }
};

import { api } from './api.js';
import { state, pollJob } from './state.js';
import { ui } from './ui.js';

const escHtml = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

export const admin = {
    async loadAll() {
        await Promise.all([
            this.loadUsers(),
            this.loadTenantsAdmin(),
            this.loadRolesAdmin(),
            this.loadProfilesAdmin(),
            this.loadInfraNodes()
        ]);
        // Reanuda el seguimiento de un import en curso (spec 016 §2.6).
        this.resumeBackupImport();
    },

    async loadUsers() {
        const token = api.getToken();
        const tbody = document.getElementById("users-table-body");
        if (!tbody) return;
        tbody.innerHTML = `<tr><td colspan="6" class="py-4 text-center opacity-75">Actualizando usuarios...</td></tr>`;
        try {
            const res = await fetch('/api/v1/admin/users', { headers: { 'Authorization': `Bearer ${token}` } });
            if (res.ok) {
                const data = await res.json();
                const users = data.users || [];
                tbody.innerHTML = users.length ? users.map(u => {
                    const safeUserJson = JSON.stringify(u).replace(/"/g, '&quot;');
                    return `
                    <tr class="border-b dynamic-border hover:opacity-90">
                        <td class="py-3 px-4 font-mono">${u.id}</td>
                        <td class="py-3 px-4 font-semibold">${u.username}</td>
                        <td class="py-3 px-4"><span class="px-2 py-1 rounded text-xs font-mono dynamic-accent">${(u.roles || []).join(', ') || 'ninguno'}</span></td>
                        <td class="py-3 px-4"><span class="text-xs opacity-75">${(u.tenants || []).join(', ') || 'ninguno'}</span></td>
                        <td class="py-3 px-4"><span class="text-xs ${u.is_active ? 'text-emerald-400' : 'text-amber-400'} font-mono">${u.is_active ? '🟢 Activo' : '🔴 Inactivo'}</span></td>
                        <td class="py-3 px-4 text-right space-x-2">
                            <button data-edit-user='${safeUserJson}' class="btn-edit-user text-xs dynamic-card border px-2 py-1 rounded">Editar</button>
                            <button data-delete-user="${u.id}" class="btn-delete-user text-xs bg-red-500/20 text-red-400 border border-red-500/30 px-2 py-1 rounded">Eliminar</button>
                        </td>
                    </tr>
                `;
                }).join('') : `<tr><td colspan="6" class="py-4 text-center opacity-75">No hay usuarios registrados.</td></tr>`;
            } else {
                tbody.innerHTML = `<tr><td colspan="6" class="py-4 text-center text-red-400 text-xs">Error al obtener usuarios del servidor.</td></tr>`;
            }
        } catch (err) {
            console.error(err);
            tbody.innerHTML = `<tr><td colspan="6" class="py-4 text-center text-red-400 text-xs">Error de red cargando usuarios.</td></tr>`;
        }
    },

    async openUserModal(user = null) {
        const catalogs = await api.fetchCatalogs();
        state.globalRolesCache = catalogs.roles || [];
        state.globalTenantsCache = catalogs.tenants || [];

        const userIdEl = document.getElementById('user-id');
        if (userIdEl) userIdEl.value = user ? user.id : '';
        const usernameEl = document.getElementById('user-input-username');
        if (usernameEl) usernameEl.value = user ? user.username : '';
        const passwordEl = document.getElementById('user-input-password');
        if (passwordEl) passwordEl.value = '';
        const passwordConfirmEl = document.getElementById('user-input-password-confirm');
        if (passwordConfirmEl) passwordConfirmEl.value = '';
        const activeEl = document.getElementById('user-input-active');
        if (activeEl) activeEl.checked = user ? user.is_active : true;
        const modalTitleEl = document.getElementById('modal-user-title');
        if (modalTitleEl) modalTitleEl.innerText = user ? 'Editar Usuario' : 'Crear Usuario';

        const assignedRoles = user && user.roles ? user.roles : [];
        const rolesContainer = document.getElementById('user-roles-checkboxes');
        if (rolesContainer) {
            rolesContainer.innerHTML = state.globalRolesCache.map(r => `
                <label class="flex items-center space-x-2 text-sm cursor-pointer py-1 px-2 hover:bg-black/10 rounded">
                    <input type="checkbox" name="user_roles_cb" value="${r.name}" ${assignedRoles.includes(r.name) ? 'checked' : ''} class="rounded dynamic-input">
                    <span>${r.name} <span class="text-xs opacity-60">(${r.description || ''})</span></span>
                </label>
            `).join('');
        }

        const assignedTenants = user && user.tenants ? user.tenants : [];
        const tenantsContainer = document.getElementById('user-tenants-checkboxes');
        if (tenantsContainer) {
            tenantsContainer.innerHTML = state.globalTenantsCache.map(t => `
                <label class="flex items-center space-x-2 text-sm cursor-pointer py-1 px-2 hover:bg-black/10 rounded">
                    <input type="checkbox" name="user_tenants_cb" value="${t.name}" ${assignedTenants.includes(t.name) ? 'checked' : ''} class="rounded dynamic-input">
                    <span>${t.name} <span class="text-xs opacity-60">(${t.type})</span></span>
                </label>
            `).join('');
        }

        ui.toggleModal('modal-user', true);
    },

    async saveUser() {
        const id = document.getElementById('user-id')?.value;
        const username = document.getElementById('user-input-username')?.value?.trim();
        const password = document.getElementById('user-input-password')?.value;
        const passwordConfirm = document.getElementById('user-input-password-confirm')?.value;

        const EMAIL_REGEX = /^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$/;
        if (!username || !EMAIL_REGEX.test(username)) {
            alert("Error: Formato de email invalido.");
            return;
        }

        if (password || passwordConfirm) {
            if (password !== passwordConfirm) {
                alert("Error: Las contraseñas no coinciden.");
                return;
            }
        }

        const roles = Array.from(document.querySelectorAll('input[name="user_roles_cb"]:checked')).map(cb => cb.value);
        const tenants = Array.from(document.querySelectorAll('input[name="user_tenants_cb"]:checked')).map(cb => cb.value);
        const is_active = document.getElementById('user-input-active')?.checked ?? true;
        const token = api.getToken();

        const method = id ? 'PUT' : 'POST';
        const url = id ? `/api/v1/admin/users/${id}` : '/api/v1/admin/users';
        const payload = { username, roles, tenants, is_active };
        if (password) payload.password = password;

        try {
            const res = await fetch(url, {
                method,
                headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                ui.toggleModal('modal-user', false);
                this.loadUsers();
            } else {
                const err = await res.json();
                alert("Error: " + (err.detail || "No se pudo guardar el usuario."));
            }
        } catch (e) {
            console.error(e);
            alert("Error de red al intentar guardar el usuario.");
        }
    },

    async deleteUser(id) {
        if (!confirm("¿Estás seguro de eliminar este usuario?")) return;
        const token = api.getToken();
        try {
            const res = await fetch(`/api/v1/admin/users/${id}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${token}` } });
            if (res.ok) this.loadUsers();
            else alert("Error al eliminar usuario.");
        } catch (e) {
            console.error(e);
            alert("Error de red al eliminar usuario.");
        }
    },

    async loadTenantsAdmin() {
        const token = api.getToken();
        const container = document.getElementById("admin-tenants-container");
        if (!container) return;
        container.innerHTML = `<p class="text-xs opacity-75 col-span-2">Actualizando tenants...</p>`;
        try {
            const res = await fetch('/api/v1/admin/tenants', { headers: { 'Authorization': `Bearer ${token}` } });
            if (res.ok) {
                const data = await res.json();
                const tenants = data.tenants || [];
                container.innerHTML = tenants.length ? tenants.map(t => {
                    const safeTenantJson = JSON.stringify(t).replace(/"/g, '&quot;');
                    const vmalertInfo = t.type === 'profiles'
                        ? 'no aplica'
                        : (t.vmalert_node_id
                            ? `nodo ${t.vmalert_node_id} · ${t.placement_mode || 'manual'}`
                            : `${t.placement_mode || 'manual'} (sin nodo)`);
                    const vmauthInfo = t.vmauth_node_id
                        ? `nodo ${t.vmauth_node_id} · ${t.vmauth_placement_mode || 'manual'}`
                        : `${t.vmauth_placement_mode || 'manual'} (sin nodo)`;
                    const ingestUrl = t.vmauth_ingest_url
                        ? `<div class="flex items-center gap-2 mt-2">
                             <p class="text-xs font-mono opacity-90">Ingesta: <span class="text-cyan-300 break-all">${t.vmauth_ingest_url}</span></p>
                             <button onclick="navigator.clipboard.writeText('${t.vmauth_ingest_url}').then(()=>alert('URL copiada'))" class="text-xs dynamic-card border px-2 py-1 rounded opacity-75 hover:opacity-100">Copiar</button>
                           </div>`
                        : '';
                    return `
                    <div class="dynamic-card border rounded-xl p-4 flex justify-between items-center">
                        <div>
                            <h4 class="font-bold text-sm">${t.name} <span class="text-emerald-400 text-xs font-mono ml-2">🟢 Operativo</span></h4>
                            <p class="text-xs opacity-75">Tipo: ${t.type} | Entorno: ${t.environment}</p>
                            <p class="text-xs opacity-75">vmalert: ${vmalertInfo} | vmauth: ${vmauthInfo} | alertas: ${t.has_alerts ? 'sí' : 'no'}</p>
                            <p class="text-xs opacity-60 mt-1">${t.description || 'Sin descripción'}</p>
                            ${ingestUrl}
                        </div>
                        <div class="flex items-center space-x-2">
                            <span class="text-xs dynamic-card px-2 py-1 rounded border font-mono">ID: ${t.id}</span>
                            <button data-edit-tenant='${safeTenantJson}' class="btn-edit-tenant text-xs dynamic-card border px-2 py-1 rounded">Editar</button>
                            <button data-delete-tenant="${t.id}" class="btn-delete-tenant text-xs bg-red-500/20 text-red-400 border border-red-500/30 px-2 py-1 rounded">Eliminar</button>
                        </div>
                    </div>
                `;
                }).join('') : `<p class="text-xs opacity-75 col-span-2">No hay tenants en la base de datos.</p>`;
            } else {
                container.innerHTML = `<p class="text-xs text-red-400 col-span-2">Error al obtener tenants del servidor.</p>`;
            }
        } catch (err) {
            console.error(err);
            container.innerHTML = `<p class="text-xs text-red-400 col-span-2">Error cargando tenants.</p>`;
        }
    },

    async openTenantModal(t = null) {
        const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
        setVal('tenant-id', t ? t.id : '');
        setVal('tenant-input-name', t ? t.name : '');
        setVal('tenant-input-type', t ? t.type : 'metrics');
        setVal('tenant-input-env', t ? t.environment : 'default');
        setVal('tenant-input-desc', t && t.description ? t.description : '');
        setVal('tenant-input-placement', t ? (t.placement_mode || 'manual') : 'manual');
        setVal('tenant-input-vmauth-placement', t ? (t.vmauth_placement_mode || 'manual') : 'manual');

        const nodeSelect = document.getElementById('tenant-input-vmalert-node');
        const placementSelect = document.getElementById('tenant-input-placement');
        const vmauthNodeSelect = document.getElementById('tenant-input-vmauth-node');
        const vmauthPlacementSelect = document.getElementById('tenant-input-vmauth-placement');

        // Utilidad compartida: llena un <select> de nodos con las opciones dadas.
        const fillNodes = (select, nodesData, currentId, emptyMsg) => {
            if (!select) return;
            select.innerHTML = '<option value="">-- Seleccionar nodo --</option>';
            (nodesData.nodes || []).forEach(n => {
                const opt = document.createElement('option');
                opt.value = n.id;
                opt.textContent = `${n.hostname} (${n.service_ip || n.ip_address}:${n.port}) - ${n.available_slots}/${n.capacity_slots} slots`;
                if (n.available_slots <= 0) opt.disabled = true;
                select.appendChild(opt);
            });
            if (!(nodesData.nodes || []).length) {
                select.innerHTML = `<option value="">${emptyMsg}</option>`;
            }
            if (currentId) select.value = currentId;
        };

        const wirePlacement = (select, placement, node) => {
            const update = () => { if (node) node.disabled = placement?.value !== 'manual'; };
            placement?.addEventListener('change', update);
            update();
        };

        if (nodeSelect) {
            nodeSelect.disabled = true;
            nodeSelect.innerHTML = '<option value="">Cargando nodos...</option>';
        }
        if (vmauthNodeSelect) {
            vmauthNodeSelect.disabled = true;
            vmauthNodeSelect.innerHTML = '<option value="">Cargando nodos...</option>';
        }

        try {
            const [vmalertData, vmauthData] = await Promise.all([
                api.fetchVmalertNodes(),
                api.fetchVmauthNodes()
            ]);
            fillNodes(nodeSelect, vmalertData, t ? t.vmalert_node_id : '', 'Sin nodos vmalert disponibles');
            fillNodes(vmauthNodeSelect, vmauthData, t ? t.vmauth_node_id : '', 'Sin nodos vmauth disponibles');
        } catch (e) {
            console.error('Error cargando nodos de infraestructura:', e);
            if (nodeSelect) nodeSelect.innerHTML = '<option value="">Error cargando nodos</option>';
            if (vmauthNodeSelect) vmauthNodeSelect.innerHTML = '<option value="">Error cargando nodos</option>';
        }

        wirePlacement(placementSelect, placementSelect, nodeSelect);
        wirePlacement(vmauthPlacementSelect, vmauthPlacementSelect, vmauthNodeSelect);

        const titleEl = document.getElementById('modal-tenant-title');
        if (titleEl) titleEl.innerText = t ? 'Editar Tenant' : 'Crear Tenant';

        // Reiniciar el panel de progreso (solo aplica a altas nuevas).
        const progress = document.getElementById('tenant-progress');
        const progressError = document.getElementById('tenant-progress-error');
        const bar = document.getElementById('tenant-progress-bar');
        const pct = document.getElementById('tenant-progress-pct');
        const phase = document.getElementById('tenant-progress-phase');
        if (progress) progress.classList.add('hidden');
        if (progressError) progressError.classList.add('hidden');
        if (bar) bar.style.width = '0%';
        if (pct) pct.textContent = '0%';
        if (phase) phase.textContent = 'Iniciando...';
        const saveBtn = document.getElementById('btn-save-tenant');
        if (saveBtn) saveBtn.disabled = false;

        ui.toggleModal('modal-tenant', true);
    },

    // Etapas del pipeline de alta (tenant_worker._tenant_create_pipeline) para la
    // barra de progreso del modal (specs/011 §4.9).
    TENANT_CREATE_PHASES: {
        init: 'Preparando alta...',
        assigning_ids: 'Asignando account/project id...',
        seeding_datasources: 'Sembrando datasources...',
        auto_placement: 'Ubicando en nodos de infraestructura...',
        deploying_vmalert: 'Desplegando vmalert...',
        publishing_routes: 'Publicando rutas (vmauth/vmagent)...',
        provisioning_parses: 'Creando project y dashboards en Parses...',
        done: 'Finalizando...'
    },

    // Etapas del pipeline de baja (tenant_worker._tenant_delete_sync) para la
    // barra de progreso del modal de hard-delete (specs/011 §4.11).
    TENANT_DELETE_PHASES: {
        init: 'Preparando eliminación...',
        cleanup_start: 'Iniciando eliminación...',
        undeploying_vmalert: 'Deteniendo instancia vmalert...',
        removing_scrapes: 'Retirando scraping (vmagent)...',
        deleting_parses: 'Borrando project y dashboards en Parses...',
        removing_vmauth: 'Retirando ruta en vmauth...',
        cleaning_db: 'Eliminando datos y auditoría...',
        done: 'Finalizado.'
    },

    async saveTenant() {
        const id = document.getElementById('tenant-id')?.value;
        const name = document.getElementById('tenant-input-name')?.value;
        const type = document.getElementById('tenant-input-type')?.value;
        const environment = document.getElementById('tenant-input-env')?.value;
        const description = document.getElementById('tenant-input-desc')?.value;
        const placement_mode = document.getElementById('tenant-input-placement')?.value;
        const vmalert_node_id = document.getElementById('tenant-input-vmalert-node')?.value ?
            parseInt(document.getElementById('tenant-input-vmalert-node')?.value) : null;
        const vmauth_placement_mode = document.getElementById('tenant-input-vmauth-placement')?.value;
        const vmauth_node_id = document.getElementById('tenant-input-vmauth-node')?.value ?
            parseInt(document.getElementById('tenant-input-vmauth-node')?.value) : null;
        const token = api.getToken();

        const method = id ? 'PUT' : 'POST';
        const url = id ? `/api/v1/admin/tenants/${id}` : '/api/v1/admin/tenants';

        const saveBtn = document.getElementById('btn-save-tenant');
        try {
            const res = await fetch(url, {
                method,
                headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
                body: JSON.stringify({
                    name, type, environment, description,
                    placement_mode, vmalert_node_id,
                    vmauth_placement_mode, vmauth_node_id
                })
            });

            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                alert("Error: " + (err.detail || "No se pudo guardar el tenant."));
                return;
            }

            const data = await res.json().catch(() => ({}));

            // Edición: no hay job que seguir.
            if (id) {
                ui.toggleModal('modal-tenant', false);
                this.loadTenantsAdmin();
                return;
            }

            // Alta nueva: el backend devuelve job_id y el trabajo corre en background.
            const jobId = data.job_id;
            if (!jobId) {
                ui.toggleModal('modal-tenant', false);
                this.loadTenantsAdmin();
                return;
            }

            const progress = document.getElementById('tenant-progress');
            const progressError = document.getElementById('tenant-progress-error');
            const bar = document.getElementById('tenant-progress-bar');
            const pct = document.getElementById('tenant-progress-pct');
            const phase = document.getElementById('tenant-progress-phase');
            if (progress) progress.classList.remove('hidden');
            if (progressError) progressError.classList.add('hidden');
            if (saveBtn) {
                saveBtn.disabled = true;
                saveBtn.textContent = 'Creando...';
            }

            state.trackJob(jobId, 'tenant_create');
            const job = await pollJob(jobId, (j) => {
                const p = Math.max(0, Math.min(100, j.progress_pct ?? 0));
                if (bar) bar.style.width = `${p}%`;
                if (pct) pct.textContent = `${p}%`;
                if (phase) phase.textContent = this.TENANT_CREATE_PHASES[j.phase] || j.phase || 'Procesando...';
            });

            if (job.status === 'succeeded') {
                ui.toggleModal('modal-tenant', false);
                await ui.refreshUserTenants();
            } else {
                const detail = job.error_code ? ` (${job.error_code})` : '';
                if (progressError) {
                    progressError.textContent = `El alta falló y se revirtió${detail}.`;
                    progressError.classList.remove('hidden');
                }
            }
        } catch (e) {
            console.error(e);
            alert("Error de red al guardar tenant.");
        } finally {
            if (saveBtn) {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Guardar';
            }
            this.loadTenantsAdmin();
        }
    },

    // Hard-delete explícito en 2 pasos (specs/011 §3):
    //   Paso 1 -> el backend genera un challenge_id de un solo uso (TTL 10 min).
    //   Paso 2 -> hay que escribir el slug EXACTO del tenant; el backend lo
    //             compara con tenants.slug y devuelve 202 + job_id.
    // El borrado real es asíncrono, así que se hace polling del job.
    // Hard-delete explicito en 2 pasos dentro de un modal (specs/011 §3 y F5):
    //   Paso 1 -> el backend genera un challenge_id de un solo uso (TTL 10 min).
    //   Paso 2 -> hay que escribir el slug EXACTO del tenant; responde 202 + job_id.
    // El borrado real es asincrono, asi que se hace polling del job.
    async hardDeleteTenant(id) {
        const tenantId = document.getElementById('tenant-delete-id');
        const elName = document.getElementById('tenant-delete-name');
        const elSlug = document.getElementById('tenant-delete-slug');
        const elReason = document.getElementById('tenant-delete-reason');
        const elConfirm = document.getElementById('tenant-delete-confirm');
        const elChallenge = document.getElementById('tenant-delete-challenge');
        const step1 = document.getElementById('tenant-delete-step1');
        const step2 = document.getElementById('tenant-delete-step2');
        if (!tenantId || !step1 || !step2) return;

        let tenant;
        try {
            tenant = (await api.fetchAdminTenants()).find((t) => String(t.id) === String(id));
        } catch (err) {
            console.error(err);
            alert("No se pudo leer el tenant: " + (err.message || err));
            return;
        }
        if (!tenant) {
            alert("El tenant ya no existe.");
            this.loadTenantsAdmin();
            return;
        }
        if (!tenant.slug) {
            alert("Este tenant no tiene slug, asi que no se puede hard-deletar desde la UI.");
            return;
        }

        tenantId.value = tenant.id;
        elName.textContent = tenant.name;
        elSlug.textContent = tenant.slug;
        elReason.value = `Hard-delete desde la UI (${tenant.name})`;
        elConfirm.value = '';
        elChallenge.value = '';
        step1.classList.remove('hidden');
        step2.classList.add('hidden');
        ui.toggleModal('modal-tenant-delete', true);
    },

    async tenantDeleteStep1() {
        const id = document.getElementById('tenant-delete-id')?.value;
        const reason = document.getElementById('tenant-delete-reason')?.value;
        if (!id) return;

        const btn = document.getElementById('btn-tenant-delete-step1');
        btn.disabled = true;
        btn.textContent = 'Generando challenge...';
        try {
            const ch = await api.deleteTenantConfirm1(id, reason);
            document.getElementById('tenant-delete-challenge').value = ch.challenge_id;
            document.getElementById('tenant-delete-challenge-label').textContent = ch.challenge_id;
            document.getElementById('tenant-delete-step1').classList.add('hidden');
            document.getElementById('tenant-delete-step2').classList.remove('hidden');
            document.getElementById('tenant-delete-confirm')?.focus();
        } catch (err) {
            console.error(err);
            alert("Paso 1 fallido: " + (err.message || err));
        } finally {
            btn.disabled = false;
            btn.textContent = 'Paso 1 · Generar challenge';
        }
    },

    async tenantDeleteStep2() {
        const id = document.getElementById('tenant-delete-id')?.value;
        const challengeId = document.getElementById('tenant-delete-challenge')?.value;
        const slug = document.getElementById('tenant-delete-slug')?.textContent.trim();
        const typed = document.getElementById('tenant-delete-confirm')?.value ?? '';
        if (!id || !challengeId) return;

        if (typed !== slug) {
            alert("El texto escrito no coincide con el slug del tenant. No se ha eliminado nada.");
            return;
        }

        const btn = document.getElementById('btn-tenant-delete-step2');
        btn.disabled = true;
        btn.textContent = 'Enviando...';
        let accepted;
        try {
            accepted = await api.deleteTenantConfirm2(id, challengeId, typed);
        } catch (err) {
            console.error(err);
            alert("Paso 2 fallido: " + (err.message || err));
            btn.disabled = false;
            btn.textContent = 'Paso 2 · Eliminar definitivamente';
            return;
        }

        const jobId = accepted.job_id;
        if (!jobId) {
            alert("El backend no devolvió un job_id.");
            ui.toggleModal('modal-tenant-delete', false);
            this.loadTenantsAdmin();
            return;
        }
        state.trackJob(jobId, 'tenant_delete');

        // Progreso del borrado secuencial (specs/011 §4.11): se ocultan los pasos y
        // se muestra la barra mapeando job.phase -> texto.
        const progress = document.getElementById('tenant-delete-progress');
        const progressError = document.getElementById('tenant-delete-progress-error');
        const bar = document.getElementById('tenant-delete-progress-bar');
        const pct = document.getElementById('tenant-delete-progress-pct');
        const phase = document.getElementById('tenant-delete-progress-phase');
        document.getElementById('tenant-delete-step1')?.classList.add('hidden');
        document.getElementById('tenant-delete-step2')?.classList.add('hidden');
        if (progress) progress.classList.remove('hidden');
        if (progressError) progressError.classList.add('hidden');
        btn.textContent = 'Eliminando...';

        let job;
        try {
            job = await pollJob(jobId, (j) => {
                const p = Math.max(0, Math.min(100, j.progress_pct ?? 0));
                if (bar) bar.style.width = `${p}%`;
                if (pct) pct.textContent = `${p}%`;
                if (phase) phase.textContent = this.TENANT_DELETE_PHASES[j.phase] || j.phase || 'Procesando...';
            });
        } catch (err) {
            console.error(err);
            if (progressError) {
                progressError.textContent = "No se pudo seguir el job de eliminación: " + (err.message || err);
                progressError.classList.remove('hidden');
            }
        }

        if (job && job.status === 'succeeded') {
            ui.toggleModal('modal-tenant-delete', false);
            await ui.refreshUserTenants();
        } else if (job && progressError) {
            const detail = job.error_code ? ` (${job.error_code})` : '';
            progressError.textContent = `La eliminación falló${detail}. El tenant no se purgó.`;
            progressError.classList.remove('hidden');
        }

        btn.disabled = false;
        btn.textContent = 'Paso 2 · Eliminar definitivamente';
        this.loadTenantsAdmin();
    },

    // --- Editor de reglas vmalert por tenant (specs/011 F5) ---

    async openVmalertRulesModal(id, slug, isInternal) {
        const tenantId = document.getElementById('vmalert-rules-tenant-id');
        const area = document.getElementById('vmalert-rules-yaml');
        const status = document.getElementById('vmalert-rules-status');
        const saveBtn = document.getElementById('btn-vmalert-rules-save');
        const reloadBtn = document.getElementById('btn-vmalert-rules-reload');
        const badge = document.getElementById('vmalert-rules-internal-badge');
        if (!tenantId || !area) return;

        tenantId.value = id;
        document.getElementById('vmalert-rules-tenant').textContent = slug || id;
        document.getElementById('vmalert-rules-path').textContent =
            `/etc/anomalia/vmalert/${slug}/alert_rules.yml`;

        // spec 011 §4.7: los tenants de control interno exponen las reglas en
        // solo-lectura; la API además rechaza el PUT (409). Al abre un tenant
        // regular se restauran los controles por si el modal se reutiliza.
        area.readOnly = !!isInternal;
        saveBtn.disabled = !!isInternal;
        reloadBtn.disabled = !!isInternal;
        badge.classList.toggle('hidden', !isInternal);
        badge.classList.toggle('flex', !!isInternal);

        status.textContent = isInternal
            ? 'Tenant de control interno: las reglas son canónicas (internal_rules) e inmutables por API.'
            : 'Cargando reglas...';
        area.value = '';
        ui.toggleModal('modal-vmalert-rules', true);

        // Initialize AI Chat for vmalert rules
        this.initVmalertAIChat(id);

        try {
            const data = await api.getTenantVmalertRules(id);
            area.value = data.yaml || '';
            status.textContent = data.yaml
                ? 'Reglas cargadas. Recuerda validar el YAML antes de recargar.'
                : 'El tenant todavia no tiene archivo de reglas.';
        } catch (err) {
            console.error(err);
            status.textContent = `No se pudieron cargar las reglas: ${err.message || err}`;
        }
    },

    initVmalertAIChat(tenantId) {
        const messagesContainer = document.getElementById('ai-chat-messages');
        const input = document.getElementById('ai-chat-input');
        const sendBtn = document.getElementById('btn-ai-chat-send');
        const closeBtn = document.getElementById('btn-ai-chat-close');
        
        if (!messagesContainer || !input || !sendBtn) return;

        // Clear previous messages
        messagesContainer.innerHTML = '';
        
        // Add welcome message
        this.addAIMessage(messagesContainer,
            '¡Hola! Soy tu asistente para reglas vmalert/PromQL. '
            + 'Puedo ayudarte con sintaxis PromQL, funciones de agregación, '
            + 'expresiones de alerta, labels/annotations, y buenas prácticas. '
            + '¿En qué te ayudo?');

        const sendMessage = async () => {
            const text = input.value.trim();
            if (!text) return;
            
            input.value = '';
            input.disabled = true;
            sendBtn.disabled = true;
            
            this.addUserMessage(messagesContainer, text);
            
            try {
                const response = await api.aiChat({
                    context: 'vmalert_rules',
                    messages: [
                        { role: 'user', content: text }
                    ],
                    tenant_id: tenantId
                });
                this.addAIMessage(messagesContainer, response.response);
            } catch (err) {
                this.addAIMessage(messagesContainer, `Error: ${err.message || err}`);
            } finally {
                input.disabled = false;
                sendBtn.disabled = false;
                input.focus();
            }
        };

        sendBtn.onclick = sendMessage;
        input.onkeydown = (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
        };
        
        closeBtn.onclick = () => {
            const chatPanel = document.querySelector('.md\\:w-1\\/3');
            if (chatPanel) chatPanel.style.display = 'none';
        };
    },

    addUserMessage(container, text) {
        const div = document.createElement('div');
        div.className = 'flex justify-end';
        div.innerHTML = `
            <div class="bg-primary/20 text-primary rounded-xl px-3 py-2 max-w-[85%] text-xs">
                ${text}
            </div>
        `;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    },

    addAIMessage(container, text) {
        const div = document.createElement('div');
        div.className = 'flex justify-start';
        div.innerHTML = `
            <div class="dynamic-card border rounded-xl px-3 py-2 max-w-[85%] text-xs">
                ${text}
            </div>
        `;
        container.appendChild(div);
        container.scrollTop = container.scrollHeight;
    },

    async saveVmalertRules(andReload) {
        const id = document.getElementById('vmalert-rules-tenant-id')?.value;
        const area = document.getElementById('vmalert-rules-yaml');
        const status = document.getElementById('vmalert-rules-status');
        if (!id || !area) return;

        const btn = andReload
            ? document.getElementById('btn-vmalert-rules-reload')
            : document.getElementById('btn-vmalert-rules-save');
        const original = btn.textContent;
        btn.disabled = true;

        try {
            btn.textContent = 'Guardando...';
            status.textContent = 'Guardando reglas...';
            await api.saveTenantVmalertRules(id, area.value);
            status.textContent = 'Reglas guardadas en la base de datos.';

            if (!andReload) return;

            status.textContent = 'Aplicando reglas en el nodo vmalert...';
            const job = await this.runVmalertSync(id, (j) => {
                status.textContent = `Sincronización en curso · fase ${j.phase || '-'} · ${j.progress_pct || 0}%`;
            });
            status.textContent = job.status === 'succeeded'
                ? 'Reglas aplicadas y vmalert recargado.'
                : `Sincronización finalizada con estado '${job.status}'` +
                  (job.error_code ? ` (${job.error_code})` : '') + ".";
        } catch (err) {
            console.error(err);
            status.textContent = `Error: ${err.message || err}`;
        } finally {
            btn.disabled = false;
            btn.textContent = original;
        }
    },

    // Materializa las reglas guardadas en la BD hacia el nodo y recarga vmalert
    // (job vmalert_sync_rules, specs 011/012).
    async runVmalertSync(tenantId, onProgress) {
        const accepted = await api.tenantVmalertAction(tenantId, 'sync_rules');
        const jobId = accepted.job_id;
        if (!jobId) throw new Error('El backend no devolvió un job_id para la sincronización.');
        state.trackJob(jobId, 'vmalert_sync_rules');
        return pollJob(jobId, onProgress);
    },

    // Recarga de vmalert por polling del job (specs/011 §5.3). Lo comparten el
    // boton "Guardar y recargar" del editor y el boton "Reload" de la tarjeta.
    async runVmalertReload(tenantId, onProgress) {
        const accepted = await api.tenantVmalertAction(tenantId, 'reload');
        const jobId = accepted.job_id;
        if (!jobId) throw new Error('El backend no devolvió un job_id para el reload.');
        state.trackJob(jobId, 'vmalert_reload');
        return pollJob(jobId, onProgress);
    },

    // Boton "Reload" de la tarjeta Alerts (conf).
    async reloadVmalert(tenantId, btn) {
        if (!tenantId || !btn) return;
        const original = btn.textContent;
        btn.disabled = true;
        btn.textContent = 'Recargando...';
        try {
            const job = await this.runVmalertReload(tenantId, (j) => {
                btn.textContent = `Recargando · ${j.progress_pct || 0}%`;
            });
            alert(job.status === 'succeeded'
                ? 'vmalert recargado correctamente.'
                : `Reload finalizado con estado '${job.status}'` +
                  (job.error_code ? ` (${job.error_code})` : '') + '.');
        } catch (err) {
            console.error('Error recargando vmalert:', err);
            alert('No se pudo recargar vmalert: ' + (err.message || err));
        } finally {
            btn.disabled = false;
            btn.textContent = original;
        }
    },

    async loadRolesAdmin() {
        const token = api.getToken();
        const tbody = document.getElementById("roles-table-body");
        if (!tbody) return;
        tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center opacity-75">Actualizando roles...</td></tr>`;
        try {
            const res = await fetch('/api/v1/admin/roles', { headers: { 'Authorization': `Bearer ${token}` } });
            if (res.ok) {
                const data = await res.json();
                const roles = data.roles || [];
                tbody.innerHTML = roles.length ? roles.map(r => {
                    const safeRoleJson = JSON.stringify(r).replace(/"/g, '&quot;');
                    return `
                    <tr class="border-b dynamic-border">
                        <td class="py-3 px-4 font-mono">${r.id}</td>
                        <td class="py-3 px-4 font-semibold">${r.name}</td>
                        <td class="py-3 px-4 opacity-75">${r.description || 'Sin descripción'}</td>
                        <td class="py-3 px-4">
                            <div class="text-xs space-y-1">
                                <div><strong>Tenants:</strong> ${(r.tenants || []).join(', ') || 'ninguno'}</div>
                                <div><strong>Perfiles:</strong> <span class="bg-indigo-500/20 text-indigo-300 px-2 py-0.5 rounded">${(r.profiles || []).join(', ') || 'ninguno'}</span></div>
                            </div>
                        </td>
                        <td class="py-3 px-4 text-right space-x-2">
                            <button data-edit-role='${safeRoleJson}' class="btn-edit-role text-xs dynamic-card border px-2 py-1 rounded">Editar</button>
                            <button data-delete-role="${r.id}" class="btn-delete-role text-xs bg-red-500/20 text-red-400 border border-red-500/30 px-2 py-1 rounded">Eliminar</button>
                        </td>
                    </tr>
                `;
                }).join('') : `<tr><td colspan="5" class="py-4 text-center opacity-75">No hay roles registrados.</td></tr>`;
            } else {
                tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-red-400 text-xs">Error al obtener roles del servidor.</td></tr>`;
            }
        } catch (err) {
            console.error(err);
            tbody.innerHTML = `<tr><td colspan="5" class="py-4 text-center text-red-400 text-xs">Error cargando roles.</td></tr>`;
        }
    },

    async openRoleModal(r = null) {
        const catalogs = await api.fetchCatalogs();
        state.globalTenantsCache = catalogs.tenants || [];
        state.globalProfilesCache = catalogs.profiles || [];

        const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
        setVal('role-id', r ? r.id : '');
        setVal('role-input-name', r ? r.name : '');
        setVal('role-input-desc', r && r.description ? r.description : '');
        const titleEl = document.getElementById('modal-role-title');
        if (titleEl) titleEl.innerText = r ? 'Editar Rol' : 'Crear Rol';

        const assignedTenants = r && r.tenants ? r.tenants : [];
        const tenantsContainer = document.getElementById('role-tenants-checkboxes');
        if (tenantsContainer) {
            tenantsContainer.innerHTML = state.globalTenantsCache.map(t => `
                <label class="flex items-center space-x-2 text-sm cursor-pointer py-1 px-2 hover:bg-black/10 rounded">
                    <input type="checkbox" name="role_tenants_cb" value="${t.name}" ${assignedTenants.includes(t.name) ? 'checked' : ''} class="rounded dynamic-input">
                    <span>${t.name} <span class="text-xs opacity-60">(${t.type})</span></span>
                </label>
            `).join('');
        }

        const assignedProfiles = r && r.profiles ? r.profiles.map(p => typeof p === 'object' ? (p.code || p.name) : p) : [];
        const profilesContainer = document.getElementById('role-profiles-checkboxes');
        if (profilesContainer) {
            profilesContainer.innerHTML = state.globalProfilesCache.map(p => {
                const pValue = p.code || p.name;
                const isChecked = assignedProfiles.some(ap => 
                    String(ap).toLowerCase() === String(pValue).toLowerCase() ||
                    String(ap).toLowerCase() === String(p.name).toLowerCase()
                );

                return `
                <label class="flex items-center space-x-2 text-sm cursor-pointer py-1 px-2 hover:bg-black/10 rounded">
                    <input type="checkbox" name="role_profiles_cb" value="${pValue}" ${isChecked ? 'checked' : ''} class="rounded dynamic-input">
                    <span>${p.name} <span class="text-xs opacity-60">(${p.description || ''})</span></span>
                </label>
            `;
            }).join('');
        }

        ui.toggleModal('modal-role', true);
    },

    async saveRole() {
        const id = document.getElementById('role-id')?.value;
        const name = document.getElementById('role-input-name')?.value;
        const description = document.getElementById('role-input-desc')?.value;
        const tenants = Array.from(document.querySelectorAll('input[name="role_tenants_cb"]:checked')).map(cb => cb.value);
        const profiles = Array.from(document.querySelectorAll('input[name="role_profiles_cb"]:checked')).map(cb => cb.value);
        const token = api.getToken();

        const method = id ? 'PUT' : 'POST';
        const url = id ? `/api/v1/admin/roles/${id}` : '/api/v1/admin/roles';

        try {
            const res = await fetch(url, {
                method,
                headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
                body: JSON.stringify({ name, description, tenants, profiles })
            });

            if (res.ok) {
                ui.toggleModal('modal-role', false);
                this.loadRolesAdmin();
            } else {
                const err = await res.json();
                alert("Error: " + (err.detail || "No se pudo guardar el rol."));
            }
        } catch (e) {
            console.error(e);
            alert("Error de red al guardar el rol.");
        }
    },

    async deleteRole(id) {
        if (!confirm("¿Estás seguro de eliminar este rol?")) return;
        const token = api.getToken();
        try {
            const res = await fetch(`/api/v1/admin/roles/${id}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${token}` } });
            if (res.ok) this.loadRolesAdmin();
            else alert("Error al eliminar rol.");
        } catch (e) {
            console.error(e);
            alert("Error de red al eliminar rol.");
        }
    },

    async loadProfilesAdmin() {
        const token = api.getToken();
        const container = document.getElementById("admin-profiles-container");
        if (!container) return;
        container.innerHTML = `<p class="text-xs opacity-75 col-span-3">Actualizando perfiles...</p>`;
        try {
            const res = await fetch('/api/v1/admin/profiles', { headers: { 'Authorization': `Bearer ${token}` } });
            if (res.ok) {
                const data = await res.json();
                const profiles = data.profiles || [];
                container.innerHTML = profiles.length ? profiles.map(p => {
                    const safeProfileJson = JSON.stringify(p).replace(/"/g, '&quot;');
                    return `
                    <div class="dynamic-card border rounded-2xl p-5 flex flex-col justify-between shadow-lg">
                        <div>
                            <div class="flex justify-between items-start mb-2">
                                <h4 class="font-bold text-base">${p.name}</h4>
                                <span class="text-xs dynamic-card px-2 py-0.5 rounded border font-mono">ID: ${p.id}</span>
                            </div>
                            <div class="mb-3">
                                <span class="text-xs dynamic-accent px-2 py-0.5 rounded font-mono">CODE: ${p.code}</span>
                            </div>
                            <p class="text-xs opacity-75 mb-4">${p.description || 'Sin descripción'}</p>
                        </div>
                        <div class="flex justify-end space-x-2 pt-3 border-t dynamic-border">
                            <button data-edit-profile='${safeProfileJson}' class="btn-edit-profile text-xs dynamic-card border px-3 py-1 rounded">Editar</button>
                            <button data-delete-profile="${p.id}" class="btn-delete-profile text-xs bg-red-500/20 text-red-400 border border-red-500/30 px-3 py-1 rounded">Eliminar</button>
                        </div>
                    </div>
                `;
                }).join('') : `<p class="text-xs opacity-75 col-span-3">No hay perfiles registrados.</p>`;
            } else {
                container.innerHTML = `<p class="text-xs text-red-400 col-span-3">Error al obtener perfiles del servidor.</p>`;
            }
        } catch (err) {
            console.error(err);
            container.innerHTML = `<p class="text-xs text-red-400 col-span-3">Error cargando perfiles.</p>`;
        }
    },

    openProfileModal(p = null) {
        const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
        setVal('profile-id', p ? p.id : '');
        setVal('profile-input-name', p ? p.name : '');
        setVal('profile-input-code', p ? p.code : '');
        setVal('profile-input-desc', p && p.description ? p.description : '');
        const titleEl = document.getElementById('modal-profile-title');
        if (titleEl) titleEl.innerText = p ? 'Editar Perfil' : 'Crear Perfil';
        ui.toggleModal('modal-profile', true);
    },

    async saveProfile() {
        const id = document.getElementById('profile-id')?.value;
        const name = document.getElementById('profile-input-name')?.value;
        const code = document.getElementById('profile-input-code')?.value;
        const description = document.getElementById('profile-input-desc')?.value;
        const token = api.getToken();

        const method = id ? 'PUT' : 'POST';
        const url = id ? `/api/v1/admin/profiles/${id}` : '/api/v1/admin/profiles';

        try {
            const res = await fetch(url, {
                method,
                headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
                body: JSON.stringify({ code, name, description })
            });

            if (res.ok) {
                ui.toggleModal('modal-profile', false);
                this.loadProfilesAdmin();
            } else {
                const err = await res.json();
                alert("Error: " + (err.detail || "No se pudo guardar el perfil."));
            }
        } catch (e) {
            console.error(e);
            alert("Error de red al guardar el perfil.");
        }
    },

    async deleteProfile(id) {
        if (!confirm("¿Estás seguro de eliminar este perfil?")) return;
        const token = api.getToken();
        try {
            const res = await fetch(`/api/v1/admin/profiles/${id}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${token}` } });
            if (res.ok) this.loadProfilesAdmin();
            else alert("Error al eliminar perfil.");
        } catch (e) {
            console.error(e);
            alert("Error de red al eliminar perfil.");
        }
    },

    async loadAIConfig() {
        const select = document.getElementById('ai-provider-select');
        if (!select) return;
        try {
            const [cfg, catalog] = await Promise.all([api.getAIConfig(), api.getAIProviders()]);
            this._aiCfg = cfg;
            const providers = catalog.providers || [];
            const groupLabels = { free: 'Free (OpenCode Zen)', local: 'Local', apikey: 'API Key' };
            const byGroup = {};
            const groups = [];
            providers.forEach(p => {
                if (!byGroup[p.group]) { byGroup[p.group] = []; groups.push(p.group); }
                byGroup[p.group].push(p);
            });
            select.innerHTML = groups.map(g =>
                `<optgroup label="${groupLabels[g] || g}">` +
                byGroup[g].map(p => `<option value="${p.id}">${p.label}</option>`).join('') +
                `</optgroup>`
            ).join('');
            select.value = cfg.provider || 'anomalia_ollama';
            select.onchange = () => this._refreshAIPanel();
            this._refreshAIPanel();
            this._populateAnalysisLanguages(cfg);
        } catch (err) {
            console.error(err);
        }
    },

    _populateAnalysisLanguages(cfg) {
        const langSelect = document.getElementById('ai-analysis-language');
        if (!langSelect) return;
        const langs = cfg.analysis_languages || { en: 'English', es: 'Spanish' };
        langSelect.innerHTML = Object.entries(langs)
            .map(([value, label]) => `<option value="${value}">${label}</option>`)
            .join('');
        langSelect.value = cfg.analysis_language || 'en';
    },

    _refreshAIPanel() {
        const select = document.getElementById('ai-provider-select');
        const cfg = this._aiCfg || {};
        const providerId = select?.value || cfg.provider || 'anomalia_ollama';
        const group = providerId === 'anomalia_ollama' ? 'local' : providerId === 'gemini' ? 'apikey' : 'free';
        const zenField = document.getElementById('ai-zen-api-key');
        const geminiKeyField = document.getElementById('ai-gemini-key-field');
        const geminiModelField = document.getElementById('ai-gemini-model-field');
        if (zenField) {
            document.getElementById('ai-zen-key-field').classList.toggle('hidden', group !== 'free');
            zenField.placeholder = cfg.has_zen_key
                ? 'Guardada — deja vacío para conservarla'
                : 'Se guarda en la base de datos';
        }
        if (geminiKeyField) geminiKeyField.classList.toggle('hidden', group !== 'apikey');
        if (geminiModelField) geminiModelField.classList.toggle('hidden', group !== 'apikey');
        const status = document.getElementById('ai-provider-status');
        if (status) {
            if (group === 'free') {
                status.innerHTML = cfg.has_zen_key
                    ? '<span class="text-emerald-400">● API Key Zen configurada</span>'
                    : '<span class="text-amber-400">● Sin API Key Zen: ingrésala para usar los modelos Free</span>';
            } else if (group === 'apikey') {
                status.innerHTML = cfg.has_gemini_key
                    ? '<span class="text-emerald-400">● API Key Gemini configurada</span>'
                    : '<span class="text-amber-400">● Sin API Key Gemini: ingrésala para usar este proveedor</span>';
            } else {
                status.innerHTML = '<span class="text-emerald-400">● Proveedor local disponible</span>';
            }
        }
        if (group === 'apikey') this.loadGeminiModels();
    },

    async loadGeminiModels() {
        const modelSelect = document.getElementById('ai-gemini-model');
        if (!modelSelect) return;
        try {
            const data = await api.getGeminiModels();
            const models = data.models || [];
            modelSelect.innerHTML = models.map(m => `<option value="${m}">${m}</option>`).join('');
            if (data.current) modelSelect.value = data.current;
        } catch (err) {
            console.error(err);
        }
    },

    async saveAIConfig() {
        const select = document.getElementById('ai-provider-select');
        if (!select) return;
        const payload = { provider: select.value };
        const zenKey = document.getElementById('ai-zen-api-key')?.value;
        const geminiKey = document.getElementById('ai-gemini-api-key')?.value;
        const geminiModel = document.getElementById('ai-gemini-model')?.value;
        const analysisLanguage = document.getElementById('ai-analysis-language')?.value;
        if (zenKey) payload.zen_api_key = zenKey;
        if (geminiKey) payload.gemini_api_key = geminiKey;
        if (geminiModel) payload.gemini_model = geminiModel;
        if (analysisLanguage) payload.analysis_language = analysisLanguage;
        try {
            await api.saveAIConfig(payload);
            alert('Configuración de IA guardada correctamente.');
            await this.loadAIConfig();
        } catch (e) {
            alert('Error: ' + (e.message || 'No se pudo guardar la configuración.'));
        }
    },

    async testAIProvider() {
        const select = document.getElementById('ai-provider-select');
        const resultBox = document.getElementById('ai-test-result');
        if (!select || !resultBox) return;
        resultBox.classList.remove('hidden');
        resultBox.innerHTML = '<span class="opacity-75">Probando conexión...</span>';
        try {
            const data = await api.testAIProvider(select.value);
            if (data.ok) {
                resultBox.innerHTML = `<span class="text-emerald-400">● Conexión OK (${data.provider}):</span> ${data.response || '(respuesta vacía)'}`;
            } else {
                resultBox.innerHTML = `<span class="text-red-400">● Error (${data.provider}):</span> ${data.error || 'Error desconocido'}`;
            }
        } catch (e) {
            resultBox.innerHTML = `<span class="text-red-400">● Error de red:</span> ${e.message || e}`;
        }
    },

    async loadInfraNodes() {
        const token = api.getToken();
        const container = document.getElementById("infra-nodes-container");
        if (!container) return;
        container.innerHTML = `<p class="text-xs opacity-75 col-span-2">Cargando servidores...</p>`;
        try {
            const res = await fetch('/api/v1/admin/infra', { headers: { 'Authorization': `Bearer ${token}` } });
            if (res.ok) {
                const data = await res.json();
                const nodes = data.nodes || [];
                container.innerHTML = nodes.length ? nodes.map(n => {
                    const safeNodeJson = JSON.stringify(n).replace(/"/g, '&quot;');
                    const hostName = n.hostname || n.host || n.name || 'Sin Host';
                    const ipAddress = n.ip_address || '--';
                    const serviceIp = n.service_ip || '--';
                    const nodeType = n.component_type || n.type || '--';
                    const canUpdate = n.updatable !== false;
                    const updateBtn = canUpdate
                        ? `<button data-update-infra='${n.id}' class="btn-update-infra text-xs dynamic-accent px-2 py-1 rounded">Update</button>`
                        : `<button disabled title="install-binaries.yml no actualiza '${nodeType}'" class="text-xs px-2 py-1 rounded opacity-40 cursor-not-allowed border">Update</button>`;
                    return `
                    <div class="dynamic-card border rounded-xl p-4 flex justify-between items-center" id="infra-node-card-${n.id}">
                        <div>
                            <h4 class="font-bold text-sm">${hostName} <span class="text-emerald-400 text-xs font-mono ml-2">🟢 ${n.status || 'Online'}</span></h4>
                            <p class="text-xs opacity-75">${ipAddress} | ${serviceIp} | ${nodeType}</p>
                        </div>
                        <div class="flex items-center space-x-2" id="infra-actions-${n.id}">
                            <button data-edit-infra='${safeNodeJson}' class="btn-edit-infra text-xs dynamic-card border px-2 py-1 rounded">Editar</button>
                            ${updateBtn}
                            <button data-delete-infra="${n.id}" class="btn-delete-infra text-xs bg-red-500/20 text-red-400 border border-red-500/30 px-2 py-1 rounded">Eliminar</button>
                        </div>
                    </div>
                `;
                }).join('') : `<p class="text-xs opacity-75 col-span-2">No hay nodos de infraestructura.</p>`;
            } else {
                container.innerHTML = `<p class="text-xs text-red-400 col-span-2">Error al obtener nodos.</p>`;
            }
        } catch (e) {
            console.error(e);
            container.innerHTML = `<p class="text-xs text-red-400 col-span-2">Error de red cargando infraestructura.</p>`;
        }
    },

    async openInfraModal(n = null) {
        const setVal = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
        setVal('infra-id', n ? n.id : '');
        setVal('infra-input-name', n ? (n.hostname || n.host || n.name || '') : '');
        setVal('infra-input-ip', n ? n.ip_address : '');
        setVal('infra-input-service-ip', n ? n.service_ip : '');
        setVal('infra-input-type', n ? (n.component_type || n.type || '') : '');

        try {
            const res = await fetch('/api/v1/admin/infra/component-types', {
                headers: { 'Authorization': `Bearer ${api.getToken()}` }
            });
            if (res.ok) {
                const data = await res.json();
                const select = document.getElementById('infra-input-type');
                select.innerHTML = '<option value="">Seleccionar...</option>' +
                    data.component_types.map(t => `<option value="${t.value}">${t.label}</option>`).join('');
                if (n && n.component_type) {
                    select.value = n.component_type;
                }
            }
        } catch (e) {
            console.error(e);
        }

        ui.toggleModal('modal-infra', true);
    },

    async saveInfraNode() {
        const id = document.getElementById('infra-id')?.value;
        const hostname = document.getElementById('infra-input-name')?.value;
        const ip_address = document.getElementById('infra-input-ip')?.value;
        const service_ip = document.getElementById('infra-input-service-ip')?.value;
        const component_type = document.getElementById('infra-input-type')?.value;
        const token = api.getToken();

        if (!id) {
            alert("Error: no se identificó el nodo a editar.");
            return;
        }
        if (!hostname || !ip_address || !component_type) {
            alert("Error: Hostname, IP y Tipo son obligatorios.");
            return;
        }

        const body = { hostname, ip_address, service_ip, component_type };

        try {
            const res = await fetch(`/api/v1/admin/infra/${id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
                body: JSON.stringify(body)
            });

            if (res.ok) {
                ui.toggleModal('modal-infra', false);
                this.loadInfraNodes();
            } else {
                const err = await res.json();
                alert(err.detail || "No se pudo guardar el nodo.");
            }
        } catch (e) {
            console.error(e);
            alert("Error de red al guardar nodo.");
        }
    },

    async updateInfraNodeStatus(id) {
        const token = api.getToken();
        const actionsContainer = document.getElementById(`infra-actions-${id}`);
        if (!actionsContainer) return;

        // Reemplazar botones por barra de progreso animada
        actionsContainer.innerHTML = `
            <div class="flex items-center space-x-2 w-48">
                <div class="w-full bg-gray-700 rounded-full h-2.5 overflow-hidden">
                    <div id="progress-bar-${id}" class="bg-blue-500 h-2.5 rounded-full transition-all duration-500" style="width: 15%"></div>
                </div>
                <span id="progress-text-${id}" class="text-xs font-mono">15%</span>
            </div>
        `;

        let progress = 15;
        const interval = setInterval(() => {
            if (progress < 85) {
                progress += 10;
                const bar = document.getElementById(`progress-bar-${id}`);
                const txt = document.getElementById(`progress-text-${id}`);
                if (bar) bar.style.width = `${progress}%`;
                if (txt) txt.innerText = `${progress}%`;
            }
        }, 400);

        try {
            const res = await fetch(`/api/v1/admin/infra/${id}/update`, { method: 'POST', headers: { 'Authorization': `Bearer ${token}` } });
            clearInterval(interval);
            
            const bar = document.getElementById(`progress-bar-${id}`);
            const txt = document.getElementById(`progress-text-${id}`);
            if (bar) bar.style.width = '100%';
            if (txt) txt.innerText = '100%';

            if (res.ok) {
                setTimeout(() => {
                    alert("Playbook ejecutado y nodo actualizado correctamente.");
                    this.loadInfraNodes();
                }, 500);
                return;
            }

            // El detalle del fallo (STDOUT/STDERR de ansible-playbook) viene en el
            // body: mostrarlo es la única forma de saber por qué falló.
            let detail = `HTTP ${res.status}`;
            try {
                const body = await res.json();
                if (body && body.detail) detail = body.detail;
            } catch (_) { /* respuesta sin JSON */ }

            console.error(`Update de infraestructura ${id} falló:`, detail);
            setTimeout(() => {
                alert(`La actualización del nodo falló (HTTP ${res.status}).\n\n${detail}`);
                this.loadInfraNodes();
            }, 500);

        } catch (e) {
            clearInterval(interval);
            console.error(e);
            alert("Error de red o timeout al ejecutar la actualización del nodo.");
            this.loadInfraNodes();
        }
    },

    async deleteInfraNode(id) {
        if (!confirm("¿Estás seguro de eliminar este nodo?")) return;
        const token = api.getToken();
        try {
            const res = await fetch(`/api/v1/admin/infra/${id}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${token}` } });
            if (res.ok) this.loadInfraNodes();
            else alert("Error al eliminar nodo.");
        } catch (e) {
            console.error(e);
            alert("Error de red al eliminar nodo.");
        }
    },

    // --- Configuracion General: SMTP, Proxy y Backup (specs/014) ---

    async loadSystemConfig() {
        try {
            this.initMobileServerConfig();
            const cfg = await api.getSystemConfig();
            const hostEl = document.getElementById('smtp-host');
            if (hostEl) hostEl.value = cfg.smtp_host || '';
            const portEl = document.getElementById('smtp-port');
            if (portEl) portEl.value = cfg.smtp_port || 587;
            const userEl = document.getElementById('smtp-username');
            if (userEl) userEl.value = cfg.smtp_username || '';
            const fromEl = document.getElementById('smtp-from');
            if (fromEl) fromEl.value = cfg.smtp_from || '';
            const tlsEl = document.getElementById('smtp-use-tls');
            if (tlsEl) tlsEl.checked = cfg.smtp_use_tls !== false;
            const pwEl = document.getElementById('smtp-password');
            if (pwEl) pwEl.value = '';
            const proxyUrlEl = document.getElementById('proxy-url');
            if (proxyUrlEl) proxyUrlEl.value = cfg.proxy_url || '';
            const proxyEnabledEl = document.getElementById('proxy-enabled');
            if (proxyEnabledEl) proxyEnabledEl.checked = !!cfg.proxy_enabled;
        } catch (e) {
            console.error(e);
        }
    },

    async saveSmtpConfig() {
        const payload = {
            smtp_host: document.getElementById('smtp-host')?.value?.trim() || '',
            smtp_port: parseInt(document.getElementById('smtp-port')?.value) || 587,
            smtp_username: document.getElementById('smtp-username')?.value?.trim() || '',
            smtp_password: document.getElementById('smtp-password')?.value || '',
            smtp_from: document.getElementById('smtp-from')?.value?.trim() || '',
            smtp_use_tls: document.getElementById('smtp-use-tls')?.checked ?? true
        };
        if (!payload.smtp_host) { alert('Error: El servidor SMTP es obligatorio.'); return; }
        try {
            await api.saveSmtpConfig(payload);
            alert('Configuracion SMTP guardada correctamente.');
        } catch (e) {
            alert(e.message);
        }
    },

    async testSmtpConfig() {
        const to = prompt('Ingrese el email de destino para la prueba:');
        if (!to) return;
        const EMAIL_REGEX = /^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$/;
        if (!EMAIL_REGEX.test(to)) { alert('Formato de email invalido.'); return; }
        try {
            await api.testSmtpConfig(to);
            alert('Email de prueba enviado correctamente.');
        } catch (e) {
            alert(e.message);
        }
    },

    async saveProxyConfig() {
        const payload = {
            proxy_url: document.getElementById('proxy-url')?.value?.trim() || '',
            proxy_enabled: document.getElementById('proxy-enabled')?.checked ?? false
        };
        try {
            await api.saveProxyConfig(payload);
            alert('Configuracion de proxy guardada correctamente.');
        } catch (e) {
            alert(e.message);
        }
    },

    async loadSSO() {
        try {
            const cfg = await api.getSSOConfig();
            const provider = cfg.provider || {};
            const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val ?? ''; };
            const check = (id, val) => { const el = document.getElementById(id); if (el) el.checked = !!val; };

            set('sso-auth-mode', cfg.auth_mode || 'local');
            set('sso-issuer', provider.issuer_url || '');
            set('sso-client-id', provider.client_id || '');
            set('sso-redirect-uri', provider.redirect_uri || 'https://anomalia.local/api/v1/oidc/callback');
            set('sso-scopes', provider.scopes || 'openid profile email');
            set('sso-client-auth-method', provider.client_auth_method || 'client_secret_basic');
            set('sso-allowed-domains', provider.allowed_domains || '');

            const secretEl = document.getElementById('sso-client-secret');
            if (secretEl) secretEl.value = '';

            check('sso-enabled', cfg.oidc_enabled);
            check('sso-use-discovery', provider.use_discovery !== false);
            check('sso-pkce-enabled', provider.pkce_enabled !== false);
            check('sso-auto-provision', cfg.oidc_auto_provision);
            check('sso-require-email-verified', cfg.oidc_require_email_verified);
        } catch (e) {
            console.error(e);
        }
    },

    async saveSSO() {
        const val = (id) => document.getElementById(id)?.value?.trim();
        const chk = (id) => document.getElementById(id)?.checked;

        const payload = {
            auth_mode: val('sso-auth-mode') || 'local',
            oidc_enabled: !!chk('sso-enabled'),
            oidc_require_email_verified: !!chk('sso-require-email-verified'),
            oidc_auto_provision: !!chk('sso-auto-provision'),
            name: 'default',
            issuer_url: val('sso-issuer') || '',
            client_id: val('sso-client-id') || '',
            redirect_uri: val('sso-redirect-uri') || '',
            scopes: val('sso-scopes') || 'openid profile email',
            use_discovery: chk('sso-use-discovery') !== false,
            client_auth_method: val('sso-client-auth-method') || 'client_secret_basic',
            pkce_enabled: chk('sso-pkce-enabled') !== false,
            allowed_domains: val('sso-allowed-domains') || '',
            is_active: true
        };
        const secret = val('sso-client-secret');
        if (secret) payload.client_secret = secret;

        if (payload.oidc_enabled && (!payload.issuer_url || !payload.client_id)) {
            alert('Error: Issuer URL y Client ID son obligatorios para habilitar OIDC.');
            return;
        }

        try {
            await api.saveSSOConfig(payload);
            alert('Configuración SSO guardada correctamente.');
        } catch (e) {
            alert(e.message);
        }
    },

    async exportBackup() {
        const password = document.getElementById('bkp-export-password')?.value;
        if (!password) { alert('Ingrese una contraseña para cifrar el backup.'); return; }
        try {
            const blob = await api.exportBackup(password);
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `anomalia-backup-${new Date().toISOString().slice(0, 10)}.json`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            alert('Backup exportado correctamente.');
        } catch (e) {
            alert(e.message);
        }
    },

    async importBackup() {
        const fileInput = document.getElementById('bkp-import-file');
        const password = document.getElementById('bkp-import-password')?.value;
        if (!fileInput?.files?.length) { alert('Seleccione un archivo de backup.'); return; }
        if (!password) { alert('Ingrese la contraseña del backup.'); return; }
        if (!confirm('¿Importar backup? Si la reconciliación de infraestructura falla, se revertirá todo (base de datos e infraestructura) al estado previo. Los tenants se reasignarán a la infraestructura de este clúster.')) return;

        try {
            const data = await api.importBackup(fileInput.files[0], password);
            const jobId = data?.job_id;
            if (!jobId) {
                this._importShowProgress(100, 'Importado', []);
                alert(data?.message || 'Backup importado correctamente.');
                await this.loadTenantsAdmin();
                return;
            }
            await this._watchBackupImport(jobId, data?.message);
        } catch (e) {
            const errEl = document.getElementById('bkp-import-progress-error');
            if (errEl) { errEl.textContent = e.message; errEl.classList.remove('hidden'); }
            document.getElementById('bkp-import-progress')?.classList.remove('hidden');
            alert(e.message);
        }
    },

    // Reanuda el seguimiento de un import en curso tras recargar la página
    // (spec 016 §2.6): el trabajo vive en el servidor, la UI se reconecta.
    async resumeBackupImport() {
        try {
            const { jobs } = await api.fetchJobs({ type: 'backup_import', limit: 1 });
            const job = jobs?.[0];
            if (!job || (job.status !== 'queued' && job.status !== 'running')) return;
            await this._watchBackupImport(job.id, null);
        } catch (_) {
            // Sesión vencida o sin jobs: silencioso, la campana informará al final.
        }
    },

    _importShowProgress(pct, phase, steps) {
        const PHASE_LABEL = { reconciling: 'Reconciliando infraestructura…', rollback: 'Revirtiendo cambios…', done: 'Listo', error: 'Error' };
        const box = document.getElementById('bkp-import-progress');
        const phaseEl = document.getElementById('bkp-import-progress-phase');
        const pctEl = document.getElementById('bkp-import-progress-pct');
        const barEl = document.getElementById('bkp-import-progress-bar');
        const errEl = document.getElementById('bkp-import-progress-error');
        const stepsEl = document.getElementById('bkp-import-steps');
        const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[c]));
        const STATUS_ICON = {
            running: 'fa-spinner fa-spin text-amber-400',
            done: 'fa-circle-check text-emerald-400',
            failed: 'fa-circle-xmark text-red-400',
            warning: 'fa-triangle-exclamation text-amber-400',
            pending: 'fa-circle text-slate-500'
        };

        box?.classList.remove('hidden');
        errEl?.classList.add('hidden');
        if (barEl) barEl.style.width = `${pct}%`;
        if (pctEl) pctEl.textContent = `${pct}%`;
        if (phaseEl && phase) phaseEl.textContent = PHASE_LABEL[phase] || phase;

        if (!stepsEl) return;
        if (!steps?.length) { stepsEl.classList.add('hidden'); stepsEl.innerHTML = ''; return; }
        stepsEl.classList.remove('hidden');
        stepsEl.innerHTML = steps.map((s) => {
            const icon = STATUS_ICON[s.status] || STATUS_ICON.pending;
            const detail = s.detail ? ` <span class="opacity-60">— ${esc(s.detail)}</span>` : '';
            return `<li class="flex items-start gap-1.5"><i class="fa-solid ${icon} mt-[2px] w-3 text-center"></i><span>${esc(s.name)}${detail}</span></li>`;
        }).join('');
    },

    async _watchBackupImport(jobId, successMessage) {
        // El import tiene su propio tope server-side (IMPORT_JOB_TIMEOUT_SECONDS):
        // el sondeo lo iguala para no abortar antes que el servidor.
        const IMPORT_POLL_TIMEOUT_MS = 60 * 60 * 1000;
        const errEl = document.getElementById('bkp-import-progress-error');
        this._importShowProgress(0, 'reconciling', []);
        try {
            const job = await pollJob(jobId, (j) => {
                this._importShowProgress(Number(j.progress_pct) || 0, j.phase || 'reconciling', j.steps);
            }, { timeoutMs: IMPORT_POLL_TIMEOUT_MS });
            if (job.status === 'failed') {
                this._importShowProgress(Number(job.progress_pct) || 0, 'error', job.steps);
                const msg = 'La importación falló y se revirtió todo (base de datos e infraestructura) al estado previo.';
                if (errEl) {
                    errEl.textContent = job.logs_ref ? `${msg} Detalle: ${job.logs_ref}` : msg;
                    errEl.classList.remove('hidden');
                }
                alert(msg);
                await this.loadTenantsAdmin();
                return;
            }
            this._importShowProgress(100, 'done', job.steps);
            alert(successMessage || 'Backup importado y reconciliado correctamente.');
            await this.loadTenantsAdmin();
        } catch (e) {
            if (e.sessionExpired) {
                // El trabajo sigue en el servidor; la campana avisará al terminar.
                if (errEl) { errEl.textContent = e.message; errEl.classList.remove('hidden'); }
                return;
            }
            if (errEl) { errEl.textContent = e.message; errEl.classList.remove('hidden'); }
            alert(e.message);
        }
    },

    // --- Solicitudes de acceso / aprobaciones (spec 019) ---

    async loadApprovals() {
        const container = document.getElementById('admin-approvals-container');
        if (container) container.innerHTML = '<p class="text-xs opacity-75">Cargando solicitudes...</p>';
        try {
            const data = await api.fetchApprovals();
            this._renderApprovals(data.items || []);
        } catch (e) {
            if (container) container.innerHTML = '<p class="text-xs text-red-400">No se pudieron cargar las solicitudes.</p>';
        }
    },

    _renderApprovals(items) {
        const container = document.getElementById('admin-approvals-container');
        if (!container) return;
        if (!items.length) {
            container.innerHTML = '<p class="text-xs opacity-75">No hay solicitudes de acceso.</p>';
            return;
        }
        const statusLabels = { pending: 'Pendiente', approved: 'Aprobada', rejected: 'Rechazada', cancelled: 'Cancelada' };
        const statusBadge = {
            pending: 'dynamic-accent',
            approved: 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30',
            rejected: 'bg-red-500/20 text-red-400 border border-red-500/30',
            cancelled: 'dynamic-card border opacity-70'
        };
        container.innerHTML = items.map((r) => {
            const roles = (r.requested_roles || []).map((x) =>
                `<span class="dynamic-input border px-2 py-0.5 rounded text-[11px] mr-1 mb-1 inline-block">${escHtml(x)}</span>`).join('');
            const tenants = (r.requested_tenants || []).map((x) =>
                `<span class="dynamic-input border px-2 py-0.5 rounded text-[11px] mr-1 mb-1 inline-block">${escHtml(x)}</span>`).join('');
            const when = r.created_at ? new Date(r.created_at).toLocaleString() : '';
            const reviewed = r.reviewed_at
                ? `<p class="text-[11px] opacity-60 mt-1">Resuelta por ${escHtml(r.reviewer || '—')} el ${new Date(r.reviewed_at).toLocaleString()}</p>`
                : '';
            const note = r.review_note
                ? `<p class="text-xs opacity-75 mt-1">Nota: ${escHtml(r.review_note)}</p>` : '';
            const actions = [];
            if (r.can_review) {
                actions.push(`<button data-approve-id="${r.id}" class="btn-approve-request bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-emerald-500/30 transition"><i class="fa-solid fa-check mr-1"></i>Aprobar</button>`);
                actions.push(`<button data-reject-id="${r.id}" class="btn-reject-request bg-red-500/20 text-red-400 border border-red-500/30 px-3 py-1.5 rounded-lg text-xs font-semibold hover:bg-red-500/30 transition"><i class="fa-solid fa-xmark mr-1"></i>Rechazar</button>`);
            }
            if (r.can_cancel) {
                actions.push(`<button data-cancel-id="${r.id}" class="btn-cancel-request dynamic-card border px-3 py-1.5 rounded-lg text-xs font-medium hover:opacity-80 transition"><i class="fa-solid fa-ban mr-1"></i>Cancelar</button>`);
            }
            return `<div class="dynamic-card border rounded-2xl p-4">
                <div class="flex justify-between items-start gap-3">
                    <div>
                        <p class="font-semibold text-sm">${escHtml(r.username)}</p>
                        <p class="text-[11px] opacity-60">${escHtml(when)}</p>
                    </div>
                    <span class="${statusBadge[r.status] || 'dynamic-card border'} px-2 py-0.5 rounded-full text-[11px] font-semibold">${escHtml(statusLabels[r.status] || r.status)}</span>
                </div>
                <div class="mt-3">
                    ${roles ? `<div class="mb-1"><span class="text-[11px] uppercase opacity-60 mr-1">Roles:</span>${roles}</div>` : ''}
                    ${tenants ? `<div class="mb-1"><span class="text-[11px] uppercase opacity-60 mr-1">Tenants:</span>${tenants}</div>` : ''}
                    <p class="text-xs opacity-85 mt-2">${escHtml(r.reason)}</p>
                    ${note}${reviewed}
                </div>
                ${actions.length ? `<div class="flex flex-wrap gap-2 mt-3">${actions.join('')}</div>` : ''}
            </div>`;
        }).join('');
    },

    async approveRequest(id) {
        if (!confirm('¿Aprobar esta solicitud y otorgar los accesos?')) return;
        try {
            await api.approveAccessRequest(id);
            await this.loadApprovals();
        } catch (e) {
            alert(e.message || 'No se pudo aprobar la solicitud.');
        }
    },

    async rejectRequest(id) {
        const note = prompt('Motivo del rechazo (opcional):') || '';
        try {
            await api.rejectAccessRequest(id, note);
            await this.loadApprovals();
        } catch (e) {
            alert(e.message || 'No se pudo rechazar la solicitud.');
        }
    },

    async cancelRequest(id) {
        if (!confirm('¿Cancelar esta solicitud?')) return;
        try {
            await api.cancelAccessRequest(id);
            await this.loadApprovals();
        } catch (e) {
            alert(e.message || 'No se pudo cancelar la solicitud.');
        }
    },

    async openAccessRequestModal() {
        const reasonEl = document.getElementById('access-request-reason');
        const errEl = document.getElementById('access-request-error');
        if (reasonEl) reasonEl.value = '';
        if (errEl) { errEl.textContent = ''; errEl.classList.add('hidden'); }
        const rolesBox = document.getElementById('access-roles-checkboxes');
        const tenantsBox = document.getElementById('access-tenants-checkboxes');
        if (rolesBox) rolesBox.innerHTML = '<p class="text-xs opacity-60">Cargando…</p>';
        if (tenantsBox) tenantsBox.innerHTML = '<p class="text-xs opacity-60">Cargando…</p>';
        ui.toggleModal('modal-access-request', true);
        try {
            const catalog = await api.fetchAccessCatalog();
            const render = (box, items, emptyMsg) => {
                if (!box) return;
                if (!items.length) { box.innerHTML = `<p class="text-xs opacity-60">${emptyMsg}</p>`; return; }
                box.innerHTML = items.map((i) =>
                    `<label class="flex items-center space-x-2 text-xs cursor-pointer"><input type="checkbox" value="${escHtml(i.name)}" class="access-req-item"><span>${escHtml(i.name)}</span></label>`).join('');
            };
            render(rolesBox, catalog.roles || [], 'No hay roles disponibles.');
            render(tenantsBox, catalog.tenants || [], 'No hay tenants disponibles.');
        } catch (e) {
            if (rolesBox) rolesBox.innerHTML = '<p class="text-xs text-red-400">Error al cargar roles.</p>';
            if (tenantsBox) tenantsBox.innerHTML = '<p class="text-xs text-red-400">Error al cargar tenants.</p>';
        }
    },


    initMobileServerConfig() {
        const mobileUrlEl = document.getElementById('adminMobileUrl');
        const copyBtn = document.getElementById('adminCopyMobileUrl');
        const qrEl = document.getElementById('adminMobileQr');
        if (!mobileUrlEl) return;
        const baseUrl = window.location.origin;
        const configUrl = 'anomalia://config?base_url=' + encodeURIComponent(baseUrl);
        mobileUrlEl.value = baseUrl;
        if (copyBtn) {
            copyBtn.onclick = async () => {
                try {
                    await navigator.clipboard.writeText(baseUrl);
                    const prev = copyBtn.textContent;
                    copyBtn.textContent = 'Copiado';
                    setTimeout(() => (copyBtn.textContent = prev), 1200);
                } catch (e) {
                    mobileUrlEl.select();
                    document.execCommand('copy');
                }
            };
        }
        if (qrEl && typeof QRCode !== 'undefined') {
            try {
                new QRCode(qrEl, {
                    text: configUrl,
                    width: 180,
                    height: 180,
                    colorDark: '#000000',
                    colorLight: '#ffffff',
                    correctLevel: QRCode.CorrectLevel.H,
                });
            } catch (e) {
                console.error('QR error', e);
            }
        }

        const toggle = document.getElementById('adminMobileAccessToggle');
        const panel = document.getElementById('adminMobileAccessPanel');
        const chevron = document.getElementById('adminMobileAccessChevron');
        if (toggle && panel) {
            toggle.addEventListener('click', () => {
                const open = panel.classList.toggle('hidden');
                if (chevron) {
                    chevron.classList.toggle('fa-chevron-down', open);
                    chevron.classList.toggle('fa-chevron-up', !open);
                }
            });
        }
    },
    async submitAccessRequest() {
        const errEl = document.getElementById('access-request-error');
        const showErr = (msg) => { if (errEl) { errEl.textContent = msg; errEl.classList.remove('hidden'); } };
        if (errEl) { errEl.textContent = ''; errEl.classList.add('hidden'); }

        const roles = Array.from(document.querySelectorAll('#access-roles-checkboxes .access-req-item:checked')).map((c) => c.value);
        const tenants = Array.from(document.querySelectorAll('#access-tenants-checkboxes .access-req-item:checked')).map((c) => c.value);
        const reason = document.getElementById('access-request-reason')?.value?.trim() || '';
        if (!roles.length && !tenants.length) { showErr('Selecciona al menos un rol o un tenant.'); return; }
        if (!reason) { showErr('El motivo es obligatorio.'); return; }

        const btn = document.getElementById('btn-submit-access-request');
        const original = btn ? btn.textContent : '';
        if (btn) { btn.disabled = true; btn.textContent = 'Enviando…'; }
        try {
            await api.createAccessRequest({ roles, tenants, reason });
            ui.toggleModal('modal-access-request', false);
            alert('Solicitud enviada. Un aprobador la revisará.');
            await this.loadApprovals();
        } catch (e) {
            showErr(e.message || 'No se pudo enviar la solicitud.');
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = original; }
        }
    }
};

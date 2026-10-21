// Centro de notificaciones (spec 017 §5): campana + badge + panel in-app.
//
// Sondea GET /api/v1/notifications cada 30s y al recuperar el foco, actualiza el
// badge de no leídas y, si el panel está abierto, la lista. Estado del trabajo
// vive en el servidor; aquí solo se refleja.
import { api } from './api.js';

const POLL_INTERVAL_MS = 30 * 1000;

let _timer = null;
let _isOpen = false;
let _initialized = false;

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[c]));

function setBadge(count) {
    const badge = document.getElementById('notifications-badge');
    if (!badge) return;
    if (count > 0) {
        badge.textContent = count > 99 ? '99+' : String(count);
        badge.classList.remove('hidden');
    } else {
        badge.classList.add('hidden');
    }
}

function renderList(items) {
    const list = document.getElementById('notifications-list');
    if (!list) return;
    if (!items || items.length === 0) {
        list.innerHTML = '<p class="text-xs opacity-60 text-center py-4">Sin notificaciones.</p>';
        return;
    }
    list.innerHTML = items.map((n) => {
        const unreadDot = n.is_read ? '' : '<span class="inline-block w-2 h-2 rounded-full bg-red-500 mt-1.5 shrink-0"></span>';
        const when = n.created_at ? new Date(n.created_at).toLocaleString() : '';
        const body = n.body ? `<p class="opacity-70 text-[11px] mt-0.5">${esc(n.body)}</p>` : '';
        const cls = n.is_read ? 'opacity-70' : '';
        return `<button data-notif-id="${n.id}" data-notif-link="${esc(n.link || '')}" class="w-full text-left px-2 py-2 rounded-lg hover:opacity-90 transition flex gap-2 ${cls}">
            ${unreadDot}
            <span class="flex-1">
                <span class="block text-xs font-medium">${esc(n.title)}</span>
                ${body}
                <span class="block text-[10px] opacity-50 mt-0.5">${esc(when)}</span>
            </span>
        </button>`;
    }).join('');
}

function renderLoading() {
    const list = document.getElementById('notifications-list');
    if (list) list.innerHTML = '<p class="text-xs opacity-60 text-center py-4">Cargando…</p>';
}

function renderError(msg) {
    const list = document.getElementById('notifications-list');
    if (list) list.innerHTML = `<p class="text-xs text-red-400 text-center py-4">${esc(msg)}</p>`;
}

async function refresh() {
    try {
        const data = await api.fetchNotifications({ limit: 50 });
        setBadge(data.unread_count || 0);
        if (_isOpen) renderList(data.items || []);
        return data;
    } catch (err) {
        if (err && err.status === 401) {
            stop();
            return null;
        }
        if (_isOpen) renderError('No se pudieron cargar las notificaciones.');
        return null;
    }
}

function openPanel() {
    const panel = document.getElementById('notifications-panel');
    if (!panel) return;
    _isOpen = true;
    panel.classList.remove('hidden');
    renderLoading();
    refresh();
}

function closePanel() {
    const panel = document.getElementById('notifications-panel');
    if (!panel) return;
    _isOpen = false;
    panel.classList.add('hidden');
}

function togglePanel() {
    if (_isOpen) closePanel(); else openPanel();
}

async function handleListClick(e) {
    const btn = e.target.closest('[data-notif-id]');
    if (!btn) return;
    const id = btn.getAttribute('data-notif-id');
    const link = btn.getAttribute('data-notif-link');
    try {
        await api.markNotificationRead(id);
    } catch (_) { /* best-effort */ }
    await refresh();
    if (link) {
        closePanel();
        window.location.href = link;
    }
}

export function initNotifications() {
    if (_initialized) return;
    const btn = document.getElementById('btn-notifications');
    if (!btn) return;
    _initialized = true;

    btn.addEventListener('click', (e) => {
        e.stopPropagation();
        togglePanel();
    });

    document.getElementById('notifications-list')
        ?.addEventListener('click', handleListClick);

    document.getElementById('notifications-mark-all')?.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
            await api.markAllNotificationsRead();
        } catch (_) { /* best-effort */ }
        await refresh();
    });

    // Cerrar al hacer clic fuera del panel.
    document.addEventListener('click', (e) => {
        if (!_isOpen) return;
        if (e.target.closest('#notifications-container')) return;
        closePanel();
    });

    // Refrescar al recuperar el foco/pestaña.
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') refresh();
    });
    window.addEventListener('focus', () => refresh());

    refresh();
    start();
}

export function start() {
    if (_timer) return;
    _timer = setInterval(refresh, POLL_INTERVAL_MS);
}

export function stop() {
    if (_timer) {
        clearInterval(_timer);
        _timer = null;
    }
}

import { api } from './api.js';

export const state = {
    globalRolesCache: [],
    globalTenantsCache: [],
    globalProfilesCache: [],
    currentUserRoles: [],
    currentUserProfiles: [],
    // Jobs en vuelo: { job_id: { type, status, phase, progress_pct, error_code } }.
    // El estado real vive en Postgres (job_state); esto es solo la vista para la
    // UI, alimentada por el polling (specs/011 §5.3).
    jobs: {},

    // Registra un job y devuelve su id para que el llamante pueda consultarlo.
    trackJob(jobId, type, status = 'queued') {
        if (!jobId) return null;
        this.jobs[jobId] = { id: jobId, type, status, phase: null, progress_pct: 0, error_code: null };
        return jobId;
    },

    updateJob(jobId, patch) {
        if (this.jobs[jobId]) Object.assign(this.jobs[jobId], patch);
    },

    dropJob(jobId) {
        delete this.jobs[jobId];
    }
};

// Estados terminales de un job: en cuanto se alcanza uno, el polling para.
const JOB_TERMINAL_STATES = new Set(['succeeded', 'failed', 'cancelled']);

const JOB_POLL_INTERVAL_MS = 1500;
// El alta de un tenant puede tardar varios minutos: despliegue de vmalert
// (hasta ~15 min) + provisión de Parses. Se alinea con el presupuesto del worker.
const JOB_POLL_TIMEOUT_MS = 20 * 60 * 1000;

/**
 * Consulta el estado de un job hasta que llega a un estado terminal.
 *
 * El trabajo vive en el servidor (job_state): si la sesión expira mientras se
 * sondea (401), se deja de sondear con un aviso explícito (`err.sessionExpired`)
 * en lugar de reportar un fallo — el job sigue y llegará una notificación al
 * terminar (spec 016 §2.6, spec 017).
 *
 * @param {string} jobId
 * @param {(job: object) => void} [onUpdate] progreso por cada cambio observado
 * @param {{timeoutMs?: number}} [options]
 * @returns {Promise<object>} el último estado conocido del job
 */
export async function pollJob(jobId, onUpdate, options = {}) {
    const timeoutMs = options.timeoutMs || JOB_POLL_TIMEOUT_MS;
    const deadline = Date.now() + timeoutMs;

    while (Date.now() < deadline) {
        let job;
        try {
            job = await api.fetchJob(jobId);
        } catch (err) {
            if (err && err.status === 401) {
                state.dropJob(jobId);
                const expired = new Error(
                    'Su sesión expiró. El trabajo continúa en el servidor y le notificaremos al terminar.'
                );
                expired.sessionExpired = true;
                throw expired;
            }
            throw err;
        }
        state.updateJob(jobId, job);
        if (onUpdate) onUpdate(job);
        if (JOB_TERMINAL_STATES.has(job.status)) {
            state.dropJob(jobId);
            return job;
        }
        await new Promise((resolve) => setTimeout(resolve, JOB_POLL_INTERVAL_MS));
    }

    state.dropJob(jobId);
    throw new Error(`El job ${jobId} no terminó dentro del tiempo de espera.`);
}

/**
 * Consulta varios jobs en paralelo hasta que todos alcanzan estado terminal.
 *
 * Pensado para operaciones que encolan N jobs (p. ej. la reconciliación
 * post-import, specs/014 §6.3). El callback recibe un agregado
 * {total, done, progress_pct, jobs}. Resuelve con los estados finales;
 * lanza si alguno falla o si vence el timeout.
 *
 * @param {string[]} jobIds
 * @param {(agg: object) => void} [onUpdate]
 * @returns {Promise<object[]>}
 */
export async function pollJobs(jobIds, onUpdate) {
    const ids = (jobIds || []).filter(Boolean);
    if (ids.length === 0) return [];

    const latest = new Map();
    const report = () => {
        if (!onUpdate) return;
        const jobs = ids.map((id) => latest.get(id)).filter(Boolean);
        const sum = jobs.reduce((acc, job) => acc + (Number(job.progress_pct) || 0), 0);
        onUpdate({
            total: ids.length,
            done: jobs.filter((job) => JOB_TERMINAL_STATES.has(job.status)).length,
            progress_pct: Math.round(sum / ids.length),
            jobs
        });
    };

    const results = await Promise.all(ids.map((id) =>
        pollJob(id, (job) => { latest.set(id, job); report(); })
    ));

    const failed = results.find((job) => job.status === 'failed' || job.status === 'cancelled');
    if (failed) {
        throw new Error(`Reconciliación falló en el job ${failed.id} (${failed.error_code || failed.status}).`);
    }
    return results;
}

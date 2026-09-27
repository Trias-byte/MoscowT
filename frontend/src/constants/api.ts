export const DEMO = import.meta.env.VITE_DEMO === 'true';
export const API_V1_BASE = (import.meta.env.VITE_API_URL || '/api/v1').replace(/\/$/, '');
export const API_V2_BASE = API_V1_BASE.replace(/\/api\/v1$/, '/api/v2');
export const EXPORT_POLL_MS = 500;
export const EXPORT_TIMEOUT_MS = 300_000;
export const DOWNLOAD_REVOKE_MS = 10_000;

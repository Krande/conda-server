// Runtime config for the SPA.
// In dev, Vite proxies /api to the backend (see vite.config.ts).
// In prod, the backend serves the SPA so /api is same-origin.

export const API_BASE = "/api";

export function backendOrigin(): string {
  // Honor an explicit build-time override; otherwise use the current origin.
  return import.meta.env.VITE_BACKEND_URL || window.location.origin;
}

/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Optional override for the backend API base URL.
   *
   * Defaults to an empty string (relative URLs) so requests go through the
   * Vite dev-server proxy. Set this when the frontend is served from a
   * different origin than the API (e.g. `http://127.0.0.1:8000`).
   */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Minimal typed fetch helper. Throws on non-2xx so TanStack Query can handle
// errors; includes credentials so the session cookie rides along.

import { API_BASE } from "@/config";

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(`${status} ${detail}`);
    this.name = "ApiError";
  }
}

type Method = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

interface RequestOptions {
  method?: Method;
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

export async function api<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const url = path.startsWith("http") ? path : `${API_BASE}${path}`;
  const init: RequestInit = {
    method: options.method ?? "GET",
    credentials: "include",
    signal: options.signal,
    headers: {
      Accept: "application/json",
      ...(options.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  };
  if (options.body !== undefined) {
    init.body = JSON.stringify(options.body);
  }

  const response = await fetch(url, init);

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const data = await response.json();
      if (typeof data?.detail === "string") detail = data.detail;
    } catch {
      // Body wasn't JSON — fall back to statusText.
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

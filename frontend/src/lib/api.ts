// Typed wrappers around the backend REST API.
// Use these via the useQuery / useMutation hooks in queries.ts rather than
// calling them directly from components.

import { api } from "./http";
import type {
  ApiToken,
  ApiTokenCreated,
  AuditEntry,
  Channel,
  ChannelMember,
  Package,
  UpstreamSearchResult,
  UpstreamVersionsResult,
} from "./types";

export interface CreateChannelBody {
  name: string;
  description?: string;
  private?: boolean;
  mirror_url?: string;
  mirror_cache_seconds?: number;
}

export const channels = {
  list: (signal?: AbortSignal) =>
    api<Channel[]>("/channels", { signal }),
  get: (name: string, signal?: AbortSignal) =>
    api<Channel>(`/channels/${encodeURIComponent(name)}`, { signal }),
  create: (body: CreateChannelBody) =>
    api<Channel>("/channels", { method: "POST", body }),
  remove: (name: string) =>
    api<void>(`/channels/${encodeURIComponent(name)}`, { method: "DELETE" }),
  reindex: (name: string) =>
    api<{ status: string; channel: string }>(
      `/channels/${encodeURIComponent(name)}/reindex`,
      { method: "POST" },
    ),
  listMembers: (name: string, signal?: AbortSignal) =>
    api<ChannelMember[]>(
      `/channels/${encodeURIComponent(name)}/members`,
      { signal },
    ),
  addMember: (name: string, body: { email: string; role: ChannelMember["role"] }) =>
    api<ChannelMember>(
      `/channels/${encodeURIComponent(name)}/members`,
      { method: "POST", body },
    ),
  updateMember: (name: string, userId: number, role: ChannelMember["role"]) =>
    api<ChannelMember>(
      `/channels/${encodeURIComponent(name)}/members/${userId}`,
      { method: "PATCH", body: { role } },
    ),
  removeMember: (name: string, userId: number) =>
    api<void>(
      `/channels/${encodeURIComponent(name)}/members/${userId}`,
      { method: "DELETE" },
    ),
  importPreview: (
    name: string,
    body: {
      upstream_url: string;
      packages: { subdir: string; filename: string }[];
      target_platforms?: string[];
    },
  ) =>
    api<{
      upstream_url: string;
      direct_requested: PreviewItem[];
      transitive_new: PreviewItem[];
      transitive_satisfied_locally: PreviewItem[];
      total_new_bytes: number;
    }>(`/channels/${encodeURIComponent(name)}/import/preview`, {
      method: "POST",
      body,
    }),
  importFromUpstream: (
    name: string,
    body: {
      upstream_url: string;
      packages: { subdir: string; filename: string }[];
    },
  ) =>
    api<{
      job_id: number;
      channel: string;
      upstream_url: string;
      status_url: string;
    }>(`/channels/${encodeURIComponent(name)}/import`, {
      method: "POST",
      body,
    }),
  getImportJob: (name: string, jobId: number, signal?: AbortSignal) =>
    api<ImportJob>(
      `/channels/${encodeURIComponent(name)}/import/jobs/${jobId}`,
      { signal },
    ),
};

export interface ImportJobResultEntry {
  filename: string;
  subdir: string;
  status?: "stored" | "error";
  size?: number;
  name?: string;
  version?: string;
  build?: string;
  imported_from?: string;
  error?: string;
}

export interface ImportJob {
  id: number;
  channel: string;
  upstream_url: string;
  status: "pending" | "running" | "completed" | "failed";
  total_count: number;
  completed_count: number;
  failed_count: number;
  written_bytes: number;
  current_filename: string | null;
  error: string | null;
  results: ImportJobResultEntry[];
  created_at: string;
  finished_at: string | null;
}

export const upstream = {
  search: (
    params: { url: string; subdir: string; name: string },
    signal?: AbortSignal,
  ) => {
    const qs = new URLSearchParams({
      url: params.url,
      subdir: params.subdir,
      name: params.name,
    }).toString();
    return api<UpstreamSearchResult>(`/upstream/search?${qs}`, { signal });
  },
  versions: (
    params: {
      url: string;
      subdir: string;
      name: string;
      target_channel?: string;
    },
    signal?: AbortSignal,
  ) => {
    const qs = new URLSearchParams({
      url: params.url,
      subdir: params.subdir,
      name: params.name,
      ...(params.target_channel ? { target_channel: params.target_channel } : {}),
    }).toString();
    return api<UpstreamVersionsResult>(`/upstream/versions?${qs}`, { signal });
  },
};

export const packages = {
  list: (channel: string, signal?: AbortSignal) =>
    api<Package[]>(
      `/channels/${encodeURIComponent(channel)}/packages`,
      { signal },
    ),
  get: (channel: string, name: string, signal?: AbortSignal) =>
    api<Package>(
      `/channels/${encodeURIComponent(channel)}/packages/${encodeURIComponent(name)}`,
      { signal },
    ),
  deleteVersion: (channel: string, subdir: string, filename: string) =>
    api<void>(
      `/channels/${encodeURIComponent(channel)}/packages/${encodeURIComponent(subdir)}/${encodeURIComponent(filename)}`,
      { method: "DELETE" },
    ),
};

export interface SearchResults {
  packages: { name: string; channel: string }[];
  channels: {
    name: string;
    description: string | null;
    private: boolean;
    mirror_url: string | null;
  }[];
}

export interface ResolveResult {
  [name: string]: { channel: string };
}

export interface PreviewItem {
  name: string;
  version: string;
  build: string;
  subdir: string;
  filename: string;
  size: number | null;
  depends: string[];
}

export const search = {
  query: (q: string, signal?: AbortSignal) =>
    api<SearchResults>(
      `/search?q=${encodeURIComponent(q)}&limit=20`,
      { signal },
    ),
  resolve: (names: string[], signal?: AbortSignal) => {
    const joined = names.join(",");
    if (!joined) return Promise.resolve<ResolveResult>({});
    return api<ResolveResult>(
      `/search/resolve?names=${encodeURIComponent(joined)}`,
      { signal },
    );
  },
};

export interface AuditQuery {
  action?: string;
  channel?: string;
  actor?: string;
  limit?: number;
  offset?: number;
}

export const audit = {
  list: (query: AuditQuery = {}, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (query.action) params.set("action", query.action);
    if (query.channel) params.set("channel", query.channel);
    if (query.actor) params.set("actor", query.actor);
    if (query.limit !== undefined) params.set("limit", String(query.limit));
    if (query.offset !== undefined) params.set("offset", String(query.offset));
    const qs = params.toString();
    return api<AuditEntry[]>(`/admin/audit${qs ? `?${qs}` : ""}`, { signal });
  },
  actions: (signal?: AbortSignal) =>
    api<string[]>("/admin/audit/actions", { signal }),
};

export const tokens = {
  list: (signal?: AbortSignal) => api<ApiToken[]>("/auth/tokens", { signal }),
  create: (body: { description?: string; expires_in_days?: number }) =>
    api<ApiTokenCreated>("/auth/tokens", { method: "POST", body }),
  revoke: (id: number) =>
    api<void>(`/auth/tokens/${id}`, { method: "DELETE" }),
};

export interface AboutBuild {
  version: string;
  git_sha: string;
  build_date: string;
  python_version: string;
  rattler_version: string;
  platform: string;
}

export interface AboutStats {
  channels: number;
  packages: number;
  package_versions: number;
  total_storage_bytes: number;
  import_jobs_total: number;
  import_jobs_running: number;
}

// Storage backends the server can be configured with. Mirrors
// StorageBackend in src/conda_server/config.py.
export type StorageBackend = "local" | "s3" | "azure" | "gcs";

export interface AboutResponse {
  build: AboutBuild;
  stats: AboutStats;
  // Object-storage backend the deployment runs on. Used to tailor the
  // "Show files" CORS hint. Optional so an older/cached server that
  // predates the field doesn't break the type.
  storage_backend?: StorageBackend;
}

export const about = {
  get: (signal?: AbortSignal) =>
    api<AboutResponse>("/about", { signal }),
};

// Build a package-download URL. The backend issues a 302 to a presigned URL
// (S3/Azure) or streams the bytes (local dev). Follow redirects, so
// <a href=packageDownloadUrl> works directly in browsers.
export function packageDownloadUrl(
  channel: string,
  subdir: string,
  filename: string,
): string {
  return `/${encodeURIComponent(channel)}/${encodeURIComponent(subdir)}/${encodeURIComponent(filename)}`;
}

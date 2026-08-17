// Response shapes mirroring the backend's Pydantic models.
// Kept here (not in each service file) so components can import types freely.

export interface User {
  id: number;
  subject: string;
  email: string | null;
  username: string | null;
  role: "user" | "admin";
}

export type ChannelRole = "reader" | "writer" | "owner" | "admin";

export interface Channel {
  id: number;
  name: string;
  description: string | null;
  private: boolean;
  storage_prefix: string;
  mirror_url: string | null;
  mirror_cache_seconds: number;
  // Caller's effective permission on the channel. null = no access
  // (shouldn't appear in responses — we filter listings first, and
  // visible_channel_or_404 returns 404 rather than a null-role row).
  my_role: ChannelRole | null;
}

export interface ChannelMember {
  user_id: number;
  email: string | null;
  username: string | null;
  role: "reader" | "writer" | "owner";
}

export interface PackageVersion {
  version: string;
  build: string;
  build_number: number;
  subdir: string;
  filename: string;
  size: number | null;
  sha256: string | null;
  md5?: string | null;
  // Match-specs from repodata. Entries look like "numpy >=1.20",
  // "openssl >=3.5.5,<4.0a0", "ucrt". The UI parses the leading name
  // for upstream links and shows the rest as a constraint.
  depends?: string[];
  constrains?: string[];
  license?: string | null;
  // Upstream-set millisecond epoch.
  timestamp?: number | null;
  // Set when this version landed via the import-from-upstream flow;
  // null for plain admin uploads.
  imported_from?: string | null;
}

export interface UpstreamPackageHit {
  name: string;
  in_channels: string[];
}

export interface UpstreamSearchResult {
  upstream_url: string;
  subdir: string;
  matched: number;
  truncated: boolean;
  packages: UpstreamPackageHit[];
}

export interface UpstreamVersion {
  name: string;
  version: string;
  build: string;
  build_number: number;
  subdir: string;
  filename: string;
  size: number | null;
  sha256: string | null;
  md5: string | null;
  depends: string[];
  constrains: string[];
  in_target_channel: boolean;
}

export interface UpstreamVersionsResult {
  upstream_url: string;
  subdir: string;
  name: string;
  versions: UpstreamVersion[];
  truncated: boolean;
}

export interface Package {
  name: string;
  description: string | null;
  versions: PackageVersion[];
}

export interface ApiToken {
  id: number;
  description: string | null;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
}

export interface ApiTokenCreated extends ApiToken {
  token: string;
}

export interface AuditEntry {
  id: number;
  actor_id: number | null;
  actor_email: string | null;
  action: string;
  channel_name: string | null;
  target: string | null;
  meta: Record<string, unknown>;
  created_at: string;
}

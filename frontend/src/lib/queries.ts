// Typed TanStack Query hooks for the backend API.
// Mutations invalidate the right keys so UIs update without manual refetches.

import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  audit,
  channels,
  packages,
  search,
  tokens,
  upstream,
  type AuditQuery,
  type CreateChannelBody,
  type ImportJob,
  type ResolveResult,
  type SearchResults,
} from "./api";
import type {
  ApiTokenCreated,
  AuditEntry,
  Channel,
  ChannelMember,
  Package,
  UpstreamSearchResult,
  UpstreamVersionsResult,
} from "./types";

export const queryKeys = {
  search: (q: string) => ["search", q] as const,
  audit: (query: AuditQuery) => ["audit", query] as const,
  auditActions: ["audit", "actions"] as const,
  channels: ["channels"] as const,
  channel: (name: string) => ["channels", name] as const,
  members: (channel: string) => ["channels", channel, "members"] as const,
  packages: (channel: string) => ["channels", channel, "packages"] as const,
  package: (channel: string, name: string) =>
    ["channels", channel, "packages", name] as const,
  tokens: ["tokens"] as const,
};

export function useResolvePackages(names: string[], enabled = true) {
  // Stable key: sort before joining so ["a","b"] and ["b","a"] share a cache entry.
  const key = [...names].sort().join(",");
  return useQuery<ResolveResult>({
    queryKey: ["resolve", key] as const,
    queryFn: ({ signal }) => search.resolve(names, signal),
    enabled: enabled && names.length > 0,
    staleTime: 60_000,
  });
}

export function useSearch(q: string, enabled = true) {
  const trimmed = q.trim();
  return useQuery<SearchResults>({
    queryKey: queryKeys.search(trimmed),
    queryFn: ({ signal }) => search.query(trimmed, signal),
    enabled: enabled && trimmed.length >= 2,
    staleTime: 30_000,
  });
}

export function useChannels() {
  return useQuery<Channel[]>({
    queryKey: queryKeys.channels,
    queryFn: ({ signal }) => channels.list(signal),
  });
}

export function useChannel(name: string | undefined) {
  return useQuery<Channel>({
    queryKey: name ? queryKeys.channel(name) : ["channels", "unknown"],
    queryFn: ({ signal }) => channels.get(name!, signal),
    enabled: !!name,
  });
}

export function usePackages(channel: string | undefined) {
  return useQuery<Package[]>({
    queryKey: channel ? queryKeys.packages(channel) : ["packages", "unknown"],
    queryFn: ({ signal }) => packages.list(channel!, signal),
    enabled: !!channel,
  });
}

export function usePackage(channel: string | undefined, name: string | undefined) {
  return useQuery<Package>({
    queryKey:
      channel && name
        ? queryKeys.package(channel, name)
        : ["packages", "unknown"],
    queryFn: ({ signal }) => packages.get(channel!, name!, signal),
    enabled: !!channel && !!name,
  });
}

export function useAuditActions() {
  return useQuery<string[]>({
    queryKey: queryKeys.auditActions,
    queryFn: ({ signal }) => audit.actions(signal),
    staleTime: 60 * 60 * 1000, // static-ish; the server's ACTIONS list rarely changes.
  });
}

export function useAuditLog(query: AuditQuery) {
  return useQuery<AuditEntry[]>({
    queryKey: queryKeys.audit(query),
    queryFn: ({ signal }) => audit.list(query, signal),
  });
}

export function useTokens() {
  return useQuery({
    queryKey: queryKeys.tokens,
    queryFn: ({ signal }) => tokens.list(signal),
  });
}

export function useCreateToken() {
  const qc = useQueryClient();
  return useMutation<ApiTokenCreated, Error, { description?: string; expires_in_days?: number }>({
    mutationFn: (body) => tokens.create(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.tokens });
    },
  });
}

export function useRevokeToken() {
  const qc = useQueryClient();
  return useMutation<void, Error, number>({
    mutationFn: (id) => tokens.revoke(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.tokens });
    },
  });
}

export function useCreateChannel() {
  const qc = useQueryClient();
  return useMutation<Channel, Error, CreateChannelBody>({
    mutationFn: (body) => channels.create(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.channels });
    },
  });
}

export function useDeleteChannel() {
  const qc = useQueryClient();
  return useMutation<void, Error, string>({
    mutationFn: (name) => channels.remove(name),
    onSuccess: (_void, name) => {
      qc.invalidateQueries({ queryKey: queryKeys.channels });
      qc.removeQueries({ queryKey: queryKeys.channel(name) });
      qc.removeQueries({ queryKey: queryKeys.packages(name) });
    },
  });
}

export function useReindexChannel() {
  return useMutation<{ status: string; channel: string }, Error, string>({
    mutationFn: (name) => channels.reindex(name),
  });
}

export function useMembers(channelName: string | undefined, enabled = true) {
  return useQuery<ChannelMember[]>({
    queryKey: channelName ? queryKeys.members(channelName) : ["members", "unknown"],
    queryFn: ({ signal }) => channels.listMembers(channelName!, signal),
    enabled: !!channelName && enabled,
  });
}

export function useAddMember(channelName: string) {
  const qc = useQueryClient();
  return useMutation<
    ChannelMember,
    Error,
    { email: string; role: ChannelMember["role"] }
  >({
    mutationFn: (body) => channels.addMember(channelName, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.members(channelName) });
    },
  });
}

export function useUpdateMember(channelName: string) {
  const qc = useQueryClient();
  return useMutation<
    ChannelMember,
    Error,
    { userId: number; role: ChannelMember["role"] }
  >({
    mutationFn: ({ userId, role }) => channels.updateMember(channelName, userId, role),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.members(channelName) });
    },
  });
}

export function useRemoveMember(channelName: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, number>({
    mutationFn: (userId) => channels.removeMember(channelName, userId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: queryKeys.members(channelName) });
    },
  });
}

export function useUpstreamSearch(
  url: string,
  subdir: string,
  name: string,
  enabled = true,
) {
  const u = url.trim();
  const n = name.trim();
  return useQuery<UpstreamSearchResult>({
    queryKey: ["upstream-search", u, subdir, n] as const,
    queryFn: ({ signal }) =>
      upstream.search({ url: u, subdir, name: n }, signal),
    enabled: enabled && u.length > 0 && n.length >= 2,
    staleTime: 30_000,
  });
}

export function useUpstreamVersions(
  url: string,
  subdir: string,
  name: string,
  targetChannel: string | undefined,
  enabled = true,
) {
  const u = url.trim();
  return useQuery<UpstreamVersionsResult>({
    queryKey: ["upstream-versions", u, subdir, name, targetChannel] as const,
    queryFn: ({ signal }) =>
      upstream.versions(
        { url: u, subdir, name, target_channel: targetChannel },
        signal,
      ),
    enabled: enabled && u.length > 0 && name.length > 0,
  });
}

export function useImportFromUpstream(channelName: string) {
  return useMutation<
    Awaited<ReturnType<typeof channels.importFromUpstream>>,
    Error,
    { upstream_url: string; packages: { subdir: string; filename: string }[] }
  >({
    mutationFn: (body) => channels.importFromUpstream(channelName, body),
    // Don't invalidate here — the import is now asynchronous; the
    // caller does the invalidation when the job completes.
  });
}

export function useImportJob(
  channelName: string,
  jobId: number | null,
) {
  return useQuery<ImportJob>({
    queryKey: ["import-job", channelName, jobId] as const,
    queryFn: ({ signal }) =>
      channels.getImportJob(channelName, jobId as number, signal),
    enabled: jobId !== null,
    // Poll while the job is in flight; stop once it's terminal. The 1s
    // cadence is the same as a typical upload progress refresh.
    refetchInterval: (q) => {
      const data = q.state.data;
      if (!data) return 1000;
      return data.status === "running" || data.status === "pending"
        ? 1000
        : false;
    },
    refetchIntervalInBackground: false,
  });
}

export function useImportPreview(channelName: string) {
  return useMutation<
    Awaited<ReturnType<typeof channels.importPreview>>,
    Error,
    {
      upstream_url: string;
      packages: { subdir: string; filename: string }[];
      target_platforms?: string[];
    }
  >({
    mutationFn: (body) => channels.importPreview(channelName, body),
  });
}

export function useDeletePackageVersion(channelName: string, packageName: string) {
  const qc = useQueryClient();
  return useMutation<
    void,
    Error,
    { subdir: string; filename: string }
  >({
    mutationFn: ({ subdir, filename }) =>
      packages.deleteVersion(channelName, subdir, filename),
    onSuccess: () => {
      // Invalidate both the single-package view (currently open) and the
      // channel's list so the removed row disappears once the reindex lands.
      qc.invalidateQueries({ queryKey: queryKeys.package(channelName, packageName) });
      qc.invalidateQueries({ queryKey: queryKeys.packages(channelName) });
    },
  });
}

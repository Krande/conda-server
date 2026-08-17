import { useEffect, useState } from "react";
import { Button } from "./ui/Button";
import { Card, CardBody, CardHeader } from "./ui/Card";
import { ErrorState } from "./ui/EmptyState";
import { Input } from "./ui/Input";
import { useQueryClient } from "@tanstack/react-query";
import {
  queryKeys,
  useImportFromUpstream,
  useImportJob,
  useImportPreview,
  useUpstreamSearch,
  useUpstreamVersions,
} from "@/lib/queries";
import type { ImportJob, PreviewItem } from "@/lib/api";
import type { Channel, UpstreamVersion } from "@/lib/types";

const SUBDIRS = [
  "noarch",
  "linux-64",
  "linux-aarch64",
  "linux-ppc64le",
  "osx-64",
  "osx-arm64",
  "win-64",
] as const;

type Subdir = (typeof SUBDIRS)[number];

// Platforms that can serve as target environments for noarch deps.
// Excludes noarch itself — that doesn't carry platform-specific builds.
const TARGET_PLATFORMS: Subdir[] = SUBDIRS.filter((s) => s !== "noarch");

const DEFAULT_UPSTREAM = "https://conda.anaconda.org/conda-forge";

function formatSize(bytes: number | null): string {
  if (!bytes || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(n >= 10 ? 0 : 1)} ${units[i]}`;
}

function useDebounced<T>(value: T, delayMs = 300): T {
  const [d, setD] = useState(value);
  useEffect(() => {
    const h = setTimeout(() => setD(value), delayMs);
    return () => clearTimeout(h);
  }, [value, delayMs]);
  return d;
}

/**
 * Curated import: search an upstream channel by package name, pick
 * specific versions, fetch them server-side, store them in this
 * channel. Each imported file's PackageVersion row records its
 * upstream URL via the imported_from column.
 *
 * Hidden for mirror channels — those proxy upstream wholesale and
 * don't need a separate curation surface — and for callers without
 * writer+ access on the channel.
 */
export function ImportFromUpstreamCard({ channel }: { channel: Channel }) {
  const canWrite =
    channel.my_role === "writer" ||
    channel.my_role === "owner" ||
    channel.my_role === "admin";

  const [url, setUrl] = useState(DEFAULT_UPSTREAM);
  const [subdir, setSubdir] = useState<Subdir>("linux-64");
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounced(search, 300);
  const [pickedName, setPickedName] = useState<string | null>(null);
  const [pickedFiles, setPickedFiles] = useState<Set<string>>(new Set());
  // Extra platforms whose repodata the solver should pull in to find
  // transitive deps for noarch picks. Defaults to linux-64 (the most
  // common server target). Only exposed in the UI when the selection
  // includes a noarch package.
  const [targetPlatforms, setTargetPlatforms] = useState<Set<Subdir>>(
    () => new Set<Subdir>(["linux-64"]),
  );

  const searchQ = useUpstreamSearch(url, subdir, debouncedSearch);
  const versionsQ = useUpstreamVersions(
    url,
    subdir,
    pickedName ?? "",
    channel.name,
    !!pickedName,
  );
  const importer = useImportFromUpstream(channel.name);
  const preview = useImportPreview(channel.name);
  const [previewOpen, setPreviewOpen] = useState(false);
  // Active job — drives the polling progress modal until the job lands
  // in a terminal state (completed | failed). Cleared by the modal's
  // "Done" button so the user can start another import.
  const [activeJobId, setActiveJobId] = useState<number | null>(null);
  const jobQ = useImportJob(channel.name, activeJobId);
  const qc = useQueryClient();

  if (!canWrite || channel.mirror_url) return null;

  const togglePicked = (filename: string) => {
    setPickedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      return next;
    });
  };

  const totalPickedBytes = (versionsQ.data?.versions ?? [])
    .filter((v) => pickedFiles.has(v.filename))
    .reduce((acc, v) => acc + (v.size ?? 0), 0);

  const pickedHasNoarch = (versionsQ.data?.versions ?? []).some(
    (v) => pickedFiles.has(v.filename) && v.subdir === "noarch",
  );

  const buildSelectionPayload = () => {
    const versions = versionsQ.data?.versions ?? [];
    return versions
      .filter((v) => pickedFiles.has(v.filename))
      .map((v) => ({ subdir: v.subdir, filename: v.filename }));
  };

  const toggleTargetPlatform = (p: Subdir) => {
    setTargetPlatforms((prev) => {
      const next = new Set(prev);
      if (next.has(p)) next.delete(p);
      else next.add(p);
      return next;
    });
  };

  const handleStartImport = async () => {
    if (!pickedName || pickedFiles.size === 0) return;
    const packages = buildSelectionPayload();
    await preview.mutateAsync({
      upstream_url: url.trim().replace(/\/$/, ""),
      packages,
      // Only meaningful for noarch picks — the backend will already
      // load the repodata for each picked package's own subdir.
      target_platforms: pickedHasNoarch ? Array.from(targetPlatforms) : [],
    });
    setPreviewOpen(true);
  };

  const handleConfirmImport = async (chosen: { subdir: string; filename: string }[]) => {
    const res = await importer.mutateAsync({
      upstream_url: url.trim().replace(/\/$/, ""),
      packages: chosen,
    });
    setActiveJobId(res.job_id);
    setPreviewOpen(false);
    setPickedFiles(new Set());
    preview.reset();
  };

  const handleCloseJob = () => {
    setActiveJobId(null);
    importer.reset();
    versionsQ.refetch();
    qc.invalidateQueries({ queryKey: queryKeys.packages(channel.name) });
  };

  const handleCancelPreview = () => {
    setPreviewOpen(false);
    preview.reset();
  };

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">Import from upstream</h2>
        <p className="mt-0.5 text-xs text-slate-600 dark:text-slate-400">
          Pick specific package versions from another conda channel and pull
          them into <code>{channel.name}</code>. Server fetches the bytes once;
          dependencies are <strong>not</strong> auto-resolved (Phase 1) — you'll
          need to import them too if your other channels don't already supply
          them.
        </p>
      </CardHeader>
      <CardBody className="space-y-4">
        {/* Upstream config */}
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_140px]">
          <label className="block">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Upstream URL
            </span>
            <Input
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder={DEFAULT_UPSTREAM}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Subdir
            </span>
            <select
              value={subdir}
              onChange={(e) => {
                setSubdir(e.target.value as Subdir);
                setPickedName(null);
                setPickedFiles(new Set());
              }}
              className="block w-full cursor-pointer rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
            >
              {SUBDIRS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
        </div>

        {/* Search */}
        <Input
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPickedName(null);
            setPickedFiles(new Set());
          }}
          placeholder="Search package name (min 2 chars)…"
        />

        {searchQ.error && <ErrorState error={searchQ.error} />}

        {searchQ.data && debouncedSearch.length >= 2 && (
          <div className="rounded-md border border-slate-200 dark:border-slate-800">
            <div className="border-b border-slate-100 px-3 py-1.5 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
              {searchQ.data.matched === 0
                ? "No matches."
                : `${searchQ.data.matched} match${searchQ.data.matched === 1 ? "" : "es"}` +
                  (searchQ.data.truncated
                    ? ` (showing first ${searchQ.data.packages.length} — refine to narrow)`
                    : "")}
            </div>
            <ul className="max-h-48 divide-y divide-slate-100 overflow-y-auto dark:divide-slate-800">
              {searchQ.data.packages.map((p) => (
                <li key={p.name}>
                  <button
                    type="button"
                    onClick={() => {
                      setPickedName(p.name);
                      setPickedFiles(new Set());
                    }}
                    className={`flex w-full cursor-pointer items-center justify-between gap-3 px-3 py-1.5 text-left text-xs hover:bg-slate-50 dark:hover:bg-slate-800/60 ${
                      pickedName === p.name ? "bg-brand-50 dark:bg-brand-500/15" : ""
                    }`}
                  >
                    <span className="font-mono text-slate-900 dark:text-slate-100">{p.name}</span>
                    {p.in_channels.length > 0 && (
                      <span className="shrink-0 rounded bg-brand-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-brand-800 dark:bg-brand-500/15 dark:text-brand-200">
                        in {p.in_channels.join(", ")}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {versionsQ.error && <ErrorState error={versionsQ.error} />}

        {pickedName && versionsQ.data && (
          <div className="space-y-2 rounded-md border border-slate-200 p-3 dark:border-slate-800">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Versions of <code className="font-mono text-slate-700 dark:text-slate-300">{pickedName}</code>
              </h3>
              <span className="text-xs text-slate-500 dark:text-slate-400">
                {versionsQ.data.versions.length} entries
                {versionsQ.data.truncated && " (truncated)"}
              </span>
            </div>
            <div className="max-h-72 divide-y divide-slate-100 overflow-y-auto dark:divide-slate-800">
              {versionsQ.data.versions.map((v) => (
                <VersionRow
                  key={v.filename}
                  v={v}
                  picked={pickedFiles.has(v.filename)}
                  onToggle={() => togglePicked(v.filename)}
                />
              ))}
            </div>
          </div>
        )}

        {pickedFiles.size > 0 && pickedHasNoarch && (
          <div className="space-y-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs dark:border-amber-900/60 dark:bg-amber-950/20">
            <div>
              <strong className="text-amber-900 dark:text-amber-200">noarch deps need a target platform.</strong>{" "}
              <span className="text-amber-800 dark:text-amber-300">
                Pick which platforms' transitive deps to resolve and import.
              </span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {TARGET_PLATFORMS.map((p) => {
                const on = targetPlatforms.has(p);
                return (
                  <button
                    key={p}
                    type="button"
                    onClick={() => toggleTargetPlatform(p)}
                    className={`cursor-pointer rounded-full border px-2.5 py-0.5 font-mono text-[11px] ${
                      on
                        ? "border-amber-400 bg-amber-200 text-amber-900 dark:border-amber-700 dark:bg-amber-500/20 dark:text-amber-200"
                        : "border-slate-300 bg-white text-slate-600 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-400 dark:hover:bg-slate-800"
                    }`}
                  >
                    {p}
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {pickedFiles.size > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-brand-200 bg-brand-50 px-3 py-2 text-xs dark:border-brand-700/60 dark:bg-brand-500/10">
            <span className="text-brand-900 dark:text-brand-200">
              <strong>{pickedFiles.size}</strong> selected · {formatSize(totalPickedBytes)} total
            </span>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setPickedFiles(new Set())}
                disabled={importer.isPending}
              >
                Clear
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={handleStartImport}
                loading={preview.isPending || importer.isPending}
                disabled={pickedHasNoarch && targetPlatforms.size === 0}
              >
                Import {pickedFiles.size}…
              </Button>
            </div>
          </div>
        )}

        {preview.error && <ErrorState error={preview.error} />}
        {importer.error && <ErrorState error={importer.error} />}

        {previewOpen && preview.data && (
          <PreviewModal
            data={preview.data}
            importing={importer.isPending}
            error={importer.error}
            onCancel={handleCancelPreview}
            onConfirm={handleConfirmImport}
          />
        )}

        {activeJobId !== null && (
          <ImportJobProgressModal
            job={jobQ.data ?? null}
            loading={jobQ.isLoading}
            onClose={handleCloseJob}
          />
        )}
      </CardBody>
    </Card>
  );
}

function VersionRow({
  v,
  picked,
  onToggle,
}: {
  v: UpstreamVersion;
  picked: boolean;
  onToggle: () => void;
}) {
  return (
    <label
      className={`flex cursor-pointer items-center gap-3 px-3 py-2 text-xs hover:bg-slate-50 dark:hover:bg-slate-800/60 ${
        v.in_target_channel ? "opacity-60" : ""
      }`}
    >
      <input
        type="checkbox"
        checked={picked}
        disabled={v.in_target_channel}
        onChange={onToggle}
        className="cursor-pointer rounded border-slate-300 text-brand-600 focus:ring-brand-500 disabled:cursor-not-allowed dark:border-slate-600 dark:bg-slate-900"
      />
      <span className="min-w-0 flex-1">
        <span className="block font-mono text-slate-800 dark:text-slate-200">
          {v.version} <span className="text-slate-500 dark:text-slate-500">·</span>{" "}
          <span className="text-slate-600 dark:text-slate-400">{v.build}</span>
        </span>
        <span className="block truncate text-[11px] text-slate-500 dark:text-slate-400" title={v.filename}>
          {v.filename}
        </span>
      </span>
      <span className="shrink-0 tabular-nums text-slate-500 dark:text-slate-400">
        {formatSize(v.size)}
      </span>
      {v.in_target_channel && (
        <span className="shrink-0 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-600 dark:bg-slate-800 dark:text-slate-400">
          already here
        </span>
      )}
    </label>
  );
}

interface PreviewData {
  upstream_url: string;
  direct_requested: PreviewItem[];
  transitive_new: PreviewItem[];
  transitive_satisfied_locally: PreviewItem[];
  total_new_bytes: number;
}

function PreviewModal({
  data,
  importing,
  error,
  onCancel,
  onConfirm,
}: {
  data: PreviewData;
  importing: boolean;
  error: Error | null;
  onCancel: () => void;
  onConfirm: (chosen: { subdir: string; filename: string }[]) => void | Promise<void>;
}) {
  // Operator can opt-out of individual transitive deps before confirming
  // — they may already have them from another channel and not want a
  // duplicate copy in this one.
  const [skip, setSkip] = useState<Set<string>>(new Set());
  const toggleSkip = (filename: string) =>
    setSkip((prev) => {
      const next = new Set(prev);
      if (next.has(filename)) next.delete(filename);
      else next.add(filename);
      return next;
    });

  const directCount = data.direct_requested.length;
  const directBytes = data.direct_requested.reduce(
    (acc, p) => acc + (p.size ?? 0), 0,
  );
  const newKept = data.transitive_new.filter((p) => !skip.has(p.filename));
  const newSkipped = data.transitive_new.length - newKept.length;
  const newBytes = newKept.reduce((acc, p) => acc + (p.size ?? 0), 0);
  const totalDownloadBytes = directBytes + newBytes;
  const localCount = data.transitive_satisfied_locally.length;

  const submit = () => {
    const chosen = [
      ...data.direct_requested,
      ...newKept,
    ].map((p) => ({ subdir: p.subdir, filename: p.filename }));
    onConfirm(chosen);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 dark:bg-black/70">
      <div className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-lg bg-white shadow-xl dark:bg-slate-900 dark:shadow-black/40">
        <div className="border-b border-slate-200 px-5 py-3 dark:border-slate-800">
          <h2 className="text-base font-semibold">Confirm import</h2>
          <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
            Resolved against <code className="break-all">{data.upstream_url}</code>
          </p>
        </div>

        <div className="flex-1 space-y-5 overflow-y-auto p-5 text-sm">
          <PreviewSection
            title={`Direct picks (${directCount})`}
            description="The packages you selected. These will always be imported."
            items={data.direct_requested}
            badge="picked"
          />

          <PreviewSection
            title={`Transitive deps to import (${data.transitive_new.length})`}
            description={
              data.transitive_new.length === 0
                ? "Every dep is already in this channel — no extras to fetch."
                : "Dependencies the solver pulled in that aren't in this channel yet. Uncheck any you'd rather supply from elsewhere."
            }
            items={data.transitive_new}
            checkable
            skip={skip}
            onToggleSkip={toggleSkip}
          />

          {localCount > 0 && (
            <PreviewSection
              title={`Already in this channel (${localCount})`}
              description="The solver picked these versions; they're already imported. Skipping them automatically."
              items={data.transitive_satisfied_locally}
              muted
            />
          )}
        </div>

        <div className="space-y-2 border-t border-slate-200 bg-slate-50 px-5 py-3 text-xs dark:border-slate-800 dark:bg-slate-900/80">
          {error && (
            <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
              {error.message}
            </div>
          )}
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="text-slate-600 dark:text-slate-400">
              <strong>{directCount + newKept.length}</strong> packages will be fetched
              {" · "}
              <strong>{formatSize(totalDownloadBytes)}</strong>
              {newSkipped > 0 && ` (${newSkipped} skipped)`}
            </div>
            <div className="flex gap-2">
              <Button
                variant="secondary"
                size="sm"
                onClick={onCancel}
                disabled={importing}
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={submit}
                loading={importing}
              >
                Import {directCount + newKept.length}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function PreviewSection({
  title,
  description,
  items,
  badge,
  checkable = false,
  skip,
  onToggleSkip,
  muted = false,
}: {
  title: string;
  description: string;
  items: PreviewItem[];
  badge?: string;
  checkable?: boolean;
  skip?: Set<string>;
  onToggleSkip?: (filename: string) => void;
  muted?: boolean;
}) {
  return (
    <div>
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {title}
      </h3>
      <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">{description}</p>
      {items.length > 0 && (
        <ul
          className={`mt-2 max-h-56 divide-y divide-slate-100 overflow-y-auto rounded-md border border-slate-200 dark:divide-slate-800 dark:border-slate-800 ${
            muted ? "opacity-70" : ""
          }`}
        >
          {items.map((p) => {
            const isSkipped = skip?.has(p.filename) ?? false;
            return (
              <li
                key={`${p.subdir}/${p.filename}`}
                className="flex items-center gap-3 px-3 py-1.5 text-xs"
              >
                {checkable && onToggleSkip && (
                  <input
                    type="checkbox"
                    checked={!isSkipped}
                    onChange={() => onToggleSkip(p.filename)}
                    className="cursor-pointer rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-900"
                  />
                )}
                <div className="min-w-0 flex-1">
                  <div className="font-mono text-slate-800 dark:text-slate-200">
                    {p.name} <span className="text-slate-500">·</span>{" "}
                    {p.version}{" "}
                    <span className="text-slate-500">·</span>{" "}
                    <span className="text-slate-600 dark:text-slate-400">{p.build}</span>
                  </div>
                  <div className="truncate text-[11px] text-slate-500 dark:text-slate-400" title={p.filename}>
                    {p.subdir}/{p.filename}
                  </div>
                </div>
                <span className="shrink-0 tabular-nums text-slate-500 dark:text-slate-400">
                  {p.size != null ? formatSize(p.size) : "—"}
                </span>
                {badge && (
                  <span className="shrink-0 rounded bg-brand-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-brand-800 dark:bg-brand-500/15 dark:text-brand-200">
                    {badge}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function ImportJobProgressModal({
  job,
  loading,
  onClose,
}: {
  job: ImportJob | null;
  loading: boolean;
  onClose: () => void;
}) {
  const isTerminal = job
    ? job.status === "completed" || job.status === "failed"
    : false;
  const total = job?.total_count ?? 0;
  const done = (job?.completed_count ?? 0) + (job?.failed_count ?? 0);
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const failedRows = (job?.results ?? []).filter((r) => r.status === "error");

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4 dark:bg-black/70">
      <div className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg bg-white shadow-xl dark:bg-slate-900 dark:shadow-black/40">
        <div className="border-b border-slate-200 px-5 py-3 dark:border-slate-800">
          <h2 className="text-base font-semibold">
            {job?.status === "completed"
              ? "Import complete"
              : job?.status === "failed"
                ? "Import failed"
                : "Importing…"}
          </h2>
          {job && (
            <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400">
              From <code className="break-all">{job.upstream_url}</code>
            </p>
          )}
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto p-5 text-sm">
          {!job && loading && (
            <p className="text-xs text-slate-500 dark:text-slate-400">Starting job…</p>
          )}

          {job && (
            <>
              <div className="space-y-1.5">
                <div className="flex items-baseline justify-between text-xs text-slate-600 dark:text-slate-400">
                  <span>
                    <strong className="text-slate-900 dark:text-slate-100">{done}</strong> of {total} ·{" "}
                    {formatSize(job.written_bytes)}
                  </span>
                  <span className="tabular-nums">{pct}%</span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
                  <div
                    className={`h-full transition-[width] duration-300 ${
                      job.status === "failed"
                        ? "bg-red-500"
                        : isTerminal
                          ? "bg-brand-500"
                          : "bg-brand-400"
                    }`}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                {!isTerminal && job.current_filename && (
                  <p
                    className="truncate text-[11px] text-slate-500 dark:text-slate-400"
                    title={job.current_filename}
                  >
                    Working on <code>{job.current_filename}</code>
                  </p>
                )}
              </div>

              {job.error && (
                <div className="rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
                  {job.error}
                </div>
              )}

              {failedRows.length > 0 && (
                <div className="space-y-1">
                  <h3 className="text-xs font-semibold uppercase tracking-wide text-red-700 dark:text-red-400">
                    Failed ({failedRows.length})
                  </h3>
                  <ul className="divide-y divide-red-100 rounded-md border border-red-200 bg-red-50/50 dark:divide-red-900 dark:border-red-900 dark:bg-red-950/30">
                    {failedRows.map((r) => (
                      <li
                        key={`${r.subdir}/${r.filename}`}
                        className="flex items-baseline justify-between gap-3 px-3 py-1.5 text-xs"
                      >
                        <code className="truncate font-mono text-slate-800 dark:text-slate-200">
                          {r.subdir}/{r.filename}
                        </code>
                        <span
                          className="shrink-0 text-red-700 dark:text-red-400"
                          title={r.error}
                        >
                          {r.error ?? "error"}
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-slate-200 bg-slate-50 px-5 py-3 text-xs dark:border-slate-800 dark:bg-slate-900/80">
          <Button
            variant={isTerminal ? "primary" : "secondary"}
            size="sm"
            onClick={onClose}
          >
            {isTerminal ? "Done" : "Hide"}
          </Button>
        </div>
      </div>
    </div>
  );
}

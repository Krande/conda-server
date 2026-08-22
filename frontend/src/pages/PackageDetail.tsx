import { Fragment, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState, ErrorState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { PageSpinner } from "@/components/ui/Spinner";
import { InstallInstructions } from "@/components/InstallInstructions";
import { packageDownloadUrl, type StorageBackend } from "@/lib/api";
import {
  useAbout,
  useChannel,
  useDeletePackageVersion,
  usePackage,
  useResolvePackages,
} from "@/lib/queries";
import type { PackageVersion } from "@/lib/types";
import type { CondaFileEntry, CondaFilesResult } from "@/lib/condaFiles";

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

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

type SortKey = "version" | "build" | "subdir" | "size" | "added";
type SortDir = "asc" | "desc";
interface SortState {
  key: SortKey;
  dir: SortDir;
}

/** Direction a column starts in when you first click it.
 *
 *  Newest-first for the two time-ish columns and biggest-first for size
 *  — the interesting end of each — but A→Z for the text columns, where
 *  ascending is what people expect.
 */
const INITIAL_DIR: Record<SortKey, SortDir> = {
  version: "desc",
  build: "asc",
  subdir: "asc",
  size: "desc",
  added: "desc",
};

/** Compare two rows *ascending* by the given column.
 *
 *  Version ordering is not computed here: conda's rules (epochs,
 *  `.post` / `.dev`, `2.31` == `2.31.0`, segment-wise numeric compare)
 *  are subtle enough that reimplementing them in the browser would be a
 *  second thing to keep correct. The server ranks the versions with
 *  rattler and sends the rank as `version_order` (0 = newest), so this
 *  is a numeric compare — inverted, because ascending *version* means
 *  descending rank.
 *
 *  Missing values (no size, no date — both happen on mirror channels)
 *  sort last in whichever direction is active, so a column of blanks
 *  never pushes real rows off the top.
 */
function compareAsc(key: SortKey, a: PackageVersion, b: PackageVersion): number {
  switch (key) {
    case "version":
      return (b.version_order ?? 0) - (a.version_order ?? 0);
    case "build":
      return a.build.localeCompare(b.build);
    case "subdir":
      return a.subdir.localeCompare(b.subdir);
    case "size":
      return nullsLast(a.size, b.size);
    case "added":
      return nullsLast(dateValue(a.created_at), dateValue(b.created_at));
  }
}

function dateValue(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? null : t;
}

function nullsLast(a: number | null | undefined, b: number | null | undefined): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return a - b;
}

function SortHeader({
  label,
  sortKey,
  sort,
  onSort,
  align = "left",
}: {
  label: string;
  sortKey: SortKey;
  sort: SortState;
  onSort: (key: SortKey) => void;
  align?: "left" | "right";
}) {
  const active = sort.key === sortKey;
  return (
    <th
      className={`px-5 py-3 font-medium ${align === "right" ? "text-right" : ""}`}
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
    >
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={`group inline-flex cursor-pointer items-center gap-1 uppercase tracking-wide transition-colors hover:text-slate-900 dark:hover:text-slate-100 ${
          active ? "text-slate-900 dark:text-slate-100" : ""
        }`}
        title={`Sort by ${label.toLowerCase()}`}
      >
        {label}
        <span
          aria-hidden
          className={`text-[9px] leading-none transition-opacity ${
            active
              ? "text-brand-700 dark:text-brand-400"
              : "text-slate-400 opacity-0 group-hover:opacity-100 dark:text-slate-500"
          }`}
        >
          {active && sort.dir === "asc" ? "▲" : "▼"}
        </span>
      </button>
    </th>
  );
}

/** Strip the leading package name from a conda match-spec.
 *
 *  Match-specs look like "numpy", "numpy >=1.0", "openssl >=3.5,<4.0a0",
 *  or occasionally with bracketed properties like
 *  "numpy[version='>=1.0']" (rare). The first non-space token up to a
 *  space, operator, or bracket is always the name; everything after is
 *  the version/build constraint we surface as a pill. If parsing fails
 *  we still render the original text so nothing is silently dropped.
 */
function parseDepSpec(spec: string): { name: string; constraint: string } {
  const match = spec.match(/^([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(.*)$/);
  if (!match) return { name: spec, constraint: "" };
  return { name: match[1], constraint: match[2].trim() };
}

// Only used as the fallback target for *dependency* links: a dep this
// server doesn't host (python, orjson, …) really does live on conda-forge,
// so pointing there is useful. The package being viewed is by definition
// hosted here, so its own header never links out — see the header below.
const condaForgeUrl = (name: string) =>
  `https://anaconda.org/channels/conda-forge/packages/${encodeURIComponent(name)}/overview`;

export default function PackageDetail() {
  const { channel, name } = useParams<{ channel: string; name: string }>();
  const { data, isLoading, error } = usePackage(channel, name);
  const channelQ = useChannel(channel);
  const del = useDeletePackageVersion(channel ?? "", name ?? "");
  // Per-row expansion state. Identified by the synthetic key the rows
  // already use (subdir + filename is unique within a package).
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  // Default matches what the server already sends — newest version
  // first — so the first paint needs no reordering.
  const [sort, setSort] = useState<SortState>({ key: "version", dir: "desc" });

  const versions = data?.versions;
  const sortedVersions = useMemo(() => {
    if (!versions) return [];
    // Decorate with the server's index so ties always fall back to the
    // canonical order (build number descending, then subdir/build)
    // instead of flipping when the direction flips.
    const rows = versions.map((v, i) => ({ v, i }));
    const mul = sort.dir === "asc" ? 1 : -1;
    rows.sort((x, y) => {
      const primary = compareAsc(sort.key, x.v, y.v) * mul;
      return primary !== 0 ? primary : x.i - y.i;
    });
    return rows.map((r) => r.v);
  }, [versions, sort]);

  if (isLoading) return <PageSpinner />;
  if (error) return <ErrorState error={error} />;
  if (!data || !channel) return <EmptyState title="Package not found" />;

  const myRole = channelQ.data?.my_role;
  const canDelete =
    (myRole === "writer" || myRole === "owner" || myRole === "admin") &&
    !channelQ.data?.mirror_url;

  const toggle = (key: string) =>
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

  // Clicking the active column reverses it; clicking a new one starts
  // from that column's natural direction.
  const handleSort = (key: SortKey) =>
    setSort((prev) =>
      prev.key === key
        ? { key, dir: prev.dir === "asc" ? "desc" : "asc" }
        : { key, dir: INITIAL_DIR[key] },
    );

  const handleDelete = (subdir: string, filename: string) => {
    if (!window.confirm(`Delete ${filename}? The bytes and row go now; repodata updates after the background reindex.`)) {
      return;
    }
    del.mutate({ subdir, filename });
  };

  return (
    <div className="space-y-8">
      <div>
        <Link to={`/channels/${channel}`} className="text-sm text-brand-700 hover:underline dark:text-brand-400">
          ← Back to {channel}
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">{data.name}</h1>
      </div>

      {data.description && (
        <p className="max-w-3xl text-sm leading-relaxed text-slate-600 dark:text-slate-400">
          {data.description}
        </p>
      )}

      <section>
        <h2 className="mb-3 text-lg font-semibold">Install</h2>
        <InstallInstructions channel={channel} packageName={data.name} />
      </section>

      <section>
        <h2 className="mb-3 text-lg font-semibold">
          Versions
          <span className="ml-2 text-sm font-normal text-slate-500 dark:text-slate-400">
            ({data.versions.length})
          </span>
        </h2>
        {del.error && <ErrorState error={del.error} />}
        {data.versions.length === 0 ? (
          <EmptyState title="No versions indexed yet" />
        ) : (
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] border-collapse text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:text-slate-400">
                    <SortHeader label="Version" sortKey="version" sort={sort} onSort={handleSort} />
                    <SortHeader label="Build" sortKey="build" sort={sort} onSort={handleSort} />
                    <SortHeader label="Subdir" sortKey="subdir" sort={sort} onSort={handleSort} />
                    <SortHeader
                      label="Size"
                      sortKey="size"
                      sort={sort}
                      onSort={handleSort}
                      align="right"
                    />
                    <SortHeader label="Added" sortKey="added" sort={sort} onSort={handleSort} />
                    <th className="px-5 py-3 font-medium">Download</th>
                    {canDelete && <th className="px-5 py-3 text-right font-medium">Action</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {sortedVersions.map((v) => {
                    const key = `${v.subdir}-${v.filename}`;
                    const isOpen = !!expanded[key];
                    const colSpan = canDelete ? 7 : 6;
                    return (
                      <Fragment key={key}>
                        <tr
                          className={`transition-colors ${
                            isOpen
                              ? "bg-slate-50 dark:bg-slate-800/40"
                              : "hover:bg-slate-50 dark:hover:bg-slate-800/40"
                          }`}
                        >
                          <td className="px-5 py-3 align-baseline">
                            <button
                              type="button"
                              onClick={() => toggle(key)}
                              className="flex cursor-pointer items-baseline gap-2 font-mono font-medium text-slate-900 hover:text-brand-700 dark:text-slate-100 dark:hover:text-brand-400"
                              aria-expanded={isOpen}
                              aria-label={`${isOpen ? "Collapse" : "Expand"} details for ${v.version}`}
                            >
                              <span
                                aria-hidden
                                className={`inline-block text-xs text-slate-400 transition-transform dark:text-slate-500 ${
                                  isOpen ? "rotate-90" : ""
                                }`}
                              >
                                ▸
                              </span>
                              {v.version}
                            </button>
                          </td>
                          <td className="px-5 py-3 align-baseline font-mono text-xs text-slate-600 dark:text-slate-400">
                            {v.build}
                          </td>
                          <td className="px-5 py-3 align-baseline text-slate-700 dark:text-slate-300">
                            {v.subdir}
                          </td>
                          <td className="px-5 py-3 align-baseline text-right tabular-nums text-slate-600 dark:text-slate-400">
                            {formatSize(v.size)}
                          </td>
                          <td
                            className="whitespace-nowrap px-5 py-3 align-baseline tabular-nums text-slate-600 dark:text-slate-400"
                            title={v.created_at ? new Date(v.created_at).toLocaleString() : undefined}
                          >
                            {formatDate(v.created_at)}
                          </td>
                          <td className="px-5 py-3 align-baseline">
                            <a
                              href={packageDownloadUrl(channel, v.subdir, v.filename)}
                              className="inline-block max-w-[260px] truncate align-bottom font-mono text-xs text-brand-700 hover:underline dark:text-brand-400"
                              title={v.filename}
                            >
                              {v.filename}
                            </a>
                          </td>
                          {canDelete && (
                            <td className="px-5 py-3 text-right align-baseline">
                              <Button
                                variant="danger"
                                size="sm"
                                onClick={() => handleDelete(v.subdir, v.filename)}
                                loading={
                                  del.isPending &&
                                  del.variables?.subdir === v.subdir &&
                                  del.variables?.filename === v.filename
                                }
                              >
                                Delete
                              </Button>
                            </td>
                          )}
                        </tr>
                        {isOpen && (
                          <tr>
                            <td colSpan={colSpan} className="p-0">
                              <VersionDetails v={v} channel={channel} />
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </section>
    </div>
  );
}

function VersionDetails({ v, channel }: { v: PackageVersion; channel: string }) {
  const depends = v.depends ?? [];
  const constrains = v.constrains ?? [];
  const built = v.timestamp ? new Date(v.timestamp).toLocaleString() : null;

  // Collect unique package names from both dep lists so the link logic
  // can prefer a locally-hosted channel over conda-forge. Names are the
  // leading token of a match-spec; see parseDepSpec.
  const depNames = Array.from(
    new Set(
      [...depends, ...constrains].map((spec) => parseDepSpec(spec).name),
    ),
  );
  const resolveQ = useResolvePackages(depNames);
  const resolved = resolveQ.data ?? {};

  return (
    <div className="border-t border-slate-200 bg-slate-50 px-5 py-5 text-sm dark:border-slate-800 dark:bg-slate-900/40">
      <div className="grid gap-6 md:grid-cols-2">
        <DepList
          title={`Depends (${depends.length})`}
          items={depends}
          resolved={resolved}
        />
        <DepList
          title={`Run constraints (${constrains.length})`}
          items={constrains}
          emptyText="—"
          resolved={resolved}
        />
      </div>
      <dl className="mt-4 grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
        {v.license && (
          <MetaRow label="License">
            <span>{v.license}</span>
          </MetaRow>
        )}
        {built && <MetaRow label="Built">{built}</MetaRow>}
        {v.sha256 && (
          <MetaRow label="sha256">
            <code className="break-all font-mono text-[11px]">{v.sha256}</code>
          </MetaRow>
        )}
        {v.md5 && (
          <MetaRow label="md5">
            <code className="break-all font-mono text-[11px]">{v.md5}</code>
          </MetaRow>
        )}
        {v.imported_from && (
          <MetaRow label="Imported">
            <a
              href={v.imported_from}
              target="_blank"
              rel="noreferrer"
              className="break-all text-brand-700 hover:underline dark:text-brand-400"
              title={v.imported_from}
            >
              from upstream ↗
            </a>
          </MetaRow>
        )}
      </dl>
      <FilesSection channel={channel} v={v} />
    </div>
  );
}

type FilesPhase = "idle" | "loading" | "ready" | "error";

interface FilesState {
  phase: FilesPhase;
  result?: CondaFilesResult;
  error?: string;
  // True when the failure was a browser-level fetch error (network or,
  // most often, a cross-origin storage response blocked for want of a
  // CORS rule). Drives the actionable CORS hint below.
  networkError?: boolean;
}

function FilesSection({ channel, v }: { channel: string; v: PackageVersion }) {
  const [state, setState] = useState<FilesState>({ phase: "idle" });
  const [filter, setFilter] = useState("");
  // Only fetch /about once we've actually hit an error — the backend
  // type only matters for tailoring the CORS hint, and the result is
  // cached (shared with the About page) so a retry costs nothing.
  const about = useAbout(state.phase === "error");
  const storageBackend = about.data?.storage_backend;

  // .tar.bz2 needs a different decompressor than zstd and the client-side
  // parser would bloat for a deprecated format. Hide the action there and
  // direct users back to the filename if they want to pull bytes.
  if (!v.filename.endsWith(".conda")) {
    return null;
  }

  const load = async () => {
    setState({ phase: "loading" });
    try {
      const mod = await import("@/lib/condaFiles");
      const url = packageDownloadUrl(channel, v.subdir, v.filename);
      const result = await mod.listCondaFiles(url);
      setState({ phase: "ready", result });
    } catch (err) {
      // Duck-typed rather than instanceof CondaFilesFetchError so we don't
      // pull the (lazy) condaFiles module into this error path, and so it
      // survives the dynamic-import boundary. Matches isNetworkFetchError
      // in condaFiles.ts.
      const networkError =
        typeof err === "object" &&
        err !== null &&
        (err as { kind?: unknown }).kind === "network";
      setState({
        phase: "error",
        error: err instanceof Error ? err.message : String(err),
        networkError,
      });
    }
  };

  const files = state.result?.files ?? [];
  const filtered = filter
    ? files.filter((f) => f.path.toLowerCase().includes(filter.toLowerCase()))
    : files;

  return (
    <div className="mt-4 border-t border-slate-200 pt-4 dark:border-slate-800">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
          Files
        </h3>
        {state.phase === "idle" && (
          <Button variant="secondary" size="sm" onClick={load}>
            Show files
          </Button>
        )}
        {state.phase === "loading" && (
          <span className="text-xs text-slate-500 dark:text-slate-400">Fetching + unpacking…</span>
        )}
        {state.phase === "ready" && state.result && (
          <>
            <span className="text-xs text-slate-500 dark:text-slate-400">
              {state.result.files.length} entries ·{" "}
              {(state.result.totalBytes / (1024 * 1024)).toFixed(1)} MB downloaded
            </span>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setState({ phase: "idle" });
                setFilter("");
              }}
            >
              Hide
            </Button>
          </>
        )}
      </div>

      {state.phase === "ready" && state.result && (
        <div className="mt-3 space-y-4">
          {state.result.runExports && (
            <RunExportsPanel exports={state.result.runExports} />
          )}
          <div>
            <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
              Paths ({state.result.files.length})
            </h4>
            <Input
              placeholder="Filter paths…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              className="text-xs"
            />
            <div className="mt-2 max-h-96 overflow-y-auto rounded-md border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
              {filtered.length === 0 ? (
                <div className="px-3 py-4 text-center text-xs text-slate-500 dark:text-slate-400">
                  No paths match the filter.
                </div>
              ) : (
                <ul className="divide-y divide-slate-100 dark:divide-slate-800">
                  {filtered.map((f) => (
                    <FileRow key={f.path} f={f} />
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      )}

      {state.phase === "error" && (
        <FilesError
          message={state.error ?? "Unknown error"}
          networkError={!!state.networkError}
          backend={storageBackend}
          onRetry={load}
        />
      )}

      {state.phase === "idle" && (
        <p className="mt-1 text-xs text-slate-500 dark:text-slate-400">
          Downloads and unpacks the <code>info/paths.json</code> manifest from
          the archive in your browser — the server isn't involved in the parse.
          Expect a few seconds for a ~100 MB package.
        </p>
      )}
    </div>
  );
}

// Deep-link into the operator docs' CORS troubleshooting section. Points
// at the canonical OSS repo (generic — no deployment-specific host).
const CORS_DOCS_URL =
  "https://github.com/Krande/conda-server/blob/main/docs/deploying.md#troubleshooting-browser-file-listing-cors";

/** Per-backend, one-line remedy for a blocked cross-origin storage fetch.
 *  Returns null when CORS can't be the cause (local backend serves the
 *  bytes same-origin) so the caller falls back to a plain network hint. */
function corsRemedy(backend: StorageBackend | undefined, origin: string): string | null {
  switch (backend) {
    case "local":
      // Local backend streams through the app itself — same origin, so a
      // failure here is a genuine network/server problem, not CORS.
      return null;
    case "s3":
      return `The storage backend is S3-compatible. Add a bucket CORS rule allowing GET/HEAD from ${origin} (PutBucketCors, e.g. \`aws s3api put-bucket-cors\`).`;
    case "azure":
      return `The storage backend is Azure Blob. Add an account-level CORS rule allowing GET/HEAD from ${origin} (e.g. \`az storage cors add --services b --methods GET HEAD --origins ${origin}\`).`;
    case "gcs":
      return `The storage backend is Google Cloud Storage. Set a bucket CORS rule allowing GET/HEAD from ${origin} (e.g. \`gcloud storage buckets update --cors-file\`).`;
    default:
      // Backend unknown (e.g. /about hasn't loaded) — stay storage-agnostic.
      return `If the server uses cloud object storage (S3 / Azure Blob / GCS), its bucket/account may need a CORS rule allowing this origin (${origin}).`;
  }
}

function FilesError({
  message,
  networkError,
  backend,
  onRetry,
}: {
  message: string;
  networkError: boolean;
  backend: StorageBackend | undefined;
  onRetry: () => void;
}) {
  const origin = typeof window !== "undefined" ? window.location.origin : "this site's origin";
  const remedy = networkError ? corsRemedy(backend, origin) : null;

  // A plain HTTP/parse error, or a local-backend network blip: show the
  // raw message, nothing to hint at.
  if (!networkError || remedy === null) {
    return (
      <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
        {networkError
          ? "Couldn't load files — the request to the server failed. Check your connection and retry."
          : message}
        {networkError && (
          <button
            type="button"
            onClick={onRetry}
            className="ml-2 underline hover:no-underline"
          >
            Retry
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="mt-3 space-y-1.5 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200">
      <p className="font-medium">Couldn't load files — the download was blocked by the browser.</p>
      <p>
        The file list is unpacked in your browser, so it fetches the package
        directly from the server's object storage. That fetch failed the way a
        missing CORS rule looks (browsers hide the exact reason). {remedy}
      </p>
      <p>
        See the{" "}
        <a
          href={CORS_DOCS_URL}
          target="_blank"
          rel="noreferrer"
          className="font-medium underline hover:no-underline"
        >
          CORS troubleshooting docs ↗
        </a>{" "}
        for per-backend steps, then{" "}
        <button type="button" onClick={onRetry} className="underline hover:no-underline">
          retry
        </button>
        .
      </p>
      <p className="text-amber-700/80 dark:text-amber-300/70">
        <span className="font-mono">{message}</span>
      </p>
    </div>
  );
}

function FileRow({ f }: { f: CondaFileEntry }) {
  return (
    <li className="flex items-baseline justify-between gap-3 px-3 py-1.5 text-xs">
      <code className="truncate font-mono text-slate-800 dark:text-slate-200" title={f.path}>
        {f.path}
      </code>
      <span className="shrink-0 tabular-nums text-slate-500 dark:text-slate-400">
        {f.size != null ? formatSize(f.size) : "—"}
      </span>
    </li>
  );
}

function RunExportsPanel({ exports }: { exports: NonNullable<CondaFilesResult["runExports"]> }) {
  const groups: Array<[string, string[], string]> = [
    ["weak", exports.weak, "Baked into downstream run deps on build-time use."],
    ["strong", exports.strong, "Baked into downstream run deps on runtime use too."],
    [
      "weak_constrains",
      exports.weakConstrains,
      "Added to downstream constrains (soft pins) on build-time use.",
    ],
    [
      "strong_constrains",
      exports.strongConstrains,
      "Added to downstream constrains on runtime use too.",
    ],
    ["noarch", exports.noarch, "Applied only to noarch consumers."],
  ];
  const anyNonEmpty = groups.some(([, items]) => items.length > 0);
  if (!anyNonEmpty) {
    return null;
  }
  return (
    <div>
      <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        Run exports
      </h4>
      <p className="mb-2 text-[11px] text-slate-500 dark:text-slate-400">
        Recipe-level pins propagated to packages that depend on this one at
        build- or runtime. Parsed locally from <code>info/run_exports.json</code>.
      </p>
      <div className="space-y-2 rounded-md border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
        {groups.map(([label, items, explainer]) =>
          items.length === 0 ? null : (
            <div key={label}>
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="font-mono text-[11px] font-semibold text-brand-800 dark:text-brand-300">
                  {label}:
                </span>
                <span className="text-[11px] text-slate-500 dark:text-slate-400">{explainer}</span>
              </div>
              <ul className="mt-1 space-y-0.5 pl-4">
                {items.map((spec) => (
                  <li key={spec} className="font-mono text-[11px] text-slate-800 dark:text-slate-200">
                    - {spec}
                  </li>
                ))}
              </ul>
            </div>
          ),
        )}
      </div>
    </div>
  );
}

function DepList({
  title,
  items,
  emptyText = "No dependencies.",
  resolved,
}: {
  title: string;
  items: string[];
  emptyText?: string;
  resolved: Record<string, { channel: string }>;
}) {
  return (
    <div>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {title}
      </h3>
      {items.length === 0 ? (
        <p className="text-xs text-slate-500 dark:text-slate-400">{emptyText}</p>
      ) : (
        <ul className="flex flex-wrap gap-x-4 gap-y-2">
          {items.map((spec) => {
            const { name, constraint } = parseDepSpec(spec);
            const local = resolved[name];
            const href = local
              ? `/channels/${encodeURIComponent(local.channel)}/packages/${encodeURIComponent(name)}`
              : condaForgeUrl(name);
            const isLocal = !!local;
            return (
              <li key={spec} className="flex items-baseline gap-1.5 text-xs">
                <a
                  href={href}
                  target={isLocal ? undefined : "_blank"}
                  rel={isLocal ? undefined : "noreferrer"}
                  className="font-mono text-brand-700 hover:underline dark:text-brand-400"
                  title={
                    isLocal
                      ? `Open in ${local.channel}`
                      : `View ${name} on conda-forge`
                  }
                >
                  {name}
                </a>
                {isLocal && (
                  <span
                    className="rounded bg-brand-100 px-1 py-0.5 text-[10px] font-medium uppercase tracking-wide text-brand-800 dark:bg-brand-500/15 dark:text-brand-200"
                    title={`Hosted locally in ${local.channel}`}
                  >
                    local
                  </span>
                )}
                {constraint && (
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                    {constraint}
                  </code>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[8ch_1fr] gap-2">
      <dt className="text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="text-slate-700 dark:text-slate-300">{children}</dd>
    </div>
  );
}

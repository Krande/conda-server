import { Fragment, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState, ErrorState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { PageSpinner } from "@/components/ui/Spinner";
import { InstallInstructions } from "@/components/InstallInstructions";
import { packageDownloadUrl } from "@/lib/api";
import {
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

  if (isLoading) return <PageSpinner />;
  if (error) return <ErrorState error={error} />;
  if (!data || !channel) return <EmptyState title="Package not found" />;

  const myRole = channelQ.data?.my_role;
  const canDelete =
    (myRole === "writer" || myRole === "owner" || myRole === "admin") &&
    !channelQ.data?.mirror_url;

  const toggle = (key: string) =>
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));

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
        <div className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">{data.name}</h1>
          <a
            href={condaForgeUrl(data.name)}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-brand-700 hover:underline dark:text-brand-400"
          >
            View on conda-forge ↗
          </a>
        </div>
        {data.description && (
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">{data.description}</p>
        )}
      </div>

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
              <table className="w-full min-w-[640px] border-collapse text-sm">
                <thead>
                  <tr className="border-b border-slate-200 text-left text-xs uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:text-slate-400">
                    <th className="px-5 py-3 font-medium">Version</th>
                    <th className="px-5 py-3 font-medium">Build</th>
                    <th className="px-5 py-3 font-medium">Subdir</th>
                    <th className="px-5 py-3 text-right font-medium">Size</th>
                    <th className="px-5 py-3 font-medium">Download</th>
                    {canDelete && <th className="px-5 py-3 text-right font-medium">Action</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                  {data.versions.map((v) => {
                    const key = `${v.subdir}-${v.filename}`;
                    const isOpen = !!expanded[key];
                    const colSpan = canDelete ? 6 : 5;
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
}

function FilesSection({ channel, v }: { channel: string; v: PackageVersion }) {
  const [state, setState] = useState<FilesState>({ phase: "idle" });
  const [filter, setFilter] = useState("");

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
      setState({ phase: "error", error: err instanceof Error ? err.message : String(err) });
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
        <div className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
          {state.error}
        </div>
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

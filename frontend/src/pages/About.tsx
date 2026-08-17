import { useQuery } from "@tanstack/react-query";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { ErrorState } from "@/components/ui/EmptyState";
import { PageSpinner } from "@/components/ui/Spinner";
import { about, type AboutResponse } from "@/lib/api";

function Row({
  label,
  value,
  mono = true,
}: {
  label: string;
  value: string | number;
  mono?: boolean;
}) {
  return (
    <div className="grid grid-cols-1 gap-1 py-2 sm:grid-cols-[180px_1fr] sm:items-center">
      <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</dt>
      <dd
        className={`break-all text-sm text-slate-800 dark:text-slate-200 ${mono ? "font-mono" : ""}`}
      >
        {value}
      </dd>
    </div>
  );
}

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function shortSha(sha: string): string {
  if (!sha || sha === "unknown") return sha;
  return sha.length > 7 ? sha.slice(0, 7) : sha;
}

function formatBuildDate(raw: string): string {
  if (!raw || raw === "unknown") return raw;
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  // Locale + timezone come from the browser, so a Norwegian visitor
  // sees the build time in Europe/Oslo without us hard-coding it.
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export default function About() {
  const q = useQuery<AboutResponse>({
    queryKey: ["about"] as const,
    queryFn: ({ signal }) => about.get(signal),
    // Build info doesn't change between renders; stats are mildly stale-
    // tolerant. 60s is plenty.
    staleTime: 60_000,
  });

  if (q.isLoading) return <PageSpinner />;
  if (q.error) return <ErrorState error={q.error} />;
  if (!q.data) return null;

  const { build, stats } = q.data;

  return (
    <div className="mx-auto max-w-3xl space-y-4 p-4 sm:p-6">
      <header className="space-y-1">
        <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-100">About</h1>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          Build provenance + headline storage stats for this server.
        </p>
      </header>

      <Card>
        <CardHeader>
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">Build</h2>
        </CardHeader>
        <CardBody>
          <dl className="divide-y divide-slate-100 dark:divide-slate-800">
            <Row label="Version" value={build.version} />
            <Row label="Git commit" value={shortSha(build.git_sha)} />
            <Row label="Build date" value={formatBuildDate(build.build_date)} />
            <Row label="Python" value={build.python_version} />
            <Row label="rattler" value={build.rattler_version} />
            <Row label="Platform" value={build.platform} />
          </dl>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="text-sm font-semibold text-slate-900 dark:text-slate-100">Stats</h2>
        </CardHeader>
        <CardBody>
          <dl className="divide-y divide-slate-100 dark:divide-slate-800">
            <Row label="Channels" value={stats.channels} mono={false} />
            <Row label="Packages" value={stats.packages} mono={false} />
            <Row
              label="Package versions"
              value={stats.package_versions}
              mono={false}
            />
            <Row
              label="Total storage"
              value={formatBytes(stats.total_storage_bytes)}
              mono={false}
            />
            <Row
              label="Import jobs (total)"
              value={stats.import_jobs_total}
              mono={false}
            />
            <Row
              label="Import jobs (running)"
              value={stats.import_jobs_running}
              mono={false}
            />
          </dl>
        </CardBody>
      </Card>
    </div>
  );
}

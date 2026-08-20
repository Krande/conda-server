import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card, CardBody } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { InstallInstructions } from "@/components/InstallInstructions";
import { useChannels, useSearch } from "@/lib/queries";

const MIN_QUERY = 2;

/**
 * Lightweight debounce — just a setTimeout dance to avoid hammering
 * /api/search on every keystroke. 200 ms feels responsive without
 * spamming the backend during fast typing.
 */
function useDebounced<T>(value: T, delayMs = 200): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const h = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(h);
  }, [value, delayMs]);
  return debounced;
}

export default function Home() {
  const { data: channels } = useChannels();
  const channelCount = channels?.length ?? 0;
  const mirrorCount = channels?.filter((c) => c.mirror_url).length ?? 0;
  const privateCount = channels?.filter((c) => c.private).length ?? 0;
  const publicCount = channelCount - privateCount;

  const [q, setQ] = useState("");
  const debouncedQ = useDebounced(q, 200);
  const searchQ = useSearch(debouncedQ);
  const hasQuery = debouncedQ.trim().length >= MIN_QUERY;

  return (
    <div className="space-y-8">
      <section className="relative overflow-hidden rounded-2xl bg-gradient-to-br from-brand-700 to-brand-950 p-8 text-white shadow-sm ring-1 ring-brand-900/40 sm:p-10 dark:shadow-none dark:ring-brand-700/40">
        {/* Decorative wordmark glyph, echoes the packaged-cube motif */}
        <svg
          viewBox="0 0 32 32"
          fill="none"
          aria-hidden="true"
          className="pointer-events-none absolute -right-8 -top-8 h-48 w-48 text-white/10"
        >
          <path d="M16 3 27 9 27 23 16 29 5 23 5 9 Z" stroke="currentColor" strokeWidth="1" />
          <path d="M16 3 16 16 5 9 M16 16 27 9 M16 16 16 29" stroke="currentColor" strokeWidth="1" />
        </svg>

        <div className="relative max-w-2xl">
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">conda-server</h1>
          <p className="mt-3 text-brand-50/90">
            A modern, open-source conda package server built on the rattler ecosystem. Serve
            private or public channels to conda, mamba, and pixi clients from S3, Azure Blob,
            or local storage.
          </p>

          <div className="mt-6">
            <div className="relative">
              <svg
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                aria-hidden="true"
                className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500"
              >
                <circle cx="11" cy="11" r="7" />
                <path d="m21 21-4-4" />
              </svg>
              <Input
                aria-label="Search packages or channels"
                placeholder="Search packages or channels…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
                className="border-transparent bg-white/95 pl-10 text-slate-900 shadow-sm placeholder:text-slate-500"
              />
            </div>
            <p className="mt-2 text-xs text-brand-100/80">
              Type at least {MIN_QUERY} characters. Results respect your channel access.
            </p>
          </div>

          <div className="mt-6 flex flex-wrap gap-3">
            <Link to="/channels">
              <Button variant="secondary" className="border-transparent bg-white text-brand-800 shadow-sm hover:bg-brand-50 dark:bg-white dark:text-brand-800 dark:hover:bg-brand-50">
                Browse channels
              </Button>
            </Link>
            <a href="/docs" target="_blank" rel="noreferrer">
              <Button variant="ghost" className="text-white ring-1 ring-inset ring-white/25 hover:bg-white/10 hover:text-white dark:text-white dark:hover:bg-white/10">
                OpenAPI docs
              </Button>
            </a>
          </div>
        </div>
      </section>

      {hasQuery ? (
        <SearchResultsPanel
          loading={searchQ.isLoading}
          error={searchQ.error}
          results={searchQ.data}
        />
      ) : (
        <>
          <section>
            <Card>
              <CardBody className="grid grid-cols-2 gap-px overflow-hidden rounded-xl bg-slate-200 p-0 sm:grid-cols-4 dark:bg-slate-800">
                <Stat label="Channels" value={channelCount} />
                <Stat label="Public" value={publicCount} />
                <Stat label="Private" value={privateCount} />
                <Stat label="Mirrors" value={mirrorCount} />
              </CardBody>
            </Card>
          </section>

          <section className="grid gap-6 md:grid-cols-2">
            <Card className="min-w-0">
              <CardBody className="flex h-full flex-col space-y-3">
                <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">
                  {channelCount === 0 ? "No channels yet" : `${channelCount} channel${channelCount === 1 ? "" : "s"}`}
                </h2>
                <p className="text-sm text-slate-600 dark:text-slate-400">
                  Channels organize packages. Each channel has its own storage prefix and access rules.
                </p>
                <div className="mt-auto pt-2">
                  <Link
                    to="/channels"
                    className="inline-flex items-center gap-1 text-sm font-medium text-brand-700 hover:underline dark:text-brand-400"
                  >
                    View channels
                    <span aria-hidden="true">→</span>
                  </Link>
                </div>
              </CardBody>
            </Card>

            <Card className="min-w-0">
              <CardBody className="space-y-3">
                <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-100">Quick install</h2>
                <p className="text-sm text-slate-600 dark:text-slate-400">
                  Any conda-compatible client can consume this server. Point it at a channel URL:
                </p>
                <InstallInstructions channel="<channel>" />
              </CardBody>
            </Card>
          </section>
        </>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="bg-white px-5 py-4 dark:bg-slate-900">
      <div className="text-2xl font-semibold tabular-nums tracking-tight text-slate-900 dark:text-slate-100">
        {value.toLocaleString()}
      </div>
      <div className="mt-1 text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </div>
    </div>
  );
}

function SearchResultsPanel({
  loading,
  error,
  results,
}: {
  loading: boolean;
  error: Error | null;
  results: { packages: { name: string; channel: string }[]; channels: { name: string; description: string | null; private: boolean; mirror_url: string | null }[] } | undefined;
}) {
  if (loading && !results) {
    return <div className="text-sm text-slate-500 dark:text-slate-400">Searching…</div>;
  }
  if (error) {
    return <div className="text-sm text-red-700 dark:text-red-400">Search failed: {error.message}</div>;
  }
  if (!results) return null;
  const { packages, channels } = results;
  const nothing = packages.length === 0 && channels.length === 0;
  if (nothing) {
    return (
      <div className="rounded-md border border-slate-200 bg-white p-6 text-sm text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400">
        No matches. Try a different term, or check that the channel is public or that you're a member.
      </div>
    );
  }

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <Card className="min-w-0">
        <CardBody className="space-y-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Packages ({packages.length})
          </h2>
          {packages.length === 0 ? (
            <div className="text-sm text-slate-500 dark:text-slate-400">No package matches.</div>
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {packages.map((p) => (
                <li key={`${p.channel}/${p.name}`} className="py-2">
                  <Link
                    to={`/channels/${encodeURIComponent(p.channel)}/packages/${encodeURIComponent(p.name)}`}
                    className="flex items-baseline justify-between gap-3 text-sm hover:underline"
                  >
                    <span className="truncate font-medium text-slate-900 dark:text-slate-100">{p.name}</span>
                    <span className="shrink-0 text-xs text-slate-500 dark:text-slate-400">{p.channel}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>

      <Card className="min-w-0">
        <CardBody className="space-y-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
            Channels ({channels.length})
          </h2>
          {channels.length === 0 ? (
            <div className="text-sm text-slate-500 dark:text-slate-400">No channel matches.</div>
          ) : (
            <ul className="divide-y divide-slate-100 dark:divide-slate-800">
              {channels.map((c) => (
                <li key={c.name} className="py-2">
                  <Link
                    to={`/channels/${encodeURIComponent(c.name)}`}
                    className="flex flex-wrap items-center gap-2 text-sm hover:underline"
                  >
                    <span className="font-medium text-slate-900 dark:text-slate-100">{c.name}</span>
                    {c.private && (
                      <span className="rounded bg-slate-900 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-100 dark:bg-slate-700">
                        private
                      </span>
                    )}
                    {c.mirror_url && (
                      <span className="rounded bg-brand-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-brand-800 dark:bg-brand-500/15 dark:text-brand-200">
                        mirror
                      </span>
                    )}
                  </Link>
                  {c.description && (
                    <p className="mt-0.5 truncate text-xs text-slate-500 dark:text-slate-400">{c.description}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>
    </div>
  );
}

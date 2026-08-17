import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card, CardBody } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
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

  const [q, setQ] = useState("");
  const debouncedQ = useDebounced(q, 200);
  const searchQ = useSearch(debouncedQ);
  const hasQuery = debouncedQ.trim().length >= MIN_QUERY;

  return (
    <div className="space-y-10">
      <section className="rounded-lg bg-gradient-to-br from-brand-700 to-brand-900 p-8 text-white shadow-sm dark:from-brand-800 dark:to-brand-950 dark:shadow-none dark:ring-1 dark:ring-brand-700/40">
        <h1 className="text-3xl font-semibold tracking-tight">conda-server</h1>
        <p className="mt-2 max-w-2xl text-brand-50">
          A modern, open-source conda package server built on the rattler ecosystem. Serve
          private or public channels to conda, mamba, and pixi clients from S3, Azure Blob,
          or local storage.
        </p>
        <div className="mt-6 max-w-2xl">
          <Input
            placeholder="Search packages or channels…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            className="bg-white/95 text-slate-900 placeholder:text-slate-500"
          />
          <p className="mt-1 text-xs text-brand-100">
            Type at least {MIN_QUERY} characters. Results respect your channel access.
          </p>
        </div>
        <div className="mt-6 flex flex-wrap gap-3">
          <Link to="/channels">
            <Button variant="secondary">Browse channels</Button>
          </Link>
          <a href="/docs" target="_blank" rel="noreferrer">
            <Button variant="ghost" className="text-white hover:bg-white/10">
              OpenAPI docs
            </Button>
          </a>
        </div>
      </section>

      {hasQuery ? (
        <SearchResultsPanel
          loading={searchQ.isLoading}
          error={searchQ.error}
          results={searchQ.data}
        />
      ) : (
        <section className="grid gap-6 md:grid-cols-2">
          <Card>
            <CardBody className="space-y-2">
              <h2 className="text-lg font-semibold">
                {channelCount === 0 ? "No channels yet" : `${channelCount} channel${channelCount === 1 ? "" : "s"}`}
              </h2>
              <p className="text-sm text-slate-600 dark:text-slate-400">
                Channels organize packages. Each channel has its own storage prefix and access rules.
              </p>
              <div className="pt-2">
                <Link to="/channels" className="text-sm font-medium text-brand-700 hover:underline dark:text-brand-400">
                  View channels →
                </Link>
              </div>
            </CardBody>
          </Card>

          <Card>
            <CardBody className="space-y-2">
              <h2 className="text-lg font-semibold">Quick install</h2>
              <p className="text-sm text-slate-600 dark:text-slate-400">
                Any conda-compatible client can consume this server. Point it at a channel URL:
              </p>
              <code className="block overflow-x-auto rounded bg-slate-900 px-3 py-2 text-xs text-slate-100 dark:bg-slate-950 dark:ring-1 dark:ring-slate-800">
                pixi add --channel {window.location.origin}/{"<channel>"} {"<package>"}
              </code>
            </CardBody>
          </Card>
        </section>
      )}
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
      <Card>
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

      <Card>
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

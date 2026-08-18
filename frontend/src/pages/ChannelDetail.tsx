import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Input } from "@/components/ui/Input";
import { EmptyState, ErrorState } from "@/components/ui/EmptyState";
import { PageSpinner } from "@/components/ui/Spinner";
import { InstallInstructions } from "@/components/InstallInstructions";
import { ChannelAdminCard } from "@/components/ChannelAdminCard";
import { ChannelMembersCard } from "@/components/ChannelMembersCard";
import { ImportFromUpstreamCard } from "@/components/ImportFromUpstreamCard";
import { PackageList } from "@/components/PackageList";
import { UploadPackageCard } from "@/components/UploadPackageCard";
import { useChannel, usePackages } from "@/lib/queries";

export default function ChannelDetail() {
  const { channel: name } = useParams<{ channel: string }>();
  const [filter, setFilter] = useState("");
  const channelQ = useChannel(name);
  const packagesQ = usePackages(name);

  if (channelQ.isLoading) return <PageSpinner />;
  if (channelQ.error) return <ErrorState error={channelQ.error} />;
  if (!channelQ.data || !name) return <EmptyState title="Channel not found" />;

  const channel = channelQ.data;
  const canManage = channel.my_role === "owner" || channel.my_role === "admin";
  const pkgs = (packagesQ.data ?? []).filter((p) =>
    !filter || p.name.toLowerCase().includes(filter.toLowerCase()),
  );

  return (
    <div className="space-y-10">
      <div>
        <Link
          to="/channels"
          className="text-sm font-medium text-brand-700 hover:underline dark:text-brand-400"
        >
          ← Back to channels
        </Link>
        <div className="mt-3 flex flex-wrap items-center gap-2.5">
          <h1 className="text-2xl font-semibold tracking-tight">{channel.name}</h1>
          {channel.private && (
            <span className="inline-flex items-center rounded-md bg-slate-900 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-white dark:bg-slate-800 dark:text-slate-300 dark:ring-1 dark:ring-inset dark:ring-slate-700">
              private
            </span>
          )}
          {channel.mirror_url && (
            <span
              className="inline-flex items-center rounded-md bg-brand-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-brand-700 dark:text-brand-400"
              title={`Upstream: ${channel.mirror_url}`}
            >
              mirror
            </span>
          )}
        </div>
        {channel.description && (
          <p className="mt-2 max-w-2xl text-sm text-slate-600 dark:text-slate-400">
            {channel.description}
          </p>
        )}
        {channel.mirror_url && (
          <div className="mt-4 rounded-lg border border-brand-500/30 bg-brand-500/5 px-4 py-3 text-sm text-brand-900 dark:border-brand-500/25 dark:bg-brand-500/10 dark:text-brand-200">
            Proxying{" "}
            <code className="rounded bg-brand-500/10 px-1.5 py-0.5 font-mono text-xs dark:bg-brand-500/15">
              {channel.mirror_url}
            </code>
            . Packages appear below as clients pull them through this server;
            repodata refreshes every {channel.mirror_cache_seconds}s.
          </div>
        )}
      </div>

      <section>
        <h2 className="mb-3 text-lg font-semibold">Install from this channel</h2>
        <InstallInstructions channel={channel.name} />
      </section>

      <ChannelAdminCard channel={channel} />

      {canManage && <ChannelMembersCard channel={channel} />}

      <UploadPackageCard channel={channel} />

      <ImportFromUpstreamCard channel={channel} />

      <section>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-4">
          <h2 className="text-lg font-semibold">
            Packages
            {packagesQ.data && (
              <span className="ml-2 text-sm font-normal text-slate-500 dark:text-slate-400">
                {filter
                  ? `${pkgs.length} of ${packagesQ.data.length}`
                  : packagesQ.data.length}
              </span>
            )}
          </h2>
          <div className="w-64 max-w-full">
            <Input
              placeholder="Filter packages…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
        </div>

        {packagesQ.isLoading ? (
          <PageSpinner />
        ) : packagesQ.error ? (
          <ErrorState error={packagesQ.error} />
        ) : pkgs.length === 0 ? (
          <EmptyState
            title={
              filter
                ? "No packages match the filter"
                : channel.mirror_url
                  ? "No packages cached yet"
                  : "No packages uploaded yet"
            }
            description={
              filter
                ? "Try a different search term."
                : channel.mirror_url
                  ? "Packages appear here the first time a client pulls them through this server."
                  : canManage
                    ? "Upload a .conda or .tar.bz2 archive via the card above; it's indexed automatically."
                    : "An owner or writer can upload packages to this channel."
            }
          />
        ) : (
          <PackageList channelName={channel.name} packages={pkgs} />
        )}
      </section>
    </div>
  );
}

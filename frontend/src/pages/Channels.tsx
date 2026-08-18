import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, ErrorState } from "@/components/ui/EmptyState";
import { PageSpinner } from "@/components/ui/Spinner";
import { CreateChannelForm } from "@/components/CreateChannelForm";
import { useChannels } from "@/lib/queries";
import { useCurrentUser } from "@/lib/auth";

export default function Channels() {
  const [filter, setFilter] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const { data, isLoading, error } = useChannels();
  const { isAdmin } = useCurrentUser();
  const navigate = useNavigate();

  if (isLoading) return <PageSpinner />;
  if (error) return <ErrorState error={error} />;

  const channels = (data ?? []).filter((c) => {
    const q = filter.toLowerCase();
    return (
      !q ||
      c.name.toLowerCase().includes(q) ||
      (c.description ?? "").toLowerCase().includes(q)
    );
  });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold tracking-tight">Channels</h1>
        <div className="flex items-center gap-2">
          <div className="w-60 max-w-full">
            <Input
              placeholder="Filter channels…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
          {isAdmin && (
            <Button
              onClick={() => setShowCreate((v) => !v)}
              aria-expanded={showCreate}
              className="shrink-0"
            >
              <PlusIcon />
              Add channel
            </Button>
          )}
        </div>
      </div>

      {isAdmin && showCreate && (
        <Card>
          <CardHeader className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">New channel</h2>
            <span className="rounded bg-brand-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-brand-800 dark:bg-brand-500/15 dark:text-brand-200">
              admin only
            </span>
          </CardHeader>
          <CardBody>
            <CreateChannelForm
              onCreated={(channelName) => {
                setShowCreate(false);
                navigate(`/channels/${channelName}`);
              }}
              onCancel={() => setShowCreate(false)}
            />
          </CardBody>
        </Card>
      )}

      {channels.length === 0 ? (
        <EmptyState
          title={filter ? "No channels match the filter" : "No channels yet"}
          description={
            filter
              ? "Try a different search term."
              : isAdmin
                ? "Use “Add channel” above to create your first one."
                : "Create a channel via the API or CLI to get started."
          }
        />
      ) : (
        <div className="grid gap-3">
          {channels.map((channel) => (
            <Link key={channel.id} to={`/channels/${channel.name}`} className="block">
              <Card className="transition hover:border-brand-300 hover:shadow-md dark:hover:border-brand-500/60 dark:hover:bg-slate-800/40">
                <CardBody className="flex items-start justify-between gap-6">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="font-semibold text-slate-900 dark:text-slate-100">{channel.name}</h2>
                      {channel.private && (
                        <span className="rounded bg-slate-900 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-100 dark:bg-slate-700">
                          private
                        </span>
                      )}
                      {channel.mirror_url && (
                        <span className="rounded bg-brand-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-brand-800 dark:bg-brand-500/15 dark:text-brand-200">
                          mirror
                        </span>
                      )}
                    </div>
                    <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
                      {channel.description || "No description."}
                    </p>
                  </div>
                  <code className="hidden shrink-0 font-mono text-xs text-slate-500 sm:block dark:text-slate-400">
                    {channel.storage_prefix}
                  </code>
                </CardBody>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function PlusIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

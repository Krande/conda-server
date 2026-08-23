import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "./ui/Button";
import { Card, CardBody, CardHeader } from "./ui/Card";
import { ErrorState } from "./ui/EmptyState";
import { Input } from "./ui/Input";
import {
  useBackfillAbout,
  useBackfillJob,
  useDeleteChannel,
  useReindexChannel,
} from "@/lib/queries";
import type { Channel } from "@/lib/types";

/**
 * Card shown on the ChannelDetail page for owners + server-admins.
 * Writers can upload via the upload card but need owner+ to trigger a
 * manual reindex or delete the channel entirely.
 */
export function ChannelAdminCard({ channel }: { channel: Channel }) {
  const reindex = useReindexChannel();
  const del = useDeleteChannel();
  const navigate = useNavigate();

  const [confirmName, setConfirmName] = useState("");
  const [reindexTriggered, setReindexTriggered] = useState(false);

  const canManage = channel.my_role === "owner" || channel.my_role === "admin";
  if (!canManage) return null;

  const handleReindex = async () => {
    setReindexTriggered(false);
    await reindex.mutateAsync(channel.name);
    setReindexTriggered(true);
    setTimeout(() => setReindexTriggered(false), 5000);
  };

  const handleDelete = async () => {
    await del.mutateAsync(channel.name);
    navigate("/channels", { replace: true });
  };

  return (
    <Card className="border-amber-200 bg-amber-50/40 dark:border-amber-900/60 dark:bg-amber-950/20">
      <CardHeader>
        <h2 className="text-sm font-semibold text-amber-900 dark:text-amber-200">Admin actions</h2>
      </CardHeader>
      <CardBody className="space-y-5">
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm font-medium text-slate-900 dark:text-slate-100">Reindex</div>
              <p className="text-xs text-slate-600 dark:text-slate-400">
                Scan object storage and regenerate <code>repodata.json</code>. Runs in the background.
              </p>
            </div>
            <Button variant="secondary" onClick={handleReindex} loading={reindex.isPending}>
              Reindex
            </Button>
          </div>
          {reindexTriggered && (
            <div className="rounded border border-brand-300 bg-brand-50 px-3 py-2 text-xs text-brand-900 dark:border-brand-700/60 dark:bg-brand-500/10 dark:text-brand-200">
              Reindex queued. Check the server logs or refresh package list to see progress.
            </div>
          )}
          {reindex.error && <ErrorState error={reindex.error} />}
        </div>

        <BackfillAboutSection channelName={channel.name} mirror={Boolean(channel.mirror_url)} />

        <div className="space-y-2 border-t border-amber-200 pt-4 dark:border-amber-900/60">
          <div>
            <div className="text-sm font-medium text-red-800 dark:text-red-300">Delete channel</div>
            <p className="text-xs text-slate-600 dark:text-slate-400">
              Removes the <code>{channel.name}</code> row, all package metadata,
              and the channel&apos;s object-storage blobs (wiped in the background).
            </p>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <Input
              placeholder={`Type "${channel.name}" to confirm`}
              value={confirmName}
              onChange={(e) => setConfirmName(e.target.value)}
              className="sm:max-w-xs"
            />
            <Button
              variant="danger"
              disabled={confirmName !== channel.name}
              loading={del.isPending}
              onClick={handleDelete}
            >
              Delete channel
            </Button>
          </div>
          {del.error && <ErrorState error={del.error} />}
        </div>
      </CardBody>
    </Card>
  );
}

/**
 * Fills in documentation / homepage / repository links for versions
 * that were indexed before the server read `info/about.json`.
 *
 * Unlike the reindex button next to it, this one reports real progress:
 * the work is one object-storage download per version, so it can run
 * for minutes and a fire-and-forget "queued, check the logs" would be
 * useless. A run is capped server-side, and says so when it stops at
 * that cap rather than pretending to be finished — pressing the button
 * again picks up where it left off, because every version it inspects
 * is stamped as inspected.
 */
function BackfillAboutSection({
  channelName,
  mirror,
}: {
  channelName: string;
  mirror: boolean;
}) {
  const start = useBackfillAbout();
  const [jobId, setJobId] = useState<number | null>(null);
  const job = useBackfillJob(channelName, jobId);

  // Mirror channels proxy an upstream and never store version rows, so
  // there is nothing local to read metadata out of.
  if (mirror) return null;

  const running =
    job.data?.status === "running" || job.data?.status === "pending";
  const upToDate = start.data?.status === "up-to-date";

  const handleStart = async () => {
    const res = await start.mutateAsync(channelName);
    setJobId(res.job_id);
  };

  return (
    <div className="space-y-2 border-t border-amber-200 pt-4 dark:border-amber-900/60">
      <div className="flex items-center justify-between gap-4">
        <div>
          <div className="text-sm font-medium text-slate-900 dark:text-slate-100">
            Backfill package metadata
          </div>
          <p className="text-xs text-slate-600 dark:text-slate-400">
            Read <code>info/about.json</code> from archives already in storage so
            package pages can show documentation, homepage and repository links.
            Downloads one archive per version, so it can take a while.
          </p>
        </div>
        <Button
          variant="secondary"
          onClick={handleStart}
          loading={start.isPending || running}
          disabled={running}
        >
          Backfill
        </Button>
      </div>

      {upToDate && (
        <div className="rounded border border-slate-300 bg-slate-50 px-3 py-2 text-xs text-slate-700 dark:border-slate-700 dark:bg-slate-800/40 dark:text-slate-300">
          Every version in this channel has already been inspected.
        </div>
      )}

      {job.data && (
        <div className="space-y-1 rounded border border-brand-300 bg-brand-50 px-3 py-2 text-xs text-brand-900 dark:border-brand-700/60 dark:bg-brand-500/10 dark:text-brand-200">
          <div>
            {running ? "Reading archives" : "Finished"}:{" "}
            <strong>
              {job.data.completed_count}
              {job.data.total_count > 0 && ` / ${job.data.total_count}`}
            </strong>{" "}
            inspected, <strong>{job.data.with_metadata_count}</strong> with metadata
            {job.data.failed_count > 0 && (
              <>
                , <strong>{job.data.failed_count}</strong> unreadable
              </>
            )}
            .
          </div>
          {job.data.status === "failed" && job.data.error && (
            <div className="text-red-700 dark:text-red-300">{job.data.error}</div>
          )}
          {job.data.status === "completed" && job.data.error && (
            <div>{job.data.error}</div>
          )}
        </div>
      )}

      {start.error && <ErrorState error={start.error} />}
      {job.error && <ErrorState error={job.error} />}
    </div>
  );
}

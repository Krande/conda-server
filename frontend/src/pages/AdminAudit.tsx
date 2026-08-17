import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, ErrorState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { PageSpinner } from "@/components/ui/Spinner";
import { useAuditActions, useAuditLog } from "@/lib/queries";
import { loginRedirectUrl, useCurrentUser } from "@/lib/auth";

export default function AdminAudit() {
  const { isLoggedIn, isAdmin, isLoading: authLoading } = useCurrentUser();
  const actionsQ = useAuditActions();

  const [action, setAction] = useState("");
  const [channel, setChannel] = useState("");
  const [actor, setActor] = useState("");

  const query = useMemo(
    () => ({
      action: action || undefined,
      channel: channel.trim() || undefined,
      actor: actor.trim() || undefined,
      limit: 200,
    }),
    [action, channel, actor],
  );
  const logQ = useAuditLog(query);

  if (authLoading) return <PageSpinner />;

  if (!isLoggedIn) {
    return (
      <EmptyState
        title="Sign in required"
        description="The audit log is restricted to server admins."
        action={
          <Button onClick={() => (window.location.href = loginRedirectUrl("/admin/audit"))}>
            Sign in
          </Button>
        }
      />
    );
  }
  if (!isAdmin) {
    return (
      <EmptyState
        title="Admin access required"
        description="Only server admins can read the audit log."
        action={
          <Link to="/">
            <Button variant="secondary">Back to home</Button>
          </Link>
        }
      />
    );
  }

  return (
    <div className="space-y-6">
      <div>
        <Link to="/admin" className="text-sm text-brand-700 hover:underline dark:text-brand-400">
          ← Back to admin
        </Link>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight">Audit log</h1>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          Administrative actions by humans and automation. Append-only, latest first.
        </p>
      </div>

      <Card>
        <CardHeader>
          <h2 className="text-sm font-semibold">Filter</h2>
        </CardHeader>
        <CardBody>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
            <label className="block">
              <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Action
              </span>
              <select
                value={action}
                onChange={(e) => setAction(e.target.value)}
                className="block w-full cursor-pointer rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-900 focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
              >
                <option value="">Any</option>
                {(actionsQ.data ?? []).map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Channel
              </span>
              <Input
                placeholder="exact channel name"
                value={channel}
                onChange={(e) => setChannel(e.target.value)}
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Actor email
              </span>
              <Input
                placeholder="who@example.com"
                value={actor}
                onChange={(e) => setActor(e.target.value)}
              />
            </label>
          </div>
        </CardBody>
      </Card>

      {logQ.isLoading ? (
        <PageSpinner />
      ) : logQ.error ? (
        <ErrorState error={logQ.error} />
      ) : (logQ.data ?? []).length === 0 ? (
        <EmptyState title="No audit entries match the filter" />
      ) : (
        <Card>
          <CardHeader className="hidden grid-cols-12 gap-4 text-xs font-medium uppercase tracking-wide text-slate-500 sm:grid dark:text-slate-400">
            <div className="col-span-3">When</div>
            <div className="col-span-3">Actor</div>
            <div className="col-span-2">Action</div>
            <div className="col-span-2">Channel</div>
            <div className="col-span-2">Target</div>
          </CardHeader>
          <CardBody className="divide-y divide-slate-100 p-0 dark:divide-slate-800">
            {(logQ.data ?? []).map((row) => {
              const metaPairs = Object.entries(row.meta ?? {});
              return (
                <div
                  key={row.id}
                  className="px-4 py-3 text-sm sm:grid sm:grid-cols-12 sm:gap-4 sm:px-5"
                >
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 sm:contents">
                    <span className="text-xs tabular-nums text-slate-500 sm:col-span-3 sm:self-center dark:text-slate-400">
                      {new Date(row.created_at).toLocaleString()}
                    </span>
                    <span className="text-slate-700 sm:col-span-3 sm:truncate sm:self-center dark:text-slate-300">
                      {row.actor_email ?? (
                        <em className="text-slate-400 dark:text-slate-500">system</em>
                      )}
                    </span>
                    <span className="font-mono text-xs text-slate-900 sm:col-span-2 sm:truncate sm:self-center dark:text-slate-100">
                      {row.action}
                    </span>
                    <span className="text-slate-700 sm:col-span-2 sm:truncate sm:self-center dark:text-slate-300">
                      {row.channel_name ?? "—"}
                    </span>
                    <span className="font-mono text-xs text-slate-600 sm:col-span-2 sm:truncate sm:self-center dark:text-slate-400">
                      {row.target ?? "—"}
                    </span>
                  </div>
                  {metaPairs.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-2 sm:col-span-12">
                      {metaPairs.map(([k, v]) => (
                        <code
                          key={k}
                          className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-700 dark:bg-slate-800 dark:text-slate-300"
                        >
                          {k}={JSON.stringify(v)}
                        </code>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </CardBody>
        </Card>
      )}
    </div>
  );
}

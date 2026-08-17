import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { CopyButton } from "@/components/ui/CopyButton";
import { EmptyState, ErrorState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { PageSpinner } from "@/components/ui/Spinner";
import { useCurrentUser, loginRedirectUrl } from "@/lib/auth";
import {
  useCreateToken,
  useRevokeToken,
  useTokens,
} from "@/lib/queries";
import type { ApiTokenCreated } from "@/lib/types";

export default function Tokens() {
  const { isLoggedIn, isLoading: authLoading } = useCurrentUser();
  const tokensQ = useTokens();
  const create = useCreateToken();
  const revoke = useRevokeToken();

  const [description, setDescription] = useState("");
  const [expiresDays, setExpiresDays] = useState<string>("");
  const [justCreated, setJustCreated] = useState<ApiTokenCreated | null>(null);

  if (authLoading) return <PageSpinner />;
  if (!isLoggedIn) {
    return (
      <EmptyState
        title="Sign in required"
        description="API tokens are scoped to your account."
        action={
          <Button
            onClick={() => {
              window.location.href = loginRedirectUrl("/tokens");
            }}
          >
            Sign in
          </Button>
        }
      />
    );
  }

  const handleCreate = async () => {
    const token = await create.mutateAsync({
      description: description || undefined,
      expires_in_days: expiresDays ? Number(expiresDays) : undefined,
    });
    setJustCreated(token);
    setDescription("");
    setExpiresDays("");
  };

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">API tokens</h1>
        <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
          Bearer tokens for programmatic access. Send as{" "}
          <code className="rounded bg-slate-100 px-1 text-xs dark:bg-slate-800 dark:text-slate-200">
            Authorization: Bearer &lt;token&gt;
          </code>
          .
        </p>
      </div>

      {justCreated && (
        <Card className="border-brand-300 bg-brand-50 dark:border-brand-700/60 dark:bg-brand-500/10">
          <CardBody className="space-y-2">
            <div className="text-sm font-medium text-brand-900 dark:text-brand-200">
              Token created — copy it now, you won't see it again.
            </div>
            <div className="flex items-center gap-2">
              <code className="flex-1 overflow-x-auto rounded bg-white px-3 py-2 text-xs dark:bg-slate-900 dark:text-slate-100">
                {justCreated.token}
              </code>
              <CopyButton value={justCreated.token}>Copy</CopyButton>
            </div>
            <button
              onClick={() => setJustCreated(null)}
              className="cursor-pointer text-xs text-slate-600 hover:underline dark:text-slate-400"
            >
              Dismiss
            </button>
          </CardBody>
        </Card>
      )}

      <Card>
        <CardHeader>
          <h2 className="text-sm font-semibold">Create a new token</h2>
        </CardHeader>
        <CardBody className="space-y-3">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_160px]">
            <Input
              placeholder="Description (e.g. ci-runner)"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
            <Input
              placeholder="Expires in days (optional)"
              type="number"
              min="1"
              max="3650"
              value={expiresDays}
              onChange={(e) => setExpiresDays(e.target.value)}
            />
          </div>
          <div>
            <Button onClick={handleCreate} loading={create.isPending}>
              Create token
            </Button>
          </div>
          {create.error && <ErrorState error={create.error} />}
        </CardBody>
      </Card>

      {tokensQ.isLoading ? (
        <PageSpinner />
      ) : tokensQ.error ? (
        <ErrorState error={tokensQ.error} />
      ) : !tokensQ.data || tokensQ.data.length === 0 ? (
        <EmptyState title="No tokens yet" description="Create one above." />
      ) : (
        <Card>
          <CardHeader className="hidden grid-cols-12 gap-4 text-xs font-medium uppercase tracking-wide text-slate-500 sm:grid dark:text-slate-400">
            <div className="col-span-4">Description</div>
            <div className="col-span-3">Created</div>
            <div className="col-span-3">Expires</div>
            <div className="col-span-2 text-right">Actions</div>
          </CardHeader>
          <CardBody className="divide-y divide-slate-100 p-0 dark:divide-slate-800">
            {tokensQ.data.map((t) => (
              <div
                key={t.id}
                className="flex flex-col gap-2 px-4 py-3 text-sm sm:grid sm:grid-cols-12 sm:items-center sm:gap-4 sm:px-5"
              >
                <div className="text-slate-900 sm:col-span-4 dark:text-slate-100">
                  {t.description ?? <span className="italic text-slate-400 dark:text-slate-500">no description</span>}
                </div>
                <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600 sm:contents sm:text-sm dark:text-slate-400">
                  <span className="sm:col-span-3">
                    <span className="text-slate-400 sm:hidden dark:text-slate-500">Created </span>
                    {new Date(t.created_at).toLocaleDateString()}
                  </span>
                  <span className="sm:col-span-3">
                    <span className="text-slate-400 sm:hidden dark:text-slate-500">Expires </span>
                    {t.expires_at ? new Date(t.expires_at).toLocaleDateString() : "never"}
                  </span>
                </div>
                <div className="sm:col-span-2 sm:text-right">
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={() => revoke.mutate(t.id)}
                    loading={revoke.isPending && revoke.variables === t.id}
                  >
                    Revoke
                  </Button>
                </div>
              </div>
            ))}
          </CardBody>
        </Card>
      )}
    </div>
  );
}

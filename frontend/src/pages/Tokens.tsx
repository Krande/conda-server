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
        <Card className="border-brand-300 bg-brand-50 dark:border-brand-600/50 dark:bg-brand-500/10">
          <CardBody className="space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-brand-800 dark:text-brand-200">
                <svg
                  aria-hidden
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  className="size-4 shrink-0"
                >
                  <path d="M12 15v2" />
                  <rect x="4" y="10" width="16" height="11" rx="2" />
                  <path d="M8 10V7a4 4 0 0 1 8 0v3" />
                </svg>
                Token created — copy it now, you won't see it again.
              </div>
              <button
                onClick={() => setJustCreated(null)}
                className="shrink-0 cursor-pointer text-xs text-brand-700/80 hover:underline dark:text-brand-300/80"
              >
                Dismiss
              </button>
            </div>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 overflow-x-auto rounded-lg bg-slate-900 px-3 py-2.5 font-mono text-xs text-slate-100 ring-1 ring-inset ring-slate-800 dark:bg-slate-950 dark:ring-slate-800">
                {justCreated.token}
              </code>
              <CopyButton value={justCreated.token} className="shrink-0">
                Copy
              </CopyButton>
            </div>
          </CardBody>
        </Card>
      )}

      <Card>
        <CardHeader>
          <h2 className="text-sm font-semibold">Create a new token</h2>
        </CardHeader>
        <CardBody className="space-y-4">
          <div className="grid grid-cols-1 gap-4 md:grid-cols-[1fr_170px]">
            <div className="space-y-1.5">
              <label
                htmlFor="token-description"
                className="block text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400"
              >
                Description
              </label>
              <Input
                id="token-description"
                placeholder="e.g. ci-runner"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <label
                htmlFor="token-expires"
                className="block text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400"
              >
                Expires (days)
              </label>
              <Input
                id="token-expires"
                placeholder="optional"
                type="number"
                min="1"
                max="3650"
                value={expiresDays}
                onChange={(e) => setExpiresDays(e.target.value)}
              />
            </div>
          </div>
          <Button onClick={handleCreate} loading={create.isPending}>
            Create token
          </Button>
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
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] border-collapse text-sm">
              <thead>
                <tr className="border-b border-slate-200 dark:border-slate-800">
                  <th className="px-5 py-3 text-left text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Description
                  </th>
                  <th className="px-5 py-3 text-left text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Created
                  </th>
                  <th className="px-5 py-3 text-left text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Expires
                  </th>
                  <th className="px-5 py-3 text-right text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 dark:divide-slate-800">
                {tokensQ.data.map((t) => (
                  <tr
                    key={t.id}
                    className="transition-colors hover:bg-slate-50 dark:hover:bg-slate-800/40"
                  >
                    <td className="px-5 py-3 font-medium text-slate-900 dark:text-slate-100">
                      {t.description ?? (
                        <span className="font-normal italic text-slate-400 dark:text-slate-500">
                          no description
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-5 py-3 text-xs text-slate-500 dark:text-slate-400">
                      {new Date(t.created_at).toLocaleDateString()}
                    </td>
                    <td className="whitespace-nowrap px-5 py-3 text-xs text-slate-500 dark:text-slate-400">
                      {t.expires_at ? new Date(t.expires_at).toLocaleDateString() : "never"}
                    </td>
                    <td className="px-5 py-3 text-right">
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => revoke.mutate(t.id)}
                        loading={revoke.isPending && revoke.variables === t.id}
                      >
                        Revoke
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}

import { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState, ErrorState } from "@/components/ui/EmptyState";
import { Input } from "@/components/ui/Input";
import { PageSpinner } from "@/components/ui/Spinner";
import { useCreateChannel } from "@/lib/queries";
import { loginRedirectUrl, useCurrentUser } from "@/lib/auth";

export default function Admin() {
  const { isLoggedIn, isAdmin, isLoading } = useCurrentUser();
  const create = useCreateChannel();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [isPrivate, setIsPrivate] = useState(false);
  const [mirrorEnabled, setMirrorEnabled] = useState(false);
  const [mirrorUrl, setMirrorUrl] = useState("https://conda.anaconda.org/conda-forge");
  const [mirrorTtl, setMirrorTtl] = useState("900");
  const [lastCreated, setLastCreated] = useState<string | null>(null);

  if (isLoading) return <PageSpinner />;

  if (!isLoggedIn) {
    return (
      <EmptyState
        title="Sign in required"
        description="The admin dashboard is restricted to signed-in admins."
        action={
          <Button
            onClick={() => {
              window.location.href = loginRedirectUrl("/admin");
            }}
          >
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
        description="Your account is not an admin. Ask an existing admin to add your email to auth.initial_admins, then sign in again."
        action={
          <Link to="/">
            <Button variant="secondary">Back to home</Button>
          </Link>
        }
      />
    );
  }

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    const channel = await create.mutateAsync({
      name: name.trim(),
      description: description.trim() || undefined,
      private: isPrivate,
      mirror_url: mirrorEnabled ? mirrorUrl.trim() : undefined,
      mirror_cache_seconds: mirrorEnabled ? Number(mirrorTtl) : undefined,
    });
    setLastCreated(channel.name);
    setName("");
    setDescription("");
    setIsPrivate(false);
    setMirrorEnabled(false);
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Admin</h1>
          <p className="mt-1 text-sm text-slate-600 dark:text-slate-400">
            Actions available to conda-server admins. More tools will land here as they're built.
          </p>
        </div>
        <Link to="/admin/audit">
          <Button variant="secondary">View audit log</Button>
        </Link>
      </div>

      {lastCreated && (
        <div className="rounded-md border border-brand-300 bg-brand-50 px-4 py-3 text-sm text-brand-900 dark:border-brand-700/60 dark:bg-brand-500/10 dark:text-brand-200">
          Channel <Link to={`/channels/${lastCreated}`} className="font-semibold underline">{lastCreated}</Link>{" "}
          created. For a local channel, seed storage and then reindex; for a mirror, the first client
          request will fetch from upstream.
        </div>
      )}

      <Card>
        <CardHeader>
          <h2 className="text-sm font-semibold">Create channel</h2>
        </CardHeader>
        <CardBody>
          <form onSubmit={handleCreate} className="space-y-3">
            <label className="block">
              <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Name
              </span>
              <Input
                required
                pattern="[a-zA-Z0-9][a-zA-Z0-9_\-\.]*"
                placeholder="my-channel"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
                Letters, digits, <code>_ - .</code> — first char must be alphanumeric.
              </span>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                Description
              </span>
              <Input
                placeholder="Internal packages"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </label>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={isPrivate}
                onChange={(e) => setIsPrivate(e.target.checked)}
                className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-900"
              />
              Private channel (requires auth to browse / download)
            </label>

            <div className="space-y-3 rounded-md border border-slate-200 bg-slate-50 p-3 dark:border-slate-800 dark:bg-slate-900/40">
              <label className="flex items-center gap-2 text-sm font-medium">
                <input
                  type="checkbox"
                  checked={mirrorEnabled}
                  onChange={(e) => setMirrorEnabled(e.target.checked)}
                  className="rounded border-slate-300 text-brand-600 focus:ring-brand-500 dark:border-slate-600 dark:bg-slate-900"
                />
                Mirror an upstream channel (proxy + cache)
              </label>
              {mirrorEnabled && (
                <div className="grid grid-cols-1 gap-3 md:grid-cols-[1fr_160px]">
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                      Upstream URL
                    </span>
                    <Input
                      required={mirrorEnabled}
                      pattern="https?://.+"
                      placeholder="https://conda.anaconda.org/conda-forge"
                      value={mirrorUrl}
                      onChange={(e) => setMirrorUrl(e.target.value)}
                    />
                    <span className="mt-1 block text-xs text-slate-500 dark:text-slate-400">
                      Packages cached on first download; repodata re-fetched after TTL expires.
                    </span>
                  </label>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                      Repodata TTL (sec)
                    </span>
                    <Input
                      type="number"
                      min="0"
                      max="86400"
                      value={mirrorTtl}
                      onChange={(e) => setMirrorTtl(e.target.value)}
                    />
                  </label>
                </div>
              )}
            </div>

            <div>
              <Button type="submit" loading={create.isPending}>
                Create channel
              </Button>
            </div>
            {create.error && <ErrorState error={create.error} />}
          </form>
        </CardBody>
      </Card>
    </div>
  );
}

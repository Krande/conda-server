import { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageSpinner } from "@/components/ui/Spinner";
import { CreateChannelForm } from "@/components/CreateChannelForm";
import { loginRedirectUrl, useCurrentUser } from "@/lib/auth";

export default function Admin() {
  const { isLoggedIn, isAdmin, isLoading } = useCurrentUser();
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
        <div className="rounded-lg border border-brand-300 bg-brand-50 px-4 py-3 text-sm text-brand-900 dark:border-brand-700/60 dark:bg-brand-500/10 dark:text-brand-200">
          Channel{" "}
          <Link to={`/channels/${lastCreated}`} className="font-semibold underline">
            {lastCreated}
          </Link>{" "}
          created. For a local channel, seed storage and then reindex; for a mirror, the first client
          request will fetch from upstream.
        </div>
      )}

      <Card>
        <CardHeader>
          <h2 className="text-sm font-semibold">Create channel</h2>
        </CardHeader>
        <CardBody>
          <CreateChannelForm onCreated={setLastCreated} />
        </CardBody>
      </Card>
    </div>
  );
}

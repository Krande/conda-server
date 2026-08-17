import { Link } from "react-router-dom";
import { Avatar } from "@/components/ui/Avatar";
import { Button } from "@/components/ui/Button";
import { Card, CardBody, CardHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { PageSpinner } from "@/components/ui/Spinner";
import { loginRedirectUrl, useCurrentUser, useLogout } from "@/lib/auth";

function Row({ label, value }: { label: string; value: string | null | undefined }) {
  return (
    <div className="grid grid-cols-1 gap-1 py-2 sm:grid-cols-[160px_1fr] sm:items-center">
      <dt className="text-xs uppercase tracking-wide text-slate-500 dark:text-slate-400">{label}</dt>
      <dd className="break-all font-mono text-sm text-slate-800 dark:text-slate-200">{value || "—"}</dd>
    </div>
  );
}

export default function Profile() {
  const { user, isLoggedIn, isLoading, isAdmin } = useCurrentUser();
  const logout = useLogout();

  if (isLoading) return <PageSpinner />;

  if (!isLoggedIn) {
    return (
      <EmptyState
        title="Sign in required"
        description="Your profile is only visible when you're signed in."
        action={
          <Button
            onClick={() => {
              window.location.href = loginRedirectUrl("/profile");
            }}
          >
            Sign in
          </Button>
        }
      />
    );
  }

  const label = user!.email ?? user!.username ?? user!.subject;

  return (
    <div className="space-y-6">
      <div className="flex flex-col items-start gap-4 sm:flex-row sm:items-center">
        <Avatar label={label} size="lg" />
        <div className="min-w-0">
          <h1 className="break-words text-2xl font-semibold tracking-tight">
            {user!.username ?? user!.email ?? "Account"}
          </h1>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-600 dark:text-slate-400">
            <span>{user!.email}</span>
            <span className={isAdmin
              ? "rounded bg-brand-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-brand-800 dark:bg-brand-500/15 dark:text-brand-200"
              : "rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-slate-700 dark:bg-slate-800 dark:text-slate-300"}>
              {user!.role}
            </span>
          </div>
        </div>
      </div>

      <Card>
        <CardHeader>
          <h2 className="text-sm font-semibold">Identity</h2>
        </CardHeader>
        <CardBody>
          <dl className="divide-y divide-slate-100 dark:divide-slate-800">
            <Row label="Email" value={user!.email} />
            <Row label="Username" value={user!.username} />
            <Row label="Role" value={user!.role} />
            <Row label="OIDC subject" value={user!.subject} />
          </dl>
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <h2 className="text-sm font-semibold">Account actions</h2>
        </CardHeader>
        <CardBody className="flex flex-wrap gap-3">
          <Link to="/tokens">
            <Button variant="secondary">Manage API tokens</Button>
          </Link>
          {isAdmin && (
            <Link to="/admin">
              <Button variant="secondary">Admin dashboard</Button>
            </Link>
          )}
          <Button
            variant="danger"
            loading={logout.isPending}
            onClick={() => logout.mutate()}
          >
            Sign out
          </Button>
        </CardBody>
      </Card>
    </div>
  );
}

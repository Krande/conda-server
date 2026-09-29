import { useMemo } from "react";
import { Card, CardBody, CardHeader } from "./ui/Card";
import { CopyButton } from "./ui/CopyButton";
import { backendOrigin } from "@/config";

/**
 * How to point `rattler-build publish` at this server with an API token.
 *
 * rattler-build picks its upload backend from the `--to` URL; the
 * `prefix://` scheme selects the prefix.dev protocol for any host, which
 * the server answers at `/api/v1/upload/<channel>`. The token is read from
 * rattler's credential store keyed by host, so it's stored once with
 * `auth login` rather than passed on the publish command.
 *
 * Build and publish are separate steps on purpose: `publish <recipe>` also
 * adds the `--to` URL to its solve channels, and the solver can't read the
 * `prefix://` scheme. Publishing already-built archives skips the solve.
 */
export function RattlerBuildPublishTip() {
  const origin = useMemo(() => backendOrigin(), []);
  const host = useMemo(() => new URL(origin).host, [origin]);

  const steps = [
    {
      label: "Store the token for this server (once per machine)",
      cmd: `rattler-build auth login ${host} --token <token>`,
    },
    {
      label: "Build the recipe",
      cmd: `rattler-build build --recipe recipe.yaml --output-dir output`,
    },
    {
      label: "Publish the built packages to a channel",
      cmd: `rattler-build publish output/*/*.conda --to prefix://${host}/<channel>`,
    },
    {
      label: "In CI, skip the login and pass the server URL and token as env vars",
      cmd: `PREFIX_SERVER_URL=${origin} PREFIX_API_KEY=<token> rattler-build upload prefix --channel <channel> output/*/*.conda`,
    },
  ];

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold">Publishing with rattler-build</h2>
      </CardHeader>
      <CardBody className="space-y-4 text-sm text-slate-600 dark:text-slate-400">
        <p>
          The server speaks the prefix.dev upload protocol. Use the{" "}
          <InlineCode>prefix://</InlineCode> scheme with this server's host in{" "}
          <InlineCode>--to</InlineCode>. You need writer access on the target channel.
        </p>
        <ol className="space-y-3">
          {steps.map((s, i) => (
            <li key={s.label} className="space-y-1.5">
              <div className="text-xs font-medium text-slate-700 dark:text-slate-300">
                {i + 1}. {s.label}
              </div>
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 overflow-x-auto whitespace-nowrap rounded-lg bg-slate-900 px-3 py-2 font-mono text-xs text-slate-100 ring-1 ring-inset ring-slate-800 dark:bg-slate-950">
                  {s.cmd}
                </code>
                <CopyButton value={s.cmd} variant="secondary" size="sm" className="shrink-0">
                  Copy
                </CopyButton>
              </div>
            </li>
          ))}
        </ol>
        <p className="text-xs">
          Publish already-built packages rather than a recipe: with a recipe, rattler-build also
          tries to resolve dependencies from the <InlineCode>prefix://</InlineCode> URL and fails.{" "}
          <InlineCode>prefix://</InlineCode> always connects over HTTPS. A package that already
          exists is refused; add <InlineCode>--force</InlineCode> to overwrite it, or{" "}
          <InlineCode>--skip-existing</InlineCode> with <InlineCode>upload prefix</InlineCode> to
          skip it.
        </p>
      </CardBody>
    </Card>
  );
}

function InlineCode({ children }: { children: React.ReactNode }) {
  return (
    <code className="rounded bg-slate-100 px-1 text-xs dark:bg-slate-800 dark:text-slate-200">
      {children}
    </code>
  );
}

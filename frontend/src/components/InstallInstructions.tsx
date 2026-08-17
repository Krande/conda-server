import { useMemo } from "react";
import { Card, CardBody } from "./ui/Card";
import { CopyButton } from "./ui/CopyButton";

interface Props {
  channel: string;
  packageName?: string;
  version?: string;
}

const MANAGERS: { name: string; cmd: string; label: string }[] = [
  { name: "pixi", cmd: "pixi add", label: "pixi" },
  { name: "mamba", cmd: "mamba install", label: "mamba" },
  { name: "conda", cmd: "conda install", label: "conda" },
];

export function InstallInstructions({ channel, packageName = "<package>", version }: Props) {
  const baseUrl = useMemo(() => `${window.location.origin}/${channel}`, [channel]);
  const spec = useMemo(
    () => (version ? `${packageName}=${version}` : packageName),
    [packageName, version],
  );

  return (
    <Card>
      <CardBody className="space-y-3">
        {MANAGERS.map((m) => {
          const cmd = m.name === "pixi"
            ? `${m.cmd} --channel ${baseUrl} ${spec}`
            : `${m.cmd} -c ${baseUrl} ${spec}`;
          return (
            <div key={m.name} className="flex items-center gap-2">
              <span className="w-14 shrink-0 text-xs font-medium uppercase tracking-wide text-slate-500 dark:text-slate-400">
                {m.label}
              </span>
              <code className="flex-1 overflow-x-auto rounded bg-slate-900 px-3 py-2 text-xs text-slate-100 dark:bg-slate-950 dark:ring-1 dark:ring-slate-800">
                {cmd}
              </code>
              <CopyButton value={cmd} />
            </div>
          );
        })}
      </CardBody>
    </Card>
  );
}

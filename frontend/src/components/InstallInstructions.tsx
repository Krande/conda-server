import { useMemo, useState } from "react";

interface Props {
  channel: string;
  packageName?: string;
  version?: string;
}

const MANAGERS: { name: string; cmd: string; flag: string; label: string }[] = [
  { name: "pixi", cmd: "pixi add", flag: "--channel", label: "pixi" },
  { name: "mamba", cmd: "mamba install", flag: "-c", label: "mamba" },
  { name: "conda", cmd: "conda install", flag: "-c", label: "conda" },
];

/**
 * Install commands rendered as a faux terminal — the block users actually
 * copy, so it's the visual anchor of the page. One row per package manager,
 * with the accent-colored prompt, dimmed flag token, and an inline copy
 * affordance.
 */
export function InstallInstructions({ channel, packageName = "<package>", version }: Props) {
  const baseUrl = useMemo(() => `${window.location.origin}/${channel}`, [channel]);
  const spec = useMemo(
    () => (version ? `${packageName}=${version}` : packageName),
    [packageName, version],
  );

  return (
    <div className="overflow-hidden rounded-xl bg-slate-950 ring-1 ring-slate-800/80 dark:ring-slate-700/50">
      <div className="flex items-center gap-2 border-b border-white/5 px-4 py-2.5">
        <span className="size-2.5 rounded-full bg-slate-600/60" />
        <span className="size-2.5 rounded-full bg-slate-600/60" />
        <span className="size-2.5 rounded-full bg-slate-600/60" />
        <span className="ml-1.5 font-mono text-[11px] text-slate-400">install</span>
      </div>
      <div className="divide-y divide-white/5">
        {MANAGERS.map((m) => {
          const cmd = `${m.cmd} ${m.flag} ${baseUrl} ${spec}`;
          return (
            <div key={m.name} className="flex items-center gap-3 px-4 py-2.5">
              <span className="w-12 shrink-0 font-mono text-[11px] uppercase tracking-wider text-slate-500">
                {m.label}
              </span>
              <code className="flex-1 overflow-x-auto whitespace-nowrap font-mono text-[12.5px] text-slate-100">
                <span className="select-none text-brand-400">$ </span>
                {m.cmd} <span className="text-slate-400">{m.flag}</span> {baseUrl} {spec}
              </code>
              <CopyIconButton value={cmd} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function CopyIconButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const handleClick = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Browser blocked clipboard access — silent fallback.
    }
  };
  return (
    <button
      type="button"
      onClick={handleClick}
      aria-label={copied ? "Copied" : "Copy command"}
      className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 rounded-md border border-white/10 bg-white/5 px-2.5 py-1 font-mono text-[11px] text-slate-300 transition-colors hover:bg-white/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500"
    >
      {copied ? (
        <>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" className="text-brand-400"><path d="M20 6 9 17l-5-5" /></svg>
          Copied
        </>
      ) : (
        <>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="9" width="11" height="11" rx="2" /><path d="M5 15V5a2 2 0 0 1 2-2h10" /></svg>
          Copy
        </>
      )}
    </button>
  );
}

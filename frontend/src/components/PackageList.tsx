import { useRef } from "react";
import { Link } from "react-router-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Card, CardHeader } from "./ui/Card";
import type { Package } from "@/lib/types";

const ROW_HEIGHT = 64;

/**
 * Virtualized package list. Renders only the rows visible in the scroll
 * viewport (plus an overscan buffer), so a mirror of conda-forge with
 * tens of thousands of cached packages still mounts in O(viewport) DOM
 * nodes.
 *
 * Layout:
 * - <sm: stacked (package name on top, metadata below). The table header
 *   is hidden because the labels don't match the stacked line shape.
 * - sm+: 4-column grid with a header row, desktop-table feel.
 * Both layouts share the 64px row height so the virtualizer doesn't need
 * per-breakpoint math.
 */
export function PackageList({
  channelName,
  packages,
}: {
  channelName: string;
  packages: Package[];
}) {
  const parentRef = useRef<HTMLDivElement>(null);

  const virtualizer = useVirtualizer({
    count: packages.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 8,
  });

  return (
    <Card className="overflow-hidden">
      <CardHeader className="hidden grid-cols-12 gap-4 text-xs font-medium uppercase tracking-wide text-slate-500 sm:grid dark:text-slate-400">
        <div className="col-span-5">Name</div>
        <div className="col-span-3">Latest version</div>
        <div className="col-span-2">Subdirs</div>
        <div className="col-span-2 text-right">Versions</div>
      </CardHeader>

      <div
        ref={parentRef}
        className="max-h-[70vh] min-h-[320px] overflow-y-auto"
        style={{ contain: "strict" }}
      >
        <div
          className="relative"
          style={{ height: `${virtualizer.getTotalSize()}px` }}
        >
          {virtualizer.getVirtualItems().map((vi) => {
            const pkg = packages[vi.index]!;
            const latest = pkg.versions[0];
            const subdirs = Array.from(new Set(pkg.versions.map((v) => v.subdir)));
            return (
              <Link
                key={pkg.name}
                to={`/channels/${channelName}/packages/${pkg.name}`}
                className="absolute inset-x-0 flex flex-col justify-center gap-0.5 border-b border-slate-100 px-4 text-sm transition hover:bg-slate-50 sm:grid sm:grid-cols-12 sm:gap-4 sm:px-5 dark:border-slate-800 dark:hover:bg-slate-800/40"
                style={{
                  top: 0,
                  transform: `translateY(${vi.start}px)`,
                  height: `${vi.size}px`,
                  alignItems: undefined,
                }}
              >
                <div className="truncate font-medium text-slate-900 sm:col-span-5 sm:self-center dark:text-slate-100">
                  {pkg.name}
                </div>
                {/* Mobile: compact one-liner with the key facts. */}
                <div className="flex items-center gap-2 truncate text-xs text-slate-500 sm:hidden dark:text-slate-400">
                  <span className="truncate text-slate-700 dark:text-slate-300">{latest?.version ?? "—"}</span>
                  <span aria-hidden>·</span>
                  <span className="tabular-nums">
                    {pkg.versions.length} ver{pkg.versions.length === 1 ? "" : "s"}
                  </span>
                  {subdirs.length > 0 && (
                    <>
                      <span aria-hidden>·</span>
                      <span className="truncate">{subdirs.join(", ")}</span>
                    </>
                  )}
                </div>
                {/* sm+: restored grid columns. */}
                <div className="hidden text-slate-700 sm:col-span-3 sm:block sm:self-center dark:text-slate-300">
                  {latest?.version ?? "—"}
                </div>
                <div className="hidden truncate text-xs text-slate-500 sm:col-span-2 sm:block sm:self-center dark:text-slate-400">
                  {subdirs.join(", ") || "—"}
                </div>
                <div className="hidden tabular-nums text-slate-500 sm:col-span-2 sm:block sm:self-center sm:text-right dark:text-slate-400">
                  {pkg.versions.length}
                </div>
              </Link>
            );
          })}
        </div>
      </div>

      <div className="border-t border-slate-200 px-5 py-2 text-xs text-slate-500 dark:border-slate-800 dark:text-slate-400">
        {packages.length} package{packages.length === 1 ? "" : "s"}
      </div>
    </Card>
  );
}

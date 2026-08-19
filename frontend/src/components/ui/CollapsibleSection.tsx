import { useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/**
 * A titled section whose body collapses, closed by default. Used to tuck the
 * owner/admin surface on the channel page out of the way so the common
 * (read + install) path stays uncluttered. The chevron + `aria-expanded`
 * mirror the "Add channel" disclosure on the Channels page.
 *
 * The body is unmounted while collapsed, so any queries it kicks off (e.g. the
 * member list) don't run until the section is opened.
 */
export function CollapsibleSection({
  title,
  description,
  badge,
  defaultOpen = false,
  children,
  className,
}: {
  title: string;
  description?: ReactNode;
  badge?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <section className={className}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="group flex w-full items-center gap-3 rounded-lg text-left"
      >
        <ChevronIcon
          className={cn(
            "size-4 shrink-0 text-slate-400 transition-transform group-hover:text-slate-600 dark:group-hover:text-slate-300",
            open && "rotate-90",
          )}
        />
        <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="text-lg font-semibold">{title}</span>
          {badge}
        </span>
      </button>
      {description && (
        <p className="ml-7 mt-1 text-sm text-slate-500 dark:text-slate-400">{description}</p>
      )}
      {open && <div className="mt-4 space-y-6">{children}</div>}
    </section>
  );
}

function ChevronIcon({ className }: { className?: string }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden
    >
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

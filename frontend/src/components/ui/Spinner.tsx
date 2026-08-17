import { cn } from "@/lib/cn";

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className={cn(
        "inline-block size-5 animate-spin rounded-full border-2 border-slate-300 border-t-brand-600 dark:border-slate-700 dark:border-t-brand-400",
        className,
      )}
    />
  );
}

export function PageSpinner() {
  return (
    <div className="flex items-center justify-center py-16">
      <Spinner className="size-8" />
    </div>
  );
}

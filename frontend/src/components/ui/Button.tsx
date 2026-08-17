import { forwardRef, type ButtonHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
}

const variants: Record<Variant, string> = {
  primary:
    "bg-brand-600 text-white hover:bg-brand-700 disabled:bg-brand-400 dark:bg-brand-500 dark:hover:bg-brand-400 dark:text-slate-950 dark:disabled:bg-brand-700",
  secondary:
    "bg-slate-100 text-slate-900 hover:bg-slate-200 border border-slate-300 disabled:opacity-50 dark:bg-slate-800 dark:text-slate-100 dark:border-slate-700 dark:hover:bg-slate-700",
  ghost:
    "text-slate-700 hover:bg-slate-100 disabled:opacity-50 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-slate-100",
  danger: "bg-red-600 text-white hover:bg-red-700 disabled:bg-red-400 dark:bg-red-500 dark:hover:bg-red-400",
};

const sizes: Record<Size, string> = {
  sm: "text-sm px-3 py-1.5",
  md: "text-sm px-4 py-2",
  lg: "text-base px-5 py-2.5",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = "primary", size = "md", className, loading, disabled, children, ...rest }, ref) => (
    <button
      ref={ref}
      className={cn(
        // cursor-pointer because browsers don't apply it to native <button>
        // by default; disabled:cursor-not-allowed tracks the visual disabled
        // state so the mouse feedback matches the button's interactivity.
        "inline-flex cursor-pointer items-center justify-center gap-2 rounded-md font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed",
        variants[variant],
        sizes[size],
        className,
      )}
      disabled={disabled || loading}
      {...rest}
    >
      {loading && (
        <span
          aria-hidden
          className="size-3.5 animate-spin rounded-full border-2 border-white/70 border-t-transparent"
        />
      )}
      {children}
    </button>
  ),
);
Button.displayName = "Button";

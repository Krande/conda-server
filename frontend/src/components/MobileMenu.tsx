import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { Avatar } from "./ui/Avatar";
import { Button } from "./ui/Button";
import { ThemeToggle } from "./ThemeToggle";
import { cn } from "@/lib/cn";
import { loginRedirectUrl, useCurrentUser, useLogout } from "@/lib/auth";
import { PALETTES, usePalette } from "@/lib/theme";

/**
 * Full-height sheet that slides in from the right on narrow viewports.
 * Used in place of the desktop nav + UserMenu when the hamburger is tapped.
 */
export function MobileMenu({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { user, isLoggedIn, isAdmin } = useCurrentUser();
  const logout = useLogout();
  const location = useLocation();
  const { palette, setPalette } = usePalette();

  // Close on ESC and on navigation.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open, onClose]);

  useEffect(() => {
    if (open) onClose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);

  const label = user?.email ?? user?.username ?? user?.subject ?? "";

  return (
    <div
      aria-hidden={!open}
      className={cn(
        "fixed inset-0 z-50 sm:hidden",
        open ? "pointer-events-auto" : "pointer-events-none",
      )}
    >
      <div
        onClick={onClose}
        className={cn(
          "absolute inset-0 bg-slate-900/40 transition-opacity dark:bg-black/60",
          open ? "opacity-100" : "opacity-0",
        )}
      />
      <aside
        className={cn(
          "absolute inset-y-0 right-0 flex w-72 max-w-[85%] transform flex-col border-l border-slate-200 bg-white shadow-xl transition-transform dark:border-slate-800 dark:bg-slate-900",
          open ? "translate-x-0" : "translate-x-full",
        )}
        role="dialog"
        aria-label="Main menu"
      >
        <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3 dark:border-slate-800">
          <span className="font-semibold">conda-server</span>
          <div className="flex items-center gap-1">
            <ThemeToggle />
            <button
              onClick={onClose}
              className="cursor-pointer rounded p-1 text-slate-500 hover:bg-slate-100 hover:text-slate-800 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100"
              aria-label="Close menu"
            >
              <CloseIcon />
            </button>
          </div>
        </div>

        {isLoggedIn && (
          <div className="flex items-center gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
            <Avatar label={label} />
            <div className="min-w-0">
              <div className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">{label}</div>
              <div className="text-xs text-slate-500 dark:text-slate-400">{isAdmin ? "admin" : "user"}</div>
            </div>
          </div>
        )}

        <nav className="flex flex-col gap-1 px-2 py-3">
          <MobileLink to="/" label="Home" />
          <MobileLink to="/channels" label="Channels" />
          {isLoggedIn && (
            <>
              <div className="my-2 h-px bg-slate-200 dark:bg-slate-800" />
              <MobileLink to="/profile" label="Profile" />
              <MobileLink to="/tokens" label="API tokens" />
              <MobileLink to="/about" label="About" />
              {isAdmin && <MobileLink to="/admin" label="Admin" />}
            </>
          )}
        </nav>

        <div className="mt-2 border-t border-slate-200 px-4 py-3 dark:border-slate-800">
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
            Theme
          </div>
          <div className="flex flex-wrap gap-2">
            {PALETTES.map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => setPalette(p.id)}
                aria-label={p.label}
                aria-pressed={palette === p.id}
                title={p.label}
                className={cn(
                  "size-8 cursor-pointer rounded-full ring-1 ring-inset ring-black/10 transition dark:ring-white/10",
                  palette === p.id &&
                    "ring-2 ring-offset-2 ring-brand-500 ring-offset-white dark:ring-offset-slate-900",
                )}
                style={{
                  background: `linear-gradient(135deg, ${p.swatch[0]} 50%, ${p.swatch[1]} 50%)`,
                }}
              />
            ))}
          </div>
        </div>

        <div className="mt-auto border-t border-slate-200 p-3 dark:border-slate-800">
          {isLoggedIn ? (
            <Button
              variant="secondary"
              className="w-full"
              loading={logout.isPending}
              onClick={() => logout.mutate()}
            >
              Sign out
            </Button>
          ) : (
            <Button
              className="w-full"
              onClick={() => {
                window.location.href = loginRedirectUrl(window.location.pathname);
              }}
            >
              Sign in
            </Button>
          )}
        </div>
      </aside>
    </div>
  );
}

function MobileLink({ to, label }: { to: string; label: string }) {
  return (
    <Link
      to={to}
      className="rounded-md px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-300 dark:hover:bg-slate-800 dark:hover:text-slate-100"
    >
      {label}
    </Link>
  );
}

function CloseIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 6 6 18M6 6l12 12" />
    </svg>
  );
}

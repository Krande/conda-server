import { useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { UserMenu } from "./UserMenu";
import { MobileMenu } from "./MobileMenu";
import { ThemeToggle } from "./ThemeToggle";
import { cn } from "@/lib/cn";

function NavItem({ to, label }: { to: string; label: string }) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        cn(
          "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
          isActive
            ? "bg-brand-50 text-brand-800 dark:bg-brand-500/15 dark:text-brand-200"
            : "text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100",
        )
      }
    >
      {label}
    </NavLink>
  );
}

export default function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <div className="flex min-h-full flex-col bg-slate-50 text-slate-900 dark:bg-slate-950 dark:text-slate-100">
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-6xl items-center gap-4 px-4 py-3 sm:px-6">
          <Link
            to="/"
            className="flex items-center gap-2 font-semibold text-slate-900 dark:text-slate-100"
          >
            <img src="/favicon.svg" alt="" className="size-6" />
            <span>conda-server</span>
          </Link>

          {/* Desktop nav */}
          <nav className="ml-2 hidden items-center gap-1 sm:flex">
            <NavItem to="/" label="Home" />
            <NavItem to="/channels" label="Channels" />
          </nav>

          <div className="ml-auto flex items-center gap-2">
            <ThemeToggle className="hidden sm:inline-flex" />
            <div className="hidden sm:block">
              <UserMenu />
            </div>
            <button
              className="inline-flex cursor-pointer rounded-md p-2 text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100 sm:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="Open menu"
            >
              <HamburgerIcon />
            </button>
          </div>
        </div>
      </header>

      <MobileMenu open={mobileOpen} onClose={() => setMobileOpen(false)} />

      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6 sm:py-8">
        <Outlet />
      </main>

      <footer className="border-t border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto max-w-6xl px-4 py-4 text-center text-xs text-slate-500 dark:text-slate-400 sm:px-6">
          conda-server — open source on the{" "}
          <a
            href="https://github.com/conda/rattler"
            className="text-brand-700 hover:underline dark:text-brand-400"
            target="_blank"
            rel="noreferrer"
          >
            rattler
          </a>{" "}
          ecosystem
        </div>
      </footer>
    </div>
  );
}

function HamburgerIcon() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="4" y1="7" x2="20" y2="7" />
      <line x1="4" y1="12" x2="20" y2="12" />
      <line x1="4" y1="17" x2="20" y2="17" />
    </svg>
  );
}

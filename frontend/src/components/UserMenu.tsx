import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { Link } from "react-router-dom";
import { Avatar } from "./ui/Avatar";
import { Button } from "./ui/Button";
import { cn } from "@/lib/cn";
import { loginRedirectUrl, useCurrentUser, useLogout } from "@/lib/auth";

const itemCls =
  "flex w-full cursor-pointer select-none items-center rounded px-3 py-2 text-sm text-slate-700 outline-none data-[highlighted]:bg-slate-100 data-[highlighted]:text-slate-900 dark:text-slate-300 dark:data-[highlighted]:bg-slate-800 dark:data-[highlighted]:text-slate-100";

/**
 * Top-right account dropdown for the desktop header. On <sm breakpoints the
 * MobileMenu replaces this entirely, so this component doesn't worry about
 * touch sizing.
 */
export function UserMenu() {
  const { user, isLoggedIn, isAdmin, isLoading } = useCurrentUser();
  const logout = useLogout();

  if (isLoading) return <div className="size-9" aria-hidden />;

  if (!isLoggedIn) {
    return (
      <Button
        size="sm"
        onClick={() => {
          window.location.href = loginRedirectUrl(window.location.pathname);
        }}
      >
        Sign in
      </Button>
    );
  }

  const label = user?.email ?? user?.username ?? user?.subject ?? "";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          className={cn(
            "cursor-pointer rounded-full outline-none ring-offset-2 transition focus-visible:ring-2 focus-visible:ring-brand-500",
          )}
          aria-label="Open account menu"
        >
          <Avatar label={label} />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          className="z-50 min-w-56 rounded-md border border-slate-200 bg-white p-1 shadow-lg dark:border-slate-800 dark:bg-slate-900 dark:shadow-black/40"
        >
          <div className="px-3 py-2">
            <div className="text-xs text-slate-500 dark:text-slate-400">Signed in as</div>
            <div className="truncate text-sm font-medium text-slate-900 dark:text-slate-100">{label}</div>
            {isAdmin && (
              <span className="mt-1 inline-block rounded bg-brand-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-brand-800 dark:bg-brand-500/15 dark:text-brand-200">
                admin
              </span>
            )}
          </div>
          <DropdownMenu.Separator className="my-1 h-px bg-slate-200 dark:bg-slate-800" />
          <DropdownMenu.Item asChild>
            <Link to="/profile" className={itemCls}>Profile</Link>
          </DropdownMenu.Item>
          <DropdownMenu.Item asChild>
            <Link to="/tokens" className={itemCls}>API tokens</Link>
          </DropdownMenu.Item>
          <DropdownMenu.Item asChild>
            <Link to="/about" className={itemCls}>About</Link>
          </DropdownMenu.Item>
          {isAdmin && (
            <DropdownMenu.Item asChild>
              <Link to="/admin" className={itemCls}>Admin</Link>
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Separator className="my-1 h-px bg-slate-200 dark:bg-slate-800" />
          <DropdownMenu.Item
            className={cn(itemCls, "text-red-700 data-[highlighted]:text-red-800 dark:text-red-400 dark:data-[highlighted]:bg-red-950/40 dark:data-[highlighted]:text-red-300")}
            onSelect={(e) => {
              e.preventDefault();
              logout.mutate();
            }}
          >
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

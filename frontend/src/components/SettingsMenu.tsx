import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import { PALETTES, usePalette, useTheme, type Palette } from "@/lib/theme";
import { cn } from "@/lib/cn";

/**
 * Gear dropdown in the header: pick the light/dark appearance and the accent
 * palette. Appearance flips the `dark` class; palette swaps the `data-palette`
 * attribute, which recolors the CSS-variable-backed `brand-*` ramp app-wide.
 */
export function SettingsMenu({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const { palette, setPalette } = usePalette();

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="Appearance and theme settings"
          title="Appearance and theme"
          className={cn(
            "inline-flex cursor-pointer items-center justify-center rounded-md p-2 text-slate-600 transition-colors hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 focus-visible:ring-offset-2 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100 dark:focus-visible:ring-offset-slate-950",
            className,
          )}
        >
          <GearIcon />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={8}
          className="z-50 min-w-60 rounded-md border border-slate-200 bg-white p-1.5 shadow-lg dark:border-slate-800 dark:bg-slate-900 dark:shadow-black/40"
        >
          <SectionLabel>Appearance</SectionLabel>
          <div className="flex gap-1 px-1 pb-1">
            <AppearanceButton
              active={theme === "light"}
              onClick={() => setTheme("light")}
              icon={<SunIcon />}
              label="Light"
            />
            <AppearanceButton
              active={theme === "dark"}
              onClick={() => setTheme("dark")}
              icon={<MoonIcon />}
              label="Dark"
            />
          </div>

          <DropdownMenu.Separator className="my-1 h-px bg-slate-200 dark:bg-slate-800" />

          <SectionLabel>Theme</SectionLabel>
          <DropdownMenu.RadioGroup
            value={palette}
            onValueChange={(v) => setPalette(v as Palette)}
          >
            {PALETTES.map((p) => (
              <DropdownMenu.RadioItem
                key={p.id}
                value={p.id}
                className="flex cursor-pointer select-none items-center gap-2.5 rounded px-2.5 py-1.5 text-sm text-slate-700 outline-none data-[highlighted]:bg-slate-100 data-[highlighted]:text-slate-900 dark:text-slate-300 dark:data-[highlighted]:bg-slate-800 dark:data-[highlighted]:text-slate-100"
              >
                <span
                  aria-hidden
                  className="h-4 w-8 shrink-0 rounded ring-1 ring-inset ring-black/10 dark:ring-white/10"
                  style={{
                    background: `linear-gradient(90deg, ${p.swatch[0]} 50%, ${p.swatch[1]} 50%)`,
                  }}
                />
                <span className="flex-1">{p.label}</span>
                <DropdownMenu.ItemIndicator>
                  <CheckIcon />
                </DropdownMenu.ItemIndicator>
              </DropdownMenu.RadioItem>
            ))}
          </DropdownMenu.RadioGroup>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-2.5 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
      {children}
    </div>
  );
}

function AppearanceButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "flex flex-1 cursor-pointer items-center justify-center gap-1.5 rounded px-2.5 py-1.5 text-sm font-medium transition-colors",
        active
          ? "bg-brand-50 text-brand-800 ring-1 ring-inset ring-brand-200 dark:bg-brand-500/15 dark:text-brand-200 dark:ring-brand-500/30"
          : "text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100",
      )}
    >
      {icon}
      {label}
    </button>
  );
}

function GearIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" className="text-brand-600 dark:text-brand-400" aria-hidden>
      <path d="M20 6 9 17l-5-5" />
    </svg>
  );
}

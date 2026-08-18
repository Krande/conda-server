import { useEffect, useState } from "react";

export type Theme = "light" | "dark";

/**
 * Accent palettes. The active palette is applied as a `data-palette` attribute
 * on <html>; the `brand-*` Tailwind ramp is CSS-variable-backed (see
 * index.css), so switching the attribute recolors the whole app at runtime.
 * `swatch` is the light/dark accent pair shown in the theme picker.
 */
export type Palette =
  | "amber"
  | "emerald"
  | "indigo"
  | "ocean"
  | "rose"
  | "graphite";

export const PALETTES: { id: Palette; label: string; swatch: [string, string] }[] = [
  { id: "amber", label: "Amber", swatch: ["#d97706", "#f59e0b"] },
  { id: "emerald", label: "Emerald", swatch: ["#059669", "#10b981"] },
  { id: "indigo", label: "Indigo", swatch: ["#4f46e5", "#6366f1"] },
  { id: "ocean", label: "Ocean", swatch: ["#0891b2", "#06b6d4"] },
  { id: "rose", label: "Rose", swatch: ["#e11d48", "#f43f5e"] },
  { id: "graphite", label: "Graphite", swatch: ["#475569", "#94a3b8"] },
];

export const DEFAULT_PALETTE: Palette = "amber";

const THEME_KEY = "conda-server:theme";
const PALETTE_KEY = "conda-server:palette";

function systemTheme(): Theme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function storedTheme(): Theme | null {
  try {
    const v = localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

function isPalette(v: string | null | undefined): v is Palette {
  return !!v && PALETTES.some((p) => p.id === v);
}

function storedPalette(): Palette | null {
  try {
    const v = localStorage.getItem(PALETTE_KEY);
    return isPalette(v) ? v : null;
  } catch {
    return null;
  }
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
}

function applyPalette(palette: Palette) {
  document.documentElement.dataset.palette = palette;
}

/**
 * Hook that exposes the active theme and a toggle. The initial value is read
 * from the `dark` class already applied by the inline boot script in
 * index.html (so first paint and React state stay in sync, and we don't get a
 * light-mode flash).
 */
export function useTheme(): { theme: Theme; toggle: () => void; setTheme: (t: Theme) => void } {
  const [theme, setThemeState] = useState<Theme>(() =>
    typeof document !== "undefined" && document.documentElement.classList.contains("dark")
      ? "dark"
      : "light",
  );

  const setTheme = (next: Theme) => {
    setThemeState(next);
    applyTheme(next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      // ignore — storage may be unavailable in some embedded contexts.
    }
  };

  // Follow OS changes only while the user hasn't expressed a preference.
  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const onChange = () => {
      if (storedTheme() === null) {
        const next = mq.matches ? "dark" : "light";
        setThemeState(next);
        applyTheme(next);
      }
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return {
    theme,
    toggle: () => setTheme(theme === "dark" ? "light" : "dark"),
    setTheme,
  };
}

/**
 * Hook for the accent palette. The initial value comes from the `data-palette`
 * attribute set by the boot script, so React state matches first paint.
 */
export function usePalette(): { palette: Palette; setPalette: (p: Palette) => void } {
  const [palette, setPaletteState] = useState<Palette>(() =>
    typeof document !== "undefined" && isPalette(document.documentElement.dataset.palette)
      ? (document.documentElement.dataset.palette as Palette)
      : DEFAULT_PALETTE,
  );

  const setPalette = (next: Palette) => {
    setPaletteState(next);
    applyPalette(next);
    try {
      localStorage.setItem(PALETTE_KEY, next);
    } catch {
      // ignore
    }
  };

  return { palette, setPalette };
}

export const __themeBootSnippet = `
(function(){try{var r=document.documentElement;var s=localStorage.getItem(${JSON.stringify(THEME_KEY)});var t=s==='dark'||s==='light'?s:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');if(t==='dark')r.classList.add('dark');r.style.colorScheme=t;var p=localStorage.getItem(${JSON.stringify(PALETTE_KEY)});var valid=['amber','emerald','indigo','ocean','rose','graphite'];r.dataset.palette=valid.indexOf(p)>=0?p:${JSON.stringify(DEFAULT_PALETTE)};}catch(e){}})();
`;

export { systemTheme, storedTheme, storedPalette };

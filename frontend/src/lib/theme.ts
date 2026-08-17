import { useEffect, useState } from "react";

export type Theme = "light" | "dark";

const STORAGE_KEY = "conda-server:theme";

function systemTheme(): Theme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function storedTheme(): Theme | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
}

/**
 * Hook that exposes the active theme and a toggle. The initial value is read
 * from the `dark` class already applied by the inline boot script in
 * index.html (so SSR-like first paint and React state stay in sync, and we
 * don't get a light-mode flash).
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
      localStorage.setItem(STORAGE_KEY, next);
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

export const __themeBootSnippet = `
(function(){try{var s=localStorage.getItem(${JSON.stringify(STORAGE_KEY)});var t=s==='dark'||s==='light'?s:(matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');var r=document.documentElement;if(t==='dark')r.classList.add('dark');r.style.colorScheme=t;}catch(e){}})();
`;

export { systemTheme, storedTheme };

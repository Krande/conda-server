/** @type {import('tailwindcss').Config} */

// The `brand` ramp is CSS-variable-backed: each shade reads an RGB-channel
// triple from a `--brand-*` custom property defined per accent palette in
// src/index.css. Switching the `data-palette` attribute on <html> recolors
// every `brand-*` utility at runtime. The `<alpha-value>` placeholder keeps
// opacity modifiers (e.g. `bg-brand-500/15`) working.
const brand = Object.fromEntries(
  [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map((shade) => [
    shade,
    `rgb(var(--brand-${shade}) / <alpha-value>)`,
  ]),
);

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["'IBM Plex Sans'", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        display: ["'Space Grotesk'", "'IBM Plex Sans'", "system-ui", "sans-serif"],
        mono: ["'IBM Plex Mono'", "ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
      colors: {
        brand,
      },
    },
  },
  plugins: [],
};

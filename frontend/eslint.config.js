import js from "@eslint/js";
import globals from "globals";
import tsPlugin from "@typescript-eslint/eslint-plugin";
import tsParser from "@typescript-eslint/parser";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";

// Flat config (ESLint 9). Lints the TS/TSX sources with the typescript-eslint
// recommended set plus the React hooks + Fast-Refresh rules that match this
// Vite SPA. Build output and Playwright artifacts are ignored.
export default [
  {
    ignores: ["dist/**", "node_modules/**", "playwright-report/**", "test-results/**"],
  },
  js.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      parser: tsParser,
      ecmaVersion: 2022,
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser },
    },
    plugins: {
      "@typescript-eslint": tsPlugin,
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...tsPlugin.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // TS already flags undefined identifiers with full type info; the core
      // rule double-reports on TS-only globals and JSX types.
      "no-undef": "off",
    },
  },
  {
    // Node context: config + tooling files.
    files: ["*.config.{js,ts}", "vite.config.ts", "playwright.config.ts"],
    languageOptions: {
      globals: { ...globals.node },
    },
  },
];

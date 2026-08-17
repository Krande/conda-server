import { expect, test } from "@playwright/test";

// These smoke tests only touch anon-accessible paths. Login requires a
// live identity provider; testing that belongs elsewhere (mock OIDC or a
// dedicated integration suite). What we're protecting against here is
// the kind of regression that breaks page loads outright — broken
// bundles, missing routes, mis-wired queries.

test.describe("public surface", () => {
  test("home renders heading, search, and primary nav", async ({ page }) => {
    await page.goto("/");
    await expect(
      page.getByRole("heading", { name: "conda-server", level: 1 }),
    ).toBeVisible();
    await expect(
      page.getByPlaceholder("Search packages or channels…"),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: /Channels/i }).first()).toBeVisible();
  });

  test("search input short-circuits under min length", async ({ page }) => {
    await page.goto("/");
    const search = page.getByPlaceholder("Search packages or channels…");
    await search.fill("x");
    // Page should NOT render the "No matches" panel or a results card —
    // the home landing content is still visible.
    await expect(page.getByText("Quick install")).toBeVisible();
  });

  test("channels page loads (empty-state or list)", async ({ page }) => {
    await page.goto("/channels");
    await expect(
      page.getByRole("heading", { name: /^Channels$/, level: 1 }),
    ).toBeVisible();
    // Either "No channels yet" empty state, or the filter input is
    // rendered. Both are healthy ways to present the page.
    const emptyState = page.getByText(/No channels (yet|match)/i);
    const filter = page.getByPlaceholder(/Filter channels/i);
    await expect(emptyState.or(filter).first()).toBeVisible();
  });

  test("unknown route lands on NotFound", async ({ page }) => {
    await page.goto("/this-path-does-not-exist-43f2");
    await expect(page.getByText(/404|not found/i).first()).toBeVisible();
  });

  test("tokens page demands sign-in for anon", async ({ page }) => {
    await page.goto("/tokens");
    // Header has its own Sign in link for anon; scope to the page's
    // main content area so we only match the EmptyState's CTA.
    await expect(
      page.getByRole("main").getByRole("button", { name: /Sign in/i }),
    ).toBeVisible();
  });

  test("admin page demands sign-in for anon", async ({ page }) => {
    await page.goto("/admin");
    await expect(
      page.getByRole("main").getByRole("button", { name: /Sign in/i }),
    ).toBeVisible();
  });
});

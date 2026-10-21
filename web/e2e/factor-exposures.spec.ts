// Factor exposures on the Risk tab (#348): loaded only on request, exposures shown with their errors and window, the
// factor file's end stated, and a holding without enough history shown with its reason, never as 0. Synthetic only.
import { type Page, expect, test } from "@playwright/test";

import { SNAPSHOT } from "./fixtures";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const m = (reason: string) => ({ value: null, unit: "fraction", reason, n: 10, note: null, how: null });
const RISK = {
  disclaimer: "Personal analysis, not advice.", privacy: "Local only.", excluded: [], warnings: [], notes: [], start_reason: "",
  reason: null, as_of: "2026-10-20", start: "2026-10-01", available: true, value: 3500,
  risk_free: { rate: 0.06, label: "e2e", as_of: null, source: null, fallback: false, user_set: false },
  realised: {
    window: { start: "2026-10-01", end: "2026-10-20", returns: 10 },
    volatility: m("needs 60 daily returns"), beta: m("needs 120"), tracking_error: m("needs 120"), sharpe: m("needs 250"),
    sortino: m("needs 250"), var_1d: m("needs 250"), cvar_1d: m("needs 250"), var_21d: m("needs 250"), cvar_21d: m("needs 250"),
    max_drawdown: { ...m("e2e"), peak: "2026-10-01", trough: "2026-10-01", recovered: null, days_to_recover: null, underwater_days: 0, current: 0 },
  },
  hypothetical: { note: "e2e", reason: "needs 120 common days", positions: [], sectors: [] },
  stress: [],
};
const ex = (factor: string, label: string, beta: number, se: number) =>
  ({ factor, label, beta, se, t: beta / se, ci95: [beta - 1.96 * se, beta + 1.96 * se] });
const FIT = {
  available: true, reason: null, r2: 0.82, adj_r2: 0.81, lags: 6,
  window: { start: "2023-01-02", end: "2025-12-31", returns: 740, dropped: 3 },
  alpha_annual: { value: 0.012, se: 0.03, unit: "fraction", reason: null, n: 740, note: null, how: "daily intercept × 252" },
  exposures: [ex("mkt", "Market (Rm − Rf)", 0.95, 0.04), ex("smb", "Size: small minus big (SMB)", 0.31, 0.06),
    ex("hml", "Value: high minus low book-to-market (HML)", -0.12, 0.07), ex("wml", "Momentum: winners minus losers (WML)", 0.08, 0.05)],
};
const FACTORS = {
  available: true, reason: null, disclaimer: "Personal analysis, not advice.", privacy: "Local only.",
  method: "OLS with Newey-West errors (e2e).", min_returns: 120, window_returns: 756, as_of: "2026-10-20",
  coverage_note: "IIMA's release 2025-12 ends on 2025-12-31; your history runs to 2026-10-20 (e2e).",
  source: { name: "IIMA Indian Fama-French-Momentum data library", citation: "Agarwalla, Jacob and Varma (2013)", terms: "Personal use (e2e).",
    url: "https://example.invalid/f.csv", page: "https://example.invalid/", release: "2025-12", first: "1993-10-04", last: "2025-12-31", fetched_at: null, skipped_days: 2 },
  portfolio: FIT,
  holdings: [
    { ...FIT, key: "NSE:AAA", name: "Example Alpha Ltd", asset_type: "stock", weight: 0.6 },
    { key: "NSE:BBB", name: "Example New Listing Ltd", asset_type: "stock", weight: 0.4, available: false, exposures: [],
      reason: "needs 120 daily returns that overlap the factor data; have 41", r2: null, adj_r2: null, lags: null, alpha_annual: null,
      window: { start: "2025-10-27", end: "2025-12-31", returns: 41, dropped: 0 } },
  ],
};
const IGNORE = ["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
  "GET /api/disclosures/tracked", "GET /api/portfolio/ais", "GET /api/portfolio/tax"];

async function open(page: Page, factors: unknown) {
  const api = await mockApi(page, {
    "GET /api/portfolio": SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/connections/summary": [],
    "GET /api/portfolio/analytics/risk": RISK,
    "GET /api/portfolio/analytics/factors": factors,
  });
  await page.goto("/portfolio#risk");
  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: /^Factor exposures/ }) });
  return { api, card };
}

test("risk: factor exposures run on request with errors, window, source and reasons (#348)", async ({ page }) => {
  const { api, card } = await open(page, FACTORS);
  await expect(card.getByRole("button", { name: "Estimate exposures" })).toBeVisible();
  expect(api.count("GET /api/portfolio/analytics/factors")).toBe(0); // nothing is fetched until asked
  await card.getByRole("button", { name: "Estimate exposures" }).click();
  await expect(card.getByText(/factor data ends 31 Dec 2025/)).toBeVisible();
  await expect(card.getByText(/740 daily returns, 2 Jan 2023 to 31 Dec 2025 \(3 not matched/)).toBeVisible();
  const port = card.getByRole("region", { name: "Portfolio factor exposures" });
  await expect(port.getByRole("row", { name: /Size: small minus big \(SMB\) 0\.31 0\.06 0\.19 to 0\.43/ })).toBeVisible();
  const rows = card.getByRole("region", { name: "Holding factor exposures" });
  await expect(rows.getByText(/needs 120 daily returns that overlap the factor data; have 41/)).toBeVisible();
  await expect(card.getByText(/Agarwalla, Jacob and Varma \(2013\)/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(IGNORE);
});

test("risk: factor data that cannot be read is a reason, not zero exposures (#348)", async ({ page }) => {
  const down = { ...FACTORS, available: false, reason: "IIMA factor data could not be read: IimaError: IIMA data library HTTP 503",
    source: null, portfolio: null, holdings: [], coverage_note: null };
  const { api, card } = await open(page, down);
  await card.getByRole("button", { name: "Estimate exposures" }).click();
  await expect(card.getByText(/IIMA factor data could not be read: IimaError: IIMA data library HTTP 503/)).toBeVisible();
  await expect(card.getByRole("table")).toHaveCount(0);
  api.expectClean(IGNORE);
});

// Risk tab: benchmark-relative, rolling, drawdown-table and VaR-backtest views (#349), and stress scenarios that show an
// unmapped holding as unknown, never as a 0 % move, with beta 1 only as the user's labelled option (#423).
// Synthetic names and values only; every figure comes from the fixture.
import { type Page, expect, test } from "@playwright/test";

import { SNAPSHOT } from "./fixtures";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const m = (value: number | null, reason: string | null = null, how: string | null = null) => ({ value, unit: "ratio", reason, n: 600, note: null, how });
const rolling = Array.from({ length: 40 }, (_, i) => ({
  day: new Date(Date.UTC(2026, 3, 1) + i * 86400000 * 3).toISOString().slice(0, 10), volatility: 0.15 + i * 0.001, beta: 1.1 + (i % 5) * 0.02,
}));
const RISK = {
  disclaimer: "Personal analysis, not advice.", privacy: "Local only.", excluded: [], warnings: [], notes: [], start_reason: "",
  reason: null, as_of: "2026-10-20", start: "2024-06-03", available: true, value: 3000,
  risk_free: { rate: 0.06, label: "e2e", as_of: null, source: null, fallback: false, user_set: false },
  realised: {
    window: { start: "2024-06-03", end: "2026-10-20", returns: 600 },
    volatility: m(0.18), beta: m(1.12), tracking_error: m(0.07), sharpe: m(0.9), sortino: m(1.2),
    var_1d: m(0.017), cvar_1d: m(0.024), var_21d: m(0.07), cvar_21d: m(0.09),
    max_drawdown: { ...m(-0.214), peak: "2024-09-26", trough: "2025-03-03", recovered: "2025-07-01", days_to_recover: 120, underwater_days: 278, current: -0.02 },
    active_return: m(0.031), information_ratio: m(0.44, null, "active return / tracking error"),
    up_capture: m(1.08), down_capture: m(null, "needs 20 down days; have 12"),
    rolling: { window: 126, how: "e2e", rows: rolling },
    drawdowns: [
      { peak: "2024-09-26", trough: "2025-03-03", depth: -0.214, recovered: "2025-07-01", days_to_recover: 120, underwater_days: 278 },
      { peak: "2026-01-30", trough: "2026-03-23", depth: -0.137, recovered: null, days_to_recover: null, underwater_days: 263 },
    ],
    var_backtest: { level: 0.95, window: 250, how: "each day against the 250 days before it", days: 349, exceptions: 29, expected: 17.45,
      p_uc: 0.0083, p_ind: 0.41, p_cc: 0.026, verdict: "too_many", note: "Losses beyond the VaR came more often than 1 day in 20: the VaR understated your risk." },
    var_ledger: { level: 0.95, window: 250, how: "e2e", logged: 41, resolved: 38, open: 1, void: 2,
      reason: "needs 250 logged days with a known outcome; have 38" },
  },
  hypothetical: { note: "e2e", reason: "needs 120 common days", positions: [], sectors: [] },
  stress: [
    { id: "gfc2008", label: "Global financial crisis, 2008-09 (equity only, NIFTYBEES proxy)", note: "Equity at NIFTYBEES's fall; every other holding unmapped.",
      window: ["2008-01-01", "2009-03-31"], available: true, peak: "2008-01-08", trough: "2008-10-27", benchmark_move: -0.6,
      portfolio_move: -0.6, inr: -1500, mapped_value: 2500, unmapped_value: 500, mapped_share: 0.8333,
      positions: [
        { key: "NSE:AAA", name: "Example Own Ltd", value: 2500, move: -0.6, inr: -1500, how: "proxy: NIFTYBEES move" },
        { key: "MF:9", name: "Example Liquid Fund", value: 500, move: null, inr: null, how: "unmapped: no own prices for the window and not equity, so no index proxy" },
      ] },
    { id: "beta20", label: "Nifty 50 falls 20% (hypothetical)", note: "e2e", available: true, assume_beta1: false, benchmark_move: -0.2,
      portfolio_move: -0.24, inr: -480, mapped_value: 2000, unmapped_value: 1000, mapped_share: 0.6667,
      positions: [
        { key: "NSE:AAA", name: "Example Own Ltd", value: 2000, move: -0.24, inr: -480, how: "beta 1.20 (240 days)", assumed: false },
        { key: "NSE:NEW", name: "Example New Listing", value: 500, move: null, inr: null, how: "unmapped: only 30 days of prices, needs 120 for a beta", assumed: false },
        { key: "MF:9", name: "Example Liquid Fund", value: 500, move: null, inr: null, how: "unmapped: only 0 days of prices, needs 120 for a beta; not equity", assumed: false },
      ] },
  ],
};

test("risk: relative, rolling, drawdown and VaR-backtest lines; unmapped stress rows are never 0 % (#349, #423)", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/portfolio": SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/connections/summary": [],
    "GET /api/portfolio/analytics/risk": RISK,
  });
  await page.goto("/portfolio#risk");
  const rel = page.locator("section").filter({ has: page.getByRole("heading", { name: "Against the index" }) });
  await expect(rel.getByText("0.44")).toBeVisible();
  await expect(rel.getByText("108%")).toBeVisible();
  await expect(rel.getByText("needs 20 down days; have 12")).toBeVisible(); // unknown shows its reason, not 0
  await expect(page.getByRole("heading", { name: "Rolling volatility and beta" })).toBeVisible();
  const dd = page.locator("section").filter({ has: page.getByRole("heading", { name: "Largest drawdowns" }) });
  await expect(dd.getByText("-21.4%")).toBeVisible();
  await expect(dd.getByText("not yet")).toBeVisible();
  // #399: the logged VaR is not tested until 250 days have an outcome: its reason, never a verdict
  await expect(page.getByText("Logged VaR backtest: needs 250 logged days with a known outcome; have 38.")).toBeVisible();
  await expect(page.getByText(/VaR backtest \(walk-forward\): 29 of 349 days lost more than the 1-day VaR \(expected 17\.\d\)/)).toBeVisible();
  await expect(page.getByText(/understated your risk/)).toBeVisible();

  const stress = page.locator("section").filter({ has: page.getByRole("heading", { name: "Stress scenarios" }) });
  await stress.getByRole("button", { name: /Global financial crisis, 2008-09/ }).click();
  const fund = stress.getByRole("row", { name: /Example Liquid Fund/ });
  await expect(fund.getByText("unmapped", { exact: true })).toBeVisible();
  await expect(fund).not.toContainText("0.0%");
  await expect(stress.getByText(/on the ₹2,500 that could be mapped, 83\.3% of today's holdings; ₹500 is unmapped/)).toBeVisible();

  const asked = page.waitForRequest((r) => r.url().includes("/api/portfolio/analytics/risk") && r.url().includes("assume_beta1=true"));
  await stress.getByLabel(/assume beta 1 for equity/).check();
  await asked; // the option is the user's, sent with the request, never on by default
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
    "GET /api/disclosures/tracked", "GET /api/portfolio/ais", "GET /api/portfolio/tax"]);
});

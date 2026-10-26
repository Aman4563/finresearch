// #479: a fund's Sharpe and Sortino against each month's own risk-free rate, with the factsheet-style pair (one
// overnight rate on the last NAV's day, as AMC factsheets do) beside them, both labelled; the category rank's Sortino
// shows its factsheet-style figure too. Synthetic scheme, NAVs and figures only; every value comes from the fixtures.
import { type Page, expect, test } from "@playwright/test";

import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const CODE = "999901";
const NAVS = Array.from({ length: 40 }, (_, i) => ({
  date: new Date(Date.UTC(2026, 7, 3) + i * 86400000).toISOString().slice(0, 10), nav: 100 + i * 0.1 + (i % 3) * 0.05,
}));
const FACTSHEET = { known: true, rate: 0.0525, as_of: "2026-09-11", label: "factsheet-style: one overnight rate on the last working day",
  source: "RBI repo rate 5.25 % in force on 11-Sep-2026 (from 05-Dec-2025)", url: "https://www.rbi.org.in/", note: null };
const ANALYTICS = {
  scheme: { scheme_code: CODE, name: "Example Flexi Cap Fund - Direct Plan - Growth", plan: "Direct Plan", option: "Growth",
    category: "Equity Scheme - Flexi Cap Fund", amc: "Example Mutual Fund", nav: "103.9", nav_date: "2026-09-11", isin: "INE000X01000" },
  years: 5, source: "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx", navs: NAVS,
  trailing: { "1y": 0.12, "3y": 0.14, "5y": 0.15, since_start: 0.15 }, rolling: {},
  risk: { return: 0.039, annualised_volatility: 0.14, max_drawdown: -0.08, drawdown_peak: "2026-08-10", drawdown_trough: "2026-08-20",
    points: 40, risk_free_annual: 0.0598, sharpe: 0.91, sortino: 1.32, factsheet: { ...FACTSHEET, sharpe: 0.97, sortino: 1.41 } },
  risk_free: { method: "monthly", label: "each month's own risk-free rate", basis: "simple yield, Actual/365", known: true, fallback: false,
    source: "each month's own risk-free rate", annual: 0.0598, tiers: { proxy: 1810, owner: 16 }, note: null, unknown: [],
    months: [{ month: "2026-09", days: 11, y91: 0.0525, tiers: { proxy: 11 } }], factsheet: FACTSHEET },
};
const RANKED = { code: CODE, name: "Example Flexi Cap Fund", amc: "Example Mutual Fund",
  values: { cagr_1y: 0.12, cagr_3y: 0.14, cagr_5y: 0.15, consistency_3y: 0.6, sortino_3y: 1.18, max_drawdown_3y: -0.2, ter: 0.6 },
  ranks: { cagr_1y: 2, cagr_3y: 1, cagr_5y: 1, consistency_3y: 2, sortino_3y: 1, max_drawdown_3y: 3, ter: 1 },
  percentiles: { cagr_1y: 0.7, cagr_3y: 0.9, cagr_5y: 0.9, consistency_3y: 0.7, sortino_3y: 0.9, max_drawdown_3y: 0.5, ter: 0.9 },
  no_downside: false, missing: {}, factsheet: { sortino_3y: 1.31, no_downside: false } };
const METRICS = [["cagr_1y", "1-year return", true, "fraction"], ["cagr_3y", "3-year CAGR", true, "fraction"],
  ["cagr_5y", "5-year CAGR", true, "fraction"], ["consistency_3y", "Consistency", true, "fraction"],
  ["sortino_3y", "Sortino ratio (3 years, monthly)", true, "ratio"], ["max_drawdown_3y", "Max drawdown", true, "fraction"],
  ["ter", "Expense ratio (direct plan)", false, "pct"]].map(([key, label, higher_is_better, unit]) => ({ key, label, higher_is_better, unit }));
const COUNTS = Object.fromEntries(METRICS.map((m) => [m.key, 3]));
const RANK = {
  status: "ok", as_of: "2026-08-31", generated_at: "2026-09-01T02:00:00Z", sampling: "month-end NAVs", ter_day: "2026-08-31", ter_error: null,
  risk_free: { ...ANALYTICS.risk_free, months: [{ month: "2026-08", days: 31, y91: 0.0525, tiers: { proxy: 31 } }],
    factsheet: { ...FACTSHEET, as_of: "2026-08-31", source: "RBI repo rate 5.25 % in force on 31-Aug-2026 (from 05-Dec-2025)" } },
  metrics: METRICS, caveats: ["Past returns."], sources: [], label: "Flexi Cap Fund", via: null, fund: RANKED, size: 3, counts: COUNTS,
  category: { key: "equity:flexi cap", label: "Flexi Cap Fund", raw_labels: ["Equity Scheme - Flexi Cap Fund"], size: 3, counts: COUNTS,
    ranked: Object.fromEntries(METRICS.map((m) => [m.key, true])), funds: [RANKED], excluded: [] },
};

test("fund: Sharpe and Sortino against each month's own rate, beside the factsheet-style pair (#479)", async ({ page }) => {
  const api = await mockApi(page, {
    [`GET /api/funds/${CODE}/analytics`]: ANALYTICS,
    [`GET /api/funds/${CODE}/category-rank`]: RANK,
  });
  await page.goto(`/funds/${CODE}`);
  // the card's heading carries its help button: "Risk What is this?"
  const risk = page.locator("section").filter({ has: page.getByRole("heading", { name: /^Risk( What is this\?)?$/ }) });
  await expect(risk.getByText("Sharpe ratio", { exact: true })).toBeVisible();
  await expect(risk.getByText("0.91")).toBeVisible();
  await expect(risk.getByText("Sharpe (factsheet-style)")).toBeVisible();
  await expect(risk.getByText("0.97")).toBeVisible();
  await expect(risk.getByText("Sortino (factsheet-style)")).toBeVisible();
  await expect(risk.getByText("1.41")).toBeVisible();
  const line = page.getByTestId("fund-risk-free");
  await expect(line).toContainText("each month's own rate, 5.98% a year on average over the window");
  await expect(line).toContainText("RBI repo-rate proxy 1810 days, your entered rates 16 days");
  await expect(line).toContainText("Factsheet-style: 5.25% — RBI repo rate 5.25 % in force on 11-Sep-2026");
  await expect(line).not.toContainText("FBIL");
  const mar = page.getByTestId("rank-mar");
  await expect(mar).toContainText("each month's own risk-free rate");
  await expect(mar).toContainText("The factsheet-style Sortino uses one rate for every month: 5.25%");
  await expect(page.getByText(/1\.18 · beats 90% · factsheet-style 1\.31/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean([`GET /api/funds/${CODE}/peers`, `GET /api/funds/${CODE}/consistency`, `POST /api/signals/fund/${CODE}`,
    `GET /api/funds/${CODE}/sip`, `GET /api/lookthrough/funds/${CODE}`]);
});

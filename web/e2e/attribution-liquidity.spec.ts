// Brinson attribution and days to liquidate on the Risk tab (#348): both load only on request; effects shown with the
// window and the index cross-check; an unknown benchmark sector, a left-out holding and a stock without enough volume
// are shown with their reasons, never as 0. Synthetic only.
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
const line = (segment: string, wp: number, wb: number, rp: number | null, rb: number | null, a: number | null, s: number | null, i: number | null, note: string | null = null, reason: string | null = null) =>
  ({ segment, wp, wb, rp, rb, allocation: a, selection: s, interaction: i, total: a == null ? null : a + (s ?? 0) + (i ?? 0), note, reason });
// hand-checked: R_p .14, R_b .2, active -.06 = .0225 + .06 - .0225 (Financials) - .06 (IT) - .03 - .03
const ATTRIBUTION = {
  available: true, reason: null, period: "3m", periods: ["1m", "3m", "6m"], method: "Brinson-Fachler (1985) (e2e).",
  caveats: ["Holdings-based (e2e)."], disclaimer: "Personal analysis, not advice.", privacy: "Local only.",
  window: { start: "2026-07-20", end: "2026-10-20" },
  benchmark: { name: "Nifty 50 (price return)", members: 50, rebuilt_return: 0.2, index_return: 0.198, difference: 0.002, tolerance: 0.005, warning: null,
    source: { name: "NSE: Nifty 50 constituents with free-float market capitalisation", url: "https://example.invalid/w", as_of: "2026-10-20T15:30:00", fetched_at: null } },
  segments: [line("Financial Services", 0.5, 0.8, 0.2, 0.125, 0.0225, 0.06, -0.0225), line("Information Technology", 0, 0.2, 0.5, 0.5, -0.06, 0, 0, "not held"),
    line("Consumer Durables", 0.3, 0, 0.1, 0.1, -0.03, 0, 0, "not in the benchmark"), line("Funds (no look-through)", 0.2, 0, 0.05, 0.05, -0.03, 0, 0, "not in the benchmark")],
  totals: { portfolio_return: 0.14, benchmark_return: 0.2, active: -0.06, allocation: -0.0975, selection: 0.06, interaction: -0.0225 },
  excluded: [{ key: "NSE:NEW", name: "Example New Listing Ltd", reason: "no price on or within 7 days before 2026-07-20 (bought or listed later?)" }],
  benchmark_unknown: [], coverage: 0.95, complete: false, unreachable: [],
};
const UNKNOWN_BENCH = {
  ...ATTRIBUTION, complete: false, excluded: [],
  segments: [line("Financial Services", 0.5, 0.8, 0.2, null, null, null, null, null, "benchmark weight or return unknown for EXA: NSE gave no free-float market cap")],
  totals: { portfolio_return: 0.14, benchmark_return: null, active: null, allocation: null, selection: null, interaction: null },
  benchmark_unknown: [{ symbol: "EXA", name: "Example Bank Ltd", reason: "NSE gave no free-float market cap" }], unreachable: ["EXA"],
};
const LIQUIDITY = {
  available: true, reason: null, participation: 0.2, adv_sessions: 20, min_sessions: 15, horizons: [1, 5, 20], method: "Days = units ÷ (participation × ADV) (e2e).",
  as_of: "2026-10-20", source: { name: "NSE full bhavcopy (e2e)", note: "NSE volume only." }, privacy: "Local only.", disclaimer: "e2e",
  positions: [
    { key: "NSE:AAA", name: "Example Alpha Ltd", asset_type: "stock", units: 30000, value: 600000, weight: 0.6, symbol: "AAA", adv_shares: 50000, adv_rupees: 10000000, pct_of_adv: 60, days: 3, status: "known", reason: null, sessions: null },
    { key: "NSE:BBB", name: "Example Thin Ltd", asset_type: "stock", units: 100, value: 150000, weight: 0.15, symbol: "BBB", adv_shares: null, adv_rupees: null, pct_of_adv: null, days: null, status: "unknown", reason: "traded on 3 of the last 20 stored sessions; needs 15", sessions: null },
    { key: "MF:111", name: "Example Flexi Cap Fund", asset_type: "mf", units: 1000, value: 250000, weight: 0.25, symbol: null, adv_shares: null, adv_rupees: null, pct_of_adv: null, days: null, status: "not_applicable", reason: "a mutual fund is redeemed at NAV", sessions: null },
  ],
  summary: { within: { "1": 0, "5": 0.6, "20": 0.6 }, unknown: 0.15, not_applicable: 0.25, max_days: 3, complete: false },
};
const IGNORE = ["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
  "GET /api/disclosures/tracked", "GET /api/portfolio/ais", "GET /api/portfolio/tax"];

async function open(page: Page, attribution: unknown, liquidity: unknown = LIQUIDITY) {
  const api = await mockApi(page, {
    "GET /api/portfolio": SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/connections/summary": [],
    "GET /api/portfolio/analytics/risk": RISK,
    "GET /api/portfolio/analytics/attribution": attribution,
    "GET /api/portfolio/analytics/liquidity": liquidity,
  });
  await page.goto("/portfolio#risk");
  const card = (name: RegExp) => page.locator("section").filter({ has: page.getByRole("heading", { name }) });
  return { api, attr: card(/^Attribution vs Nifty 50/), liq: card(/^Days to liquidate/) };
}

test("risk: attribution runs on request and its effects add up to the active return (#348)", async ({ page }) => {
  const { api, attr } = await open(page, ATTRIBUTION);
  await expect(attr.getByRole("button", { name: "Attribute returns" })).toBeVisible();
  expect(api.count("GET /api/portfolio/analytics/attribution")).toBe(0); // nothing is fetched until asked
  await attr.getByRole("button", { name: "Attribute returns" }).click();
  await expect(attr.getByText(/active -6\.00 pp = allocation -9\.75 pp \+ selection \+6\.00 pp \+ interaction -2\.25 pp/)).toBeVisible();
  const rows = attr.getByRole("region", { name: "Attribution by sector" });
  await expect(rows.getByRole("row", { name: /Financial Services 50\.0% 80\.0% \+20\.0% \+12\.5% \+2\.25 pp \+6\.00 pp -2\.25 pp \+6\.00 pp/ })).toBeVisible();
  await expect(attr.getByText(/Left out: Example New Listing Ltd: no price on or within 7 days/)).toBeVisible();
  await expect(attr.getByText(/Covers 95\.0% of your holdings/)).toBeVisible();
  await expect(attr.getByText(/against NSE's own Nifty 50 \+19\.80%/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(IGNORE);
});

test("risk: an unknown benchmark weight is a reason, not a zero effect (#348)", async ({ page }) => {
  const { api, attr } = await open(page, UNKNOWN_BENCH);
  await attr.getByRole("button", { name: "Attribute returns" }).click();
  await expect(attr.getByText(/active unknown/)).toBeVisible();
  await expect(attr.getByText(/unknown: benchmark weight or return unknown for EXA: NSE gave no free-float market cap/)).toBeVisible();
  await expect(attr.getByText(/Index member Example Bank Ltd: NSE gave no free-float market cap/)).toBeVisible();
  api.expectClean(IGNORE);
});

test("risk: attribution source down is a reason (#348)", async ({ page }) => {
  const down = { ...ATTRIBUTION, available: false, reason: "Nifty 50 weights could not be read: IndexWeightsError: HTTP 403", segments: [], totals: null, benchmark: null };
  const { api, attr } = await open(page, down);
  await attr.getByRole("button", { name: "Attribute returns" }).click();
  await expect(attr.getByText(/Nifty 50 weights could not be read: IndexWeightsError: HTTP 403/)).toBeVisible();
  await expect(attr.getByRole("table")).toHaveCount(0);
  api.expectClean(IGNORE);
});

test("risk: days to liquidate on request, with unknown and not-applicable holdings stated (#348)", async ({ page }) => {
  const { api, liq } = await open(page, ATTRIBUTION);
  expect(api.count("GET /api/portfolio/analytics/liquidity")).toBe(0);
  await liq.getByRole("button", { name: "Estimate days" }).click();
  await expect(liq.getByText(/Sellable within 1 day: at least 0\.0%; 5 days: at least 60\.0%/)).toBeVisible();
  await expect(liq.getByText(/Unknown: 15\.0%/)).toBeVisible();
  const rows = liq.getByRole("region", { name: "Days to liquidate" });
  await expect(rows.getByRole("row", { name: /Example Alpha Ltd 60\.0% 50,000 60\.0% 3\.0/ })).toBeVisible();
  await expect(rows.getByText(/unknown: traded on 3 of the last 20 stored sessions; needs 15/)).toBeVisible();
  await expect(rows.getByText(/n\/a: a mutual fund is redeemed at NAV/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(IGNORE);
});

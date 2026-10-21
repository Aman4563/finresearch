// Optimiser on the Allocation tab (#346): run on request, a target and trades only after the out-of-sample check
// passes, unknown costs and tax shown as unknown (never ₹0), and no trade list when the check fails. Synthetic only.
import { type Page, expect, test } from "@playwright/test";

import { SNAPSHOT } from "./fixtures";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const VALIDATION = {
  passed: true, reasons: [], windows: 12, days: 252, min_windows: 6, max_turnover: 1,
  vol: { method: 0.11, equal_weight: 0.14, current: 0.19 }, turnover: { method: 0.32, equal_weight: 0.1, current: 0.12 },
};
const PLAN = {
  status: "suggested", message: "Suggested trades toward the validated target (informational; nothing is placed).",
  disclaimer: "Illustrative, not investment advice: suggested trades only, no order is placed or can be placed from the app.",
  method: "min_variance", complete: true, excluded: [], tax_complete: true, unclassified: { detail: "" },
  holdings: [
    { holding_id: 1, name: "Example Volatile Ltd", current_pct: 60, target_pct: 22.5, lo_pct: 0, hi_pct: 10, status: "optimised", reason: null },
    { holding_id: 2, name: "Example Calm Fund", current_pct: 30, target_pct: 67.5, lo_pct: 5, hi_pct: 100, status: "optimised", reason: null },
    { holding_id: 3, name: "Example New Listing Ltd", current_pct: 10, target_pct: 10, lo_pct: null, hi_pct: null, status: "fixed",
      reason: "only 41 daily returns (the validation needs 378)" },
  ],
  skipped: [{ holding_id: 2, name: "Example Calm Fund", units: 50, reason: "ELSS units within the 3-year lock-in (the next unlock is 2027-01-10)" }],
  sells: [{ holding_id: 1, name: "Example Volatile Ltd", category: "Short-term gain", units: 375, gross: 37500, charges: 37.5,
    exit_load: 0, gain: 7462.5, tax: 1552.2, costs_unknown: ["brokerage, DP and exchange charges (not known to the app)"], notes: [] }],
  buys: [{ holding_id: 2, name: "Example Calm Fund", units: 3745.8, amount: 37458, stamp: 1.87, costs_unknown: [] }],
  validation: VALIDATION, risk: { vol_current: 0.19, vol_target: 0.1, shrinkage: 0.12, note: "ex-ante (e2e)" },
  totals: { sold_gross: 37500, sell_charges: 37.5, stamp_duty: 1.87, tax: 1552.2, bought: 37458, unfunded: 4.5,
    costs_unknown: ["brokerage, DP and exchange charges (not known to the app)"] },
  notes: ["Methods use no expected returns (e2e)."],
};
const IGNORE = ["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
  "GET /api/disclosures/tracked", "GET /api/portfolio/ais", "GET /api/portfolio/tax", "GET /api/lookthrough/coverage"];

async function open(page: Page, plan: unknown) {
  const api = await mockApi(page, {
    "GET /api/portfolio": SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/connections/summary": [],
    "GET /api/portfolio/optimise": plan,
  });
  await page.goto("/portfolio#allocation");
  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: /^Optimiser/ }) });
  return { api, card };
}

test("allocation: the optimiser runs on request and shows a validated target with its trades (#346)", async ({ page }) => {
  const { api, card } = await open(page, PLAN);
  await expect(card.getByRole("button", { name: "Run optimiser" })).toBeVisible();
  expect(api.count("GET /api/portfolio/optimise")).toBe(0); // nothing is fetched until asked
  await card.getByRole("button", { name: "Run optimiser" }).click();
  await expect(card.getByText("passed", { exact: true })).toBeVisible();
  await expect(card.getByRole("region", { name: "Out-of-sample comparison" }).getByRole("row", { name: /Equal weight 14\.0%/ })).toBeVisible();
  const w = card.getByRole("region", { name: "Target weights" });
  await expect(w.getByRole("row", { name: /Example Volatile Ltd 60\.0% 22\.5%/ })).toBeVisible();
  await expect(w.getByText("only 41 daily returns (the validation needs 378)")).toBeVisible();
  const sells = card.getByRole("region", { name: "Suggested sales" });
  await expect(sells.getByText(/unknown: brokerage/)).toBeVisible();
  await expect(card.getByText(/no order is placed or can be placed/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(IGNORE);
});

test("allocation: a method that fails the out-of-sample check shows no target and no trades (#346)", async ({ page }) => {
  const failed = { ...PLAN, status: "not_validated", sells: [], buys: [], totals: null, risk: null, skipped: [],
    message: "The method did not pass the out-of-sample check, so no target or trade is suggested: out-of-sample volatility 15.0% is above equal weight (14.0%).",
    validation: { ...VALIDATION, passed: false, reasons: ["out-of-sample volatility 15.0% is above equal weight (14.0%)"], vol: { ...VALIDATION.vol, method: 0.15 } },
    holdings: PLAN.holdings.map((h) => ({ ...h, target_pct: null })) };
  const { api, card } = await open(page, failed);
  await card.getByRole("button", { name: "Run optimiser" }).click();
  await expect(card.getByText("not passed", { exact: true })).toBeVisible();
  await expect(card.getByRole("region", { name: "Suggested sales" })).toHaveCount(0);
  await expect(card.getByRole("region", { name: "Target weights" }).getByRole("row", { name: /Example Volatile Ltd 60\.0% —/ })).toBeVisible();
  api.expectClean(IGNORE);
});

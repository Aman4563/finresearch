// The F&O backtest page (#307): a failing strategy says so plainly with its failed criteria, a candidate shows its
// evidence and the rule's current legs, an unknown value reads "unknown" (never 0), and with no saved run the page
// says how to make one. Made-up numbers; no API or database involved.
import { type Page, expect, test } from "@playwright/test";

import type { BacktestsResponse, StrategyResult, WindowStats } from "../src/components/fno/backtest-types";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const NOTICE = { headline: "87.7% lost", text: "SEBI study", source: "SEBI", url: "https://www.sebi.gov.in", date: "2026-08-20" };

const w = (over: Partial<WindowStats>): WindowStats => ({
  from: "2024-07-01", to: "2026-10-09", trades: 41, expiries: 26, wins: 30, losses: 11, win_rate: 0.73, expectancy: 812,
  avg_win: 3100, avg_loss: -5400, worst_trade: -21000, best_trade: 6000, cvar_5: -19000, sd: 5200, total_net: 33292,
  t_stat: 2.4, ci_low: 120, ci_high: 1500, costs: 9100, return_on_margin_annual: 0.06, trades_without_margin: 0, days: 560,
  cagr: 0.014, max_drawdown_pct: 4.2, sharpe: 1.3, sortino: 1.6, daily_sr: 0.08, deflated_sharpe: 0.96, ...over,
});
const crit = (pass: boolean) => [
  { criterion: "OOS trades", value: 41, pass: true, rule: ">= 30" },
  { criterion: "OOS deflated Sharpe", value: pass ? 0.96 : null, pass, rule: ">= 0.95 (N = 8 variants)" },
];
const base: StrategyResult = {
  key: "nifty_pcs_m", name: "NIFTY put credit spread, monthly", role: "candidate", kind: "put_credit", underlying: "NIFTY",
  spec: {}, spec_hash: "a".repeat(64), status: "candidate", reasons: ["passes every pre-registered criterion out of sample"],
  windows: { is: w({ from: "2022-01-01", to: "2024-06-30" }), oos: w({}), oos_stress: w({ expectancy: 300 }) }, criteria: crit(true),
  equity: [["2022-01-03", 1000000], ["2024-06-28", 1020000], ["2026-10-09", 1040000]], skips: { "IV percentile below the filter": 12 },
  by_year: { "2025": w({ trades: 20 }) }, by_iv_regime: { high: w({ trades: 15 }) },
};
const fails: StrategyResult = { ...base, key: "nifty_dcs_m", name: "NIFTY debit call spread, monthly (control)", role: "control",
  status: "fails", reasons: ["fails: OOS deflated Sharpe"], criteria: crit(false), windows: { is: w({}), oos: w({ deflated_sharpe: null }) } };
const REG = { version: "2026-10-12.1", hash: "6d92407630d7f406", registered_on: "2026-10-12", matches_code: true, note: "Registered before any real-data run.",
  variants_tried: 8, specs: [], book: {}, criteria: {}, windows: { is: ["2022-01-01", "2024-06-30"] as [string, string], oos_from: "2024-07-01" },
  doc: "evals/experiments/fno_backtest/PREREG.md", deflated_sharpe_source: "Bailey & López de Prado (2014)" };
const DATA: BacktestsResponse = {
  registration: REG,
  run: { id: 3, created_at: "2026-10-12T12:00:00+00:00", version: "2026-10-12.1", registration_hash: REG.hash, registered_hash: REG.hash,
    oos_evaluated: true, source: "finresearch_fnohist", variants_tried: 8, stock_rule_symbol: null,
    windows: { is: ["2022-01-01", "2024-06-30"], oos: ["2024-07-01", "2026-10-09"] }, generated_at: "2026-10-12T17:30:00+05:30",
    book: {}, criteria: {}, deflated_sharpe_source: "", strategies: [base, fails], note: "A candidate is evidence, not a guarantee.",
    candidates: [{ key: "nifty_pcs_m", name: base.name, oos: base.windows!.oos!, criteria: crit(true), setup: {
      status: "entry", as_of: "2026-10-09", symbol: "NIFTY", expiry: "2026-11-24", dte: 46, lots: 1, lot: 65,
      legs: [{ right: "put", strike: 24300, side: "sell", ref_price: 88.5, fill_assumed: 86.7 }, { right: "put", strike: 23800, side: "buy", ref_price: 41.2, fill_assumed: 42.0 }],
      premium: 2905, max_loss: 29595, margin_approx: null, entry_costs: 61.2, iv_percentile: 0.55, note: "End of day; prices have moved since." } }] },
};

test("fno backtests: candidate with setup, failing strategy plainly labelled, unknowns not zero", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/backtests": DATA, "GET /api/fno/risk-notice": NOTICE });
  await page.goto("/fno/backtests");
  await expect(page.getByText(/Pre-registered 2026-10-12 · 8 variants/)).toBeVisible();
  await expect(page.getByText(/sell 24300 put @ ~88.5/)).toBeVisible();
  await expect(page.getByText(/margin ≈ —/)).toBeVisible(); // unknown margin: a dash, never ₹0
  const failing = page.locator("[id='nifty_dcs_m']");
  await expect(failing.getByText("fails: OOS deflated Sharpe")).toBeVisible();
  await expect(failing.getByText(/OOS deflated Sharpe: unknown/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("fno backtests: no saved run explains how to make one; nothing passed says so", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/backtests": { registration: REG, run: null }, "GET /api/fno/risk-notice": NOTICE });
  await page.goto("/fno/backtests");
  await expect(page.getByText("No backtest saved yet")).toBeVisible();
  await expect(page.getByText(/finresearch fno backtest run/)).toBeVisible();
  api.expectClean();
  await page.unrouteAll({ behavior: "ignoreErrors" });
  const none = { ...DATA, run: { ...DATA.run!, candidates: [], strategies: [fails] } };
  const api2 = await mockApi(page, { "GET /api/fno/backtests": none, "GET /api/fno/risk-notice": NOTICE });
  await page.goto("/fno/backtests");
  await expect(page.getByText(/No strategy passed every pre-registered criterion/)).toBeVisible();
  api2.expectClean();
});

// The F&O Strategy Lab (#318): the variants count and the locked split are stated, a finished run shows its
// overfitting warnings before the numbers and the library beside it, a refused spec shows the server's reason, and a
// paper rule set lists its signals. Made-up numbers; no API or database involved.
import { type Page, expect, test } from "@playwright/test";

import type { WindowStats } from "../src/components/fno/backtest-types";
import type { LabResponse, LabRunView, LabSpec, PaperRule } from "../src/components/fno/lab-types";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const NOTICE = { headline: "87.7% lost", text: "SEBI study", source: "SEBI", url: "https://www.sebi.gov.in", date: "2026-08-20" };
const w = (over: Partial<WindowStats>): WindowStats => ({
  from: "2025-02-01", to: "2026-10-09", trades: 18, expiries: 12, wins: 13, losses: 5, win_rate: 0.72, expectancy: 640,
  avg_win: 2900, avg_loss: -5200, worst_trade: -16000, best_trade: 5000, cvar_5: -16000, sd: 4800, total_net: 11520,
  t_stat: 1.1, ci_low: -900, ci_high: 2100, costs: 4100, return_on_margin_annual: 0.05, trades_without_margin: 0, days: 420,
  cagr: 0.01, max_drawdown_pct: 3.1, sharpe: 0.6, sortino: 0.8, daily_sr: 0.04, deflated_sharpe: 0.41, ...over,
});
const DEFAULT_SPEC: LabSpec = {
  underlying: "NIFTY", structure: "put_credit", expiry: "monthly", dte_min: 30, dte_max: 45, weekdays: [0, 1, 2, 3, 4],
  iv_measure: "percentile", iv_min: null, iv_max: null, trend: "none", strike_by: "delta", delta: 0.2, otm_pct: null,
  width_pct: 2, max_loss_pct: 5, capital: 1_000_000, max_lots: 20, max_concurrent: 2, max_per_expiry: 2, min_days_between: 5,
  profit_take_pct: 50, stop_mult: 2, exit_dte: 7, exit_before_expiry_day: true, exit_before_results: false,
};
const BLOCKS: LabResponse["blocks"] = {
  structures: { put_credit: "Put credit spread (bull put)", call_credit: "Call credit spread (bear call)", iron_condor: "Iron condor",
    debit_call: "Call debit spread (bull call)", debit_put: "Put debit spread (bear put)" },
  trends: { none: "no trend filter", above_ma50: "price above its 50-session average", above_ma200: "price above its 200-session average",
    below_ma50: "price below its 50-session average", below_ma200: "price below its 200-session average" },
  weekdays: ["Mon", "Tue", "Wed", "Thu", "Fri"], max_dte: 70, run_cap_minutes: 15, min_span_days: 540,
  results_note: "Exit before results is applied in paper trading only.", deflated_sharpe_source: "Bailey & López de Prado (2014)",
};
const RUN_ROW = { id: 7, version_id: 3, oos: true, status: "done" as const, progress: 1, progress_note: null, created_at: null,
  started_at: null, finished_at: null, error: null, n_trials: 10, counted: true, n_trades: 41,
  summary: { status: "fails", is: null, oos: null, warnings: 3 } };
const LAB: LabResponse = {
  underlyings: ["NIFTY", "BANKNIFTY"], variants: { library: 8, lab: 2, n: 10 }, blocks: BLOCKS,
  strategies: [{ id: 1, name: "Bull put", underlying: "NIFTY", created_at: null, oos_versions: 2,
    split: { is: ["2022-01-01", "2024-06-30"], oos_from: "2024-07-01", data: ["2022-01-03", "2026-10-09"], rule: "the pre-registered library's windows" },
    versions: [{ id: 3, version: 2, created_at: null, spec: DEFAULT_SPEC, spec_hash: "b".repeat(64), note: "", counted_variant: true,
      describe: "NIFTY put credit spread (bull put); 30-45 DTE monthly; 0.2 delta, wings 2%", runs: [RUN_ROW], paper_rules: [] }] }],
};
const VIEW: LabRunView = {
  ...RUN_ROW, strategy: { id: 1, name: "Bull put", version: 2, underlying: "NIFTY" },
  result: { key: "lab1v2", name: "Bull put (v2)", role: "candidate", kind: "put_credit", underlying: "NIFTY", spec: {}, spec_hash: "b".repeat(64),
    status: "fails", reasons: ["fails: OOS deflated Sharpe"], oos: true, n_trials: 10, sr_trials_variance: 0.0004,
    warnings: ["This strategy has had 2 versions tested out of sample. Only the first look at the out-of-sample years was clean.",
      "10 variants have now been tried in this app (8 pre-registered + 2 lab)."],
    windows_dates: { is: ["2022-01-01", "2024-06-30"], oos: ["2024-07-01", "2026-10-09"] }, book: { capital: 1_000_000 },
    windows: { is: w({ from: "2022-01-01", to: "2024-06-30", sharpe: 1.9 }), oos: w({}), oos_stress: w({ expectancy: -120 }) },
    criteria: [{ criterion: "OOS deflated Sharpe", value: 0.41, pass: false, rule: ">= 0.95 (N = 10 variants: 8 pre-registered + 2 from the lab)" }],
    equity: [["2022-01-03", 1000000], ["2026-10-09", 1011520]], by_year: { "2025": w({}) }, by_iv_regime: { low: w({}) } },
  library: { run_id: 3, oos_evaluated: true, variants_tried: 8, strategies: [
    { key: "nifty_pcs_m", name: "NIFTY put credit spread, monthly", underlying: "NIFTY", status: "fails", is: null,
      oos: { trades: 41, expectancy: 812, win_rate: 0.7, sharpe: 1.1, max_drawdown_pct: 4, deflated_sharpe: null, total_net: 30000 } }] },
  bar_now: { n_trials: 10, deflated_sharpe: 0.41, note: "Recomputed with today's variants count." },
};
const PAPER: PaperRule[] = [{ id: 4, version_id: 3, version: 2, strategy_id: 1, strategy: "Bull put", underlying: "NIFTY", status: "active",
  created_at: "2026-10-10T14:00:00+00:00", start_after: "2026-10-10", last_day: "2026-10-12", note: "12-Oct-2026 close: signal",
  signals: [{ id: 1, day: "2026-10-12", status: "filled", expiry: "2026-11-24", lots: 2, lot: 65, legs: [], premium_at_signal: 3000,
    paper_id: 9, paper_status: "open", mtm_gross: null, net_pnl: null, exit_reason: null, note: null }],
  method: "Signals come from each stored close.", results: { closed: 0, wins: 0, total_net: 0, expectancy: null, max_drawdown: 0 } }];

test("fno lab: variants count, locked split, run warnings first, library beside it, paper signals", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/lab": LAB, "GET /api/fno/lab/paper": { rules: PAPER },
    "GET /api/fno/lab/runs/7": VIEW, "GET /api/fno/risk-notice": NOTICE });
  await page.goto("/fno/lab");
  await expect(page.getByText(/10 variants tried so far: 8 pre-registered \+ 2 from the lab/)).toBeVisible();
  await expect(page.getByText(/out of sample from 2024-07-01 \(locked/)).toBeVisible();
  await expect(page.getByText("2 versions seen out of sample")).toBeVisible();
  await page.getByRole("button", { name: /#7 OOS done/ }).click();
  await expect(page.getByText(/Only the first look at the out-of-sample years was clean/)).toBeVisible();
  await expect(page.getByText(/N = 10 variants: 8 pre-registered \+ 2 from the lab/)).toBeVisible();
  await expect(page.getByRole("cell", { name: "NIFTY put credit spread, monthly" })).toBeVisible();
  await expect(page.getByRole("link", { name: "#9 open" })).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("fno lab: a refused spec shows the server's reason", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/lab": { ...LAB, strategies: [] }, "GET /api/fno/lab/paper": { rules: [] },
    "GET /api/fno/risk-notice": NOTICE,
    "POST /api/fno/lab/strategies": [422, { detail: "only 200 days of stored history; a strategy needs at least 540" }] });
  await page.goto("/fno/lab");
  await expect(page.getByText("No strategy yet")).toBeVisible();
  await page.getByRole("button", { name: "Save strategy" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /at least 540/ })).toBeVisible();
  api.expectClean();
});

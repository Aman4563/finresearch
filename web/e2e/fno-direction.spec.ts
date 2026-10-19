// The direction panel on /fno (#327): with no accepted model it shows the market-implied probabilities labelled
// "no edge", NO TRADE with each candidate's EV, the out-of-sample table and calibration chart, and logs a forecast on
// demand; switching the horizon refetches. Made-up numbers; no API, database or broker.
import { type Page, expect, test } from "@playwright/test";

import type { Chain } from "../src/components/fno/model";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const q = (p: string) => ({ last_price: p, iv: "14.0", oi: "5000", change_in_oi: "10", volume: "1000", bid: p, ask: p });
const CHAIN: Chain = {
  symbol: "NIFTY", expiry: "2026-10-20", underlying: "25000", as_of: "2026-10-13T09:30:00+05:30", lot_size: 65, atm_strike: "25000",
  atm_iv: { call: "14.0", put: "14.5" }, pcr_oi: "1.10", max_pain: "25000", source_label: "NSE",
  rows: [24800, 24900, 25000, 25100, 25200].map((k) => ({ strike: String(k), call: q("100"), put: q("90") })),
};
const ev = (implied: number, logistic: number) => ({ implied, base_rate: implied - 0.01, logistic, shown: implied, shown_model: "implied", status: "no_edge",
  label: "market-implied, no edge", train_n: 900, text: "closes above today's close" });
const hz = (h: number) => ({ h, sigma_pct: 0.9 * Math.sqrt(h), threshold_pct: 0.45 * Math.sqrt(h),
  events: { up: ev(0.51, 0.53), up_k: ev(0.3, 0.29), down_k: ev(0.31, 0.33) } });
const stats = (brier: number) => ({ n: 566, base_rate: 0.48, mean_p: 0.5, brier, log_loss: 0.69, hit_rate: 0.48, auc: 0.52, calibration_intercept: 0, calibration_slope: 1.37,
  reliability: [0.4, 0.45, 0.5, 0.55, 0.6].map((p) => ({ n: 113, mean_p: p, observed: p - 0.03, ci: [p - 0.12, p + 0.06] })) });
const test_ = (bss: number, p: number) => ({ brier_skill: bss, log_loss_skill: bss, dm: -1, p, p_holm: 1, lag: 5 });
const cell = (h: number, event: string) => ({ symbol: "NIFTY", horizon: h, event, event_text: "", status: "no_edge", reasons: ["fails: Brier skill vs implied"],
  is: { n: 348, from: "2023-01-02", to: "2024-06-28", models: {}, tests: {} },
  oos: { n: 566, from: "2024-07-01", to: "2026-10-06", models: { base_rate: stats(0.253), implied: stats(0.2523), logistic: stats(0.2556) },
    tests: { logistic_vs_implied: test_(-0.0132, 0.86), logistic_vs_base_rate: test_(-0.0101, 0.87), base_rate_vs_implied: test_(-0.003, 0.58) } } });
const cand = (key: string, label: string, view: string, evm: number) => ({ key, label, view, status: "fails", why: ["the probabilities come from no accepted model (market-implied, no edge)"],
  ev_model: evm, ev_implied: evm, edge: 0, costs: 120, max_loss: -6500, legs: [{ right: "call", strike: 25000, side: "buy", price: 100, iv: 14 }] });
const STATE = (h: number) => ({
  symbol: "NIFTY", as_of: "2026-10-12", spot: 25000, iv_30: 13.9, horizons: [1, 5, 10, 20].map(hz), accepted: [], model_status: "no_edge",
  headline: { h: 5, p_up: 0.51, label: "market-implied, no edge", call_or_put: null },
  run: { id: 1, created_at: "2026-10-19T20:00:00+05:30", verdict: "no edge: no logistic cell passed the pre-registered out-of-sample criteria",
    source: "finresearch_fnohist2 (read only)", registration_hash: "372dabfe1d07", windows: { oos_start: "2024-07-01" },
    criteria: { min_oos_n: 250, alpha: 0.05, slope_low: 0.5, slope_high: 3 }, cells: [1, 5, 10, 20].flatMap((x) => ["up", "up_k", "down_k"].map((e) => cell(x, e))) },
  live_accuracy: [{ method: "m", event_kind: `fno_dir_up_h${h}`, h, event: "up", model: "implied", logged: 12, open: 4, resolved: 8, independent: 2, void: 0,
    summary: { n: 2, brier: 0.24, brier_skill: null, hit_rate: 0.5, base_rate: 0.5 } }],
  decision_horizon: h, evidence_note: "Probabilities are for the index's close h sessions after the stored close.",
  registration: { hash: "372dabfe1d070d22", registered_on: "2026-10-19", matches_code: true, doc: "PREREG.md" },
  decision: { status: "ok", action: "NO TRADE", label: "NO TRADE: no measured edge", chosen: null, horizon: h, expiry: "2026-10-20", horizon_day: "2026-10-19",
    held_to_expiry: false, lot_size: 65, plan: null, notes: [],
    candidates: [cand("long_call", "Long call (ATM)", "up", -310), cand("long_put", "Long put (ATM)", "down", -295)],
    non_directional: { text: "No measured directional edge. Non-directional plans: the pre-registered library.", suggest_api: "/api/fno/NIFTY/suggest", href: "/fno?symbol=NIFTY", evidence_href: "/fno/backtests" } },
});
const ROUTES = {
  "GET /api/fno/NIFTY/expiries": { symbol: "NIFTY", expiries: ["2026-10-20", "2026-10-27"], strikes: [] },
  "GET /api/fno/NIFTY/chain": CHAIN,
  "GET /api/fno/risk-notice": { headline: "87.7% lost", text: "SEBI study", source: "SEBI", url: "https://www.sebi.gov.in", date: "2026-08-20" },
  "GET /api/fno/direction/NIFTY": STATE(5),
  "POST /api/fno/direction/NIFTY/log": { logged: [{ id: 1 }, { id: 2 }], skipped: null, as_of: "2026-10-12" },
};
const OPTIONAL = ["GET /api/market/status", "GET /api/fno/positions", "GET /api/fno/paper", "GET /api/fno/NIFTY/events", "GET /api/fno/NIFTY/iv", "GET /api/fno/NIFTY/context"];

test("fno: the direction panel says no edge, NO TRADE, shows out-of-sample accuracy and logs a forecast", async ({ page }) => {
  const api = await mockApi(page, ROUTES);
  await page.goto("/fno");
  const panel = page.locator("section").filter({ has: page.getByRole("heading", { name: "CALL or PUT?" }) });
  await expect(panel.getByText("no edge: showing market-implied probabilities")).toBeVisible();
  await expect(panel.getByText("NO TRADE: no measured edge")).toBeVisible();
  await expect(panel.getByText("Long put (ATM)")).toBeVisible();
  await expect(panel.getByRole("img", { name: /Calibration: Logistic and Market-implied/ })).toBeVisible();
  await expect(panel.getByText(/Walk-forward from 2024-07-01/)).toBeVisible();
  await expect(panel.getByRole("button", { name: "Best non-directional plan" })).toBeVisible();
  await panel.getByRole("button", { name: "Log today's forecast" }).click();
  await expect(panel.getByText("Logged 2 forecasts from the 2026-10-12 close.")).toBeVisible();
  expect(api.count("POST /api/fno/direction/NIFTY/log")).toBe(1);
  await panel.getByRole("tab", { name: "20d" }).click();
  await expect.poll(() => api.count("GET /api/fno/direction/NIFTY")).toBeGreaterThan(1);
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(OPTIONAL);
});

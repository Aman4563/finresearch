// F&O trade plans and market context (#317): the context panel labels every indicator with its evidence and says it
// is not a signal; "Suggest setups" shows an AVOID card with its failed backtest and a qualified ENTER card; the
// one-click action records a paper trade and arms its exit alerts. Made-up chain and plans; no API or broker.
import { type Page, expect, test } from "@playwright/test";

import type { Chain } from "../src/components/fno/model";
import type { MarketContext, TradePlan } from "../src/components/fno/plan-types";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const q = (p: string, oi: string, chg: string) => ({ last_price: p, iv: "14.0", oi, change_in_oi: chg, volume: "1000", bid: p, ask: p });
const CHAIN: Chain = {
  symbol: "NIFTY", expiry: "2026-10-20", underlying: "25000", as_of: "2026-10-13T09:30:00+05:30", lot_size: 65, atm_strike: "25000",
  atm_iv: { call: "14.0", put: "14.5" }, pcr_oi: "1.10", max_pain: "25000", source_label: "NSE",
  rows: [24800, 24900, 25000, 25100, 25200].map((k) => ({ strike: String(k), call: q("100", "5000", "200"), put: q("90", "6000", "-100") })),
};
const ev = (level: string) => ({ measures: "What it measures.", level, evidence: "What studies found." });
const CONTEXT: MarketContext = {
  symbol: "NIFTY", today: "2026-10-13", not_a_signal: "Context, not signals: none of these indicators changes the trade-plan verdict.",
  trend: { close: 25000, closes: 290, source: "stored", trend: "uptrend", strength: "emerging", note: null,
    averages: [{ days: 20, sma: 24800, slope_pct_10d: 0.4, distance_pct: 0.8 }, { days: 50, sma: 24500, slope_pct_10d: 0.3, distance_pct: 2.0 }, { days: 200, sma: 24000, slope_pct_10d: 0.1, distance_pct: 4.2 }],
    adx: { adx: 22.5, plus_di: 25, minus_di: 18, bars: 290, on: "near-month future" }, evidence: ev("mixed"), adx_evidence: ev("none verified") },
  volatility: { atm_iv: 14.2, realised_20d: 11.1, realised_60d: 12.0, iv_minus_rv20: 3.1, iv_rank: 0.3, iv_percentile: 0.42, iv_days: 252, iv_status: "ok",
    iv_source: "NSE F&O bhavcopy, 30-day IV", skew_25d: 2.1, evidence: ev("documented on average"), rank_evidence: ev("none verified"), skew_evidence: ev("weak / US stocks only") },
  term: { near_iv: 14.0, next_iv: 14.6, near_expiry: "2026-10-20", next_expiry: "2026-10-27", shape: "normal (upward)", day: "2026-10-10", evidence: ev("weak / US only") },
  chain: { pcr_oi: 1.1, pcr_volume: 0.92, max_pain: 25000, spot: 25000, source: "NSE", as_of: null, pcr_evidence: ev("weak / contested"), max_pain_evidence: ev("weak / contested") },
  buildup: { futures: { day: "2026-10-10", previous_day: "2026-10-09", source: "NSE F&O bhavcopy", rows: [{ expiry: "2026-10-27", close: 25050, price_change: -60, oi: 1.2e7, oi_change: 3e5, buildup: "short build-up" }] },
    options: { status: "ok", source: "NSE option chain (live)", rows: [{ strike: 25000, call: { price_change: -10, oi_change: 200, oi: 5000, buildup: "short build-up" }, put: { price_change: 5, oi_change: 100, oi: 6000, buildup: "long build-up" } }] },
    evidence: ev("none verified"), method: "Price up + OI up = long build-up." },
  participants: { day: "2026-10-10", previous_day: "2026-10-09", source: "NSE", url: "https://www.nseindia.com", evidence: ev("none verified"),
    rows: [{ participant: "FII", net: { index_futures: -293988, stock_futures: 1, index_calls: -346818, index_puts: 540492 }, net_change: { index_futures: -2000, stock_futures: 0, index_calls: 10, index_puts: -5 } }] },
  events: { items: [{ day: "2026-10-20", kind: "expiry", title: "NIFTY expiry" }], rbi: { status: "no_source", note: "RBI policy dates: no source in the app yet" }, evidence: ev("documented (risk, not direction)") },
};
const evidence = (level: TradePlan["evidence"]["level"], text: string) => ({ spec_key: "nifty_pcs_m", spec_name: "NIFTY put credit spread, monthly", match: "exact" as const, status: "fails", level, raw_level: level, text, url: "/fno/backtests" });
const AVOID: TradePlan = { title: "NIFTY · NIFTY put credit spread, monthly", symbol: "NIFTY", kind: "put_credit", action: "AVOID", qualifier: null, label: "AVOID",
  failed: ["Entry window"], reasons: ["No monthly expiry 30-45 days away today.", "The pre-registered backtest: FAILED out-of-sample: expectancy ₹-1,789/trade, t=-1.3."],
  legs: [], spec_key: "nifty_pcs_m", evidence: evidence("negative", "FAILED out-of-sample: expectancy ₹-1,789/trade, t=-1.3, 42 trades.") };
const lvl = (r: number, p: number, b: number) => ({ rupees: r, premium_per_share: p, underlying_now: { below: b, above: null }, underlying_at_expiry: { below: b + 50, above: null } });
const ENTER: TradePlan = { title: "NIFTY 20-Oct-2026 · NIFTY short iron condor, weekly", symbol: "NIFTY", expiry: "2026-10-20", days_to_expiry: 7, spot: 25000, lot_size: 65,
  kind: "iron_condor", action: "ENTER", qualifier: "no proven edge — size small", label: "ENTER (no proven edge — size small)", failed: [], spec_key: "nifty_ic_w",
  reasons: ["Every check passes: defined risk, within budget, EV after costs no worse than fair, IV fits."],
  legs: [{ right: "put", strike: 24600, side: "sell", lots: 1 }, { right: "put", strike: 24350, side: "buy", lots: 1 }, { right: "call", strike: 25400, side: "sell", lots: 1 }, { right: "call", strike: 25650, side: "buy", lots: 1 }],
  size: { multiple: 3, suggested: 1, budget: 20000, risk_per_set: 6000, reason: "₹20,000 budget ÷ ₹6,000 risk per set = 3; no proven edge, so half of it: 1", capital: 1e6, max_loss_pct: 2 },
  entry: { legs: [], status: "ok", net_mid: 42.5, net_fill_now: 40, notes: [], levels_priced_at: "bid/ask mid" },
  max_profit: 2762, max_loss: -13488, breakevens: [24557.5, 25442.5], net_premium: -2762, costs: 180,
  probability: { real_world: 0.71, real_world_window: "60-day", risk_neutral: 0.66, note: "Model probabilities." },
  exits: { source: "spec:nifty_ic_w", rule_of_thumb: null, stop: lvl(5525, 127.5, 24420), target: lvl(1381, 21.25, 24990), entry_net: 2762, credit: true, per_share_unit: 65, notes: [],
    time: { dte: 1, by: "2026-10-19", text: "Close on the last session before expiry day." }, profit_pct: 50, stop_pct: 200 },
  exit_rules: [{ kind: "profit", threshold: 50, why: "profit target" }, { kind: "stop", threshold: 200, why: "stop-loss" }, { kind: "dte", threshold: 1, why: "time exit" }],
  adjustments: { rules: ["Do not add to a losing short-premium position."], note: "Rules of thumb, not backtested." }, invalidation: ["The underlying closes below the short put strike 24,600."],
  evidence: { ...evidence("none", "Not judged out of sample (fails)."), spec_key: "nifty_ic_w", match: "exact" },
  paper: { trades: 2, open: 0, closed: 2, wins: 1, losses: 1, total_net: -320, expectancy: -160, note: "Too few to be evidence." }, disclaimer: "Nothing is ordered. Not advice." };
const NOTICE = { headline: "87.7% lost", text: "SEBI study", source: "SEBI", url: "https://www.sebi.gov.in", date: "2026-08-20" };
const ROUTES = {
  "GET /api/fno/NIFTY/expiries": { symbol: "NIFTY", expiries: ["2026-10-20", "2026-10-27"], strikes: [] },
  "GET /api/fno/NIFTY/chain": CHAIN, "GET /api/fno/risk-notice": NOTICE, "GET /api/fno/NIFTY/context": CONTEXT,
  "GET /api/fno/NIFTY/suggest": { symbol: "NIFTY", today: "2026-10-13", cards: [AVOID, ENTER], note: "The library's pre-registered recipes. Not a recommendation." },
  "POST /api/fno/paper": { id: 7, legs: [] }, "POST /api/fno/exit-rules": { id: 1 },
};
const OPTIONAL = ["GET /api/market/status", "GET /api/fno/positions", "GET /api/fno/paper", "GET /api/fno/NIFTY/events", "GET /api/fno/NIFTY/iv"];

test("fno: market context labels evidence; suggest shows AVOID with the failed backtest and a qualified ENTER", async ({ page }) => {
  const api = await mockApi(page, ROUTES);
  await page.goto("/fno");
  const ctx = page.locator("section").filter({ has: page.getByRole("heading", { name: "Market context" }) });
  await expect(page.getByText(/Context, not signals/)).toBeVisible();
  await expect(ctx.getByText("Trend: uptrend (emerging)")).toBeVisible();
  await expect(ctx.getByText("evidence: weak / contested").first()).toBeVisible();
  await expect(ctx.getByText(/Contracts long − short/)).toBeVisible();
  await page.getByRole("button", { name: "Suggest setups" }).click();
  const avoid = page.getByRole("article", { name: /NIFTY put credit spread/ });
  await expect(avoid.getByTestId("plan-verdict")).toHaveText("AVOID");
  await expect(avoid.getByTestId("plan-evidence")).toContainText("FAILED out-of-sample: expectancy ₹-1,789/trade, t=-1.3");
  const enter = page.getByRole("article", { name: /short iron condor/ });
  await expect(enter.getByTestId("plan-verdict")).toHaveText("ENTER (no proven edge — size small)");
  await expect(enter.getByText(/1× these legs/)).toBeVisible();
  await expect(enter.getByText(/buy back at ₹127.50\/share/)).toBeVisible();
  await enter.getByRole("button", { name: "Paper trade + alerts" }).click();
  await expect(enter.getByRole("status")).toContainText("Paper trade #7 recorded with 3 exit alerts");
  expect(api.count("POST /api/fno/paper")).toBe(1);
  expect(api.count("POST /api/fno/exit-rules")).toBe(3);
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(OPTIONAL);
});

// Synthetic F&O answers for the e2e tests (#307 #308 #317 #318 #326): a made-up chain, trade plans, market context,
// positions, paper book, backtests and lab runs. No API, broker or personal data. Each fixture is typed with the app's
// own response types, so `pnpm typecheck` fails when a shape drifts. `LONG_*` variants stress the layout: nine-digit
// rupee values, crore-sized open interest and strategy names far longer than any real one (#326).
import type { BacktestsResponse, StrategyResult, WindowStats } from "../src/components/fno/backtest-types";
import type { PaperBook, PaperTrade, PositionsView, EventStripData } from "../src/components/fno/book-types";
import type { LabResponse, LabRunView, LabSpec, LabTrade, PaperRule } from "../src/components/fno/lab-types";
import type { Analysis, Chain, Result } from "../src/components/fno/model";
import type { MarketContext, SuggestResponse, TradePlan } from "../src/components/fno/plan-types";

export const NOTICE = { headline: "87.7% lost", text: "SEBI study", source: "SEBI", url: "https://www.sebi.gov.in", date: "2026-08-20" };

// ------------------------------------------------------------------ /fno: chain, context, plans (#317)
const q = (p: string, oi: string, chg: string) => ({ last_price: p, iv: "14.0", oi, change_in_oi: chg, volume: "1000", bid: p, ask: p });
export const CHAIN: Chain = {
  symbol: "NIFTY", expiry: "2026-10-20", underlying: "25000", as_of: "2026-10-13T09:30:00+05:30", lot_size: 65, atm_strike: "25000",
  atm_iv: { call: "14.0", put: "14.5" }, pcr_oi: "1.10", max_pain: "25000", source_label: "NSE",
  rows: [24800, 24900, 25000, 25100, 25200].map((k) => ({ strike: String(k), call: q("100", "5000", "200"), put: q("90", "6000", "-100") })),
};
/** 41 strikes, crore-sized open interest and five-digit premiums: the widest chain the page should hold. */
export const WIDE_CHAIN: Chain = {
  ...CHAIN,
  rows: Array.from({ length: 41 }, (_, i) => 24000 + i * 50).map((k, i) => {
    const d = (k - 25000) / 50;
    const call = Math.max(1.05, 1000 - k / 25 + 20 * Math.exp(-d * d / 50)).toFixed(2);
    const put = Math.max(1.05, k / 25 - 1000 + 20 * Math.exp(-d * d / 50)).toFixed(2);
    return { strike: String(k), call: q((Number(call) * 13.37).toFixed(2), String(123456789 - i * 1e6), String((i % 3 - 1) * 4567890)),
      put: q((Number(put) * 13.37).toFixed(2), String(98765432 + i * 1e6), String((i % 4 - 2) * 3456789)) };
  }),
};

const ev = (level: string) => ({ measures: "What it measures.", level, evidence: "What studies found." });
export const CONTEXT: MarketContext = {
  symbol: "NIFTY", today: "2026-10-13", not_a_signal: "Context, not signals: none of these indicators changes the trade-plan verdict.",
  trend: { close: 25000, closes: 290, source: "stored", trend: "uptrend", strength: "emerging", note: null,
    averages: [{ days: 20, sma: 24800, slope_pct_10d: 0.4, distance_pct: 0.8 }, { days: 50, sma: 24500, slope_pct_10d: 0.3, distance_pct: 2.0 }, { days: 200, sma: 24000, slope_pct_10d: 0.1, distance_pct: 4.2 }],
    adx: { adx: 22.5, plus_di: 25, minus_di: 18, bars: 290, on: "front future, ratio-adjusted at each roll as the direction model's ADX" }, evidence: ev("mixed"), adx_evidence: ev("none verified") },
  volatility: { atm_iv: 14.2, realised_20d: 11.1, realised_60d: 12.0, iv_minus_rv20: 2.5, iv_minus_rv20_label: "30-day IV 13.6% − 20-day realised 11.1% (NSE F&O bhavcopy, both at the 2026-10-10 close)", iv_rank: 0.3, iv_percentile: 0.42, iv_days: 252, iv_status: "ok",
    iv_source: "NSE F&O bhavcopy, 30-day IV", skew_25d: 2.1, evidence: ev("documented on average"), rank_evidence: ev("none verified"), skew_evidence: ev("weak / US stocks only") },
  term: { near_iv: 14.0, next_iv: 14.6, near_expiry: "2026-10-20", next_expiry: "2026-10-27", shape: "next above near", slope: 0.6, day: "2026-10-10", evidence: ev("weak / US only") },
  chain: { pcr_oi: 1.1, pcr_volume: 0.92, max_pain: 25000, spot: 25000, source: "NSE", as_of: null, pcr_evidence: ev("weak / contested"), max_pain_evidence: ev("weak / contested") },
  buildup: { futures: { day: "2026-10-10", previous_day: "2026-10-09", source: "NSE F&O bhavcopy", rows: [{ expiry: "2026-10-27", close: 25050, price_change: -60, oi: 1.2e7, oi_change: 3e5, buildup: "short build-up" }] },
    options: { status: "ok", source: "NSE option chain (live)", rows: [{ strike: 25000, call: { price_change: -10, oi_change: 200, oi: 5000, buildup: "short build-up" }, put: { price_change: 5, oi_change: 100, oi: 6000, buildup: "long build-up" } }] },
    evidence: ev("none verified"), method: "Price up + OI up = long build-up." },
  participants: { day: "2026-10-10", previous_day: "2026-10-09", source: "NSE", url: "https://www.nseindia.com", evidence: ev("none verified"),
    rows: [{ participant: "FII", net: { index_futures: -293988, stock_futures: 1, index_calls: -346818, index_puts: 540492 }, net_change: { index_futures: -2000, stock_futures: 0, index_calls: 10, index_puts: -5 } }] },
  events: { items: [{ day: "2026-10-20", kind: "expiry", title: "NIFTY expiry" }], rbi: { status: "no_source", note: "RBI policy dates: no source in the app yet" }, evidence: ev("documented (risk, not direction)") },
};
/** Every participant, seven-digit nets and changes, many build-up strikes. */
export const LONG_CONTEXT: MarketContext = {
  ...CONTEXT,
  participants: { ...CONTEXT.participants, rows: ["Client", "DII", "FII", "Pro"].map((participant, i) => ({ participant,
    net: { index_futures: -2939880 * (i + 1), stock_futures: 1, index_calls: -3468180 * (i + 1), index_puts: 5404920 * (i + 1) },
    net_change: { index_futures: -200000 * (i + 1), stock_futures: 0, index_calls: 1000000, index_puts: -500000 } })) },
  buildup: { ...CONTEXT.buildup, futures: { ...CONTEXT.buildup.futures!, rows: [
    { expiry: "2026-10-27", close: 25050, price_change: -60, oi: 1.2e7, oi_change: 3456789, buildup: "short build-up" },
    { expiry: "2026-11-24", close: 25150, price_change: 45, oi: 9.8e6, oi_change: -1234567, buildup: "short covering" }] },
    options: { status: "ok", source: "NSE option chain (live)", rows: Array.from({ length: 21 }, (_, i) => ({ strike: 24500 + i * 50,
      call: { price_change: -10, oi_change: 200, oi: 5000, buildup: i % 2 ? "short build-up" : "long unwinding" },
      put: { price_change: 5, oi_change: 100, oi: 6000, buildup: i % 3 ? "long build-up" : "short covering" } })) } },
};

const evidence = (level: TradePlan["evidence"]["level"], text: string) => ({ spec_key: "nifty_pcs_m", spec_name: "NIFTY put credit spread, monthly", match: "exact" as const, status: "fails", level, raw_level: level, text, url: "/fno/backtests" });
export const AVOID: TradePlan = { title: "NIFTY · NIFTY put credit spread, monthly", symbol: "NIFTY", kind: "put_credit", action: "AVOID", qualifier: null, label: "AVOID",
  failed: ["Max loss within your F&O budget"], reasons: ["Max loss within your F&O budget: one set risks ₹12,375, above your ₹2,000 budget."],
  legs: [], spec_key: "nifty_pcs_m", evidence: evidence("directional", "FAILED out-of-sample, directional — reflects the index path, not the structure: expectancy ₹-1,789/trade, t=-1.3, 95% CI ₹-4,581 to ₹569, 47 trades.") };
const lvl = (r: number, p: number, b: number) => ({ rupees: r, premium_per_share: p, underlying_now: { below: b, above: null }, underlying_at_expiry: { below: b + 50, above: null } });
export const PAPER: TradePlan = { title: "NIFTY 20-Oct-2026 · NIFTY short iron condor, weekly", symbol: "NIFTY", expiry: "2026-10-20", days_to_expiry: 7, spot: 25000, lot_size: 65,
  kind: "iron_condor", action: "PAPER", qualifier: "NO PROVEN EDGE — paper only (0 real lots)", label: "NO PROVEN EDGE — paper only (0 real lots)", failed: [], spec_key: "nifty_ic_w",
  reasons: ["Every check passes: defined risk, within budget, EV after costs no worse than fair, IV fits."],
  legs: [{ right: "put", strike: 24600, side: "sell", lots: 1 }, { right: "put", strike: 24350, side: "buy", lots: 1 }, { right: "call", strike: 25400, side: "sell", lots: 1 }, { right: "call", strike: 25650, side: "buy", lots: 1 }],
  size: { multiple: 3, suggested: 3, real_lots: 0, open_book: { budget: 80000, budget_pct: 8, open_risk: 12000, open_same: 0, max_concurrent: 1, mtd_paper: -1500, month_stop_pct: 6, notes: ["Groww is not connected: only paper positions are counted in the open book."] }, budget: 20000, risk_per_set: 6000, reason: "₹20,000 budget ÷ ₹6,000 risk per set = 3; no strategy that passed out of sample matches, so paper only: 0 real-money lots", capital: 1e6, max_loss_pct: 2 },
  entry: { legs: [], status: "ok", net_mid: 42.5, net_fill_now: 40, notes: [], levels_priced_at: "bid/ask mid" },
  max_profit: 2762, max_loss: -13488, breakevens: [24557.5, 25442.5], net_premium: -2762, costs: 180,
  probability: { real_world: 0.71, real_world_window: "60-day", risk_neutral: 0.66, note: "Model probabilities." },
  exits: { source: "spec:nifty_ic_w", rule_of_thumb: null, stop: lvl(5525, 127.5, 24420), target: lvl(1381, 21.25, 24990), entry_net: 2762, credit: true, per_share_unit: 65, notes: [],
    time: { dte: 1, by: "2026-10-19", text: "Close on the last session before expiry day." }, profit_pct: 50, stop_pct: 200 },
  exit_rules: [{ kind: "profit", threshold: 50, why: "profit target" }, { kind: "stop", threshold: 200, why: "stop-loss" }, { kind: "dte", threshold: 1, why: "time exit" }],
  adjustments: { rules: ["Do not add to a losing short-premium position."], note: "Rules of thumb, not backtested." }, invalidation: ["The underlying closes below the short put strike 24,600."],
  evidence: { ...evidence("none", "Not judged out of sample (fails)."), spec_key: "nifty_ic_w", match: "exact" },
  paper: { trades: 2, open: 0, closed: 2, wins: 1, losses: 1, total_net: -320, expectancy: -160, note: "Too few to be evidence." }, disclaimer: "Nothing is ordered. Not advice." };
const WAIT: TradePlan = { ...PAPER, title: "NIFTY 27-Oct-2026 · NIFTY call debit spread, weekly", action: "WAIT", qualifier: "IV too high for a debit", label: "WAIT",
  kind: "debit_call", failed: ["IV fit"], reasons: ["IV percentile 88 is above the recipe's 60 limit for buying premium."], spec_key: "nifty_dcs_w",
  legs: [{ right: "call", strike: 25000, side: "buy", lots: 1 }, { right: "call", strike: 25300, side: "sell", lots: 1 }], max_loss: -6500, max_profit: 13000, breakevens: [25100] };
export const SUGGEST: SuggestResponse = { symbol: "NIFTY", today: "2026-10-13", cards: [AVOID, PAPER, WAIT], note: "The library's pre-registered recipes. Not a recommendation." };

const LONG_NAME = "NIFTY 20-Oct-2026 · NIFTY short iron condor with ultra-wide protective wings, a delta-neutral adjustment rule and a pre-earnings exit, weekly (version 12)";
const big = (x: number) => x * 12345;
export const LONG_PAPER: TradePlan = { ...PAPER, title: LONG_NAME, kind: "iron_condor_with_delta_neutral_adjustments", label: "NO PROVEN EDGE — paper only (0 real lots), paper first, half size",
  spot: 125000.75, max_profit: big(2762), max_loss: -big(13488), breakevens: [124557.5, 125442.5, 126013.25], costs: big(180),
  legs: [...PAPER.legs, { right: "put" as const, strike: 24100, side: "buy" as const, lots: 25 }, { right: "call" as const, strike: 25900, side: "buy" as const, lots: 25 }].map((l) => ({ ...l, strike: l.strike + 100000, lots: 25 })),
  size: { ...PAPER.size!, suggested: 25, budget: big(20000), reason: "₹24,69,00,000 budget ÷ ₹7,40,70,000 risk per set = 3; no strategy that passed out of sample matches, so paper only: 0 real-money lots" },
  entry: { legs: [{ right: "put", strike: 124600, side: "sell", lots: 25, bid: 12345.25, ask: 12399.75, last: 12370, mid: 12372.5, limit_from: 12372.5, limit_to: 12345.25, spread_pct: 0.44, slippage: 27.25, status: "ok" }],
    status: "ok", net_mid: 12342.5, net_fill_now: 12240, notes: ["The far leg's spread is over 3%: fill the near legs first."], levels_priced_at: "bid/ask mid",
    source: "bid/ask: NSE chain at 09:31", slippage_inr: big(1500) },
  exits: { ...PAPER.exits!, stop: lvl(big(5525), 12127.5, 124420), target: lvl(big(1381), 2021.25, 124990), notes: ["A results meeting falls before expiry: the stop may gap through."], rule_of_thumb: "Close at 50% of the credit or 21 days to expiry, whichever comes first." },
  paper: { ...PAPER.paper!, total_net: -big(320) } };
export const LONG_SUGGEST: SuggestResponse = { ...SUGGEST, cards: [{ ...AVOID, title: `${LONG_NAME} (avoid)` }, LONG_PAPER] };

// POST /api/fno/strategy: the builder's analysis of an iron condor
const costs = (total: number) => ({ lines: { stt: 12.5, exchange: 18.24, sebi: 0.05, stamp: 1.2, brokerage: 80, gst: 17.68 }, total, turnover: 52000, orders: 4,
  rates: [{ key: "stt", rate_pct: 0.1, basis: "of the sell-side premium", effective_from: "2024-10-01", source: "https://www.nseindia.com", status: "verified" as const, note: "" },
    { key: "exchange", rate_pct: 0.03503, basis: "of the premium", effective_from: "2024-10-01", source: "NSE circular", status: "unconfirmed" as const, note: "Rate not confirmed since the last revision." }] });
const out = (pop: number, ev: number) => ({ vol: 0.14, drift: 0.065, pop, ev, ev_gross: ev + 120, quantiles: {}, window: "60-day" });
export const ANALYSIS: Analysis = {
  spot: 25000, days_to_expiry: 7, rate: 0.065, drift: 0, max_loss: -13488, net_vega: -120,
  vols: { atm_iv: 14.2, atm_strike: 25000, realised_20d: 0.111, realised_60d: 0.12, closes: 290 },
  costs: { entry: costs(129.67), square_off_at_same_prices: costs(129.67), expected_exercise_stt: 4.5, total_to_expiry: 263.84, brokerage_per_order: 20, note: "From the dated charges table." },
  risk_neutral: out(0.66, -210), real_world: out(0.71, 140), real_world_20d: out(0.73, 160), real_world_60d: out(0.71, 140),
  fair_price_note: "Under the risk-neutral model the position is roughly fair before costs; costs make it slightly negative.",
  expected_move: { move: 520, low: 24480, high: 25520, vol: 0.142 },
  capital_check: { capital: 1e6, max_loss: -13488, pct: 1.35, limit_pct: 2, within: true, message: "Max loss is 1.3% of your ₹10,00,000 F&O capital (limit 2%)." },
  drawdown: { probability: 0.12, threshold_pct: 10, repeats: 20 },
  iv_context: { n: 252, current: 14.2, rank: 0.3, percentile: 0.42, status: "ok", skew_25d: 2.1, iv_rv_ratio: 1.28 },
  notes: ["The short call is within the expected move."],
};
export const RESULT: Result = { analysis: ANALYSIS, breakevens: [24557, 25443], max_profit: 2762, max_loss: -13488, net_premium: -2762,
  model_probability_of_profit: 0.66, rate: { rate: 0.065, par_yield: 6.5, tenor_years: 0.02, as_of: "2026-10-12", fallback: false, source: "FBIL", note: "par yield at the expiry" },
  net_greeks: { delta: -1.2, gamma: -0.004, vega: -120.5, theta: 310.2 }, curve: [], lot_size: 65, notes: [], disclaimer: "Model numbers, not advice. No order is placed." };
export const LONG_RESULT: Result = { ...RESULT, max_profit: big(2762), max_loss: -big(13488), net_premium: -big(2762), breakevens: [124557, 125443],
  analysis: { ...ANALYSIS, expected_move: { move: 123456, low: 1234567, high: 1481479, vol: 0.142 }, risk_neutral: out(0.66, -big(210)), real_world: out(0.71, big(140)) },
  net_greeks: { delta: -123456.7, gamma: -1234.567, vega: -9876543.2, theta: 1234567.8 } };

export const EVENTS: EventStripData = { symbol: "NIFTY", today: "2026-10-13",
  items: [{ day: "2026-10-20", kind: "expiry", title: "NIFTY weekly expiry", source: "NSE" }, { day: "2026-10-27", kind: "expiry", title: "NIFTY monthly expiry", source: "NSE" }],
  results: { status: "none" }, rbi: { status: "no_source", note: "RBI policy dates: no source in the app yet" } };

const pleg = (symbol: string, strike: number, right: "call" | "put", qty: number, pnl: number) => ({
  symbol, product: "NRML", quantity: qty, qty, direction_known: true, note: null, avg_price: 120.5, realised_pnl: null, contract_known: true,
  underlying: "NIFTY", expiry: "2026-10-27", strike, right, lot_size: 65, ltp: 98.25, unrealised_pnl: pnl, iv: 0.142,
  greeks: { delta: 12.3, gamma: 0.01, vega: 40, theta: -12 } });
export const POSITIONS: PositionsView = { status: "ok", read_at: "2026-10-13T04:00:00+00:00", ltp_source: "Groww live LTP", as_of: "2026-10-13",
  notes: ["Positions are read-only from Groww."],
  groups: [{ key: "NIFTY:2026-10-27", underlying: "NIFTY", expiry: "2026-10-27", days_to_expiry: 2, spot: 25000, chain_source: "NSE",
    legs: [pleg("NIFTY26OCT25000CE", 25000, "call", -65, 1446), pleg("NIFTY26OCT25300CE", 25300, "call", 65, -520)],
    unrealised_pnl: 926, net_greeks: { delta: -12.5, gamma: -0.01, vega: -40, theta: 22 }, max_loss: -18574, max_loss_note: null }] };
export const LONG_POSITIONS: PositionsView = { ...POSITIONS, groups: [{ ...POSITIONS.groups[0],
  legs: [pleg("BANKNIFTY26OCT125000CEWEEKLYADJUSTED", 125000, "call", -6500, -123456789), pleg("BANKNIFTY26OCT125300CEWEEKLYADJUSTED", 125300, "call", 6500, 98765432)],
  unrealised_pnl: -24691357, max_loss: -185740000, net_greeks: { delta: -123456.5, gamma: -0.01, vega: -4000000, theta: 2200000 } }] };

export const MARGIN = { status: "approximate" as const, label: "SPAN-like scan of a ±9% move", scan_risk: 18574, exposure: 4800, premium_paid: 0, total: 23374,
  worst_move_pct: 9, kind: "index" as const, assumptions: {}, note: "Exchange SPAN files are not read; the broker's number decides.",
  groww_calculator: { available: false, reason: "Groww is not connected" } };

// ------------------------------------------------------------------ /fno/paper (#308)
const leg = (id: number, side: "buy" | "sell", strike: number, entry: number) => ({
  id, right: "call" as const, strike, side, lots: 1, qty: 75, ref_price: entry, entry_price: entry,
  trading_symbol: `NIFTY26OCT${strike}CE`, last_price: null, last_price_at: null, exit_price: null,
});
const base: PaperTrade = {
  id: 1, symbol: "NIFTY", expiry: "2026-10-27", label: "e2e spread", status: "open", opened_at: "2026-10-12T04:30:00+00:00",
  days_to_expiry: 15, lot_size: 75, slippage_pct: 1, brokerage_per_order: 20, spot_at_entry: 24950,
  price_source: "Groww LTP", net_premium: 5377.5, entry_costs: 58.42, mtm_gross: null, mtm_at: null, mtm_source: null,
  mtm_net_if_closed: null, closed_at: null, close_reason: null, exit_costs: null, exit_detail: {}, settlement_price: null,
  settlement_source: null, gross_pnl: null, net_pnl: null, note: null, legs: [leg(1, "buy", 25000, 121.2), leg(2, "sell", 25200, 49.5)],
};
export const BOOK: PaperBook = {
  trades: [
    base,
    { ...base, id: 2, status: "awaiting_settlement", expiry: "2026-10-06", days_to_expiry: -6,
      note: "waiting for NSE's close of NIFTY on 06-Oct-2026 to settle (checked 07-Oct 09:00 IST)" },
    { ...base, id: 3, status: "closed", closed_at: "2026-10-09T09:00:00+00:00", close_reason: "square_off", exit_costs: 67.95,
      gross_pnl: -127.5, net_pnl: -253.87 },
  ],
  counts: { open: 1, awaiting_settlement: 1, closed: 1, all: 3 },
  results: { closed: 1, wins: 0, losses: 1, win_rate: 0, expectancy: -253.87, avg_win: null, avg_loss: -253.87, total_net: -253.87,
    max_drawdown: 253.87, curve: [{ closed_at: "2026-10-09T09:00:00+00:00", id: 3, cumulative: -253.87 }] },
  method: "Every paper trade is kept: none can be deleted.", rules: [], default_slippage_pct: 0.5,
};
export const LONG_BOOK: PaperBook = {
  ...BOOK,
  trades: BOOK.trades.map((t) => ({ ...t, symbol: "BANKNIFTY", label: LONG_NAME, entry_costs: 123456.78, mtm_gross: t.status === "open" ? -123456789 : null,
    mtm_net_if_closed: t.status === "open" ? -123580245.78 : null, mtm_at: t.status === "open" ? "2026-10-13T04:00:00+00:00" : null, mtm_source: t.status === "open" ? "Groww live LTP" : null,
    net_pnl: t.net_pnl == null ? null : -98765432.1, gross_pnl: t.gross_pnl == null ? null : -98641975.3,
    legs: t.legs.map((l) => ({ ...l, strike: l.strike + 100000, qty: 7500, ref_price: 12345.65, entry_price: 12469.11, last_price: 9876.5 })) })),
  rules: [{ id: 1, paper_id: 1, position_key: null, kind: "profit", threshold: 50, status: "armed", phrase: "net P&L reaches +50% of the ₹1,23,45,678 credit", last_value: null,
    note: "checked: −₹12,34,56,789 (−1,000.0% of the credit)", checked_at: "2026-10-13T04:00:00+00:00", fired_at: null, alert_id: null }],
  results: { closed: 1, wins: 0, losses: 1, win_rate: 0, expectancy: -98765432.1, avg_win: 123456789, avg_loss: -98765432.1, total_net: -98765432.1,
    max_drawdown: 98765432.1, curve: [] },
};
export const EMPTY_BOOK: PaperBook = { ...BOOK, trades: [], counts: { open: 0, awaiting_settlement: 0, closed: 0, all: 0 },
  results: { closed: 0, wins: 0, losses: 0, win_rate: null, expectancy: null, avg_win: null, avg_loss: null, total_net: 0, max_drawdown: 0, curve: [] } };

// ------------------------------------------------------------------ /fno/backtests (#307)
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
export const BT_BASE: StrategyResult = {
  key: "nifty_pcs_m", name: "NIFTY put credit spread, monthly", role: "candidate", kind: "put_credit", underlying: "NIFTY",
  spec: {}, spec_hash: "a".repeat(64), status: "candidate", reasons: ["passes every pre-registered criterion out of sample"],
  windows: { is: w({ from: "2022-01-01", to: "2024-06-30" }), oos: w({}), oos_stress: w({ expectancy: 300 }) }, criteria: crit(true),
  equity: [["2022-01-03", 1000000], ["2024-06-28", 1020000], ["2026-10-09", 1040000]], skips: { "IV percentile below the filter": 12 },
  by_year: { "2025": w({ trades: 20 }) }, by_iv_regime: { high: w({ trades: 15 }) },
};
export const BT_FAILS: StrategyResult = { ...BT_BASE, key: "nifty_dcs_m", name: "NIFTY debit call spread, monthly (control)", role: "control",
  status: "fails", reasons: ["fails: OOS deflated Sharpe"], criteria: crit(false), windows: { is: w({}), oos: w({ deflated_sharpe: null }) } };
export const REG = { version: "2026-10-12.1", hash: "6d92407630d7f406", registered_on: "2026-10-12", matches_spec: true, matches_code: true,
  hash_covers: "spec parameters only (specs.registration()): the engine code that runs them is not hashed until an engine hash exists (roadmap P2.4)", note: "Registered before any real-data run.",
  variants_tried: 8, specs: [], book: {}, criteria: {}, windows: { is: ["2022-01-01", "2024-06-30"] as [string, string], oos_from: "2024-07-01" },
  doc: "evals/experiments/fno_backtest/PREREG.md", deflated_sharpe_source: "Bailey & López de Prado (2014)" };
export const BACKTESTS: BacktestsResponse = {
  registration: REG,
  run: { id: 3, created_at: "2026-10-12T12:00:00+00:00", version: "2026-10-12.1", registration_hash: REG.hash, registered_hash: REG.hash,
    oos_evaluated: true, source: "finresearch_fnohist", variants_tried: 8, stock_rule_symbol: null,
    windows: { is: ["2022-01-01", "2024-06-30"], oos: ["2024-07-01", "2026-10-09"] }, generated_at: "2026-10-12T17:30:00+05:30",
    book: {}, criteria: {}, deflated_sharpe_source: "", strategies: [BT_BASE, BT_FAILS], note: "A candidate is evidence, not a guarantee.",
    candidates: [{ key: "nifty_pcs_m", name: BT_BASE.name, oos: BT_BASE.windows!.oos!, criteria: crit(true), setup: {
      status: "entry", as_of: "2026-10-09", symbol: "NIFTY", expiry: "2026-11-24", dte: 46, lots: 1, lot: 65,
      legs: [{ right: "put", strike: 24300, side: "sell", ref_price: 88.5, fill_assumed: 86.7 }, { right: "put", strike: 23800, side: "buy", ref_price: 41.2, fill_assumed: 42.0 }],
      premium: 2905, max_loss: 29595, margin_approx: null, entry_costs: 61.2, iv_percentile: 0.55, note: "End of day; prices have moved since." } }] },
};
const wl = (over: Partial<WindowStats>) => w({ expectancy: -98765432, total_net: -9876543210, worst_trade: -123456789, cvar_5: -123456789, costs: 98765432,
  avg_win: 123456789, avg_loss: -98765432, ci_low: -123456789, ci_high: 98765432, ...over });
const LONG_BT: StrategyResult = { ...BT_BASE, name: `${LONG_NAME} (library)`, key: "banknifty_ic_w_long",
  windows: { is: wl({ from: "2022-01-01", to: "2024-06-30" }), oos: wl({}), oos_stress: wl({}) },
  by_year: { "2024": wl({}), "2025": wl({}), "2026": wl({}) }, by_iv_regime: { low: wl({}), mid: wl({}), high: wl({}) },
  equity: [["2022-01-03", 1e9], ["2024-06-28", 9.2e8], ["2026-10-09", 1.04e9]] };
export const LONG_BACKTESTS: BacktestsResponse = { ...BACKTESTS, run: { ...BACKTESTS.run!, strategies: [LONG_BT, BT_FAILS],
  candidates: [{ ...BACKTESTS.run!.candidates[0], name: LONG_BT.name, key: LONG_BT.key,
    setup: { ...BACKTESTS.run!.candidates[0].setup!, premium: 123456789, max_loss: 987654321, entry_costs: 1234567 } }] } };

// ------------------------------------------------------------------ /fno/lab (#318)
const lw = (over: Partial<WindowStats>): WindowStats => w({ from: "2025-02-01", to: "2026-10-09", trades: 18, expiries: 12, wins: 13, losses: 5,
  win_rate: 0.72, expectancy: 640, avg_win: 2900, avg_loss: -5200, worst_trade: -16000, best_trade: 5000, cvar_5: -16000, sd: 4800,
  total_net: 11520, t_stat: 1.1, ci_low: -900, ci_high: 2100, costs: 4100, return_on_margin_annual: 0.05, days: 420, cagr: 0.01,
  max_drawdown_pct: 3.1, sharpe: 0.6, sortino: 0.8, daily_sr: 0.04, deflated_sharpe: 0.41, ...over });
export const DEFAULT_SPEC: LabSpec = {
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
export const LAB: LabResponse = {
  underlyings: ["NIFTY", "BANKNIFTY"], variants: { library: 8, lab: 2, n: 10 }, blocks: BLOCKS,
  strategies: [{ id: 1, name: "Bull put", underlying: "NIFTY", created_at: null, oos_versions: 2,
    split: { is: ["2022-01-01", "2024-06-30"], oos_from: "2024-07-01", data: ["2022-01-03", "2026-10-09"], rule: "the pre-registered library's windows" },
    versions: [{ id: 3, version: 2, created_at: null, spec: DEFAULT_SPEC, spec_hash: "b".repeat(64), note: "", counted_variant: true,
      describe: "NIFTY put credit spread (bull put); 30-45 DTE monthly; 0.2 delta, wings 2%", runs: [RUN_ROW], paper_rules: [] }] }],
};
export const LAB_VIEW: LabRunView = {
  ...RUN_ROW, strategy: { id: 1, name: "Bull put", version: 2, underlying: "NIFTY" },
  result: { key: "lab1v2", name: "Bull put (v2)", role: "lab", kind: "put_credit", underlying: "NIFTY", spec: {}, spec_hash: "b".repeat(64),
    status: "fails", reasons: ["fails: OOS deflated Sharpe"], oos: true, n_trials: 10, sr_trials_variance: 0.0004,
    warnings: ["This strategy has had 2 versions tested out of sample. Only the first look at the out-of-sample years was clean.",
      "10 variants have now been tried in this app (8 pre-registered + 2 lab)."],
    windows_dates: { is: ["2022-01-01", "2024-06-30"], oos: ["2024-07-01", "2026-10-09"] }, book: { capital: 1_000_000 },
    windows: { is: lw({ from: "2022-01-01", to: "2024-06-30", sharpe: 1.9 }), oos: lw({}), oos_stress: lw({ expectancy: -120 }) },
    criteria: [{ criterion: "OOS deflated Sharpe", value: 0.41, pass: false, rule: ">= 0.95 (N = 10 variants: 8 pre-registered + 2 from the lab)" }],
    equity: [["2022-01-03", 1000000], ["2026-10-09", 1011520]], by_year: { "2025": lw({}) }, by_iv_regime: { low: lw({}) } },
  library: { run_id: 3, oos_evaluated: true, variants_tried: 8, strategies: [
    { key: "nifty_pcs_m", name: "NIFTY put credit spread, monthly", underlying: "NIFTY", status: "fails", is: null,
      oos: { trades: 41, expectancy: 812, win_rate: 0.7, sharpe: 1.1, max_drawdown_pct: 4, deflated_sharpe: null, total_net: 30000 } }] },
  bar_now: { n_trials: 10, deflated_sharpe: 0.41, note: "Recomputed with today's variants count." },
};
export const LAB_PAPER: PaperRule[] = [{ id: 4, version_id: 3, version: 2, strategy_id: 1, strategy: "Bull put", underlying: "NIFTY", status: "active",
  created_at: "2026-10-10T14:00:00+00:00", start_after: "2026-10-10", last_day: "2026-10-12", note: "12-Oct-2026 close: signal",
  signals: [{ id: 1, day: "2026-10-12", status: "filled", expiry: "2026-11-24", lots: 2, lot: 65, legs: [], premium_at_signal: 3000,
    paper_id: 9, paper_status: "open", mtm_gross: null, net_pnl: null, exit_reason: null, note: null }],
  method: "Signals come from each stored close.", results: { closed: 0, wins: 0, total_net: 0, expectancy: null, max_drawdown: 0 } }];
export const LAB_TRADES: LabTrade[] = Array.from({ length: 6 }, (_, i) => ({ entry_day: "2025-03-03", exit_day: "2025-03-20", expiry: "2025-03-27", dte: 24,
  days_held: 17, lots: 2, lot: 65, premium: 3120.5, max_loss: -9870, entry_costs: 61, exit_costs: 58, exercise_stt: 0, gross: 1560, net: i % 2 ? -4321 : 1441,
  reason: i % 2 ? "stop_loss" : "profit_take", iv_pct: 0.55,
  legs: [{ right: "put", strike: 22100, side: "sell", entry: 88.5, exit: 40.1 }, { right: "put", strike: 21600, side: "buy", entry: 41.2, exit: 18.2 }] }));

const LONG_LAB_NAME = "Bull put with a 200-session trend filter, IV percentile above 40 and an exit before every results meeting";
export const LONG_LAB: LabResponse = { ...LAB, strategies: [{ ...LAB.strategies[0], name: LONG_LAB_NAME,
  versions: [{ ...LAB.strategies[0].versions[0], describe: `${LAB.strategies[0].versions[0].describe}; IV percentile 40-100; price above its 200-session average; exit before results; profit-take 50%, stop 2× credit, exit at 7 DTE`,
    runs: [RUN_ROW, { ...RUN_ROW, id: 8, oos: false }, { ...RUN_ROW, id: 9, status: "running" as const, summary: undefined }] }] }] };
export const LONG_LAB_VIEW: LabRunView = { ...LAB_VIEW, strategy: { ...LAB_VIEW.strategy, name: LONG_LAB_NAME },
  result: { ...LAB_VIEW.result!, name: `${LONG_LAB_NAME} (v2)`, windows: { is: wl({}), oos: wl({}), oos_stress: wl({}) }, by_year: { "2025": wl({}), "2026": wl({}) } },
  library: { ...LAB_VIEW.library!, strategies: [{ ...LAB_VIEW.library!.strategies[0], name: `${LONG_NAME} (library)`,
    oos: { trades: 41, expectancy: -98765432, win_rate: 0.7, sharpe: 1.1, max_drawdown_pct: 4, deflated_sharpe: null, total_net: -9876543210 } }] } };
export const LONG_LAB_PAPER: PaperRule[] = [{ ...LAB_PAPER[0], strategy: LONG_LAB_NAME, results: { ...LAB_PAPER[0].results, total_net: -98765432 },
  signals: [{ ...LAB_PAPER[0].signals[0], net_pnl: -98765432, exit_reason: "stop loss after a results-day gap", note: null }] }];

// ------------------------------------------------------------------ /fno direction panel (#327)
// moved from fno-direction.spec.ts so the layout spec can mount the panel too
const dev = (implied: number, logistic: number) => ({ implied, base_rate: implied - 0.01, logistic, shown: implied, shown_model: "implied", status: "no_edge",
  label: "market-implied, no edge", train_n: 900, text: "closes above today's close" });
const hz = (h: number) => ({ h, sigma_pct: 0.9 * Math.sqrt(h), threshold_pct: 0.45 * Math.sqrt(h),
  events: { up: dev(0.51, 0.53), up_k: dev(0.3, 0.29), down_k: dev(0.31, 0.33) } });
const stats = (brier: number) => ({ n: 566, base_rate: 0.48, mean_p: 0.5, brier, log_loss: 0.69, hit_rate: 0.48, auc: 0.52, calibration_intercept: 0, calibration_slope: 1.37,
  reliability: [0.4, 0.45, 0.5, 0.55, 0.6].map((p) => ({ n: 113, mean_p: p, observed: p - 0.03, ci: [p - 0.12, p + 0.06] })) });
const test_ = (bss: number, p: number) => ({ brier_skill: bss, log_loss_skill: bss, dm: -1, p, p_holm: 1, lag: 5 });
const cell = (h: number, event: string) => ({ symbol: "NIFTY", horizon: h, event, event_text: "", status: "no_edge", reasons: ["fails: Brier skill vs implied"],
  is: { n: 348, from: "2023-01-02", to: "2024-06-28", models: {}, tests: {} },
  oos: { n: 566, from: "2024-07-01", to: "2026-10-06", models: { base_rate: stats(0.253), implied: stats(0.2523), logistic: stats(0.2556) },
    tests: { logistic_vs_implied: test_(-0.0132, 0.86), logistic_vs_base_rate: test_(-0.0101, 0.87), base_rate_vs_implied: test_(-0.003, 0.58) } } });
const cand = (key: string, label: string, view: string, evm: number) => ({ key, label, view, status: "fails", why: ["the probabilities come from no accepted model (market-implied, no edge)"],
  ev_model: evm, ev_implied: evm, edge: 0, costs: 120, max_loss: -6500, legs: [{ right: "call", strike: 25000, side: "buy", price: 100, iv: 14 }] });
export const DIRECTION = (h: number) => ({
  symbol: "NIFTY", as_of: "2026-10-12", spot: 25000, iv_30: 13.9, horizons: [1, 5, 10, 20].map(hz), accepted: [], model_status: "no_edge",
  headline: { h: 5, p_up: 0.51, label: "market-implied, no edge", call_or_put: null },
  run: { id: 1, created_at: "2026-10-19T20:00:00+05:30", verdict: "no edge: no logistic cell passed the pre-registered out-of-sample criteria",
    source: "finresearch_fnohist2 (read only)", registration_hash: "372dabfe1d07", windows: { oos_start: "2024-07-01" },
    criteria: { min_oos_n: 250, alpha: 0.05, slope_low: 0.5, slope_high: 3 }, cells: [1, 5, 10, 20].flatMap((x) => ["up", "up_k", "down_k"].map((e) => cell(x, e))) },
  live_accuracy: [{ method: "m", event_kind: `fno_dir_up_h${h}`, h, event: "up", model: "implied", logged: 12, open: 4, resolved: 8, independent: 2, void: 0,
    summary: { n: 2, brier: 0.24, brier_skill: null, hit_rate: 0.5, hit_rate_ci: [0.0945, 0.9055], base_rate: 0.5 } }],
  decision_horizon: h, evidence_note: "Probabilities are for the index's close h sessions after the stored close.",
  registration: { hash: "372dabfe1d070d22", registered_on: "2026-10-19", matches_spec: true, matches_code: true, doc: "PREREG.md",
    hash_covers: "spec parameters only (specs.registration()): the engine code that runs them is not hashed until an engine hash exists (roadmap P2.4)" },
  decision: { status: "ok", action: "NO TRADE", label: "NO TRADE: no measured edge", chosen: null, horizon: h, expiry: "2026-10-20", horizon_day: "2026-10-19",
    held_to_expiry: false, lot_size: 65, plan: null, notes: [],
    candidates: [cand("long_call", "Long call (ATM)", "up", -310), cand("long_put", "Long put (ATM)", "down", -295)],
    non_directional: { text: "No measured directional edge. Non-directional plans: the pre-registered library.", suggest_api: "/api/fno/NIFTY/suggest", href: "/fno?symbol=NIFTY", evidence_href: "/fno/backtests" } },
});

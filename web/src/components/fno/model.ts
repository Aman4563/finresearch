// F&O types and pure helpers (payoff at expiry, presets). Server results stay authoritative; these power the live chart.

export type Quote = { last_price: string | null; iv: string | null; oi: string | null; change_in_oi: string | null; volume: string | null; bid?: string | null; ask?: string | null };
export type Row = { strike: string; call: Quote | null; put: Quote | null };
export type Chain = {
  symbol: string;
  expiry: string;
  underlying: string;
  as_of: string | null;
  lot_size: number | null;
  atm_strike: string | null;
  atm_iv: { call: string | null; put: string | null };
  pcr_oi: string | null;
  max_pain: string | null;
  rows: Row[];
};
export type Leg = { right: "call" | "put" | "future"; strike: string; side: "buy" | "sell"; lots: number };
export type ChargeRate = { key: string; rate_pct: number; basis: string; effective_from: string; source: string; status: "verified" | "secondary" | "unconfirmed"; note: string };
export type Costs = { lines: Record<string, number>; total: number; turnover: number; orders: number; rates: ChargeRate[] };
export type Outcome = { vol: number; drift: number; pop: number; ev: number; ev_gross: number; quantiles: Record<string, number>; window?: string } | null;
export type RateInfo = { rate: number; par_yield: number | null; tenor_years: number | null; as_of: string | null; fallback: boolean; source: string; note: string };
export type RiskNotice = { headline: string; text: string; source: string; url: string; date: string };
export type Analysis = {
  spot: number;
  days_to_expiry: number;
  rate: number;
  drift: number;
  max_loss: number | null;
  net_vega: number | null;
  vols: { atm_iv: number | null; atm_strike: number | null; realised_20d: number | null; realised_60d: number | null; closes: number };
  costs: { entry: Costs; square_off_at_same_prices: Costs; expected_exercise_stt: number | null; total_to_expiry: number | null; brokerage_per_order: number; note: string };
  risk_neutral: Outcome;
  real_world: Outcome;
  real_world_20d: Outcome;
  real_world_60d: Outcome;
  fair_price_note: string;
  expected_move: { move: number; low: number; high: number; vol: number } | null;
  capital_check: { capital: number; max_loss: number | null; pct: number | null; limit_pct: number; within: boolean; message: string };
  drawdown: { probability: number; threshold_pct: number; repeats: number } | null;
  iv_context: { n: number; current: number | null; rank: number | null; percentile: number | null; status: string; skew_25d: number | null; iv_rv_ratio: number | null };
  notes: string[];
};
export type IvHistory = {
  symbol: string; n: number; min_days: number; status: string; current: number | null; rank: number | null; percentile: number | null;
  low: number | null; high: number | null; skew_25d: number | null; last_day: string | null; series: { day: string; atm_iv: number }[]; method: string;
};
export type Result = {
  analysis?: Analysis;
  risk_notice?: RiskNotice;
  breakevens: number[];
  max_profit: number | null;
  max_loss: number | null;
  net_premium: number;
  /** risk-neutral model probability after costs (lognormal), not a forecast (#244) */
  model_probability_of_profit: number | null;
  /** @deprecated use model_probability_of_profit; kept by the API for one release */
  probability_of_profit?: number | null;
  /** where the risk-free rate came from: FBIL's par yield at the expiry, its dated fallback curve, or the fixed 6.5 % */
  rate?: RateInfo;
  net_greeks: Record<string, number>;
  curve: [number, number][];
  lot_size: number;
  notes: string[];
  disclaimer: string;
};

export const n = (x: string | null | undefined) => (x == null || x === "" ? null : Number(x));

export function premiumOf(chain: Chain, leg: Leg) {
  if (leg.right === "future") return 0;
  const row = chain.rows.find((r) => Number(r.strike) === Number(leg.strike));
  const q = leg.right === "call" ? row?.call : row?.put;
  return n(q?.last_price) ?? null;
}

/** P&L at expiry for an underlying price `s`, in rupees. */
export function pnlAt(chain: Chain, legs: Leg[], lot: number, s: number) {
  let total = 0;
  for (const l of legs) {
    const qty = l.lots * lot * (l.side === "buy" ? 1 : -1);
    const k = Number(l.strike);
    if (l.right === "future") total += qty * (s - k);
    else {
      const prem = premiumOf(chain, l) ?? 0;
      const intrinsic = l.right === "call" ? Math.max(0, s - k) : Math.max(0, k - s);
      total += qty * (intrinsic - prem);
    }
  }
  return total;
}

export function payoffCurve(chain: Chain, legs: Leg[], lot: number) {
  const spot = Number(chain.underlying);
  const strikes = legs.map((l) => Number(l.strike));
  const lo = Math.min(spot * 0.9, ...strikes.map((k) => k * 0.97));
  const hi = Math.max(spot * 1.1, ...strikes.map((k) => k * 1.03));
  const xs = new Set<number>();
  for (let i = 0; i <= 160; i++) xs.add(Math.round(lo + ((hi - lo) * i) / 160));
  strikes.forEach((k) => xs.add(k));
  const points = [...xs].sort((a, b) => a - b).map((s) => ({ spot: s, pnl: Math.round(pnlAt(chain, legs, lot, s)) }));
  const breakevens: number[] = [];
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1], b = points[i];
    if ((a.pnl < 0 && b.pnl >= 0) || (a.pnl >= 0 && b.pnl < 0)) {
      breakevens.push(Math.round(a.spot + ((0 - a.pnl) * (b.spot - a.spot)) / (b.pnl - a.pnl || 1)));
    }
  }
  return { points, breakevens, lo, hi };
}

export type PresetKey = "long_call" | "long_put" | "bull_call" | "bear_put" | "straddle" | "strangle" | "iron_condor";

export const PRESETS: { key: PresetKey; label: string; view: string; help: string }[] = [
  { key: "long_call", label: "Long call", view: "bullish", help: "Buy an at-the-money call. Profits if the price rises above strike + premium; the most you can lose is the premium." },
  { key: "long_put", label: "Long put", view: "bearish", help: "Buy an at-the-money put. Profits if the price falls below strike − premium; the most you can lose is the premium." },
  { key: "bull_call", label: "Bull call spread", view: "mildly bullish", help: "Buy a call near the money and sell a higher one. Cheaper than a plain call, but profit is capped at the higher strike." },
  { key: "bear_put", label: "Bear put spread", view: "mildly bearish", help: "Buy a put near the money and sell a lower one. Cheaper than a plain put, profit capped at the lower strike." },
  { key: "straddle", label: "Long straddle", view: "big move", help: "Buy a call and a put at the same strike. Profits from a large move either way; loses if the price stays put." },
  { key: "strangle", label: "Short strangle", view: "range-bound", help: "Sell an out-of-the-money call and put. Collects premium if the price stays in a range, but losses are unlimited on a big move." },
  { key: "iron_condor", label: "Iron condor", view: "range-bound", help: "A short strangle with protective wings bought further out. Keeps the range-bound profit with a capped loss." },
];

/** Legs for a preset around the ATM strike, using only strikes that have a traded price. */
export function presetLegs(chain: Chain, key: PresetKey): Leg[] | null {
  const spot = Number(chain.underlying);
  const rows = [...chain.rows].sort((a, b) => Number(a.strike) - Number(b.strike));
  if (!rows.length) return null;
  const atm = chain.atm_strike ? Number(chain.atm_strike) : spot;
  const iAtm = rows.reduce((best, r, i) => (Math.abs(Number(r.strike) - atm) < Math.abs(Number(rows[best].strike) - atm) ? i : best), 0);
  const step = rows.length > 1 ? Math.abs(Number(rows[Math.min(iAtm + 1, rows.length - 1)].strike) - Number(rows[Math.max(iAtm - 1, 0)].strike)) / 2 || 1 : 1;
  const w = Math.max(1, Math.round((spot * 0.01) / step)); // about 1% of spot per wing
  const pick = (offset: number, right: "call" | "put") => {
    for (let d = 0; d < rows.length; d++) {
      for (const sgn of offset >= 0 ? [1, -1] : [-1, 1]) {
        const r = rows[iAtm + offset + sgn * d];
        const q = r && (right === "call" ? r.call : r.put);
        if (q?.last_price && Number(q.last_price) > 0) return r.strike;
      }
    }
    return null;
  };
  const leg = (right: "call" | "put", offset: number, side: "buy" | "sell"): Leg | null => {
    const strike = pick(offset, right);
    return strike ? { right, strike, side, lots: 1 } : null;
  };
  const sets: Record<PresetKey, (Leg | null)[]> = {
    long_call: [leg("call", 0, "buy")],
    long_put: [leg("put", 0, "buy")],
    bull_call: [leg("call", 0, "buy"), leg("call", w, "sell")],
    bear_put: [leg("put", 0, "buy"), leg("put", -w, "sell")],
    straddle: [leg("call", 0, "buy"), leg("put", 0, "buy")],
    strangle: [leg("call", w, "sell"), leg("put", -w, "sell")],
    iron_condor: [leg("call", w, "sell"), leg("call", 2 * w, "buy"), leg("put", -w, "sell"), leg("put", -2 * w, "buy")],
  };
  const out = sets[key];
  return out.every(Boolean) ? (out as Leg[]) : null;
}

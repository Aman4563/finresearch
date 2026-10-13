// Black-Scholes (Black & Scholes 1973; Hull, "The Black-Scholes-Merton model") for the lessons' illustrative
// payoffs: no dividends, a fixed 6.5 % rate, one flat IV. Prices are teaching numbers, never market quotes.
import type { Leg, Sketch } from "./types";

export const RATE = 0.065;

function erf(x: number) {
  // Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7)
  const s = Math.sign(x), a = Math.abs(x), t = 1 / (1 + 0.3275911 * a);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-a * a);
  return s * y;
}
const N = (x: number) => 0.5 * (1 + erf(x / Math.SQRT2));

/** Option price per unit; at or after expiry (t <= 0) the intrinsic value. */
export function price(right: "call" | "put", s: number, k: number, t: number, iv: number, r = RATE) {
  if (t <= 0) return right === "call" ? Math.max(0, s - k) : Math.max(0, k - s);
  const d1 = (Math.log(s / k) + (r + (iv * iv) / 2) * t) / (iv * Math.sqrt(t));
  const d2 = d1 - iv * Math.sqrt(t);
  return right === "call" ? s * N(d1) - k * Math.exp(-r * t) * N(d2) : k * Math.exp(-r * t) * N(-d2) - s * N(-d1);
}

/** Value of one leg per unit when the underlying is `s` and `elapsed` days have passed since entry. */
function legValue(leg: Leg, sk: Sketch, s: number, elapsed: number) {
  if (leg.right === "future" || leg.right === "stock") return s;
  const days = (leg.days ?? sk.days) - elapsed;
  return price(leg.right, s, leg.strike!, days / 365, sk.iv);
}

/** P&L (₹) on the first expiry day for underlying prices around the spot; legs expiring later keep time value. */
export function sketchCurve(sk: Sketch) {
  const strikes = sk.legs.filter((l) => l.strike != null).map((l) => l.strike!);
  const lo = Math.round(Math.min(sk.spot * 0.9, ...strikes.map((k) => k * 0.96)));
  const hi = Math.round(Math.max(sk.spot * 1.1, ...strikes.map((k) => k * 1.04)));
  const entry = sk.legs.map((l) => legValue(l, sk, sk.spot, 0));
  const pnl = (s: number) =>
    sk.legs.reduce((sum, l, i) => {
      const qty = (l.lots ?? 1) * sk.lot * (l.side === "buy" ? 1 : -1);
      return sum + qty * (legValue(l, sk, s, sk.days) - entry[i]);
    }, 0);
  const xs = new Set<number>();
  for (let i = 0; i <= 120; i++) xs.add(Math.round(lo + ((hi - lo) * i) / 120));
  strikes.forEach((k) => xs.add(k));
  const points = [...xs].sort((a, b) => a - b).map((s) => ({ spot: s, pnl: Math.round(pnl(s)) }));
  const breakevens: number[] = [];
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1], b = points[i];
    if ((a.pnl < 0 && b.pnl >= 0) || (a.pnl >= 0 && b.pnl < 0)) {
      breakevens.push(Math.round(a.spot + ((0 - a.pnl) * (b.spot - a.spot)) / (b.pnl - a.pnl || 1)));
    }
  }
  const pnls = points.map((p) => p.pnl);
  const premiums = sk.legs.map((l, i) => ({ leg: l, premium: l.right === "call" || l.right === "put" ? entry[i] : null }));
  return { points, breakevens, max: Math.max(...pnls), min: Math.min(...pnls), premiums };
}

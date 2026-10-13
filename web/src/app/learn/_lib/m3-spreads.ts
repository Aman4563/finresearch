// Module 3, part 2: vertical spreads, credit spreads and condors (NIFTY 24,000, lot 65, IV 14 %, 30 days).
import type { Section, Sketch } from "./types";

const N = (legs: Sketch["legs"]): Sketch => ({ spot: 24000, days: 30, iv: 0.14, lot: 65, legs });

export const M3_SPREADS: Section[] = [
  {
    id: "bull-call-spread",
    title: "Bull call spread (debit)",
    body: "Buy a call and sell a higher-strike call on the same expiry. The short call pays for part of the long one, so the trade costs less and decays more slowly than a plain call, but profit is capped at the upper strike [[hull]] [[natenberg]].",
    sketch: N([{ right: "call", side: "buy", strike: 24000 }, { right: "call", side: "sell", strike: 24500 }]),
    card: { view: "Moderately bullish", when: "A target price you can name, by a date you can name.", maxProfit: "Strike width − net debit (× lot).", maxLoss: "The net debit.", greeks: "Delta +, gamma and theta small and changing sign with the price, vega small.", manage: "Often closed at 50–75 % of maximum value; holding to expiry captures the last part slowly.", backtest: { key: "nifty_dcs_m", label: "NIFTY debit call spread, monthly (control)" }, builder: "Bull call spread" },
  },
  {
    id: "bear-put-spread",
    title: "Bear put spread (debit)",
    body: "Buy a put and sell a lower-strike put. The bearish mirror of the bull call spread; skew often makes the sold lower put relatively rich, which helps the buyer [[hull]] [[natenberg]].",
    sketch: N([{ right: "put", side: "buy", strike: 24000 }, { right: "put", side: "sell", strike: 23500 }]),
    card: { view: "Moderately bearish", when: "Expecting a decline to around the lower strike.", maxProfit: "Strike width − net debit.", maxLoss: "The net debit.", greeks: "Delta −, small gamma, theta and vega.", manage: "As the bull call spread.", backtest: { key: "nifty_dps_m", label: "NIFTY debit put spread, monthly (control)" }, builder: "Bear put spread" },
  },
  {
    id: "put-credit-spread",
    title: "Bull put spread (credit)",
    body: "Sell an out-of-the-money put and buy a further one. You receive a credit and keep it if the index stays above the short strike. Same shape as the bull call spread (parity), but you are paid up front and carry the loss if it falls [[hull]]. Most retail-friendly way to sell premium with a defined maximum loss.",
    sketch: N([{ right: "put", side: "sell", strike: 23500 }, { right: "put", side: "buy", strike: 23000 }]),
    card: { view: "Neutral to bullish", when: "IV relatively high (IV percentile), no big event inside the expiry, and you accept that one bad month can erase several good ones.", maxProfit: "The net credit.", maxLoss: "Strike width − credit. Usually several times the credit.", greeks: "Delta +, gamma −, theta +, vega −.", manage: "Common rules: take profit at about 50 % of the credit, stop at a loss of about 2× the credit, close a week or so before expiry to avoid gamma risk.", backtest: { key: "nifty_pcs_m", label: "NIFTY put credit spread, monthly" } },
  },
  {
    id: "call-credit-spread",
    title: "Bear call spread (credit)",
    body: "Sell an out-of-the-money call and buy a further one: paid to be right that the index stays below the short strike. Index call skew is usually flatter than put skew, so credits are smaller for the same distance [[hull]] [[sinclair]].",
    sketch: N([{ right: "call", side: "sell", strike: 24500 }, { right: "call", side: "buy", strike: 25000 }]),
    card: { view: "Neutral to bearish", when: "After a sharp rally with IV elevated, expecting a pause.", maxProfit: "The net credit.", maxLoss: "Strike width − credit.", greeks: "Delta −, gamma −, theta +, vega −.", manage: "As the bull put spread.", backtest: { key: "nifty_ccs_m", label: "NIFTY call credit spread, monthly" } },
  },
  {
    id: "iron-condor",
    title: "Iron condor",
    body: "A bull put spread plus a bear call spread on the same expiry: paid for the index staying in a range. Maximum loss is defined by the wings. The app pre-registered both a monthly BANKNIFTY and a weekly NIFTY version; see both on the backtest page [[app-prereg]].",
    sketch: N([{ right: "put", side: "buy", strike: 23000 }, { right: "put", side: "sell", strike: 23500 }, { right: "call", side: "sell", strike: 24500 }, { right: "call", side: "buy", strike: 25000 }]),
    card: { view: "Range-bound", when: "IV high relative to expected movement, no event inside the expiry.", maxProfit: "Total net credit.", maxLoss: "Wider wing width − total credit (only one side can lose at expiry).", greeks: "Delta ≈ 0 at entry, gamma −, theta +, vega −.", manage: "Take profit around 50 % of the credit; close or roll the tested side early; do not hold the short strikes into the last day.", backtest: { key: "banknifty_ic_m", label: "BANKNIFTY iron condor, monthly (and nifty_ic_w, weekly)" }, builder: "Iron condor" },
  },
  {
    id: "iron-butterfly",
    title: "Iron butterfly",
    body: "Sell an at-the-money straddle and buy wings on both sides. A larger credit than a condor, but the profit zone is a narrow peak around the short strike and the full credit is kept only if expiry lands near it [[natenberg]].",
    sketch: N([{ right: "put", side: "buy", strike: 23500 }, { right: "put", side: "sell", strike: 24000 }, { right: "call", side: "sell", strike: 24000 }, { right: "call", side: "buy", strike: 24500 }]),
    card: { view: "Pinned near the current price", when: "Very low expected movement and high IV, a short time frame.", maxProfit: "Total net credit (only at the short strike).", maxLoss: "Wing width − credit.", greeks: "Delta ≈ 0, strongly negative gamma near expiry, theta +, vega −.", manage: "Usually closed at 20–30 % of the credit because the peak is rarely hit exactly.", backtest: null },
  },
];

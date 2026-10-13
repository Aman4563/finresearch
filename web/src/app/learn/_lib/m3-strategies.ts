// Module 3: strategy catalogue. Sketches are Black-Scholes illustrations (NIFTY 24,000, lot 65, IV 14 %, 30 days;
// stock overlays on an illustrative ₹1,500 stock, lot 500, IV 25 %). Spreads: m3-spreads.ts; volatility: m3-vol.ts.
import { M3_SPREADS } from "./m3-spreads";
import { M3_QUIZ, M3_VOL } from "./m3-vol";
import type { Module, Sketch } from "./types";

const N = (legs: Sketch["legs"], unlimited?: Sketch["unlimited"], days = 30): Sketch => ({ spot: 24000, days, iv: 0.14, lot: 65, legs, unlimited });
const STOCK = (legs: Sketch["legs"], unlimited?: Sketch["unlimited"]): Sketch => ({ spot: 1500, days: 30, iv: 0.25, lot: 500, legs, unlimited });

export const M3: Module = {
  slug: "strategies",
  n: 3,
  title: "Strategy catalogue",
  summary: "Each common strategy with when it fits, an illustrative payoff, maximum profit and loss, its greeks, how it is usually managed, and whether the app has backtested it.",
  sections: [
    {
      id: "read-me",
      title: "How to read these cards",
      body: `Every chart is the P&L on expiry day for **one lot**, using Black-Scholes premiums at a single IV: an illustration of the shape, not a price you can trade [[black-scholes]]. Real chains have skew, wider spreads and costs (module 1), which move the breakevens against you. For live prices use the [strategy builder](/fno), which draws the same chart from the current chain.

"Management" lists common practitioner rules (for example, take profit at 50 % of a credit, exit around 7 days to expiry). They are conventions, not proven optima; the app's own backtest pre-registered such rules before testing them and reports honestly whether they worked after costs [[app-prereg]]. Strategy mechanics follow Hull, Natenberg and Sinclair [[hull]] [[natenberg]] [[sinclair]].`,
    },
    {
      id: "long-call",
      title: "Long call",
      body: "Buy a call. The simplest bullish trade with a fixed maximum loss, and the one most retail traders lose money on: the market must rise past strike + premium before expiry [[hull]] [[sebi-pr-50-2026]].",
      sketch: N([{ right: "call", side: "buy", strike: 24000 }], "profit"),
      card: { view: "Bullish, expecting a large move soon", when: "Expecting a move bigger than IV implies, with IV low relative to its own history.", maxProfit: "Unlimited (the underlying minus strike minus premium).", maxLoss: "The premium paid.", greeks: "Delta +, gamma +, theta − (decay works against you), vega +.", manage: "Decide the exit before entry: a premium stop (e.g. −50 %), a target, and a time stop well before the last week when decay is fastest.", backtest: null, builder: "Long call" },
    },
    {
      id: "long-put",
      title: "Long put",
      body: "Buy a put: a bearish bet, or insurance on a portfolio. Index puts are usually expensive because of skew and the crash premium [[hull]] [[coval-shumway]].",
      sketch: N([{ right: "put", side: "buy", strike: 24000 }]),
      card: { view: "Bearish, or hedging", when: "Expecting a sharp fall; or protecting holdings through a specific risk window.", maxProfit: "Strike − premium (the underlying cannot fall below zero).", maxLoss: "The premium paid.", greeks: "Delta −, gamma +, theta −, vega +.", manage: "As the long call. As a hedge, size it to the exposure you want protected and accept the cost as insurance.", backtest: null, builder: "Long put" },
    },
    {
      id: "covered-call",
      title: "Covered call",
      body: "Own the shares and sell an out-of-the-money call against them. You keep the premium and give up gains above the strike. By put-call parity it has the same payoff as selling a put at that strike [[hull]]. Stock calls settle physically, so an in-the-money call at expiry delivers your shares [[sebi-2018-161]].",
      sketch: STOCK([{ right: "stock", side: "buy" }, { right: "call", side: "sell", strike: 1600 }]),
      card: { view: "Neutral to mildly bullish on a stock you already hold", when: "You would be content to sell at the strike and want income from the holding.", maxProfit: "Strike − purchase price + premium.", maxLoss: "Purchase price − premium (the stock can still fall a long way).", greeks: "Delta + (less than the stock alone), gamma −, theta +, vega −.", manage: "Close or roll the call when most of the premium is earned (e.g. 50 %), and before results or other events if you do not want to be called away.", backtest: { key: "stock_cc_m", label: "covered call on a held stock (overlay)" } },
    },
    {
      id: "protective-put",
      title: "Protective put",
      body: "Own the shares and buy a put below the price: insurance with a known cost and a floor under the loss [[hull]].",
      sketch: STOCK([{ right: "stock", side: "buy" }, { right: "put", side: "buy", strike: 1400 }], "profit"),
      card: { view: "Bullish long-term, worried short-term", when: "Holding through a known risk (results, a court ruling) that you cannot or will not sell before.", maxProfit: "Unlimited, less the premium.", maxLoss: "Purchase price − strike + premium.", greeks: "Delta + (reduced), gamma +, theta −, vega +.", manage: "Insurance costs money every month; buying it continuously is a steady drag. Buy it for defined risk windows.", backtest: { key: "stock_pp_m", label: "protective put on a held stock (overlay)" } },
    },
    {
      id: "collar",
      title: "Collar",
      body: "Own the shares, buy a put below and sell a call above. The call premium pays for some or all of the put; you trade away upside for a floor [[hull]].",
      sketch: STOCK([{ right: "stock", side: "buy" }, { right: "put", side: "buy", strike: 1400 }, { right: "call", side: "sell", strike: 1600 }]),
      card: { view: "Protect gains on a holding at little net cost", when: "A large unrealised gain you want to protect for a period without selling.", maxProfit: "Call strike − purchase price ± net premium.", maxLoss: "Purchase price − put strike ± net premium.", greeks: "Delta + (small), gamma and vega depend on the strikes, theta near zero.", manage: "Roll both legs together; beware physical delivery of either leg at expiry.", backtest: null },
    },
    ...M3_SPREADS,
    ...M3_VOL,
  ],
  quiz: M3_QUIZ,
};

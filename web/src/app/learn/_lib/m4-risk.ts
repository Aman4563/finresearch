// Module 4: risk management. Worked numbers computed in Python: ruin = ((1 − p)/p)^units; the gap example is a short
// 23,500 put (BS premium 145.82 at 30 days, IV 14 %) with NIFTY gapping 10 % to 21,600, × 65.
import { M4_MORE, M4_QUIZ } from "./m4-more";
import type { Module } from "./types";

export const M4: Module = {
  slug: "risk",
  n: 4,
  title: "Risk management like professionals",
  summary: "Size every trade by what it can lose, understand risk of ruin and correlation, prefer defined risk, choose stop types deliberately, know when not to adjust, and respect expiry day, events, liquidity and margin calls.",
  sections: [
    {
      id: "sizing",
      title: "Position sizing: fixed fractional",
      body: `Professionals decide **how much they can lose** before they decide what to trade. The usual retail rule is fixed-fractional sizing: risk at most **1–2 % of trading capital** on any one trade, where "risk" is the maximum loss (defined-risk trades) or a stress loss (anything else). It is a convention, not a law; its point is that a run of ordinary losses cannot end your trading. The app's profile uses 2 % by default and flags any strategy whose maximum loss is above it.

Worked, with ₹5,00,000 set aside for F&O:

| Trade | Max loss per lot | 1 % budget (₹5,000) | 2 % budget (₹10,000) |
|---|---|---|---|
| NIFTY 24,000/24,200 bull call spread, debit ≈ ₹100 | ≈ ₹6,514 | 0 lots | 1 lot |
| NIFTY 23,500/23,000 put credit spread, credit ≈ ₹92 | ≈ ₹26,497 | 0 lots | 0 lots |
| Naked short 23,500 put | undefined | size by stress | size by stress |

Two lessons: with a small account, even one NIFTY lot can exceed the budget (the 500-point credit spread needs about ₹13 lakh of capital at 2 %), and the honest answer is "smaller structures or no trade", not "bend the rule". And a naked position has no maximum loss to size by: if NIFTY gaps 10 % to 21,600, that one short put loses about **₹1,14,000**, 23 % of this account, in a single morning.

Kelly-style formulas give the growth-optimal fraction only when you know your edge exactly; with an estimated edge, a fraction of Kelly (a quarter or less) is the usual ceiling, never a target [[sinclair]].`,
    },
    {
      id: "ruin",
      title: "Risk of ruin",
      body: `For repeated even-money bets won with probability p, staking one unit from a bankroll of C units, the chance of eventually losing everything is ((1 − p) ÷ p)^C (the gambler's ruin) [[feller]]:

| Win rate p | Bankroll in bets | Risk of ruin |
|---|---|---|
| 55 % | 20 (5 % a bet) | 1.8 % |
| 55 % | 10 (10 % a bet) | 13.4 % |
| 52 % | 20 | 20.2 % |
| 52 % | 50 (2 % a bet) | 1.8 % |
| 50 % or less | any | 100 % |

A real edge is small and uncertain, so bet size, not the edge, decides survival. Option selling is worse than even-money: many small wins and rare large losses, so the formula understates the danger. The app instead simulates repeating your strategy and reports the chance of a given drawdown; read it as a model, not a forecast.`,
    },
    {
      id: "correlation",
      title: "Correlation: one bet in five disguises",
      body: `NIFTY, BANKNIFTY and FINNIFTY positions are largely the same bet on Indian large caps; five credit spreads on five stocks are mostly one bet on the market. Correlations between equity markets rise in large down moves, exactly when diversification is needed [[longin-solnik]]. Add up the **combined** worst case of all open positions in a 5–10 % market gap, not each trade alone, and keep that within what you can lose.`,
    },
    {
      id: "defined-risk",
      title: "Defined risk first",
      body: `A defined-risk structure (a debit spread, a credit spread with a bought wing, an iron condor) has a maximum loss you know before you enter. That makes sizing possible, removes the gap-to-ruin scenario, and lowers margin. The cost is a little premium paid for the wings. For an individual account this is almost always worth it [[taleb]] [[sinclair]]. The app's default profile allows only defined-risk strategies until you change it.`,
    },
    ...M4_MORE,
  ],
  quiz: M4_QUIZ,
};

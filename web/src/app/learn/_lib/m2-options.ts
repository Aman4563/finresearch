// Module 2: options mechanics. Worked numbers: Black-Scholes, S = K = 24,000, IV 14 %, r 6.5 %, no dividends,
// computed in Python and re-checked by the page's own bs.ts. Volatility sections: m2-vol.ts.
import { M2_QUIZ, M2_VOL } from "./m2-vol";
import type { Module } from "./types";

export const M2: Module = {
  slug: "options",
  n: 2,
  title: "Options mechanics",
  summary: "Payoffs, intrinsic and time value, put-call parity, what Black-Scholes really says, the greeks with worked numbers, implied vs realised volatility, IV rank, skew, the volatility risk premium, and why option buyers usually lose.",
  sections: [
    {
      id: "payoff",
      title: "Payoff at expiry",
      body: `At expiry an option is worth only its **intrinsic value** [[hull]]:

- call: max(0, S − K); put: max(0, K − S), where S is the settlement price and K the strike.
- A long call's P&L is max(0, S − K) − premium; it breaks even at **K + premium**. A long put breaks even at **K − premium**.
- The seller's P&L is the exact mirror image: what the buyer makes, the seller loses, before costs.

Multiply by the lot size for rupees. The chart: a 30-day 24,000 call bought at about ₹451 (₹29,287 a lot) breaks even near 24,451 on expiry; at 25,000 it makes (1,000 − 451) × 65 ≈ ₹35,700; anywhere at or below 24,000 it loses the whole ₹29,287.`,
      sketch: { spot: 24000, days: 30, iv: 0.14, lot: 65, legs: [{ right: "call", side: "buy", strike: 24000 }], unlimited: "profit" },
    },
    {
      id: "moneyness",
      title: "Intrinsic value, time value and moneyness",
      body: `Before expiry, **premium = intrinsic value + time value** [[hull]] [[natenberg]]. Time value is what you pay for the chance that the option ends further in the money; it shrinks to zero at expiry.

| Moneyness | Call | Put | Premium is… |
|---|---|---|---|
| In the money (ITM) | S > K | S < K | intrinsic + some time value |
| At the money (ATM) | S ≈ K | S ≈ K | all time value, and the most of it |
| Out of the money (OTM) | S < K | S > K | all time value |

With NIFTY at 24,000, 30 days to expiry and 14 % IV, the 24,000 call is worth about ₹451 and the 24,500 call about ₹230: both are **pure time value**. If nothing moves, both decay to zero.`,
    },
    {
      id: "parity",
      title: "Put-call parity",
      body: `For European options on the same strike and expiry, with no dividends [[hull]]:

> **C − P = S − K·e^(−rT)**

Same example: C = 450.57, P = 322.70, so C − P = 127.87; and 24,000 − 24,000·e^(−0.065 × 30/365) = 127.88. With an index future F the cleaner form is C − P = (F − K)·e^(−rT) [[hull]].

Why it matters: a call is a put plus a long future (and vice versa). A "covered call" has the same payoff as a short put at that strike; a long straddle is a long call plus a long put. If a chain seems to violate parity by more than the costs and spread, the prices are stale or illiquid, not free money.`,
    },
    {
      id: "black-scholes",
      title: "Black-Scholes, intuitively",
      body: `Black and Scholes priced an option by building a riskless hedge from the option and the underlying [[black-scholes]]. The price depends on five inputs: spot, strike, time, interest rate and **volatility**; it does not depend on anyone's view of where the market is going [[hull]].

Intuitions worth keeping:
- The price is the discounted average payoff over a lognormal spread of outcomes whose width is σ√T. Double the time, and the spread grows by √2, not 2.
- **N(d₂)** is the model's (risk-neutral) probability that a call finishes in the money; delta is close to it but not the same thing [[hull]].
- Every input but volatility can be observed, so traders quote options **in volatility**: the IV that makes the model price equal the market price [[natenberg]].
- The model assumes constant volatility and no jumps. Real markets jump and volatility changes, which is why IV differs across strikes (skew) and expiries (term structure) [[hull]] [[sinclair]].

A one-standard-deviation move over T is roughly S·σ·√T: for 24,000 at 14 % over 30 days, about **±963 points**. Around two in three outcomes land inside that band in the model, and real markets have fatter tails [[natenberg]] [[taleb]].`,
    },
    {
      id: "greeks",
      title: "The greeks, worked",
      body: `Greeks are sensitivities of the option price [[hull]] [[natenberg]]. Black-Scholes values for NIFTY 24,000, IV 14 %, per unit (× 65 for one lot):

| Option | Days | Price | Delta | Gamma | Theta / day | Vega / vol pt |
|---|---|---|---|---|---|---|
| 24,000 call | 30 | 450.6 | 0.561 | 0.00041 | −8.65 | 27.1 |
| 24,000 put | 30 | 322.7 | −0.439 | 0.00041 | −4.40 | 27.1 |
| 24,500 call | 30 | 230.2 | 0.359 | 0.00039 | −7.50 | 25.7 |
| 24,000 call | 7 | 200.9 | 0.529 | 0.00086 | −15.45 | 13.2 |
| 24,000 call | 1 | 72.3 | 0.511 | 0.00227 | −37.24 | 5.0 |

- **Delta**: NIFTY +100 points → the 30-day ATM call gains about 56 (≈ ₹3,650 a lot). Also the position's equivalent in futures: 1 lot of 0.56 delta ≈ 0.56 of a futures lot.
- **Gamma**: how fast delta changes. Over 100 points the 30-day delta moves by about 0.04; with 1 day left gamma is five times larger, so a short ATM option near expiry can swing from harmless to deep in the money in minutes.
- **Theta**: time decay. The ATM call loses about ₹8.65 a day (₹562 a lot) at 30 days, ₹15 at 7 days and ₹37 on the last day: decay accelerates into expiry.
- **Vega**: IV from 14 % to 15 % adds about ₹27 (₹1,763 a lot) at 30 days, but only ₹5 on the last day. Long-dated options are volatility trades as much as direction trades.

Sellers have the opposite signs: positive theta, negative gamma and vega. You earn decay by carrying the risk of sharp moves and volatility spikes [[sinclair]].`,
    },
    ...M2_VOL,
  ],
  quiz: M2_QUIZ,
};

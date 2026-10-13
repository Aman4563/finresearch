// Module 3, part 3: volatility strategies (straddles, strangles, naked short premium, calendars, diagonals).
import type { Question, Section, Sketch } from "./types";

const N = (legs: Sketch["legs"], unlimited?: Sketch["unlimited"]): Sketch => ({ spot: 24000, days: 30, iv: 0.14, lot: 65, legs, unlimited });

export const M3_VOL: Section[] = [
  {
    id: "long-straddle",
    title: "Long straddle and strangle",
    body: "Buy a call and a put (same strike: straddle; out-of-the-money strikes: strangle). You profit from a move larger than the combined premium in either direction. You pay double decay and usually a high IV ahead of events, which then collapses: the classic way to be right about a big move and still lose [[natenberg]] [[sinclair]].",
    sketch: N([{ right: "call", side: "buy", strike: 24000 }, { right: "put", side: "buy", strike: 24000 }], "profit"),
    card: { view: "A big move, direction unknown", when: "You expect realised volatility well above the IV you pay, and the IV is not already inflated for the event.", maxProfit: "Unlimited upward; large downward.", maxLoss: "Total premium paid.", greeks: "Delta ≈ 0, gamma +, theta strongly −, vega +.", manage: "Time stop before the last week; exit after the event rather than holding through the IV crush.", backtest: null, builder: "Long straddle" },
  },
  {
    id: "short-straddle",
    title: "Short straddle and strangle (naked): why they are dangerous",
    body: `Selling a call and a put without buying wings collects the most premium and wins most months, which is exactly why it is dangerous. The loss is **unlimited** on the call side and close to unlimited on the put side, the margin is large, and the losses come in gaps that stop-losses cannot catch (overnight news, a budget, a war) [[taleb]] [[sinclair]].

The variance premium (module 2) means naked selling has had a positive average in studies, but the distribution is many small wins and a few catastrophic losses; a single bad week can take more than a year of premium and, with leverage, the account [[carr-wu]] [[taleb]]. The extra 2 % expiry-day margin and SEBI's other expiry-day rules exist because of this risk [[sebi-2024-132]]. **Defined-risk versions (iron condor, iron butterfly) give up a little premium to cap the loss; for a retail account that trade-off is almost always worth it.**`,
    sketch: N([{ right: "call", side: "sell", strike: 24500 }, { right: "put", side: "sell", strike: 23500 }], "loss"),
    card: { view: "Range-bound, with a high tolerance for tail risk", when: "Rarely appropriate for an individual account. If at all: small size, ample spare margin, no events, and a hard plan for gaps.", maxProfit: "Total premium received.", maxLoss: "Unlimited.", greeks: "Delta ≈ 0, gamma strongly −, theta +, vega −.", manage: "Professionals delta-hedge and size by stress loss, not by margin. Most retail accounts should use wings instead.", backtest: null, builder: "Short strangle" },
  },
  {
    id: "calendar",
    title: "Calendar spread",
    body: "Sell a near-expiry option and buy a later expiry at the same strike. You earn the faster decay of the near option and are long vega in the far one. The chart is drawn on the near expiry with the far option still holding time value; it peaks if the index sits at the strike [[natenberg]] [[hull]]. Since 1-Feb-2025 (index) and three months after 5-Feb-2026 (stocks), the two legs get no calendar-spread margin offset on the near leg's expiry day [[sebi-2024-132]] [[sebi-2026-calspread]].",
    sketch: { spot: 24000, days: 30, iv: 0.14, lot: 65, legs: [{ right: "call", side: "sell", strike: 24000 }, { right: "call", side: "buy", strike: 24000, days: 60 }] },
    card: { view: "Neutral near term, with IV expected to hold or rise", when: "Near-dated IV high relative to later expiries (an inverted term structure), or a quiet period expected.", maxProfit: "Depends on the far option's value at the near expiry; highest at the strike.", maxLoss: "Roughly the net debit (more if the term structure moves against you).", greeks: "Delta ≈ 0, gamma −, theta +, vega +.", manage: "Close before the near expiry; a large move either way loses. Model the far leg's IV, not just the price.", backtest: null },
  },
  {
    id: "diagonal",
    title: "Diagonal spread",
    body: "A calendar with different strikes: for example buy a later-expiry call slightly in the money and sell a nearer, higher-strike call against it, repeatedly. It behaves like a covered call that uses an option instead of the shares, with a directional tilt [[natenberg]].",
    sketch: { spot: 24000, days: 30, iv: 0.14, lot: 65, legs: [{ right: "call", side: "buy", strike: 23800, days: 60 }, { right: "call", side: "sell", strike: 24300 }] },
    card: { view: "Mildly bullish (call diagonal) over several expiries", when: "A slow drift up expected, IV not depressed.", maxProfit: "Limited; highest near the short strike at the near expiry.", maxLoss: "The net debit if the index falls far.", greeks: "Delta +, gamma −, theta +, vega +.", manage: "Roll the short leg each cycle; close the whole position if the long leg's thesis breaks.", backtest: null },
  },
];

export const M3_QUIZ: Question[] = [
  { q: "Which strategy has a defined maximum loss?",
    options: ["Short strangle", "Iron condor", "Naked short call", "Short futures"], answer: 1,
    why: "The bought wings cap the iron condor's loss at the wing width minus the credit; the others have unlimited or very large losses." },
  { q: "A bull put credit spread 23,500/23,000 collects ₹92 a unit. The maximum loss per unit is…",
    options: ["₹92", "₹408", "₹500", "Unlimited"], answer: 1,
    why: "Strike width (500) − credit (92) = ₹408 a unit (₹26,520 a lot), over four times the credit: why one bad month can erase several good ones." },
  { q: "You bought a straddle before results, the stock moved 4 %, yet you lost money. The most likely reason?",
    options: ["STT", "The move was smaller than the IV priced in, and IV collapsed after results", "Physical settlement", "Put-call parity"], answer: 1,
    why: "Pre-event IV prices a big move; after the event IV falls (IV crush). A move smaller than implied loses even when it happens." },
  { q: "By put-call parity a covered call has the same payoff as…",
    options: ["A long put", "A short put at the same strike", "A long straddle", "A protective put"], answer: 1,
    why: "Long stock + short call = short put (plus cash). Same risk: most of the stock's downside, capped upside." },
  { q: "Why are naked short straddles described as dangerous for retail accounts?",
    options: ["They never make money", "Loss is unlimited and arrives in gaps a stop cannot catch, despite frequent small wins", "SEBI bans them", "They need physical delivery"], answer: 1,
    why: "They win often, then lose large and suddenly. Defined-risk versions cap that tail for a small cost." },
];

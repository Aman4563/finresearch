// Module 2, part 2: volatility (IV vs realised, IV rank, term structure, skew, the variance risk premium) and the
// evidence on option buyers.
import type { Question, Section } from "./types";

export const M2_VOL: Section[] = [
  {
    id: "iv-vs-rv",
    title: "Implied vs realised volatility",
    body: `**Implied volatility (IV)** is the volatility priced into options today, for the life of the option. **Realised (historical) volatility** is how much the underlying actually moved, measured from past daily returns and annualised [[sinclair]]. IV is a forecast with a price on it; realised volatility is the outcome.

An option buyer profits, roughly, when the underlying moves more than the IV paid for; a seller profits when it moves less [[sinclair]] [[natenberg]]. The app shows ATM IV next to 20- and 60-day realised volatility (the IV/RV ratio) so you can see which one you are betting against.`,
  },
  {
    id: "iv-rank",
    title: "IV rank and IV percentile",
    body: `Two ways to say whether today's IV is high for this underlying, as the app computes them from the daily ATM IV it records [[app-iv]]:

- **IV rank** = (IV today − lowest IV) ÷ (highest IV − lowest IV), over the last 252 recorded days.
- **IV percentile** = the share of those days on which IV was lower than today.

They can disagree. Say IV ranged 10–30 % over the year, with one short spike to 30 % and most days near 12 %. At 14 % the **rank** is (14 − 10) ÷ 20 = **20 %** (looks low) while the **percentile** may be **80 %** (higher than most days). One spike drags the rank down for a year; the percentile is the steadier guide.

The app needs at least 60 recorded days before it shows either number, and says "insufficient history" until then [[app-iv]]. Neither is a signal by itself: high IV is usually high for a reason (results, budget, a crash), and IV can rise further.`,
  },
  {
    id: "term-skew",
    title: "Term structure and skew",
    body: `**Term structure** is IV by expiry. Normally longer expiries carry slightly higher IV; in a sell-off the curve inverts and near-dated IV jumps above the rest. A known event (results, an RBI policy, the Budget) lifts the IV of the expiry that contains it and collapses after the event: the "IV crush" that hurts option buyers who were right about direction but paid too much [[sinclair]] [[natenberg]].

**Skew** is IV by strike. On equity indices, out-of-the-money puts trade at higher IV than equally distant calls, a pattern that became persistent after the 1987 crash: investors pay up for crash protection [[hull]]. The app reports **25-delta skew** = IV of the 25-delta put − IV of the 25-delta call, in volatility points [[app-iv]]. Steep skew makes put spreads cheaper than they look and naked puts riskier than they look.`,
  },
  {
    id: "vrp",
    title: "The volatility risk premium",
    body: `On average, implied volatility has been **higher** than the volatility that followed. Carr and Wu measured this with synthetic variance swaps on US indices and stocks and found a large, negative average return to buying variance (a positive premium to selling it), especially on indices [[carr-wu]]. Delta-hedged long index options lose money on average [[bakshi-kapadia]], and index puts have strongly negative expected returns [[coval-shumway]].

Two cautions:
1. The premium is pay for bearing **crash risk**. Sellers collect small amounts most months and give much of it back in a few violent ones; the average hides the tail [[taleb]] [[sinclair]].
2. These studies are on US data. No India-specific study of the variance premium was verified for these lessons; SEBI's finding that option sellers fared better (below) is consistent with it but is not a measurement of it.`,
  },
  {
    id: "why-buyers-lose",
    title: "Why option buyers usually lose",
    body: `SEBI's FY25–FY26 studies of individual traders [[sebi-pr-50-2026]]:

- **87.7 %** of individual equity-derivative traders lost money in FY26; net losses were about **₹91,685 crore**, about ₹1.17 lakh per trader.
- About **92 %** of losses came from options. Nearly **97 %** of traders were mainly option buyers; only the ~2 % who were mainly option sellers had a **positive median** return.
- About **59 %** of index-option turnover was in contracts expiring the same day.
- Among traders who lost two years running and kept trading, about **90 %** lost again; experience did not improve outcomes.
- 79 % of traders with both winning and losing quarters had smaller average gains than average losses.

The mechanics behind it: time value decays to zero, so a buyer must be right about direction **and** size **and** timing; IV is on average overpriced (the premium above); costs and spreads are paid on every round trip; and cheap far-OTM weekly options feel like lottery tickets but price in a probability that rarely pays off. At fair prices the expected P&L of any option position is minus its costs; an edge must come from somewhere specific, and most traders cannot say where theirs comes from.`,
  },
];

export const M2_QUIZ: Question[] = [
  { q: "NIFTY is at 24,000. A 24,500 call costs ₹230 with 30 days left. Its intrinsic and time value are…",
    options: ["₹500 intrinsic, ₹0 time", "₹0 intrinsic, ₹230 time", "₹230 intrinsic, ₹0 time", "₹270 intrinsic, ₹230 time"], answer: 1,
    why: "An OTM call has no intrinsic value (S < K); the whole premium is time value that decays to zero if NIFTY stays below 24,500." },
  { q: "The ATM call's theta is −8.65 a day at 30 days and −37.24 on the last day. What does that tell a buyer?",
    options: ["Decay is constant", "Decay accelerates into expiry, so holding a long ATM option into the last days is expensive", "Theta only matters for sellers", "Theta turns positive near expiry"], answer: 1,
    why: "Time decay of ATM options speeds up as expiry nears (the theta column), which is why same-day options are so punishing to buy." },
  { q: "IV ranged 10–30 % this year; today it is 14 % and higher than 80 % of days. IV rank and percentile are…",
    options: ["Rank 80 %, percentile 20 %", "Rank 20 %, percentile 80 %", "Both 14 %", "Both 20 %"], answer: 1,
    why: "Rank = (14 − 10) ÷ (30 − 10) = 20 %; percentile = share of days lower = 80 %. A single spike depresses the rank." },
  { q: "Put-call parity says that, for the same strike and expiry, C − P equals…",
    options: ["Zero", "S − K·e^(−rT)", "The premium of a straddle", "IV × √T"], answer: 1,
    why: "C − P = S − K·e^(−rT) for European options without dividends; with a future, (F − K)·e^(−rT)." },
  { q: "What did SEBI find about traders who were mainly option sellers in FY26?",
    options: ["They lost the most", "They were the only group with a positive median return, but were only about 2 % of traders", "They were 97 % of traders", "SEBI did not study them"], answer: 1,
    why: "Mainly option sellers were the only strategy group with a positive median; ~97 % of traders were mainly buyers. A positive median does not mean selling is safe: the tail is where sellers lose." },
];

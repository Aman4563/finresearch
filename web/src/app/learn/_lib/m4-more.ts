// Module 4, part 2: stops, adjustments, expiry day, events, liquidity and margin calls.
import type { Question, Section } from "./types";

export const M4_MORE: Section[] = [
  {
    id: "stops",
    title: "Stop-loss types: premium, underlying, time",
    body: `Three kinds of exit, each with a weakness [[natenberg]] [[sinclair]]:

| Stop | Example | Weakness |
|---|---|---|
| **Premium** | exit a credit spread when its value reaches 2× the credit; exit a long option at −50 % | IV spikes and wide spreads trigger it even when the underlying barely moved |
| **Underlying** | exit if NIFTY closes below the short put strike | slower; the option can lose a lot before the level is hit |
| **Time** | exit at 7 days to expiry, or before a results date | gives up the last part of the decay |

No stop protects against a **gap**: if the market opens 5 % lower, every stop fills at the open. That is what defined-risk structures are for. The app's exit alerts cover all three kinds plus a short-strike delta level and an upcoming event; they alert you and never place an order.`,
  },
  {
    id: "adjustments",
    title: "Adjustments, and when not to adjust",
    body: `Adjusting means changing a position under pressure: rolling the tested side further out, rolling to a later expiry, adding a wing, or closing one side. A useful adjustment leaves a position you would open **today, from scratch**, at a size within your rules.

Do **not** adjust when:
- the adjustment raises the maximum loss or the margin, just to avoid booking a loss. That is the disposition effect, holding losers and selling winners, which costs investors money [[odean]], and loss aversion at work: losses loom larger than equal gains [[kahneman-tversky]];
- you cannot state the new thesis in one sentence;
- the event that broke the trade (results, a policy surprise) is still ahead.

Rolling a losing short option out in time "for a credit" often just postpones and enlarges the loss. Closing and accepting the defined loss is a legitimate adjustment.`,
  },
  {
    id: "expiry-day",
    title: "Avoid expiry-day gambling",
    body: `In SEBI's FY26 study, about **59 %** of index-option turnover was in contracts expiring that same day and 75 % within one day [[sebi-pr-50-2026]]. On expiry day gamma is at its highest (module 2), cheap far-OTM options are near-certain to expire worthless, and sellers face an extra 2 % margin and no calendar-spread offset [[sebi-2024-132]]. SEBI cut weekly expiries to one index per exchange precisely to curb this speculation [[sebi-2024-132]]. A rule worth adopting: no new positions in options expiring today.`,
  },
  {
    id: "events",
    title: "Event risk",
    body: `Results, RBI policy, the Union Budget, elections and global shocks cause gaps. Before the event, IV of the expiry containing it is bid up; after it, IV collapses. Sellers holding through events take gap risk for the extra premium; buyers pay that premium and lose to the crush even when right about direction [[sinclair]] [[natenberg]]. The app's F&O page lists results dates from NSE board-meeting filings before expiry, and an exit rule can warn you a set number of days ahead.`,
  },
  {
    id: "liquidity",
    title: "Liquidity and slippage",
    body: `Every round trip crosses the bid-ask spread. On NIFTY near-the-money strikes it is small; on far strikes, stock options and later expiries it can be several percent of the premium, more than every statutory charge combined. The app's backtest assumes slippage of 1 / 2 / 4 / 8 % of the price for contracts that traded ≥ 1,000 / 100 / 10 / 1 times that day and 15 % for untraded ones, and re-runs with double slippage as a stress test [[app-prereg]]. In practice: use limit orders, check volume and open interest before trading a strike, and avoid strikes you could not exit in a hurry.`,
  },
  {
    id: "margin-calls",
    title: "Margin calls",
    body: `Margin is recomputed through the day as prices and volatility move [[nsecl-margins]]. When a short position moves against you, SPAN rises at the same time as your mark-to-market loss, and the broker asks for more money or squares the position off at the worst moment. Keep a large share of the F&O capital unused as a buffer, know your broker's square-off rules, and never treat a position's current margin as the cash it can cost you.`,
  },
];

export const M4_QUIZ: Question[] = [
  { q: "Capital ₹5 lakh, rule: risk at most 2 % a trade. A spread's maximum loss is ₹26,500 per lot. How many lots?",
    options: ["2", "1", "0: skip or use a narrower structure", "As many as margin allows"], answer: 2,
    why: "The budget is ₹10,000; one lot already risks ₹26,500. The rule says no trade (or a narrower, cheaper structure)." },
  { q: "Win rate 52 %, even-money bets, 20 bets of bankroll. Roughly what is the risk of ruin?",
    options: ["0 %", "About 2 %", "About 20 %", "100 %"], answer: 2,
    why: "((1 − 0.52)/0.52)^20 ≈ 20 %. With a thin edge, bet size decides survival; at 2 % a bet (50 units) it falls to about 2 %." },
  { q: "Which risk can no stop-loss protect against?",
    options: ["Theta", "An overnight gap", "Brokerage", "Low IV"], answer: 1,
    why: "A gap opens beyond the stop; the order fills at the open. Only defined-risk structures cap that loss." },
  { q: "Your short put is tested. Which adjustment breaks the rules?",
    options: ["Closing and taking the defined loss", "Rolling down and out for a credit that doubles the maximum loss", "Buying a wing to define the risk", "Reducing size"], answer: 1,
    why: "An adjustment that raises the maximum loss to avoid booking a loss is the disposition effect in action." },
  { q: "Per SEBI's FY26 study, what share of individuals' index-option turnover was in same-day expiries?",
    options: ["About 10 %", "About 30 %", "About 59 %", "About 97 %"], answer: 2,
    why: "About 59 % same-day (0DTE), 75 % within one day and 97 % within a week of expiry." },
];

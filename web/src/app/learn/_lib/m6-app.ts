// Module 6: using FinResearch for F&O, step by step. Screens are described in text (what you will see), from the
// pages as built: /brief, /fno, /fno/paper, /fno/backtests, /journal, /profile.
import type { Module } from "./types";

export const M6: Module = {
  slug: "app",
  n: 6,
  title: "Using this app, step by step",
  summary: "The workflow the app is built around: set your budget, read the market context, write a plan, test it on paper, let exit alerts watch it, and journal the outcome. The app analyses and alerts; it never places an order.",
  sections: [
    {
      id: "setup",
      title: "0. Set your F&O budget once",
      body: `On [Profile](/profile), set the capital you have set aside for F&O, the maximum loss per strategy as a % of it (2 % by default), your brokerage per order (₹20 by default) and your experience. Until capital is set, the strategy check says so instead of judging the trade. By default only defined-risk strategies pass the check.

*What you see:* a form section for F&O with those fields; the strategy check on /fno quotes them back ("max loss is x % of your F&O capital; limit 2 %").`,
    },
    {
      id: "context",
      title: "1. Market context",
      body: `Start with the [Morning brief](/brief) for today's events, fired rules and data health. Then open [F&O](/fno) and load NIFTY (or a stock) and an expiry.

*What you see:* an amber risk banner (collapsible) with SEBI's latest figure on trader losses [[sebi-pr-50-2026]]; KPI tiles for spot, ATM strike, **PCR (OI)**, **max pain** (context only, see module 5) and **IV rank** with its percentile and the number of recorded days; an event strip listing results dates before the expiry; a bar chart of open interest by strike (calls left, puts right); and the option chain with in-the-money cells shaded.

Ask: is IV high or low for this underlying ([IV rank](/learn/options#iv-rank))? Is there an event inside the expiry ([event risk](/learn/risk#events))? Is today expiry day ([avoid it](/learn/risk#expiry-day))?`,
    },
    {
      id: "plan",
      title: "2. Write the trade plan",
      body: `In [Journal](/journal) → "Plan a trade", write the thesis, what would prove it wrong, the expected holding period, your confidence and a review date. Then build the position in the **strategy builder** on /fno: pick a preset (long call, bull call spread, iron condor…) or tap B / S on chain rows to add legs.

*What you see:* the payoff chart at expiry (profit green, loss red, breakevens as orange dots, spot as a dashed line) with a slider to read the P&L at any price; tiles for maximum profit, maximum loss, net debit or credit and the 1σ expected move; and the **strategy check**: entry and exit costs line by line with each rate's source and status, STT if held to expiry, a risk-neutral and a real-world model chance of profit (both labelled "not a forecast"), and the capital check against your budget.

If the capital check fails, the plan fails: use a smaller structure or no trade ([sizing](/learn/risk#sizing)).`,
    },
    {
      id: "paper",
      title: "3. Paper trade it",
      body: `Under the builder, set a slippage % and press **Paper trade this**. The app records the legs at the live price plus or minus that slippage, with entry costs, and sends nothing to a broker. **Estimate margin** gives an approximate SPAN-style figure, labelled approximate.

*What you see:* a green note "Paper trade #n recorded" with the fills; the [paper book](/fno/paper) lists open and closed paper trades with P&L after costs, marked every 5 minutes in market hours and settled automatically after expiry at the underlying's close.`,
    },
    {
      id: "alerts",
      title: "4. Let exit alerts watch it",
      body: `On a paper trade (or a broker position shown under "My positions"), add exit rules: **profit target** (% of premium, 50 by default), **stop** (% of premium, 100), **days to expiry** (7), **short-strike delta** (0.30) and **event before expiry** (3 days ahead). Each fires one alert when it triggers; nothing is ever traded.

*What you see:* rule chips on the trade card; the alert appears in the app's alerts (the badge in the header) and on the [Monitor](/monitor). Act on it according to your plan, not on the spot.`,
    },
    {
      id: "journal",
      title: "5. Journal and review",
      body: `When the trade is closed (paper or real), record the outcome in the journal against the plan: did you follow it? Once a month open the **behaviour report** in the journal: the disposition gap, turnover, holding periods and the cost of churn [[odean]] [[barber-odean]].

Before trusting any strategy with real money, check its evidence on [backtests](/fno/backtests): each pre-registered variant shows whether it passed every criterion out of sample, its expectancy after costs, drawdown and deflated Sharpe ratio, and a strategy that fails says so plainly [[app-prereg]].`,
    },
  ],
  quiz: [
    { q: "Where does the app take your maximum loss per strategy from?",
      options: ["From SEBI", "From your profile: F&O capital × max-loss %", "From the broker", "It has none"], answer: 1,
      why: "The strategy check compares the maximum loss with fno_max_loss_pct (2 % by default) of the F&O capital you set." },
    { q: "What happens when an exit alert triggers?",
      options: ["The app closes the position", "You get one alert; nothing is traded", "The broker is notified", "The rule deletes itself silently"], answer: 1,
      why: "The app never places orders; alerts are reminders to act on your plan." },
    { q: "The model 'chance of profit' tiles are…",
      options: ["Forecasts you can rely on", "Model probabilities under stated assumptions, not forecasts", "SEBI statistics", "Historical win rates"], answer: 1,
      why: "Both are labelled as model probabilities: one from IV and the risk-free rate, one from realised volatility." },
  ],
};

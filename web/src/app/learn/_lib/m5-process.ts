// Module 5: the process of disciplined traders. Expectancy examples computed by hand (0.7·3,000 − 0.3·8,000 = −300;
// 0.4·9,000 − 0.6·4,000 = 1,200). Evidence on indicators and psychology: m5-evidence.ts.
import { M5_EVIDENCE, M5_QUIZ } from "./m5-evidence";
import type { Module } from "./types";

export const M5: Module = {
  slug: "process",
  n: 5,
  title: "The process of disciplined traders",
  summary: "A written plan with rules fixed in advance, a backtest → paper → small-size pipeline, journaling and review, the statistics that matter (expectancy, not win rate), avoiding overfitting, the psychology of losses, and what the evidence says about indicators, PCR, max pain and open interest.",
  sections: [
    {
      id: "plan",
      title: "A written plan",
      body: `Write it before money is at risk, short enough to follow when stressed:

1. **What** you trade and why it should work (the edge in one sentence, and who is on the other side).
2. **Entry** conditions you could check from data, not a feeling.
3. **Size**: maximum loss per trade and in total (module 4).
4. **Exits**: profit target, stop, time stop, and events you will not hold through.
5. **What would prove the idea wrong**, and how many trades you will take before judging it.

The app's journal has a "Plan a trade" checklist with exactly these fields (thesis, what would prove it wrong, expected holding, confidence, review date) and matches the plan to the trade when it is imported.`,
    },
    {
      id: "pre-register",
      title: "Pre-registered rules",
      body: `If you decide the rules after seeing results, you can always find rules that would have worked. Pre-registration means writing the rules, the test period and the pass criteria **before** the test, and counting every variant you try. The app does this for its own F&O backtest: 8 variants fixed on 12-Oct-2026, in-sample 2022 to mid-2024, out-of-sample after that, six pass criteria, and a hash that makes the evaluator refuse an out-of-sample run if the rules were edited afterwards [[app-prereg]].`,
    },
    {
      id: "pipeline",
      title: "Backtest → paper trade → small size",
      body: `1. **Backtest** with realistic costs, slippage and fills, out of sample. Know its limits: end-of-day fills, assumed slippage and approximate margin are the app's stated ones [[app-prereg]].
2. **Paper trade** forward, in real time, for enough trades to judge (the app's backtest requires at least 30 out-of-sample trades before calling anything a candidate [[app-prereg]]). The app's paper book fills at the live price plus a slippage you set, marks positions every 5 minutes in market hours and settles them after expiry.
3. **Real money at the smallest size** (one lot of a defined-risk structure within your loss budget) and compare live results with the paper ones. Scale only when the live numbers agree with the plan, never after a lucky streak.`,
    },
    {
      id: "journal",
      title: "Journaling and review",
      body: `Record for every trade: the plan, the entry and exit with prices and costs, why you exited, and what you felt. Review weekly: did you follow the plan? Losses from breaking the plan are a discipline problem; losses within the plan are the cost of the strategy.

Measure behaviour, not just P&L. Individual investors who trade most earn the least, mainly through costs [[barber-odean]], and SEBI found that higher trading intensity went with higher loss rates in F&O [[sebi-pr-50-2026]]. The app's behaviour report shows the disposition gap (the proportion of gains you realise minus the proportion of losses, after Odean [[odean]]), your turnover and what churning has cost.`,
    },
    {
      id: "statistics",
      title: "Statistics: expectancy beats win rate",
      body: `**Expectancy** per trade = win rate × average win − loss rate × average loss, after costs.

| Win rate | Avg win | Avg loss | Expectancy |
|---|---|---|---|
| 70 % | ₹3,000 | ₹8,000 | **−₹300** (loses money while winning most trades) |
| 40 % | ₹9,000 | ₹4,000 | **+₹1,200** (wins money while losing most trades) |

Premium selling usually looks like the first row until a big loss arrives, and SEBI found 79 % of traders with both winning and losing quarters had average gains smaller than average losses [[sebi-pr-50-2026]]. Small samples lie: a 20-trade record cannot separate skill from luck. The app's backtest demands a t-statistic of at least 2 **and** a bootstrap confidence interval above zero before it calls anything a candidate [[app-prereg]].`,
    },
    {
      id: "overfitting",
      title: "Avoiding overfitting",
      body: `Try enough variants and the best one will look excellent by chance alone. The **deflated Sharpe ratio** corrects a backtest's Sharpe ratio for the number of variants tried and for non-normal returns [[bailey-dsr]]; the **probability of backtest overfitting** measures how often the in-sample winner disappoints out of sample [[bailey-pbo]]. Bailey and co-authors show how easily a strategy fitted to past data looks good and fails afterwards [[bailey-charlatanism]], and data-snooping corrections remove much of the apparent success of technical trading rules [[sullivan-timmermann-white]].

Practical defences: few parameters, values chosen for a reason before testing, an untouched out-of-sample period, a count of every variant tried, and suspicion of any result that only works with one specific setting.`,
    },
    ...M5_EVIDENCE,
  ],
  quiz: M5_QUIZ,
};

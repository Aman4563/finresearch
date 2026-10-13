// Module 5, part 2: psychology, and what the evidence says about technical indicators, PCR, max pain and OI.
import type { Question, Section } from "./types";

export const M5_EVIDENCE: Section[] = [
  {
    id: "psychology",
    title: "Psychology: loss aversion and revenge trading",
    body: `- **Loss aversion**: losses loom larger than equal gains, and people take more risk to avoid a sure loss than to chase a gain [[kahneman-tversky]]. In trading this becomes holding a losing short option "until it comes back".
- **Disposition effect**: investors sell winners too early and hold losers too long, and the losers they keep go on to do worse than the winners they sold [[odean]].
- **Overtrading**: the most active traders earn the least [[barber-odean]].
- **Revenge trading**: after a loss, a bigger or faster trade to win it back. SEBI found losses persist: about 90 % of traders who lost two years running and kept trading lost again, and experience did not lower loss rates [[sebi-pr-50-2026]].

Rules that help because they are decided in advance: a daily and a monthly loss limit after which you stop, no new trade within an hour of a stop-out, no increase in size after a loss, and a weekly review of whether you kept your own rules.`,
  },
  {
    id: "indicators",
    title: "What the evidence says about indicators, PCR, max pain and OI",
    body: `Be honest about how weak most popular signals are:

- **Technical indicators.** A large survey found many studies reporting profits for technical rules, but most suffered from data snooping, ignored realistic costs or risk, and profits faded in later decades [[park-irwin]]. Correcting for the number of rules tried, the best rules' apparent outperformance largely disappears out of sample [[sullivan-timmermann-white]]. Evidence: weak and contested.
- **Put-call ratio (PCR).** Option volume can carry information about future stock prices, but in the main study the signal came from a non-public breakdown of volume (opening trades by buyers, by investor type), not the plain PCR shown on option chains [[pan-poteshman]]. No verified study supports the chain's PCR as a reliable contrarian signal for Indian indices.
- **Max pain.** On expiry dates, US optionable stocks close near strike prices more often than chance, which the authors link to hedging and possible manipulation [[ni-pearson-poteshman]]. That is a small, stock-level effect; it is not evidence that the max-pain strike predicts where NIFTY settles, and no study verified for these lessons shows that it does.
- **Open-interest "build-up" readings** (long build-up, short covering…). Every open contract has a buyer and a seller, so OI alone cannot say who is "right"; no peer-reviewed evidence for these labels as trading signals was found for these lessons.

The app shows PCR, max pain and OI by strike for context only, says so, and never turns them into a buy or sell call.`,
  },
];

export const M5_QUIZ: Question[] = [
  { q: "Win rate 70 %, average win ₹3,000, average loss ₹8,000. Expectancy per trade?",
    options: ["+₹2,100", "−₹300", "+₹300", "Cannot be computed"], answer: 1,
    why: "0.7 × 3,000 − 0.3 × 8,000 = 2,100 − 2,400 = −₹300. A high win rate with large losses loses money." },
  { q: "You tested 40 variants and kept the best. What does the deflated Sharpe ratio adjust for?",
    options: ["Brokerage", "The number of variants tried and non-normal returns", "Inflation", "Lot size changes"], answer: 1,
    why: "Selection among many trials inflates the best Sharpe ratio; deflating it estimates whether it survives that selection." },
  { q: "Pre-registration means…",
    options: ["Registering with SEBI", "Fixing rules, test windows and pass criteria before running the test", "Paper trading first", "Using only weekly options"], answer: 1,
    why: "Writing the rules first stops you from choosing them after seeing results." },
  { q: "What does the evidence say about max pain as a predictor of NIFTY's expiry close?",
    options: ["Strong evidence it works", "Stock-level pinning exists in US data, but no verified study shows max pain predicts index settlement", "SEBI recommends it", "It is exact"], answer: 1,
    why: "Ni, Pearson and Poteshman found strike clustering in US stocks; that does not validate max pain as an index trading signal." },
  { q: "After three losses in a row, the rule-following response is…",
    options: ["Double the size to recover", "Stop for the day as the plan says and review whether the losses were within the plan", "Switch to same-day expiries", "Remove the stop-loss"], answer: 1,
    why: "Pre-committed loss limits and reviews counter revenge trading and loss aversion." },
];

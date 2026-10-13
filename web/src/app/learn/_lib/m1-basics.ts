// Module 1: how F&O works in India (contracts, lots, expiries, margins, settlement). Costs and tax: m1-costs.ts.
import { M1_COSTS, M1_QUIZ } from "./m1-costs";
import type { Module } from "./types";

export const M1: Module = {
  slug: "basics",
  n: 1,
  title: "How F&O works in India",
  summary: "Futures and options on NSE: what you are actually agreeing to, lot sizes, the expiry calendar, margins, physical settlement of stock F&O, STT, the real cost of a trade, and how F&O profits and losses are taxed.",
  sections: [
    {
      id: "futures-vs-options",
      title: "Futures vs options",
      body: `A **future** is an obligation: both sides agree to trade the underlying at today's futures price on expiry, and gains and losses are settled every day (mark-to-market) until then. Its P&L moves one-for-one with the underlying, up or down, without limit [[hull]].

An **option** is a right without an obligation for the buyer. A **call** gives the right to buy at the strike, a **put** the right to sell at the strike. The buyer pays a premium up front and can lose at most that premium; the **seller (writer)** receives the premium and carries the obligation, so a seller's loss can be many times the premium [[hull]].

| | Futures | Option buyer | Option seller |
|---|---|---|---|
| Pays or receives up front | margin only | full premium | receives premium, posts margin |
| Most you can lose | unlimited (in practice, the whole move) | the premium | unlimited for a naked call; strike − premium for a naked put |
| Daily settlement | yes, mark-to-market | no (the premium is paid) | margin rises and falls with the position |

NSE index options are European-style (exercised only at expiry) and settled in cash [[nse-nifty-spec]]; stock F&O ends in **physical delivery** of the shares (see below) [[sebi-2018-161]].`,
    },
    {
      id: "lot-sizes",
      title: "Lot sizes and contract value",
      body: `You trade whole **lots**. SEBI sets the size through contract value: a new index derivative must be worth at least ₹15 lakh when introduced, and on each review the lot is set so the contract is worth ₹15–20 lakh (in force for index contracts introduced after 20-Nov-2024) [[sebi-2024-132]].

Lots in NSE's file for the October-2026 contracts [[nse-lots]]:

| Index | Symbol | Lot |
|---|---|---|
| NIFTY 50 | NIFTY | 65 |
| NIFTY BANK | BANKNIFTY | 30 |
| NIFTY FINANCIAL SERVICES | FINNIFTY | 60 |
| NIFTY MID SELECT | MIDCPNIFTY | 120 |
| NIFTY NEXT 50 | NIFTYNXT50 | 25 |

So one NIFTY lot with the index at 24,000 controls 24,000 × 65 = **₹15.6 lakh** of exposure. A ₹100 option premium is ₹6,500 per lot, but the risk a seller carries is on the ₹15.6 lakh. Lots are revised periodically; the app reads the current lot from NSE with each option chain.`,
    },
    {
      id: "expiries",
      title: "Expiries: weekly and monthly",
      body: `Since 20-Nov-2024 each exchange may offer weekly expiries on **only one** of its benchmark indices [[sebi-2024-132]]. Since SEBI's May-2025 circular, all equity-derivative expiries of an exchange fall on **either Tuesday or Thursday** (the exchange's choice, with SEBI approval to change); the one weekly benchmark index option stays, and every other contract (index futures, other indices, all stock F&O) has a minimum tenor of one month and expires in the last week of the month on that day [[sebi-2025-76]].

What NSE lists on 13-Oct-2026 [[nse-expiries]]: NIFTY has weekly expiries on Tuesdays (13-Oct, 27-Oct, 3-Nov…), BANKNIFTY only monthly ones (27-Oct, 23-Nov, 29-Dec…). When the Tuesday is a trading holiday the listed date is an earlier day (19-Oct-2026 and 23-Nov-2026 are Mondays). BSE's chosen day was not checked for these lessons.

On **expiry day** the rules get stricter for sellers: an extra 2 % extreme-loss margin on short index options expiring that day (from 20-Nov-2024), no calendar-spread margin offset for contracts expiring that day (index from 1-Feb-2025) [[sebi-2024-132]], and the same for single-stock derivatives under a 5-Feb-2026 circular effective three months after its date [[sebi-2026-calspread]].`,
    },
    {
      id: "margins",
      title: "Margins: SPAN + exposure (ELM)",
      body: `Sellers and futures traders post margin with their broker, who passes it to the clearing corporation. Two parts [[nsecl-margins]]:

1. **SPAN initial margin**: a portfolio scan of price and volatility scenarios sized to a 99 % one-day value at risk (two days for futures). For index products the price scan range is the larger of 3 standard deviations or 5 % of the underlying, and a short option always attracts at least the short-option minimum charge of 5 % [[sebi-2018-155]].
2. **Extreme loss margin (exposure)**: 2 % of notional for index derivatives and 3.5 % for stock derivatives; 3 % for short index options more than 10 % out of the money, plus the extra 2 % on expiry day for short index options [[nsecl-margins]].

Option **buyers** pay the full premium up front: since 1-Feb-2025 brokers must collect the net premium before the trade, so no intraday leverage on buying [[sebi-2024-132]].

What this means for you: margin is not the most you can lose. The margin on a short option is a deposit sized for a normal bad day, not a cap; a gap beyond the scan range is still your loss, and the broker will ask for more margin (or square you off) when the position moves against you. The app's margin figure is an approximation, labelled as such; your broker's calculator is the one that binds.`,
    },
    {
      id: "physical-settlement",
      title: "Physical settlement of stock F&O",
      body: `Every stock future and stock option on NSE settles by **delivery of shares**: SEBI made it mandatory for all stock derivatives, phased in by the October-2019 expiry, and stocks with extreme intraday moves were moved earlier [[sebi-2018-161]] [[sebi-2019-28]].

At expiry:
- a long stock future or an in-the-money **long call** becomes an obligation to **buy** the shares (full value, not margin);
- a short future, an in-the-money **long put**, or an assigned **short call** becomes an obligation to **deliver** shares;
- an assigned **short put** means you must buy the shares at the strike.

One lot of a stock contract is typically several lakh rupees of shares, so expiry can demand that much cash (or the shares themselves). Brokers typically raise margins through expiry week and may close positions they think you cannot settle; check your broker's policy. Groww charges ₹20 per executed physical-delivery transaction on top [[groww-fno]]. If you do not want the shares, close the position before expiry day.`,
    },
    ...M1_COSTS,
  ],
  quiz: M1_QUIZ,
};

// Module 1, part 2: STT, the cost of a trade (worked with Groww's published charges) and taxation.
// Worked numbers: Groww rates read 13-Oct-2026, computed in Python (lot 65; GST 18 % on brokerage + exchange + SEBI + IPFT).
import type { Question, Section } from "./types";

export const M1_COSTS: Section[] = [
  {
    id: "stt",
    title: "STT, including STT on exercise",
    body: `Securities transaction tax on equity F&O in October 2026. Groww's page lists the sale rates [[groww-fno]]; only Zerodha's also lists the rate on exercise [[zerodha-charges]]:

| Trade | STT |
|---|---|
| Selling an option (opening a short or closing a long) | 0.15 % of the premium |
| A long option that expires in the money and is exercised | 0.15 % of the intrinsic value |
| Selling a future | 0.05 % of the contract value |

These are the Finance Bill 2026 rates from 1-Apr-2026 [[budget-2026]]; the app's cost model still marks them unconfirmed until the Finance Act text is checked [[app-charges]]. SEBI notes that the STT increase of 1-Oct-2024 kept individuals' total costs roughly flat even as turnover fell [[sebi-pr-50-2026]].

**Exercise vs selling before the close.** A NIFTY call 100 points in the money at expiry, 1 lot of 65: exercise STT = 0.15 % × ₹6,500 = **₹9.75**. Selling it at ₹100 just before the close costs the same ₹9.75 STT plus ₹20 brokerage and small charges, about ₹36 in all. Older articles warn of an "STT trap" on exercise; at today's rates the STT is the same either way, so the real question is liquidity and slippage in the last minutes, not tax.`,
  },
  {
    id: "costs",
    title: "What one trade really costs (Groww, worked)",
    body: `Groww's F&O charges, read 13-Oct-2026 [[groww-fno]]: brokerage **₹20 per executed order**; NSE transaction charge 0.03503 % of premium (options) or 0.00173 % (futures); SEBI fee 0.0001 %; stamp duty 0.003 % of premium on the buy side (options); investor-protection (IPFT) charge 0.0005 % on options; **GST 18 %** on brokerage, exchange, SEBI and IPFT charges; plus STT above.

**Example: buy 1 lot of a NIFTY call at ₹120, sell at ₹150** (2 orders, 65 units):

| Line | ₹ |
|---|---|
| Brokerage (2 × ₹20) | 40.00 |
| STT (0.15 % of ₹9,750 sale) | 14.62 |
| Exchange charges | 6.15 |
| GST | 8.33 |
| Stamp, SEBI, IPFT | 0.34 |
| **Total** | **69.44** |

Gross profit ₹1,950 → net about ₹1,881 (costs 3.6 % of the gain). The same trade exiting at ₹60 (a ₹3,900 loss) still costs ₹58.20. An iron condor opened and closed (8 orders) costs about **₹220** against a typical gross of a few thousand: multi-leg strategies pay brokerage on every leg. Zerodha and the app's cost model use slightly different exchange-charge rates (0.03553 % / 0.00183 %) [[zerodha-charges]] [[app-charges]]; the difference is paise per lot.

Costs add up: individual traders paid about **₹25,000 crore** in transaction costs in FY26 and about ₹1 lakh crore over FY22–FY26 [[sebi-pr-50-2026]]. Slippage (the spread you cross) is usually larger than all of the above for far out-of-the-money or illiquid strikes.`,
  },
  {
    id: "tax",
    title: "Taxation: F&O is business income",
    body: `> Tax rules here come from secondary sources and were **not re-read from the statute**; the Income-tax Act 2025 renumbers sections from tax year 2026-27. Use this to ask a chartered accountant the right questions, not as the answer.

- **Classification.** Trading exchange-traded derivatives is **non-speculative business income**, intraday or overnight, not capital gains [[varsity-tax]]. Under the 1961 Act this is the proviso to s.43(5) [[ita-1961]].
- **Return.** Business income means **ITR-3** (not ITR-2), with a profit and loss account; Varsity lists business code 21010 for F&O [[varsity-tax]]. Profit is taxed at your slab rate, and advance tax applies.
- **Losses.** An F&O loss can be set off against other income except salary in the same year, and the rest carried forward for **8 years** against business income, provided the return is filed on time [[varsity-tax]] [[ita-1961]].
- **Turnover** for audit is not your traded value: per the ICAI Guidance Note it is the sum of the absolute profit or loss of each trade [[icai-gn]].
- **Tax audit (s.44AB).** Required above ₹1 crore turnover, or ₹10 crore when cash receipts and payments are each within 5 % (normal for F&O). A smaller trader can still need an audit if the presumptive scheme (s.44AD) was used earlier and profit is declared below its rate while income exceeds the exemption limit [[varsity-tax]] [[ita-1961]].

Costs such as brokerage and exchange charges are business expenses; keep the broker's tax P&L and contract notes for every year.`,
  },
];

export const M1_QUIZ: Question[] = [
  { q: "With NIFTY at 24,000 and a lot of 65, roughly how much exposure does one NIFTY futures lot carry?",
    options: ["₹65,000", "₹1.56 lakh", "₹15.6 lakh", "₹24,000"], answer: 2,
    why: "24,000 × 65 = ₹15,60,000, inside SEBI's ₹15–20 lakh contract-value band. Margin is a fraction of this; the risk is on the whole amount." },
  { q: "You hold 1 lot of an in-the-money stock call into expiry. What happens?",
    options: ["It is settled in cash like NIFTY", "You must take delivery and pay for the full lot of shares", "It expires worthless", "The broker always rolls it to next month"], answer: 1,
    why: "All stock derivatives are physically settled: an ITM long call becomes an obligation to buy the shares at the strike." },
  { q: "How many benchmark indices may one exchange offer with weekly expiries?",
    options: ["Unlimited", "One", "Three", "None; weekly expiries were banned"], answer: 1,
    why: "SEBI's 1-Oct-2024 circular allowed one weekly benchmark index per exchange from 20-Nov-2024; the May-2025 circular kept it." },
  { q: "Which statement about margin is true for an option seller?",
    options: ["Margin is the most I can lose", "Margin covers roughly a 99 % one-day move; losses beyond it are still mine", "Margin is only charged on expiry day", "Buyers and sellers post the same margin"], answer: 1,
    why: "SPAN is sized to a 99 % one-day VaR plus an extreme-loss margin. A larger move is still your loss and triggers a margin call." },
  { q: "For tax, profits from F&O trading are generally treated as…",
    options: ["Short-term capital gains at 20 %", "Speculative income", "Non-speculative business income, filed in ITR-3", "Tax-free below ₹1.25 lakh"], answer: 2,
    why: "Exchange-traded derivatives are non-speculative business income, taxed at slab rates and reported in ITR-3 (confirm with a CA)." },
];

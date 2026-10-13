// Every source the lessons cite. Status says how sure we are, and the page shows it next to the citation:
// - verified: the primary document (regulator, exchange, clearing corporation) was read on `checked`;
// - secondary: a broker or education page read on `checked`, not the primary circular or statute;
// - reference: a book or peer-reviewed paper (paper metadata checked against Crossref on `checked`);
// - unverified: stated from the app's earlier research or general knowledge and NOT re-read for these lessons.

import { PAPERS } from "./papers";

export type SourceStatus = "verified" | "secondary" | "reference" | "unverified";
export type Source = { label: string; url?: string; status: SourceStatus; checked?: string; note?: string };

const SEBI = "https://www.sebi.gov.in/legal/circulars";
const C = "2026-10-13"; // the day these lessons' sources were read

export const SOURCES: Record<string, Source> = {
  // regulators, exchanges, clearing
  "sebi-2024-132": { label: "SEBI circular SEBI/HO/MRD/TPD-1/P/CIR/2024/132, 1-Oct-2024: Measures to strengthen equity index derivatives framework", url: `${SEBI}/oct-2024/measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html`, status: "verified", checked: C },
  "sebi-2025-76": { label: "SEBI circular SEBI/HO/MRD/MRD-TPD-1/P/CIR/2025/76, 26-May-2025: Final settlement day (expiry day) for equity derivatives", url: `${SEBI}/may-2025/final-settlement-day-expiry-day-for-equity-derivatives-contracts_94189.html`, status: "verified", checked: C },
  "sebi-2025-79": { label: "SEBI circular SEBI/HO/MRD/TPD-1/P/CIR/2025/79, 29-May-2025: Trading convenience and risk monitoring in equity derivatives (delta-based OI, MWPL, ban period)", url: `${SEBI}/may-2025/measures-for-enhancing-trading-convenience-and-strengthening-risk-monitoring-in-equity-derivatives_94293.html`, status: "verified", checked: C },
  "sebi-2026-calspread": { label: "SEBI circular HO/47/15/11(2)2025-MRD-TPD1/I/4226/2026, 5-Feb-2026: No calendar-spread margin benefit for single-stock derivatives on expiry day", url: `${SEBI}/feb-2026/review-of-calendar-spread-margin-benefit-in-single-stock-derivatives-on-expiry-day_99533.html`, status: "verified", checked: C },
  "sebi-2018-161": { label: "SEBI circular SEBI/HO/MRD/DOPI/CIR/P/2018/161, 31-Dec-2018: Physical settlement of stock derivatives", url: `${SEBI}/dec-2018/physical-settlement-of-stock-derivatives_41482.html`, status: "verified", checked: C },
  "sebi-2019-28": { label: "SEBI circular SEBI/HO/MRD/DOP1/CIR/P/2019/28, 8-Feb-2019: Physical settlement of stock derivatives (volatility criteria)", url: `${SEBI}/feb-2019/physical-settlement-of-stock-derivatives_42021.html`, status: "verified", checked: C },
  "sebi-2018-155": { label: "SEBI circular SEBI/HO/MRD/DRMNP/CIR/P/2018/155, 17-Dec-2018: Review of risk management framework for equity derivatives (price scan range, SOMC)", url: `${SEBI}/dec-2018/review-of-risk-management-framework-for-equity-derivatives-segment_41314.html`, status: "verified", checked: C },
  "sebi-pr-50-2026": { label: "SEBI press release PR No. 50/2026, 20-Aug-2026: Studies on profitability and trading behaviour of individual traders in equity derivatives (FY25–FY26)", url: "https://www.sebi.gov.in/media-and-notifications/press-releases/aug-2026/sebi-studies-indicate-key-trends-in-retail-participation-trading-behaviour-and-profitability-in-the-equity-derivatives_103838.html", status: "verified", checked: C },
  "nse-lots": { label: "NSE, F&O market lots file (fo_mktlots.csv), read 13-Oct-2026", url: "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", status: "verified", checked: C },
  "nse-expiries": { label: "NSE option-chain contract information (expiry dates listed for NIFTY and BANKNIFTY), read 13-Oct-2026", url: "https://www.nseindia.com/option-chain", status: "verified", checked: C, note: "An observation of the listed dates, not a circular." },
  "nse-nifty-spec": { label: "NSE, NIFTY 50 index options product page (contract style and settlement)", url: "https://www.nseindia.com/static/products-services/equity-derivatives-nifty50", status: "unverified", checked: C, note: "Read 13-Oct-2026: it says the options are 'European style' and also 'physical settled', which cannot apply to an index. Cash settlement of index options is the standard reading but was not confirmed from a primary NSE document here." },
  "nsecl-margins": { label: "NSE Clearing, Equity derivatives: Margins (SPAN initial margin, extreme loss margin)", url: "https://www.nseclearing.in/risk-management/equity-derivatives/margins", status: "verified", checked: C },
  // brokers and tax (secondary or unverified)
  "groww-fno": { label: "Groww, Pricing: Futures & Options (brokerage and statutory charges), read 13-Oct-2026", url: "https://groww.in/pricing/futures-and-options", status: "secondary", checked: C },
  "zerodha-charges": { label: "Zerodha, Charges (F&O STT including STT on exercised options), read 13-Oct-2026", url: "https://zerodha.com/charges", status: "secondary", checked: C },
  "budget-2026": { label: "Finance Bill 2026, memorandum (STT: options 0.15 %, futures 0.05 % from 1-Apr-2026)", url: "https://www.indiabudget.gov.in/doc/memo.pdf", status: "unverified", note: "The app marks these rates 'unconfirmed': enactment of the Finance Act 2026 was not re-read. Both brokers above apply them." },
  "varsity-tax": { label: "Zerodha Varsity, Taxation for traders (F&O as non-speculative business income, ITR-3, loss carry-forward)", url: "https://zerodha.com/varsity/chapter/taxation-for-traders/", status: "secondary", checked: C },
  "ita-1961": { label: "Income-tax Act 1961: s.43(5) proviso (d) (exchange-traded derivatives are not speculative), s.44AB (tax audit), s.44AD, s.72 (8-year carry-forward)", url: "https://incometaxindia.gov.in", status: "unverified", note: "Statute text not re-read (the department's site refused automated reads). From tax year 2026-27 the Income-tax Act 2025 applies and section numbers change; the rules are believed unchanged but this is not verified." },
  "icai-gn": { label: "ICAI, Guidance Note on Tax Audit under section 44AB (2022 revision onward): F&O turnover = sum of absolute profits and losses per trade", url: "https://www.icai.org", status: "unverified", note: "Not re-read for these lessons; confirm the current edition with a chartered accountant." },
  // books
  hull: { label: "John C. Hull, Options, Futures, and Other Derivatives, 11th ed., Pearson, 2021", status: "reference" },
  natenberg: { label: "Sheldon Natenberg, Option Volatility and Pricing, 2nd ed., McGraw-Hill, 2015", status: "reference" },
  sinclair: { label: "Euan Sinclair, Volatility Trading, 2nd ed., Wiley, 2013", status: "reference" },
  taleb: { label: "Nassim Nicholas Taleb, Dynamic Hedging: Managing Vanilla and Exotic Options, Wiley, 1997", status: "reference" },
  feller: { label: "William Feller, An Introduction to Probability Theory and Its Applications, Vol. 1, 3rd ed., Wiley, 1968 (random walks and the gambler's ruin)", status: "reference" },
  ...PAPERS,
};

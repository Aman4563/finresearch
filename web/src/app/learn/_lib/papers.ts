// Peer-reviewed papers (title, authors, journal, volume, pages and DOI checked against api.crossref.org on
// 13-Oct-2026) and the app's own documents. Imported into SOURCES (sources.ts).
import type { Source } from "./sources";

const doi = (d: string) => `https://doi.org/${d}`;
const C = "2026-10-13";
const p = (label: string, d: string): Source => ({ label, url: doi(d), status: "reference", checked: C });

export const PAPERS: Record<string, Source> = {
  "black-scholes": p("Black & Scholes (1973), The Pricing of Options and Corporate Liabilities, Journal of Political Economy 81(3) 637–654", "10.1086/260062"),
  "carr-wu": p("Carr & Wu (2009), Variance Risk Premiums, Review of Financial Studies 22(3) 1311–1341", "10.1093/rfs/hhn038"),
  "bakshi-kapadia": p("Bakshi & Kapadia (2003), Delta-Hedged Gains and the Negative Market Volatility Risk Premium, Review of Financial Studies 16(2) 527–566", "10.1093/rfs/hhg002"),
  "coval-shumway": p("Coval & Shumway (2001), Expected Option Returns, Journal of Finance 56(3) 983–1009", "10.1111/0022-1082.00352"),
  "bailey-dsr": p("Bailey & López de Prado (2014), The Deflated Sharpe Ratio, Journal of Portfolio Management 40(5) 94–107", "10.3905/jpm.2014.40.5.094"),
  "bailey-pbo": p("Bailey, Borwein, López de Prado & Zhu (2016), The Probability of Backtest Overfitting, Journal of Computational Finance", "10.21314/JCF.2016.322"),
  "bailey-charlatanism": p("Bailey, Borwein, López de Prado & Zhu (2014), Pseudo-Mathematics and Financial Charlatanism, Notices of the AMS 61(5) 458–471", "10.1090/noti1105"),
  "park-irwin": p("Park & Irwin (2007), What Do We Know About the Profitability of Technical Analysis?, Journal of Economic Surveys 21(4) 786–826", "10.1111/j.1467-6419.2007.00519.x"),
  "sullivan-timmermann-white": p("Sullivan, Timmermann & White (1999), Data-Snooping, Technical Trading Rule Performance, and the Bootstrap, Journal of Finance 54(5) 1647–1691", "10.1111/0022-1082.00163"),
  "pan-poteshman": p("Pan & Poteshman (2006), The Information in Option Volume for Future Stock Prices, Review of Financial Studies 19(3) 871–908", "10.1093/rfs/hhj024"),
  "ni-pearson-poteshman": p("Ni, Pearson & Poteshman (2005), Stock Price Clustering on Option Expiration Dates, Journal of Financial Economics 78(1) 49–87", "10.1016/j.jfineco.2004.08.005"),
  "longin-solnik": p("Longin & Solnik (2001), Extreme Correlation of International Equity Markets, Journal of Finance 56(2) 649–676", "10.1111/0022-1082.00340"),
  "kahneman-tversky": p("Kahneman & Tversky (1979), Prospect Theory: An Analysis of Decision under Risk, Econometrica 47(2) 263–291", "10.2307/1914185"),
  odean: p("Odean (1998), Are Investors Reluctant to Realize Their Losses?, Journal of Finance 53(5) 1775–1798", "10.1111/0022-1082.00072"),
  "barber-odean": p("Barber & Odean (2000), Trading Is Hazardous to Your Wealth, Journal of Finance 55(2) 773–806", "10.1111/0022-1082.00226"),
  // the app's own records
  "app-prereg": { label: "FinResearch, F&O backtest pre-registration (evals/experiments/fno_backtest/PREREG.md, 12-Oct-2026)", url: "https://github.com/Aman4563/finresearch/blob/main/evals/experiments/fno_backtest/PREREG.md", status: "verified", checked: C },
  "app-iv": { label: "FinResearch IV context (src/finresearch/fincalc/volatility.py): ATM IV, IV rank and percentile over the last 252 recorded days (needs 60), 25-delta skew", url: "https://github.com/Aman4563/finresearch/blob/main/src/finresearch/fincalc/volatility.py", status: "verified", checked: C },
  "app-charges": { label: "FinResearch cost model (src/finresearch/fincalc/charges.py): dated STT and charge rates with their status", url: "https://github.com/Aman4563/finresearch/blob/main/src/finresearch/fincalc/charges.py", status: "verified", checked: C },
};

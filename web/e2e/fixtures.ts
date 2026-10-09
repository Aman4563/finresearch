// Synthetic API answers for the smoke tests: made-up companies (Example …), fake ISINs, no personal data. Each is
// typed with the app's own response types (`satisfies`), so `pnpm typecheck` fails when a shape drifts.
import type { Holding, Snapshot, TaxView } from "@/components/portfolio/types";
import type { Signal } from "@/components/signal";
import type { Claim, Report } from "@/lib/api";

const holding = (h: Partial<Holding> & Pick<Holding, "id" | "name" | "asset_type">): Holding => ({
  account: "Example Broker", ikey: `e2e-${h.id}`, isin: null, nse_symbol: null, bse_code: null, scheme_code: null,
  category: null, sector: null, tax_class: "equity", tax_class_auto: "equity", tax_class_why: "listed equity share",
  tax_class_override: null, listed: true, fmv_2018: null, sgb_original_subscriber: false, units: 10, cost: 1000,
  cost_known: true, avg_cost: 100, price: 120, price_as_of: "2026-10-06", price_source: "NSE close (official)",
  price_error: null, value: 1200, unrealised: 200, unrealised_pct: 20, realised: 0, dividends: 0, xirr: 0.12,
  xirr_reason: null, market_cap_cr: null, cap_bucket: "Unknown", lots: 1, closed: false, signal: null, warnings: [],
  sources: ["manual"], pending: false, elss: null, ...h,
});

export const HOLDINGS: Holding[] = [
  holding({ id: 1, name: "Example Textiles Ltd", asset_type: "stock", nse_symbol: "EXTEX", isin: "INE000E01011",
    signal: { asset: "stock", instrument: "EXTEX", href: "/stocks/EXTEX" } }),
  // a statement NAV months old: the price is stale and must say so
  holding({ id: 2, name: "Example Flexi Cap Fund", asset_type: "mf", scheme_code: "990001", tax_class: "other_mf",
    tax_class_auto: "other_mf", units: 50, cost: 2500, avg_cost: 50, price: 61.2, price_as_of: "2026-06-30",
    price_source: "CAS statement NAV", value: 3060, unrealised: 560, unrealised_pct: 22.4, xirr: null,
    xirr_reason: "price is a statement NAV" }),
  // an opening balance with no cost: P&L and tax are unknown, never zero
  holding({ id: 3, name: "Example Opening Balance Fund", asset_type: "mf", scheme_code: "990002", tax_class: "other_mf",
    tax_class_auto: "other_mf", units: 20, cost: null, cost_known: false, avg_cost: null, price: 40, value: 800,
    unrealised: null, unrealised_pct: null, xirr: null, xirr_reason: "cost unknown",
    warnings: ["Opening balance: cost and purchase date are not in the statement"] }),
  // an unresolved demerger (#263): the holding's XIRR is incomplete, never a number
  holding({ id: 4, name: "Example Demerge Ltd", asset_type: "stock", nse_symbol: "EXDEMO", isin: "INE000D01019",
    cost: null, cost_known: false, avg_cost: null, unrealised: null, unrealised_pct: null, xirr: null,
    xirr_reason: "unsupported corporate action: demerger on 2024-06-03 — cost split not modelled; enter the cost allocation manually",
    pending_actions: [{ key: "demerger:2024-06-03", type: "demerger", ex_date: "2024-06-03", subject: "Demerger",
      source: "nse_actions", source_url: null, detected: "2026-10-01", status: "pending",
      reason: "unsupported corporate action: demerger on 2024-06-03 — cost split not modelled; enter the cost allocation manually" }] }),
];

export const SNAPSHOT = {
  as_of: "2026-10-06",
  holdings: HOLDINGS,
  summary: { value: 5060, cost: 3500, unrealised: 760, realised: 0, dividends: 0, xirr: null,
    xirr_reason: "1 holding has an unknown cost", holdings: 3, unknown_cost: 1, unpriced: 0 },
  allocation: { asset: [{ label: "Stocks", value: 1200 }, { label: "Equity funds", value: 3860 }], sector: [], cap: [] },
  cap_list: {},
  timeline: [{ date: "2026-01-02", invested: 3500, realised: 0, dividends: 0 }],
  dividends: { items: [], by_fy: [], note: "" },
  privacy: "Synthetic e2e data.",
  complete: false,
  pending: 0,
  targets: {},
  drift: [],
  lookthrough_coverage: null,
} satisfies Snapshot;

const unclassified = { count: 1, gain: null, no_cost: 1, no_date: 1,
  detail: "1 disposal of Example Opening Balance Fund has no purchase date or cost" };

export const TAX = {
  as_of: "2026-10-06",
  fys: [{
    fy: 2026, label: "FY 2026-27", disposals: 2, stcg: 1500, ltcg: 0, equity_ltcg: 0, exempt: 0, unknown: 1, intraday: 0,
    complete: false, unclassified, rules_verified: true, rules_note: null,
    exemption: { limit: 125000, used: 0, remaining: 125000, complete: false },
    slices: [{ bucket: "equity_st", rate_pct: 20, slab: false, long: false, gain: 1500, set_off: 0, exempted: 0, taxable: 1500 }],
    losses_carried: { short: 0, long: 0 },
    tax: null, cess: null, total: null, total_classified: 312, slab_rate_pct: 30, notes: [],
  }],
  disposals: [],
  harvest: { fy: 2026, label: "FY 2026-27", days_left: 175, deadline: "2027-03-31", exemption_remaining: null,
    tax_so_far: null, complete: false, unclassified, rules_verified: true, rules_note: null, gain_harvest: [],
    loss_harvest: [], notes: [], verify: "Verify with a chartered accountant." },
  rules: [],
  rules_verified_through: { fy: 2026, label: "FY 2026-27", source: "Finance Act 2025" },
  verify: "A personal estimate from the dated rule table; verify with a chartered accountant.",
  caveats: [],
} satisfies TaxView;

// ------------------------------------------------------------------ research report (blocked by the publish gate)
export const CLAIM: Claim = {
  id: 12, run_id: 1, stream: "financials", statement: "Revenue from operations for Fiscal 2026 was Rs. 3,912.40 million.",
  claim_type: "numeric", metric: "revenue_from_operations", value: "3912.40", unit: "INR million", period: "FY2026",
  importance: "high", status: "verified", verifier_note: null, corrects_claim_id: null,
  citations: [{ document_id: 7, document_title: "Example Pumps RHP (synthetic)", page: 14, line_start: 230, line_end: 230,
    quote: "Revenue from operations for Fiscal 2026 was Rs. 3,912.40 million", quote_found: true, url: null,
    accessed_at: null }],
};

export const REPORT = {
  run_id: 1,
  kind: "stock_report",
  markdown: "# Example Pumps and Valves Ltd\n\n## Financials\n\nRevenue grew to Rs. 3,912.40 million in FY2026 [C12].\n",
  published: false,
  gate: { ok: false, blocking: ["C40 is contradicted by its cited lines"], warnings: [] },
  claims: { "12": CLAIM },
} satisfies Report;

export const DOC_LINES = {
  start: 227,
  lines: ["Certain amounts may not add up due to rounding off.", "", "RESTATED FINANCIAL INFORMATION",
    "Revenue from operations for Fiscal 2026 was Rs. 3,912.40 million", "Restated profit after tax was Rs. 402.75 million.",
    "", "Investors should read this section together with the other sections."],
};

// ------------------------------------------------------------------ an informational stock signal (#193)
export const SIGNAL = {
  asset: "stock", instrument: "EXTEX", name: "Example Textiles Ltd", action: "INFORMATIONAL", score: 18,
  event: "12-month total return above NIFTYBEES", horizon: "12 months", method: "composite v1",
  validation: { status: "backtested", n: 120, metrics: {}, description: "Walk-forward backtest of the momentum bucket." },
  probability: 0.55, probability_interval: [0.48, 0.62], expected_return: null,
  base_rate: { n: 6000, p: 0.52, description: "all NIFTY 50 stock-months" },
  factors: [{ name: "12-month momentum", value: 0.18, contribution: 8, explanation: "Up 18 % over 12 months.",
    source: null, unit: null }],
  caveats: ["Informational: the composite would read ACCUMULATE, but no model has shown an edge."],
  sizing: null, sources: [], as_of: "2026-10-06T16:00:00+05:30",
  disclaimer: "Personal research, not investment advice.",
  call: { status: "informational", label: "Informational — no proven edge", tilt: "leans positive",
    composite_action: "ACCUMULATE", promotion_rule: "Shown as a call once a pre-registered test passes.",
    universe_base_rate: null, probability_vs_base: null },
  reliability: null,
} satisfies Signal;

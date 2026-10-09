// Shapes of /api/portfolio* (finresearch.portfolio.report and api/portfolio.py).

export type TaxClass = "equity" | "debt_mf" | "other_mf" | "sgb" | "other";

export const TAX_CLASS_LABEL: Record<TaxClass, string> = {
  equity: "Equity (STT paid)",
  debt_mf: "Debt fund (specified MF)",
  other_mf: "Gold / international fund",
  sgb: "Sovereign Gold Bond",
  other: "Other asset",
};

/** ELSS lock-in of a fund holding (finresearch.portfolio.elss): each lot is locked 3 years from its allotment. */
export type ElssLock = {
  detected: { verified: boolean; why: string } | null;
  as_of: string; price: number | null;
  locked_units: number; locked_value: number | null; unlocked_units: number; unlocked_value: number | null;
  unknown_units: number; unknown_value: number | null; sellable_units: number;
  next_unlock: { day: string; days: number; units: number; value: number | null } | null;
  schedule: { month: string; first: string; units: number; value: number | null; lots: number }[];
  lots: { acquired: string | null; origin: string; units: number; unlocks: string | null; status: "locked" | "unlocked" | "unknown" }[];
  rule: string; source: string; notes: string[];
};

export type Holding = {
  id: number;
  name: string;
  account: string;
  asset_type: "stock" | "mf" | "other";
  ikey: string;
  isin: string | null;
  nse_symbol: string | null;
  bse_code: string | null;
  scheme_code: string | null;
  category: string | null;
  sector: string | null;
  /** the display sector every view groups by (portfolio.limits.sector_label); `sector` is the raw/own label */
  sector_label?: string;
  tax_class: TaxClass;
  tax_class_auto: TaxClass;
  tax_class_why: string;
  tax_class_override: TaxClass | null;
  listed: boolean;
  fmv_2018: number | null;
  sgb_original_subscriber: boolean;
  units: number;
  cost: number | null;
  cost_known: boolean;
  avg_cost: number | null;
  price: number | null;
  price_as_of: string | null;
  price_source: string | null;
  price_error: string | null;
  /** e.g. "NSE symbol changed: OLD is now NEW (matched by ISIN …)" */
  price_note?: string | null;
  /** a statement price older than 5 trading days: still shown, but not a current price (#238) */
  price_stale?: boolean;
  price_stale_reason?: string | null;
  value: number | null;
  unrealised: number | null;
  unrealised_pct: number | null;
  realised: number;
  /** sales whose cost is unknown: their gain is not in `realised` */
  realised_unknown?: number;
  dividends: number;
  xirr: number | null;
  xirr_reason: string | null;
  market_cap_cr: number | null;
  cap_bucket: string;
  lots: number;
  closed: boolean;
  signal: { asset: "stock" | "fund"; instrument: string; href: string } | null;
  warnings: string[];
  /** where its transactions came from: manual, cas, zerodha/groww/upstox (CSV), groww_api ... (broker sync) */
  sources?: string[];
  /** has a broker holdings baseline (average cost, purchase date unknown) */
  broker_baseline?: boolean;
  /** the price is still being fetched (the page streams it in) */
  pending?: boolean;
  /** ELSS lock-in, for a fund recognised as ELSS (else null) */
  elss?: ElssLock | null;
  /** #237: unresolved unsupported corporate actions (demerger, rights ...): the cost is unknown until resolved */
  pending_actions?: PendingAction[];
};

/** A corporate action the lots do not model (finresearch.portfolio.service.record_unsupported, #237). */
export type PendingAction = {
  key: string; type: string; ex_date: string; subject: string; source: string; source_url: string | null;
  detected: string; status: "pending" | "resolved"; note?: string; resolved?: string; reason: string;
};

/** #236: a row another source already has (skipped), or a partial overlap (a conflict: not added). */
export type CrossSource = {
  name: string; account: string; day: string; kind: string; units: string; matched?: string; sources: string[];
  accounts?: string[]; label?: string; other_units?: string; why?: string;
};

export type Slice = { label: string; value: number };

export type Snapshot = {
  as_of: string;
  holdings: Holding[];
  summary: {
    value: number; cost: number; unrealised: number; realised: number; dividends: number; xirr: number | null;
    xirr_reason: string | null; holdings: number; unknown_cost: number; unpriced: number;
    stale?: number; stale_note?: string | null;
    realised_unknown?: number; realised_unknown_proceeds?: number; realised_note?: string | null;
  };
  allocation: { asset: Slice[]; sector: Slice[]; cap: Slice[] };
  cap_list: Record<string, string>;
  timeline: { date: string; invested: number; realised: number; dividends: number }[];
  dividends: {
    items: { day: string; holding_id: number; name: string; amount: number; reinvested: boolean }[];
    by_fy: { fy: number; label: string; amount: number }[];
    note: string;
  };
  privacy: string;
  complete: boolean;
  /** holdings whose price was not cached yet (prices=cached) */
  pending?: number;
  targets?: Record<string, number>;
  drift?: { label: string; weight_pct: number; target_pct: number; drift_pp: number }[];
  /** #214: the share of the portfolio in funds not looked through; null = unknown (shown as such) */
  lookthrough_coverage?: import("./lookthrough-panel").Coverage | null;
};

export const ASSET_CLASSES = ["Stocks", "Equity funds", "Debt funds", "Gold & international funds", "Sovereign Gold Bonds", "Other"] as const;

export type HoldingDetail = {
  id: number; name: string; account: string; asset_type: string;
  transactions: {
    id: number; day: string; kind: string; quantity: string | null; price: string | null; amount: string | null;
    charges: string | null; stt_paid: boolean; source: string; import_id: number | null; note: string | null;
    meta: Record<string, unknown>;
  }[];
  lots: { id: number; acquired: string | null; origin: string; quantity: string; open_quantity: string; cost_per_unit: string | null; stt_paid: boolean }[];
  disposals: { id: number; acquired: string | null; sold: string; quantity: string; cost: string | null; proceeds: string; origin: string }[];
  warnings: string[];
  elss?: ElssLock | null;
  pending_actions?: PendingAction[];
};

// a CAS reconciliation sends statement_units / lot_units; a broker holdings statement (connectors.merge) sends
// broker_units / app_units and a status ("not_at_broker": the app holds units the statement does not list)
export type Reconciliation = { name: string; account: string; ikey: string; statement_units?: string; lot_units?: string;
  broker_units?: string; app_units?: string; status?: string; diff: string; ok: boolean; as_of?: string | null;
  /** #237: an unresolved corporate action on this holding that may explain the difference */
  pending_action?: string | null };

export type ImportPreview = {
  dry_run: boolean;
  kind: string;
  source: string;
  period: [string, string] | null;
  rows: number;
  new_rows?: number;
  duplicates: number;
  added?: number;
  import_id?: number;
  holdings?: { name: string; account: string; ikey: string; new_rows: number; existing: boolean; units_after: string; warnings: string[] }[];
  reconciliation: Reconciliation[];
  reconciled: boolean;
  warnings: string[];
  skipped: Record<string, number>;
  holdings_only?: boolean;
  already_imported?: number | null;
  sample?: { day: string; kind: string; name: string; account: string; quantity: string | null; price: string | null; amount: string | null }[];
  /** #236: rows another source already has (never shown as new), partial overlaps, fund baselines a CAS replaces */
  cross_source?: CrossSource[];
  conflicts?: CrossSource[];
  superseded_baselines?: (CrossSource & { cas_units?: string })[];
  rows_preview?: { day: string; kind: string; name: string; account: string; quantity: string | null; price: string | null; amount: string | null; status: string }[];
};

export type ImportRow = { id: number; kind: string; source: string; filename: string; created_at: string | null; summary: Partial<ImportPreview> };

export type FySummary = {
  fy: number; label: string; disposals: number; stcg: number; ltcg: number; equity_ltcg: number; exempt: number; unknown: number;
  intraday: number;
  /** false when a disposal can't be classified short/long-term (#213): tax, cess and total are then null */
  complete: boolean; unclassified: Unclassified; rules_verified: boolean; rules_note: string | null;
  exemption: { limit: number; used: number; remaining: number; complete: boolean };
  slices: { bucket: string; rate_pct: number | null; slab: boolean; long: boolean; gain: number; set_off: number; exempted: number; taxable: number }[];
  losses_carried: { short: number; long: number };
  tax: number | null; cess: number | null; total: number | null; total_classified: number; slab_rate_pct: number; notes: string[];
};

export type Unclassified = { count: number; gain: number | null; no_cost: number; no_date: number; detail: string; corporate_action?: number };

export type DisposalRow = {
  holding_id: number; name: string; account: string; fy: number; fy_label: string; tax_class: TaxClass; acquired: string | null;
  sold: string; quantity: number; cost: number | null; tax_cost: number | null; proceeds: number; gain: number | null;
  term: "short" | "long" | "exempt" | "unknown" | null; bucket: string | null; rate_pct: number | null; slab: boolean;
  rule: string | null; holding_days: number | null; stt_paid: boolean; notes: string[];
};

export type HarvestIdea = {
  holding_id: number; name: string; account: string; sell_units: number; price: number; value: number; est_costs: number; why: string;
  gain?: number; future_tax_saved_up_to?: number; loss?: number; short_term?: boolean; tax_saved?: number; estimate?: boolean;
};

export type TaxRule = {
  id: string; tax_class: string; effective_from: string; effective_to: string | null; long_term_months: number | null;
  stcg_rate_pct: number | null; ltcg_rate_pct: number | null; ltcg_exemption: number; status: "verified" | "secondary" | "uncertain";
  source: string; note: string; exempt: boolean;
};

export type TaxView = {
  as_of: string;
  fys: FySummary[];
  disposals: DisposalRow[];
  harvest: {
    fy: number; label: string; days_left: number; deadline: string; exemption_remaining: number | null; tax_so_far: number | null;
    complete: boolean; unclassified: Unclassified; rules_verified: boolean; rules_note: string | null;
    gain_harvest: HarvestIdea[]; loss_harvest: HarvestIdea[]; notes: string[]; verify: string;
  };
  rules: TaxRule[];
  rules_verified_through: { fy: number; label: string; source: string };
  verify: string;
  caveats: string[];
};

export const inr = (v: number | null | undefined, d = 0) =>
  v == null ? "—" : `${v < 0 ? "−" : ""}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d })}`;
export const signed = (v: number | null | undefined, d = 0) => (v == null ? "—" : `${v > 0 ? "+" : ""}${inr(v, d)}`);
export const pctx = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(d)}%`);
export const units = (v: number | string | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 3 });

// AIS check (/api/portfolio/ais): the user's AIS for one FY against the app's dividends and trades
export type AisStatus = "matched" | "mismatch" | "only_ais" | "only_app";
export type AisCategory = "dividend" | "sale" | "purchase";
export type AisSourceRow = { day: string | null; code: string | null; part: string; source: string | null; tan: string | null; security: string | null; amount: number | null; tds: number | null; quantity: number | null; stt: number | null };
export type AisRow = {
  category: AisCategory; label: string; isin: string | null; match: "isin" | "name" | "amc" | "total" | null; status: AisStatus;
  ais_amount: number | null; app_amount: number | null; diff: number | null; ais_tds: number | null;
  ais_quantity: number | null; app_quantity: number | null; holdings: string[]; duplicates_dropped: number;
  ais_rows: AisSourceRow[]; app_rows: { day: string; name: string; amount: number | null; quantity: number | null }[];
  cause: string | null; action: string | null;
};
export type AisInfo = { category: "interest" | "off_market"; label: string; ais_amount: number | null; ais_tds: number | null; duplicates_dropped: number; ais_rows: AisSourceRow[]; note: string };
export type AisCheck = {
  fy: number; label: string; counts: Record<AisStatus, number>; totals: Record<AisCategory, { ais: number; app: number }>;
  rows: AisRow[]; info: AisInfo[]; tolerance: string; notes: string[];
};
export type AisList = {
  statements: { fy: number; label: string; format: string; rows: number; ignored: number; imported_at: string | null; warnings: string[] }[];
  app_years: { fy: number; label: string }[];
  /** formats (json | pdf) a saved import has validated: no unrecognised rows and matching totals (#216) */
  validated?: Record<string, { validated_at: string; fy: number }>;
};
export type AisImport = { dry_run: boolean; already_imported: number | null; fy: number; format: string; rows: number; ignored: number; warnings: string[]; check: AisCheck };

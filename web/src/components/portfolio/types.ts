// Shapes of /api/portfolio* (finresearch.portfolio.report and api/portfolio.py).

export type TaxClass = "equity" | "debt_mf" | "other_mf" | "sgb" | "other";

export const TAX_CLASS_LABEL: Record<TaxClass, string> = {
  equity: "Equity (STT paid)",
  debt_mf: "Debt fund (specified MF)",
  other_mf: "Gold / international fund",
  sgb: "Sovereign Gold Bond",
  other: "Other asset",
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
  value: number | null;
  unrealised: number | null;
  unrealised_pct: number | null;
  realised: number;
  dividends: number;
  xirr: number | null;
  xirr_reason: string | null;
  market_cap_cr: number | null;
  cap_bucket: string;
  lots: number;
  closed: boolean;
  signal: { asset: "stock" | "fund"; instrument: string; href: string } | null;
  warnings: string[];
};

export type Slice = { label: string; value: number };

export type Snapshot = {
  as_of: string;
  holdings: Holding[];
  summary: {
    value: number; cost: number; unrealised: number; realised: number; dividends: number; xirr: number | null;
    xirr_reason: string | null; holdings: number; unknown_cost: number; unpriced: number;
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
  targets?: Record<string, number>;
  drift?: { label: string; weight_pct: number; target_pct: number; drift_pp: number }[];
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
};

export type Reconciliation = { name: string; account: string; ikey: string; statement_units: string; lot_units: string; diff: string; ok: boolean; as_of: string | null };

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
};

export type ImportRow = { id: number; kind: string; source: string; filename: string; created_at: string | null; summary: Partial<ImportPreview> };

export type FySummary = {
  fy: number; label: string; disposals: number; stcg: number; ltcg: number; equity_ltcg: number; exempt: number; unknown: number;
  intraday: number;
  exemption: { limit: number; used: number; remaining: number };
  slices: { bucket: string; rate_pct: number | null; slab: boolean; long: boolean; gain: number; set_off: number; exempted: number; taxable: number }[];
  losses_carried: { short: number; long: number };
  tax: number; cess: number; total: number; slab_rate_pct: number; notes: string[];
};

export type DisposalRow = {
  holding_id: number; name: string; account: string; fy: number; fy_label: string; tax_class: TaxClass; acquired: string | null;
  sold: string; quantity: number; cost: number | null; tax_cost: number | null; proceeds: number; gain: number | null;
  term: "short" | "long" | "exempt" | "unknown" | null; bucket: string | null; rate_pct: number | null; slab: boolean;
  rule: string | null; holding_days: number | null; stt_paid: boolean; notes: string[];
};

export type HarvestIdea = {
  holding_id: number; name: string; account: string; sell_units: number; price: number; value: number; est_costs: number; why: string;
  gain?: number; future_tax_saved_up_to?: number; loss?: number; short_term?: boolean; tax_saved?: number;
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
    fy: number; label: string; days_left: number; deadline: string; exemption_remaining: number; tax_so_far: number;
    gain_harvest: HarvestIdea[]; loss_harvest: HarvestIdea[]; notes: string[]; verify: string;
  };
  rules: TaxRule[];
  verify: string;
  caveats: string[];
};

export const inr = (v: number | null | undefined, d = 0) =>
  v == null ? "—" : `${v < 0 ? "−" : ""}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d })}`;
export const signed = (v: number | null | undefined, d = 0) => (v == null ? "—" : `${v > 0 ? "+" : ""}${inr(v, d)}`);
export const pctx = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(d)}%`);
export const units = (v: number | string | null | undefined) =>
  v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: 3 });

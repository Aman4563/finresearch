// Shapes of /api/wealth* (finresearch.wealth.service and api/wealth.py). Local to the /wealth page.

export type AssetKind = "fd" | "rd" | "epf" | "ppf" | "nps" | "gold" | "sgb" | "real_estate" | "cash" | "other";
export type LoanKind = "home" | "car" | "personal" | "education" | "other";
export type AssetClass = "Equity" | "Debt" | "Gold" | "Cash" | "Real estate" | "Other";

export const ASSET_KIND_LABEL: Record<AssetKind, string> = {
  cash: "Savings / cash", fd: "Fixed deposit", rd: "Recurring deposit", epf: "EPF", ppf: "PPF", nps: "NPS",
  gold: "Physical gold", sgb: "Sovereign Gold Bond", real_estate: "Real estate", other: "Other",
};
export const LOAN_KIND_LABEL: Record<LoanKind, string> = {
  home: "Home loan", car: "Car loan", personal: "Personal loan", education: "Education loan", other: "Other loan",
};

export type Valuation = { value: number | null; method: string; as_of: string | null; stale: boolean; matured?: boolean };

export type Asset = {
  id: number; kind: AssetKind; name: string; institution: string | null; asset_class: AssetClass | null;
  principal: number | null; rate_pct: number | null; compounding: number; monthly_contribution: number | null;
  start_date: string | null; maturity_date: string | null; liquid: boolean; equity_pct: number | null; notes: string | null;
  valuation: Valuation; history: { day: string; value: number }[];
};

export type PrepayVsInvest = {
  after_tax_loan_pct: number; deductible_share: number; deduction_note: string; debt_post_tax_pct: number;
  equity_post_tax_pct: number; p_equity_beats_loan: number | null; years_left: number; reading: string;
};

export type Loan = {
  id: number; kind: LoanKind; name: string; lender: string | null; principal: number; rate_pct: number; tenure_months: number;
  emi: number | null; start_date: string; outstanding: number | null; outstanding_as_of: string | null; floating: boolean;
  notes: string | null; emi_used: number; emi_computed: boolean; outstanding_now: number | null; months_left: number | null;
  active: boolean; prepay_vs_invest: PrepayVsInvest | null;
};

export type Goal = {
  id: number; name: string; target_inr: number; target_date: string; priority: "high" | "medium" | "low";
  inflation_pct: number; current_inr: number; monthly_sip: number; step_up_pct: number; linked_asset_ids: number[];
  portfolio_pct: number; equity_pct: number | null; gold_pct: number; in_cover: boolean; notes: string | null;
  funded_now: number; months_left: number; shared_links: number[];
};

export type Policy = {
  id: number; kind: "term" | "health" | "other"; name: string; cover_inr: number; premium_inr: number | null;
  end_date: string | null; employer: boolean; notes: string | null;
};

export type Household = {
  age: number | null; retirement_age: number; monthly_income_inr: string | null; monthly_expenses_inr: string | null;
  dependants: number; earners: number; emergency_months_target: string | null; support_years: number | null;
  tax_regime: "new" | "old"; target_equity_pct: string | null;
};

export type ClassAssumption = { mu_pct: number; sigma_pct: number; note: string; default: { mu_pct: number; sigma_pct: number } };
export type Assumptions = { equity: ClassAssumption; debt: ClassAssumption; gold: ClassAssumption; inflation_pct: number; seed: number; n: number };

export type NetWorthPoint = {
  // portfolio null = unknown (holdings but no valuation for that day): the totals then add up the known parts only
  date: string; portfolio: number | null; manual: number; assets: number; liabilities: number; net_worth: number;
  liquid_net_worth: number; portfolio_day: string | null; portfolio_source?: "reconstructed" | "as shown";
  complete: boolean; missing: string[];
};

export type Wealth = {
  as_of: string;
  net_worth: NetWorthPoint;
  portfolio: { value: number | null; day: string | null; complete: boolean | null; why?: string | null; by_class: Record<string, number> };
  assets: Asset[];
  loans: Loan[];
  goals: Goal[];
  policies: Policy[];
  allocation: {
    by_class: Record<string, number>; weights_financial: Record<string, number>; target: Record<string, number> | null;
    comparison: { label: string; weight_pct: number; target_pct: number; drift_pp: number; band_pp: number; outside_band: boolean }[];
    message: string; rule: string; risk_appetite: string; own_target: boolean; bands?: { abs_pp: number; rel_pct: number };
  };
  emergency: {
    liquid: number; monthly_expenses: number | null; months: number | null; target_months: number; target_why: string;
    message: string; banks: { bank: string; total: number; count: number; over_limit: boolean; uninsured: number }[];
    dicgc_limit: number; dicgc_source: string;
  };
  insurance: {
    term_existing: number; health_total: number; health_employer: number; health_rule_inr: number; term_message: string;
    term: { expenses_pv: number; loans: number; goal_gaps: number; assets: number; existing: number; need: number; gap: number; years: number; real_rate_pct: number } | null;
    income_multiple: number[] | null;
  };
  debt: { total_emi: number; monthly_income: number | null; foir_pct: number | null; message: string; weighted_rate_pct: number | null; debt_to_assets_pct: number | null };
  household: Household;
  tax_slab_pct: number;
  assumptions: Assumptions;
  history: NetWorthPoint[];
  rates: Record<string, { rate_pct: string; as_of: string; note: string }>;
  privacy: string;
  disclaimer: string;
};

export type GoalPlan = {
  goal_id: number; name: string; months: number; target_today: number; target_nominal: number; start: number; sip0: number;
  step_up_pct: number; n: number; seed: number; p_success: number; p_ci: [number, number]; p_haircut: number;
  p_plus_year: number | null; p_step_up_plus5: number | null; sip_for_75: number | null; sip_for_90: number | null;
  fan: { month: number; p10: number; p50: number; p90: number; target: number }[];
  terminal_pcts: { p10: number; p50: number; p90: number };
  assumptions: Record<string, { mu_pct: number; sigma_pct: number; note: string }>;
  equity_path: string; message: string; method: string; disclaimer: string; linked: number[];
};

export const inr = (v: number | null | undefined, d = 0) =>
  v == null ? "—" : `₹${v.toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d })}`;
export const pc = (v: number | null | undefined, d = 1) => (v == null ? "—" : `${v.toFixed(d)}%`);
export const prob = (p: number | null | undefined) =>
  p == null ? "—" : p > 0 && p < 0.005 ? "<1%" : p < 1 && p > 0.995 ? ">99%" : `${Math.round(p * 100)}%`;
export const num = (s: string) => (s.trim() === "" ? null : s.trim());

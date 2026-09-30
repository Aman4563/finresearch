// Types for GET /api/runs/{id}/insights: chart-ready figures derived from the run's claim ledger (no model).

/** A cited number: `value` is unit-normalised for charts, `raw` is exactly what the claim holds. */
export type Pt = {
  value: number;
  display: string;
  unit: string;
  raw: string;
  raw_unit: string | null;
  period: string | null;
  claim_id: number;
  status: string;
  metric: string;
};

export type SeriesPt = Pt & { period: string; period_raw: string | null };
export type FinSeries = {
  key: string;
  label: string;
  group: "income" | "margin" | "returns" | "balance" | "cash" | "per_share" | "other";
  freq: "annual" | "quarterly";
  unit: string;
  points: SeriesPt[];
  family: boolean;
};

export type Tile = Pt & { label: string; term: string | null; hint: string | null; delta: { pct: number; vs: string; claim_ids: number[] } | null };
export type CasePoint = { text: string; weight: string | null; claim_ids: number[] };
export type Risk = { rank: number; title: string; text: string; severity: string | null; claim_ids: number[] };
export type PeerSet = { key: string; label: string; unit: string; rows: (Pt & { name: string })[]; subject: (Pt & { name: string }) | null };

export type IpoInsights = {
  price_band: { low: Pt | null; high: Pt | null; face_value: Pt | null };
  lot: { shares: Pt | null; cost_cap?: Pt | null; cost_floor?: Pt | null };
  issue: { total: Pt | null; fresh: Pt | null; ofs: Pt | null };
  proceeds: (Pt & { label: string; cap: boolean })[];
  anchor: { shares: Pt | null; amount: Pt | null; mf_shares: Pt | null; insurance_shares: Pt | null };
  holding: { pre: Pt | null; post: Pt | null; post_floor: Pt | null };
  subscription: (Pt & { category: string; label: string })[];
  subscription_as_of: string | null;
  subscription_timeline: ({ as_of: string; source: string | null; total: number | null } & Partial<Record<"qib" | "nii" | "bnii" | "snii" | "retail", number>>)[];
  reservation: { label: string; value: number; display: string; claim_id: number; status: string }[];
  timeline: { title: string; date: string; claim_id?: number; status?: string; source?: string }[];
};

export type FundInsights = {
  returns: ({ horizon: string } & Record<"fund" | "benchmark" | "category" | "index_fund", Pt | null>)[];
  peers: ({ name: string } & Partial<Record<"r1y" | "r3y" | "r5y", Pt>>)[];
  rolling: { min: Pt | null; median: Pt | null; max: Pt | null };
  phases: (Pt & { label: string })[];
  risk: (Pt & { label: string; key: string })[];
  costs: (Pt & { label: string })[];
  allocation: (Pt & { label: string })[];
  nav: Pt | null;
  aum: Pt | null;
};

export type BondInsights = {
  coupon: Pt | null;
  face_value: Pt | null;
  last_price: Pt | null;
  frequency: Pt | null;
  issue_size: Pt | null;
  maturity: { date: string; claim_id: number; status: string } | null;
  yields: (Pt & { label: string })[];
  ratings: { agency: string; text: string; claim_id: number; status: string }[];
  accrued: Pt | null;
  duration: Pt | null;
};

/** One accounting-identity / recomputation / scale check (verify/identities.py). */
export type IdentityCheck = {
  family: string; title: string; status: "pass" | "warn" | "fail"; period: string | null; basis: string; formula: string; detail: string;
  claims: { claim_id: number; metric: string; value: string; unit: string | null; period: string | null; status: string; importance: string }[];
  claim_ids: number[]; cited: number[]; expected: number | null; actual: number | null; diff_pct: number | null; tolerance: string | null;
  hard: boolean; hint: string | null;
};
export type Accuracy = { counts: { pass: number; warn: number; fail: number }; applicable: number; facts: number; checks: IdentityCheck[]; not_enough_inputs: string[] };

export type McRange = { low: number; mode: number; high: number; source: string };
export type Triangulation = {
  status: "ok" | "not_enough_inputs";
  unit: string; years: number; seed: number;
  inputs: { name: string; value: number | null; display: string; source: string; claim_id: number | null; status: string | null }[];
  missing: string[]; notes: string[]; cash_flow_basis: string | null;
  history: { label: string; value: number; claim_ids: number[]; status: string }[];
  price: number | null; price_claim: number | null;
  reverse_dcf: { implied_growth: number | null; discount_rate: number; terminal_growth: number; years: number; note: string | null } | null;
  monte_carlo: {
    p5: number; p25: number; p50: number; p75: number; p95: number; mean: number; prob_above_price: number | null; n: number; rejected: number;
    histogram: { lo: number; hi: number; count: number }[]; ranges: Record<"growth" | "discount_rate" | "terminal_growth", McRange>;
  } | null;
  grid: { growth: number; discount_rates: number[]; terminal_growths: number[]; values: (number | null)[][] } | null;
  peers: { metric: string; n: number; q1: number; median: number; q3: number; own: number; own_claim: number; own_label: string; percentile: number | null;
    claim_ids: number[]; values: number[]; implied?: { low: number; mid: number; high: number } } | null;
  band: { low: number; high: number; intersection: [number, number] | null; disagree: boolean; spread: number; methods: { name: string; low: number; high: number }[] } | null;
};

export type Insights = {
  run_id: number;
  kind: string;
  subject: { slug?: string | null; name?: string | null; nse_symbol?: string | null; isin?: string | null; amfi_code?: string | null; watch_id?: number };
  verdict: {
    word: string | null; confidence: string | null; horizon: string | null; entry_zone: string | null; price_or_yield: string | null;
    suits: string | null; condition: string | null; listing: string | null; long_term: string | null; summary: string | null;
  };
  plain_english: string[];
  key_numbers: Tile[];
  financials: { series: FinSeries[] };
  valuation: { multiples: (Pt & { key: string; label: string; context: string | null })[]; peers: PeerSet[]; fair_value: (Pt & { label: string; group: string; role: "low" | "mid" | "high" | null; basis: string | null })[] };
  ipo: IpoInsights | null;
  fund: FundInsights | null;
  bond: BondInsights | null;
  scenarios: { name: string; horizon: string; low: number | null; high: number | null; likelihood: string | null; rationale: string }[];
  pros: CasePoint[];
  cons: CasePoint[];
  risks: Risk[];
  checklist: { text: string; claim_ids: number[] }[];
  alternatives: string[];
  quality: {
    total: number; by_status: Record<string, number>; verified_pct: number | null; cited: number; cited_verified_pct: number | null;
    by_stream: ({ stream: string; total: number } & Record<string, number | string>)[];
  };
  /** Absent on insights served by an older API. */
  accuracy?: Accuracy;
  triangulation?: Triangulation | null;
};

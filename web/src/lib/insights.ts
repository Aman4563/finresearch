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
};

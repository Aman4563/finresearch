// Shapes of /api/brief, /api/brief/digest, /api/brief/calendar and /api/dashboard/portfolio (finresearch.api.brief).

export type BriefEvent = { day: string; kind: string; title: string; path?: string; verified?: boolean; note?: string };
export type FiredAlert = { id: number; kind: string; level: string; message: string; at: string; path: string };
export type SignalChange = { instrument: string; name: string; from: string; to: string };
export type Sip = {
  holding_id: number; name: string; account: string; amount: number; day_of_month: number; instalments: number;
  last: string; next_expected: string; days_since: number; status: "on track" | "missed" | "stopped";
};
export type LtLot = {
  holding_id: number; name: string; account: string; acquired: string; quantity: number; lt_date: string; days: number;
  gain: number; tax_now: number; tax_later: number; saved: number; older_lots: number; price: number;
};
export type TaxItem = { day: string; kind: string; title: string; verified: boolean; note: string };
export type AdvanceTax = {
  fy: number; capital_gains_tax: number; dividends: number; dividend_tax: number; total: number; threshold: number;
  below_threshold: boolean; next: { due: string; cumulative_pct: number; amount: number; days: number } | null;
  schedule: { due: string; cumulative_pct: number; label: string; amount: number; past: boolean }[];
  status: string; note: string; caveats: string[];
};
export type ValueChange = {
  from: string; to: string; start: number; end: number; change: number; new_money: number; market: number;
  twr_pct: number; days: number;
};
export type Channel = "ntfy" | "telegram" | "macos";
export type BriefSettings = { enabled: boolean; channels: Channel[]; daily_performance: boolean; weekly_digest: boolean };

export type Brief = {
  day: string; generated_at: string; has_portfolio: boolean; headline: string; events: BriefEvent[];
  rules_fired: FiredAlert[]; since: string; signal_changes: SignalChange[]; sip: Sip[]; sip_missed: Sip[];
  long_term: LtLot[]; tax_calendar: TaxItem[]; advance_tax: AdvanceTax | null;
  health: { level: "warn" | "info"; text: string }[]; performance: ValueChange | null; settings: BriefSettings;
  method: string; behaviour_note: string; disclaimer: string;
  history: { id: number; kind: string; at: string; message: string }[];
};

export type Contributor = { name: string; inr: number; return_pct: number };
export type HeldSignal = {
  asset: string; instrument: string; name: string; action: string; score: number; probability: number | null;
  validation: string; n: number; weight_pct: number; method: string;
};
export type Digest = {
  day: string; window_days: number; value: ValueChange | null;
  contributors: { from: string; to: string; top: Contributor[]; bottom: Contributor[] } | null;
  signals: HeldSignal[]; benchmark: string; method: string; behaviour_note: string; disclaimer: string;
};

export type CalendarView = {
  from: string; days: number; items: BriefEvent[]; tax: TaxItem[]; advance_tax: AdvanceTax | null; long_term: LtLot[];
  sip: Sip[]; events_read: string | null; bse_only: string[];
};

export type Strip = {
  has_portfolio: boolean; value?: number | null; as_of?: string | null; complete?: boolean; holdings?: number;
  week?: ValueChange | null; ltcg_headroom?: number; ltcg_limit?: number;
  top_alert?: { message: string; at: string; level: string } | null;
  net_worth?: { net_worth: number; liabilities: number; assets: number; date: string } | null; unpriced?: number; daily_pass?: string | null; note?: string;
};

export const inr = (x: number | null | undefined, digits = 0) =>
  x == null ? "—" : `${x < 0 ? "−" : ""}₹${Math.abs(x).toLocaleString("en-IN", { maximumFractionDigits: digits })}`;

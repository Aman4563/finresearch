// Shapes of /api/brief, /api/brief/digest, /api/brief/calendar and /api/dashboard/portfolio (finresearch.api.brief).

export type BriefEvent = { day: string; kind: string; title: string; path?: string; verified?: boolean; note?: string };
export type FiredAlert = { id: number; kind: string; level: string; message: string; at: string; path: string };
export type SignalChange = { instrument: string; name: string; from: string; to: string; informational?: boolean; label?: string | null };
export type Sip = {
  holding_id: number; name: string; account: string; amount: number; day_of_month: number; instalments: number;
  last: string; next_expected: string; days_since: number; status: "on track" | "missed" | "stopped";
};
export type LtLot = {
  holding_id: number; name: string; account: string; acquired: string; quantity: number; lt_date: string; days: number;
  gain: number; tax_now: number; tax_later: number; saved: number; older_lots: number; price: number;
  /** older lots without a date or cost: FIFO sells them first and their tax is unknown */
  older_unknown?: number; estimate?: boolean; rules_verified?: boolean; rules_note?: string | null;
};
export type ElssUnlock = {
  holding_id: number; name: string; account: string; day: string; days: number; units: number; value: number | null; verified: boolean;
};
export type TaxItem = { day: string; kind: string; title: string; verified: boolean; note: string };
export type AdvanceTax = {
  fy: number; capital_gains_tax: number; dividends: number; dividend_tax: number; total: number; threshold: number;
  below_threshold: boolean; complete?: boolean; unclassified?: { count: number; gain: number | null; detail: string };
  rules_verified?: boolean; rules_note?: string | null; next: { due: string; cumulative_pct: number; amount: number; days: number } | null;
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
  long_term: LtLot[]; tax_calendar: TaxItem[]; advance_tax: AdvanceTax | null; elss_unlocks?: ElssUnlock[];
  health: { level: "warn" | "info"; text: string }[]; performance: ValueChange | null; performance_why?: string | null; settings: BriefSettings;
  method: string; behaviour_note: string; disclaimer: string;
  history: { id: number; kind: string; at: string; message: string }[];
};

export type Contributor = { name: string; inr: number; return_pct: number };
export type HeldSignal = {
  asset: string; instrument: string; name: string; action: string; score: number; probability: number | null;
  validation: string; n: number; weight_pct: number; method: string;
};
export type Digest = {
  day: string; window_days: number; value: ValueChange | null; value_why?: string | null;
  contributors: { from: string; to: string; top: Contributor[]; bottom: Contributor[] } | null;
  signals: HeldSignal[]; benchmark: string; method: string; behaviour_note: string; disclaimer: string;
  elss_unlocks?: ElssUnlock[];
};

export type CalendarView = {
  from: string; days: number; items: BriefEvent[]; tax: TaxItem[]; advance_tax: AdvanceTax | null; long_term: LtLot[];
  sip: Sip[]; events_read: string | null; bse_only: string[]; elss_unlocks?: ElssUnlock[];
};

export type Strip = {
  has_portfolio: boolean; value?: number | null; as_of?: string | null; complete?: boolean; holdings?: number;
  /** why `week` is null ("unavailable: the value history is out of date ..."), #264 */
  week?: ValueChange | null; week_why?: string | null; ltcg_headroom?: number | null; ltcg_limit?: number;
  top_alert?: { message: string; at: string; level: string } | null;
  net_worth?: { net_worth: number; liabilities: number; assets: number; date: string } | null; unpriced?: number; daily_pass?: string | null; note?: string;
};

export const inr = (x: number | null | undefined, digits = 0) =>
  x == null ? "—" : `${x < 0 ? "−" : ""}₹${Math.abs(x).toLocaleString("en-IN", { maximumFractionDigits: digits })}`;

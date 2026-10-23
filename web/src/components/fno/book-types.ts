// Shapes of the F&O book API (#308): finresearch.api.fno_book. Nothing here places an order.

import type { RateInfo } from "./model";

export type Greeks = { delta: number; gamma: number; vega: number; theta: number };

export type PositionLeg = {
  symbol: string; product: string | null; quantity: number | null; qty: number | null; direction_known: boolean;
  note: string | null; avg_price: number | null; realised_pnl: number | null; contract_known: boolean;
  underlying: string | null; expiry: string | null; strike: number | null; right: "call" | "put" | "future" | null;
  lot_size: number | null; ltp: number | null; unrealised_pnl: number | null; iv?: number | null; greeks: Greeks | null;
};

export type PositionGroup = {
  key: string; underlying: string | null; expiry: string | null; days_to_expiry: number | null; spot: number | null;
  chain_source: string | null; legs: PositionLeg[]; unrealised_pnl: number | null; net_greeks: Greeks | null;
  max_loss: number | null; max_loss_note: string | null;
  /** #366/#385: the rate the group's greeks used (null when its expiry is unknown or past) */
  rate?: RateInfo | null;
};

export type PositionsView = {
  status: "ok" | "not_connected" | "not_read"; read_at: string | null; groups: PositionGroup[];
  ltp_source: string | null; as_of: string; notes: string[];
};

export type PaperLegRow = {
  id: number; right: "call" | "put" | "future"; strike: number; side: "buy" | "sell"; lots: number; qty: number;
  ref_price: number; entry_price: number; trading_symbol: string | null; last_price: number | null;
  last_price_at: string | null; exit_price: number | null;
};

export type PaperTrade = {
  id: number; symbol: string; expiry: string; label: string; status: "open" | "awaiting_settlement" | "closed";
  opened_at: string; days_to_expiry: number; lot_size: number; slippage_pct: number; brokerage_per_order: number;
  spot_at_entry: number | null; price_source: string; net_premium: number; entry_costs: number;
  mtm_gross: number | null; mtm_at: string | null; mtm_source: string | null; mtm_net_if_closed: number | null;
  closed_at: string | null; close_reason: "square_off" | "expiry" | null; exit_costs: number | null;
  exit_detail: Record<string, unknown>; settlement_price: number | null; settlement_source: string | null;
  gross_pnl: number | null; net_pnl: number | null; note: string | null; legs: PaperLegRow[];
};

export type ExitRule = {
  id: number; paper_id: number | null; position_key: string | null; kind: ExitKind; threshold: number;
  status: "armed" | "fired" | "off"; phrase: string; last_value: number | null; note: string | null;
  checked_at: string | null; fired_at: string | null; alert_id: number | null;
};
export type ExitKind = "profit" | "stop" | "dte" | "delta" | "event";

export type PaperBook = {
  trades: PaperTrade[];
  counts: { open: number; awaiting_settlement: number; closed: number; all: number };
  results: {
    closed: number; wins: number; losses: number; win_rate: number | null; expectancy: number | null;
    avg_win: number | null; avg_loss: number | null; total_net: number; max_drawdown: number;
    curve: { closed_at: string; id: number; cumulative: number }[];
  };
  method: string; rules: ExitRule[]; default_slippage_pct: number; default_slippage_source?: string;
};

export type EventStripData = {
  symbol: string; today: string;
  items: { day: string; kind: "results" | "rbi" | "macro" | "expiry"; title: string; source: string }[];
  results: { status: string; next?: string | null; note?: string | null; source?: string };
  rbi: { status: "ok" | "no_source"; note?: string; source?: string };
};

export type MarginEstimate = {
  status: "approximate"; label: string; scan_risk: number; exposure: number; premium_paid: number; total: number;
  worst_move_pct: number; kind: "index" | "stock"; assumptions: Record<string, { value: number; status: string }>;
  note: string; groww_calculator: { available: boolean; reason: string };
};

export const EXIT_KINDS: { key: ExitKind; label: string; unit: string; help: string; preset: number }[] = [
  { key: "profit", label: "Profit target", unit: "% of premium", preset: 50, help: "Gross P&L reaches this % of the entry premium (debit paid or credit received)." },
  { key: "stop", label: "Stop", unit: "% of premium", preset: 100, help: "Gross P&L falls to minus this % of the entry premium." },
  { key: "dte", label: "Days to expiry", unit: "days", preset: 7, help: "Calendar days to expiry fall to this number." },
  { key: "delta", label: "Short-strike delta", unit: "|delta|", preset: 0.3, help: "A short option's |delta| reaches this level (Black-Scholes at the chain's IV)." },
  { key: "event", label: "Event before expiry", unit: "days ahead", preset: 3, help: "A results date (or RBI policy date, when the app has a source) falls within this many days, before expiry." },
];

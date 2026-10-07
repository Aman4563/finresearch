"use client";

// A compact IPO signal on an issue card: P(listing gain), how many past issues it rests on, and the expected value of
// applying for one lot. "Details" opens the full SignalView (factors, method, validation, caveats).

import { Activity } from "lucide-react";
import { useState } from "react";

import { ReliabilityLine, ShadowLine, type Signal, SignalView, pctText } from "@/components/signal";
import { Badge, InfoTip, Modal, Skeleton, cx } from "@/components/ui";
import { useApi } from "@/lib/api";

const LABEL: Record<string, string> = { APPLY: "Apply", SKIP: "Skip", NO_SIGNAL: "No signal", SELL_AT_LISTING: "Sell at listing", HOLD_AFTER_LISTING: "Hold" };

const inr0 = (v: number) => `${v < 0 ? "−" : ""}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;

export function IpoSignalLine({ symbol, series, company, closed }: { symbol: string; series: string | null; company: string; closed?: boolean }) {
  const q = series ? `?series=${encodeURIComponent(series)}` : "";
  const { data: s, error } = useApi<Signal>(`/api/signals/ipo/${encodeURIComponent(symbol)}${q}`, undefined, "POST"); // a logged view (#247)
  const [open, setOpen] = useState(false);
  if (error) return null; // the card still shows the exchange data; the signal is optional
  if (!s) return <Skeleton className="mt-3 h-12 rounded-lg" />;
  // the lottery unit is the category's minimum application (1 lot retail, 14+ lots sNII): show EV per application
  const minLots = (s.sizing?.min_lots as number | undefined) ?? 1;
  const ev = (minLots > 1 ? s.sizing?.ev_per_application : s.sizing?.ev_per_lot) as number | null | undefined;
  const evLabel = minLots > 1 ? `EV per ${minLots}-lot application` : "EV per lot";
  const reason = s.probability == null ? s.caveats[0] : null; // no call, a rule-only skip, or after listing
  return (
    <div className="mt-3 rounded-lg bg-background-subtle px-3 py-2">
      <div className="flex items-center gap-2 text-[11px] text-muted">
        <Activity className="size-3.5" /> Signal
        <InfoTip>Share of comparable past issues that opened above the issue price, with its 95% range. Built on FINAL subscription, so it is optimistic before the close. Personal research, not advice.</InfoTip>
        {closed && !s.action.includes("LISTING")
          ? <Badge tone="neutral">Closed · awaiting listing</Badge>
          : <Badge tone="neutral">{/* a rule-based view, not advice: no green "go" / red "stop" (#241) */}Rule-based view: {LABEL[s.action] ?? s.action}</Badge>}
        <button type="button" onClick={() => setOpen(true)} className="ml-auto font-medium text-brand hover:underline">Details</button>
      </div>
      {reason ? (
        <p className="mt-1 line-clamp-2 text-xs text-muted" title={reason}>{reason}</p>
      ) : (
        <dl className="mt-1 grid grid-cols-3 gap-2 text-xs">
          <div>
            <dt className="text-[10px] text-muted">{s.action.includes("LISTING") ? "Evidence" : "P(listing gain)"}</dt>
            <dd className="num font-medium">{s.probability == null ? "—" : pctText(s.probability)}
              {s.probability_interval && <span className="ml-1 text-[10px] font-normal text-muted">{pctText(s.probability_interval[0])}–{pctText(s.probability_interval[1])}</span>}
            </dd>
          </div>
          <div>
            <dt className="text-[10px] text-muted">Past issues</dt>
            <dd className="num font-medium">{s.base_rate?.n ?? s.validation.n ?? "—"}</dd>
          </div>
          <div title={s.sizing?.p_allot_basis as string | undefined}>
            <dt className="text-[10px] text-muted">{evLabel}</dt>
            <dd className={cx("num font-medium", ev != null && (ev > 0 ? "text-gain" : ev < 0 ? "text-loss" : ""))}>{ev == null ? "—" : inr0(ev)}</dd>
          </div>
        </dl>
      )}
      {!reason && s.reliability && s.probability != null && <ReliabilityLine r={s.reliability} className="mt-1" />}
      {!reason && s.shadow && <ShadowLine sh={s.shadow} className="mt-1" />}
      <Modal open={open} onClose={() => setOpen(false)} title={`${company} · signal`} wide>
        <div className="max-h-[70vh] space-y-4 overflow-y-auto p-4">
          {s.sizing?.lot_cost != null && (
            <dl className="grid grid-cols-2 gap-3 rounded-lg border border-border p-3 text-xs sm:grid-cols-4">
              <div><dt className="text-muted">Lot cost</dt><dd className="num font-medium">{inr0(s.sizing.lot_cost as number)}</dd></div>
              <div title={s.sizing.p_allot_basis as string}><dt className="text-muted">P(allotment) ≥</dt><dd className="num font-medium">{pctText(s.sizing.p_allot as number | null, 1)}</dd></div>
              <div><dt className="text-muted">Mean open return</dt><dd className="num font-medium">{pctText(s.sizing.expected_return_mean as number | null, 1)}</dd></div>
              <div><dt className="text-muted">{minLots > 1 ? `EV of one ${minLots}-lot application` : "EV of one lot"}</dt><dd className="num font-medium">{ev == null ? "—" : inr0(ev)}</dd></div>
              <p className="col-span-full text-[11px] text-muted">EV ≈ P(allotment) × {minLots > 1 ? `${minLots} lots` : "lot cost"}{minLots > 1 ? " × lot cost" : ""} × mean past open return. Oversubscribed books are lotteries over minimum applications: a bigger bid gives the same odds. A book below 1x allots every valid bid in full.</p>
            </dl>
          )}
          <SignalView s={s} />
        </div>
      </Modal>
    </div>
  );
}

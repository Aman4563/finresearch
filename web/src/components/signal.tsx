"use client";

// One buy/sell signal (finresearch.signals.base.Signal), the same way for every asset class: the action, the score,
// the probability of a stated event with its range, the factors that moved it, how the method was validated, and the
// caveats. A signal is a probability with a range, never a command.

import { Activity, FlaskConical, Info, ShieldAlert, TrendingDown, TrendingUp } from "lucide-react";
import { useState } from "react";

import { Badge, Card, InfoTip, SkeletonRows, cx } from "@/components/ui";
import { useApi } from "@/lib/api";

export type SignalFactor = { name: string; value: number | string | null; contribution: number; explanation: string; source: string | null; unit: string | null };
export type SignalValidation = { status: "backtested" | "base_rate" | "rule_based" | "uncalibrated" | "shadow"; n: number; metrics: Record<string, number>; description: string };
export type Signal = {
  asset: "ipo" | "stock" | "fund" | "bond" | "fno";
  instrument: string;
  name: string | null;
  action: string;
  score: number;
  event: string;
  horizon: string;
  method: string;
  validation: SignalValidation;
  probability: number | null;
  probability_interval: [number, number] | null;
  expected_return: Record<string, number> | null;
  base_rate: { n: number; p: number; ci?: [number, number]; description?: string } | null;
  factors: SignalFactor[];
  caveats: string[];
  sizing: Record<string, unknown> | null;
  sources: string[];
  as_of: string | null;
  disclaimer: string;
  shadow?: SignalShadow | null;
};

const POSITIVE = new Set(["APPLY", "BUY", "ACCUMULATE", "ENTER", "HOLD_AFTER_LISTING"]);
const NEGATIVE = new Set(["SKIP", "SELL", "REDUCE", "AVOID", "EXIT", "SELL_AT_LISTING"]);
const LABEL: Record<string, string> = {
  SELL_AT_LISTING: "Sell at listing", HOLD_AFTER_LISTING: "Hold after listing", NO_SIGNAL: "No signal", WAIT: "Wait",
};
const VALIDATION: Record<SignalValidation["status"], { label: string; tone: "gain" | "info" | "warn" | "neutral"; help: string }> = {
  backtested: { label: "Backtested", tone: "gain", help: "Checked out of sample on past data; the numbers below are its track record." },
  base_rate: { label: "Historical base rate", tone: "info", help: "How often this happened in comparable past cases; no fitted model." },
  rule_based: { label: "Rule-based", tone: "warn", help: "Fixed rules drawn from published research, not yet validated on this app's own outcomes." },
  uncalibrated: { label: "Uncalibrated", tone: "neutral", help: "Too few resolved cases to measure accuracy yet. Treat the probability as a rough guide." },
  shadow: { label: "Shadow test", tone: "neutral", help: "Shadow test: logged for out-of-sample scoring, not used for the call." },
};

/** An alternative method computed beside the call and logged as a shadow test (never used for the action). */
export type SignalShadow = {
  method: string; probability: number; probability_interval: [number, number] | null; status: "shadow";
  description: string; backtest?: string; caveats?: string[];
};

/** "Experimental comparison": the shadow method's P and range, with what the shadow test is and when it could switch. */
export function ShadowLine({ sh, className }: { sh: SignalShadow; className?: string }) {
  return (
    <p className={cx("flex flex-wrap items-center gap-x-1.5 text-[11px] text-muted", className)}>
      <FlaskConical className="size-3 shrink-0" />
      <span>Experimental comparison:</span>
      <span className="num text-foreground/80">{pctText(sh.probability)}</span>
      {sh.probability_interval && <span className="num">({pctText(sh.probability_interval[0])}–{pctText(sh.probability_interval[1])})</span>}
      <InfoTip>{sh.description} {sh.backtest ? `Backtest: ${sh.backtest}.` : ""} Method: {sh.method}.</InfoTip>
    </p>
  );
}

export const pctText = (p: number | null | undefined, d = 0) => (p == null ? "—" : `${(p * 100).toFixed(d)}%`);

function ScoreBar({ score }: { score: number }) {
  const left = score < 0 ? 50 + score / 2 : 50;
  const width = Math.abs(score) / 2;
  return (
    <div className="relative h-2 rounded-full bg-background-subtle" role="img" aria-label={`score ${score.toFixed(0)} of ±100`}>
      <span className="absolute inset-y-0 left-1/2 w-px bg-border-strong" />
      <span className={cx("absolute inset-y-0 rounded-full transition-all duration-700", score >= 0 ? "bg-gain" : "bg-loss")} style={{ left: `${left}%`, width: `${width}%` }} />
    </div>
  );
}

export function SignalView({ s, compact }: { s: Signal; compact?: boolean }) {
  const [open, setOpen] = useState(!compact);
  const tone = POSITIVE.has(s.action) ? "gain" : NEGATIVE.has(s.action) ? "loss" : "neutral";
  const v = VALIDATION[s.validation.status];
  const maxAbs = Math.max(1, ...s.factors.map((f) => Math.abs(f.contribution)));
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className={cx("inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-lg font-semibold tracking-tight ring-1 ring-inset",
          tone === "gain" ? "bg-gain-soft text-gain ring-gain/30" : tone === "loss" ? "bg-loss-soft text-loss ring-loss/30" : "bg-background-subtle text-foreground ring-border")}>
          {tone === "gain" ? <TrendingUp className="size-4" /> : tone === "loss" ? <TrendingDown className="size-4" /> : <Activity className="size-4" />}
          {LABEL[s.action] ?? s.action.replaceAll("_", " ")}
        </span>
        <Badge tone={v.tone}>{v.label}{s.validation.n ? ` · n=${s.validation.n}` : ""}</Badge>
        <InfoTip>{v.help} {s.validation.description}</InfoTip>
        <span className="ml-auto text-xs text-muted">{s.horizon}</span>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="rounded-lg bg-background-subtle p-3">
          <p className="text-[11px] text-muted">Probability · {s.event}</p>
          <p className="num mt-1 text-2xl font-semibold">{pctText(s.probability)}</p>
          {s.probability_interval && <p className="num text-xs text-muted">range {pctText(s.probability_interval[0])}–{pctText(s.probability_interval[1])}</p>}
        </div>
        <div className="rounded-lg bg-background-subtle p-3">
          <p className="flex items-center gap-1 text-[11px] text-muted">Score <InfoTip>−100 is strongly negative, +100 strongly positive; the factors below add up to it.</InfoTip></p>
          <p className={cx("num mt-1 text-2xl font-semibold", s.score > 0 ? "text-gain" : s.score < 0 ? "text-loss" : "")}>{s.score > 0 ? "+" : ""}{s.score.toFixed(0)}</p>
          <ScoreBar score={s.score} />
        </div>
      </div>

      {s.action === "NO_SIGNAL" && s.asset !== "fund" && s.caveats[0] && (
        // the providers put the reason for "no signal" first (stock, IPO, F&O, bond): never hide it behind "Why?"
        <p className="rounded-lg bg-background-subtle px-3 py-2 text-xs text-foreground/90"><span className="font-medium">Why no signal: </span>{s.caveats[0]}</p>
      )}
      {s.shadow && <ShadowLine sh={s.shadow} />}
      {s.expected_return && (
        <p className="text-xs text-muted">
          Return range:{" "}
          {Object.entries(s.expected_return).map(([k, x]) => (
            <span key={k} className="num mr-2 text-foreground">{k} {x >= 0 ? "+" : ""}{(x * 100).toFixed(1)}%</span>
          ))}
        </p>
      )}
      {s.base_rate && (
        <p className="text-xs text-muted">
          Base rate: <span className="num text-foreground">{pctText(s.base_rate.p)}</span> of {s.base_rate.n} comparable cases
          {s.base_rate.ci && <> (95% CI {pctText(s.base_rate.ci[0])}–{pctText(s.base_rate.ci[1])})</>}
          {s.base_rate.description && <> · {s.base_rate.description}</>}
        </p>
      )}

      {compact && (
        <button type="button" onClick={() => setOpen((o) => !o)} className="text-xs font-medium text-brand">
          {open ? "Hide the reasoning" : `Why? ${s.factors.length} factors`}
        </button>
      )}
      {open && (
        <>
          {s.factors.length > 0 && (
            <ul className="space-y-2">
              {s.factors.map((f) => (
                <li key={f.name} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 text-sm">
                  <span className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate font-medium">{f.name}</span>
                    <InfoTip>{f.explanation}{f.source && <> Source: {f.source}</>}</InfoTip>
                  </span>
                  <span className="num text-xs text-muted">{f.value == null ? "not computed" : typeof f.value === "number" ? f.value.toLocaleString("en-IN", { maximumFractionDigits: 2 }) : f.value}{f.unit && f.value != null ? ` ${f.unit}` : ""}</span>
                  {f.value == null && <span className="col-span-2 text-xs text-muted">{f.explanation}</span>}
                  <span className="col-span-2 flex h-1.5 overflow-hidden rounded-full bg-background-subtle">
                    <span className="w-1/2">{f.contribution < 0 && <span className="ml-auto block h-full rounded-full bg-loss" style={{ width: `${(Math.abs(f.contribution) / maxAbs) * 100}%` }} />}</span>
                    <span className="w-1/2">{f.contribution > 0 && <span className="block h-full rounded-full bg-gain" style={{ width: `${(f.contribution / maxAbs) * 100}%` }} />}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
          <p className="flex items-start gap-1.5 text-xs text-muted"><FlaskConical className="mt-0.5 size-3.5 shrink-0" />Method: {s.method}</p>
          {s.caveats.length > 0 && (
            <ul className="space-y-1 rounded-lg bg-warn-soft px-3 py-2 text-xs text-foreground/90">
              {s.caveats.map((c) => <li key={c} className="flex gap-1.5"><ShieldAlert className="mt-0.5 size-3.5 shrink-0 text-warn" />{c}</li>)}
            </ul>
          )}
        </>
      )}
      <p className="flex items-start gap-1.5 text-[11px] text-muted"><Info className="mt-0.5 size-3 shrink-0" />{s.disclaimer}{s.as_of && <> · as of {new Date(s.as_of).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kolkata" })}</>}</p>
    </div>
  );
}

/** Fetches and shows the signal for one instrument; `query` goes to the provider as context. */
export function SignalCard({ asset, instrument, query, title = "Signal", compact = true }: {
  asset: Signal["asset"]; instrument: string; query?: Record<string, string>; title?: string; compact?: boolean;
}) {
  const qs = query && Object.keys(query).length ? `?${new URLSearchParams(query)}` : "";
  const { data, error } = useApi<Signal>(`/api/signals/${asset}/${encodeURIComponent(instrument)}${qs}`);
  return (
    <Card title={title} icon={<Activity className="size-4" />}
      help="A personal, research-backed estimate: the probability of a stated event with its range, the factors behind it and how it was validated. Not investment advice.">
      {error ? <p className="text-sm text-muted">No signal: {error}</p> : !data ? <SkeletonRows rows={4} /> : <SignalView s={data} compact={compact} />}
    </Card>
  );
}

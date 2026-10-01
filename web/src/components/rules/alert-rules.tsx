"use client";

// Alert rules for every asset kind (finresearch.alerts): notify-only, evaluated by the monitor, delivered to the phone.
import {
  BellPlus, BellRing, Check, Clock3, Loader2, Play, Plus, Radio, Smartphone, Sparkles, Trash2,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { Labelled, Switch } from "@/components/profile/common";
import { Badge, Button, Card, cx, EmptyState, Field, InfoTip, inputClass } from "@/components/ui";
import {
  type AlertKind, type AlertRegistry, type AlertRule, type AlertRuleState, api, type Channel, type MetricSpec,
  type NotificationSettings, type Priority, type RuleTemplate, when,
} from "@/lib/api";

export const KIND_LABEL: Record<AlertKind, string> = {
  ipo: "IPOs", stock: "Stocks", fund: "Funds", bond: "Bonds", fno: "F&O", portfolio: "Portfolio",
};
const ALL_LABEL: Record<AlertKind, string> = {
  ipo: "every watched IPO", stock: "every watched stock", fund: "every fund added to the app", bond: "every bond added to the app",
  fno: "", portfolio: "",
};
const ANY: Record<AlertKind, string> = {
  ipo: "Any watched IPO", stock: "Any watched stock", fund: "Any tracked fund", bond: "Any tracked bond", fno: "Underlying", portfolio: "Portfolio",
};
const PLACEHOLDER: Record<AlertKind, string> = {
  ipo: "e.g. ORIENTCABL", stock: "e.g. INFY or BSE:500209", fund: "AMFI scheme code, e.g. 120503", bond: "ISIN, e.g. INE001A07TP5",
  fno: "e.g. NIFTY", portfolio: "",
};
const OP_LABEL: Record<string, string> = { "<": "is below", "<=": "is at most", ">": "is above", ">=": "is at least", "==": "equals", "!=": "is not" };
const PRIORITIES: { value: Priority; label: string; hint: string }[] = [
  { value: "min", label: "Min", hint: "silent, below the fold" },
  { value: "low", label: "Low", hint: "silent" },
  { value: "default", label: "Default", hint: "sound" },
  { value: "high", label: "High", hint: "pops up" },
  { value: "urgent", label: "Urgent", hint: "pops up, ignores quiet hours" },
];
const CHANNEL_LABEL: Record<Channel, string> = { ntfy: "ntfy", telegram: "Telegram", macos: "Mac" };
const UNIT_SUFFIX: Record<string, string> = {
  inr: "₹", pct: "%", pp: "pp", times: "x", days: "days", count: "flags", flag: "", score: "score", points: "pts",
};

export function fmtUnit(unit: string, v: string | number | null | undefined) {
  const n = Number(v);
  if (v == null || String(v).trim() === "" || !Number.isFinite(n)) return String(v ?? "…") || "…";
  const trim = (x: number, d = 2) => String(+x.toFixed(d));
  switch (unit) {
    case "inr": return `${n < 0 ? "-" : ""}₹${Math.abs(n).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
    case "pct": return `${trim(n)}%`;
    case "pp": return `${n > 0 ? "+" : ""}${trim(n)} pp`;
    case "times": return `${trim(n)}x`;
    case "days": return `${n} ${n === 1 ? "day" : "days"}`;
    default: return trim(n, 4);
  }
}

export function alertSentence(r: Pick<AlertRule, "kind" | "metric" | "op" | "value" | "instrument">, m?: MetricSpec) {
  const who = r.kind === "portfolio" ? "Portfolio" : r.instrument || ANY[r.kind];
  if (!m) return `${who}: ${r.metric}`;
  if (m.event) return `${who}: ${m.event_text}`;
  return `${who}: ${m.phrase} ${OP_LABEL[r.op] ?? r.op} ${fmtUnit(m.unit, r.value)}`;
}

// the metrics signals.fund_rank ranks a fund on (category_rank_drop's optional `basis` param)
const RANK_BASES: [string, string][] = [
  ["cagr_3y", "3-year CAGR"], ["cagr_1y", "1-year return"], ["cagr_5y", "5-year CAGR"],
  ["consistency_3y", "Rolling 1-year consistency"], ["sortino_3y", "Sortino ratio"], ["max_drawdown_3y", "Max drawdown"],
  ["ter", "Expense ratio"],
];
const isNumber = (v: string) => /^-?\d+(\.\d+)?$/.test(v.trim());

/** Problems per alert-rule id-index (empty when every alert rule can be saved). */
export function validateAlertRules(rules: AlertRule[], metrics: MetricSpec[]): Record<number, string[]> {
  const out: Record<number, string[]> = {};
  const seen = new Set<string>();
  rules.forEach((r, i) => {
    const errs: string[] = [];
    const m = metrics.find((x) => x.kind === r.kind && x.key === r.metric);
    const id = r.id.trim();
    if (!id) errs.push("Give the rule an id.");
    else if (id.length > 40) errs.push("The id can be at most 40 characters.");
    else if (seen.has(id)) errs.push(`The id “${id}” is used twice.`);
    seen.add(id);
    if (!m) errs.push("Pick a metric.");
    if (m && !m.event && !isNumber(String(r.value))) errs.push("The value must be a number.");
    if (m && !m.allows_all && r.kind !== "portfolio" && !r.instrument?.trim()) errs.push("Pick an instrument.");
    for (const p of m?.params ?? []) if (!r.params?.[p]?.trim()) errs.push(`Fill in ${p}.`);
    if (!isNumber(String(r.cooldown_h)) || Number(r.cooldown_h) < 0 || Number(r.cooldown_h) > 720) errs.push("Cooldown is 0 to 720 hours.");
    if (errs.length) out[i] = errs;
  });
  return out;
}

export function uniqueAlertId(rules: { id: string }[], base: string) {
  const ids = new Set(rules.map((r) => r.id));
  if (!ids.has(base)) return base;
  let k = 2;
  while (ids.has(`${base}-${k}`)) k += 1;
  return `${base}-${k}`;
}

export function newAlertRule(kind: AlertKind, m: MetricSpec, existing: AlertRule[], from?: RuleTemplate, instrument?: string | null): AlertRule {
  return {
    id: uniqueAlertId(existing, from?.id ?? `${kind}-${m.key.replaceAll("_", "-")}`.slice(0, 36)),
    kind, metric: m.key, op: from?.op ?? m.default_op, value: from?.value ?? m.default_value,
    instrument: kind === "portfolio" ? "PORTFOLIO" : instrument ?? null, params: { ...(from?.params ?? {}) },
    channels: [], priority: from?.priority ?? "default", cooldown_h: "24", enabled: true, description: "",
  };
}

export function AlertRulesPanel({ kind, rules, allRules, registry, states, suggestions, notifications, savedIds, problems, onChange, onAdd }: {
  kind: AlertKind;
  rules: { rule: AlertRule; index: number }[];
  allRules: AlertRule[];
  registry: AlertRegistry;
  states: AlertRuleState[];
  suggestions: string[];
  notifications: NotificationSettings | null;
  /** ids of rules saved unchanged (a "Check now" only makes sense for those) */
  savedIds: Set<string>;
  problems: Record<number, string[]>;
  onChange: (index: number, r: AlertRule | null) => void;
  onAdd: (r: AlertRule) => void;
}) {
  const metrics = registry.metrics.filter((m) => m.kind === kind);
  const templates = registry.templates.filter((t) => t.kind === kind);
  const has = (t: RuleTemplate) => allRules.some((r) => r.kind === kind && r.metric === t.metric && r.op === t.op && Number(r.value) === Number(t.value));
  const ready = (notifications?.channels ?? []).filter((c) => notifications?.[c].status === "ready");
  const listId = `inst-${kind}`;

  return (
    <div className="grid grid-cols-[minmax(0,1fr)] gap-6 xl:grid-cols-[minmax(0,1fr)_340px]">
      <datalist id={listId}>{suggestions.map((s) => <option key={s} value={s} />)}</datalist>
      <div className="space-y-4">
        {!rules.length ? (
          <EmptyState icon={<BellPlus className="size-5" />} title={`No ${KIND_LABEL[kind]} alerts yet`}
            action={metrics[0] && (
              <Button icon={<Plus className="size-3.5" />} onClick={() => onAdd(newAlertRule(kind, metrics[0], allRules))}>Add an alert</Button>
            )}>
            {kind === "portfolio"
              ? "Portfolio alerts start working once your holdings are imported on the portfolio page. Until then they show as unknown and never fire."
              : "Pick a template on the right or add your own. An alert fires once when its condition becomes true, and again only after it clears and the cooldown passes."}
          </EmptyState>
        ) : (
          <div className="grid grid-cols-[minmax(0,1fr)] gap-4 2xl:grid-cols-2">
            {rules.map(({ rule, index }) => (
              <AlertRuleCard key={index} rule={rule} metrics={metrics} listId={listId} errors={problems[index]}
                states={states.filter((s) => s.rule_id === rule.id)} readyChannels={ready} saved={savedIds.has(rule.id)}
                onChange={(r) => onChange(index, r)} onRemove={() => onChange(index, null)} />
            ))}
          </div>
        )}
      </div>

      <div className="space-y-4">
        <Card icon={<Sparkles className="size-4" />} title="Templates" subtitle="One click to add; set the instrument and numbers after.">
          <ul className="space-y-2">
            {templates.map((t) => {
              const m = metrics.find((x) => x.key === t.metric);
              if (!m) return null;
              const added = has(t);
              return (
                <li key={t.id}>
                  <button type="button" disabled={added} onClick={() => onAdd(newAlertRule(kind, m, allRules, t))}
                    className={cx("group flex w-full items-start gap-3 rounded-lg border p-2.5 text-left transition",
                      added ? "border-border opacity-60" : "border-border hover:border-brand/50 hover:bg-brand-soft/40")}>
                    <span className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-md bg-info-soft text-info"><BellRing className="size-3.5" /></span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-medium">{t.title}</span>
                      <span className="block text-xs text-muted">{t.why}</span>
                      <span className="mt-1 block text-[11px] font-medium text-brand">{alertSentence({ ...t, kind, instrument: null }, m)}</span>
                    </span>
                    <span className="mt-0.5 shrink-0 text-muted transition group-hover:text-brand">
                      {added ? <Check className="size-4 text-gain" /> : <Plus className="size-4" />}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </Card>
        <Card icon={<Smartphone className="size-4" />} title="Delivery" subtitle="Where fired alerts go besides the bell.">
          {ready.length ? (
            <p className="text-sm text-muted">Ready: <span className="font-medium text-foreground">{ready.map((c) => CHANNEL_LABEL[c]).join(", ")}</span>. Pick channels per rule. Change them on your{" "}
              <Link href="/profile#notifications" className="text-brand underline-offset-2 hover:underline">profile</Link>.</p>
          ) : (
            <p className="text-sm text-muted">No phone channel is set up yet, so alerts stay in the app. Set up ntfy or Telegram on your{" "}
              <Link href="/profile#notifications" className="text-brand underline-offset-2 hover:underline">profile</Link>.</p>
          )}
        </Card>
      </div>
    </div>
  );
}

function StatusLine({ st, unit }: { st: AlertRuleState; unit: string }) {
  const tone = st.status === "fired" ? "warn" : st.status === "clear" ? "gain" : "neutral";
  const label = st.status === "fired" ? "firing" : st.status === "clear" ? "clear" : st.status === "new" ? "not checked" : "unknown";
  return (
    <li className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
      <Badge tone={tone}>{label}</Badge>
      <span className="num font-medium">{st.instrument}</span>
      {st.value != null && <span className="num">{fmtUnit(unit, st.value)}</span>}
      {st.last_fired_at && <span className="text-muted">last alert {when(st.last_fired_at)}</span>}
      {st.checked_at && <span className="text-muted">checked {when(st.checked_at)}</span>}
      {st.note && <span className="w-full text-muted">{st.note}</span>}
    </li>
  );
}

function AlertRuleCard({ rule: r, metrics, listId, errors, states, readyChannels, saved, onChange, onRemove }: {
  rule: AlertRule; metrics: MetricSpec[]; listId: string; errors?: string[]; states: AlertRuleState[]; readyChannels: Channel[];
  saved: boolean; onChange: (r: AlertRule) => void; onRemove: () => void;
}) {
  const m = metrics.find((x) => x.key === r.metric);
  const set = (patch: Partial<AlertRule>) => onChange({ ...r, ...patch });
  const [checking, setChecking] = useState(false);
  const [checkMsg, setCheckMsg] = useState<string | null>(null);
  const all = !r.instrument && r.kind !== "portfolio";
  const checkNow = async () => {
    setChecking(true);
    try {
      const res = await api<{ checks: { instrument: string; result: string; value: string | null; note: string | null }[] }>(
        `/api/alert-rules/${encodeURIComponent(r.id)}/check`, { method: "POST" });
      setCheckMsg(res.checks.length
        ? res.checks.map((c) => `${c.instrument}: ${c.result.replace("_", " ")}${c.value != null && m ? ` (${fmtUnit(m.unit, c.value)})` : ""}${c.note ? ` — ${c.note}` : ""}`).join(" · ")
        : "Nothing to check: no instrument matches this rule yet.");
    } catch (e) {
      setCheckMsg((e as Error).message);
    } finally {
      setChecking(false);
    }
  };
  return (
    <section className={cx("rounded-xl border bg-card shadow-card transition duration-200 hover:border-border-strong",
      errors ? "border-loss/50" : "border-border", !r.enabled && "opacity-70")}>
      <div className="flex items-start gap-3 rounded-t-xl border-b border-border bg-info-soft/50 px-4 py-3">
        <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-lg bg-info text-white"><BellRing className="size-4" /></span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold leading-snug">{alertSentence(r, m)}</p>
          <p className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] text-muted">
            <Clock3 className="size-3" />{m?.cadence === "intraday" ? "every 15 min in market hours" : "daily, after the close"}
            {r.channels.length ? <><Radio className="ml-1 size-3" />{r.channels.map((c) => CHANNEL_LABEL[c]).join(", ")}</> : <span className="ml-1">· in-app only</span>}
          </p>
        </div>
        <button type="button" aria-label={`Delete alert ${r.id}`} onClick={onRemove}
          className="grid size-8 shrink-0 place-items-center rounded-lg text-muted transition hover:bg-loss-soft hover:text-loss">
          <Trash2 className="size-4" />
        </button>
      </div>

      <div className="space-y-3 p-4">
        <Labelled label={<span className="inline-flex items-center gap-1">Metric<InfoTip>{m?.description ?? "Pick what the alert watches."}</InfoTip></span>}>
          <select aria-label="Metric" className={cx(inputClass, "w-full")} value={r.metric}
            onChange={(e) => {
              const nm = metrics.find((x) => x.key === e.target.value);
              if (nm) set({ metric: nm.key, op: nm.default_op, value: nm.default_value, instrument: !nm.allows_all && !r.instrument && r.kind !== "portfolio" ? "" : r.instrument });
            }}>
            {metrics.map((x) => <option key={x.key} value={x.key}>{x.label}</option>)}
          </select>
          {m && <span className="block text-[11px] leading-snug text-muted">{m.description} <span className="text-muted/80">Source: {m.source}</span></span>}
        </Labelled>

        {r.kind !== "portfolio" && (
          <Labelled label="Applies to">
            <div className="space-y-2">
              {m?.allows_all !== false && (
                <Switch checked={all} onChange={(v) => set({ instrument: v ? null : "" })} label={`All: ${ALL_LABEL[r.kind]}`} />
              )}
              {!all && (
                <input aria-label="Instrument" list={listId} className={cx(inputClass, "num w-full uppercase")} placeholder={PLACEHOLDER[r.kind]}
                  value={r.instrument ?? ""} onChange={(e) => set({ instrument: e.target.value.toUpperCase() })} />
              )}
            </div>
          </Labelled>
        )}

        {m?.event ? (
          <p className="rounded-lg bg-background-subtle px-3 py-2 text-xs text-muted">Fires when it changes from one check to the next (the first check only records the current value).</p>
        ) : (
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-3">
            <Field label="Comparison">
              <select className={cx(inputClass, "w-full")} value={r.op} onChange={(e) => set({ op: e.target.value })}>
                {Object.entries(OP_LABEL).map(([v, l]) => <option key={v} value={v}>{l} ({v})</option>)}
              </select>
            </Field>
            <Field label="Value">
              <div className="relative">
                <input className={cx(inputClass, "num w-full pr-14", !isNumber(String(r.value)) && "border-loss")} inputMode="decimal"
                  value={r.value} aria-invalid={!isNumber(String(r.value))} onChange={(e) => set({ value: e.target.value.replace(/[,\s₹x%]/gi, "") })} />
                {m && <span className="pointer-events-none absolute inset-y-0 right-3 grid place-items-center text-[11px] text-muted">{UNIT_SUFFIX[m.unit] ?? m.unit}</span>}
              </div>
            </Field>
          </div>
        )}

        {m?.key === "category_rank_drop" && (
          <Field label="Rank on" hint="Which metric's category percentile to watch (the stored daily ranking; direct-growth plans, month-end NAVs).">
            <select className={cx(inputClass, "w-full")} value={r.params.basis ?? "cagr_3y"}
              onChange={(e) => set({ params: { ...r.params, basis: e.target.value } })}>
              {RANK_BASES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </Field>
        )}

        {m?.params.includes("legs") && (
          <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_150px]">
            <Field label="Legs you hold" hint="side:right:strike:lots:entry premium, comma-separated. e.g. buy:call:25000:1:120,sell:call:25200:1:55">
              <input className={cx(inputClass, "num w-full")} value={r.params.legs ?? ""} placeholder="buy:call:25000:1:120,sell:call:25200:1:55"
                onChange={(e) => set({ params: { ...r.params, legs: e.target.value } })} />
            </Field>
            <Field label="Expiry">
              <input type="date" className={cx(inputClass, "num w-full")} value={r.params.expiry ?? ""}
                onChange={(e) => set({ params: { ...r.params, expiry: e.target.value } })} />
            </Field>
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Priority">
            <select className={cx(inputClass, "w-full")} value={r.priority} onChange={(e) => set({ priority: e.target.value as Priority })}>
              {PRIORITIES.map((p) => <option key={p.value} value={p.value}>{p.label}: {p.hint}</option>)}
            </select>
          </Field>
          <Field label="Cooldown (hours)" hint="After it clears, it cannot alert again sooner than this.">
            <input className={cx(inputClass, "num w-full")} inputMode="decimal" value={r.cooldown_h} onChange={(e) => set({ cooldown_h: e.target.value })} />
          </Field>
        </div>

        <Labelled label="Send to" hint={readyChannels.length ? "Only channels set up on your profile are listed as ready." : undefined}>
          <div className="flex flex-wrap gap-2">
            {(["ntfy", "telegram", "macos"] as Channel[]).map((c) => {
              const on = r.channels.includes(c);
              const ready = readyChannels.includes(c);
              return (
                <button key={c} type="button" aria-pressed={on}
                  onClick={() => set({ channels: on ? r.channels.filter((x) => x !== c) : [...r.channels, c] })}
                  className={cx("inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs font-medium ring-1 ring-inset transition",
                    on ? "bg-brand-soft text-brand-strong ring-brand/40" : "text-muted ring-border hover:bg-card-hover")}>
                  {on ? <Check className="size-3.5" /> : <Plus className="size-3.5" />}{CHANNEL_LABEL[c]}
                  {!ready && <span className="text-[10px] font-normal text-warn">not set up</span>}
                </button>
              );
            })}
          </div>
        </Labelled>

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-3">
          <Switch checked={r.enabled} onChange={(v) => set({ enabled: v })} label="On" />
          <Button variant="secondary" disabled={!saved || checking} onClick={checkNow}
            title={saved ? "Evaluate now with live data (it alerts if the condition is true)" : "Save first"}
            icon={checking ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}>
            Check now
          </Button>
        </div>
        {checkMsg && <p className="rounded-lg bg-background-subtle px-3 py-2 text-xs break-words">{checkMsg}</p>}
        {states.length > 0 && <ul className="space-y-1.5 border-t border-border pt-3">{states.slice(0, 6).map((s) => <StatusLine key={s.instrument} st={s} unit={m?.unit ?? ""} />)}</ul>}

        <details className="group rounded-lg border border-border px-3 py-2 text-xs">
          <summary className="cursor-pointer list-none text-muted transition hover:text-foreground">
            <span className="inline-block transition group-open:rotate-90">›</span> Id and note <span className="num ml-1 text-foreground">{r.id || "—"}</span>
          </summary>
          <div className="mt-3 grid gap-3 sm:grid-cols-[140px_minmax(0,1fr)]">
            <Field label="Rule id" hint="Unique; shown on each alert.">
              <input className={cx(inputClass, "num w-full")} maxLength={40} value={r.id} onChange={(e) => set({ id: e.target.value })} />
            </Field>
            <Field label="Note (optional)">
              <input className={cx(inputClass, "w-full")} maxLength={300} value={r.description} placeholder={alertSentence(r, m)}
                onChange={(e) => set({ description: e.target.value })} />
            </Field>
          </div>
        </details>
        {errors && (
          <ul role="alert" className="space-y-0.5 rounded-lg bg-loss-soft px-3 py-2 text-xs text-loss">
            {errors.map((e) => <li key={e}>{e}</li>)}
          </ul>
        )}
      </div>
    </section>
  );
}

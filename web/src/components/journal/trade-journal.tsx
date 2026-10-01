"use client";

import { AlarmClock, Check, ChevronDown, FilePenLine, NotebookPen, Pencil, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge, Button, Card, EmptyState, ErrorNote, Field, Segmented, Skeleton, cx, inputClass } from "@/components/ui";
import { api, day, useApi } from "@/lib/api";

import { ChecklistView } from "./pretrade";
import type { NotesResponse, NoteStatus, TradeNote, Verdict } from "./types";

const STATUS_TONE: Record<NoteStatus, "warn" | "info" | "gain" | "neutral" | "brand"> = {
  draft: "warn", planned: "info", active: "brand", reviewed: "gain", cancelled: "neutral",
};
const VERDICTS: { value: Verdict; label: string }[] = [
  { value: "right", label: "Thesis played out" },
  { value: "wrong", label: "Thesis was wrong" },
  { value: "mixed", label: "Mixed" },
  { value: "too_early", label: "Too early to tell" },
];
type Filter = "due" | "draft" | "open" | "reviewed" | "all";

const inr = (n: number | null | undefined) => (n == null ? "—" : `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 2 })}`);

function NoteEditor({ n, onSaved, review }: { n: TradeNote; onSaved: () => void; review: boolean }) {
  const [v, setV] = useState({
    thesis: n.thesis ?? "", invalidation: n.invalidation ?? "",
    expected_holding_days: n.expected_holding_days?.toString() ?? "", confidence_pct: n.confidence_pct?.toString() ?? "",
    review_on: n.review_on ?? "", outcome_verdict: (n.outcome_verdict ?? "") as Verdict | "", outcome_notes: n.outcome_notes ?? "",
  });
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const save = async (extra: Record<string, unknown> = {}) => {
    const body: Record<string, unknown> = {
      thesis: v.thesis, invalidation: v.invalidation,
      expected_holding_days: v.expected_holding_days ? Number(v.expected_holding_days) : null,
      confidence_pct: v.confidence_pct ? Number(v.confidence_pct) : null,
      review_on: v.review_on || null, ...extra,
    };
    if (review && v.outcome_verdict) Object.assign(body, { outcome_verdict: v.outcome_verdict, outcome_notes: v.outcome_notes });
    setSaving(true);
    try {
      await api(`/api/journal/notes/${n.id}`, { method: "PATCH", body: JSON.stringify(body) });
      setError(null);
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  const text = (k: "thesis" | "invalidation" | "outcome_notes", label: string, hint: string) => (
    <Field label={label} hint={hint}>
      <textarea className={cx(inputClass, "h-20 w-full py-2")} value={v[k]} onChange={(e) => setV({ ...v, [k]: e.target.value })} />
    </Field>
  );
  return (
    <div className="space-y-3 animate-fade-in">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {text("thesis", "Why (the thesis)", "What you expect to happen and why.")}
        {text("invalidation", "What would prove it wrong (exit rule)", "Decide now, while you are not looking at a loss.")}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Field label="Expected holding (days)">
          <input className={cx(inputClass, "num w-full")} inputMode="numeric" value={v.expected_holding_days}
            onChange={(e) => setV({ ...v, expected_holding_days: e.target.value })} />
        </Field>
        <Field label="Confidence %" hint="How likely the thesis plays out (0–100).">
          <input className={cx(inputClass, "num w-full")} inputMode="numeric" value={v.confidence_pct}
            onChange={(e) => setV({ ...v, confidence_pct: e.target.value })} />
        </Field>
        <Field label="Review on" hint="You get a reminder that day.">
          <input type="date" className={cx(inputClass, "w-full")} value={v.review_on} onChange={(e) => setV({ ...v, review_on: e.target.value })} />
        </Field>
      </div>
      {review && (
        <div className="grid grid-cols-1 gap-3 rounded-lg border border-border bg-background-subtle/50 p-3 md:grid-cols-2">
          <Field label="What happened vs the thesis">
            <select className={cx(inputClass, "w-full")} value={v.outcome_verdict} onChange={(e) => setV({ ...v, outcome_verdict: e.target.value as Verdict })}>
              <option value="">—</option>
              {VERDICTS.map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}
            </select>
          </Field>
          {text("outcome_notes", "Review notes", "What you got right or wrong, and what you would do differently.")}
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={() => save()} disabled={saving} icon={<Check className="size-3.5" />}>{saving ? "Saving…" : review ? "Save review" : "Save"}</Button>
        {n.status === "planned" && (
          <Button variant="secondary" onClick={() => save({ status: "cancelled" })} disabled={saving}>Not taking this trade</Button>
        )}
      </div>
      <ErrorNote error={error} />
    </div>
  );
}

function Outcome({ n }: { n: TradeNote }) {
  const o = n.outcome as Record<string, number | string | null | undefined>;
  if (!n.outcome_verdict) return null;
  const r = typeof o.return_pct === "number" ? o.return_pct : typeof o.move_since_sale_pct === "number" ? o.move_since_sale_pct : null;
  return (
    <p className="mt-2 text-xs">
      <Badge tone={n.outcome_verdict === "right" ? "gain" : n.outcome_verdict === "wrong" ? "loss" : "neutral"}>
        {VERDICTS.find((x) => x.value === n.outcome_verdict)?.label}
      </Badge>
      {r != null && (
        <span className={cx("num ml-2 font-semibold", r > 0 ? "text-gain" : r < 0 ? "text-loss" : "")}>
          {n.side === "buy" ? "return" : "move since you sold"} {r > 0 ? "+" : ""}{r.toFixed(2)}%
        </span>
      )}
      {typeof o.why === "string" && <span className="ml-2 text-muted">{o.why}</span>}
      {n.outcome_notes && <span className="mt-1 block text-muted">{n.outcome_notes}</span>}
    </p>
  );
}

function NoteRow({ n, due, onSaved }: { n: TradeNote; due: boolean; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [showChecklist, setShowChecklist] = useState(false);
  const review = due || n.status === "reviewed" || n.status === "active";
  const del = async () => {
    if (!confirm("Delete this journal entry? The trade itself is not touched.")) return;
    await api(`/api/journal/notes/${n.id}`, { method: "DELETE" });
    onSaved();
  };
  return (
    <Card padded={false} className="animate-fade-up">
      <div className="flex flex-wrap items-start gap-3 p-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={n.side === "buy" ? "gain" : "loss"}>{n.side}</Badge>
            <p className="font-semibold">{n.name}</p>
            <Badge tone={STATUS_TONE[n.status]}>{n.status === "draft" ? "thesis missing" : n.status}</Badge>
            {due && <Badge tone="warn"><AlarmClock className="size-3" /> review due</Badge>}
          </div>
          <p className="mt-0.5 text-xs text-muted">
            {n.trade_day ? day(n.trade_day) : "no date"}
            {n.quantity != null && <> · <span className="num">{n.quantity.toLocaleString("en-IN")}</span> units</>}
            {n.price != null && <> at <span className="num">{inr(n.price)}</span></>}
            {n.txn_ids.length > 0 && ` · ${n.txn_ids.length} trade${n.txn_ids.length === 1 ? "" : "s"} linked`}
            {n.review_on && ` · review ${day(n.review_on)}`}
            {n.confidence_pct != null && ` · confidence ${n.confidence_pct}%`}
          </p>
          {n.thesis ? (
            <p className="mt-2 text-sm">{n.thesis}</p>
          ) : (
            <p className="mt-2 text-sm text-muted">No thesis yet. Write down why you made this trade while you still remember.</p>
          )}
          {n.invalidation && <p className="mt-1 text-xs text-muted"><span className="font-medium text-foreground">Exit if:</span> {n.invalidation}</p>}
          <Outcome n={n} />
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {n.checklist?.items && (
            <Button variant="ghost" onClick={() => setShowChecklist((x) => !x)}>Checklist</Button>
          )}
          <Button variant="secondary" icon={open ? <ChevronDown className="size-3.5 rotate-180" /> : n.status === "draft" ? <FilePenLine className="size-3.5" /> : <Pencil className="size-3.5" />}
            onClick={() => setOpen((x) => !x)}>
            {open ? "Close" : n.status === "draft" ? "Write thesis" : due ? "Review" : "Edit"}
          </Button>
          <Button variant="ghost" aria-label="Delete entry" icon={<Trash2 className="size-3.5" />} onClick={del} />
        </div>
      </div>
      {showChecklist && n.checklist?.items && (
        <div className="border-t border-border p-4 pt-3"><ChecklistView c={n.checklist as never} compact /></div>
      )}
      {open && (
        <div className="border-t border-border p-4 pt-3">
          <NoteEditor n={n} review={review} onSaved={() => { setOpen(false); onSaved(); }} />
        </div>
      )}
    </Card>
  );
}

/** Every buy and sell with its thesis: drafts for imported trades, planned trades, reviews due and past reviews. */
export function TradeJournal({ refreshKey }: { refreshKey?: number }) {
  const { data, error, reload } = useApi<NotesResponse>(`/api/journal/notes${refreshKey ? `?r=${refreshKey}` : ""}`);
  const [filter, setFilter] = useState<Filter>("open");
  const due = useMemo(() => new Set(data?.due ?? []), [data]);
  const counts = useMemo(() => {
    const l = data?.notes ?? [];
    return {
      due: l.filter((n) => due.has(n.id)).length,
      draft: l.filter((n) => n.status === "draft").length,
      open: l.filter((n) => ["draft", "planned", "active"].includes(n.status)).length,
      reviewed: l.filter((n) => n.status === "reviewed").length,
      all: l.length,
    };
  }, [data, due]);
  const shown = (data?.notes ?? []).filter((n) =>
    filter === "all" ? true : filter === "due" ? due.has(n.id) : filter === "draft" ? n.status === "draft"
      : filter === "reviewed" ? n.status === "reviewed" : ["draft", "planned", "active"].includes(n.status));
  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Segmented<Filter> value={filter} onChange={setFilter} options={[
          { value: "open", label: `Open (${counts.open})` },
          { value: "due", label: `Reviews due (${counts.due})` },
          { value: "draft", label: `Thesis missing (${counts.draft})` },
          { value: "reviewed", label: `Reviewed (${counts.reviewed})` },
          { value: "all", label: `All (${counts.all})` },
        ]} />
        {data && <p className="text-[11px] text-muted">{data.privacy}</p>}
      </div>
      <ErrorNote error={error} onRetry={reload} />
      {!data && !error && Array.from({ length: 2 }, (_, i) => <Skeleton key={i} className="h-24 rounded-xl" />)}
      {data && shown.length === 0 && (
        <EmptyState icon={<NotebookPen className="size-5" />} title={filter === "due" ? "No reviews due" : "Nothing here yet"}>
          Trades you import get an entry here asking for the thesis (trades from the last 30 days; SIP instalments and
          corporate actions are left out). Use &ldquo;Plan a trade&rdquo; to run the checklist and write the thesis before you trade.
        </EmptyState>
      )}
      <div className="space-y-3">
        {shown.map((n) => <NoteRow key={n.id} n={n} due={due.has(n.id)} onSaved={reload} />)}
      </div>
    </section>
  );
}

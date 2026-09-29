"use client";

import { Check, ChevronDown, FileText, IndianRupee, NotebookPen, Pencil, Target, Trophy, Wallet } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { BarsChart, DonutChart, fmtINR } from "@/components/charts";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, PageHeader, Skeleton, Stat, cx, inputClass } from "@/components/ui";
import { type Decision, api, day, useApi, when } from "@/lib/api";

const ACTION: Record<string, string> = { APPLY: "done", "APPLY-CONDITIONAL": "unverified", SKIP: "blocked" };
const num = (x: unknown) => (typeof x === "string" && x !== "" && Number.isFinite(Number(x)) ? Number(x) : null);
const returnOf = (d: Decision) => num(d.outcome.exit_return_pct) ?? num(d.outcome.listing_gain_pct);

function OutcomeEditor({ d, onSaved }: { d: Decision; onSaved: () => void }) {
  const [v, setV] = useState({
    user_action: d.user_action ?? "",
    applied_lots: d.applied_lots?.toString() ?? "",
    allotted_lots: d.allotted_lots?.toString() ?? "",
    issue_price: d.issue_price ?? "",
    listing_price: d.listing_price ?? "",
    exit_price: d.exit_price ?? "",
    exit_date: d.exit_date ?? "",
  });
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const save = async () => {
    const body: Record<string, string | number | null> = {};
    for (const [k, x] of Object.entries(v)) {
      if (x === "") continue;
      body[k] = k.endsWith("_lots") ? Number(x) : x;
    }
    setSaving(true);
    try {
      await api(`/api/decisions/${d.id}`, { method: "PATCH", body: JSON.stringify(body) });
      setError(null);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };
  const input = (k: keyof typeof v, label: string, type = "text", hint?: string) => (
    <Field label={label} hint={hint}>
      <input className={cx(inputClass, "num w-full")} type={type} inputMode={type === "text" ? "decimal" : undefined} value={v[k]}
        onChange={(e) => setV({ ...v, [k]: e.target.value })} />
    </Field>
  );
  return (
    <div className="space-y-3 rounded-lg border border-border bg-background-subtle/50 p-3 animate-fade-in">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
        <Field label="What I did">
          <select className={cx(inputClass, "w-full")} value={v.user_action} onChange={(e) => setV({ ...v, user_action: e.target.value })}>
            <option value="">—</option>
            <option value="applied">applied</option>
            <option value="skipped">skipped</option>
          </select>
        </Field>
        {input("applied_lots", "Lots applied")}
        {input("allotted_lots", "Lots allotted")}
        {input("issue_price", "Issue price ₹")}
        {input("listing_price", "Listing price ₹")}
        {input("exit_price", "Exit price ₹")}
        {input("exit_date", "Exit date", "date")}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={save} disabled={saving} icon={saved ? <Check className="size-3.5" /> : undefined}>
          {saving ? "Saving…" : saved ? "Saved" : "Save outcome"}
        </Button>
        <span className="text-xs text-muted">Returns and profit are calculated for you from these prices.</span>
      </div>
      <ErrorNote error={error} />
    </div>
  );
}

function OutcomeChips({ d }: { d: Decision }) {
  const o = d.outcome;
  const chips: { label: string; value: string; tone?: "gain" | "loss" | "neutral" }[] = [];
  const lg = num(o.listing_gain_pct), er = num(o.exit_return_pct), p = num(o.profit_inr);
  const sign = (x: number) => (x > 0 ? "gain" : x < 0 ? "loss" : "neutral");
  if (lg != null) chips.push({ label: "Listing gain", value: `${lg > 0 ? "+" : ""}${lg.toFixed(2)}%`, tone: sign(lg) });
  if (er != null) chips.push({ label: "Exit return", value: `${er > 0 ? "+" : ""}${er.toFixed(2)}%`, tone: sign(er) });
  if (p != null) chips.push({ label: "Profit", value: fmtINR(p), tone: sign(p) });
  if (typeof o.followed_suggestion === "boolean") chips.push({ label: "Followed suggestion", value: o.followed_suggestion ? "yes" : "no" });
  const known = new Set(["listing_gain_pct", "exit_return_pct", "profit_inr", "followed_suggestion"]);
  for (const [k, x] of Object.entries(o)) if (!known.has(k)) chips.push({ label: k.replaceAll("_", " "), value: String(x) });
  if (!chips.length) return <p className="text-xs text-muted">No outcome recorded yet.</p>;
  return (
    <div className="flex flex-wrap gap-2">
      {chips.map((c) => (
        <span key={c.label} className="inline-flex items-center gap-1.5 rounded-lg bg-background-subtle px-2 py-1 text-xs">
          <span className="text-muted">{c.label}</span>
          <span className={cx("num font-semibold", c.tone === "gain" && "text-gain", c.tone === "loss" && "text-loss")}>{c.value}</span>
        </span>
      ))}
    </div>
  );
}

function DecisionRow({ d, onSaved }: { d: Decision; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  return (
    <Card padded={false} className="animate-fade-up">
      <div className="flex flex-wrap items-start gap-3 p-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="font-semibold">{d.company_name ?? `Run #${d.run_id}`}</p>
            <Badge status={ACTION[d.action]}>{d.action}</Badge>
            {d.user_action && <Badge status={d.user_action}>you {d.user_action}</Badge>}
          </div>
          <p className="mt-0.5 text-xs text-muted">
            Suggested {when(d.created_at)} · <span className="num">{d.lots}</span> lot{d.lots === 1 ? "" : "s"}
            {d.category ? ` · ${d.category}` : ""}
            {d.exit_date ? ` · exited ${day(d.exit_date)}` : ""}
          </p>
          <div className="mt-3">
            <OutcomeChips d={d} />
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Link href={`/runs/${d.run_id}/report`} className="inline-flex h-8 items-center gap-1 rounded-lg px-2.5 text-xs font-medium text-muted transition hover:bg-background-subtle hover:text-foreground">
            <FileText className="size-3.5" /> Report
          </Link>
          <Button variant="secondary" icon={editing ? <ChevronDown className="size-3.5 rotate-180" /> : <Pencil className="size-3.5" />} onClick={() => setEditing((e) => !e)}>
            {editing ? "Close" : "Record outcome"}
          </Button>
        </div>
      </div>
      {editing && (
        <div className="border-t border-border p-4 pt-3">
          <OutcomeEditor d={d} onSaved={onSaved} />
        </div>
      )}
    </Card>
  );
}

export default function Journal() {
  const { data, error, reload } = useApi<Decision[]>("/api/decisions");

  const k = useMemo(() => {
    const list = data ?? [];
    const followed = list.filter((d) => typeof d.outcome.followed_suggestion === "boolean");
    const returns = list.map(returnOf).filter((x): x is number => x != null);
    const applied = list.filter((d) => d.user_action === "applied");
    const appliedLots = applied.reduce((a, d) => a + (d.applied_lots ?? 0), 0);
    const allotLots = applied.reduce((a, d) => a + (d.allotted_lots ?? 0), 0);
    const withAllot = applied.filter((d) => d.allotted_lots != null);
    const profit = list.map((d) => num(d.outcome.profit_inr)).filter((x): x is number => x != null);
    return {
      total: list.length,
      followedYes: followed.filter((d) => d.outcome.followed_suggestion).length,
      followed: followed.length,
      winRate: returns.length ? (returns.filter((x) => x > 0).length / returns.length) * 100 : null,
      avgReturn: returns.length ? returns.reduce((a, b) => a + b, 0) / returns.length : null,
      applied: applied.length,
      skipped: list.filter((d) => d.user_action === "skipped").length,
      unrecorded: list.filter((d) => !d.user_action).length,
      allotRate: appliedLots ? (allotLots / appliedLots) * 100 : null,
      allotted: withAllot.filter((d) => (d.allotted_lots ?? 0) > 0).length,
      withAllot: withAllot.length,
      profit: profit.length ? profit.reduce((a, b) => a + b, 0) : null,
      suggested: ["APPLY", "APPLY-CONDITIONAL", "SKIP"].map((a) => ({ name: a, value: list.filter((d) => d.action === a).length })),
      gains: list
        .map((d) => ({ name: d.company_name ?? `#${d.run_id}`, gain: returnOf(d) }))
        .filter((r): r is { name: string; gain: number } => r.gain != null)
        .reverse(),
    };
  }, [data]);

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<NotebookPen className="size-5" />}
        eyebrow="Workspace"
        title="Decision journal"
        description="Every suggestion you asked for, what you actually did, and how it turned out. Record outcomes to see if the suggestions (and your rules) are working."
      />

      <ErrorNote error={error} onRetry={reload} />

      <div className="stagger grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Decisions" value={data ? k.total : null} format={(n) => String(Math.round(n))} icon={<NotebookPen className="size-4" />}
          hint={data ? (k.followed ? `you followed ${k.followedYes} of ${k.followed} suggestions` : "no outcomes recorded yet") : undefined} />
        <Stat label="Win rate" value={k.winRate} format={(n) => `${Math.round(n)}%`} tone="gain" icon={<Trophy className="size-4" />}
          help="Share of recorded decisions with a positive return: exit price over issue price if you sold, else listing price over issue price."
          hint={k.avgReturn != null ? `average return ${k.avgReturn.toFixed(2)}%` : "record listing prices to see it"} />
        <Stat label="Allotment rate" value={k.allotRate} format={(n) => `${Math.round(n)}%`} tone="info" icon={<Target className="size-4" />}
          help="Lots allotted to you out of lots you applied for. Popular IPOs are oversubscribed, so retail investors often get one lot or none (by lottery)."
          hint={k.withAllot ? `allotted in ${k.allotted} of ${k.withAllot} IPOs` : "record allotted lots to see it"} />
        <Stat label="Realised profit" value={k.profit} format={fmtINR} tone={k.profit != null && k.profit < 0 ? "loss" : "gain"} icon={<Wallet className="size-4" />}
          help="(Exit price − issue price) × allotted shares, summed over decisions where you recorded an exit. Before tax and charges." hint="before tax and charges" />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card title="Return per decision" icon={<IndianRupee className="size-4" />} className="lg:col-span-2"
          subtitle="Exit return if you sold, otherwise the listing-day gain over the issue price"
          help="Listing gain = (listing price − issue price) ÷ issue price. Green bars made money, red lost.">
          {!data ? (
            <Skeleton className="h-56 rounded-lg" />
          ) : k.gains.length ? (
            <BarsChart data={k.gains} x="name" series={[{ key: "gain", label: "Return" }]} format={(v) => `${v.toFixed(1)}%`}
              colorBy={(r) => (Number(r.gain) >= 0 ? "var(--gain)" : "var(--loss)")} height={240} reference={{ value: 0, label: "" }} />
          ) : (
            <EmptyState icon={<IndianRupee className="size-5" />} title="No returns recorded yet">
              After an IPO lists, open its decision below, press Record outcome and enter the issue and listing (or exit) prices.
            </EmptyState>
          )}
        </Card>
        <Card title="Applied vs skipped" subtitle="What you did, and what was suggested">
          {!data ? (
            <Skeleton className="h-40 rounded-lg" />
          ) : k.total ? (
            <div className="space-y-5">
              <DonutChart height={120}
                data={[
                  { name: "applied", value: k.applied, color: "var(--gain)" },
                  { name: "skipped", value: k.skipped, color: "var(--loss)" },
                  { name: "not recorded", value: k.unrecorded, color: "var(--border-strong)" },
                ]}
                center={<p className="text-[11px] text-muted">you</p>} />
              <DonutChart height={120}
                data={k.suggested.map((s) => ({ ...s, name: s.name === "APPLY-CONDITIONAL" ? "apply if…" : s.name.toLowerCase(), color: s.name === "APPLY" ? "var(--gain)" : s.name === "SKIP" ? "var(--loss)" : "var(--warn)" }))}
                center={<p className="text-[11px] text-muted">suggested</p>} />
            </div>
          ) : (
            <p className="text-sm text-muted">Nothing to compare yet.</p>
          )}
        </Card>
      </div>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold">Decisions</h2>
        {!data && !error && Array.from({ length: 2 }, (_, i) => <Skeleton key={i} className="h-24 rounded-xl" />)}
        {data?.length === 0 && (
          <EmptyState icon={<NotebookPen className="size-5" />} title="No suggestions yet"
            action={<Link href="/runs" className="text-sm font-medium text-brand hover:underline">Go to research runs</Link>}>
            Open an IPO report and press &ldquo;Get my suggestion&rdquo;. Each suggestion is saved here so you can record what you did.
          </EmptyState>
        )}
        <div className="space-y-3">
          {data?.map((d) => (
            <DecisionRow key={d.id} d={d} onSaved={reload} />
          ))}
        </div>
      </section>
    </div>
  );
}

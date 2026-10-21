"use client";

// Capital-loss carry-forward (#344): the ledger by year of origin with its 8-year expiry, the "was that year's return
// filed on time?" question (unknown is shown as unknown, never assumed) and losses typed in from an earlier return.
// Law: s.70 / s.74 / s.80 of the Income-tax Act 1961 (s.108 / s.111 / s.121 of the 2025 Act). Personal data.

import { History, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { Badge, Button, Card, Field, InfoTip, Table, type Tone, cx, inputClass } from "@/components/ui";
import { api } from "@/lib/api";

import { type CarryForward, type LossStatus, type ManualLossInput, type TaxLossInputs, inr } from "./types";

const STATUS: Record<LossStatus, [string, Tone]> = {
  available: ["available", "gain"], used: ["used up", "neutral"], lapsed: ["lapsed (8 years)", "loss"],
  not_carried: ["not carried: late return", "loss"], unknown_filing: ["filing unknown", "warn"],
  not_yet_due: ["return not yet due", "info"], unknown_amount: ["amount unknown", "warn"],
};
const KIND = { short: "Short-term", long: "Long-term", unknown: "Unknown term" } as const;
const fyName = (fy: number) => `FY ${fy - 1}-${String(fy % 100).padStart(2, "0")}`;

export function CarryForwardCard({ cf, onSaved }: { cf: CarryForward; onSaved: () => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  const [draft, setDraft] = useState({ origin_fy: "", kind: "short", amount_inr: "", source: "", apply_from_fy: "" });
  const save = async (next: TaxLossInputs, done: string) => {
    setMsg(null);
    try {
      await api("/api/portfolio/tax/losses", { method: "PUT", body: JSON.stringify(next) });
      setMsg(done); onSaved();
    } catch (e) { setMsg((e as Error).message); }
  };
  const setFiling = (fy: number, v: boolean | null) => {
    const filed = { ...cf.inputs.returns_filed_on_time };
    if (v == null) delete filed[String(fy)]; else filed[String(fy)] = v;
    save({ ...cf.inputs, returns_filed_on_time: filed }, `Saved for ${fyName(fy)}`);
  };
  const add = () => {
    const entry: ManualLossInput = { origin_fy: Number(draft.origin_fy), kind: draft.kind as "short" | "long",
      amount_inr: draft.amount_inr, source: draft.source, apply_from_fy: draft.apply_from_fy ? Number(draft.apply_from_fy) : null };
    save({ ...cf.inputs, brought_forward: [...cf.inputs.brought_forward, entry] }, "Loss added");
  };
  const remove = (i: number) => save({ ...cf.inputs, brought_forward: cf.inputs.brought_forward.filter((_, j) => j !== i) }, "Removed");
  const years = [...new Set([...cf.ask_filing, ...Object.keys(cf.inputs.returns_filed_on_time).map(Number),
    ...cf.entries.map((e) => e.origin_fy)])].sort((a, b) => b - a);

  return (
    <Card title="Losses carried forward" icon={<History className="size-4" />} subtitle="By year of origin · usable for 8 years if that year's return was filed on time"
      help={cf.law}>
      {cf.entries.length === 0 ? <p className="text-sm text-muted">No capital losses to carry forward. Add one from an earlier return below if you have it.</p> : (
        <Table label="Carry-forward ledger">
          <thead><tr><th>Loss year</th><th>Kind</th><th className="text-right">Loss</th><th className="text-right">Set off</th><th className="text-right">Left</th><th>Usable until</th><th>Status</th><th>Source</th></tr></thead>
          <tbody>
            {cf.entries.map((e) => {
              const [label, tone] = STATUS[e.status];
              const used = Object.entries(e.used).map(([fy, a]) => `${fyName(Number(fy))}: ${inr(a)}`).join(", ");
              return (
                <tr key={`${e.origin_fy}-${e.kind}`}>
                  <td className="num text-xs">{e.label}</td>
                  <td className="text-xs">{KIND[e.kind]}</td>
                  <td className="num text-right">{e.amount == null ? <span className="text-warn">unknown</span> : inr(e.amount)}</td>
                  <td className="num text-right text-muted">{used ? <span title={used}>{inr(Object.values(e.used).reduce((a, b) => a + b, 0))}</span> : "—"}</td>
                  <td className="num text-right font-medium">{e.remaining == null ? <span className="text-warn">unknown</span> : inr(e.remaining)}</td>
                  <td className="num text-xs">{e.last_label}</td>
                  <td><Badge tone={tone}>{label}</Badge>{e.notes.length > 0 && <InfoTip>{e.notes.join(" ")}</InfoTip>}</td>
                  <td className="max-w-[12rem] truncate text-xs text-muted" title={e.source}>{e.source === "computed" ? "from your trades" : e.source}</td>
                </tr>
              );
            })}
          </tbody>
        </Table>
      )}
      <FilingQuestions years={years} filed={cf.inputs.returns_filed_on_time} onSet={setFiling} />
      <ManualLosses inputs={cf.inputs} draft={draft} setDraft={setDraft} onAdd={add} onRemove={remove} />
      {msg && <p className="mt-2 text-xs text-muted" role="status">{msg}</p>}
    </Card>
  );
}

/** Per loss year: was the return filed by the s.139(1) due date? Three answers, and "not sure" stays unknown. */
function FilingQuestions({ years, filed, onSet }: { years: number[]; filed: Record<string, boolean>; onSet: (fy: number, v: boolean | null) => void }) {
  if (years.length === 0) return null;
  return (
    <div className="mt-4 space-y-1.5">
      <p className="text-xs font-medium">
        Was the return for the loss year filed on time?{" "}
        <InfoTip>A capital loss is carried forward only if it was declared in a return filed by the s.139(1) due date (usually 31 July of the assessment year for an individual without an audit): s.80 with s.139(3) of the 1961 Act, s.121 of the 2025 Act. A belated or updated return does not carry it. Until you answer, the loss is left out of later years&apos; tax and the years are marked incomplete.</InfoTip>
      </p>
      {years.map((fy) => {
        const v = filed[String(fy)];
        return (
          <div key={fy} className="flex flex-wrap items-center gap-2 text-xs">
            <span className="num w-24">{fyName(fy)}</span>
            {([[true, "On time"], [false, "Late"], [null, "Not sure"]] as const).map(([val, label]) => (
              <button key={label} type="button" aria-pressed={v === val || (val === null && v === undefined)}
                onClick={() => onSet(fy, val)}
                className={cx("rounded-md px-2 py-0.5 ring-1 ring-inset ring-border",
                  (v === val || (val === null && v === undefined)) ? "bg-brand/15 font-medium text-brand" : "hover:bg-card-hover")}>
                {label}
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );
}

type Draft = { origin_fy: string; kind: string; amount_inr: string; source: string; apply_from_fy: string };

/** Losses brought forward from returns filed before the app's records (Schedule CFL), each with its source. */
function ManualLosses({ inputs, draft, setDraft, onAdd, onRemove }: {
  inputs: TaxLossInputs; draft: Draft; setDraft: (d: Draft) => void; onAdd: () => void; onRemove: (i: number) => void;
}) {
  const ok = Number(draft.origin_fy) >= 2000 && Number(draft.amount_inr) > 0 && draft.source.trim().length > 0;
  return (
    <div className="mt-4 border-t border-border pt-3">
      <p className="text-xs font-medium">
        Losses from earlier returns{" "}
        <InfoTip>Enter what your last return&apos;s Schedule CFL shows as still to be carried forward for each loss year, and from which year the app should start using it (default: the year after the loss). It replaces the app&apos;s own figure for that year and kind.</InfoTip>
      </p>
      {inputs.brought_forward.length > 0 && (
        <ul className="mt-1 space-y-1 text-xs">
          {inputs.brought_forward.map((b, i) => (
            <li key={`${b.origin_fy}-${b.kind}`} className="flex items-center gap-2">
              <span className="num">{fyName(b.origin_fy)}</span><span>{KIND[b.kind]}</span><span className="num">{inr(Number(b.amount_inr))}</span>
              <span className="truncate text-muted">{b.source}</span>
              <button type="button" aria-label={`Remove the ${fyName(b.origin_fy)} loss`} onClick={() => onRemove(i)} className="text-muted hover:text-loss"><Trash2 className="size-3.5" /></button>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-2 grid gap-2 sm:grid-cols-5">
        <Field label="Loss year (FY ending)"><input inputMode="numeric" placeholder="2024" value={draft.origin_fy} onChange={(e) => setDraft({ ...draft, origin_fy: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Kind">
          <select value={draft.kind} onChange={(e) => setDraft({ ...draft, kind: e.target.value })} className={cx(inputClass, "w-full")}>
            <option value="short">Short-term</option><option value="long">Long-term</option>
          </select>
        </Field>
        <Field label="Amount left (₹)"><input inputMode="decimal" value={draft.amount_inr} onChange={(e) => setDraft({ ...draft, amount_inr: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
        <Field label="Source"><input placeholder="ITR AY 2024-25 Schedule CFL" value={draft.source} onChange={(e) => setDraft({ ...draft, source: e.target.value })} className={cx(inputClass, "w-full")} /></Field>
        <Field label="Use from FY ending (optional)"><input inputMode="numeric" value={draft.apply_from_fy} onChange={(e) => setDraft({ ...draft, apply_from_fy: e.target.value })} className={cx(inputClass, "w-full num")} /></Field>
      </div>
      <Button className="mt-2" variant="secondary" icon={<Plus className="size-3.5" />} disabled={!ok} onClick={onAdd}>Add loss</Button>
    </div>
  );
}

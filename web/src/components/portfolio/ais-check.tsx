"use client";

// Tax → AIS check: the user's AIS (imported on the Import tab or via the statement inbox) against the app's dividends,
// sales and purchases for one financial year. Mismatches first, each with its likely cause and what to do.

import { ChevronDown, ChevronRight, FileSearch, Trash2 } from "lucide-react";
import { Fragment, useState } from "react";

import { Badge, Button, Callout, Card, EmptyState, ErrorNote, Segmented, SkeletonRows, Table, cx } from "@/components/ui";
import { api, day, useApi, when } from "@/lib/api";

import { type AisCheck as Check, type AisList, type AisRow, type AisStatus, inr, signed, units } from "./types";

const STATUS: Record<AisStatus, { label: string; tone: "gain" | "loss" | "warn" | "info" }> = {
  matched: { label: "matched", tone: "gain" }, mismatch: { label: "amount differs", tone: "loss" },
  only_ais: { label: "only in AIS", tone: "warn" }, only_app: { label: "only in app", tone: "info" },
};
const CAT: Record<string, string> = { dividend: "Dividend", sale: "Sale value", purchase: "Purchase", interest: "Interest", off_market: "Off-market" };
const MATCH: Record<string, string> = { isin: "by ISIN", name: "by name", amc: "by fund house", total: "year total" };

function Detail({ r }: { r: AisRow }) {
  return (
    <div className="space-y-2 py-1 text-xs">
      {r.cause && <p><span className="font-medium">Likely cause: </span>{r.cause}</p>}
      {r.action && <p><span className="font-medium">What to do: </span>{r.action}</p>}
      {r.duplicates_dropped > 0 && <p className="text-muted">{r.duplicates_dropped} row(s) reported under both TDS and SFT counted once.</p>}
      <div className="grid gap-3 md:grid-cols-2">
        <div>
          <p className="mb-1 font-medium">In the AIS</p>
          {r.ais_rows.length === 0 ? <p className="text-muted">nothing</p> : r.ais_rows.map((a, i) => (
            <p key={i} className="num">{a.day ? day(a.day) : "no date"} · {a.code ?? a.part.toUpperCase()} · {a.security ?? a.source ?? ""}{a.tan ? ` (${a.tan})` : ""} · {inr(a.amount, 2)}{a.quantity != null ? ` · ${units(a.quantity)} units` : ""}{a.tds ? ` · TDS ${inr(a.tds, 2)}` : ""}{a.stt ? ` · STT ${inr(a.stt, 2)}` : ""}</p>
          ))}
        </div>
        <div>
          <p className="mb-1 font-medium">In the app</p>
          {r.app_rows.length === 0 ? <p className="text-muted">nothing</p> : r.app_rows.map((a, i) => (
            <p key={i} className="num">{day(a.day)} · {a.name} · {inr(a.amount, 2)}{a.quantity != null ? ` · ${units(a.quantity)} units` : ""}</p>
          ))}
        </div>
      </div>
    </div>
  );
}

function Rows({ c }: { c: Check }) {
  const [open, setOpen] = useState<number | null>(null);
  if (c.rows.length === 0) return <EmptyState title="Nothing to compare">This AIS has no dividends, sales or purchases, and the app has none in {c.label}.</EmptyState>;
  return (
    <Table label={`AIS check ${c.label}`}>
      <thead><tr><th /><th>What</th><th>Security / payer</th><th className="text-right">AIS</th><th className="text-right">App</th><th className="text-right">Difference</th><th>Status</th></tr></thead>
      <tbody>
        {c.rows.map((r, i) => (
          <Fragment key={i}>
            <tr className={cx("cursor-pointer", r.status !== "matched" && "font-medium")} onClick={() => setOpen(open === i ? null : i)}>
              <td className="w-6">{open === i ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}</td>
              <td className="text-xs">{CAT[r.category]}</td>
              <td className="max-w-[18rem]"><span className="block truncate">{r.label}</span>
                <span className="text-[11px] font-normal text-muted">{r.isin ?? ""}{r.match ? ` ${MATCH[r.match]}` : ""}{r.ais_tds ? ` · TDS ${inr(r.ais_tds)}` : ""}</span></td>
              <td className="num text-right">{r.ais_amount != null ? inr(r.ais_amount, 2) : "—"}</td>
              <td className="num text-right">{r.app_amount != null ? inr(r.app_amount, 2) : "—"}</td>
              <td className={cx("num text-right", r.status === "mismatch" && "text-loss")}>{r.diff != null ? signed(r.diff, 2) : "—"}</td>
              <td><Badge tone={STATUS[r.status].tone}>{STATUS[r.status].label}</Badge></td>
            </tr>
            {open === i && <tr><td /><td colSpan={6} className="whitespace-normal"><Detail r={r} /></td></tr>}
          </Fragment>
        ))}
      </tbody>
    </Table>
  );
}

export function AisCheck({ refresh, preferFy }: { refresh: number; preferFy?: number }) {
  const [bump, setBump] = useState(0);
  const list = useApi<AisList>(`/api/portfolio/ais?r=${refresh}-${bump}`);
  const [pick, setPick] = useState<number | null>(null);
  const years = list.data?.statements ?? [];
  const fy = pick ?? (years.find((s) => s.fy === preferFy) ?? years[0])?.fy ?? null;
  const check = useApi<Check>(fy ? `/api/portfolio/ais/${fy}?r=${refresh}-${bump}` : null);
  const [err, setErr] = useState<string | null>(null);
  const stmt = years.find((s) => s.fy === fy);
  // #216: "Experimental" until a saved import of this format parsed with no unrecognised rows and matching totals
  const valid = list.data?.validated ?? {};
  const proof = stmt ? valid[stmt.format] : Object.values(valid)[0];

  const forget = async () => {
    if (!fy || !window.confirm(`Forget the AIS for FY ${fy - 1}-${String(fy % 100).padStart(2, "0")}? Your transactions are not touched.`)) return;
    try { await api(`/api/portfolio/ais/${fy}`, { method: "DELETE" }); setPick(null); setBump((b) => b + 1); } catch (e) { setErr((e as Error).message); }
  };

  if (list.error) return <ErrorNote error={list.error} onRetry={list.reload} />;
  return (
    <Card title="AIS check" icon={<FileSearch className="size-4" />}
      subtitle="Your Annual Information Statement against the app: dividends, sale values and purchases per security, before you file the ITR."
      help="Import the AIS (JSON or PDF from the income-tax portal → AIS) on the Import tab or drop the JSON in the statement inbox. Only the rows this check needs are kept: no PAN, name, address or account numbers, and the file itself is not saved."
      actions={stmt && <Button variant="ghost" icon={<Trash2 className="size-3.5" />} onClick={() => void forget()}>Forget</Button>}>
      {list.data && (proof ? (
        <p className="mb-3 text-[11px] text-muted">AIS format first validated on {new Date(proof.validated_at).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}{stmt ? ` (${stmt.format.toUpperCase()})` : ""}.</p>
      ) : (
        <p className="mb-3 flex flex-wrap items-center gap-1.5 text-xs text-foreground/90" data-testid="ais-experimental">
          <Badge tone="warn">Experimental</Badge>
          <span>The AIS format isn&apos;t yet validated on a real AIS file{stmt ? ` (${stmt.format.toUpperCase()})` : ""}. It is marked validated after the first import with no unrecognised rows and matching totals; until then, check every row against the AIS on the portal.</span>
        </p>
      ))}
      {!list.data ? <SkeletonRows rows={3} /> : years.length === 0 ? (
        <EmptyState title="No AIS imported yet">
          Download your AIS from the income-tax portal (AIS → Download → JSON, or the PDF, whose password is your PAN in lower case followed by your date of birth as ddmmyyyy) and drop it on the Import tab.
        </EmptyState>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Segmented value={String(fy)} onChange={(v) => setPick(Number(v))} options={years.map((s) => ({ value: String(s.fy), label: s.label.replace("FY ", "FY") }))} />
            {stmt && <span className="text-xs text-muted">{stmt.format.toUpperCase()} · {stmt.rows} rows kept · imported {when(stmt.imported_at)}</span>}
          </div>
          {err && <ErrorNote error={err} />}
          {check.error ? <ErrorNote error={check.error} onRetry={check.reload} /> : !check.data ? <SkeletonRows rows={5} /> : (
            <>
              <div className="flex flex-wrap gap-2 text-xs">
                {(Object.keys(STATUS) as AisStatus[]).map((s) => <Badge key={s} tone={STATUS[s].tone}>{check.data!.counts[s]} {STATUS[s].label}</Badge>)}
                <span className="text-muted">match tolerance {check.data.tolerance}</span>
              </div>
              {(stmt?.warnings ?? []).filter((w) => !check.data!.notes.includes(w)).map((w) => <Callout key={w} tone="warn">{w}</Callout>)}
              <Rows c={check.data} />
              {check.data.info.length > 0 && (
                <div>
                  <p className="mb-1 text-xs font-medium">Also in the AIS (not tracked by the app: check by hand)</p>
                  <ul className="space-y-1 text-xs">
                    {check.data.info.map((i, k) => (
                      <li key={k}><Badge tone="info">{CAT[i.category]}</Badge> {i.label}: <span className="num">{inr(i.ais_amount, 2)}</span>{i.ais_tds ? <span className="num text-muted"> · TDS {inr(i.ais_tds, 2)}</span> : null}<span className="block text-muted">{i.note}</span></li>
                    ))}
                  </ul>
                </div>
              )}
              <ul className="space-y-1 text-xs text-muted">{check.data.notes.map((n) => <li key={n}>• {n}</li>)}</ul>
            </>
          )}
        </div>
      )}
    </Card>
  );
}

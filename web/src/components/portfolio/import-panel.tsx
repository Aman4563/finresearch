"use client";

// Import flows: a CAS PDF (password typed here, sent once with the upload, never stored), a broker tradebook
// (CSV/XLSX, broker detected from the headers) and a manual form. Every file import previews first (dry run) and
// shows the reconciliation of the statement's closing units against the lots before anything is written.

import { CheckCircle2, FileSpreadsheet, FileText, FileUp, KeyRound, PenLine, RefreshCw, ShieldCheck, Trash2, TriangleAlert, Upload, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { Badge, Button, Callout, Card, ErrorNote, Field, InfoTip, Segmented, Table, cx, inputClass } from "@/components/ui";
import { api, day, useApi, when } from "@/lib/api";

import { type AisImport, type ImportPreview, type ImportRow, units } from "./types";

function readB64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",", 2)[1] ?? "");
    r.onerror = () => reject(new Error("could not read the file"));
    r.readAsDataURL(file);
  });
}

function PreviewView({ p }: { p: ImportPreview }) {
  const skipped = Object.entries(p.skipped ?? {});
  return (
    <div className="space-y-3 animate-fade-in">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge tone="info">{p.source}</Badge>
        {p.period && <span className="text-muted">{p.period[0]} → {p.period[1]}</span>}
        <span className="num">{p.rows} rows</span>
        {p.dry_run ? <span className="num text-muted">{p.new_rows ?? 0} new · {p.duplicates} already imported</span>
          : <span className="num text-gain">{p.added ?? 0} added · {p.duplicates} duplicates skipped</span>}
        {p.already_imported != null && <Badge tone="warn">this file is import #{p.already_imported}</Badge>}
      </div>
      {p.reconciliation.length > 0 && (
        <div>
          <p className="mb-1 flex items-center gap-1.5 text-xs font-medium">
            {p.reconciled ? <CheckCircle2 className="size-3.5 text-gain" /> : <TriangleAlert className="size-3.5 text-warn" />}
            Reconciliation: statement closing units vs units in your lots
            <InfoTip>After the import, the units your FIFO lots hold should equal what the statement says you hold at its end. A mismatch means a transaction is missing (e.g. an older statement) or a row could not be read.</InfoTip>
          </p>
          <Table label="Reconciliation: statement units vs lot units">
            <thead><tr><th>Scheme / security</th><th className="text-right">Statement</th><th className="text-right">Lots</th><th>Check</th></tr></thead>
            <tbody>
              {p.reconciliation.map((r) => (
                <tr key={`${r.ikey}|${r.account}`}>
                  <td className="max-w-[18rem] truncate">{r.name}<span className="block text-[11px] text-muted">{r.account}</span></td>
                  <td className="num text-right">{units(r.statement_units)}</td>
                  <td className="num text-right">{units(r.lot_units)}</td>
                  <td>{r.ok ? <Badge tone="gain">matches</Badge> : <Badge tone="loss">off by {units(r.diff)}</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </Table>
        </div>
      )}
      {p.sample && p.sample.length > 0 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted">First {p.sample.length} new rows</summary>
          <Table label="First new rows" className="mt-2">
            <thead><tr><th>Date</th><th>Kind</th><th>Name</th><th className="text-right">Units</th><th className="text-right">Price</th><th className="text-right">Amount</th></tr></thead>
            <tbody>
              {p.sample.map((t, i) => (
                <tr key={i}><td className="num">{day(t.day)}</td><td>{t.kind}</td><td className="max-w-[16rem] truncate">{t.name}</td>
                  <td className="num text-right">{units(t.quantity)}</td><td className="num text-right">{t.price ?? "—"}</td><td className="num text-right">{t.amount ?? "—"}</td></tr>
              ))}
            </tbody>
          </Table>
        </details>
      )}
      {(p.warnings.length > 0 || skipped.length > 0) && (
        <ul className="space-y-1 rounded-lg bg-warn-soft px-3 py-2 text-xs">
          {p.warnings.map((w) => <li key={w} className="flex gap-1.5"><TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warn" />{w}</li>)}
          {skipped.length > 0 && <li className="text-muted">Skipped: {skipped.map(([k, n]) => `${k} ×${n}`).join(", ")}</li>}
        </ul>
      )}
    </div>
  );
}

function CasImport({ onDone }: { onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  useEffect(() => () => setPassword(""), []); // forget the password when the panel goes away

  const run = async (dry: boolean) => {
    if (!file || !password) return;
    setBusy(true);
    setErr(null);
    try {
      const content_b64 = await readB64(file);
      const res = await api<ImportPreview>("/api/portfolio/import/cas", {
        method: "POST", body: JSON.stringify({ filename: file.name, content_b64, password, dry_run: dry }),
      });
      setPreview(res);
      if (!dry) {
        setPassword("");
        setFile(null);
        onDone();
      }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="CAS statement (CAMS / KFintech / NSDL / CDSL)" icon={<FileText className="size-4" />}
      help="The Consolidated Account Statement from CAMS or KFintech (camsonline.com → Statements → CAS, 'Detailed' with transactions). It covers every mutual fund folio in your PAN. NSDL/CDSL statements are read for a units check only (they have no transactions).">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="PDF file">
          <input type="file" accept="application/pdf,.pdf" className={cx(inputClass, "w-full py-1.5 file:mr-2 file:rounded file:border-0 file:bg-background-subtle file:px-2 file:text-xs")}
            onChange={(e) => { setFile(e.target.files?.[0] ?? null); setPreview(null); }} />
        </Field>
        <Field label={<span className="inline-flex items-center gap-1"><KeyRound className="size-3" />PDF password</span>}
          hint="Usually your PAN in capitals. Sent once with this upload, used to open the file, then dropped: never stored or logged.">
          <input type="password" autoComplete="off" spellCheck={false} value={password} onChange={(e) => setPassword(e.target.value)}
            className={cx(inputClass, "w-full")} placeholder="Type the PDF password" />
        </Field>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="secondary" icon={<RefreshCw className="size-3.5" />} disabled={!file || !password || busy} onClick={() => run(true)}>Preview</Button>
        <Button icon={<Upload className="size-3.5" />} disabled={!file || !password || busy || !preview?.dry_run || preview.holdings_only} onClick={() => run(false)}>Import</Button>
      </div>
      {err && <div className="mt-3"><ErrorNote error={err} /></div>}
      {preview && <div className="mt-4"><PreviewView p={preview} /></div>}
    </Card>
  );
}

function TradebookImport({ onDone }: { onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [broker, setBroker] = useState<"auto" | "zerodha" | "groww" | "upstox">("auto");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const run = async (dry: boolean) => {
    if (!file) return;
    setBusy(true);
    setErr(null);
    try {
      const content_b64 = await readB64(file);
      const res = await api<ImportPreview>("/api/portfolio/import/tradebook", {
        method: "POST", body: JSON.stringify({ filename: file.name, content_b64, broker: broker === "auto" ? null : broker, dry_run: dry }),
      });
      setPreview(res);
      if (!dry) { setFile(null); onDone(); }
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card title="Broker tradebook or holdings (equity)" icon={<FileSpreadsheet className="size-4" />}
      help="Zerodha Console → Reports → Tradebook (CSV); Groww → Stocks → Order history (XLSX); Upstox → Reports → Trade book. The broker is recognised from the column headers. F&O rows are skipped. Tradebooks have no charges, bonus/split shares or IPO allotments: add those below or sync corporate actions.">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="CSV or XLSX file">
          <input type="file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" className={cx(inputClass, "w-full py-1.5 file:mr-2 file:rounded file:border-0 file:bg-background-subtle file:px-2 file:text-xs")}
            onChange={(e) => { setFile(e.target.files?.[0] ?? null); setPreview(null); }} />
        </Field>
        <Field label="Broker">
          <Segmented value={broker} onChange={setBroker}
            options={[{ value: "auto", label: "Detect" }, { value: "zerodha", label: "Zerodha" }, { value: "groww", label: "Groww" }, { value: "upstox", label: "Upstox" }]} />
        </Field>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="secondary" icon={<RefreshCw className="size-3.5" />} disabled={!file || busy} onClick={() => run(true)}>Preview</Button>
        <Button icon={<Upload className="size-3.5" />} disabled={!file || busy || !preview?.dry_run} onClick={() => run(false)}>Import</Button>
      </div>
      {err && <div className="mt-3"><ErrorNote error={err} /></div>}
      {preview && <div className="mt-4"><PreviewView p={preview} /></div>}
    </Card>
  );
}


// ---------------------------------------------------------------- drop zone
// One place for every statement: drop (anywhere on this tab) or pick several files; each is routed by its type. A PDF
// waits for its password in a masked field on its own row; spreadsheets preview at once. Nothing is written until
// the row's Import is pressed. An income-tax AIS (a .json, or a PDF named like one; a PDF row can be switched) goes to
// /api/portfolio/ais and shows up under Tax → AIS check.
type FileKind = "pdf" | "sheet" | "ais" | "bad";
type Item = { id: number; file: File; kind: FileKind; password: string; busy: boolean; err: string | null; preview: ImportPreview | null; done: boolean; ais: AisImport | null; fy: string };

const isPdf = (f: File) => f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf";
const AIS_PW_HINT = "Your PAN in lower case followed by your date of birth as ddmmyyyy (e.g. abcde1234f01011990). Sent once to this app's local API, then dropped.";
const FY_CHOICES = Array.from({ length: 6 }, (_, i) => new Date().getFullYear() + 1 - i); // FYs by their end year

function kindOf(f: File): FileKind {
  const n = f.name.toLowerCase();
  if (n.endsWith(".json")) return "ais";
  if (isPdf(f) && /(^|[^a-z])ais([^a-z]|$)|annual.?information/.test(n)) return "ais";
  if (isPdf(f)) return "pdf";
  if (n.endsWith(".xlsx") || n.endsWith(".csv")) return "sheet";
  return "bad";
}

function QuickImport({ onDone }: { onDone: () => void }) {
  const [items, setItems] = useState<Item[]>([]);
  const [over, setOver] = useState(false);
  const seq = useRef(0);
  const input = useRef<HTMLInputElement>(null);
  const patch = (id: number, p: Partial<Item>) => setItems((xs) => xs.map((x) => (x.id === id ? { ...x, ...p } : x)));

  const send = useCallback(async (it: Item, dry: boolean) => {
    if ((it.kind === "pdf" || (it.kind === "ais" && isPdf(it.file))) && !it.password) return;
    patch(it.id, { busy: true, err: null });
    try {
      const content_b64 = await readB64(it.file);
      if (it.kind === "ais") {
        const body = { filename: it.file.name, content_b64, dry_run: dry, ...(isPdf(it.file) ? { password: it.password } : {}), ...(it.fy ? { fy: Number(it.fy) } : {}) };
        const res = await api<AisImport>("/api/portfolio/ais/import", { method: "POST", body: JSON.stringify(body) });
        patch(it.id, { busy: false, ais: res, ...(dry ? {} : { done: true, password: "" }) });
        if (!dry) onDone();
        return;
      }
      const res = it.kind === "pdf"
        ? await api<ImportPreview>("/api/portfolio/import/cas", { method: "POST", body: JSON.stringify({ filename: it.file.name, content_b64, password: it.password, dry_run: dry }) })
        : await api<ImportPreview>("/api/portfolio/import/tradebook", { method: "POST", body: JSON.stringify({ filename: it.file.name, content_b64, broker: null, dry_run: dry }) });
      patch(it.id, { busy: false, preview: res, ...(dry ? {} : { done: true, password: "" }) });
      if (!dry) onDone();
    } catch (e) {
      patch(it.id, { busy: false, err: (e as Error).message });
    }
  }, [onDone]);

  const add = useCallback((files: FileList | File[] | null) => {
    const fresh = Array.from(files ?? []).map((file): Item => {
      const kind = kindOf(file);
      return { id: ++seq.current, file, kind, password: "", busy: false, preview: null, done: false, ais: null, fy: "",
        err: kind === "bad" ? (file.name.toLowerCase().endsWith(".xls") ? "Old .xls format: open it and save as .xlsx or .csv, then drop it again." : "Not a PDF, XLSX, CSV or AIS JSON statement.") : null };
    });
    if (!fresh.length) return;
    setItems((xs) => [...xs, ...fresh]);
    fresh.filter((i) => i.kind === "sheet" || (i.kind === "ais" && !isPdf(i.file))).forEach((i) => void send(i, true));
  }, [send]);

  // A file dropped anywhere on this tab comes here instead of the browser opening it (and leaving the app).
  useEffect(() => {
    const onOver = (e: DragEvent) => { if (e.dataTransfer?.types.includes("Files")) { e.preventDefault(); setOver(true); } };
    const onLeave = (e: DragEvent) => { if (!e.relatedTarget) setOver(false); };
    const onDrop = (e: DragEvent) => {
      if (!e.dataTransfer?.files.length) return;
      e.preventDefault();
      setOver(false);
      add(e.dataTransfer.files);
    };
    window.addEventListener("dragover", onOver);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("drop", onDrop);
    return () => { window.removeEventListener("dragover", onOver); window.removeEventListener("dragleave", onLeave); window.removeEventListener("drop", onDrop); };
  }, [add]);
  useEffect(() => () => setItems([]), []); // passwords go with the rows when the tab closes

  return (
    <Card title="Import statements" icon={<FileUp className="size-4" />}
      help="Drop or choose any mix: Groww/Zerodha/Upstox order history or tradebook (XLSX/CSV), a broker holdings statement (XLSX/CSV), a CAMS/KFintech CAS or an NSDL/CDSL e-CAS (PDF), or your income-tax AIS (JSON or PDF) for the Tax → AIS check. Import the order history before the holdings statement.">
      <button type="button" onClick={() => input.current?.click()}
        className={cx("flex w-full flex-col items-center justify-center gap-1.5 rounded-xl border-2 border-dashed px-4 py-8 text-center transition-colors",
          over ? "border-brand bg-brand-soft" : "border-border hover:border-brand/60 hover:bg-background-subtle")}>
        <Upload className={cx("size-6", over ? "text-brand" : "text-muted")} />
        <span className="text-sm font-medium">{over ? "Drop to add" : "Drop statements here, or click to choose files"}</span>
        <span className="text-xs text-muted">PDF (CAS / e-CAS / AIS) · XLSX or CSV (order history, tradebook, holdings) · AIS JSON · several at once</span>
      </button>
      <input ref={input} type="file" multiple hidden accept=".pdf,.xlsx,.csv,.json,application/json,application/pdf,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
      {items.length > 0 && (
        <ul className="mt-4 space-y-3">
          {items.map((it) => (
            <li key={it.id} className="rounded-lg border border-border p-3">
              <div className="flex flex-wrap items-center gap-2">
                {isPdf(it.file) ? <FileText className="size-4 text-muted" /> : <FileSpreadsheet className="size-4 text-muted" />}
                <span className="min-w-0 flex-1 truncate text-sm" title={it.file.name}>{it.file.name}</span>
                {isPdf(it.file) && !it.done && (
                  <Segmented label="Statement type" value={it.kind === "ais" ? "ais" : "pdf"} onChange={(v) => patch(it.id, { kind: v, preview: null, ais: null, err: null })}
                    options={[{ value: "pdf", label: "CAS" }, { value: "ais", label: "AIS" }]} />
                )}
                {it.done && <Badge tone="gain">imported</Badge>}
                {it.busy && <span className="text-xs text-muted">reading…</span>}
                <Button variant="ghost" aria-label="Remove from list" icon={<X className="size-3.5" />} onClick={() => setItems((xs) => xs.filter((x) => x.id !== it.id))} />
              </div>
              {it.kind === "ais" && !it.done && it.err?.startsWith("fy:") && (
                <div className="mt-2 flex flex-wrap items-end gap-2">
                  <Field label="Financial year of this AIS">
                    <select value={it.fy} onChange={(e) => patch(it.id, { fy: e.target.value })} className={cx(inputClass, "w-40")}>
                      <option value="">Choose…</option>
                      {FY_CHOICES.map((y) => <option key={y} value={y}>FY {y - 1}-{String(y % 100).padStart(2, "0")}</option>)}
                    </select>
                  </Field>
                  <Button variant="secondary" icon={<RefreshCw className="size-3.5" />} disabled={!it.fy || it.busy} onClick={() => void send(it, true)}>Preview</Button>
                </div>
              )}
              {isPdf(it.file) && it.kind !== "bad" && !it.done && (
                <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); void send(it, true); }}>
                  <Field label={<span className="inline-flex items-center gap-1"><KeyRound className="size-3" />PDF password</span>}
                    hint={it.kind === "ais" ? AIS_PW_HINT : "Usually your PAN in capitals (NSDL/CDSL: PAN, sometimes with your date of birth). Sent once to this app's local API, then dropped."}>
                    <input type="password" name={`pdf-pw-${it.id}`} autoComplete="new-password" spellCheck={false} value={it.password}
                      onChange={(e) => patch(it.id, { password: e.target.value })} className={cx(inputClass, "w-64")} placeholder="Type the PDF password" />
                  </Field>
                  <Button type="submit" variant="secondary" icon={<RefreshCw className="size-3.5" />} disabled={!it.password || it.busy}>Preview</Button>
                </form>
              )}
              {it.preview?.dry_run && !it.done && (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {it.kind === "pdf" && it.preview.holdings_only
                    ? <span className="text-xs text-muted">A depository statement (NSDL/CDSL) has no transactions: it is used as a units check only, nothing to import.</span>
                    : <Button icon={<Upload className="size-3.5" />} disabled={it.busy || it.preview.already_imported != null} onClick={() => void send(it, false)}>Import</Button>}
                  {it.preview.already_imported != null && <span className="text-xs text-muted">already imported</span>}
                </div>
              )}
              {it.ais && (
                <div className="mt-2 space-y-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone="info">AIS FY {it.ais.fy - 1}-{String(it.ais.fy % 100).padStart(2, "0")}</Badge>
                    <span className="num">{it.ais.rows} rows kept · {it.ais.ignored} other rows not used</span>
                    <span className="num">{it.ais.check.counts.mismatch} differ · {it.ais.check.counts.only_ais} only in AIS · {it.ais.check.counts.only_app} only in app · {it.ais.check.counts.matched} match</span>
                    {it.ais.already_imported != null && <Badge tone="warn">already imported</Badge>}
                    {it.ais.dry_run
                      ? <Button icon={<Upload className="size-3.5" />} disabled={it.busy} onClick={() => void send(it, false)}>{it.ais.already_imported != null ? "Import again" : "Import"}</Button>
                      : <span className="text-gain">saved: see Tax → AIS check</span>}
                  </div>
                  {it.ais.warnings.map((w) => <p key={w} className="text-muted">{w}</p>)}
                  <p className="text-muted">Kept: category, source name/TAN, security, date and amounts. Not kept: PAN, name, address, account numbers or the file.</p>
                </div>
              )}
              {it.err && !(it.kind === "ais" && it.err.startsWith("fy:")) && <div className="mt-2"><ErrorNote error={it.err} /></div>}
              {it.preview && <div className="mt-3"><PreviewView p={it.preview} /></div>}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

const KINDS = [
  { value: "buy", label: "Buy" }, { value: "sell", label: "Sell" }, { value: "dividend", label: "Dividend" },
  { value: "bonus", label: "Bonus" }, { value: "split", label: "Split" },
] as const;
type Kind = (typeof KINDS)[number]["value"];

function ManualForm({ onDone }: { onDone: () => void }) {
  const [f, setF] = useState({ asset_type: "stock", name: "", nse_symbol: "", scheme_code: "", account: "Manual", day: "", kind: "buy" as Kind,
    quantity: "", price: "", amount: "", charges: "", stt_paid: true, a: "1", b: "1", from: "10", to: "1", note: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setF((x) => ({ ...x, [k]: e.target.type === "checkbox" ? (e.target as HTMLInputElement).checked : e.target.value }));
  const submit = async () => {
    setBusy(true); setErr(null); setOk(null);
    const meta = f.kind === "bonus" ? { a: f.a, b: f.b } : f.kind === "split" ? { from: f.from, to: f.to } : {};
    const nz = (v: string) => (v.trim() ? v.trim() : null);
    try {
      await api("/api/portfolio/transactions", {
        method: "POST",
        body: JSON.stringify({ asset_type: f.asset_type, name: f.name, nse_symbol: nz(f.nse_symbol), scheme_code: nz(f.scheme_code),
          account: f.account || "Manual", day: f.day, kind: f.kind, quantity: nz(f.quantity), price: nz(f.price), amount: nz(f.amount),
          charges: nz(f.charges) ?? "0", stt_paid: f.stt_paid, note: nz(f.note), meta }),
      });
      setOk(`Added a ${f.kind} of ${f.name}`);
      setF((x) => ({ ...x, quantity: "", price: "", amount: "", charges: "", note: "" }));
      onDone();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const trade = f.kind === "buy" || f.kind === "sell";
  return (
    <Card title="Add a transaction by hand" icon={<PenLine className="size-4" />}
      help="For anything a file does not have: IPO allotments, gifts, an older purchase, a bonus or split. The same name + symbol + account adds to an existing holding.">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Kind">
          <select value={f.kind} onChange={set("kind")} className={cx(inputClass, "w-full")}>
            {KINDS.map((k) => <option key={k.value} value={k.value}>{k.label}</option>)}
          </select>
        </Field>
        <Field label="Type">
          <select value={f.asset_type} onChange={set("asset_type")} className={cx(inputClass, "w-full")}>
            <option value="stock">Stock / ETF / SGB</option><option value="mf">Mutual fund</option><option value="other">Other</option>
          </select>
        </Field>
        <Field label="Name"><input value={f.name} onChange={set("name")} className={cx(inputClass, "w-full")} placeholder="e.g. Infosys" /></Field>
        {f.asset_type === "mf"
          ? <Field label="AMFI scheme code"><input value={f.scheme_code} onChange={set("scheme_code")} className={cx(inputClass, "w-full num")} placeholder="e.g. 120503" /></Field>
          : <Field label="NSE symbol"><input value={f.nse_symbol} onChange={set("nse_symbol")} className={cx(inputClass, "w-full num")} placeholder="e.g. INFY" /></Field>}
        <Field label="Account"><input value={f.account} onChange={set("account")} className={cx(inputClass, "w-full")} /></Field>
        <Field label={f.kind === "bonus" || f.kind === "split" ? "Ex-date" : "Trade date"}><input type="date" value={f.day} onChange={set("day")} className={cx(inputClass, "w-full num")} /></Field>
        {trade && <>
          <Field label="Units"><input inputMode="decimal" value={f.quantity} onChange={set("quantity")} className={cx(inputClass, "w-full num")} /></Field>
          <Field label="Price per unit (₹)"><input inputMode="decimal" value={f.price} onChange={set("price")} className={cx(inputClass, "w-full num")} /></Field>
          <Field label="Charges (₹)" hint="Brokerage and stamp duty add to the cost."><input inputMode="decimal" value={f.charges} onChange={set("charges")} className={cx(inputClass, "w-full num")} /></Field>
          <Field label={<span className="inline-flex items-center gap-1">STT paid <InfoTip>The 20 % / 12.5 % equity rates need STT on the sale (and, for shares, on the purchase, except IPO, bonus, rights and gift shares, which still qualify). Untick for an off-market sale.</InfoTip></span>}>
            <label className="flex h-9 items-center gap-2 text-sm"><input type="checkbox" checked={f.stt_paid} onChange={set("stt_paid")} /> Yes</label>
          </Field>
        </>}
        {f.kind === "dividend" && <Field label="Amount (₹)"><input inputMode="decimal" value={f.amount} onChange={set("amount")} className={cx(inputClass, "w-full num")} /></Field>}
        {f.kind === "bonus" && <Field label="Ratio (new : held)" hint="1 : 1 = one new share for every share held">
          <div className="flex items-center gap-1.5"><input value={f.a} onChange={set("a")} className={cx(inputClass, "w-16 num")} /> : <input value={f.b} onChange={set("b")} className={cx(inputClass, "w-16 num")} /></div></Field>}
        {f.kind === "split" && <Field label="Face value from → to (₹)" hint="10 → 2 turns 1 share into 5">
          <div className="flex items-center gap-1.5"><input value={f.from} onChange={set("from")} className={cx(inputClass, "w-16 num")} /> → <input value={f.to} onChange={set("to")} className={cx(inputClass, "w-16 num")} /></div></Field>}
      </div>
      <div className="mt-3 flex items-center gap-3">
        <Button onClick={submit} disabled={busy || !f.name.trim() || !f.day}>Add</Button>
        {ok && <span className="text-xs text-gain">{ok}</span>}
      </div>
      {err && <div className="mt-3"><ErrorNote error={err} /></div>}
    </Card>
  );
}

function ImportsList({ refresh }: { refresh: number }) {
  const { data, error, reload } = useApi<ImportRow[]>(`/api/portfolio/imports?r=${refresh}`);
  const [confirm, setConfirm] = useState<number | null>(null);
  const del = async (id: number) => {
    await api(`/api/portfolio/imports/${id}`, { method: "DELETE" });
    setConfirm(null);
    reload();
  };
  if (error) return <ErrorNote error={error} onRetry={reload} />;
  if (!data?.length) return null;
  return (
    <Card title="Imported files" subtitle="Deleting an import removes every transaction it added; lots are rebuilt.">
      <Table label="Imported files">
        <thead><tr><th>File</th><th>Source</th><th>Imported</th><th className="text-right">Rows added</th><th>Units check</th><th /></tr></thead>
        <tbody>
          {data.map((i) => (
            <tr key={i.id}>
              <td className="max-w-[16rem] truncate">{i.filename}</td>
              <td><Badge tone="info">{i.source}</Badge></td>
              <td className="text-xs text-muted">{when(i.created_at)}</td>
              <td className="num text-right">{i.summary.added ?? "—"}</td>
              <td>{i.summary.reconciliation?.length ? (i.summary.reconciled ? <Badge tone="gain">matches</Badge> : <Badge tone="loss">mismatch</Badge>) : <span className="text-xs text-muted">n/a</span>}</td>
              <td className="text-right">
                {confirm === i.id
                  ? <span className="inline-flex gap-1"><Button variant="danger" onClick={() => del(i.id)}>Delete</Button><Button variant="ghost" onClick={() => setConfirm(null)}>Cancel</Button></span>
                  : <Button variant="ghost" aria-label="Delete import" icon={<Trash2 className="size-3.5" />} onClick={() => setConfirm(i.id)} />}
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

export function ImportPanel({ onChanged }: { onChanged: () => void }) {
  const [refresh, setRefresh] = useState(0);
  const [sync, setSync] = useState<string | null>(null);
  const done = () => { setRefresh((r) => r + 1); onChanged(); };
  const runSync = async () => {
    setSync("Checking NSE corporate actions…");
    try {
      const r = await api<{ checked: number; added: Record<string, string[]>; errors: string[] }>("/api/portfolio/actions/sync", { method: "POST" });
      const n = Object.values(r.added).flat().length;
      setSync(`${r.checked} stocks checked · ${n} split/bonus event${n === 1 ? "" : "s"} added${r.errors.length ? ` · ${r.errors.length} failed` : ""}`);
      done();
    } catch (e) {
      setSync((e as Error).message);
    }
  };
  return (
    <div className="space-y-4">
      <Callout tone="info" icon={<ShieldCheck className="size-4" />} title="Your data stays on this machine">
        Files are parsed by the local API and kept under <span className="num">data/portfolio/</span> (gitignored); nothing is sent to an LLM. CAS passwords are never stored.
      </Callout>
      <QuickImport onDone={done} />
      <div className="grid gap-4 xl:grid-cols-2">
        <CasImport onDone={done} />
        <TradebookImport onDone={done} />
      </div>
      <ManualForm onDone={done} />
      <Card title="Splits and bonuses" subtitle="Tradebooks don't include them. This reads NSE's corporate actions for your stocks and adds each split or bonus after your first purchase, once.">
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="secondary" icon={<RefreshCw className="size-3.5" />} onClick={runSync}>Sync corporate actions</Button>
          {sync && <span className="text-xs text-muted">{sync}</span>}
        </div>
      </Card>
      <ImportsList refresh={refresh} />
    </div>
  );
}

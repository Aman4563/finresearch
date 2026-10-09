"use client";

// Profile → Connections: read-only links to the user's broker accounts (portfolio/connectors) and the statement
// inbox. Secrets are masked by the API and kept server-side when the masked value is sent back. No order can ever
// be placed from here: the connectors only read.
import {
  AlertTriangle, ArrowUpRight, CheckCircle2, ChevronDown, Copy, FolderInput, Link2, Loader2, LogIn, PlugZap, RefreshCw,
  Save, ShieldCheck, Trash2,
} from "lucide-react";
import { useEffect, useState } from "react";

import { Switch } from "@/components/profile/common";
import { SecretInput } from "@/components/profile/notifications";
import { Badge, Button, Callout, Card, cx, ErrorNote, Field, InfoTip, inputClass, Skeleton } from "@/components/ui";
import { api, useApi, when } from "@/lib/api";

type FieldSpec = { name: string; label: string; secret: boolean; required: boolean; help: string };
type Position = { symbol: string; quantity: number; product: string | null; exchange: string | null; avg_price: number | null; pnl: number | null };
export type Connection = {
  key: string; label: string; account?: string; auth_kind: "totp" | "oauth" | "token" | "password_totp" | "none";
  capabilities: string[]; docs_url?: string; cost?: string; token_note?: string; trades_note?: string; verified?: "docs" | "partial";
  fields: FieldSpec[]; configured: boolean; enabled: boolean; auto_sync: boolean; config: Record<string, string | boolean>;
  status: "connected" | "ready" | "reconnect" | "error" | "incomplete" | "not_connected" | "off"; next_step: string;
  token_set: boolean; connected_as: string | null; token_expires_at: string | null; last_sync_at: string | null;
  last_error: string | null; positions: Position[]; funds: Record<string, number | null> | null; callback_url?: string;
  // differences/reconciled null: the holdings were not read, so the reconciliation is unknown (never "0 differences")
  last_summary: { added: number; duplicates: number; updated?: number; differences: number | null; baselines: number; conflicts: number; reconciled: boolean | null } | null;
};
type RecRow = { name: string; ikey: string; account: string; broker_units: string; app_units: string; diff: string; ok: boolean; status: string };
export type SyncLog = {
  id: number; key: string; trigger: string; status: string; started_at: string; error: string | null;
  summary: {
    added?: number; duplicates?: number; baselines?: { name: string; units: string }[]; conflicts?: { name: string; day: string; why: string }[];
    cross_source?: { name: string; day: string; matched: string; sources: string[] }[]; reconciliation?: RecRow[]; differences?: number | null;
    baseline_skipped?: { name: string; held_in: string[]; why: string }[]; file?: string; kind?: string; note?: string; status?: string;
  };
};
type Listing = { connections: Connection[]; inbox: { path: string; pending: { name: string; bytes: number }[] } };

const CAP: Record<string, string> = { holdings: "Holdings", positions: "Positions", trades: "Trades", mf: "Mutual funds", funds: "Funds",
  cas: "CAS PDFs", tradebook: "Tradebooks", holdings_statement: "Holdings statements" };
const TONE: Record<Connection["status"], "gain" | "info" | "warn" | "loss" | "neutral"> = {
  connected: "gain", ready: "info", reconnect: "warn", error: "loss", incomplete: "warn", not_connected: "neutral", off: "neutral" };
const STATUS_LABEL: Record<Connection["status"], string> = {
  connected: "connected", ready: "ready", reconnect: "log in again", error: "error", incomplete: "incomplete", not_connected: "not connected", off: "off" };
const inr = (n: number | null | undefined) => (n == null ? "—" : `₹${n.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`);

function Copyable({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <code className="num min-w-0 flex-1 truncate rounded bg-background-subtle px-2 py-1 text-[11px]">{text}</code>
      <button type="button" aria-label="Copy" className="shrink-0 text-muted hover:text-foreground"
        onClick={() => { navigator.clipboard?.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); }}>
        {done ? <CheckCircle2 className="size-3.5 text-gain" /> : <Copy className="size-3.5" />}
      </button>
    </span>
  );
}

function Differences({ log }: { log: SyncLog | undefined }) {
  if (!log) return null;
  const s = log.summary;
  const bad = (s.reconciliation ?? []).filter((r) => !r.ok);
  const items = [
    ...bad.map((r) => `${r.name}: broker ${r.broker_units}, app ${r.app_units} (${r.status.replaceAll("_", " ")})`),
    ...(s.conflicts ?? []).map((c) => `${c.name} ${c.day}: ${c.why}`),
    ...(s.baseline_skipped ?? []).map((b) => `${b.name}: ${b.why} (${b.held_in.join(", ")})`),
  ];
  if (!items.length) return null;
  return (
    <details className="rounded-lg bg-warn-soft/40 px-3 py-2 text-xs">
      <summary className="cursor-pointer font-medium text-warn">{items.length} difference{items.length > 1 ? "s" : ""} to review (nothing was changed)</summary>
      <ul className="mt-1.5 list-disc space-y-0.5 pl-4 text-muted">{items.slice(0, 30).map((t) => <li key={t}>{t}</li>)}</ul>
    </details>
  );
}

type MarketData = {
  active: boolean; session: string; phase: string; instruments_day: string | null;
  supplies: { kind: string; label: string }[]; last_answered: Record<string, string>;
  calls_today: { day: string; total: number; categories: { category: string; label: string; calls: number; per_second: number | null; per_minute: number | null; daily_cap: number | null }[] };
};

// Groww's paid Trade API also supplies market data (#267): which data it is supplying now and today's calls by Groww's
// rate categories. Counts and labels only: the API never returns a key, secret or token.
function GrowwMarketData() {
  const { data } = useApi<MarketData>("/api/connections/groww/market-data", 60000);
  if (!data) return null;
  return (
    <div className="space-y-1.5 rounded-lg bg-background-subtle/60 px-3 py-2 text-xs ring-1 ring-inset ring-border" data-testid="groww-market-data">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium text-foreground">Market data from Groww</span>
        <Badge tone={data.active ? "gain" : "neutral"} dot={data.active}>{data.active ? "in use" : "not in use: NSE/BSE/AMFI"}</Badge>
        <InfoTip>When the Groww session is valid, prices, index values, recent daily closes and option chains come from Groww first; anything Groww cannot answer falls back to NSE, BSE or AMFI, and each price says where it came from.</InfoTip>
      </div>
      <ul className="space-y-0.5 text-muted">
        {data.supplies.map((s) => (
          <li key={s.kind}>{s.label}{data.last_answered[s.kind] && <span className="num"> · last {when(data.last_answered[s.kind])}</span>}</li>
        ))}
      </ul>
      <p className="text-muted">
        Calls today: <span className="num text-foreground">{data.calls_today.total}</span>
        {" "}({data.calls_today.categories.map((c) => `${c.label} ${c.calls}${c.daily_cap ? ` of ${c.daily_cap}` : ""}`).join(" · ")})
      </p>
    </div>
  );
}

function BrokerRow({ c, log, onChanged }: { c: Connection; log?: SyncLog; onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [cfg, setCfg] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reseed the form from the saved (masked) settings
    setCfg(Object.fromEntries(c.fields.map((f) => [f.name, String(c.config[f.name] ?? "")])));
  }, [c]);
  const dirty = c.fields.some((f) => (cfg[f.name] ?? "") !== String(c.config[f.name] ?? ""));

  const act = async (what: string, fn: () => Promise<unknown>) => {
    setBusy(what); setErr(null); setNote(null);
    try {
      await fn();
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const save = () => act("save", async () => {
    const body: Record<string, string> = {};
    for (const f of c.fields) if ((cfg[f.name] ?? "") !== String(c.config[f.name] ?? "")) body[f.name] = cfg[f.name] ?? "";
    await api(`/api/connections/${c.key}`, { method: "PUT", body: JSON.stringify({ config: body, enabled: true }) });
    setNote("Saved.");
  });
  const login = () => act("login", async () => {
    if (c.auth_kind === "oauth") {
      const r = await api<{ url: string }>(`/api/connections/${c.key}/login-url`);
      window.location.href = r.url;
      return;
    }
    await api(`/api/connections/${c.key}/login`, { method: "POST" });
    setNote("Logged in.");
  });
  const sync = () => act("sync", async () => {
    const r = await api<SyncLog>(`/api/connections/${c.key}/sync`, { method: "POST" });
    setNote(r.status === "ok" ? `Synced: ${r.summary.added ?? 0} new trade(s), ${r.summary.baselines?.length ?? 0} baseline(s).` : `Sync ${r.status}: ${r.error ?? ""}`);
  });
  const setFlag = (k: "auto_sync" | "enabled", v: boolean) => act(k, () => api(`/api/connections/${c.key}`, { method: "PUT", body: JSON.stringify({ [k]: v }) }));
  const remove = () => {
    if (!confirm(`Forget the ${c.label} credentials and session? Imported transactions stay (delete their imports on the portfolio page).`)) return;
    act("remove", () => api(`/api/connections/${c.key}`, { method: "DELETE" }));
  };
  const loginLabel = c.auth_kind === "oauth" ? (c.token_set || c.status === "reconnect" ? "Reconnect" : "Connect") : "Log in now";
  const canLogin = c.configured && c.status !== "incomplete" && c.enabled;
  const canSync = c.configured && c.enabled && (c.status === "connected" || c.status === "ready" || c.status === "error");

  return (
    <section className="space-y-3 border-t border-border pt-4 first:border-t-0 first:pt-0">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
        <div className="flex min-w-0 flex-1 items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-brand-soft text-sm font-semibold text-brand">{c.label[0]}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{c.label}</span>
            <Badge tone={TONE[c.status]} dot={c.status === "connected"}>{STATUS_LABEL[c.status]}</Badge>
            {c.connected_as && <span className="num text-[11px] text-muted">as {c.connected_as}</span>}
            {c.verified === "partial" && <Badge tone="neutral">some fields unverified</Badge>}
          </div>
          <p className="mt-0.5 text-xs text-muted">{c.next_step}</p>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {c.capabilities.map((k) => <span key={k} className="rounded-md bg-background-subtle px-1.5 py-0.5 text-[10px] text-muted ring-1 ring-inset ring-border">{CAP[k] ?? k}</span>)}
          </div>
        </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 pl-12 sm:pl-0">
          {canLogin && (c.auth_kind === "oauth" || c.status !== "connected") && (
            <Button variant={c.status === "reconnect" ? "primary" : "secondary"} onClick={login} disabled={busy !== null || dirty}
              icon={busy === "login" ? <Loader2 className="size-3.5 animate-spin" /> : <LogIn className="size-3.5" />}>{loginLabel}</Button>
          )}
          {canSync && (
            <Button variant="secondary" onClick={sync} disabled={busy !== null || dirty}
              icon={busy === "sync" ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}>Sync now</Button>
          )}
          <Button variant="ghost" onClick={() => setOpen(!open)} aria-expanded={open}
            icon={<ChevronDown className={cx("size-3.5 transition", open && "rotate-180")} />}>{c.configured ? "Settings" : "Set up"}</Button>
        </div>
      </div>

      <div className="grid gap-x-6 gap-y-1 pl-12 text-xs text-muted sm:grid-cols-2">
        <span>Last sync: <span className="num text-foreground">{c.last_sync_at ? when(c.last_sync_at) : "never"}</span></span>
        <span>Session: <span className="text-foreground">{c.token_set && c.token_expires_at ? `until ${when(c.token_expires_at)}` : c.token_set ? "active" : "none"}</span></span>
        {c.last_summary && (
          <span className="sm:col-span-2">Last result: {c.last_summary.added} new trade(s), {c.last_summary.duplicates} already there,
            {c.last_summary.updated ? ` ${c.last_summary.updated} updated with later fills,` : ""}
            {" "}{c.last_summary.baselines} baseline(s){c.last_summary.reconciled == null || c.last_summary.differences == null
              ? <>, <span className="text-warn">holdings not read: not reconciled</span></>
              : c.last_summary.differences ? <>, <span className="text-warn">{c.last_summary.differences} difference(s)</span></> : ", reconciled"}.</span>
        )}
        {c.funds && (c.funds.cash != null || c.funds.net != null) && <span>Cash available: <span className="num text-foreground">{inr(c.funds.cash ?? c.funds.net)}</span></span>}
        {c.positions.length > 0 && <span>Open positions: <span className="text-foreground">{c.positions.map((p) => `${p.symbol} ${p.quantity}${p.product ? ` ${p.product}` : ""}`).slice(0, 4).join(", ")}</span></span>}
      </div>
      {(c.last_error || err) && <div className="pl-12"><ErrorNote error={err ?? c.last_error} /></div>}
      {note && <p className="pl-12 text-xs text-gain">{note}</p>}
      <div className="pl-12"><Differences log={log} /></div>
      {c.key === "groww" && c.configured && <div className="pl-12"><GrowwMarketData /></div>}

      {open && (
        <div className="ml-12 space-y-3 rounded-xl bg-background-subtle/60 p-3 ring-1 ring-inset ring-border animate-fade-in">
          <div className="grid gap-1 text-xs text-muted">
            {c.cost && <span><span className="font-medium text-foreground">Cost:</span> {c.cost}</span>}
            {c.token_note && <span><span className="font-medium text-foreground">Login:</span> {c.token_note}</span>}
            {c.trades_note && <span><span className="font-medium text-foreground">History:</span> {c.trades_note}</span>}
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            {c.fields.map((f) => f.secret ? (
              <SecretInput key={f.name} label={`${f.label}${f.required ? "" : " (optional)"}`} value={cfg[f.name] ?? ""} isSet={Boolean(c.config[`${f.name}_set`])}
                set={(v) => setCfg({ ...cfg, [f.name]: v })} hint={f.help} />
            ) : (
              <Field key={f.name} label={f.label} hint={f.help}>
                <input className={cx(inputClass, "num w-full")} value={cfg[f.name] ?? ""} autoComplete="off" spellCheck={false}
                  onChange={(e) => setCfg({ ...cfg, [f.name]: e.target.value })} />
              </Field>
            ))}
          </div>
          {c.auth_kind === "oauth" && c.callback_url && (
            <Field label={<span className="inline-flex items-center gap-1">Redirect URL for your {c.label} app<InfoTip>Paste this exactly into the app you create on the broker&apos;s developer site. After you log in there, the broker sends you back here with a one-time code that the app exchanges for the day&apos;s session.</InfoTip></span>}>
              <Copyable text={c.callback_url} />
            </Field>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={save} disabled={!dirty || busy !== null} icon={busy === "save" ? <Loader2 className="size-3.5 animate-spin" /> : <Save className="size-3.5" />}>Save</Button>
            {c.configured && (
              <>
                <div className="min-w-48"><Switch checked={c.auto_sync} onChange={(v) => setFlag("auto_sync", v)} label="Sync daily after the close" /></div>
                <div className="min-w-32"><Switch checked={c.enabled} onChange={(v) => setFlag("enabled", v)} label="On" /></div>
                <Button variant="danger" onClick={remove} disabled={busy !== null} icon={<Trash2 className="size-3.5" />}>Disconnect</Button>
              </>
            )}
            {c.docs_url && <a href={c.docs_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-brand hover:underline">API docs<ArrowUpRight className="size-3" /></a>}
          </div>
        </div>
      )}
    </section>
  );
}

function InboxRow({ c, inbox, log, onChanged }: { c: Connection; inbox: Listing["inbox"]; log: SyncLog[]; onChanged: () => void }) {
  const [pw, setPw] = useState(String(c.config.password ?? ""));
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- reseed from the saved (masked) password
    setPw(String(c.config.password ?? ""));
  }, [c]);
  const put = async (body: object, what: string) => {
    setBusy(what); setErr(null);
    try {
      await api(`/api/connections/cas_inbox`, { method: "PUT", body: JSON.stringify(body) });
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const scan = async () => {
    setBusy("scan"); setErr(null);
    try {
      const r = await api<{ imported: number; files: { file: string; status: string; note?: string }[] }>("/api/connections/cas_inbox/scan", { method: "POST" });
      setResult(r.files.length ? r.files.map((f) => `${f.file}: ${f.status}${f.note ? ` (${f.note})` : ""}`).join(" · ") : "Nothing waiting in the inbox.");
      onChanged();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  return (
    <section className="space-y-3 border-t border-border pt-4">
      <div className="flex flex-wrap items-start gap-3">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-accent-soft text-accent"><FolderInput className="size-4" /></span>
        <div className="min-w-0 flex-1">
          <Switch checked={c.enabled} onChange={(v) => put({ enabled: v }, "enabled")}
            label={<span className="inline-flex items-center gap-2">Statement inbox<Badge tone={c.enabled ? "gain" : "neutral"}>{c.enabled ? "watching" : "off"}</Badge></span>}
            description="No API needed: drop CAMS/KFintech CAS, NSDL/CDSL e-CAS PDFs, broker tradebooks or holdings statements into this folder. The monitor imports them every few minutes; nothing is uploaded anywhere." />
        </div>
        <Button variant="secondary" onClick={scan} disabled={busy !== null}
          icon={busy === "scan" ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}>Scan now</Button>
      </div>
      <div className="space-y-3 pl-12">
        <Field label="Folder"><Copyable text={inbox.path} /></Field>
        <div className="max-w-md">
          <SecretInput label="CAS PDF password (optional, saved only if you enter it)" value={pw} isSet={Boolean(c.config.password_set)} set={setPw}
            placeholder="usually your PAN in capitals" hint="Without it, PDFs wait in the folder until you import them on the portfolio page." />
          {pw !== String(c.config.password ?? "") && (
            <div className="mt-2 flex gap-2">
              <Button onClick={() => put({ config: { password: pw } }, "pw")} disabled={busy !== null} icon={<Save className="size-3.5" />}>Save password</Button>
              {c.config.password_set && <Button variant="ghost" onClick={() => put({ config: { clear_password: true } }, "pw")}>Remove saved password</Button>}
            </div>
          )}
        </div>
        {inbox.pending.length > 0 && <p className="text-xs text-muted">Waiting: <span className="text-foreground">{inbox.pending.map((f) => f.name).join(", ")}</span></p>}
        {result && <p className="text-xs text-muted">{result}</p>}
        <ErrorNote error={err} />
        {log.slice(0, 1).map((l) => <Differences key={l.id} log={l} />)}
      </div>
    </section>
  );
}

export function ConnectionsCard() {
  const { data, error, reload } = useApi<Listing>("/api/connections");
  const logs = useApi<SyncLog[]>("/api/connections/log?limit=30");
  const [banner, setBanner] = useState<{ tone: "gain" | "loss"; text: string } | null>(null);
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const ok = q.get("connected"), bad = q.get("connect_error");
    // eslint-disable-next-line react-hooks/set-state-in-effect -- read the broker's redirect result once, then clean the URL
    if (ok) setBanner({ tone: "gain", text: `Connected. Press Sync now to read your ${ok} account.` });
    if (bad) setBanner({ tone: "loss", text: bad });
    if (ok || bad) history.replaceState(null, "", `${window.location.pathname}#connections`);
  }, []);
  const refresh = () => { reload(); logs.reload(); };
  const latest = (key: string) => logs.data?.find((l) => l.key === key && l.trigger !== "inbox");
  const brokers = data?.connections.filter((c) => c.key !== "cas_inbox") ?? [];
  const inbox = data?.connections.find((c) => c.key === "cas_inbox");

  return (
    <Card icon={<PlugZap className="size-4" />} title="Connections"
      subtitle="Read-only links to your broker accounts, and a folder for statements. The portfolio updates after each sync."
      help="Credentials stay in your local database, are never shown again, never logged and never sent to the AI. The connectors can only read: there is no code in the app that places, changes or cancels an order.">
      <div className="space-y-4">
        {banner && <Callout tone={banner.tone === "gain" ? "info" : "warn"} icon={banner.tone === "gain" ? <CheckCircle2 className="size-4" /> : <AlertTriangle className="size-4" />}>{banner.text}</Callout>}
        <Callout tone="info" icon={<ShieldCheck className="size-4" />}>
          Read-only by design. A sync adds new trades and, for holdings with no history here, a baseline at the broker&apos;s average cost (purchase date unknown). It never edits your manual, CAS or CSV entries: differences are listed for you to review.
        </Callout>
        {error ? <ErrorNote error={error} onRetry={reload} /> : !data ? <Skeleton className="h-64 rounded-xl" /> : (
          <>
            {brokers.map((c) => <BrokerRow key={c.key} c={c} log={latest(c.key)} onChanged={refresh} />)}
            {inbox && <InboxRow c={inbox} inbox={data.inbox} log={(logs.data ?? []).filter((l) => l.key === "cas_inbox")} onChanged={refresh} />}
          </>
        )}
        <section className="border-t border-border pt-4">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="inline-flex items-center gap-1.5 text-sm font-semibold"><Link2 className="size-3.5" />Sync log</h3>
            <button type="button" onClick={logs.reload} aria-label="Refresh the sync log" className="text-muted hover:text-foreground"><RefreshCw className="size-3.5" /></button>
          </div>
          {logs.error ? <ErrorNote error={logs.error} onRetry={logs.reload} /> : !logs.data ? <Skeleton className="h-16 rounded-lg" /> : !logs.data.length ? (
            <p className="text-sm text-muted">No syncs yet.</p>
          ) : (
            <ul className="space-y-1.5">
              {logs.data.slice(0, 12).map((l) => (
                <li key={l.id} className="rounded-lg bg-background-subtle/60 px-3 py-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={l.status === "ok" ? "gain" : l.status === "partial" || l.status === "reconnect" ? "warn" : "loss"}>{l.status}</Badge>
                    <span className="font-medium">{l.key === "cas_inbox" ? "Inbox" : data?.connections.find((c) => c.key === l.key)?.label ?? l.key}</span>
                    <span className="min-w-0 flex-1 truncate text-muted">
                      {l.trigger} · {l.summary.file ? `${l.summary.file} ${l.summary.kind ?? ""}` : `${l.summary.added ?? 0} new, ${l.summary.baselines?.length ?? 0} baseline(s), ${l.summary.differences == null ? "reconciliation unknown" : `${l.summary.differences} difference(s)`}`}
                    </span>
                    <span className="num text-muted">{when(l.started_at)}</span>
                  </div>
                  {l.error && <p className="mt-1 break-words text-muted">{l.error}</p>}
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </Card>
  );
}

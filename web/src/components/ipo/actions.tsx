"use client";

import { Check, Copy, FlaskConical, Terminal } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button, Callout, ErrorNote, Modal, cx } from "@/components/ui";
import { api, type Issue, type ResearchKind } from "@/lib/api";

export const KIND_LABEL: Record<ResearchKind, string> = {
  ipo_report: "IPO",
  stock_report: "stock",
  fund_report: "fund",
  bond_report: "bond",
};

/** Start a research run after a confirmation (it uses the Claude plan window). With `issue` (an NSE issue not in the
 * store yet) the company is added first. */
export function ResearchButton({ slug, kind, issue, label, size = "sm", variant = "primary" }: {
  slug?: string; kind: ResearchKind; issue?: Issue; label?: string; size?: "sm" | "md"; variant?: "primary" | "secondary";
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = label ?? issue?.company ?? slug;
  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const company =
        slug ??
        (
          await api<{ slug: string }>("/api/companies", {
            method: "POST",
            body: JSON.stringify({ nse_symbol: issue!.symbol, name: issue!.company }),
          })
        ).slug;
      const r = await api<{ run_id: number }>("/api/runs", { method: "POST", body: JSON.stringify({ company, kind }) });
      router.push(`/runs/${r.run_id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };
  return (
    <>
      <Button size={size} variant={variant} icon={<FlaskConical className="size-3.5" />} onClick={() => setOpen(true)}>
        Research
      </Button>
      <Modal open={open} onClose={() => !busy && setOpen(false)} title={`Research ${name}?`}>
        <div className="space-y-3 p-4 text-sm">
          <p>
            This starts a full <b>{KIND_LABEL[kind]}</b> research run: agents collect the filings, check every figure against its
            source and write a cited report. It takes a while and runs in the background.
          </p>
          <Callout tone="warn" title="Uses your Claude plan window">
            A run consumes part of your 5-hour Claude window. Check Plan usage first if you are close to the limit.
          </Callout>
          <ErrorNote error={error} />
        </div>
        <div className="flex justify-end gap-2 border-t border-border px-4 py-3">
          <Button variant="ghost" onClick={() => setOpen(false)} disabled={busy}>Cancel</Button>
          <Button onClick={start} disabled={busy}>{busy ? "Starting…" : "Start research"}</Button>
        </div>
      </Modal>
    </>
  );
}

/** A command shown in mono with a copy button (BSE SME issues are researched from the CLI). */
export function CopyCommand({ command, className, compact }: { command: string; className?: string; compact?: boolean }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked: the command stays visible to copy by hand */
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      title={copied ? "Copied" : compact ? `${command}\n\nBSE SME filings are researched from the CLI: copy, then run it in your terminal.` : "Copy the command, then run it in your terminal"}
      className={cx(
        "group inline-flex max-w-full items-center gap-1.5 rounded-lg bg-background-subtle px-2 py-1 text-left ring-1 ring-inset ring-border transition hover:ring-border-strong",
        className,
      )}
    >
      <Terminal className="size-3.5 shrink-0 text-muted" />
      <code className="num min-w-0 truncate text-[11px] text-muted group-hover:text-foreground">{compact ? (copied ? "Copied" : "Copy CLI") : command}</code>
      {copied ? <Check className="size-3.5 shrink-0 text-gain animate-scale-in" /> : <Copy className="size-3.5 shrink-0 text-muted" />}
    </button>
  );
}

const slugify = (name: string) =>
  name.toLowerCase().replace(/\b(limited|ltd)\b\.?/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");

/** The CLI command that researches a BSE-only SME issue (the dashboard cannot fetch BSE filings itself). */
export const bseCommand = (i: Issue) =>
  `finresearch ipo run ${slugify(i.company)} --name "${i.company.replace(/"/g, "")}" --bse-ipo ${i.bse_ipo_no}`;

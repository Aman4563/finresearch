"use client";

import { BellRing } from "lucide-react";
import { useState } from "react";

import { Badge, Button, InfoTip, cx, inputClass } from "@/components/ui";
import { api, when } from "@/lib/api";

import { EXIT_KINDS, type ExitKind, type ExitRule } from "./book-types";

const STATUS_TONE = { armed: "info", fired: "warn", off: "neutral" } as const;

/** The exit rules of one position (paper or real) and a form to arm one more. Rules raise one alert; they never trade. */
export function ExitRules({ paperId, positionKey, rules, onChange }: {
  paperId?: number; positionKey?: string; rules: ExitRule[]; onChange: () => void;
}) {
  const [kind, setKind] = useState<ExitKind>("profit");
  const [value, setValue] = useState(String(EXIT_KINDS[0].preset));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mine = rules.filter((r) => (paperId != null ? r.paper_id === paperId : r.position_key === positionKey));
  const spec = EXIT_KINDS.find((k) => k.key === kind)!;

  const add = async () => {
    setBusy(true);
    try {
      await api("/api/fno/exit-rules", { method: "POST", body: JSON.stringify({ paper_id: paperId ?? null, position_key: positionKey ?? null, kind, threshold: value }) });
      setError(null);
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const off = async (id: number) => {
    await api(`/api/fno/exit-rules/${id}/off`, { method: "POST" }).catch(() => undefined);
    onChange();
  };

  return (
    <div className="space-y-2 text-xs">
      {mine.length > 0 && (
        <ul className="space-y-1">
          {mine.map((r) => (
            <li key={r.id} className="flex flex-wrap items-center gap-2">
              <Badge tone={STATUS_TONE[r.status]}>{r.status}</Badge>
              <span className="font-medium">{r.phrase}</span>
              <span className="text-muted">{r.note ?? "not checked yet"}{r.checked_at ? ` · ${when(r.checked_at)}` : ""}</span>
              {r.status === "armed" && <button type="button" className="rounded px-1 text-muted underline decoration-dotted underline-offset-2 hover:text-loss" onClick={() => off(r.id)}>turn off</button>}
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap items-center gap-1.5">
        <BellRing className="size-3.5 text-muted" />
        <select aria-label="Exit rule" className={cx(inputClass, "py-1 text-xs")} value={kind}
          onChange={(e) => { const k = e.target.value as ExitKind; setKind(k); setValue(String(EXIT_KINDS.find((x) => x.key === k)!.preset)); }}>
          {EXIT_KINDS.map((k) => <option key={k.key} value={k.key}>{k.label}</option>)}
        </select>
        <input aria-label="Threshold" type="number" min={0} step="any" className={cx(inputClass, "num w-20 py-1 text-xs")} value={value} onChange={(e) => setValue(e.target.value)} />
        <span className="text-muted">{spec.unit}</span>
        <InfoTip>{spec.help} One alert when it triggers; nothing is ever traded.</InfoTip>
        <Button variant="ghost" onClick={add} disabled={busy || value === ""}>Add alert rule</Button>
      </div>
      {error && <p role="alert" className="text-loss">{error}</p>}
    </div>
  );
}

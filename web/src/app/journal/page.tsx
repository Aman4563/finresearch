"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge, Button, Card, ErrorNote } from "@/components/ui";
import { api, type Decision, useApi, when } from "@/lib/api";

const ACTION: Record<string, string> = { APPLY: "done", "APPLY-CONDITIONAL": "unverified", SKIP: "blocked" };
const field = "w-20 rounded border border-border bg-background px-1 py-0.5 text-xs";

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
  const save = async () => {
    const body: Record<string, string | number | null> = {};
    for (const [k, x] of Object.entries(v)) {
      if (x === "") continue;
      body[k] = k.endsWith("_lots") ? Number(x) : x;
    }
    try {
      await api(`/api/decisions/${d.id}`, { method: "PATCH", body: JSON.stringify(body) });
      setError(null);
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const input = (k: keyof typeof v, label: string, type = "text") => (
    <label className="flex flex-col text-[0.65rem] text-muted">
      {label}
      <input className={field} type={type} value={v[k]} onChange={(e) => setV({ ...v, [k]: e.target.value })} />
    </label>
  );
  return (
    <div className="flex flex-wrap items-end gap-2">
      <label className="flex flex-col text-[0.65rem] text-muted">
        I
        <select className={field} value={v.user_action} onChange={(e) => setV({ ...v, user_action: e.target.value })}>
          <option value="">—</option>
          <option value="applied">applied</option>
          <option value="skipped">skipped</option>
        </select>
      </label>
      {input("applied_lots", "lots applied")}
      {input("allotted_lots", "lots allotted")}
      {input("issue_price", "issue ₹")}
      {input("listing_price", "listing ₹")}
      {input("exit_price", "exit ₹")}
      {input("exit_date", "exit date", "date")}
      <Button onClick={save}>Save</Button>
      <ErrorNote error={error} />
    </div>
  );
}

export default function Journal() {
  const { data, error, reload } = useApi<Decision[]>("/api/decisions");
  const followed = (data ?? []).filter((d) => typeof d.outcome.followed_suggestion === "boolean");
  const gains = (data ?? []).map((d) => d.outcome.exit_return_pct ?? d.outcome.listing_gain_pct).filter((x) => typeof x === "string");
  return (
    <Card title="Decision journal">
      <ErrorNote error={error} />
      {data && data.length > 0 && (
        <p className="mb-3 text-sm text-muted">
          {data.length} suggestion(s) · followed {followed.filter((d) => d.outcome.followed_suggestion).length}/{followed.length}
          {gains.length > 0 && ` · average realised return ${(gains.map(Number).reduce((a, b) => a + b, 0) / gains.length).toFixed(2)}%`}
        </p>
      )}
      <div className="space-y-4">
        {data?.map((d) => (
          <div key={d.id} className="border-t border-border pt-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-medium">{d.company_name}</span>
              <Link className="underline" href={`/runs/${d.run_id}/report`}>
                run {d.run_id}
              </Link>
              <Badge status={ACTION[d.action]}>{d.action}</Badge>
              <span>{d.lots} lot(s)</span>
              <span className="text-xs text-muted">{when(d.created_at)}</span>
              {Object.entries(d.outcome).map(([k, x]) => (
                <span key={k} className="rounded bg-background px-1.5 text-xs">
                  {k.replaceAll("_", " ")}: {String(x)}
                </span>
              ))}
            </div>
            <div className="mt-2">
              <OutcomeEditor d={d} onSaved={reload} />
            </div>
          </div>
        ))}
        {data?.length === 0 && <p className="text-sm text-muted">No suggestions yet. Open a report and ask for one.</p>}
      </div>
    </Card>
  );
}

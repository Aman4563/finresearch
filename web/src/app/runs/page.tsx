"use client";

import Link from "next/link";

import { Badge, Card, ErrorNote } from "@/components/ui";
import { type RunSummary, useApi, when } from "@/lib/api";

export default function Runs() {
  const { data, error } = useApi<RunSummary[]>("/api/runs", 10000);
  return (
    <Card title="Research runs">
      <ErrorNote error={error} />
      <table className="w-full text-sm">
        <thead className="text-left text-muted">
          <tr>
            <th className="py-1">Run</th>
            <th>Company</th>
            <th>Kind</th>
            <th>Status</th>
            <th>Steps</th>
            <th>Started</th>
            <th>Report</th>
          </tr>
        </thead>
        <tbody>
          {data?.map((r) => (
            <tr key={r.id} className="border-t border-border">
              <td className="py-2">
                <Link className="underline" href={`/runs/${r.id}`}>
                  #{r.id}
                </Link>
              </td>
              <td>{r.company_name}</td>
              <td className="text-muted">{r.kind}</td>
              <td>
                <Badge status={r.status} />
                {r.worker?.alive && <span className="ml-1 text-xs text-sky-600">● worker</span>}
              </td>
              <td className="text-xs text-muted">
                {Object.entries(r.steps)
                  .map(([k, v]) => `${v} ${k}`)
                  .join(" · ")}
              </td>
              <td className="text-muted">{when(r.created_at)}</td>
              <td>
                {r.kind !== "discovery" && r.status !== "running" && (
                  <Link className="underline" href={`/runs/${r.id}/report`}>
                    read
                  </Link>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Card>
  );
}

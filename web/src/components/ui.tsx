"use client";

import type { ReactNode } from "react";

const STATUS: Record<string, string> = {
  done: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
  verified: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-300",
  running: "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-300",
  pending: "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300",
  unverified: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  needs_review: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  paused: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  deferred: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300",
  blocked: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300",
  failed: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300",
  contradicted: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300",
  unsupported: "bg-rose-100 text-rose-800 dark:bg-rose-900/40 dark:text-rose-300",
};

export function Badge({ status, children }: { status: string; children?: ReactNode }) {
  return (
    <span className={`inline-block rounded px-1.5 py-0.5 text-xs font-medium ${STATUS[status] ?? STATUS.pending}`}>
      {children ?? status.replace("_", " ")}
    </span>
  );
}

export function Card({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card p-4 shadow-sm">
      {(title || actions) && (
        <div className="mb-3 flex items-center justify-between gap-3">
          {title && <h2 className="font-semibold">{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <p className="rounded border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-800 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-300">
      {error}
    </p>
  );
}

export function Button(props: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      className={`rounded bg-slate-900 px-2.5 py-1 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-50 dark:bg-slate-100 dark:text-slate-900 dark:hover:bg-white ${props.className ?? ""}`}
    />
  );
}

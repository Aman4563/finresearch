"use client";

import { useEffect, useState } from "react";

import { api, type WatchDetail, type WatchSummary } from "@/lib/api";

/** Full details (snapshots, jobs) for the active IPO watches in `watches`; re-fetched when the list changes. */
export function useWatchDetails(watches: WatchSummary[] | null, pollMs = 60000) {
  const [data, setData] = useState<WatchDetail[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ids = (watches ?? []).filter((w) => w.kind === "ipo" && w.active).map((w) => w.id).join(",");
  useEffect(() => {
    if (watches == null) return;
    let alive = true;
    const load = () => {
      const list = ids ? ids.split(",") : [];
      Promise.all(list.map((id) => api<WatchDetail>(`/api/watches/${id}`)))
        .then((d) => {
          if (alive) {
            setData(d);
            setError(null);
          }
        })
        .catch((e: Error) => alive && setError(e.message));
    };
    load();
    const t = setInterval(load, pollMs);
    return () => {
      alive = false;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `ids` captures what matters in `watches`
  }, [ids, watches == null, pollMs]);
  return { data, error };
}

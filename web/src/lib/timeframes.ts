"use client";

// Chart time frames and watch windows: the profile's defaults (suggest/profile.py Preferences.time_frames / .watch)
// and a page's last choice (remembered in this browser). A page starts from its last choice, else the profile default.

import { useCallback, useEffect, useState } from "react";

import { type Profile, useApi } from "@/lib/api";

export type ChartRange = "1D" | "5D" | "1M" | "3M" | "6M" | "1Y" | "3Y" | "5Y";
export type ChartInterval = "1m" | "5m" | "15m" | "30m" | "1h" | "1D";
export type ChartType = "line" | "candle";
export type ChartDefaults = { range: ChartRange; interval: ChartInterval; type: ChartType };

export type TimeFrames = {
  stock: ChartDefaults;
  index: ChartDefaults;
  quote_refresh_s: 0 | 15 | 30 | 60;
  fno_refresh_s: 0 | 60 | 120 | 300;
  ipo_book_refresh_s: 0 | 60 | 120 | 300;
};

export type WatchWindows = {
  ipo_check_times: string[];
  stock_daily_time: string;
  quiet_start: string | null;
  quiet_end: string | null;
};

export const INTRADAY_RANGES: ChartRange[] = ["1D", "5D"];
export const DAILY_RANGES: ChartRange[] = ["1M", "3M", "6M", "1Y", "3Y", "5Y"];
export const RANGE_DAYS: Record<ChartRange, number> = { "1D": 1, "5D": 5, "1M": 31, "3M": 92, "6M": 183, "1Y": 366, "3Y": 1096, "5Y": 1827 };

/** Which candle sizes fit a range (mirrors ChartDefaults in suggest/profile.py). 1m is a line: NSE's series is one
 * price a minute, so a 1-minute candle would have open = high = low = close. */
export function intervalsFor(range: ChartRange): ChartInterval[] {
  if (range === "1D") return ["1m", "5m", "15m", "30m", "1h"];
  if (range === "5D") return ["5m", "15m", "30m", "1h"];
  return ["1D"];
}

export function fitInterval(range: ChartRange, interval: ChartInterval): ChartInterval {
  const ok = intervalsFor(range);
  return ok.includes(interval) ? interval : range === "1D" ? "5m" : range === "5D" ? "15m" : "1D";
}

export const DEFAULT_TIME_FRAMES: TimeFrames = {
  stock: { range: "1Y", interval: "1D", type: "line" },
  index: { range: "1D", interval: "5m", type: "line" },
  quote_refresh_s: 30,
  fno_refresh_s: 60,
  ipo_book_refresh_s: 60,
};

export const DEFAULT_WATCH: WatchWindows = {
  ipo_check_times: ["10:30", "12:00", "13:30", "15:00", "16:00"],
  stock_daily_time: "16:30",
  quiet_start: null,
  quiet_end: null,
};

// the choices the API accepts (suggest/profile.py)
export const IPO_CHECK_CHOICES = [
  "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00", "15:30", "16:00", "16:30", "16:45",
];
export const IPO_FINAL_CHECK = "17:15";
export const STOCK_DAILY_CHOICES = ["16:00", "16:30", "17:00", "18:00", "19:00", "20:00", "21:00"];

/** The profile's time frames (defaults while it loads or when an older API omits them). */
export function useTimeFrames(): { tf: TimeFrames; watch: WatchWindows; loaded: boolean } {
  const { data, error } = useApi<Profile>("/api/profile");
  const p = data?.preferences;
  return {
    tf: { ...DEFAULT_TIME_FRAMES, ...(p?.time_frames ?? {}) },
    watch: { ...DEFAULT_WATCH, ...(p?.watch ?? {}) },
    loaded: data != null || error != null,
  };
}

function read<T>(key: string): T | null {
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

/** A chart's range/interval/type: this browser's last choice for `key`, else `fallback` (the profile default, which
 * may arrive after the first render; it applies until the viewer picks something). */
export function useChartChoice(key: string, fallback: ChartDefaults) {
  const storageKey = `finresearch:chart:${key}`;
  const [picked, setPicked] = useState<ChartDefaults | null>(null);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- localStorage is only readable after mount
    setPicked(read<ChartDefaults>(storageKey));
  }, [storageKey]);
  const value = picked ?? fallback;
  const set = useCallback(
    (patch: Partial<ChartDefaults>) => {
      setPicked((prev) => {
        const base = { ...(prev ?? fallback), ...patch };
        const next = { ...base, interval: fitInterval(base.range, base.interval) };
        try {
          window.localStorage.setItem(storageKey, JSON.stringify(next));
        } catch {
          /* private window: the choice lasts for this visit */
        }
        return next;
      });
    },
    [fallback, storageKey],
  );
  const reset = useCallback(() => {
    try {
      window.localStorage.removeItem(storageKey);
    } catch {
      /* ignore */
    }
    setPicked(null);
  }, [storageKey]);
  return { value: { ...value, interval: fitInterval(value.range, value.interval) }, set, reset, remembered: picked != null };
}

/** True when `now` (IST) falls inside quiet hours [start, end) (the window may cross midnight). */
export function inQuietHours(watch: WatchWindows, now = new Date()): boolean {
  if (!watch.quiet_start || !watch.quiet_end) return false;
  const hm = now.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
  const [s, e] = [watch.quiet_start, watch.quiet_end];
  return s < e ? hm >= s && hm < e : hm >= s || hm < e;
}

export function refreshLabel(s: number) {
  return s === 0 ? "Off" : s >= 60 ? `${s / 60} min` : `${s} s`;
}

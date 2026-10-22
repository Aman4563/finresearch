// The event-volatility panel on /fno (#391): an implied event move with its expected |move|, an unknown one shown as
// unknown (never 0 %), the IV-elevated flag and the past moves by type. Made-up numbers; no API, database or broker.
import { expect, test } from "@playwright/test";

import type { Chain } from "../src/components/fno/model";
import { EVENT_VOL } from "./fixtures-fno";
import { mockApi } from "./mock-api";

const q = (p: string) => ({ last_price: p, iv: "14.0", oi: "5000", change_in_oi: "10", volume: "1000", bid: p, ask: p });
const CHAIN: Chain = {
  symbol: "NIFTY", expiry: "2026-10-20", underlying: "25000", as_of: "2026-10-13T09:30:00+05:30", lot_size: 65, atm_strike: "25000",
  atm_iv: { call: "14.0", put: "14.5" }, pcr_oi: "1.10", max_pain: "25000", source_label: "NSE",
  rows: [24800, 24900, 25000, 25100, 25200].map((k) => ({ strike: String(k), call: q("100"), put: q("90") })),
};
const ROUTES = {
  "GET /api/fno/NIFTY/expiries": { symbol: "NIFTY", expiries: [new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10)], strikes: [] },
  "GET /api/fno/NIFTY/chain": CHAIN,
  "GET /api/fno/risk-notice": { headline: "87.7% lost", text: "SEBI study", source: "SEBI", url: "https://www.sebi.gov.in", date: "2026-08-20" },
  "GET /api/fno/NIFTY/event-vol": EVENT_VOL,
};
const OPTIONAL = ["GET /api/market/status", "GET /api/fno/positions", "GET /api/fno/paper", "GET /api/fno/NIFTY/events", "GET /api/fno/NIFTY/iv",
  "GET /api/fno/NIFTY/context", "GET /api/fno/direction/NIFTY"];

test("fno: the event-volatility panel shows the implied move, an unknown one as unknown, and past moves", async ({ page }) => {
  const api = await mockApi(page, ROUTES);
  await page.goto("/fno");
  const panel = page.getByLabel("Event volatility for NIFTY");
  await expect(panel.getByText("implied ±1.20% (expected |move| 0.96%)")).toBeVisible();
  await expect(panel.getByText("implied move unknown")).toBeVisible();
  await expect(panel.getByText("no expiry after the event: its move is not priced yet")).toBeVisible();
  await expect(panel.getByText("IV elevated by event")).toBeVisible();
  await expect(panel.getByText("short gamma looks cheap")).toBeVisible();
  await expect(panel.getByText(/24 past moves · median \|move\| 0\.61%/)).toBeVisible();
  await expect(panel.getByText("0%", { exact: true })).toHaveCount(0);
  api.expectClean(OPTIONAL);
});

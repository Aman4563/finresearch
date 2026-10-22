// F&O layout at every width (#326): each F&O page in each state (data, long numbers and names, empty, loading, error)
// at 360, 390, 768, 1024, 1280 and 1536 px must have no text overlapping other text, no text cut off by a box that
// cannot scroll, no truncated label without a tooltip, no figure split over two lines and no horizontal page scroll.
// A full-page screenshot of each is attached to the report. Light theme at every width, dark at a phone and a desktop
// width; FNO_SHOTS=all screenshots both themes everywhere (with SHOT_DIR=<dir> to collect them for a review).
// Made-up data (e2e/fixtures-fno.ts); no API, broker or database.
import { type Page, expect, test } from "@playwright/test";

import * as F from "./fixtures-fno";
import { attachShot, expectCleanLayout } from "./layout";
import { type Routes, mockApi } from "./mock-api";

const WIDTHS = [360, 390, 768, 1024, 1280, 1536];
const DARK_AT = process.env.FNO_SHOTS === "all" ? WIDTHS : [390, 1280];

// an expiry a week ahead: the page drops expiries before today
const expiry = new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10);
const IV = { symbol: "NIFTY", n: 252, min_days: 60, status: "ok", current: 14.2, rank: 0.3, percentile: 0.42, low: 9.8, high: 24.1, skew_25d: 2.1,
  last_day: "2026-10-12", series: [], method: "Daily ATM IV after the close." };

type Scenario = { name: string; path: string; routes: Routes; /** a path that never answers: the page stays in its loading state */ hold?: string; ready: (page: Page) => Promise<void> };

const fnoRoutes = (long: boolean): Routes => ({
  "GET /api/fno/risk-notice": F.NOTICE, "GET /api/fno/NIFTY/expiries": { symbol: "NIFTY", expiries: [expiry], strikes: [] },
  "GET /api/fno/NIFTY/chain": F.WIDE_CHAIN, "GET /api/fno/NIFTY/context": long ? F.LONG_CONTEXT : F.CONTEXT,
  "GET /api/fno/NIFTY/suggest": long ? F.LONG_SUGGEST : F.SUGGEST, "POST /api/fno/strategy": long ? F.LONG_RESULT : F.RESULT,
  "POST /api/fno/plan": long ? F.LONG_PAPER : F.PAPER, "GET /api/fno/positions": long ? F.LONG_POSITIONS : F.POSITIONS,
  "GET /api/fno/paper": long ? F.LONG_BOOK : F.BOOK, "GET /api/fno/NIFTY/events": F.EVENTS, "GET /api/fno/NIFTY/event-vol": F.EVENT_VOL, "GET /api/fno/NIFTY/iv": IV, "POST /api/fno/margin": F.MARGIN, "GET /api/fno/direction/NIFTY": F.DIRECTION(5),
});
const fnoReady = async (page: Page) => {
  await expect(page.getByRole("heading", { name: "Trade plan" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Market context" })).toBeVisible();
  await page.getByRole("button", { name: "Suggest setups" }).click();
  await expect(page.getByRole("article").nth(2)).toBeVisible(); // the suggested setups and the plan for the legs
  await page.getByRole("button", { name: "Estimate margin" }).click();
  await expect(page.getByText(/margin \(approximate/)).toBeVisible();
};
const labReady = async (page: Page) => {
  await page.getByRole("button", { name: /#7 OOS done/ }).click();
  await page.getByRole("button", { name: /Show the 41 trades/ }).click();
  await expect(page.getByRole("region", { name: "Trades" })).toBeVisible();
};
const alert = (page: Page) => expect(page.getByRole("alert").first()).toBeVisible();
const skeleton = (page: Page) => expect(page.locator("main .skeleton").first()).toBeVisible();
const fail: [number, unknown] = [500, { detail: "the API answered 500 (e2e)" }];

const SCENARIOS: Scenario[] = [
  { name: "fno-data", path: "/fno?preset=iron_condor", routes: fnoRoutes(false), ready: fnoReady },
  { name: "fno-long", path: "/fno?preset=iron_condor", routes: fnoRoutes(true), ready: fnoReady },
  { name: "fno-loading", path: "/fno", routes: fnoRoutes(false), hold: "/api/fno/NIFTY/chain", ready: skeleton },
  { name: "fno-error", path: "/fno", routes: { ...fnoRoutes(false), "GET /api/fno/NIFTY/chain": fail }, ready: alert },
  { name: "fno-empty", path: "/fno", routes: { ...fnoRoutes(false), "GET /api/fno/NIFTY/expiries": { symbol: "NIFTY", expiries: [], strikes: [] } }, ready: alert },
  { name: "paper-data", path: "/fno/paper", routes: { "GET /api/fno/paper": F.BOOK, "GET /api/fno/risk-notice": F.NOTICE }, ready: (p) => expect(p.locator("[id='3']")).toBeVisible() },
  { name: "paper-long", path: "/fno/paper", routes: { "GET /api/fno/paper": F.LONG_BOOK, "GET /api/fno/risk-notice": F.NOTICE }, ready: (p) => expect(p.locator("[id='3']")).toBeVisible() },
  { name: "paper-empty", path: "/fno/paper", routes: { "GET /api/fno/paper": F.EMPTY_BOOK, "GET /api/fno/risk-notice": F.NOTICE }, ready: (p) => expect(p.getByText("No paper trades yet")).toBeVisible() },
  { name: "paper-loading", path: "/fno/paper", routes: { "GET /api/fno/risk-notice": F.NOTICE }, hold: "/api/fno/paper", ready: skeleton },
  { name: "paper-error", path: "/fno/paper", routes: { "GET /api/fno/paper": fail, "GET /api/fno/risk-notice": F.NOTICE }, ready: alert },
  { name: "backtests-data", path: "/fno/backtests", routes: { "GET /api/fno/backtests": F.BACKTESTS, "GET /api/fno/risk-notice": F.NOTICE }, ready: (p) => expect(p.locator("[id='nifty_dcs_m']")).toBeVisible() },
  { name: "backtests-long", path: "/fno/backtests", routes: { "GET /api/fno/backtests": F.LONG_BACKTESTS, "GET /api/fno/risk-notice": F.NOTICE }, ready: (p) => expect(p.locator("[id='nifty_dcs_m']")).toBeVisible() },
  { name: "backtests-empty", path: "/fno/backtests", routes: { "GET /api/fno/backtests": { registration: F.REG, run: null }, "GET /api/fno/risk-notice": F.NOTICE }, ready: (p) => expect(p.getByText("No backtest saved yet")).toBeVisible() },
  { name: "backtests-loading", path: "/fno/backtests", routes: { "GET /api/fno/risk-notice": F.NOTICE }, hold: "/api/fno/backtests", ready: skeleton },
  { name: "backtests-error", path: "/fno/backtests", routes: { "GET /api/fno/backtests": fail, "GET /api/fno/risk-notice": F.NOTICE }, ready: alert },
  { name: "lab-data", path: "/fno/lab", routes: { "GET /api/fno/lab": F.LAB, "GET /api/fno/lab/paper": { rules: F.LAB_PAPER }, "GET /api/fno/lab/runs/7": F.LAB_VIEW,
    "GET /api/fno/lab/runs/7/trades": { trades: F.LAB_TRADES }, "GET /api/fno/risk-notice": F.NOTICE }, ready: labReady },
  { name: "lab-long", path: "/fno/lab", routes: { "GET /api/fno/lab": F.LONG_LAB, "GET /api/fno/lab/paper": { rules: F.LONG_LAB_PAPER }, "GET /api/fno/lab/runs/7": F.LONG_LAB_VIEW,
    "GET /api/fno/lab/runs/7/trades": { trades: F.LAB_TRADES }, "GET /api/fno/lab/runs/9": { ...F.LONG_LAB_VIEW, id: 9, status: "running", progress: 0.4, result: null },
    "GET /api/fno/risk-notice": F.NOTICE }, ready: labReady },
  { name: "lab-empty", path: "/fno/lab", routes: { "GET /api/fno/lab": { ...F.LAB, strategies: [] }, "GET /api/fno/lab/paper": { rules: [] }, "GET /api/fno/risk-notice": F.NOTICE },
    ready: (p) => expect(p.getByText("No strategy yet")).toBeVisible() },
  { name: "lab-loading", path: "/fno/lab", routes: { "GET /api/fno/lab/paper": { rules: [] }, "GET /api/fno/risk-notice": F.NOTICE }, hold: "/api/fno/lab", ready: skeleton },
  { name: "lab-error", path: "/fno/lab", routes: { "GET /api/fno/lab": fail, "GET /api/fno/lab/paper": { rules: [] }, "GET /api/fno/risk-notice": F.NOTICE }, ready: alert },
];

test.describe("fno layout", () => {
  test.use({ reducedMotion: "reduce" }); // no fade-up transforms half-way through a measurement

  for (const s of SCENARIOS) {
    test(`${s.name}: no overlap, clipping or page scroll at any width`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop", "sets its own widths; once is enough");
      test.setTimeout(240_000);
      const api = await mockApi(page, s.routes);
      if (s.hold) await page.route((u) => u.pathname === s.hold, () => undefined);
      for (const width of WIDTHS) {
        for (const theme of DARK_AT.includes(width) ? ["light", "dark"] : ["light"]) {
          await page.setViewportSize({ width, height: 900 });
          await page.goto(`${s.path}${s.path.includes("?") ? "&" : "?"}theme=${theme}`);
          await s.ready(page);
          if (!s.hold) await page.waitForLoadState("networkidle");
          const where = `${s.name} ${width}px ${theme}`;
          // layout is theme-independent: measure once per width
          if (theme === "light") await expectCleanLayout(page, where);
          await attachShot(page, testInfo, `${s.name}-${width}-${theme}`);
        }
      }
      api.expectClean(["GET /api/market/status", "POST /api/signals/", "GET /api/signals/"]);
    });
  }
});

// F&O trade plans and market context (#317): the context panel labels every indicator with its evidence and says it
// is not a signal; "Suggest setups" shows an AVOID card with its failed backtest and a paper-only card (never ENTER without a candidate, #350); the
// one-click action records a paper trade and arms its exit alerts. Made-up chain and plans; no API or broker.
import { type Page, expect, test } from "@playwright/test";

import { AVOID, CHAIN, CONTEXT, NOTICE, PAPER } from "./fixtures-fno";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

const ROUTES = {
  // a week ahead of today: the page drops expiries before today, so fixed dates went stale after 20-Oct-2026
  "GET /api/fno/NIFTY/expiries": { symbol: "NIFTY", expiries: [new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10)], strikes: [] },
  "GET /api/fno/NIFTY/chain": CHAIN, "GET /api/fno/risk-notice": NOTICE, "GET /api/fno/NIFTY/context": CONTEXT,
  "GET /api/fno/NIFTY/suggest": { symbol: "NIFTY", today: "2026-10-13", cards: [AVOID, PAPER], note: "The library's pre-registered recipes. Not a recommendation." },
  "POST /api/fno/paper": { id: 7, legs: [] }, "POST /api/fno/exit-rules": { id: 1 },
};
const OPTIONAL = ["GET /api/market/status", "GET /api/fno/positions", "GET /api/fno/paper", "GET /api/fno/NIFTY/events", "GET /api/fno/NIFTY/iv",
  "GET /api/fno/direction/NIFTY"]; // the direction panel (#327) has its own spec

test("fno: market context labels evidence; suggest shows AVOID on a failed hard check, directional evidence, and a paper-only card with 0 real lots", async ({ page }) => {
  const api = await mockApi(page, ROUTES);
  await page.goto("/fno");
  const ctx = page.locator("section").filter({ has: page.getByRole("heading", { name: "Market context" }) });
  await expect(page.getByText(/Context, not signals/)).toBeVisible();
  await expect(ctx.getByText("Trend: uptrend (emerging)")).toBeVisible();
  await expect(ctx.getByText("evidence: weak / contested").first()).toBeVisible();
  await expect(ctx.getByText(/Contracts long − short/)).toBeVisible();
  // #354 e: build-up badges are neutral, not gain/loss colours that read as direction calls (puts included)
  for (const b of ["short build-up", "long build-up"]) await expect(ctx.getByText(b, { exact: true }).first()).toHaveAttribute("data-tone", "neutral");
  // "none verified" is at least as cautionary as "weak / contested" (it used to get the milder neutral badge)
  await expect(ctx.getByText("evidence: none verified").first()).toHaveAttribute("data-tone", "warn");
  await expect(ctx.getByText("evidence: weak / contested").first()).toHaveAttribute("data-tone", "warn");
  // max pain is labelled context inside the panel, no longer a KPI tile above it
  await expect(page.getByText("Max pain", { exact: true })).toHaveCount(0);
  await expect(ctx.getByText(/Max pain 25,000 \(spot 25,000\), context only/)).toBeVisible();
  // #354 c/d/g: the term slope without a "stress" reading; IV − RV on matched horizons; ADX roll-adjusted
  await expect(ctx.getByText(/slope \+0\.6 pts/)).toBeVisible();
  await expect(ctx.getByText(/stress|inverted/i)).toHaveCount(0);
  await expect(ctx.getByText(/IV30 − RV20: \+2\.5 pts/)).toBeVisible();
  await expect(ctx.getByText(/ratio-adjusted at each roll/)).toBeVisible();
  await page.getByRole("button", { name: "Suggest setups" }).click();
  const avoid = page.getByRole("article", { name: /NIFTY put credit spread/ });
  await expect(avoid.getByTestId("plan-verdict")).toHaveText("AVOID");
  await expect(avoid.getByTestId("plan-evidence")).toContainText("directional — reflects the index path, not the structure");
  await expect(avoid.getByText("directional — the index path, not the structure")).toBeVisible();
  const enter = page.getByRole("article", { name: /short iron condor/ });
  await expect(enter.getByTestId("plan-verdict")).toHaveText("NO PROVEN EDGE — paper only (0 real lots)");
  await expect(enter.getByTestId("plan-size")).toHaveText("3× these legs");
  await expect(enter.getByTestId("plan-real-lots")).toHaveText("0 lots");
  await expect(enter.getByTestId("plan-event-check")).toContainText("tier-1 event before expiry"); // #367
  await expect(enter.getByTestId("plan-event-check")).toContainText("RBI monetary policy decision");
  if (!phone(page)) {
    await enter.getByText(/^Details: sizing/).click();
    await expect(enter.getByTestId("plan-open-book")).toContainText("at risk of"); // #358: the open-book cap is shown
    // #394/#395: the shock row shows each underlying's registered beta, its SE, band, window and as-of date, or why unknown
    await expect(enter.getByTestId("plan-shock-beta").first()).toContainText("BANKNIFTY beta 1.15 (SE 0.09), shocked at the worse of 1.00–1.30");
    await expect(enter.getByTestId("plan-shock-beta").first()).toContainText("as of 9 Oct 2026");
    await expect(enter.getByTestId("plan-shock-beta").nth(1)).toContainText("FINNIFTY: beta to NIFTY unknown, only 40 paired returns (needs 120)");
  }
  await expect(enter.getByText(/buy back at ₹127.50\/share/)).toBeVisible();
  await enter.getByRole("button", { name: "Paper trade + alerts" }).click();
  await expect(enter.getByRole("status")).toContainText("Paper trade #7 recorded with 3 exit alerts");
  expect(api.count("POST /api/fno/paper")).toBe(1);
  expect(api.count("POST /api/fno/exit-rules")).toBe(3);
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(OPTIONAL);
});

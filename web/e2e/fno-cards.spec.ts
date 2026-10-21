// Uncertainty-honest F&O cards (#357): no F&O card shows a probability without its reliability line, a model-span
// interval is never written as a confidence interval, the model EV comes before the chance of profit, and the model EV
// carries its "model EV - not validated" label. Made-up chain, plans and signal (e2e/fixtures-fno.ts); no API or broker.
import { type Locator, type Page, expect, test } from "@playwright/test";

import * as F from "./fixtures-fno";
import { mockApi } from "./mock-api";

const expiry = new Date(Date.now() + 7 * 864e5).toISOString().slice(0, 10); // the page drops expiries before today
const ROUTES = {
  "GET /api/fno/risk-notice": F.NOTICE, "GET /api/fno/NIFTY/expiries": { symbol: "NIFTY", expiries: [expiry], strikes: [] },
  "GET /api/fno/NIFTY/chain": F.WIDE_CHAIN, "GET /api/fno/NIFTY/context": F.CONTEXT, "GET /api/fno/NIFTY/suggest": F.SUGGEST,
  "POST /api/fno/strategy": F.RESULT, "POST /api/fno/plan": F.PAPER, "POST /api/signals/fno/NIFTY": F.FNO_SIGNAL,
};
const OPTIONAL = ["GET /api/market/status", "GET /api/fno/positions", "GET /api/fno/paper", "GET /api/fno/NIFTY/events", "GET /api/fno/NIFTY/iv",
  "GET /api/fno/direction/NIFTY"];

/** True when `a` comes before `b` in the document. */
async function before(a: Locator, b: Locator) {
  const other = await b.elementHandle();
  return a.evaluate((x, y) => !!(x.compareDocumentPosition(y as Node) & Node.DOCUMENT_POSITION_FOLLOWING), other);
}

async function open(page: Page) {
  const api = await mockApi(page, ROUTES);
  await page.goto("/fno?preset=iron_condor");
  await expect(page.getByRole("heading", { name: "Trade plan" })).toBeVisible();
  return api;
}

test("fno: every probability carries its reliability, EV leads, a model span is not a confidence interval", async ({ page }) => {
  const api = await open(page);
  // the plan card for the legs: EV, then payoff ratio, then chance of profit with its reliability line
  const plan = page.getByTestId("plan-key-numbers").first();
  await expect(plan.getByTestId("plan-ev")).toContainText("model EV - not validated");
  expect(await before(plan.getByTestId("plan-ev"), plan.getByTestId("plan-pop"))).toBe(true);
  await expect(plan.getByText("0.31 : 1")).toBeVisible();
  await expect(plan.getByTestId("plan-pop").getByTestId("reliability")).toContainText("calibration not established: 0 scored");

  // the suggested setups: every card that shows a chance of profit shows its reliability line too
  await page.getByRole("button", { name: "Suggest setups" }).click();
  await expect(page.getByTestId("plan-key-numbers").nth(1)).toBeVisible();
  for (const card of await page.getByTestId("plan-key-numbers").all()) {
    if (await card.getByText("Chance of profit").count()) await expect(card.getByTestId("reliability")).toHaveCount(1);
  }

  // the analysis panel: the model EV line comes before its chance-of-profit tiles, which carry a reliability line
  const ev = page.getByTestId("fno-model-ev");
  await expect(ev).toContainText("model EV - not validated");
  expect(await before(ev, page.getByText("Chance of profit · risk-neutral"))).toBe(true);

  // the Strategy check signal card: a model span, labelled as such and never as a confidence interval
  // (a backtest's expectancy CI elsewhere on the page is a real confidence interval and keeps its label)
  const strategy = page.locator("section").filter({ has: page.getByRole("heading", { name: "Strategy check" }) });
  await expect(strategy.getByTestId("probability-interval")).toHaveText("span across models 62%–74% · not a confidence interval");
  await expect(strategy.getByText(/95% CI|confidence interval \d/)).toHaveCount(0);
  await expect(strategy.getByTestId("reliability")).toBeVisible();

  // the P&L quantile dot-plot: 20 dots, each 5% of outcomes
  await page.getByText("Details: sizing, limit prices, adjustments and what invalidates the trade").first().click();
  await expect(page.getByTestId("plan-pnl-dots").first().locator("circle")).toHaveCount(20);
  api.expectClean(OPTIONAL);
});

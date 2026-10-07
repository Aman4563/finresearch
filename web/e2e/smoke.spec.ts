// Smoke tests (#248): the states the app must never get wrong, on desktop and on a 390 px phone (projects in
// playwright.config.ts). Assertions use roles and loose patterns rather than exact copy, so wording changes don't
// break them; what they pin down is that an unknown or stale value is labelled as such.
import { type Page, expect, test } from "@playwright/test";

import { CLAIM, DOC_LINES, REPORT, SIGNAL, SNAPSHOT, TAX } from "./fixtures";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

test("portfolio: unknown cost, stale price and incomplete tax are labelled", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/portfolio": SNAPSHOT,
    "POST /api/portfolio/snapshot": { recorded: false, reason: "e2e" },
    "GET /api/portfolio/tax": TAX,
    "GET /api/connections/summary": [],
  });
  await page.goto("/portfolio");
  const holdings = page.getByRole("region", { name: "Holdings" });
  await expect(holdings.getByText("Example Opening Balance Fund")).toBeVisible();
  await expect(holdings.getByText(/cost unknown/i)).toBeVisible();
  await expect(page.getByText(/some costs are unknown/i)).toBeVisible();
  // the fund's price is a statement NAV from June: shown with its date, never as today's price
  await expect(holdings.getByText(/statement NAV|stale/i)).toBeVisible();
  await expect(holdings.getByText(/30 Jun/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);

  await page.goto("/portfolio#tax");
  await expect(page.getByText(/tax incomplete/i)).toBeVisible();
  await expect(page.getByText(/^incomplete$/i)).toBeVisible(); // the year's total is not shown as a figure
  if (phone(page)) await expectNoHorizontalOverflow(page);
  // cards outside this test's scope fall back to their error / unknown states
  api.expectClean(["GET /api/funds/category-ranks", "GET /api/lookthrough", "GET /api/portfolio/health",
    "GET /api/disclosures/tracked", "GET /api/portfolio/ais"]);
});

test("research: a blocked report is marked not published, and a citation opens its evidence", async ({ page }) => {
  const api = await mockApi(page, {
    "GET /api/runs/1/report": REPORT,
    "GET /api/runs/1/claims": [CLAIM],
    "GET /api/runs/1/pack": { files: [], entries: [] },
    "GET /api/documents/7/lines": DOC_LINES,
  });
  await page.goto("/runs/1/report#report");
  await expect(page.getByText(/not published/i).first()).toBeVisible();
  await expect(page.getByText(/C40 is contradicted/)).toBeVisible();
  await page.getByRole("button", { name: /claim 12/ }).first().click();
  const panel = phone(page) ? page.getByRole("dialog") : page.locator("aside").filter({ hasText: "Evidence for" });
  await expect(panel.getByText(/Evidence for/)).toBeVisible();
  await expect(panel.getByText(/p14 L230/)).toBeVisible();
  await expect(panel.getByText(/quote found/i)).toBeVisible();
  await expect(panel.getByText("Revenue from operations for Fiscal 2026 was Rs. 3,912.40 million").last()).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean(["GET /api/runs/1/insights", "GET /api/runs/1/suggestion"]);
});

test("signals: an informational signal is labelled as no proven edge, never as a call", async ({ page }) => {
  const api = await mockApi(page, { "POST /api/signals/stock/EXTEX": SIGNAL });
  await page.goto("/stocks/EXTEX");
  await expect(page.getByText(/informational/i).first()).toBeVisible();
  await expect(page.getByText(/no proven edge/i).first()).toBeVisible();
  await expect(page.getByText(/^(BUY|ACCUMULATE)$/)).toHaveCount(0); // the composite's action is not shown as the call
  if (phone(page)) await expectNoHorizontalOverflow(page);
  // the stock page's market cards read exchange data the fixtures don't carry: they show their error states
  api.expectClean(["GET /api/stocks/EXTEX", "GET /api/watches", "GET /api/market/status"]);
});

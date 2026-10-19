// The F&O paper book (#308): an unmarked open trade reads "unknown / not marked yet" (never ₹0), a trade waiting for
// its settlement price says so, and the results count every closed trade. Made-up trades; no API or broker involved.
import { type Page, expect, test } from "@playwright/test";

import { BOOK } from "./fixtures-fno";
import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;

test("fno paper book: unmarked, awaiting and closed trades are labelled; results count them", async ({ page }) => {
  const api = await mockApi(page, { "GET /api/fno/paper": BOOK, "GET /api/fno/risk-notice": { headline: "87.7% lost", text: "SEBI study", source: "SEBI", url: "https://www.sebi.gov.in", date: "2026-08-20" } });
  await page.goto("/fno/paper");
  await expect(page.getByText(/none can be deleted/i).first()).toBeVisible();
  const open = page.locator("[id='1']");
  await expect(open.getByText(/net if closed now unknown/)).toBeVisible(); // not marked yet: never ₹0
  await expect(open.getByText(/not marked yet/)).toBeVisible();
  await expect(page.locator("[id='2']").getByText(/waiting for the settlement price/)).toBeVisible();
  await expect(page.locator("[id='2']").getByText(/waiting for NSE's close/)).toBeVisible();
  await expect(page.locator("[id='3']").getByText(/net −₹254/)).toBeVisible();
  await expect(page.getByText(/0 won · 1 lost/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

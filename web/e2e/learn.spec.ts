// Learn F&O (#319): every lesson page renders its sections, a payoff sketch where it has one, a working self-check and
// a source list in which every citation resolves (no "unknown source"); unverified sources are labelled; nothing is
// fetched from the API beyond the shell's calls; no horizontal scroll on a 390 px phone.
import { type Page, expect, test } from "@playwright/test";

import { expectNoHorizontalOverflow, mockApi } from "./mock-api";

const phone = (page: Page) => (page.viewportSize()?.width ?? 1280) < 640;
const MODULES = [
  { slug: "basics", title: /How F&O works in India/, section: /Lot sizes and contract value/ },
  { slug: "options", title: /Options mechanics/, section: /The greeks, worked/ },
  { slug: "strategies", title: /Strategy catalogue/, section: /Iron condor/ },
  { slug: "risk", title: /Risk management like professionals/, section: /Risk of ruin/ },
  { slug: "process", title: /The process of disciplined traders/, section: /Avoiding overfitting/ },
  { slug: "app", title: /Using this app, step by step/, section: /Paper trade it/ },
];

test("learn: index lists six modules and the glossary", async ({ page }) => {
  const api = await mockApi(page, {});
  await page.goto("/learn");
  await expect(page.getByRole("heading", { level: 1, name: "Learn F&O" })).toBeVisible();
  for (const m of MODULES) await expect(page.getByRole("heading", { level: 2, name: m.title })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: /Glossary/ })).toBeVisible();
  await expect(page.getByText(/not investment advice/).first()).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

for (const m of MODULES) {
  test(`learn: ${m.slug} renders sections, quiz and resolved sources`, async ({ page }) => {
    const api = await mockApi(page, {});
    await page.goto(`/learn/${m.slug}`);
    await expect(page.getByRole("heading", { level: 1, name: m.title })).toBeVisible();
    await expect(page.getByRole("heading", { level: 2, name: m.section })).toBeVisible();
    const sources = page.locator("#sources");
    await expect(sources.locator("li").first()).toBeVisible();
    await expect(sources.getByText(/unknown source/)).toHaveCount(0);
    // the self-check: pick the first option of question 1 and get an explanation
    const q1 = page.getByRole("radiogroup", { name: "Question 1" });
    await q1.getByRole("radio").first().click();
    await expect(page.locator("#quiz").getByRole("status").first()).toContainText(/Correct|Not quite/);
    if (phone(page)) await expectNoHorizontalOverflow(page);
    api.expectClean();
  });
}

test("learn: strategy sketches draw payoffs and link to the backtest", async ({ page }) => {
  const api = await mockApi(page, {});
  await page.goto("/learn/strategies#put-credit-spread");
  const card = page.locator("#put-credit-spread");
  await expect(card.locator(".recharts-area").first()).toBeVisible();
  await expect(card.getByText(/breakeven/)).toBeVisible();
  await expect(card.getByRole("link", { name: /NIFTY put credit spread, monthly/ })).toHaveAttribute("href", "/fno/backtests#nifty_pcs_m");
  await expect(page.locator("#short-straddle").getByText(/worst in range unlimited/)).toBeVisible();
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

test("learn: unverified tax sources are labelled, and the glossary filters", async ({ page }) => {
  const api = await mockApi(page, {});
  await page.goto("/learn/basics");
  await expect(page.locator("#src-ita-1961").getByText("unverified")).toBeVisible();
  await page.goto("/learn/glossary");
  await page.getByLabel("Search the glossary").fill("theta");
  await expect(page.locator("#term-theta")).toBeVisible();
  await expect(page.locator("#term-vega")).toHaveCount(0);
  if (phone(page)) await expectNoHorizontalOverflow(page);
  api.expectClean();
});

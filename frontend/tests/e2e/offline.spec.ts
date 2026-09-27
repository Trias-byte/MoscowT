import { expect, test } from '@playwright/test';

test('application and API documentation load only local assets', async ({ page, baseURL }) => {
  const origin = new URL(baseURL!).origin;
  const external: string[] = [];
  const failed: string[] = [];
  page.on('requestfailed', (request) => {
    if (/\/assets\//.test(request.url())) failed.push(request.url());
  });
  await page.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (/^https?:$/.test(url.protocol) && url.origin !== origin) {
      external.push(url.href);
      await route.abort();
    } else {
      await route.continue();
    }
  });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Пассажиропоток', exact: true })).toBeVisible();
  await expect(page.locator('.maplibregl-canvas')).toBeVisible();
  await expect(page.locator('.v2-map-fallback')).toHaveCount(0);
  await expect
    .poll(() => page.evaluate(() => document.fonts.check('13px "Golos Text"')))
    .toBe(true);
  await page.goto('/docs');
  await expect(page.getByText('MoscowT route-hour API', { exact: false }).first()).toBeVisible();
  await expect(page.locator('.swagger-ui .opblock').first()).toBeVisible();
  expect(external).toEqual([]);
  expect(failed).toEqual([]);
});

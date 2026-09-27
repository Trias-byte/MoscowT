import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('https://tiles.openfreemap.org/**', (r) => r.abort());
  await page.route('https://tile.openstreetmap.org/**', (r) => r.abort());
  await page.route('https://fonts.googleapis.com/**', (r) => r.abort());
  await page.goto('/');
  await page.getByRole('button', { name: /^Данные и модели/ }).click();
  await expect(page.getByLabel('Готовый прогноз')).not.toHaveValue('');
});

test('scenario preview changes immediately; saved state agrees with API and survives reload', async ({
  page,
  request,
}) => {
  const panel = page.getByRole('complementary', { name: 'Данные и модели', exact: true });
  const preview = page.getByRole('table', { name: 'Сравнение сценария' });
  await expect(preview.locator('tbody tr').first()).not.toContainText('Нет данных');
  const forecast = await page.getByLabel('Готовый прогноз').inputValue();
  const base = await preview.locator('tbody tr').first().locator('td').nth(1).innerText();
  await page.getByLabel('Коэффициент event').focus();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  await expect(panel).toContainText('110.0% базового');
  await expect(preview.locator('tbody tr').first().locator('td').nth(1)).toHaveText(base);
  const created = page.waitForResponse(
    (r) => r.url().endsWith('/api/v2/scenarios') && r.request().method() === 'POST',
  );
  await page.getByRole('button', { name: 'Сохранить сценарий', exact: true }).click();
  const scenario = await (await created).json();
  expect(scenario.spec.forecast_id).toBe(forecast);
  expect(scenario.spec.coefficients.event).toBe(1.1);
  await expect.poll(() => new URL(page.url()).searchParams.get('view')).toContain(scenario.id);
  expect((await request.get('/api/v2/scenarios/' + scenario.id)).status()).toBe(200);
  await page.reload();
  await expect(panel).toHaveClass(/expanded/);
  await page.getByRole('button', { name: 'Вернуться к базе' }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('view')).not.toContain(scenario.id);
});

test('year selects annual model and long scenario can be inspected by month', async ({
  page,
  request,
}) => {
  const models = await (await request.get('/api/v2/models')).json();
  await page.getByRole('button', { name: 'Год', exact: true }).click();
  const chosen = await page.getByLabel('Обученная модель').inputValue();
  expect(models.find((m: { id: string }) => m.id === chosen).spec.model_type).toBe(
    'annual_scenario',
  );
  const runs = await (await request.get('/api/v2/forecast-runs')).json();
  const annual = runs.find((r: { model_type: string }) => r.model_type === 'annual_scenario');
  await page.getByLabel('Готовый прогноз').selectOption(annual.id);
  await page
    .getByLabel('Период сценария и выгрузки: начало')
    .fill(annual.spec.time_range.start.slice(0, 10));
  await page
    .getByLabel('Период сценария и выгрузки: конец')
    .fill(annual.spec.time_range.end.slice(0, 10));
  await page.getByLabel('Шаг выгрузки').selectOption('month');
  await page.getByText('Динамика выбранного выпуска (по месяцам)', { exact: true }).click();
  await expect(page.locator('.platform-series tbody tr')).toHaveCount(12);
  await expect(
    page.getByRole('complementary', { name: 'Данные и модели', exact: true }),
  ).toContainText('Невалидированный');
});

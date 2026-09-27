import { test, expect, type APIRequestContext, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

test.setTimeout(120000);
test.beforeEach(async ({ context }) => {
  await context.route('https://tiles.openfreemap.org/**', (r) => r.abort());
  await context.route('https://tile.openstreetmap.org/**', (r) => r.abort());
});
const scenarioPanel = (page: Page) =>
  page.getByRole('complementary', { name: 'Сценарии', exact: true });
async function openScenario(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Сценарии', exact: true }).click();
  await expect(scenarioPanel(page).getByLabel('Базовый выпуск')).not.toHaveValue('');
  return scenarioPanel(page);
}
async function finished(request: APIRequestContext, id: string) {
  await expect
    .poll(async () => (await (await request.get(`/api/v2/jobs/${id}`)).json()).status, {
      timeout: 90000,
    })
    .toBe('ready');
  return (await (await request.get(`/api/v2/jobs/${id}`)).json()).result;
}
async function calculate(page: Page) {
  const response = page.waitForResponse(
    (r) => /\/scenarios\/[^/]+\/runs$/.test(r.url()) && r.request().method() === 'POST',
  );
  await scenarioPanel(page)
    .getByRole('button', { name: 'Рассчитать сценарий', exact: true })
    .click();
  return (await (await response).json()).id as string;
}

test('D03 D09 D11 D12 D15 D19: time boundaries, disabled incomplete incident and narrow editors', async ({
  page,
  request,
}) => {
  const panel = await openScenario(page);
  await panel.getByLabel('Маршруты сценария').fill('17');
  await panel.getByText('Базовые условия выбранного объекта', { exact: true }).click();
  await expect(panel.getByRole('status')).toContainText('вне маршрутов сценария');
  await page.getByRole('button', { name: 'Час', exact: true }).click();
  const time = page.getByRole('slider', { name: 'Выбранное время' });
  await time.fill('3');
  await panel.getByRole('button', { name: 'Добавить ДТП на карте' }).click();
  await page.locator('.maplibregl-canvas').click({ position: { x: 180, y: 160 } });
  await expect(panel.getByLabel('Начало ДТП')).toHaveValue('2025-11-01T03:00');
  await expect(page.locator('.incident-marker')).toHaveCount(1);
  await time.fill('4');
  await expect(page.locator('.incident-marker')).toHaveCount(0);
  await time.fill('3');
  await expect(page.locator('.incident-marker')).toHaveCount(1);
  await panel.getByText('Расписание и бюджет', { exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  for (const label of [
    'Начало ДТП',
    'Подтверждённые пользователем маршруты',
    'Новый интервал, минут',
  ]) {
    const input = panel.getByLabel(label, { exact: true });
    await input.scrollIntoViewIfNeeded();
    expect((await input.boundingBox())!.width).toBeGreaterThan(180);
  }
  expect(await panel.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
  await panel.getByLabel('Начало ДТП').scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/report-editors-mobile.png', fullPage: true });
  await panel.getByLabel('Новый интервал, минут', { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/report-schedule-mobile.png', fullPage: true });
  const contrast = await panel
    .getByRole('button', { name: 'Рассчитать сценарий', exact: true })
    .evaluate((node) => {
      const style = getComputedStyle(node);
      const luminance = (color: string) =>
        color
          .match(/[\d.]+/g)!
          .slice(0, 3)
          .map((v) => Number(v) / 255)
          .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
          .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
      const a = luminance(style.color),
        b = luminance(style.backgroundColor);
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    });
  expect(contrast).toBeGreaterThanOrEqual(4.5);
  await panel.getByRole('checkbox', { name: 'ДТП', exact: true }).uncheck();
  const result = await finished(request, await calculate(page));
  expect(result.spec.incidents).toEqual([]);
  expect(result.total).toBeCloseTo(result.base_total, 6);
  await panel.getByRole('button', { name: 'Вернуться к базе' }).click();
  await panel.getByRole('checkbox', { name: 'ДТП', exact: true }).check();
  await expect(page.locator('.incident-marker')).toHaveCount(0);
  await page.reload();
  await expect(panel.getByLabel('Базовый выпуск')).not.toHaveValue('');
  await expect(page.locator('.incident-marker')).toHaveCount(0);
});

test('D04 D10 D12: delayed job metadata preserves edits across tabs and never applies a result', async ({
  page,
  context,
  request,
}) => {
  const name = `Late result ${Date.now()}`;
  let hideReady = true;
  await context.route(
    (url) => url.pathname.startsWith('/api/v2/jobs'),
    async (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      const response = await route.fetch();
      const json = await response.json();
      const hide = (job: any) =>
        hideReady && job.result?.spec?.name === name
          ? { ...job, status: 'running', result: null }
          : job;
      await route.fulfill({ response, json: Array.isArray(json) ? json.map(hide) : hide(json) });
    },
  );
  const panel = await openScenario(page);
  await panel.getByLabel('Название сценария').fill(name);
  const originalId = await panel.getByLabel('Сохранённый сценарий').inputValue();
  const job = await calculate(page);
  const second = await context.newPage();
  await second.goto('/');
  const other = scenarioPanel(second);
  await expect(other.getByLabel('Сохранённый сценарий')).toHaveValue(originalId);
  await other.getByLabel('Название сценария').fill('Новые правки A');
  await other.getByRole('slider', { name: 'Сезонная поправка', exact: true }).press('End');
  await other.getByRole('button', { name: 'Вернуться к базе' }).click();
  await panel.getByRole('button', { name: 'Создать копию сценария' }).click();
  await panel.getByLabel('Название сценария').fill('Независимый B');
  const copyId = await panel.getByLabel('Сохранённый сценарий').inputValue();
  expect(copyId).not.toBe(originalId);
  await finished(request, job);
  hideReady = false;
  await expect(other).toContainText('Параметры изменены', { timeout: 15000 });
  await expect(other.getByLabel('Название сценария')).toHaveValue('Новые правки A');
  await expect(other.getByRole('slider', { name: 'Сезонная поправка', exact: true })).toHaveValue(
    '2',
  );
  await expect(other.getByRole('button', { name: 'Показать результат на карте' })).toBeDisabled();
  await expect(panel.getByLabel('Название сценария')).toHaveValue('Независимый B');
  for (const tab of [page, second]) {
    expect(JSON.parse(new URL(tab.url()).searchParams.get('view')!).scenarioId).toBeUndefined();
    await tab.reload();
    await expect(tab.locator('.incident-marker')).toHaveCount(0);
  }
  await expect(other.getByLabel('Название сценария')).toHaveValue('Новые правки A');
  await expect(panel.getByLabel('Сохранённый сценарий')).toHaveValue(copyId);
});

test('D16 D17: real research training, opt-in catalogue, local map and selected-base submission export', async ({
  page,
  request,
}) => {
  const caps = await (await request.get('/api/v2/capabilities')).json();
  const published = caps.current_snapshot;
  const base = await (await request.get(`/api/v2/forecast-runs/${published.forecastId}`)).json();
  const baseModel = (await (await request.get('/api/v2/models')).json()).find(
    (model: any) => model.id === base.spec.model_id,
  );
  const train = await request.post('/api/v2/training-runs', {
    headers: { 'Idempotency-Key': `report-research-${Date.now()}` },
    data: {
      dataset_id: base.spec.dataset_id,
      model_type: 'seasonal',
      route_ids: ['17'],
      time_range: baseModel.spec.time_range,
      purpose: 'research',
    },
  });
  expect(train.status()).toBe(202);
  const model = await finished(request, (await train.json()).id);
  const forecast = await request.post('/api/v2/forecast-runs', {
    headers: { 'Idempotency-Key': `report-forecast-${Date.now()}` },
    data: {
      dataset_id: base.spec.dataset_id,
      model_id: model.id,
      route_ids: ['17'],
      origin: base.spec.origin,
      time_range: { start: '2025-11-01T00:00:00+03:00', end: '2025-11-02T00:00:00+03:00' },
    },
  });
  expect(forecast.status()).toBe(202);
  const run = await finished(request, (await forecast.json()).id);
  expect(run.purpose).toBe('research');
  const panel = await openScenario(page);
  const baseSelect = panel.getByLabel('Базовый выпуск');
  await expect(baseSelect.locator(`option[value="${run.id}"]`)).toHaveCount(0);
  await panel.getByLabel('Показать исследовательские выпуски').check();
  await expect(baseSelect.locator(`option[value="${run.id}"]`)).toHaveCount(1);
  await baseSelect.selectOption(run.id);
  await panel.getByLabel('Начало', { exact: true }).fill('2025-11-01T00:00');
  await panel.getByLabel('Конец', { exact: true }).fill('2025-11-01T02:00');
  await panel.getByLabel('Режим сценария').selectOption('season');
  await panel.getByRole('slider', { name: 'Сезонная поправка', exact: true }).press('End');
  const result = await finished(request, await calculate(page));
  await panel.getByRole('button', { name: 'Показать результат на карте' }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('view')).toContain(run.id);
  await page.reload();
  await expect(baseSelect).toHaveValue(run.id);
  await page.getByRole('button', { name: 'Экспорт', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Экспорт данных' });
  await expect(dialog.getByLabel('Выпуск прогноза')).toHaveValue(run.id);
  await expect(dialog.getByLabel('Выпуск прогноза').locator('option:checked')).toContainText(
    'Исследование',
  );
  await dialog.getByRole('button', { name: 'Подготовить CSV как submission' }).click();
  const link = dialog.getByRole('link', { name: 'Скачать файл' });
  await expect(link).toBeVisible({ timeout: 30000 });
  const downloading = page.waitForEvent('download');
  await link.click();
  const download = await downloading;
  const csv = (await readFile((await download.path())!, 'utf8'))
    .replace(/^\uFEFF/, '')
    .trim()
    .split('\n');
  const rows = (
    await (
      await request.post('/api/v2/forecasts/query', {
        data: {
          dataset_id: base.spec.dataset_id,
          forecast_id: run.id,
          scenario_id: result.id,
          route_ids: ['17'],
          time_range: result.spec.time_range,
          mode: 'forecast',
        },
      })
    ).json()
  ).rows;
  expect(csv[0]).toBe('route;date;hour;prediction');
  expect(csv).toHaveLength(3);
  expect(csv.slice(1).map((line) => Number(line.split(';')[3]))).toEqual(
    rows.map((r: any) => Math.floor(r.value + 0.5)),
  );
  const rejected = await request.post('/api/v2/publications', { data: { forecast_id: run.id } });
  expect(rejected.status()).toBe(409);
  expect((await rejected.json()).error.code).toBe('RESEARCH_ONLY');
  expect((await (await request.get('/api/v2/capabilities')).json()).current_snapshot).toEqual(
    published,
  );
});

import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.route('https://tiles.openfreemap.org/**', (r) => r.abort());
  await page.route('https://tile.openstreetmap.org/**', (r) => r.abort());
  await page.route('https://fonts.googleapis.com/**', (r) => r.abort());
  await page.goto('/');
});

test('scenario job survives reload; finished result agrees with map and API', async ({
  page,
  request,
}) => {
  await page.getByRole('button', { name: 'Сценарии', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Сценарии', exact: true });
  await expect(panel.getByLabel('Базовый выпуск')).not.toHaveValue('');
  const baseId = await panel.getByLabel('Базовый выпуск').inputValue();
  await panel.getByRole('slider', { name: 'Сезонная поправка', exact: true }).focus();
  await page.keyboard.press('ArrowRight');
  await page.keyboard.press('ArrowRight');
  const queued = page.waitForResponse(
    (r) => /\/scenarios\/[^/]+\/runs$/.test(r.url()) && r.request().method() === 'POST',
  );
  await panel.getByRole('button', { name: 'Рассчитать сценарий', exact: true }).click();
  const job = await (await queued).json();
  // Wait for the UI to receive and save the server response before reloading it.
  await expect(panel.getByRole('region', { name: 'Результаты режимов' })).toBeVisible();
  await page.reload();
  await expect(panel).toBeVisible();
  await expect
    .poll(async () => (await (await request.get('/api/v2/jobs/' + job.id)).json()).status, {
      timeout: 60000,
    })
    .toBe('ready');
  await expect(panel.getByRole('region', { name: 'Сравнение сценария' })).toBeVisible();
  const completed = await (await request.get('/api/v2/jobs/' + job.id)).json();
  const result = completed.result;
  expect(result.spec.forecast_id).toBe(baseId);
  expect(result.total).toBeCloseTo(result.base_total * 1.1, 5);
  await expect(panel.getByRole('button', { name: 'Показать результат на карте' })).toBeEnabled();
  await panel.getByRole('button', { name: 'Показать результат на карте' }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('view')).toContain(result.id);
  const caps = await (await request.get('/api/v2/capabilities')).json();
  const scope = {
    snapshotId: caps.current_snapshot.snapshotId,
    routeIds: result.spec.route_ids,
    timeRange: result.spec.time_range,
    scenarioId: result.id,
  };
  const map = await (await request.post('/api/v1/map-snapshot', { data: scope })).json();
  const query = await (
    await request.post('/api/v2/forecasts/query', {
      data: {
        dataset_id: result.dataset_id,
        forecast_id: baseId,
        route_ids: result.spec.route_ids,
        time_range: result.spec.time_range,
        mode: 'forecast',
        scenario_id: result.id,
      },
    })
  ).json();
  const sum = query.rows.reduce((n: number, row: { value: number }) => n + row.value, 0);
  expect(sum).toBeCloseTo(result.total, 5);
  expect(
    map.frames.reduce((n: number, f: { aggregate: number }) => n + f.aggregate, 0),
  ).toBeCloseTo(sum, 5);
  await panel.getByRole('slider', { name: 'Сезонная поправка', exact: true }).press('ArrowRight');
  await expect(panel).toContainText('Параметры изменены');
  await expect(panel.getByRole('button', { name: 'Показать результат на карте' })).toBeDisabled();
  await page.screenshot({ path: 'test-results/scenarios-panel.png', fullPage: true });
  await panel.getByRole('button', { name: 'Вернуться к базе' }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get('view')).not.toContain(result.id);
});

test('data/model navigation and annual period keeps the chosen model', async ({ page }) => {
  await expect(page.locator('.v2-header-actions button')).toHaveCount(1);
  await page.getByRole('button', { name: 'Данные и модели', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Данные и модели', exact: true });
  await expect(panel.getByLabel('Набор и редакция')).not.toHaveValue('');
  await panel.getByLabel('Действие импорта').selectOption('new');
  await panel.getByLabel('Название набора').fill('Другой эксперимент');
  await expect(panel).toContainText('Редакция — сохранённое состояние');
  await expect(panel.getByText('Поправки, бюджет и выгрузка')).toHaveCount(0);
  const adapter = await panel.getByLabel('Адаптер модели').inputValue();
  const model = await panel.getByLabel('Обученная модель').inputValue();
  await panel.getByRole('button', { name: 'Год', exact: true }).click();
  await expect(panel.getByLabel('Адаптер модели')).toHaveValue(adapter);
  await expect(panel.getByLabel('Обученная модель')).toHaveValue(model);
  await expect(panel.getByLabel('Обученная модель')).not.toHaveValue('');
});

test('interactive map incident can be placed, edited and removed', async ({ page }) => {
  await page.getByRole('button', { name: 'Сценарии', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Сценарии', exact: true });
  await panel.getByRole('button', { name: 'Добавить ДТП на карте' }).click();
  await page.locator('.maplibregl-canvas').click({ position: { x: 100, y: 80 } });
  await expect(panel.getByLabel('Начало ДТП')).toBeVisible();
  await panel.getByLabel('Подтверждённые пользователем маршруты').fill('1');
  await panel.getByLabel('Длительность, минут').fill('90');
  await expect(page.getByRole('button', { name: 'ДТП 1', exact: true })).toBeVisible();
  await page.reload();
  await expect(panel.getByLabel('Базовый выпуск')).not.toHaveValue('');
  await panel.getByRole('button', { name: /ДТП ·/ }).click();
  await expect(panel.getByLabel('Длительность, минут')).toHaveValue('90');
  await panel.getByRole('button', { name: 'Удалить ДТП' }).click();
  await expect(page.getByRole('button', { name: 'ДТП 1', exact: true })).toHaveCount(0);
});

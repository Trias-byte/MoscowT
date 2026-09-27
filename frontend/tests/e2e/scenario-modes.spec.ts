import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
test.use({ actionTimeout: 10000 });

test('each factor is independent; map and submission export use its exact window', async ({
  page,
  request,
}) => {
  test.setTimeout(120000);
  await page.route('https://tiles.openfreemap.org/**', (r) => r.abort());
  await page.route('https://tile.openstreetmap.org/**', (r) => r.abort());
  await page.goto('/');
  await page.getByRole('button', { name: 'Сценарии', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Сценарии', exact: true });
  await expect(panel.getByLabel('Базовый выпуск')).not.toHaveValue('');
  const name = `Проверка режимов ${Date.now()}`;
  await panel.getByLabel('Название сценария').fill(name);
  await panel.getByLabel('Маршруты сценария').fill('');
  await panel.getByLabel('Маршруты сценария').pressSequentially('17,25');
  await expect(panel.getByLabel('Маршруты сценария')).toHaveValue('17,25');
  await panel.getByLabel('Маршруты сценария').fill('17');
  await panel.getByLabel('Начало', { exact: true }).fill('2025-11-01T08:00');
  await panel.getByLabel('Конец', { exact: true }).fill('2025-11-01T10:00');
  const slider = panel.getByRole('slider', { name: 'Сезонная поправка', exact: true });
  await slider.press('End');
  for (let i = 0; i < 10; i++) await slider.press('ArrowLeft');
  await panel.getByLabel('Температура, °C').fill('-15');
  await panel.getByLabel('Влажность, %').fill('90');
  await panel.getByLabel('Осадки, мм/ч').fill('5');
  await panel.getByText('Расписание и бюджет', { exact: true }).click();
  await panel.getByRole('combobox', { name: 'Исходное расписание', exact: true }).selectOption('');
  await panel.getByLabel('Исходный интервал, минут · допущение').fill('10');
  await panel.getByLabel('Новый интервал, минут').fill('5');
  await panel.getByRole('button', { name: 'Добавить ДТП на карте' }).click();
  await page.locator('.maplibregl-canvas').click({ position: { x: 220, y: 200 } });
  await panel.getByLabel('Подтверждённые пользователем маршруты').fill('17');
  await panel.getByLabel('Начало ДТП').fill('2025-11-01T08:00');
  await panel.getByLabel('Длительность, минут', { exact: true }).fill('120');
  await panel.getByLabel('Снижение движения, %').fill('50');
  await panel.getByRole('button', { name: 'Рассчитать все режимы отдельно' }).click();
  // Wait until all requests have been persisted before reloading.
  await expect(panel.getByRole('button', { name: 'Вернуться к базе' })).toBeEnabled();
  let completed: any[] = [];
  await expect
    .poll(
      async () => {
        const jobs = await (await request.get('/api/v2/jobs')).json();
        completed = jobs.filter(
          (j: any) =>
            j.kind === 'scenario_run' && j.status === 'ready' && j.result?.spec.name === name,
        );
        return completed.length;
      },
      { timeout: 90000 },
    )
    .toBe(5);
  await page.reload();
  const results = Object.fromEntries(completed.map((j) => [j.result.spec.mode, j.result]));
  expect(new Set(completed.map((j) => j.result.id)).size).toBe(5);
  expect(results.season.total).toBeCloseTo(results.season.base_total * 1.5, 5);
  expect(results.schedule.total).toBeCloseTo(results.schedule.base_total * 2 ** 0.3, 5);
  expect(results.incidents.total).toBeCloseTo(results.incidents.base_total * 0.5 ** 0.3, 5);
  expect(results.weather.total).not.toBeCloseTo(results.weather.base_total, 2);
  expect(results.combined.total).toBeCloseTo(results.weather.total * 1.5, 5);
  const comparison = panel.getByRole('region', { name: 'Результаты режимов' });
  await expect(comparison.getByRole('button')).toHaveCount(5);
  await comparison
    .getByRole('button', { name: 'На карту: Только сезонность', exact: true })
    .click();
  await expect
    .poll(() => JSON.parse(new URL(page.url()).searchParams.get('view')!).scenarioId)
    .toBe(results.season.id);
  const view = JSON.parse(new URL(page.url()).searchParams.get('view')!);
  expect(view.routeIds).toEqual(['17']);
  expect(view.scenarioRange).toEqual(results.season.spec.time_range);
  await expect
    .poll(async () =>
      Number((await page.locator('.v2-total strong').innerText()).replace(/[^\d,.-]/g, '').replace(',', '.')),
    )
    .toBeCloseTo(results.season.total, 0);
  await page.getByRole('button', { name: 'Экспорт', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Экспорт данных' });
  await expect(dialog.getByLabel('Результат', { exact: true })).toHaveValue(results.season.id);
  await dialog.getByRole('button', { name: 'Подготовить CSV как submission' }).click();
  await expect(dialog.getByRole('link', { name: 'Скачать файл' })).toBeVisible({ timeout: 30000 });
  const downloadPromise = page.waitForEvent('download');
  await dialog.getByRole('link', { name: 'Скачать файл' }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe('submission.csv');
  const csv = (await readFile((await download.path())!, 'utf8'))
    .replace(/^\uFEFF/, '')
    .trim()
    .split('\n');
  expect(csv[0]).toBe('route;date;hour;prediction');
  expect(csv).toHaveLength(3);
  expect(csv.slice(1).map((line) => line.split(';').slice(0, 3).join(';'))).toEqual([
    '17;2025-11-01;8',
    '17;2025-11-01;9',
  ]);
  const query = await (
    await request.post('/api/v2/forecasts/query', {
      data: {
        dataset_id: results.season.dataset_id,
        forecast_id: results.season.spec.forecast_id,
        route_ids: ['17'],
        time_range: results.season.spec.time_range,
        mode: 'forecast',
        scenario_id: results.season.id,
      },
    })
  ).json();
  expect(csv.slice(1).map((line) => Number(line.split(';')[3]))).toEqual(
    query.rows.map((r: any) => Math.floor(r.value + 0.5)),
  );
  await page.screenshot({ path: 'test-results/scenario-submission.png', fullPage: true });
});

test('scenario copies and active choices stay independent across tabs', async ({
  page,
  context,
}) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Сценарии', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Сценарии', exact: true });
  await panel.getByLabel('Название сценария').fill('Сценарий A');
  const a = await panel.getByLabel('Сохранённый сценарий').inputValue();
  const second = await context.newPage();
  await second.goto('/');
  const other = second.getByRole('complementary', { name: 'Сценарии', exact: true });
  await other.getByRole('button', { name: 'Создать копию сценария' }).click();
  await other.getByLabel('Название сценария').fill('Сценарий B');
  await other.getByLabel('Режим сценария').selectOption('season');
  await other.getByRole('slider', { name: 'Сезонная поправка', exact: true }).press('End');
  await expect(panel.getByLabel('Сохранённый сценарий')).toHaveValue(a);
  await expect(panel.getByLabel('Название сценария')).toHaveValue('Сценарий A');
  await page.reload();
  await second.reload();
  await expect(panel.getByLabel('Сохранённый сценарий')).toHaveValue(a);
  await expect(panel.getByRole('slider', { name: 'Сезонная поправка', exact: true })).toHaveValue(
    '1',
  );
  await expect(other.getByLabel('Название сценария')).toHaveValue('Сценарий B');
  await expect(other.getByRole('slider', { name: 'Сезонная поправка', exact: true })).toHaveValue(
    '2',
  );
});

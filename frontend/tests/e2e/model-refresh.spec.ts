import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// An isolated server with the real two-route, three-estimator fixture from test_model_refresh.
test('CSV import survives closing the page and publishes a refitted annual ensemble', async ({
  page,
  context,
  request,
}) => {
  test.skip(!process.env.MODEL_REFRESH_FIXTURE, 'Requires an isolated model-refresh fixture');
  test.setTimeout(120000);
  const directory = process.env.MODEL_REFRESH_FIXTURE!;
  const initial = JSON.parse(readFileSync(resolve(directory, 'fixture.json'), 'utf8'));
  await context.route(/tiles\.|tile\.|fonts.googleapis/, (route) => route.abort());
  await page.goto('/');
  await page.getByRole('button', { name: 'Данные и модели', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Данные и модели', exact: true });
  await expect(panel.getByLabel('Набор и редакция')).toHaveValue(initial.dataset_id);
  await expect(panel.getByLabel('Период прогноза: начало')).toHaveValue('2026-05-01T00:00');
  const model = await panel.getByLabel('Обученная модель').inputValue();
  await panel.getByRole('button', { name: 'Год', exact: true }).click();
  await expect(panel.getByLabel('Обученная модель')).toHaveValue(model);
  await expect(panel.getByLabel('Период прогноза: конец')).toHaveValue('2027-05-01T00:00');
  await panel
    .getByLabel('Файл истории')
    .setInputFiles(resolve(directory, 'history-2026-05-01-1.3.csv'));
  await panel.getByText(/Задания \(/).click();
  await expect(
    panel.getByRole('checkbox', { name: 'После импорта обновить модель и прогноз' }),
  ).toBeChecked();
  const applied = page.waitForResponse((r) => /\/uploads\/[^/]+\/apply$/.test(r.url()));
  await panel.getByRole('button', { name: 'Применить импорт', exact: true }).click();
  const job = await (await applied).json();
  expect(job.kind).toBe('model_refresh');
  await page.close();
  await expect
    .poll(async () => (await (await request.get(`/api/v2/jobs/${job.id}`)).json()).status, {
      timeout: 90000,
    })
    .toBe('ready');
  const completed = await (await request.get(`/api/v2/jobs/${job.id}`)).json();
  const result = completed.result;
  expect(result.publication_status).toBe('published');
  expect(result.model_id).not.toBe(initial.model_id);
  const run = await (await request.get(`/api/v2/forecast-runs/${result.forecast_id}`)).json();
  expect(run.rows).toBe(17520);
  expect(run.spec.time_range).toEqual({
    start: '2026-06-01T00:00:00+03:00',
    end: '2027-06-01T00:00:00+03:00',
  });
  const report = await (
    await request.get(`/api/v2/model-evaluations/${result.evaluation_id}`)
  ).json();
  expect(report.gate.passed).toBe(true);
  expect(report.horizons.find((h: { days: number }) => h.days === 365).status).toBe(
    'insufficient_history',
  );
  const reopened = await context.newPage();
  await reopened.goto('/');
  await expect
    .poll(() => new URL(reopened.url()).searchParams.get('view'))
    .toContain(result.snapshot_id);
  await reopened.getByRole('button', { name: 'Дата и время', exact: true }).click();
  const date = reopened.getByRole('dialog', { name: 'Дата и время' });
  await date.getByLabel('Дата', { exact: true }).fill('2027-05-31');
  await date.getByLabel('Показать').selectOption('24');
  await date.getByRole('button', { name: 'Применить', exact: true }).click();
  await expect.poll(() => new URL(reopened.url()).searchParams.get('view')).toContain('2027-05-31');
  const view = JSON.parse(new URL(reopened.url()).searchParams.get('view')!);
  const timeRange = { start: '2027-05-31T00:00:00+03:00', end: '2027-06-01T00:00:00+03:00' };
  const query = await (
    await request.post('/api/v2/forecasts/query', {
      data: {
        dataset_id: result.dataset_id,
        forecast_id: result.forecast_id,
        route_ids: ['5', '17'],
        time_range: timeRange,
        mode: 'forecast',
      },
    })
  ).json();
  const map = await (
    await request.post('/api/v1/map-snapshot', {
      data: { snapshotId: view.snapshotId, routeIds: ['5', '17'], timeRange, mode: 'forecast' },
    })
  ).json();
  expect(
    map.frames.reduce((sum: number, frame: { aggregate: number }) => sum + frame.aggregate, 0),
  ).toBeCloseTo(
    query.rows.reduce((sum: number, row: { value: number }) => sum + row.value, 0),
    5,
  );
  await reopened.getByRole('button', { name: 'Экспорт', exact: true }).click();
  const dialog = reopened.getByRole('dialog', { name: 'Экспорт данных' });
  await expect(dialog.getByLabel('Выпуск прогноза')).toHaveValue(result.forecast_id);
  await dialog.getByRole('button', { name: 'Подготовить CSV как submission' }).click();
  await expect(dialog.getByRole('link', { name: 'Скачать файл' })).toBeVisible({ timeout: 30000 });
  const [download] = await Promise.all([
    reopened.waitForEvent('download'),
    dialog.getByRole('link', { name: 'Скачать файл' }).click(),
  ]);
  const csv = readFileSync((await download.path())!, 'utf8')
    .replace(/^\uFEFF/, '')
    .trim()
    .split(/\r?\n/);
  expect(csv.shift()).toBe('route;date;hour;prediction');
  expect(csv).toHaveLength(48);
  const expected = new Map(
    query.rows.map((row: { route: string; timestamp: string; value: number }) => [
      `${row.route}:${Number(row.timestamp.slice(11, 13))}`,
      row.value,
    ]),
  );
  for (const line of csv) {
    const [route, day, hour, value] = line.split(';');
    expect(day).toBe('2027-05-31');
    expect(
      Math.abs(Number(value) - Number(expected.get(`${route}:${Number(hour)}`))),
    ).toBeLessThanOrEqual(0.5);
  }
  await reopened.screenshot({ path: 'test-results/annual-refresh-export.png', fullPage: true });
});

import { expect, test } from '@playwright/test';
import { createHash } from 'node:crypto';

test('published competition baseline is selected and exports the exact submitted CSV', async ({
  page,
  request,
  context,
}) => {
  test.skip(!process.env.COMPETITION_BASELINE, 'Requires the installed competition baseline');
  test.setTimeout(90000);
  await expect
    .poll(
      async () => {
        try {
          return (await request.get('/health/ready')).status();
        } catch {
          return 0;
        }
      },
      { timeout: 60000 },
    )
    .toBe(200);
  await context.route(/tiles\.|tile\.|fonts.googleapis/, (route) => route.abort());
  const caps = await (await request.get('/api/v2/capabilities')).json();
  const run = await (
    await request.get(`/api/v2/forecast-runs/${caps.current_snapshot.forecastId}`)
  ).json();
  expect(run.model_type).toBe('competition_catboost');
  expect(run.rows).toBe(14640);
  await page.goto('/');
  await page.getByRole('button', { name: 'Данные и модели', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Данные и модели', exact: true });
  await expect(panel.getByLabel('Обученная модель')).toHaveValue(run.spec.model_id);
  await expect(panel.getByRole('note')).toContainText('0,88');
  await expect(panel.getByLabel('Адаптер модели')).toHaveValue('competition_catboost');
  await expect(panel.getByLabel('Период прогноза: начало')).toHaveValue('2025-11-01T00:00');
  await expect(panel.getByLabel('Период прогноза: конец')).toHaveValue('2026-01-01T00:00');
  await expect(panel.getByRole('button', { name: 'Год', exact: true })).toBeDisabled();
  await panel.getByRole('button', { name: '61 день', exact: true }).click();
  await panel.getByRole('note').scrollIntoViewIfNeeded();
  await page.screenshot({ path: '../outputs/baseline/application.png', fullPage: true });

  const response = await request.post('/api/v2/exports', {
    headers: { 'Idempotency-Key': `competition-parity-${run.id}` },
    data: {
      dataset_id: run.spec.dataset_id,
      forecast_id: run.id,
      route_ids: run.spec.route_ids,
      time_range: run.spec.time_range,
      mode: 'forecast',
      grain: 'hour',
      format: 'competition',
    },
  });
  expect(response.status()).toBe(202);
  const job = await response.json();
  await expect
    .poll(async () => (await (await request.get(`/api/v2/jobs/${job.id}`)).json()).status)
    .toBe('ready');
  const downloaded = await request.get(`/api/v2/jobs/${job.id}/download`);
  expect(downloaded.status()).toBe(200);
  expect(
    createHash('sha256')
      .update(await downloaded.body())
      .digest('hex'),
  ).toBe('4adcc1b06ccd53eeddee5637560919396483809fc2da696cae3c1cba013a8d95');
});

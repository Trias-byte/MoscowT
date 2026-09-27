import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';

test('export selects whole Moscow hours and downloads all three formats across midnight', async ({
  page,
}) => {
  test.setTimeout(90000);
  await page.route('https://tiles.openfreemap.org/**', (r) => r.abort());
  await page.route('https://tile.openstreetmap.org/**', (r) => r.abort());
  await page.goto('/');
  await page.getByRole('button', { name: 'Экспорт', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Экспорт данных' });
  await expect(dialog.getByLabel('Выпуск прогноза')).not.toHaveValue('');
  await expect(dialog.locator('input[type="datetime-local"]')).toHaveCount(0);
  await dialog.getByLabel('Маршруты', { exact: true }).fill('17');
  await dialog.getByLabel('Начало выгрузки: дата').fill('2025-11-01');
  await dialog.getByLabel('Начало выгрузки: час').selectOption('23');
  await dialog.getByLabel('Конец выгрузки: дата').fill('2025-11-02');
  await dialog.getByLabel('Конец выгрузки: час').selectOption('00');
  for (const button of ['Подготовить CSV как submission', 'Подробный CSV', 'Parquet + manifest']) {
    const response = page.waitForResponse(
      (r) => r.url().endsWith('/api/v2/exports') && r.request().method() === 'POST',
    );
    await dialog.getByRole('button', { name: button, exact: true }).click();
    const created = await response;
    expect(created.status()).toBe(202);
    expect(created.request().postDataJSON().time_range).toEqual({
      start: '2025-11-01T23:00:00+03:00',
      end: '2025-11-02T00:00:00+03:00',
    });
    await expect(dialog.getByRole('link', { name: 'Скачать файл' })).toBeVisible({
      timeout: 30000,
    });
    const downloaded = page.waitForEvent('download');
    await dialog.getByRole('link', { name: 'Скачать файл' }).click();
    const download = await downloaded;
    expect(await download.failure()).toBeNull();
    if (button === 'Подготовить CSV как submission') {
      const rows = (await readFile((await download.path())!, 'utf8'))
        .replace(/^\uFEFF/, '')
        .trim()
        .split('\n');
      expect(rows).toHaveLength(2);
      expect(rows[0]).toBe('route;date;hour;prediction');
      expect(rows[1]).toMatch(/^17;2025-11-01;23;\d+$/);
    }
  }
  await dialog.getByLabel('Начало выгрузки: дата').fill('');
  await expect(dialog.getByRole('alert')).toContainText('Укажите дату');
  await expect(
    dialog.getByRole('button', { name: 'Подготовить CSV как submission' }),
  ).toBeDisabled();
  await dialog.getByLabel('Начало выгрузки: дата').fill('2025-11-03');
  await expect(dialog.getByRole('alert')).toContainText('позже начала');
  await expect(dialog.getByRole('button', { name: 'Подробный CSV' })).toBeDisabled();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
});

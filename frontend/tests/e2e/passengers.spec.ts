import { test, expect } from '@playwright/test';

test.beforeEach(async ({ page, request }) => {
  await page.route('https://tile.openstreetmap.org/**', (r) => r.abort());
  await page.route('https://tiles.openfreemap.org/**', (r) => r.abort());
  const caps = await (await request.get('/api/v1/capabilities')).json();
  await page.goto(
    '/?view=' +
      encodeURIComponent(
        JSON.stringify({
          contractVersion: 2,
          snapshotId: caps.snapshotId,
          routeIds: ['1'],
          mode: 'history',
          windowHours: 24,
          date: '2025-09-01',
          index: 0,
        }),
      ),
  );
  await expect(page.getByRole('heading', { name: 'Пассажиропоток', exact: true })).toBeVisible();
});

test('categories reconcile, filter and export; fixed research periods stay explicit', async ({
  page,
  request,
}) => {
  await page.getByRole('tab', { name: 'Пассажиры', exact: true }).click();
  const panel = page.getByLabel('Пассажирская аналитика', { exact: true });
  await expect(panel.locator('tbody tr')).toHaveCount(6);
  const view = JSON.parse(new URL(page.url()).searchParams.get('view')!);
  const response = await request.post('/api/v2/passengers/query', {
    data: {
      snapshot_id: view.snapshotId,
      route_ids: ['1'],
      time_range: { start: '2025-09-01T00:00:00+03:00', end: '2025-09-02T00:00:00+03:00' },
    },
  });
  const data = await response.json();
  expect(data.summary.reduce((n: number, r: { boardings: number }) => n + r.boardings, 0)).toBe(
    data.total,
  );
  await panel.getByLabel('Категория билета').selectOption('social');
  await expect(panel.locator('tbody tr')).toHaveCount(1);
  await expect(panel.locator('tbody')).toContainText('Социальные льготники');
  const download = page.waitForEvent('download');
  await panel.getByRole('button', { name: 'Скачать CSV', exact: true }).click();
  const file = await download;
  expect(file.suggestedFilename()).toBe('passengers-categories.csv');
  await file.saveAs('test-results/passenger-categories.csv');
  await page.screenshot({ path: 'test-results/passenger-categories.png', fullPage: true });
  await panel.getByRole('tab', { name: 'Сезонность', exact: true }).click();
  await expect(panel).toContainText('Месячные профили всей сети');
  await expect(panel.getByRole('img', { name: 'Сезонность категорий по всей сети' })).toBeVisible();
  await panel.getByRole('tab', { name: 'Когорты', exact: true }).click();
  await expect(panel).toContainText('Выбор маршрутов и даты карты не меняет эту сводку');
  await expect(
    panel.getByRole('img', { name: 'Доля активных карт весенней когорты' }),
  ).toBeVisible();
  await page.screenshot({ path: 'test-results/passenger-cohorts.png', fullPage: true });
});

test('route and stop context, dated POIs and radius controls', async ({ page }) => {
  const details = page.getByRole('complementary', { name: 'Сведения об объекте' });
  await expect(
    details.getByRole('region', { name: 'Состав пассажиропотока маршрута' }),
  ).toContainText('Социальные льготники');
  await details.getByText('Инфраструктура в радиусе 500 м', { exact: true }).click();
  await expect(details).toContainText('Срез 2025-09-01');
  await page.getByLabel('Инфраструктура вокруг остановок', { exact: true }).check();
  await page.getByLabel('Тип объектов', { exact: true }).selectOption('school');
  const objects = page
    .getByLabel('Объект инфраструктуры', { exact: true })
    .filter({ has: page.locator('option') });
  await expect.poll(() => objects.locator('option').count()).toBeGreaterThan(1);
  await objects.selectOption({ index: 1 });
  await expect(page.getByRole('article', { name: 'Объект инфраструктуры' })).toContainText(
    'Срез 2025-09-01',
  );
  await page.getByLabel('Радиус инфраструктуры').selectOption('1000');
  await expect(details.getByText('Инфраструктура в радиусе 1000 м', { exact: true })).toBeVisible();
  await expect(page.getByRole('article', { name: 'Объект инфраструктуры' })).toHaveCount(0);
  await page.screenshot({ path: 'test-results/passenger-infrastructure.png', fullPage: true });
  await details.getByText('Остановки маршрута', { exact: true }).click();
  await details.locator('.v2-route-stops ol button').first().click();
  await details.getByText('Инфраструктура в радиусе 1000 м', { exact: true }).click();
  await expect(details).toContainText('окружение остановки');
  await expect(details).toContainText('Отдельный числовой прогноз остановки недоступен');
});

test('future dates have no category forecast; passport shows research comparison', async ({
  page,
}) => {
  await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Дата', { exact: true }).fill('2025-11-01');
  await dialog.getByRole('button', { name: 'Применить', exact: true }).click();
  await page.getByRole('tab', { name: 'Пассажиры', exact: true }).click();
  await expect(page.getByLabel('Пассажирская аналитика', { exact: true })).toContainText(
    'Категориального прогноза нет',
  );
  await page.getByRole('button', { name: 'Данные и модели', exact: true }).click();
  const panel = page.getByRole('complementary', { name: 'Данные и модели', exact: true });
  await panel.getByText('Пассажирский набор · категории и инфраструктура', { exact: true }).click();
  await expect(panel).toContainText('59');
  await expect(panel).toContainText('не сравнение с текущей рабочей моделью');
  await expect(panel.locator('.passenger-passport tbody tr')).toHaveCount(250);
});

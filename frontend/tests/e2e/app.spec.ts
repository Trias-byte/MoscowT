import { test, expect } from '@playwright/test';
test.beforeAll(async ({ request }) => {
  await expect
    .poll(
      async () => {
        try {
          return (await request.get('/health/ready')).status();
        } catch {
          return 0;
        }
      },
      { timeout: 30000 },
    )
    .toBe(200);
});
test.beforeEach(async ({ page }) => {
  await page.route('https://tiles.openfreemap.org/**', (r) => r.abort());
  await page.route('https://tile.openstreetmap.org/**', (r) => r.abort());
  await page.route('https://fonts.googleapis.com/**', (r) => r.abort());
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Пассажиропоток', exact: true })).toBeVisible();
  await expect(page.getByText('Загружаем выбранный период…')).toHaveCount(0);
});
test('all routes and unsupported endpoint gating', async ({ page }) => {
  const bad: string[] = [];
  page.on('request', (r) => {
    if (/\/stream|\/route-profile/.test(r.url())) bad.push(r.url());
  });
  await page.reload();
  await page.getByRole('tab', { name: 'Таблица', exact: true }).click();
  await expect(page.locator('tbody tr')).toHaveCount(10);
  await page.getByRole('button', { name: 'Выбрать маршрут 17', exact: true }).click();
  await expect(page.locator('.v2-details')).toContainText('Трасса по трамвайным путям');
  await expect(page.locator('.v2-detail-metric strong')).not.toHaveText('Нет данных');
  expect(bad).toEqual([]);
});
test('Moscow hour selection and keyboard', async ({ page }) => {
  await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
  const d = page.getByRole('dialog');
  await d.getByRole('combobox', { name: 'Показать', exact: true }).selectOption('1');
  await d.getByRole('combobox', { name: 'Время · МСК', exact: true }).selectOption('8');
  await expect(d).toContainText('08:00–09:00');
  await d.getByRole('button', { name: 'Применить' }).click();
  const slider = page.getByRole('slider');
  await slider.focus();
  await page.keyboard.press('ArrowRight');
  await expect(slider).toHaveValue('9');
  await expect(slider).toHaveAttribute('aria-valuetext', /09:00–10:00/);
  await page.keyboard.press('End');
  await expect(slider).toHaveValue('23');
  await expect(slider).toHaveAttribute('aria-valuetext', /23:00–24:00/);
});
test('day slider selects an hour with a pointer and playback works from the day total', async ({
  page,
}) => {
  const slider = page.getByRole('slider');
  await expect(slider).toBeVisible();
  await expect(slider).toBeEnabled();
  await expect(slider).toHaveAttribute('max', '23');
  const box = (await slider.boundingBox())!;
  await slider.click({ position: { x: box.width / 2, y: box.height / 2 } });
  await expect(page.getByRole('button', { name: 'Час', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  expect(Number(await slider.inputValue())).toBeGreaterThanOrEqual(11);
  expect(Number(await slider.inputValue())).toBeLessThanOrEqual(12);
  expect(Math.abs((await slider.boundingBox())!.y - box.y)).toBeLessThan(1);
  await page.getByRole('button', { name: 'Весь день', exact: true }).click();
  // The start of the day must also be selectable when the thumb is already at zero.
  await slider.press('Home');
  await expect(page.locator('.v2-time-heading')).toContainText('00:00–01:00');
  await page.getByRole('button', { name: 'Весь день', exact: true }).click();
  await page.getByRole('button', { name: 'Воспроизвести', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Час', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect.poll(async () => Number(await slider.inputValue())).toBeGreaterThan(0);
  await page.getByRole('button', { name: 'Пауза', exact: true }).click();
});
test('section preserves route totals', async ({ page }) => {
  await page.getByRole('tab', { name: 'Таблица', exact: true }).click();
  const total = await page.locator('.v2-total strong').innerText();
  await page.getByLabel('Направление маршрута № 1').selectOption({ index: 1 });
  await page.getByLabel('Начальная остановка', { exact: true }).selectOption({ index: 2 });
  await page.getByLabel('Конечная остановка', { exact: true }).selectOption({ index: 5 });
  await page.getByRole('button', { name: 'Показать только участок' }).click();
  await expect(page.getByRole('status')).toContainText('только карту');
  await expect(page.locator('tbody tr')).toHaveCount(10);
  await expect(page.locator('.v2-total strong')).toHaveText(total);
});
test('late-2025 routes have real paths and searchable stops', async ({ page, request }) => {
  const response = await request.get('/api/v1/network?date=2025-12-31');
  const network = await response.json();
  expect(network.geometryQuality).toBe('mapped');
  expect(network.missingGeometryRouteIds).toEqual([]);
  for (const route of network.routes.filter((r: { isTarget: boolean }) => r.isTarget)) {
    expect(
      network.patterns.filter((p: { routeId: string }) => p.routeId === route.id),
    ).toHaveLength(2);
  }
  await page.getByRole('button', { name: 'Выбрать маршрут 17', exact: true }).click();
  await expect(page.locator('.v2-details h2')).toContainText('Медведково');
  await page.getByText('Остановки маршрута', { exact: true }).click();
  await expect(page.locator('.v2-route-stops')).toContainText('Останкино');
  await expect(page.getByRole('link', { name: 'Маршрут в OpenStreetMap ↗' })).toHaveAttribute(
    'href',
    /openstreetmap.org\/relation\//,
  );
  await page.getByLabel('Поиск остановок').fill('Медведково');
  await page.locator('.v2-search-results button').first().click();
  await expect(page.locator('.v2-details h2')).toContainText('Медведково');
  await expect(page.getByRole('link', { name: 'Остановка в OpenStreetMap ↗' })).toHaveAttribute(
    'href',
    /openstreetmap.org\/node\//,
  );
});
test('old November-only URL modes migrate to a single January day', async ({ page }) => {
  const base = JSON.parse(new URL(page.url()).searchParams.get('view')!);
  for (const horizon of ['month', 'competition_61d']) {
    const legacy = {
      ...base,
      windowHours: undefined,
      mode: 'forecast',
      horizon,
      date: '2025-01-01',
      endDate: '2025-11-02',
      index: 30,
    };
    await page.goto(`/?${new URLSearchParams({ view: JSON.stringify(legacy) })}`);
    await expect(page.getByLabel('Источник данных', { exact: true })).toHaveText(
      'Фактические данные',
    );
    await expect(page.getByRole('button', { name: 'Весь день', exact: true })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await expect(page.locator('.v2-map-note')).toContainText('2025-01-01');
    await expect(page.locator('.v2-details')).toContainText('Версия OSM от 2025-01-01');
    const saved = JSON.parse(new URL(page.url()).searchParams.get('view')!);
    expect(saved).not.toHaveProperty('horizon');
    expect(saved).not.toHaveProperty('endDate');
    await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
    await expect(page.getByRole('dialog').locator('input[type=date]')).toHaveCount(1);
    await page.keyboard.press('Escape');
  }
});
test('calendar automatically switches actuals and forecasts across the full year', async ({
  page,
  request,
}) => {
  const capabilities = await (await request.get('/api/v1/capabilities')).json();
  await page.getByRole('tab', { name: 'Таблица', exact: true }).click();
  for (const [date, source] of [
    ['2025-01-01', 'Фактические данные'],
    ['2025-07-15', 'Фактические данные'],
    ['2025-10-31', 'Фактические данные'],
    ['2025-11-01', 'Прогноз'],
    ['2025-12-31', 'Прогноз'],
  ]) {
    await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
    const input = page.getByRole('dialog').getByLabel('Дата', { exact: true });
    await expect(input).toHaveAttribute('min', '2025-01-01');
    await expect(input).toHaveAttribute('max', '2025-12-31');
    await input.fill(date);
    await page.getByRole('button', { name: 'Применить', exact: true }).click();
    const saved = JSON.parse(new URL(page.url()).searchParams.get('view')!);
    expect(saved.mode).toBe('auto');
    expect(saved.date).toBe(date);
    const explicit = await request.post('/api/v1/map-snapshot', {
      data: {
        routeIds: capabilities.targetRouteIds,
        snapshotId: capabilities.snapshotId,
        mode: source === 'Прогноз' ? 'forecast' : 'history',
        grain: 'day',
        timeRange: {
          start: `${date}T00:00:00+03:00`,
          end: new Date(Date.parse(`${date}T00:00:00+03:00`) + 86400000).toISOString(),
        },
      },
    });
    expect(explicit.status()).toBe(200);
    await expect(page.getByLabel('Источник данных', { exact: true })).toHaveText(source);
    await expect(page.locator('tbody tr')).toHaveCount(10);
    await expect(page.locator('tbody tr').first()).toContainText(source);
    const expected = (await explicit.json()).frames[0].values;
    for (let i = 0; i < expected.length; i++) {
      await expect(page.locator('tbody tr').nth(i).getByRole('cell').nth(1)).toHaveText(
        new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(expected[i].value),
      );
    }
    if (date === '2025-01-01') {
      await page.screenshot({ path: 'test-results/january-actuals.png', fullPage: true });
    }
  }
});

test('12-hour and whole-day totals match hourly data and survive reloading', async ({
  page,
  request,
}) => {
  await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Дата', { exact: true }).fill('2025-01-01');
  await dialog.getByRole('combobox', { name: 'Показать', exact: true }).selectOption('12');
  await dialog.getByRole('combobox', { name: 'Начало · МСК', exact: true }).selectOption('8');
  await expect(dialog).toContainText('08:00–20:00');
  await dialog.getByRole('button', { name: 'Применить' }).click();
  const slider = page.getByRole('slider');
  await expect(slider).toHaveAttribute('max', '12');
  await expect(slider).toHaveValue('8');
  await expect(page.getByLabel('Источник данных', { exact: true })).toHaveText(
    'Фактические данные',
  );
  await page.getByRole('tab', { name: 'Таблица', exact: true }).click();
  const saved = JSON.parse(new URL(page.url()).searchParams.get('view')!);
  const response = await request.post('/api/v1/map-snapshot', {
    data: {
      routeIds: saved.routeIds,
      snapshotId: saved.snapshotId,
      mode: 'auto',
      timeRange: { start: '2025-01-01T00:00:00+03:00', end: '2025-01-02T00:00:00+03:00' },
    },
  });
  expect(response.status()).toBe(200);
  const frames = (await response.json()).frames;
  const fmt = (n: number) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(n);
  async function checkWindow(start: number, hours: number) {
    let total = 0;
    for (let r = 0; r < saved.routeIds.length; r++) {
      const sum = frames
        .slice(start, start + hours)
        .reduce((s: number, f: { values: { value: number }[] }) => s + f.values[r].value, 0);
      total += sum;
      await expect(page.locator('tbody tr').nth(r).getByRole('cell').nth(1)).toHaveText(fmt(sum));
    }
    await expect(page.locator('.v2-total strong')).toHaveText(fmt(total));
  }
  await checkWindow(8, 12);
  await page.reload();
  await expect(slider).toHaveValue('8');
  await page.getByRole('tab', { name: 'Таблица', exact: true }).click();
  await checkWindow(8, 12);
  await slider.focus();
  await page.keyboard.press('End');
  await expect(slider).toHaveAttribute('aria-valuetext', /12:00–24:00/);
  await checkWindow(12, 12);
  await page.getByRole('button', { name: 'Весь день', exact: true }).click();
  await expect(slider).toBeVisible();
  await expect(slider).toBeEnabled();
  await expect(slider).toHaveAttribute('max', '23');
  await expect(page.locator('.v2-time-heading')).toContainText('00:00–24:00');
  await checkWindow(0, 24);
  await page.screenshot({ path: 'test-results/full-day-january.png', fullPage: true });
  await slider.focus();
  await page.keyboard.press('End');
  await expect(slider).toHaveValue('23');
  await expect(slider).toHaveAttribute('aria-valuetext', /23:00–24:00/);
  await expect(page.getByRole('button', { name: 'Час', exact: true })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  await expect(page.locator('.v2-map-note')).toContainText('2025-01-01');
  await checkWindow(23, 1);
  await page.reload();
  await expect(slider).toHaveValue('23');
  await expect(page.locator('.v2-time-heading')).toContainText('1 янв. · 23:00–24:00');
});
test('vehicle estimates drive the same hourly scale for one, twelve and twenty-four hours', async ({
  page,
  request,
}) => {
  const capabilities = await (await request.get('/api/v1/capabilities')).json();
  expect(capabilities.vehicleLoadThresholds).toEqual([5, 20, 34, 50]);
  const legend = page.locator('.v2-legend');
  const labels = ['0', '5', '20', '34', '50+'];
  const fmt = (n: number) => new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(n);
  for (const date of ['2025-10-31', '2025-11-01']) {
    await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('Дата', { exact: true }).fill(date);
    await dialog.getByRole('combobox', { name: 'Показать', exact: true }).selectOption('1');
    await dialog.getByRole('combobox', { name: 'Время · МСК', exact: true }).selectOption('8');
    await dialog.getByRole('button', { name: 'Применить' }).click();
    await page.getByRole('button', { name: 'Выбрать маршрут 17', exact: true }).click();
    const response = await request.post('/api/v1/map-snapshot', {
      data: {
        snapshotId: capabilities.snapshotId,
        routeIds: ['17'],
        mode: 'auto',
        timeRange: {
          start: `${date}T00:00:00+03:00`,
          end: new Date(Date.parse(`${date}T00:00:00+03:00`) + 86400000).toISOString(),
        },
      },
    });
    expect(response.status()).toBe(200);
    const data = await response.json();
    expect(data.meta.fleetId).toBe(capabilities.fleetId);
    expect(data.meta.fleetMethod).toBe('mean_garage_activity_5min_short_gaps_20min');
    await expect(page.getByTestId('fleet-method')).toContainText('5-минутным');
    for (const [hours, start, label] of [
      [1, 8, 'Час'],
      [12, 8, '12 часов'],
      [24, 0, 'Весь день'],
    ] as const) {
      await page.getByRole('button', { name: label, exact: true }).click();
      const selected = data.frames
        .slice(start, start + hours)
        .map((f: { values: { value: number; vehicleHours: number }[] }) => f.values[0]);
      const vehicles = selected.reduce(
        (n: number, v: { vehicleHours: number }) => n + v.vehicleHours,
        0,
      );
      expect(selected.every((v: { vehicleHours: number | null }) => v.vehicleHours !== null)).toBe(
        true,
      );
      const total = selected.reduce((n: number, v: { value: number }) => n + v.value, 0);
      await expect(page.getByTestId('fleet-vehicles')).toHaveText(fmt(vehicles / hours));
      await expect(page.getByTestId('fleet-load')).toHaveText(fmt(total / vehicles));
      await expect(page.getByTestId('fleet-source')).toContainText(
        date === '2025-10-31' ? 'по бортовым номерам' : 'по предыдущим 8 неделям',
      );
      await expect(legend).toContainText('на вагон в час');
      await expect(legend.locator('span')).toHaveText(labels);
    }
  }
  await page.screenshot({ path: 'test-results/fleet-load-day.png', fullPage: true });
  await page.getByRole('button', { name: 'Выбрать маршрут 5', exact: true }).click();
  await expect(page.getByTestId('fleet-load')).toHaveText('Нет данных');
});
test('daily service dataset is downloadable and partial archive coverage is explicit', async ({
  page,
  request,
}) => {
  await page.getByRole('button', { name: 'О данных', exact: true }).click();
  const dataset = page.getByTestId('service-dataset');
  await expect(dataset).toContainText('Архив расписаний неполный');
  const link = dataset.getByRole('link', { name: 'По дням · CSV', exact: true });
  const path = await link.getAttribute('href');
  const response = await request.get(path!);
  expect(response.status()).toBe(200);
  expect(response.headers()['content-disposition']).toContain('daily_service_2025.csv');
  const text = await response.text();
  expect(text.split(/\r?\n/).filter(Boolean)).toHaveLength(3651);
  expect(text).toContain('interval_applies_to_date_confirmed');
});
test('user CSV contains the selected 12 hours and full submission stays complete', async ({
  page,
}) => {
  await page.getByRole('button', { name: '12 часов', exact: true }).click();
  await page.getByRole('slider').focus();
  await page.keyboard.press('End');
  await page.getByRole('button', { name: 'Выбрать маршрут 17', exact: true }).click();
  await page.getByRole('button', { name: 'Только выбранный', exact: true }).click();
  await expect(page.getByText('Загружаем выбранный период…')).toHaveCount(0);
  await page.getByRole('button', { name: 'Экспорт', exact: true }).click();
  let d = page.getByRole('dialog');
  await expect(d).toContainText('№ 17');
  let pending = page.waitForEvent('download');
  await d.getByRole('button', { name: 'Скачать CSV' }).click();
  let download = await pending;
  let stream = await download.createReadStream();
  let csv = '';
  for await (const chunk of stream!) csv += chunk.toString();
  expect(csv.trim().split(/\r?\n/)).toHaveLength(13);
  expect(csv).toContain('T12:00:00+03:00');
  expect(csv).toContain('T23:00:00+03:00');
  expect(csv).not.toContain('T11:00:00+03:00');
  expect(csv).toContain('17;successful_validations');
  expect(csv).toContain('validations_per_vehicle_hour');
  expect(csv).toContain('fleet_source');
  await page.getByRole('button', { name: 'Экспорт', exact: true }).click();
  d = page.getByRole('dialog');
  await d.getByLabel(/Конкурсный submission/).check();
  pending = page.waitForEvent('download');
  await d.getByRole('button', { name: 'Скачать CSV' }).click();
  download = await pending;
  stream = await download.createReadStream();
  csv = '';
  for await (const chunk of stream!) csv += chunk.toString();
  expect(csv.trim().split(/\r?\n/)).toHaveLength(14641);
  expect(csv).toContain('5;2025-11-01;0;');
  expect(download.suggestedFilename()).toBe('submission.csv');
});
test('section cards distinguish directions and keep route totals intact', async ({ page }) => {
  await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Дата', { exact: true }).fill('2025-10-31');
  await dialog.getByRole('combobox', { name: 'Показать', exact: true }).selectOption('1');
  await dialog.getByRole('combobox', { name: 'Время · МСК', exact: true }).selectOption('8');
  await dialog.getByRole('button', { name: 'Применить' }).click();
  await page.getByRole('button', { name: 'Выбрать маршрут 17', exact: true }).click();
  const sections = page.getByRole('region', { name: 'Участки маршрута' });
  await expect(sections).toBeVisible();
  await expect(page.getByLabel('Источник данных', { exact: true })).toHaveText(
    'Фактические данные',
  );
  const choices = page.getByRole('combobox', { name: 'Выбрать участок маршрута', exact: true });
  await expect(choices.locator('optgroup')).toHaveCount(2);
  const routeTotal = await page.locator('.v2-detail-metric strong').innerText();
  const networkTotal = await page.locator('.v2-total strong').innerText();
  const directions = await choices.locator('optgroup').evaluateAll((groups) =>
    groups.map((g) => ({
      name: g.getAttribute('label'),
      id: g.querySelector('option')!.value,
    })),
  );
  expect(directions).toHaveLength(2);
  expect(directions[0].name).not.toBe(directions[1].name);
  for (const direction of directions) {
    await choices.selectOption(direction.id);
    await expect(page.getByTestId('section-direction')).toContainText(direction.name!);
    await expect(page.getByTestId('section-model-note')).toContainText('Места валидаций');
    await expect(page.getByTestId('section-load')).not.toHaveText('Нет данных');
    await expect(page.locator('.v2-details h2')).toContainText('→');
    await expect(page.locator('.v2-total strong')).toHaveText(networkTotal);
    await page
      .getByRole('button', { name: 'Показать значения всего маршрута', exact: true })
      .click();
    await expect(page.locator('.v2-detail-metric strong')).toHaveText(routeTotal);
  }
  // Adjacent sections need their own values, rather than a copied route-wide color.
  const options = await choices.locator('option').allTextContents();
  expect(new Set(options.slice(1).map((s) => s.split(' · ')[1])).size).toBeGreaterThan(2);
  await expect(page.locator('.v2-legend')).toContainText('модельная оценка');
  await expect(sections.locator('.v2-section-item').first()).toBeVisible();
  await sections.locator('.v2-section-item').first().click();
  await page.screenshot({ path: 'test-results/section-model.png', fullPage: true });
});
test('heatmap selects route and time', async ({ page }) => {
  await page.getByRole('tab', { name: 'Тепловая матрица', exact: true }).click();
  const chart = page.locator('.v2-analytics .chart'),
    box = (await chart.boundingBox())!;
  await chart.click({
    position: { x: 65 + ((box.width - 110) * 6.5) / 24, y: 12 + ((box.height - 47) * 0.5) / 10 },
  });
  await expect(page.getByRole('slider')).toHaveValue('6');
  await expect(page.locator('.v2-detail-metric small')).toContainText('06:00–07:00');
});
test('stop unavailable and modal focus', async ({ page }) => {
  await page.getByLabel('Поиск остановок').fill('Метро');
  await page.locator('.v2-search-results button').first().click();
  await expect(page.locator('.v2-details')).toContainText(
    'Отдельный числовой прогноз остановки недоступен',
  );
  const trigger = page.getByRole('button', { name: 'Дата и время', exact: true });
  await trigger.click();
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press('Tab');
    expect(await page.evaluate(() => !!document.activeElement?.closest('dialog'))).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(trigger).toBeFocused();
});
test('desktop and mobile layout', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.screenshot({ path: 'test-results/backend-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await expect(page.getByRole('button', { name: 'Весь день', exact: true })).toBeVisible();
  await expect(page.getByRole('slider')).toBeVisible();
  await expect(page.getByRole('slider')).toBeEnabled();
  await expect(page.getByText('Загружаем выбранный период…')).toHaveCount(0);
  await page.getByRole('button', { name: 'Маршруты', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Трамвайная сеть' })).toBeVisible();
  await page.getByRole('button', { name: 'Скрыть управление' }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: 'test-results/backend-mobile.png', fullPage: true });
  expect(errors).toEqual([]);
});

test('map follows active day and route 5 opening boundary', async ({ page }) => {
  await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('Дата', { exact: true }).fill('2025-12-15');
  await dialog.getByRole('button', { name: 'Применить' }).click();
  await page.getByRole('button', { name: 'Выбрать маршрут 5', exact: true }).click();
  await expect(page.locator('.v2-map-note')).toContainText('2025-12-15');
  await expect(page.locator('.v2-details')).toContainText('География отсутствует');
  await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
  await dialog.getByLabel('Дата', { exact: true }).fill('2025-12-16');
  await dialog.getByRole('button', { name: 'Применить' }).click();
  await expect(page.locator('.v2-map-note')).toContainText('2025-12-16');
  await expect(page.locator('.v2-details')).toContainText('Трасса по трамвайным путям');
  await page.reload();
  await expect(page.locator('.v2-map-note')).toContainText('2025-12-16');
});

test('separate route CSV upload validates and round-trips current geometry', async ({ page }) => {
  await page.getByRole('button', { name: 'Загрузить маршрут', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Загрузка маршрута из CSV' });
  await dialog.getByLabel('CSV маршрута').setInputFiles({
    name: 'bad.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from('route;value\n1;2'),
  });
  await expect(dialog.getByRole('alert')).toContainText('Столбцы');
  await expect(dialog.getByRole('button', { name: 'Добавить маршрут на карту' })).toBeDisabled();
  const pending = page.waitForEvent('download');
  await dialog.getByRole('button', { name: 'Скачать маршрут № 1 для изменения' }).click();
  const stream = await (await pending).createReadStream();
  let csv = '';
  for await (const chunk of stream!) csv += chunk.toString();
  expect(csv).toContain('point_order;longitude;latitude');
  await dialog
    .getByLabel('CSV маршрута')
    .setInputFiles({ name: 'route.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) });
  await expect(dialog.getByRole('region', { name: 'Проверенный маршрут' })).toContainText(
    'Указанные направления заменят',
  );
  await expect(dialog.getByRole('button', { name: 'Добавить маршрут на карту' })).toBeEnabled();
  await page.screenshot({ path: 'test-results/route-upload-preview.png', fullPage: true });
});

test('CSV publication persists and obeys effective dates', async ({ page }) => {
  test.skip(
    process.env.MOSCOWT_E2E_MUTATIONS !== 'true',
    'Run only against an isolated state directory',
  );
  await page.getByRole('button', { name: 'Загрузить маршрут', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Загрузка маршрута из CSV' });
  const csv =
    'route_id;route_name;direction;valid_from;valid_to;point_order;longitude;latitude;stop_id;stop_name\n' +
    'Тест99;Проверка загрузки;0;2025-07-01;2025-07-02;1;37.620;55.750;a;Первая\n' +
    'Тест99;Проверка загрузки;0;2025-07-01;2025-07-02;2;37.621;55.751;;\n' +
    'Тест99;Проверка загрузки;0;2025-07-01;2025-07-02;3;37.622;55.752;b;Вторая\n';
  await dialog
    .getByLabel('CSV маршрута')
    .setInputFiles({ name: 'test-route.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) });
  await expect(dialog).toContainText('Пассажиропоток для нового номера отсутствует');
  await dialog.getByRole('button', { name: 'Добавить маршрут на карту' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('.v2-map-note')).toContainText('2025-07-01');
  await expect(page.locator('.v2-custom-routes')).toContainText('Тест99');
  await expect(page.locator('.v2-details')).toContainText('Трасса из CSV пользователя');
  await page.reload();
  await expect(page.locator('.v2-custom-routes')).toContainText('Тест99');
  await page.getByRole('button', { name: 'Дата и время', exact: true }).click();
  await page.getByRole('dialog').getByLabel('Дата', { exact: true }).fill('2025-07-02');
  await page.getByRole('button', { name: 'Применить', exact: true }).click();
  await expect(page.locator('.v2-map-note')).toContainText('2025-07-02');
  await expect(page.locator('.v2-custom-routes')).toHaveCount(0);
});

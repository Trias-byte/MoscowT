import { afterEach, expect, it, vi } from 'vitest';
import { makeDemo } from '../../src/data/demo';
import type { Scope } from '../../src/lib/contracts';
const scope: Scope = {
  routeIds: ['1', '5', '17'],
  metric: 'successful_validations',
  metricScope: 'route',
  mode: 'forecast',
  grain: 'hour',
  aggregation: 'sum',
  snapshotId: 'snapshot-demo-v2',
  timeRange: { start: '2025-11-01T00:00:00+03:00', end: '2025-11-02T00:00:00+03:00' },
  geometry: { patternIds: [], section: null, bbox: null, referenceMode: 'reference' },
};
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});
function response(path: string, data = makeDemo(scope)) {
  return path.endsWith('map-snapshot')
    ? data
    : path.endsWith('timeseries')
      ? { meta: data.meta, series: data.series }
      : path.endsWith('route-comparison')
        ? { meta: data.meta, routes: data.comparison }
        : data.heatmap;
}
it('requests supported views and forwards cancellation', async () => {
  vi.stubEnv('VITE_DEMO', 'false');
  const controller = new AbortController();
  const fetch = vi.fn(async (path: string, o: RequestInit) => {
    expect(o.signal).toBe(controller.signal);
    expect(path).not.toMatch(/route-profile|stream/);
    return Response.json(response(path));
  });
  vi.stubGlobal('fetch', fetch);
  const { getView } = await import('../../src/lib/api');
  const data = await getView(scope, controller.signal);
  expect(data.frames).toHaveLength(24);
  expect(fetch).toHaveBeenCalledTimes(4);
});
it.each(['snapshotId', 'fleetId'])('rejects mixed %s versions', async (field) => {
  vi.stubEnv('VITE_DEMO', 'false');
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string) => {
      const v = response(path);
      return Response.json(
        path.endsWith('heatmap') ? { ...v, meta: { ...v.meta, [field]: 'wrong' } } : v,
      );
    }),
  );
  const { getView } = await import('../../src/lib/api');
  await expect(getView(scope)).rejects.toThrow('Версии');
});
it('rejects incomplete route rows', async () => {
  vi.stubEnv('VITE_DEMO', 'false');
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string) => {
      const data = makeDemo(scope);
      data.series.pop();
      return Response.json(response(path, data));
    }),
  );
  const { getView } = await import('../../src/lib/api');
  await expect(getView(scope)).rejects.toThrow('Неполный');
});
it('never substitutes demo for API failures', async () => {
  vi.stubEnv('VITE_DEMO', 'false');
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json({ error: { message: 'Снимок отсутствует' } }, { status: 404 })),
  );
  const { getView } = await import('../../src/lib/api');
  await expect(getView(scope)).rejects.toThrow('Снимок отсутствует');
});
it('CSV retains missing values and routes without geography', async () => {
  const { createDemoCsv } = await import('../../src/lib/api');
  const data = makeDemo({ ...scope, routeIds: ['17', '28'] });
  expect(data.frames[3].aggregate).toBeNull();
  const csv = createDemoCsv([data.frames[3]], scope);
  expect(csv).toContain('17;');
  expect(csv).toContain('28;;');
});

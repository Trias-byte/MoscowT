import { ApiClient } from '../services/ApiClient';
import {
  API_V1_BASE,
  DEMO,
  DOWNLOAD_REVOKE_MS,
  EXPORT_POLL_MS,
  EXPORT_TIMEOUT_MS,
} from '../constants/api';
import { z } from 'zod';
import {
  CapabilitiesSchema,
  NetworkSchema,
  SnapshotSchema,
  SeriesSchema,
  ComparisonSchema,
  HeatmapSchema,
  JobSchema,
  GeometrySchema,
} from './contracts';
import type { Scope, ViewData, Frame } from './contracts';
const client = new ApiClient(API_V1_BASE);
export { DEMO } from '../constants/api';
export const serviceDatasetUrl = (filename: string) =>
  client.url(`/service-dataset-2025/${encodeURIComponent(filename)}`);
export async function request<T>(
  path: string,
  schema: z.ZodType<T>,
  signal?: AbortSignal,
  body?: unknown,
  headers?: Record<string, string>,
): Promise<T> {
  return client.request(path, schema, { signal, body, headers });
}

export async function getCapabilities(signal?: AbortSignal) {
  return DEMO
    ? (await import('../data/demo')).capabilities
    : request('/capabilities', CapabilitiesSchema, signal);
}
export async function getNetwork(snapshotId: string, signal?: AbortSignal, date?: string) {
  return DEMO
    ? (await import('../data/demo')).network
    : request(
        `/network?snapshotId=${encodeURIComponent(snapshotId)}${date ? `&date=${date}` : ''}`,
        NetworkSchema,
        signal,
      );
}
export async function getGeometry(scope: Scope, signal?: AbortSignal) {
  return DEMO
    ? (await import('../data/demo')).makeDemo(scope).geometry
    : request('/geometry', GeometrySchema, signal, scope);
}

const ImportPreviewSchema = z.object({
  id: z.string(),
  route: z.object({ id: z.string(), name: z.string(), hasData: z.boolean() }),
  validFrom: z.string(),
  validTo: z.string(),
  directions: z.array(z.number()),
  stopCount: z.number(),
  pointCount: z.number(),
  replacesExisting: z.boolean(),
  overlappingImports: z.array(z.string()),
  warning: z.string(),
});
export type ImportPreview = z.infer<typeof ImportPreviewSchema>;
export const previewRoute = (
  snapshotId: string,
  filename: string,
  csv: string,
  signal?: AbortSignal,
) => request('/route-imports/preview', ImportPreviewSchema, signal, { snapshotId, filename, csv });
export const applyRoute = (id: string, snapshotId: string, signal?: AbortSignal) =>
  request(
    `/route-imports/${id}/apply`,
    z.object({
      snapshotId: z.string(),
      networkSnapshotId: z.string(),
      routeId: z.string(),
      validFrom: z.string(),
    }),
    signal,
    { snapshotId },
  );
export async function getView(scope: Scope, signal?: AbortSignal): Promise<ViewData> {
  if (DEMO) {
    signal?.throwIfAborted();
    return (await import('../data/demo')).makeDemo(scope);
  }
  const [snapshot, series, comparison, heatmap] = await Promise.all([
    request('/map-snapshot', SnapshotSchema, signal, scope),
    request('/timeseries', SeriesSchema, signal, scope),
    request('/route-comparison', ComparisonSchema, signal, scope),
    request('/heatmap', HeatmapSchema, signal, scope),
  ]);
  if (
    [snapshot, series, comparison, heatmap].some(
      (x) =>
        x.meta.snapshotId !== scope.snapshotId ||
        x.meta.networkSnapshotId !== snapshot.meta.networkSnapshotId ||
        x.meta.forecastId !== snapshot.meta.forecastId ||
        x.meta.fleetId !== snapshot.meta.fleetId ||
        x.meta.metric !== scope.metric ||
        x.meta.grain !== scope.grain ||
        Date.parse(x.meta.timeRange.start) !== Date.parse(scope.timeRange.start) ||
        Date.parse(x.meta.timeRange.end) !== Date.parse(scope.timeRange.end),
    )
  )
    throw new Error('Версии или интервалы данных не совпали');
  const ids = scope.routeIds.slice().sort().join(','),
    count = snapshot.frames.length;
  if (
    series.series
      .map((s) => s.routeId)
      .sort()
      .join(',') !== ids ||
    comparison.routes
      .map((r) => r.routeId)
      .sort()
      .join(',') !== ids ||
    heatmap.routeIds.slice().sort().join(',') !== ids ||
    series.series.some((s) => s.points.length !== count || s.baseline.length !== count) ||
    snapshot.frames.some(
      (f) =>
        f.values
          .map((v) => v.routeId)
          .sort()
          .join(',') !== ids,
    ) ||
    heatmap.cells.length !== count * scope.routeIds.length
  )
    throw new Error('Неполный маршрутный ряд');
  if (snapshot.geometry.networkSnapshotId !== snapshot.meta.networkSnapshotId)
    throw new Error('Версии геометрии не совпали');
  return { ...snapshot, series: series.series, comparison: comparison.routes, heatmap };
}
export function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob),
    a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_REVOKE_MS);
}
export function createDemoCsv(frames: Frame[], scope: Scope) {
  return (
    '\ufeffroute;value;interval_start;interval_end;snapshot_id\r\n' +
    frames
      .flatMap((f) =>
        f.values.map((v) => [v.routeId, v.value ?? '', f.start, f.end, scope.snapshotId].join(';')),
      )
      .join('\r\n')
  );
}
async function pause(signal: AbortSignal) {
  signal.throwIfAborted();
  await new Promise<void>((resolve, reject) => {
    const cancel = () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', cancel);
      resolve();
    }, EXPORT_POLL_MS);
    signal.addEventListener('abort', cancel, { once: true });
  });
}
export async function exportData(
  scope: Scope,
  index: number | null,
  submission: boolean,
  signal: AbortSignal,
) {
  if (DEMO) {
    const data = (await import('../data/demo')).makeDemo(scope);
    download(
      new Blob([createDemoCsv(index === null ? data.frames : [data.frames[index]], scope)], {
        type: 'text/csv',
      }),
      'demo-validations.csv',
    );
    return;
  }
  const body = submission ? { snapshotId: scope.snapshotId } : { scope, index, format: 'csv' };
  let job = await request(submission ? '/submissions' : '/exports', JobSchema, signal, body, {
    'Idempotency-Key': crypto.randomUUID(),
  });
  const deadline = Date.now() + EXPORT_TIMEOUT_MS;
  while (job.status !== 'ready') {
    if (job.status === 'failed' || Date.now() > deadline)
      throw new Error(job.error || 'Экспорт не завершён за 5 минут');
    await pause(signal);
    job = await request(`/jobs/${job.id}`, JobSchema, signal);
  }
  if (!job.downloadUrl) throw new Error('Ссылка на файл отсутствует');
  const url = new URL(job.downloadUrl, new URL(`${API_V1_BASE}/`, location.origin));
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error('Не удалось скачать файл');
  download(await response.blob(), submission ? 'submission.csv' : 'validations.csv');
}

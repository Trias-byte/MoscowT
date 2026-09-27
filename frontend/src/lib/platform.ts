import { z } from 'zod';
import type { Scope } from './contracts';
import type { SectionLoad } from './sectionLoad';

const BASE = (import.meta.env.VITE_API_URL || '/api/v1').replace(/\/api\/v1\/?$/, '/api/v2');
export async function platform<T>(
  path: string,
  schema: z.ZodType<T>,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    signal,
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID() },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const value = await response.json();
  if (!response.ok)
    throw new Error(
      value.error?.message || JSON.stringify(value.detail) || `HTTP ${response.status}`,
    );
  return schema.parse(value);
}
export const Loose = z.object({}).catchall(z.unknown());
export const Dataset = z
  .object({
    id: z.string(),
    start: z.string(),
    end: z.string(),
    routes: z.array(z.string()),
    rows: z.number(),
    known_rows: z.number(),
    total: z.number(),
    name: z.string().optional(),
    revision: z.number().optional(),
    created_at: z.string().optional(),
  })
  .passthrough();
export const Model = z
  .object({
    id: z.string(),
    recipe: z.string(),
    spec: z.object({
      dataset_id: z.string(),
      model_type: z.string(),
      route_ids: z.array(z.string()),
      time_range: z.object({ start: z.string(), end: z.string() }),
      weather_hourly_id: z.string().nullable().optional(),
      accident_links_id: z.string().nullable().optional(),
      feature_groups: z.array(z.string()).optional(),
    }),
  })
  .passthrough();
export const Forecast = z
  .object({
    id: z.string(),
    rows: z.number(),
    quality_note: z.string().optional(),
    spec: z.object({
      dataset_id: z.string(),
      model_id: z.string(),
      origin: z.string(),
      route_ids: z.array(z.string()),
      time_range: z.object({ start: z.string(), end: z.string() }),
    }),
  })
  .passthrough();
export const Job = z
  .object({
    id: z.string(),
    kind: z.string(),
    status: z.enum(['pending', 'running', 'ready', 'failed', 'cancelled']),
    progress: z.number(),
    error: z.string().nullable(),
    result: Loose.nullable(),
  })
  .passthrough();
export const Schedule = z
  .object({
    id: z.string(),
    method: z.string(),
    valid_from: z.string(),
    valid_to: z.string(),
    routes: z.array(z.string()),
  })
  .passthrough();
export async function upload(file: File, kind: 'data' | 'model' = 'data') {
  const response = await fetch(`${BASE}/blobs?kind=${kind}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: file,
  });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || 'Не удалось загрузить файл');
  return z.object({ id: z.string() }).parse(value).id;
}
export const downloadUrl = (id: string) => `${BASE}/jobs/${id}/download`;
const Section = z.object({
  segmentId: z.string(),
  routeId: z.string(),
  patternId: z.string(),
  fromName: z.string(),
  toName: z.string(),
  directionName: z.string(),
  rate: z.number().nullable(),
  estimatedFlow: z.number().nullable(),
  vehicleHours: z.number().nullable(),
  meanVehicles: z.number().nullable(),
  hours: z.number(),
  source: z.enum(['scenario', 'missing']),
  modelVersion: z.literal('ordered-stop-scenario-v2'),
});
export async function getSections(
  scope: Scope,
  signal?: AbortSignal,
): Promise<Record<string, SectionLoad>> {
  return (
    await platform(
      '/sections',
      z.object({ sections: z.record(z.string(), Section) }),
      scope,
      signal,
    )
  ).sections;
}

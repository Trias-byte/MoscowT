import { API_V2_BASE } from '../constants/api';
import { ApiClient } from '../services/ApiClient';
import { z } from 'zod';
import type { Scope } from './contracts';
import type { SectionLoad } from './sectionLoad';

const client = new ApiClient(API_V2_BASE, true);
export function platform<T>(
  path: string,
  schema: z.ZodType<T>,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  return client.request(path, schema, { body, signal });
}
export { platformErrorMessage } from '../services/ApiClient';
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
    first_observation: z.string().nullable().optional(),
    last_observation: z.string().nullable().optional(),
    forecast_origin: z.string().nullable().optional(),
    route_coverage: z
      .record(
        z.string(),
        z.object({
          known_hours: z.number(),
          missing_hours: z.number(),
          last_observation: z.string().nullable(),
        }),
      )
      .optional(),
  })
  .passthrough();
export const Model = z
  .object({
    id: z.string(),
    recipe: z.string(),
    label: z.string().optional(),
    competition_result: z
      .object({
        score: z.number(),
        source: z.string(),
        submission_sha256: z.string(),
      })
      .passthrough()
      .optional(),
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
    phase: z.string().optional(),
    update: z
      .object({
        dataset_id: z.string().optional(),
        model_id: z.string().optional(),
        forecast_id: z.string().optional(),
        evaluation_id: z.string().optional(),
        excluded_routes: z.record(z.string(), z.string()).optional(),
      })
      .optional(),
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
  const value = await client.upload(`/blobs?kind=${kind}`, file, z.object({ id: z.string() }));
  return value.id;
}
export const downloadUrl = (id: string) => client.url(`/jobs/${encodeURIComponent(id)}/download`);
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

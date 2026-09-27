export { CATEGORIES, POI_KINDS } from '../constants/passengers';
import { z } from 'zod';
import type { Scope } from './contracts';
import { platform } from './platform';

export const ResearchRow = z.record(
  z.string(),
  z.union([z.string(), z.number(), z.boolean(), z.null()]),
);
export type ResearchRow = z.infer<typeof ResearchRow>;
export const PassengerMeta = z.object({
  dataset_id: z.string(),
  coverage: z.object({ start: z.string(), end: z.string() }),
  timezone: z.literal('Europe/Moscow'),
  filters: z.record(z.string(), z.unknown()),
  status: z.enum(['ready', 'unavailable', 'incompatible']),
  reason: z.string().nullable(),
});
export type PassengerMeta = z.infer<typeof PassengerMeta>;
const Experiment = z
  .object({
    model: z.string(),
    mean_wape: z.number(),
    fold_wins: z.number(),
    meets_acceptance: z.boolean(),
  })
  .passthrough();
export const PassengerMetadata = z.discriminatedUnion('available', [
  z.object({ available: z.literal(false), status: z.string(), reason: z.string() }),
  z.object({
    available: z.literal(true),
    status: z.literal('ready'),
    id: z.string(),
    coverage: z.object({ start: z.string(), end: z.string() }),
    timezone: z.string(),
    taxonomy_version: z.string(),
    categories: z.record(z.string(), z.string()),
    routes: z.array(z.string()),
    snapshots: z.array(z.string()),
    total_boardings: z.number(),
    unique_cards: z.number(),
    source_checks: z.number(),
    notes: z.array(z.string()),
    attribution: z.string(),
    experiments: z.array(Experiment),
    experiment_metrics: z.array(ResearchRow),
    experiment_intervals: z.array(ResearchRow),
    sources: z.array(z.record(z.string(), z.unknown())),
    fares: z.array(ResearchRow),
  }),
]);
const CategorySummary = z.object({
  category: z.string(),
  boardings: z.number().int().nonnegative(),
  share: z.number().nullable(),
  unique_cards: z.number().int().nonnegative().nullable(),
});
export const PassengerResult = z.object({
  meta: PassengerMeta,
  rows: z.array(
    CategorySummary.extend({
      route: z.string(),
      timestamp: z.string(),
      all_boardings: z.number().int().nonnegative(),
    }),
  ),
  summary: z.array(CategorySummary),
  total: z.number().int().nonnegative().nullable(),
});
export const Seasonality = z.object({
  meta: PassengerMeta,
  monthly: z.array(ResearchRow),
  hourly: z.array(ResearchRow),
  weekdays: z.array(ResearchRow),
  comparisons: z.array(ResearchRow),
});
export const Cohorts = z.object({
  meta: PassengerMeta,
  monthly: z.array(ResearchRow),
  autumn: z.array(ResearchRow),
  autumn_categories: z.array(ResearchRow),
  category_changes: z.array(ResearchRow),
});
export const Infrastructure = z.object({
  meta: PassengerMeta,
  rows: z.array(z.object({ kind: z.string(), value: z.number().nullable() })),
  attribution: z.string().optional(),
  source: ResearchRow.optional(),
});
export const PoiFeature = z.object({
  type: z.literal('Feature'),
  id: z.string(),
  geometry: z.object({ type: z.literal('Point'), coordinates: z.tuple([z.number(), z.number()]) }),
  properties: z
    .object({
      object_id: z.string(),
      name: z.string(),
      category: z.string(),
      address: z.string(),
      source_url: z.string(),
      snapshot: z.string(),
      distance_m: z.number(),
      route_ids: z.array(z.string()),
    })
    .passthrough(),
});
export type PoiFeature = z.infer<typeof PoiFeature>;
export const PoiCollection = z.object({
  meta: PassengerMeta,
  type: z.literal('FeatureCollection'),
  features: z.array(PoiFeature),
  attribution: z.string().optional(),
});
export type PoiCollection = z.infer<typeof PoiCollection>;
export function parameters(values: Record<string, unknown>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined)
      for (const item of Array.isArray(value) ? value : [value]) params.append(key, String(item));
  }
  return params.toString();
}
export const passengerMetadata = (signal?: AbortSignal) =>
  platform('/passengers/metadata', PassengerMetadata, undefined, signal);
export function passengerQuery(scope: Scope, categories: string[], signal?: AbortSignal) {
  return platform(
    '/passengers/query',
    PassengerResult,
    {
      snapshot_id: scope.snapshotId,
      route_ids: scope.routeIds,
      time_range: scope.timeRange,
      grain: scope.grain,
      mode: scope.mode,
      categories,
    },
    signal,
  );
}
export function passengerCsv(rows: ResearchRow[], meta: PassengerMeta) {
  const columns = [...new Set(rows.flatMap((r) => Object.keys(r)))];
  const quote = (v: unknown) => {
    let text = v == null ? '' : String(v);
    if (typeof v === 'string' && /^[=+\-@\t\r]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  const prefix = [
    meta.dataset_id,
    meta.coverage.start,
    meta.coverage.end,
    JSON.stringify(meta.filters),
  ];
  return (
    '\uFEFF' +
    [
      ['dataset_id', 'coverage_start', 'coverage_end', 'filters', ...columns],
      ...rows.map((row) => [...prefix, ...columns.map((c) => row[c])]),
    ]
      .map((line) => line.map(quote).join(';'))
      .join('\r\n')
  );
}
export function downloadPassengerCsv(name: string, rows: ResearchRow[], meta: PassengerMeta) {
  const url = URL.createObjectURL(
    new Blob([passengerCsv(rows, meta)], { type: 'text/csv;charset=utf-8' }),
  );
  const a = document.createElement('a');
  a.href = url;
  a.download = `passengers-${name}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

import { z } from 'zod';
const nullable = z.number().finite().nonnegative().nullable();
export const PointSchema = z.tuple([z.number(), z.number()]);
export const RouteSchema = z.object({
  id: z.string(),
  number: z.string(),
  name: z.string(),
  isTarget: z.boolean(),
  hasData: z.boolean(),
  hasGeometry: z.boolean(),
  historySupport: z.string(),
  sourceKind: z.string().optional(),
  geometryNote: z.string().optional(),
  sourceUrl: z.string().optional(),
  officialUrl: z.string().nullable().optional(),
});
export const PatternSchema = z.object({
  id: z.string(),
  routeId: z.string(),
  direction: z.number(),
  name: z.string(),
  validFrom: z.string(),
  validTo: z.string().nullable(),
  observedAt: z.string(),
  sourceKind: z.string().optional(),
  sourceUrl: z.string().optional(),
});
export const StopSchema = z.object({
  id: z.string(),
  stationId: z.string(),
  name: z.string(),
  routeId: z.string(),
  patternId: z.string(),
  direction: z.number(),
  order: z.number(),
  coordinates: PointSchema,
  sourceUrl: z.string().optional(),
});
export const SegmentSchema = z.object({
  id: z.string(),
  routeId: z.string(),
  patternId: z.string(),
  fromId: z.string(),
  toId: z.string(),
  order: z.number(),
  coordinates: z.array(PointSchema),
});
export const NetworkSchema = z.object({
  networkSnapshotId: z.string(),
  geometryQuality: z.enum(['schematic', 'mapped']),
  sourceUrl: z.string().optional(),
  officialMapUrl: z.string().optional(),
  asOf: z.string(),
  sourceAsOf: z.string().optional(),
  historyRange: z.object({ start: z.string(), end: z.string() }).optional(),
  warning: z.string(),
  routes: z.array(RouteSchema),
  patterns: z.array(PatternSchema),
  stops: z.array(StopSchema),
  segments: z.array(SegmentSchema),
  missingGeometryRouteIds: z.array(z.string()),
});
const RangeSchema = z.object({ start: z.string(), end: z.string() });
const ProvenanceSchema = z.enum(['forecast', 'observation', 'mixed', 'missing']);
export const CapabilitiesSchema = z.object({
  contractVersion: z.literal(2),
  snapshotId: z.string(),
  networkSnapshotId: z.string().nullable(),
  metrics: z.array(z.literal('successful_validations')),
  modes: z.array(z.enum(['auto', 'history', 'forecast'])),
  horizons: z.array(z.enum(['day', 'month', 'competition_61d'])),
  stream: z.literal(false),
  historyRange: RangeSchema.nullable(),
  forecastRange: RangeSchema.nullable(),
  forecastId: z.string().nullable(),
  defaultDate: z.string(),
  targetRouteIds: z.array(z.string()),
  absoluteThresholds: z.array(z.number()),
  fleetId: z.string().nullable().optional(),
  vehicleLoadThresholds: z.array(z.number()).optional(),
  submissionAvailable: z.boolean(),
});
export const MetaSchema = z.object({
  contractVersion: z.literal(2),
  snapshotId: z.string(),
  networkSnapshotId: z.string().nullable(),
  historyId: z.string(),
  fleetId: z.string().nullable().optional(),
  fleetMethod: z.string().nullable().optional(),
  metric: z.literal('successful_validations'),
  metricScope: z.literal('route'),
  unit: z.literal('validations'),
  intervalUnit: z.enum(['validations/hour', 'validations/day']),
  aggregation: z.literal('sum'),
  grain: z.enum(['hour', 'day']),
  timeRange: RangeSchema,
  provenance: ProvenanceSchema,
  forecastId: z.string().nullable(),
  modelId: z.string().nullable(),
  issuedAt: z.string().nullable(),
  createdAt: z.string().nullable(),
  geometryFilterAffectsMetric: z.literal(false),
  baselineDescription: z.string(),
  coverageNote: z.string(),
});
export const ValueSchema = z.object({
  routeId: z.string(),
  provenance: ProvenanceSchema,
  value: nullable,
  baseline: nullable,
  baselineCount: nullable,
  valueOrigins: z.array(z.string()),
  qualityFlags: z.array(z.string()),
  coverageStatus: z.string(),
  historySupport: z.string(),
  fleetVehicles: nullable.optional(),
  vehicleHours: nullable.optional(),
  loadPerVehicleHour: nullable.optional(),
  fleetSource: z.enum(['observed', 'estimated', 'mixed', 'missing']).optional(),
  fleetSampleDays: z.number().int().nonnegative().optional(),
  fleetCoverage: z.number().min(0).max(1).nullable().optional(),
});
export const FrameSchema = z.object({
  start: z.string(),
  end: z.string(),
  values: z.array(ValueSchema),
  aggregate: nullable,
  baseline: nullable,
});
export const GeometrySchema = z.object({
  networkSnapshotId: z.string(),
  patternIds: z.array(z.string()),
  stopIds: z.array(z.string()),
  segmentIds: z.array(z.string()),
  geometryQuality: z.enum(['schematic', 'mapped']),
  asOf: z.string(),
  referenceMode: z.enum(['reference', 'historical']),
  historicallyUnavailablePatternIds: z.array(z.string()),
  missingGeometryRouteIds: z.array(z.string()),
  geometryFilterAffectsMetric: z.literal(false),
  warning: z.string(),
});
export const SnapshotSchema = z.object({
  meta: MetaSchema,
  frames: z.array(FrameSchema),
  geometry: GeometrySchema,
});
export const SeriesSchema = z.object({
  meta: MetaSchema,
  series: z.array(
    z.object({ routeId: z.string(), points: z.array(nullable), baseline: z.array(nullable) }),
  ),
});
export const ComparisonSchema = z.object({
  meta: MetaSchema,
  routes: z.array(
    z.object({
      routeId: z.string(),
      value: nullable,
      baseline: nullable,
      difference: z.number().finite().nullable(),
    }),
  ),
});
export const HeatmapSchema = z.object({
  meta: MetaSchema,
  routeIds: z.array(z.string()),
  cells: z.array(
    z.tuple([z.number().int().nonnegative(), z.number().int().nonnegative(), nullable]),
  ),
});
export const JobSchema = z.object({
  id: z.string(),
  status: z.enum(['pending', 'running', 'ready', 'failed']),
  downloadUrl: z.string().nullable(),
  error: z.string().nullable(),
});
export type Network = z.infer<typeof NetworkSchema>;
export type Route = z.infer<typeof RouteSchema>;
export type Frame = z.infer<typeof FrameSchema>;
export type GeometrySelection = z.infer<typeof GeometrySchema>;
export type Capabilities = z.infer<typeof CapabilitiesSchema>;
export type WindowHours = 1 | 12 | 24;
export type Mode = 'auto' | 'history' | 'forecast';
export type Provenance = z.infer<typeof ProvenanceSchema>;
export interface GeometryScope {
  date?: string;
  patternIds: string[];
  section: { patternId: string; fromId: string; toId: string } | null;
  bbox: [number, number, number, number] | null;
  referenceMode: 'reference' | 'historical';
}
export interface Scope {
  routeIds: string[];
  metric: 'successful_validations';
  metricScope: 'route';
  mode: Mode;
  timeRange: { start: string; end: string };
  grain: 'hour' | 'day';
  aggregation: 'sum';
  snapshotId: string;
  geometry: GeometryScope;
}
export interface ViewState {
  contractVersion: 2;
  routeIds: string[];
  mode: 'auto';
  windowHours: WindowHours;
  date: string;
  snapshotId: string;
  index: number;
  geometry: GeometryScope;
}
export type ObjectSelection =
  { kind: 'route' | 'stop'; id: string } | { kind: 'segment'; id: string; routeId: string };
export interface ViewData extends z.infer<typeof SnapshotSchema> {
  series: z.infer<typeof SeriesSchema>['series'];
  comparison: z.infer<typeof ComparisonSchema>['routes'];
  heatmap: z.infer<typeof HeatmapSchema>;
}

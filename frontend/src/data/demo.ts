import { network } from './network';
import type { Capabilities, Scope, ViewData } from '../lib/contracts';
import { HOURLY_LOAD_THRESHOLDS, VEHICLE_LOAD_THRESHOLDS } from '../lib/domain';
export { network };
export const capabilities: Capabilities = {
  contractVersion: 2,
  snapshotId: 'snapshot-demo-v2',
  networkSnapshotId: network.networkSnapshotId,
  metrics: ['successful_validations'],
  modes: ['auto', 'history', 'forecast'],
  horizons: ['day', 'month', 'competition_61d'],
  stream: false,
  historyRange: { start: '2025-01-01T00:00:00+03:00', end: '2025-11-01T00:00:00+03:00' },
  forecastRange: { start: '2025-11-01T00:00:00+03:00', end: '2026-01-01T00:00:00+03:00' },
  forecastId: 'demo-forecast',
  defaultDate: '2025-11-01',
  targetRouteIds: ['1', '5', '7', '11', '12', '17', '25', '26', '28', '50'],
  absoluteThresholds: HOURLY_LOAD_THRESHOLDS,
  vehicleLoadThresholds: VEHICLE_LOAD_THRESHOLDS,
  fleetId: 'demo-fleet',
  submissionAvailable: false,
};
export function makeDemo(scope: Scope): ViewData {
  const step = scope.grain === 'hour' ? 3600000 : 86400000,
    count = Math.round(
      (Date.parse(scope.timeRange.end) - Date.parse(scope.timeRange.start)) / step,
    );
  const frames = Array.from({ length: count }, (_, i) => {
    const start = Date.parse(scope.timeRange.start) + i * step;
    const observed =
      scope.mode === 'history' ||
      (scope.mode === 'auto' &&
        start >= Date.parse(capabilities.historyRange!.start) &&
        start < Date.parse(capabilities.historyRange!.end));
    const values = scope.routeIds.map((routeId) => {
      const value =
        routeId === '5'
          ? 0
          : Math.max(
              0,
              Math.round(
                (600 + Number(routeId) * 17) *
                  (1 + Math.sin(i / 3)) *
                  (scope.grain === 'day' ? 24 : 1),
              ),
            );
      const vehicleHours =
        routeId === '5' || (routeId === '28' && i === 3) ? null : (12 * step) / 3600000;
      return {
        routeId,
        provenance:
          routeId === '28' && i === 3
            ? ('missing' as const)
            : observed
              ? ('observation' as const)
              : ('forecast' as const),
        value: routeId === '28' && i === 3 ? null : value,
        baseline: value * 0.9,
        baselineCount: 8,
        valueOrigins: ['synthetic_demo'],
        qualityFlags: [],
        coverageStatus: 'synthetic_demo',
        historySupport: routeId === '5' ? 'no_positive_history' : 'available',
        fleetVehicles: vehicleHours === null ? null : 12,
        vehicleHours,
        loadPerVehicleHour: vehicleHours === null ? null : value / vehicleHours,
        fleetSource: vehicleHours === null ? ('missing' as const) : ('estimated' as const),
        fleetSampleDays: 0,
        fleetCoverage: null,
      };
    });
    return {
      start: new Date(Date.parse(scope.timeRange.start) + i * step).toISOString(),
      end: new Date(Date.parse(scope.timeRange.start) + (i + 1) * step).toISOString(),
      values,
      aggregate: values.some((v) => v.value === null)
        ? null
        : values.reduce((sum, v) => sum + (v.value || 0), 0),
      baseline: values.reduce((sum, v) => sum + v.baseline, 0),
    };
  });
  const hasHistory = frames.some((f) => f.values.some((v) => v.provenance === 'observation'));
  const hasForecast = frames.some((f) => f.values.some((v) => v.provenance === 'forecast'));
  const meta: ViewData['meta'] = {
    contractVersion: 2,
    snapshotId: scope.snapshotId,
    networkSnapshotId: network.networkSnapshotId,
    historyId: 'demo-history',
    fleetId: 'demo-fleet',
    metric: 'successful_validations',
    metricScope: 'route',
    unit: 'validations',
    intervalUnit: scope.grain === 'hour' ? 'validations/hour' : 'validations/day',
    aggregation: 'sum',
    grain: scope.grain,
    timeRange: scope.timeRange,
    provenance: hasHistory && hasForecast ? 'mixed' : hasHistory ? 'observation' : 'forecast',
    forecastId: hasForecast ? 'demo-forecast' : null,
    modelId: hasForecast ? 'demo-model' : null,
    issuedAt: hasForecast ? '2025-11-01T00:00:00+03:00' : null,
    createdAt: null,
    geometryFilterAffectsMetric: false,
    baselineDescription: 'Синтетическое среднее для демонстрации',
    coverageNote: 'Демонстрационные числа',
  };
  let patterns = network.patterns.filter(
    (p) =>
      scope.routeIds.includes(p.routeId) &&
      (!scope.geometry.patternIds.length || scope.geometry.patternIds.includes(p.id)),
  );
  const hidden =
    scope.geometry.referenceMode === 'historical'
      ? patterns.filter((p) => scope.timeRange.start.slice(0, 10) < p.observedAt).map((p) => p.id)
      : [];
  patterns = patterns.filter((p) => !hidden.includes(p.id));
  const section = scope.geometry.section;
  if (section) patterns = patterns.filter((p) => p.id === section.patternId);
  const ids = patterns.map((p) => p.id),
    first = network.stops.find((s) => s.id === section?.fromId)?.order ?? 0,
    last = network.stops.find((s) => s.id === section?.toId)?.order ?? Infinity;
  const series = scope.routeIds.map((routeId) => ({
    routeId,
    points: frames.map((f) => f.values.find((v) => v.routeId === routeId)!.value),
    baseline: frames.map((f) => f.values.find((v) => v.routeId === routeId)!.baseline),
  }));
  return {
    meta,
    frames,
    series,
    comparison: series.map((s) => {
      const value = s.points.some((v) => v === null)
          ? null
          : s.points.reduce<number>((a, v) => a + (v ?? 0), 0),
        baseline = s.baseline.reduce((a, v) => a + v, 0);
      return {
        routeId: s.routeId,
        value,
        baseline,
        difference: value === null ? null : value - baseline,
      };
    }),
    geometry: {
      networkSnapshotId: network.networkSnapshotId,
      patternIds: ids,
      stopIds: network.stops
        .filter((s) => ids.includes(s.patternId) && s.order >= first && s.order <= last)
        .map((s) => s.id),
      segmentIds: network.segments
        .filter((s) => ids.includes(s.patternId) && s.order >= first && s.order < last)
        .map((s) => s.id),
      geometryQuality: network.geometryQuality,
      asOf: network.asOf,
      referenceMode: scope.geometry.referenceMode,
      historicallyUnavailablePatternIds: hidden,
      missingGeometryRouteIds: scope.routeIds.filter((r) =>
        network.missingGeometryRouteIds.includes(r),
      ),
      geometryFilterAffectsMetric: false,
      warning: network.warning,
    },
    heatmap: {
      meta,
      routeIds: scope.routeIds,
      cells: series.flatMap((s, r) =>
        s.points.map((v, i): [number, number, number | null] => [i, r, v]),
      ),
    },
  };
}

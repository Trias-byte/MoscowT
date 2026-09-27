import type { Frame, Network } from './contracts';

// A scenario model, not a stop-level observation or a fitted passenger model.
export const SECTION_MODEL_VERSION = 'ordered-stop-scenario-v1';
export const SECTION_MODEL_NOTE =
  'Модельная оценка по маршруту, остановкам и времени суток. Места валидаций и направление каждого вагона неизвестны.';
type Phase = 'morning' | 'evening' | 'neutral';
type Segment = Network['segments'][number];
interface PatternModel {
  id: string;
  routeId: string;
  name: string;
  segments: Segment[];
  stops: Network['stops'];
  distance: number[];
  attraction: number[];
  fleetShare: number;
}
interface Profile {
  shares: number[];
  tripShare: number;
}
export interface SectionModel {
  routes: Map<string, PatternModel[]>;
  profiles: Map<string, Map<string, Profile>>;
}
export interface SectionLoad {
  segmentId: string;
  routeId: string;
  patternId: string;
  fromName: string;
  toName: string;
  directionName: string;
  rate: number | null;
  estimatedFlow: number | null;
  vehicleHours: number | null;
  meanVehicles: number | null;
  hours: number;
  source: 'scenario' | 'missing';
  modelVersion: typeof SECTION_MODEL_VERSION | 'ordered-stop-scenario-v2';
}

function meters(a: number[], b: number[]) {
  const rad = Math.PI / 180;
  const lat = (b[1] - a[1]) * rad,
    lon = (b[0] - a[0]) * rad;
  const s =
    Math.sin(lat / 2) ** 2 + Math.cos(a[1] * rad) * Math.cos(b[1] * rad) * Math.sin(lon / 2) ** 2;
  return 12742000 * Math.asin(Math.min(1, Math.sqrt(s)));
}

export function buildSectionModel(network: Network): SectionModel {
  const routes: SectionModel['routes'] = new Map();
  const stopRoutes = new Map<string, Set<string>>();
  // The whole dated network, not the currently visible subset, defines transfers.
  for (const stop of network.stops) {
    const ids = stopRoutes.get(stop.stationId) ?? new Set<string>();
    ids.add(stop.routeId);
    stopRoutes.set(stop.stationId, ids);
  }
  for (const route of network.routes) {
    const patterns = network.patterns.filter((p) => p.routeId === route.id);
    // A missing direction must not receive the other direction's demand.
    if (patterns.length !== 2 || new Set(patterns.map((p) => p.direction)).size !== 2) continue;
    const compiled: PatternModel[] = [];
    for (const pattern of patterns) {
      const stops = network.stops
        .filter((s) => s.patternId === pattern.id)
        .sort((a, b) => a.order - b.order);
      const segments = network.segments
        .filter((s) => s.patternId === pattern.id)
        .sort((a, b) => a.order - b.order);
      if (
        stops.length < 2 ||
        segments.length !== stops.length - 1 ||
        segments.some((s, i) => s.fromId !== stops[i].id || s.toId !== stops[i + 1].id)
      )
        continue;
      const distance = [0];
      for (const segment of segments) {
        const length = segment.coordinates
          .slice(1)
          .reduce((sum, point, i) => sum + meters(segment.coordinates[i], point), 0);
        distance.push(distance[distance.length - 1] + length);
      }
      if (distance[distance.length - 1] <= 0) continue;
      const attraction = stops.map(
        (s) =>
          1 +
          (/метро|мцк|мцд|вокзал/i.test(s.name) ? 2 : 0) +
          Math.min(2, 0.4 * ((stopRoutes.get(s.stationId)?.size ?? 1) - 1)),
      );
      compiled.push({
        id: pattern.id,
        routeId: route.id,
        name: pattern.name,
        segments,
        stops,
        distance,
        attraction,
        fleetShare: 0,
      });
    }
    if (compiled.length !== 2) continue;
    const length = compiled.reduce((sum, p) => sum + p.distance[p.distance.length - 1], 0);
    compiled.forEach((p) => {
      p.fleetShare = p.distance[p.distance.length - 1] / length;
    });
    routes.set(route.id, compiled);
  }
  return { routes, profiles: new Map() };
}

export function timePhase(start: string): Phase {
  const moscow = new Date(Date.parse(start) + 3 * 3600000);
  const day = moscow.getUTCDay(),
    hour = moscow.getUTCHours();
  if (day === 0 || day === 6) return 'neutral';
  return hour >= 7 && hour < 11 ? 'morning' : hour >= 16 && hour < 20 ? 'evening' : 'neutral';
}

// Every validation is assigned once to an ordered origin/destination pair.
// A modeled trip contributes to each traversed section; sections cannot be summed as boardings.
export function sectionProfile(model: SectionModel, routeId: string, phase: Phase) {
  const key = `${routeId}:${phase}`;
  const cached = model.profiles.get(key);
  if (cached) return cached;
  const profiles = new Map<string, Profile>();
  let total = 0;
  for (const pattern of model.routes.get(routeId) ?? []) {
    const delta = pattern.stops.map(() => 0);
    let trips = 0;
    for (let from = 0; from < pattern.stops.length - 1; from++) {
      for (let to = from + 1; to < pattern.stops.length; to++) {
        const origin =
          phase === 'morning'
            ? 1
            : phase === 'evening'
              ? pattern.attraction[from]
              : Math.sqrt(pattern.attraction[from]);
        const destination =
          phase === 'evening'
            ? 1
            : phase === 'morning'
              ? pattern.attraction[to]
              : Math.sqrt(pattern.attraction[to]);
        // The 4 km distance decay and hub weights are explicit scenario assumptions.
        const weight =
          origin * destination * Math.exp(-(pattern.distance[to] - pattern.distance[from]) / 4000);
        trips += weight;
        delta[from] += weight;
        delta[to] -= weight;
      }
    }
    let flow = 0;
    const shares = delta.slice(0, -1).map((change) => {
      flow += change;
      return Math.max(0, flow);
    });
    profiles.set(pattern.id, { shares, tripShare: trips });
    total += trips;
  }
  if (total > 0)
    profiles.forEach((p) => {
      p.shares = p.shares.map((v) => v / total);
      p.tripShare /= total;
    });
  model.profiles.set(key, profiles);
  return profiles;
}

export function estimateSections(
  model: SectionModel,
  frames: Frame[],
): Record<string, SectionLoad> {
  const result: Record<string, SectionLoad> = {};
  for (const [routeId, patterns] of model.routes) {
    const rows = frames.map((f) => f.values.find((v) => v.routeId === routeId));
    const complete =
      frames.length > 0 &&
      frames.every((f) => Date.parse(f.end) - Date.parse(f.start) === 3600000) &&
      rows.every((r) => r?.value != null && r.vehicleHours != null);
    const profiles = frames.map((f) => sectionProfile(model, routeId, timePhase(f.start)));
    for (const pattern of patterns) {
      const vehicleHours = complete
        ? rows.reduce((sum, r) => sum + r!.vehicleHours!, 0) * pattern.fleetShare
        : null;
      pattern.segments.forEach((segment, i) => {
        const flow = complete
          ? rows.reduce((sum, r, h) => sum + r!.value! * profiles[h].get(pattern.id)!.shares[i], 0)
          : null;
        const rate =
          flow !== null && vehicleHours !== null && vehicleHours > 0 ? flow / vehicleHours : null;
        result[segment.id] = {
          segmentId: segment.id,
          routeId,
          patternId: pattern.id,
          fromName: pattern.stops[i].name,
          toName: pattern.stops[i + 1].name,
          directionName: pattern.name,
          rate,
          estimatedFlow: flow,
          vehicleHours,
          meanVehicles: vehicleHours === null ? null : vehicleHours / frames.length,
          hours: frames.length,
          source: rate === null ? 'missing' : 'scenario',
          modelVersion: SECTION_MODEL_VERSION,
        };
      });
    }
  }
  return result;
}

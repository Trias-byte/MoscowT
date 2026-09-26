import { expect, it } from 'vitest';
import type { Frame, Network } from '../../src/lib/contracts';
import {
  buildSectionModel,
  estimateSections,
  sectionProfile,
  timePhase,
} from '../../src/lib/sectionLoad';

function network(): Network {
  const n: Network = {
    networkSnapshotId: 'n',
    geometryQuality: 'mapped',
    asOf: '2025-10-31',
    warning: '',
    missingGeometryRouteIds: [],
    routes: [
      {
        id: '1',
        number: '1',
        name: 'Маршрут',
        isTarget: true,
        hasData: true,
        hasGeometry: true,
        historySupport: 'available',
      },
    ],
    patterns: [],
    stops: [],
    segments: [],
  };
  for (const direction of [0, 1]) {
    const id = `p${direction}`;
    n.patterns.push({
      id,
      routeId: '1',
      direction,
      name: direction ? 'Метро → Парк' : 'Парк → Метро',
      validFrom: '2025-01-01',
      validTo: null,
      observedAt: '2025-01-01',
    });
    for (let i = 0; i < 5; i++) {
      const station = direction ? 4 - i : i;
      n.stops.push({
        id: `${id}-s${i}`,
        stationId: `s${station}`,
        name: station === 4 ? 'Метро Центр' : `Остановка ${station}`,
        routeId: '1',
        patternId: id,
        direction,
        order: i,
        coordinates: [37.5 + station * 0.008, 55.7],
      });
    }
    const stops = n.stops.filter((s) => s.patternId === id);
    stops
      .slice(0, -1)
      .forEach((s, i) =>
        n.segments.push({
          id: `${id}-e${i}`,
          routeId: '1',
          patternId: id,
          fromId: s.id,
          toId: stops[i + 1].id,
          order: i,
          coordinates: [s.coordinates, stops[i + 1].coordinates],
        }),
      );
  }
  return n;
}
function frame(hour: number, value = 1000, vehicles = 10): Frame {
  const start = Date.parse('2025-10-31T00:00:00+03:00') + hour * 3600000;
  return {
    start: new Date(start).toISOString(),
    end: new Date(start + 3600000).toISOString(),
    aggregate: value,
    baseline: null,
    values: [
      {
        routeId: '1',
        provenance: 'observation',
        value,
        baseline: null,
        baselineCount: null,
        valueOrigins: ['label'],
        qualityFlags: [],
        coverageStatus: 'provided_extract',
        historySupport: 'available',
        vehicleHours: vehicles,
        fleetVehicles: vehicles,
        fleetSource: 'observed',
      },
    ],
  };
}
it('conserves route trip allocation and distinguishes direction by ordered stops, not its numeric code', () => {
  const n = network(),
    model = buildSectionModel(n);
  const morning = sectionProfile(model, '1', 'morning'),
    evening = sectionProfile(model, '1', 'evening');
  expect([...morning.values()].reduce((sum, p) => sum + p.tripShare, 0)).toBeCloseTo(1);
  expect(morning.get('p0')!.tripShare).toBeGreaterThan(morning.get('p1')!.tripShare);
  expect(evening.get('p0')!.tripShare).toBeLessThan(evening.get('p1')!.tripShare);
  n.patterns.forEach((p) => {
    p.direction = 1 - p.direction;
  });
  expect([...sectionProfile(buildSectionModel(n), '1', 'morning')]).toEqual([...morning]);
  const loads = estimateSections(model, [frame(8)]);
  const rates = Object.values(loads)
    .filter((s) => s.patternId === 'p0')
    .map((s) => s.rate!);
  expect(Math.max(...rates)).toBeGreaterThan(Math.min(...rates) * 1.2);
  expect(loads['p0-e0'].source).toBe('scenario');
  expect(loads['p0-e0'].fromName).toBe('Остановка 0');
  expect(loads['p1-e0'].fromName).toBe('Метро Центр');
});
it('combines hourly flows and vehicle-hours before division and preserves missing data', () => {
  const model = buildSectionModel(network());
  const first = frame(8, 100, 2),
    second = frame(9, 900, 10);
  const a = estimateSections(model, [first])['p0-e1'],
    b = estimateSections(model, [second])['p0-e1'];
  const sum = estimateSections(model, [first, second])['p0-e1'];
  expect(sum.rate).toBeCloseTo(
    (a.estimatedFlow! + b.estimatedFlow!) / (a.vehicleHours! + b.vehicleHours!),
  );
  expect(sum.meanVehicles).toBeCloseTo(sum.vehicleHours! / 2);
  second.values[0].vehicleHours = null;
  expect(estimateSections(model, [first, second])['p0-e1'].rate).toBeNull();
  expect(estimateSections(model, [frame(8, 0, 10)])['p0-e1'].rate).toBe(0);
  expect(estimateSections(model, [frame(8, 0, 0)])['p0-e1'].rate).toBeNull();
  second.values[0].vehicleHours = 10;
  second.values[0].value = null;
  expect(estimateSections(model, [first, second])['p0-e1'].estimatedFlow).toBeNull();
});
it('uses Moscow time, neutral weekends and refuses incomplete direction geometry', () => {
  expect(timePhase('2025-10-31T05:00:00Z')).toBe('morning');
  expect(timePhase('2025-10-31T14:00:00Z')).toBe('evening');
  expect(timePhase('2025-11-01T05:00:00Z')).toBe('neutral');
  const n = network();
  n.segments.pop();
  expect(estimateSections(buildSectionModel(n), [frame(8)])).toEqual({});
});

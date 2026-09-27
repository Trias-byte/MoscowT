import { describe, expect, it } from 'vitest';
import {
  emptyScenario,
  mergeEdits,
  scenarioInput,
  stable,
  visibleIncidents,
} from '../../src/lib/scenario';
import { makeScope } from '../../src/lib/time';
import type { ViewState } from '../../src/lib/contracts';

describe('independent scenario modes', () => {
  const draft = structuredClone(emptyScenario);
  draft.coefficients.season = 1.5;
  draft.weather.temperature_2m = -15;
  draft.schedule.headway_minutes = 5;
  draft.schedule.base_headway_minutes = 10;
  draft.additional_vehicle_hours = 10;
  draft.incidents = [
    {
      id: 'a',
      start: '2025-11-01T08:00:00+03:00',
      duration_minutes: 30,
      reduction: 1,
      route_ids: ['17'],
      longitude: 37.6,
      latitude: 55.7,
    },
  ];
  it('keeps only inputs from the requested factor and preserves the draft', () => {
    const before = stable(draft);
    expect(scenarioInput(draft, 'season').coefficients.season).toBe(1.5);
    expect(scenarioInput(draft, 'season').incidents).toEqual([]);
    expect(scenarioInput(draft, 'season').weather.temperature_2m).toBeNull();
    expect(scenarioInput(draft, 'weather').coefficients.season).toBe(1);
    expect(scenarioInput(draft, 'incidents').incidents).toEqual(draft.incidents);
    expect(scenarioInput(draft, 'incidents').schedule.headway_minutes).toBeNull();
    expect(scenarioInput(draft, 'schedule').additional_vehicle_hours).toBe(10);
    expect(stable(draft)).toBe(before);
  });
  it('does not mark a single-factor result stale when another factor changes', () => {
    const other = structuredClone(draft);
    other.coefficients.season = 0.5;
    expect(stable(scenarioInput(other, 'weather'))).toBe(stable(scenarioInput(draft, 'weather')));
    expect(stable(scenarioInput(other, 'combined'))).not.toBe(
      stable(scenarioInput(draft, 'combined')),
    );
  });
  it('preserves edits in another tab when saving separate fields', () => {
    const latest = structuredClone(draft),
      after = structuredClone(draft);
    latest.weather.precipitation = 5;
    after.weather.temperature_2m = 10;
    expect(mergeEdits(latest, draft, after).weather).toMatchObject({
      precipitation: 5,
      temperature_2m: 10,
    });
  });
  it('filters incident markers at exact boundaries', () => {
    expect(
      visibleIncidents(draft.incidents, {
        start: '2025-11-01T08:00:00+03:00',
        end: '2025-11-01T08:30:00+03:00',
      }),
    ).toHaveLength(1);
    expect(
      visibleIncidents(draft.incidents, {
        start: '2025-11-01T08:30:00+03:00',
        end: '2025-11-01T09:00:00+03:00',
      }),
    ).toHaveLength(0);
  });
  it('requests the exact result period in forecast mode', () => {
    const range = { start: '2025-11-01T08:00:00+03:00', end: '2025-11-02T10:00:00+03:00' };
    const view: ViewState = {
      contractVersion: 2,
      routeIds: ['17'],
      mode: 'auto',
      windowHours: 24,
      date: '2025-11-01',
      snapshotId: 'snapshot-a',
      scenarioId: 'scenario-a',
      scenarioRange: range,
      index: 0,
      geometry: { patternIds: [], section: null, bbox: null, referenceMode: 'reference' },
    };
    expect(makeScope(view).timeRange).toEqual(range);
    expect(makeScope(view).mode).toBe('forecast');
    expect(makeScope({ ...view, scenarioId: undefined }).timeRange).toEqual(range);
  });
});

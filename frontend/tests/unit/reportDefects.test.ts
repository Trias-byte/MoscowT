import { expect, it } from 'vitest';
import { factorNumber, factorRange, contextSelection } from '../../src/lib/factorContext';
import { hourViewAt, moscowDateTime } from '../../src/lib/time';
import { emptyScenario, scenarioInput, mergeEdits } from '../../src/lib/scenario';

it('D09 ignores a disabled unfinished incident in the combined mode', () => {
  const draft = structuredClone(emptyScenario);
  draft.incidents = [
    {
      id: 'unfinished',
      route_ids: [],
      longitude: 37.6,
      latitude: 55.7,
      start: draft.time_range.start,
      reduction: 1,
      duration_minutes: 0,
    },
  ];
  draft.enabled.incidents = false;
  expect(scenarioInput(draft, 'combined').incidents).toEqual([]);
  expect(draft.incidents).toHaveLength(1);
});
it('D10 merges later job metadata without restoring an older draft', () => {
  const before = {
    draft: structuredClone(emptyScenario),
    runs: { combined: { job: 'old', resultId: '' } },
  };
  const latest = structuredClone(before),
    metadata = structuredClone(before);
  latest.draft.weather.precipitation = 1.25;
  latest.draft.name = 'Новая редакция';
  metadata.runs.combined = { job: '', resultId: 'completed-old-inputs' };
  const merged = mergeEdits(latest, before, metadata);
  expect(merged.draft).toEqual(latest.draft);
  expect(merged.runs.combined.resultId).toBe('completed-old-inputs');
});
it('D11 adds incidents at the selected Moscow hour, independent of browser zone', () => {
  expect(moscowDateTime('2025-11-01T00:00:00Z')).toBe('2025-11-01T03:00:00+03:00');
});
it('D13 selects a timestamp across days and month boundaries, not its series index', () => {
  expect(hourViewAt('2025-11-30T22:00:00Z')).toEqual({
    date: '2025-12-01',
    index: 1,
    windowHours: 1,
  });
  expect(hourViewAt('2025-11-02T08:00:00+03:00')).toEqual({
    date: '2025-11-02',
    index: 8,
    windowHours: 1,
  });
});
it('D15 falls back explicitly when a selected object belongs to another route', () => {
  expect(contextSelection({ kind: 'stop', id: 'stop-1' }, '1', ['17'])).toBeNull();
  expect(
    contextSelection({ kind: 'segment', id: 'section-17', routeId: '17' }, '17', ['17']),
  ).toEqual({ kind: 'segment', id: 'section-17' });
});
it('D18 displays small differences and omits unknown ranges', () => {
  expect(factorRange(0.19, 0.21)).toBe('0,19…0,21');
  expect(factorRange(null, null)).toBe('');
  expect(factorRange(0.2, 0.2)).toBe('0,2');
  expect(factorNumber(null)).toBe('Нет данных');
  expect(factorNumber(NaN)).toBe('Нет данных');
});

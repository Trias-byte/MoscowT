import { expect, it, vi } from 'vitest';
import {
  valueColor,
  number,
  csvEscape,
  sumWindow,
  COLORS,
  VEHICLE_LOAD_THRESHOLDS,
} from '../../src/lib/domain';
import { makeScope, localDate, dateBounds, intervalLabel } from '../../src/lib/time';
import { capabilities } from '../../src/data/demo';
import { initialView } from '../../src/lib/state';
import type { Frame, ViewState } from '../../src/lib/contracts';
const view: ViewState = {
  contractVersion: 2,
  routeIds: ['1', '5', '17'],
  snapshotId: 'snapshot-demo-v2',
  mode: 'auto',
  windowHours: 24,
  date: '2025-11-01',
  index: 0,
  geometry: { patternIds: [], section: null, bbox: null, referenceMode: 'reference' },
};
function restore(saved: unknown) {
  vi.stubGlobal('location', { search: `?${new URLSearchParams({ view: JSON.stringify(saved) })}` });
  try {
    return initialView(capabilities);
  } finally {
    vi.unstubAllGlobals();
  }
}
it('defaults to the whole Moscow day', () => {
  expect(restore(null).windowHours).toBe(24);
  const s = makeScope(view);
  expect(s.timeRange).toEqual({
    start: '2025-10-31T21:00:00.000Z',
    end: '2025-11-01T21:00:00.000Z',
  });
  expect(localDate(s.timeRange.start)).toBe('2025-11-01');
  expect(intervalLabel(s.timeRange.start, s.timeRange.end, 'hour')).toContain('00:00–24:00');
});
it('calendar includes all of 2025 and never clips a day to the forecast period', () => {
  expect(dateBounds(capabilities)).toEqual({ min: '2025-01-01', max: '2025-12-31' });
  for (const date of ['2025-01-01', '2025-07-15', '2025-10-31', '2025-11-01', '2025-12-31']) {
    const s = makeScope({ ...view, date });
    expect(localDate(s.timeRange.start)).toBe(date);
    expect(Date.parse(s.timeRange.end) - Date.parse(s.timeRange.start)).toBe(86400000);
    expect(s.mode).toBe('auto');
  }
});
it.each(['day', 'month', 'competition_61d'])(
  'migrates old %s links without restoring the November lock or end date',
  (horizon) => {
    const restored = restore({
      ...view,
      windowHours: undefined,
      horizon,
      mode: 'forecast',
      date: '2025-01-01',
      endDate: '2025-11-03',
      index: 40,
    });
    expect(restored.mode).toBe('auto');
    expect(restored.date).toBe('2025-01-01');
    expect(restored).not.toHaveProperty('horizon');
    expect(restored).not.toHaveProperty('endDate');
    expect(restored.index).toBe(horizon === 'day' ? 23 : 0);
    expect(localDate(makeScope(restored).timeRange.start)).toBe('2025-01-01');
  },
);
it('restores and bounds time windows and rejects impossible dates', () => {
  expect(restore({ ...view, windowHours: 12, index: 8 })).toMatchObject({
    windowHours: 12,
    index: 8,
  });
  expect(restore({ ...view, windowHours: 12, index: 23 }).index).toBe(12);
  expect(restore({ ...view, windowHours: 24, index: 23 }).index).toBe(0);
  expect(restore({ ...view, date: '2025-02-30' }).date).toBe(capabilities.defaultDate);
});
function hourlyFrames(): Frame[] {
  const start = Date.parse('2025-01-01T00:00:00+03:00');
  return Array.from({ length: 24 }, (_, h) => ({
    start: new Date(start + h * 3600000).toISOString(),
    end: new Date(start + (h + 1) * 3600000).toISOString(),
    values: ['1', '5'].map((routeId) => ({
      routeId,
      provenance: 'observation',
      value: routeId === '5' ? 0 : h + 1,
      baseline: 10,
      baselineCount: 8 - (h % 3),
      valueOrigins: ['extract'],
      qualityFlags: [],
      coverageStatus: 'provided_extract',
      historySupport: 'available',
    })),
    aggregate: h + 1,
    baseline: 20,
  }));
}
it('sums exactly the selected 12 or 24 hours, including zeros, and uses minimum baseline support', () => {
  const frames = hourlyFrames();
  const half = sumWindow(frames, 8, 12)!;
  expect(half.aggregate).toBe(174);
  expect(half.values[1].value).toBe(0);
  expect(half.values[0].baseline).toBe(120);
  expect(half.values[0].baselineCount).toBe(6);
  expect(intervalLabel(half.start, half.end, 'hour')).toContain('08:00–20:00');
  expect(sumWindow(frames, 0, 24)?.aggregate).toBe(300);
  expect(sumWindow(frames, 23, 1)?.aggregate).toBe(24);
  expect(sumWindow(frames, 13, 12)).toBeUndefined();
});
it('window sums retain mixed sources and flags without turning missing values into zero', () => {
  const frames = hourlyFrames();
  frames[9].values[0].provenance = 'forecast';
  frames[9].values[0].valueOrigins = ['model'];
  frames[9].values[0].qualityFlags = ['check'];
  expect(sumWindow(frames, 8, 12)?.values[0]).toMatchObject({
    provenance: 'mixed',
    valueOrigins: ['extract', 'model'],
    qualityFlags: ['check'],
  });
  frames[8].values[0].value = null;
  frames[8].values[0].provenance = 'missing';
  const half = sumWindow(frames, 8, 12)!;
  expect(half.aggregate).toBeNull();
  expect(half.values[0]).toMatchObject({ value: null, coverageStatus: 'missing' });
  expect(half.values[1].value).toBe(0);
});
it('geometry does not change numeric routes', () => {
  const s = makeScope({
    ...view,
    geometry: { ...view.geometry, section: { patternId: 'p', fromId: 'a', toId: 'b' } },
  });
  expect(s.routeIds).toEqual(['1', '5', '17']);
  expect(s.metricScope).toBe('route');
});
it('missing differs from zero', () => {
  expect(number(null)).toBe('Нет данных');
  expect(number(0)).toBe('0');
  expect(valueColor(null)).not.toBe(valueColor(0));
  expect(valueColor(200)).not.toBe(valueColor(199));
});
it('escapes CSV text formulas', () => {
  expect(csvEscape('=SUM(A1)')).toBe('"\'=SUM(A1)"');
  expect(csvEscape('a"b')).toBe('"a""b"');
});
it('uses vehicle-hours as the denominator for a window and keeps the scale fixed', () => {
  const frames = hourlyFrames();
  frames.forEach((frame, i) => {
    Object.assign(frame.values[0], {
      vehicleHours: i % 2 ? 10 : 2,
      fleetSource: i % 2 ? 'estimated' : 'observed',
      fleetSampleDays: 8,
      fleetCoverage: 0.99,
    });
  });
  const half = sumWindow(frames, 8, 12)!.values[0];
  expect(half).toMatchObject({ vehicleHours: 72, fleetVehicles: 6, fleetSource: 'mixed' });
  expect(half.loadPerVehicleHour).toBe(174 / 72);
  expect(sumWindow(frames, 0, 24)!.values[0].loadPerVehicleHour).toBe(300 / 144);
  expect(valueColor(49.9, VEHICLE_LOAD_THRESHOLDS)).toBe(COLORS[3]);
  expect(valueColor(50, VEHICLE_LOAD_THRESHOLDS)).toBe(COLORS[4]);
  frames[9].values[0].vehicleHours = null;
  frames[9].values[0].fleetSource = 'missing';
  expect(sumWindow(frames, 8, 12)!.values[0]).toMatchObject({
    value: 174,
    vehicleHours: null,
    loadPerVehicleHour: null,
    fleetSource: 'missing',
  });
  frames.forEach((f) => {
    f.values[0].vehicleHours = 0;
  });
  expect(sumWindow(frames, 0, 24)!.values[0].loadPerVehicleHour).toBeNull();
});

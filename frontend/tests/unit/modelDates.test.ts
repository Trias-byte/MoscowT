import { expect, it } from 'vitest';
import { dateTimeInput, forecastPeriod, inputTimestamp } from '../../src/lib/modelDates';
it('keeps the actual last hour in Moscow and forecasts one calendar year', () => {
  const origin = dateTimeInput('2026-01-14T12:00:00Z');
  expect(origin).toBe('2026-01-14T15:00');
  expect(forecastPeriod(origin, 'year')).toEqual(['2026-01-14T15:00', '2027-01-14T15:00']);
  expect(inputTimestamp(origin)).toBe('2026-01-14T15:00:00+03:00');
});
it('clamps leap-day and month-end civil dates without UTC day shifts', () => {
  expect(forecastPeriod('2028-02-29T00:00', 'year')[1]).toBe('2029-02-28T00:00');
  expect(forecastPeriod('2028-01-31T15:00', 'month')[1]).toBe('2028-02-29T15:00');
  expect(forecastPeriod('2026-12-31T23:00', 'day')[1]).toBe('2027-01-01T23:00');
});
it('covers the complete competition period with a fixed 61-day origin', () => {
  expect(forecastPeriod('2025-11-01T00:00', '61days')).toEqual([
    '2025-11-01T00:00',
    '2026-01-01T00:00',
  ]);
});

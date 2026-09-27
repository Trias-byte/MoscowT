import { expect, it } from 'vitest';
import { exportPeriodError } from '../../src/lib/exportPeriod';
import { platformErrorMessage } from '../../src/lib/platform';

const day = { start: '2025-11-01T00:00:00+03:00', end: '2025-11-02T00:00:00+03:00' };
it('rejects the reported 23:59 boundary and accepts the complete last hour of a day', () => {
  expect(exportPeriodError({ ...day, start: '2025-11-01T23:59:00+03:00' }, 'hour')).toContain(
    'нулевыми минутами',
  );
  expect(exportPeriodError({ ...day, start: '2025-11-01T23:00:00+03:00' }, 'hour')).toBeNull();
  expect(exportPeriodError(day, 'day')).toBeNull();
});
it('validates empty and reversed periods before export', () => {
  expect(exportPeriodError({ ...day, start: 'T00:00:00+03:00' }, 'hour')).toContain('Укажите дату');
  expect(exportPeriodError({ start: day.end, end: day.start }, 'hour')).toContain('позже начала');
  expect(exportPeriodError({ start: day.start, end: day.start }, 'hour')).toContain('позже начала');
});
it('validates day and month boundaries in Moscow, including UTC input', () => {
  expect(
    exportPeriodError({ start: '2025-10-31T21:00:00Z', end: '2025-11-30T21:00:00Z' }, 'month'),
  ).toBeNull();
  expect(exportPeriodError({ ...day, start: '2025-11-01T23:00:00+03:00' }, 'day')).toContain(
    '00:00',
  );
  expect(exportPeriodError(day, 'month')).toContain('первое число');
});
it('renders validation errors as human-readable messages instead of raw JSON', () => {
  expect(
    platformErrorMessage(
      {
        detail: [
          {
            type: 'value_error',
            loc: ['body', 'time_range', 'start'],
            msg: 'Value error, Time must be aligned to a Moscow calendar hour',
            input: '2025-11-01T23:59:00+03:00',
            ctx: { error: {} },
          },
        ],
      },
      422,
    ),
  ).toBe('Данные почасовые. Выберите время с нулевыми минутами, например 23:00.');
  expect(platformErrorMessage({ error: { message: 'Прогноз не покрывает период' } }, 422)).toBe(
    'Прогноз не покрывает период',
  );
});

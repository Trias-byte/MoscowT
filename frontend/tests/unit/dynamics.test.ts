import { expect, it } from 'vitest';
import { dynamicsTooltip } from '../../src/lib/dynamics';
import { dateBounds } from '../../src/lib/time';
import { initialView } from '../../src/lib/state';
import { capabilities } from '../../src/data/demo';
import { vi } from 'vitest';

it('keeps unknown values distinct from zero and escapes uploaded route names', () => {
  const html = dynamicsTooltip(
    [{ routeId: '<img src=x>', points: [null, 0], baseline: [10, 12] }],
    0,
    '12:00',
    true,
  );
  expect(html).toContain('&lt;img src=x&gt;');
  expect(html).not.toContain('<img');
  expect(html).toContain('Нет данных');
  expect(
    dynamicsTooltip([{ routeId: '1', points: [0], baseline: [2] }], 0, '12:00', false),
  ).toContain('<td>0</td>');
});

it('accepts and restores dates throughout the ready annual horizon', () => {
  const c = {
    ...capabilities,
    forecastOptions: [
      {
        id: 'annual',
        kind: 'annual_scenario' as const,
        start: '2025-11-01T00:00:00+03:00',
        end: '2026-11-01T00:00:00+03:00',
        origin: '2025-11-01T00:00:00+03:00',
        qualityNote: 'Сценарий',
      },
    ],
  };
  expect(dateBounds(c)).toEqual({ min: '2025-01-01', max: '2026-10-31' });
  vi.stubGlobal('location', {
    search:
      '?' +
      new URLSearchParams({
        view: JSON.stringify({
          contractVersion: 2,
          date: '2026-10-31',
          snapshotId: 'snapshot-annual',
          forecastId: 'annual',
        }),
      }),
  });
  try {
    expect(initialView(c)).toMatchObject({
      date: '2026-10-31',
      forecastId: 'annual',
      snapshotId: 'snapshot-annual',
    });
  } finally {
    vi.unstubAllGlobals();
  }
});

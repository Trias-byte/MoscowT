import { describe, expect, it } from 'vitest';
import {
  passengerCsv,
  PassengerResult,
  PoiCollection,
  type PassengerMeta,
} from '../../src/lib/passengers';

const meta: PassengerMeta = {
  dataset_id: 'passengers-test',
  coverage: { start: '2025-01-01', end: '2025-11-01' },
  timezone: 'Europe/Moscow',
  filters: { route_ids: ['1'], categories: ['social'] },
  status: 'ready',
  reason: null,
};
describe('passenger contracts and export', () => {
  it('keeps unavailable counts distinct from zero and does not invent unique-card counts', () => {
    const result = PassengerResult.parse({
      meta,
      total: 0,
      summary: [{ category: 'social', boardings: 0, share: null, unique_cards: null }],
      rows: [],
    });
    expect(result.summary[0].unique_cards).toBeNull();
    expect(result.total).toBe(0);
    expect(
      PassengerResult.parse({ ...result, meta: { ...meta, status: 'unavailable' }, total: null })
        .total,
    ).toBeNull();
  });
  it('exports the displayed values, release and exact filters with CSV quoting', () => {
    const csv = passengerCsv(
      [{ category: 'social', boardings: 12, unique_cards: null, name: '=1+1;"test"' }],
      meta,
    );
    expect(csv).toContain('passengers-test');
    expect(csv).toContain('""route_ids"":[');
    expect(csv).toContain('"12";"";"\'=1+1;""test"""');
  });
  it('requires dated POIs and WGS84 point coordinates', () => {
    expect(
      PoiCollection.safeParse({
        meta,
        type: 'FeatureCollection',
        features: [{ type: 'Feature', id: '1', geometry: { type: 'Polygon', coordinates: [] } }],
      }).success,
    ).toBe(false);
  });
});

import { writeFileSync, mkdirSync } from 'node:fs';
const features = [];
for (let i = 0; i < 10000; i++) {
  const x = 37.45 + (i % 100) * 0.0024,
    y = 55.7 + Math.floor(i / 100) * 0.0017;
  features.push({
    type: 'Feature',
    id: `load-segment-${i}`,
    properties: { kind: 'segment', synthetic: true, value: i % 151 },
    geometry: {
      type: 'LineString',
      coordinates: [
        [x, y],
        [x + 0.0018, y + 0.0012],
      ],
    },
  });
}
for (let i = 0; i < 1000; i++)
  features.push({
    type: 'Feature',
    id: `load-stop-${i}`,
    properties: { kind: 'stop', synthetic: true, value: i % 240 },
    geometry: {
      type: 'Point',
      coordinates: [37.45 + (i % 50) * 0.0048, 55.7 + Math.floor(i / 50) * 0.0085],
    },
  });
mkdirSync('tests/fixtures', { recursive: true });
writeFileSync(
  'tests/fixtures/load-network.geojson',
  JSON.stringify({ type: 'FeatureCollection', features }),
);
console.log(
  'Generated synthetic fixture: 10,000 lines and 1,000 points. This is not the Moscow network.',
);

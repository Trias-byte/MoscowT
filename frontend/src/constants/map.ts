import type { MapOptions } from 'maplibre-gl';

export const MAP_ATTRIBUTION =
  '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

export const mapStyle: Exclude<MapOptions['style'], string | undefined> = {
  version: 8,
  glyphs: '/assets/fonts/{fontstack}/{range}.pbf',
  sources: {
    city: {
      type: 'geojson',
      data: '/assets/map/reference-tracks.geojson',
      attribution: MAP_ATTRIBUTION,
    },
  },
  layers: [
    { id: 'background', type: 'background', paint: { 'background-color': '#f1f3ef' } },
    {
      id: 'reference-tracks',
      type: 'line',
      source: 'city',
      paint: { 'line-color': '#c8d2ca', 'line-width': 2, 'line-opacity': 0.6 },
    },
  ],
};

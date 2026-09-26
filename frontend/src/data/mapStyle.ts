import type { StyleSpecification } from 'maplibre-gl';

// Standard OSM tiles are loaded only for the visible viewport and use browser caching.
// A custom hosted style can still be supplied with VITE_MAP_STYLE_URL.
export const mapStyle: StyleSpecification = {
  version: 8,
  glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
  sources: {
    city: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 19,
      attribution:
        '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  },
  layers: [
    { id: 'background', type: 'background', paint: { 'background-color': '#f1f3ef' } },
    {
      id: 'moscow-streets',
      type: 'raster',
      source: 'city',
      paint: {
        'raster-saturation': -0.7,
        'raster-opacity': 0.85,
      },
    },
  ],
};

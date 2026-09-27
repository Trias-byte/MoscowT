import { useEffect, useMemo, useRef, useState } from 'react';
import * as maplibregl from 'maplibre-gl';
import type { GeoJSONSource } from 'maplibre-gl';
import type { FeatureCollection } from 'geojson';
import 'maplibre-gl/dist/maplibre-gl.css';
import type { Network, GeometrySelection, ObjectSelection } from '../lib/contracts';
import type { SectionLoad } from '../lib/sectionLoad';
import { number, valueColor } from '../lib/domain';
import { mapStyle } from '../data/mapStyle';
import type { Incident } from '../lib/scenario';
import type { PoiCollection } from '../lib/passengers';
interface Props {
  poi?: PoiCollection;
  onPoiSelect?: (id: string) => void;
  incidents?: Incident[];
  placingIncident?: boolean;
  onIncidentPlace?: (longitude: number, latitude: number) => void;
  onIncidentMove?: (id: string, longitude: number, latitude: number) => void;
  onIncidentSelect?: (id: string) => void;
  network: Network;
  sectionLoads: Record<string, SectionLoad>;
  geometry?: GeometrySelection;
  selected: ObjectSelection | null;
  onSelect: (selection: ObjectSelection) => void;
  onBounds: (bounds: [number, number, number, number]) => void;
  showReference: boolean;
  showStops: boolean;
  loadThresholds: number[];
}
// Put a direction cue halfway along each section, including short sections where
// line-label placement may omit the symbol entirely.
function arrowPosition(coordinates: number[][]) {
  const lengths = coordinates.slice(1).map((b, i) => {
    const a = coordinates[i];
    return Math.hypot((b[0] - a[0]) * Math.cos(((a[1] + b[1]) * Math.PI) / 360), b[1] - a[1]);
  });
  let remaining = lengths.reduce((sum, length) => sum + length, 0) / 2;
  let index = 0;
  while (index < lengths.length - 1 && remaining > lengths[index]) remaining -= lengths[index++];
  const a = coordinates[index],
    b = coordinates[index + 1] ?? a;
  const fraction = lengths[index] ? remaining / lengths[index] : 0;
  return {
    coordinates: [a[0] + (b[0] - a[0]) * fraction, a[1] + (b[1] - a[1]) * fraction],
    angle:
      (Math.atan2(-(b[1] - a[1]), (b[0] - a[0]) * Math.cos(((a[1] + b[1]) * Math.PI) / 360)) *
        180) /
      Math.PI,
  };
}
export function NetworkMap(p: Props) {
  const element = useRef<HTMLDivElement>(null),
    instance = useRef<maplibregl.Map | null>(null),
    latest = useRef(p);
  latest.current = p;
  const [ready, setReady] = useState(false),
    [failed, setFailed] = useState(false),
    [baseError, setBaseError] = useState(false);
  useEffect(() => {
    const map = instance.current;
    if (!map || !ready) return;
    map.getCanvas().style.cursor = p.placingIncident ? 'crosshair' : '';
  }, [p.placingIncident, ready]);
  useEffect(() => {
    const map = instance.current;
    if (!map || !ready) return;
    const markers = (p.incidents || []).map((incident, index) => {
      const element = document.createElement('button');
      element.className = 'incident-marker';
      element.textContent = '⚠';
      element.setAttribute('aria-label', `ДТП ${index + 1}`);
      element.title = `ДТП: ${incident.duration_minutes} мин, −${Math.round(incident.reduction * 100)}% движения (допущение)`;
      element.addEventListener('click', (event) => {
        event.stopPropagation();
        latest.current.onIncidentSelect?.(incident.id);
      });
      const marker = new maplibregl.Marker({ element, draggable: true })
        .setLngLat([incident.longitude, incident.latitude])
        .addTo(map);
      marker.on('dragend', () => {
        const point = marker.getLngLat();
        latest.current.onIncidentMove?.(incident.id, point.lng, point.lat);
      });
      return marker;
    });
    return () => markers.forEach((marker) => marker.remove());
  }, [p.incidents, ready]);
  const previousSelection = useRef(p.selected);
  const displayed = useMemo(() => {
    const segments = new Set(p.geometry?.segmentIds ?? []),
      stops = new Set(p.geometry?.stopIds ?? []);
    const references = new Set(p.network.routes.filter((r) => !r.isTarget).map((r) => r.id));
    return {
      segments: p.network.segments.filter(
        (s) => segments.has(s.id) || (p.showReference && references.has(s.routeId)),
      ),
      stops: p.network.stops.filter(
        (s) => stops.has(s.id) || (p.showReference && references.has(s.routeId)),
      ),
    };
  }, [p.network, p.geometry, p.showReference]);
  const value = (segmentId: string) => p.sectionLoads[segmentId]?.rate;
  const selectedLine = (segment: Network['segments'][number]) =>
    p.selected?.kind === 'segment'
      ? p.selected.id === segment.id
      : p.selected?.kind === 'route' && p.selected.id === segment.routeId;
  const thresholds = p.loadThresholds;
  useEffect(() => {
    if (!ready || !instance.current) return;
    (instance.current.getSource('passenger-poi') as GeoJSONSource).setData(
      p.poi
        ? { type: 'FeatureCollection', features: p.poi.features }
        : { type: 'FeatureCollection', features: [] },
    );
  }, [ready, p.poi]);
  useEffect(() => {
    setReady(false);
    setBaseError(false);
    let map: maplibregl.Map;
    try {
      map = new maplibregl.Map({
        container: element.current!,
        style: mapStyle,
        center: [37.62, 55.75],
        zoom: 10.1,
        attributionControl: { compact: false },
      });
    } catch {
      setFailed(true);
      return;
    }
    instance.current = map;
    let initialExtent: maplibregl.LngLatBounds | null = null;
    let userMoved = false;
    const fitInitialExtent = () => {
      if (initialExtent && !userMoved)
        map.fitBounds(initialExtent, { padding: 35, duration: 0, maxZoom: 12 });
    };
    map.on('click', (event) => {
      if (latest.current.placingIncident)
        latest.current.onIncidentPlace?.(event.lngLat.lng, event.lngLat.lat);
    });
    map.on('dragstart', () => {
      userMoved = true;
    });
    map.on('zoomstart', (event) => {
      if (event.originalEvent) userMoved = true;
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    map.on('error', (event) => {
      if ('sourceId' in event && event.sourceId === 'city') setBaseError(true);
    });
    map.once('style.load', () => {
      const empty: FeatureCollection = { type: 'FeatureCollection', features: [] };
      map.addSource('route-values', {
        type: 'geojson',
        data: empty,
        attribution:
          '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      });
      map.addSource('section-arrows', { type: 'geojson', data: empty });
      map.addSource('passenger-poi', {
        type: 'geojson',
        data: empty,
        cluster: true,
        clusterRadius: 35,
        attribution: '© OpenStreetMap contributors · ODbL 1.0',
      });
      map.addSource('reference-stops', {
        type: 'geojson',
        data: empty,
        cluster: true,
        clusterRadius: 30,
        clusterMaxZoom: 10,
      });
      map.addLayer({
        id: 'route-casing',
        type: 'line',
        source: 'route-values',
        layout: { 'line-cap': 'round', 'line-join': 'round', 'line-sort-key': ['get', 'sort'] },
        paint: { 'line-color': '#ffffff', 'line-width': 6, 'line-opacity': 0.8, 'line-offset': 3 },
      });
      map.addLayer({
        id: 'route-lines',
        type: 'line',
        source: 'route-values',
        layout: { 'line-cap': 'round', 'line-join': 'round', 'line-sort-key': ['get', 'sort'] },
        paint: {
          'line-color': ['get', 'color'],
          'line-width': 3,
          'line-opacity': 0.85,
          'line-offset': 3,
        },
      });
      map.addLayer({
        id: 'route-selected-halo',
        type: 'line',
        source: 'route-values',
        filter: ['==', ['get', 'selected'], true],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': '#173d34', 'line-width': 8, 'line-offset': 3 },
      });
      map.addLayer({
        id: 'route-selected',
        type: 'line',
        source: 'route-values',
        filter: ['==', ['get', 'selected'], true],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': ['get', 'color'], 'line-width': 5, 'line-offset': 3 },
      });
      const arrow = document.createElement('canvas');
      arrow.width = arrow.height = 24;
      const context = arrow.getContext('2d')!;
      context.lineJoin = 'round';
      context.beginPath();
      context.moveTo(7, 6);
      context.lineTo(15, 12);
      context.lineTo(7, 18);
      context.strokeStyle = '#ffffff';
      context.lineWidth = 5;
      context.stroke();
      context.strokeStyle = '#29493f';
      context.lineWidth = 2;
      context.stroke();
      map.addImage('flow-direction', context.getImageData(0, 0, 24, 24));
      map.addLayer({
        id: 'section-directions',
        type: 'symbol',
        source: 'section-arrows',
        filter: ['!=', ['get', 'selectedSegment'], true],
        layout: {
          'icon-image': 'flow-direction',
          'icon-size': 0.7,
          'icon-rotation-alignment': 'map',
          'icon-rotate': ['get', 'angle'],
          'icon-offset': [0, 4.3],
        },
      });
      map.addLayer({
        id: 'selected-section-direction',
        type: 'symbol',
        source: 'section-arrows',
        filter: ['==', ['get', 'selectedSegment'], true],
        layout: {
          'icon-image': 'flow-direction',
          'icon-size': 0.9,
          'icon-rotation-alignment': 'map',
          'icon-rotate': ['get', 'angle'],
          'icon-offset': [0, 3.3],
          'icon-allow-overlap': true,
        },
      });
      map.addLayer({
        id: 'stop-clusters',
        type: 'circle',
        source: 'reference-stops',
        filter: ['has', 'point_count'],
        paint: {
          'circle-color': '#fff',
          'circle-radius': 14,
          'circle-stroke-width': 2,
          'circle-stroke-color': '#718c7f',
        },
      });
      map.addLayer({
        id: 'stop-cluster-labels',
        type: 'symbol',
        source: 'reference-stops',
        filter: ['has', 'point_count'],
        layout: {
          'text-field': ['get', 'point_count_abbreviated'],
          'text-font': ['Noto Sans Regular'],
          'text-size': 11,
        },
        paint: { 'text-color': '#29493f' },
      });
      map.addLayer({
        id: 'stop-points',
        type: 'circle',
        source: 'reference-stops',
        filter: ['!', ['has', 'point_count']],
        paint: {
          'circle-color': 'white',
          'circle-radius': ['case', ['get', 'selected'], 7, 3.5],
          'circle-stroke-color': '#217b69',
          'circle-stroke-width': 1.5,
        },
      });
      map.addLayer({
        id: 'stop-names',
        type: 'symbol',
        source: 'reference-stops',
        minzoom: 13,
        filter: ['!', ['has', 'point_count']],
        layout: {
          'text-field': ['get', 'name'],
          'text-font': ['Noto Sans Regular'],
          'text-size': 11,
          'text-anchor': 'top',
          'text-offset': [0, 0.7],
          'text-max-width': 14,
        },
        paint: { 'text-color': '#173d34', 'text-halo-color': '#fff', 'text-halo-width': 2 },
      });
      map.on('click', 'stop-clusters', async (event) => {
        if (latest.current.placingIncident) return;
        const feature = event.features?.[0];
        if (feature?.geometry.type !== 'Point') return;
        const zoom = await (
          map.getSource('reference-stops') as GeoJSONSource
        ).getClusterExpansionZoom(feature.properties.cluster_id);
        map.easeTo({ center: feature.geometry.coordinates as [number, number], zoom });
      });
      const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
      map.on('mouseenter', 'stop-points', (event) => {
        const feature = event.features?.[0];
        if (feature?.geometry.type !== 'Point') return;
        map.getCanvas().style.cursor = 'pointer';
        popup
          .setLngLat(feature.geometry.coordinates as [number, number])
          .setText(feature.properties.name)
          .addTo(map);
      });
      map.on('mouseleave', 'stop-points', () => {
        map.getCanvas().style.cursor = '';
        popup.remove();
      });
      map.on('click', 'route-lines', (e) => {
        if (latest.current.placingIncident) return;
        const properties = e.features?.[0]?.properties;
        if (properties?.id && properties?.routeId)
          latest.current.onSelect({
            kind: 'segment',
            id: properties.id,
            routeId: properties.routeId,
          });
      });
      map.on('click', 'stop-points', (e) => {
        if (latest.current.placingIncident) return;
        const id = e.features?.[0]?.properties?.id;
        if (id) latest.current.onSelect({ kind: 'stop', id });
      });
      map.on('mousemove', 'route-lines', (event) => {
        map.getCanvas().style.cursor = 'pointer';
        const text = event.features?.[0]?.properties?.description;
        if (text) popup.setLngLat(event.lngLat).setText(text).addTo(map);
      });
      map.on('mouseleave', 'route-lines', () => {
        map.getCanvas().style.cursor = '';
        popup.remove();
      });
      map.addLayer({
        id: 'passenger-poi-clusters',
        type: 'circle',
        source: 'passenger-poi',
        filter: ['has', 'point_count'],
        paint: { 'circle-color': '#3267a8', 'circle-radius': 15, 'circle-opacity': 0.85 },
      });
      map.addLayer({
        id: 'passenger-poi-count',
        type: 'symbol',
        source: 'passenger-poi',
        filter: ['has', 'point_count'],
        layout: { 'text-field': ['get', 'point_count_abbreviated'], 'text-size': 11 },
        paint: { 'text-color': '#ffffff' },
      });
      map.addLayer({
        id: 'passenger-poi-points',
        type: 'circle',
        source: 'passenger-poi',
        filter: ['!', ['has', 'point_count']],
        paint: {
          'circle-color': '#3267a8',
          'circle-radius': 5,
          'circle-stroke-width': 1.5,
          'circle-stroke-color': '#ffffff',
        },
      });
      map.on('click', 'passenger-poi-points', (e) => {
        if (!latest.current.placingIncident)
          latest.current.onPoiSelect?.(String(e.features?.[0]?.properties?.object_id));
      });
      map.on('click', 'passenger-poi-clusters', async (e) => {
        const feature = e.features?.[0];
        if (!feature || feature.geometry.type !== 'Point') return;
        const zoom = await (
          map.getSource('passenger-poi') as GeoJSONSource
        ).getClusterExpansionZoom(Number(feature.properties.cluster_id));
        map.easeTo({ center: feature.geometry.coordinates as [number, number], zoom });
      });
      map.on('mouseenter', 'passenger-poi-points', () => {
        map.getCanvas().style.cursor = 'pointer';
      });
      map.on('mouseleave', 'passenger-poi-points', () => {
        map.getCanvas().style.cursor = '';
      });
      setReady(true);
      const targetIds = new Set(
        latest.current.network.routes.filter((r) => r.isTarget).map((r) => r.id),
      );
      const extent = new maplibregl.LngLatBounds();
      latest.current.network.stops
        .filter((s) => targetIds.has(s.routeId))
        .forEach((s) => extent.extend(s.coordinates));
      if (!extent.isEmpty()) {
        initialExtent = extent;
        fitInitialExtent();
      }
    });
    const bounds = () => {
      const b = map.getBounds();
      latest.current.onBounds([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
    };
    map.on('moveend', bounds);
    bounds();
    const resize = new ResizeObserver(() => {
      map.resize();
      fitInitialExtent();
    });
    resize.observe(element.current!);
    return () => {
      resize.disconnect();
      map.remove();
      instance.current = null;
    };
  }, [p.network.networkSnapshotId]);
  useEffect(() => {
    const map = instance.current;
    if (!map || !ready || !map.getSource('route-values')) return;
    const lines: FeatureCollection = {
      type: 'FeatureCollection',
      features: displayed.segments.map((s) => ({
        type: 'Feature',
        id: s.id,
        properties: {
          id: s.id,
          routeId: s.routeId,
          color: valueColor(value(s.id), thresholds),
          sort: value(s.id) ?? -1,
          selected: selectedLine(s),
          description: p.sectionLoads[s.id]
            ? `${p.sectionLoads[s.id].fromName} → ${p.sectionLoads[s.id].toName}. ${p.sectionLoads[s.id].directionName}. Модельная оценка: ${number(value(s.id))} на вагон в час.`
            : 'Нет оценки участка',
        },
        geometry: { type: 'LineString', coordinates: s.coordinates },
      })),
    };
    // Deduplicate physical stops. Cluster counts are stops, never sums of route metrics.
    const stations = new Map<string, (typeof displayed.stops)[number]>();
    displayed.stops.forEach((s) => {
      if (
        !stations.has(s.stationId) ||
        (p.selected?.kind === 'route' && p.selected.id === s.routeId) ||
        (p.selected?.kind === 'stop' && p.selected.id === s.id)
      )
        stations.set(s.stationId, s);
    });
    const points: FeatureCollection = {
      type: 'FeatureCollection',
      features: p.showStops
        ? [...stations.values()].map((s) => ({
            type: 'Feature',
            id: s.stationId,
            properties: {
              id: s.id,
              name: s.name,
              selected: p.selected?.kind === 'stop' && p.selected.id === s.id,
            },
            geometry: { type: 'Point', coordinates: s.coordinates },
          }))
        : [],
    };
    (map.getSource('route-values') as GeoJSONSource).setData(lines);
    (map.getSource('section-arrows') as GeoJSONSource).setData({
      type: 'FeatureCollection',
      features: displayed.segments.map((s) => {
        const arrow = arrowPosition(s.coordinates);
        return {
          type: 'Feature',
          properties: {
            angle: arrow.angle,
            selectedSegment: p.selected?.kind === 'segment' && p.selected.id === s.id,
          },
          geometry: { type: 'Point', coordinates: arrow.coordinates },
        };
      }),
    });
    (map.getSource('reference-stops') as GeoJSONSource).setData(points);
  }, [ready, displayed, p.sectionLoads, p.selected, p.showStops, thresholds]);
  useEffect(() => {
    const map = instance.current;
    if (!map || !ready || previousSelection.current === p.selected) return;
    previousSelection.current = p.selected;
    if (p.selected?.kind === 'stop') {
      const stop = p.network.stops.find((s) => s.id === p.selected?.id);
      if (stop)
        map.easeTo({
          center: stop.coordinates,
          zoom: Math.max(map.getZoom(), 14.5),
          duration: 350,
        });
    } else if (p.selected?.kind === 'segment') {
      const segment = p.network.segments.find((s) => s.id === p.selected?.id);
      if (segment) {
        const extent = new maplibregl.LngLatBounds();
        segment.coordinates.forEach((point) => extent.extend(point));
        map.fitBounds(extent, { padding: 70, duration: 350, maxZoom: 15 });
      }
    } else if (p.selected?.kind === 'route') {
      const extent = new maplibregl.LngLatBounds();
      p.network.stops
        .filter((s) => s.routeId === p.selected?.id)
        .forEach((s) => extent.extend(s.coordinates));
      if (!extent.isEmpty()) map.fitBounds(extent, { padding: 50, duration: 350, maxZoom: 14 });
    }
  }, [ready, p.selected, p.network]);
  const project = ([lon, lat]: number[]) => [
    (lon * Math.PI) / 180,
    Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360)),
  ];
  const coordinates = p.network.stops.map((s) => project(s.coordinates)),
    xs = coordinates.map((c) => c[0]),
    ys = coordinates.map((c) => c[1]);
  const west = Math.min(...xs),
    east = Math.max(...xs),
    south = Math.min(...ys),
    north = Math.max(...ys);
  const scale = Math.min(740 / (east - west), 390 / (north - south));
  const xy = (coordinate: number[]) => {
    const [x, y] = project(coordinate);
    return `${400 + (x - (west + east) / 2) * scale},${225 - (y - (south + north) / 2) * scale}`;
  };
  const directedPoints = (coordinates: number[][]) => {
    const points = coordinates.map((c) => xy(c).split(',').map(Number));
    return points.map((point, i) => {
      const a = points[Math.max(0, i - 1)],
        b = points[Math.min(points.length - 1, i + 1)];
      const dx = b[0] - a[0],
        dy = b[1] - a[1],
        length = Math.hypot(dx, dy) || 1;
      return [point[0] - (3 * dy) / length, point[1] + (3 * dx) / length];
    });
  };
  return (
    <div className="v2-map">
      <div ref={element} className="v2-map-canvas" aria-label="Карта трамвайных маршрутов Москвы" />
      {(!ready || failed) && (
        <svg
          className="v2-map-fallback"
          viewBox="0 0 800 450"
          role="img"
          aria-label="Трамвайные пути без подложки"
        >
          <rect width="800" height="450" fill="#eef3ed" />
          {displayed.segments
            .slice()
            .sort((a, b) => (value(a.id) ?? -1) - (value(b.id) ?? -1))
            .map((s) => {
              const points = directedPoints(s.coordinates);
              const mid = Math.min(points.length - 2, Math.floor(points.length / 2));
              const a = points[mid],
                b = points[mid + 1];
              return (
                <g key={s.id}>
                  <polyline
                    key={s.id}
                    points={points.map((p) => p.join(',')).join(' ')}
                    fill="none"
                    stroke={valueColor(value(s.id), thresholds)}
                    strokeWidth={selectedLine(s) ? 5 : 2}
                    onClick={() => p.onSelect({ kind: 'segment', id: s.id, routeId: s.routeId })}
                  />
                  {a && b && (
                    <path
                      d="M -3 -3 L 1 0 L -3 3"
                      transform={`translate(${(a[0] + b[0]) / 2} ${(a[1] + b[1]) / 2}) rotate(${(Math.atan2(b[1] - a[1], b[0] - a[0]) * 180) / Math.PI})`}
                      fill="none"
                      stroke="#29493f"
                      strokeWidth="1.2"
                      pointerEvents="none"
                    />
                  )}
                </g>
              );
            })}
          {p.showStops &&
            displayed.stops.map((s) => {
              const [cx, cy] = xy(s.coordinates).split(',');
              return (
                <circle
                  key={s.id}
                  cx={cx}
                  cy={cy}
                  r="2.5"
                  fill="white"
                  stroke="#497d69"
                  onClick={() => p.onSelect({ kind: 'stop', id: s.id })}
                >
                  <title>{s.name}</title>
                </circle>
              );
            })}
        </svg>
      )}
      <div className="v2-map-note">
        <b>Карта Москвы</b>
        <span>Маршруты на {p.network.asOf} · архив 2025</span>
        <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">
          © OpenStreetMap · ODbL
        </a>
        {baseError && <small>Справочная схема недоступна; выбранные маршруты сохранены.</small>}
      </div>
      <div className="v2-legend">
        <b>Участки · модельная оценка</b>
        <small>Условный поток на вагон в час</small>
        <div>
          {['0', ...thresholds.map(String)].map((label, i) => (
            <span key={label}>
              <i style={{ background: valueColor(i ? thresholds[i - 1] : 0, thresholds) }} />
              {label}
              {i === 4 ? '+' : ''}
            </span>
          ))}
        </div>
        <small>
          Стрелки — направление. Серый — нет оценки.
          <br />
          Места валидаций неизвестны.
        </small>
      </div>
      {p.geometry?.historicallyUnavailablePatternIds.length ? (
        <div className="v2-map-warning">
          Историческая география для выбранной даты не подтверждена. Числовые ряды сохранены.
        </div>
      ) : null}
    </div>
  );
}

import type { Capabilities, ViewState, GeometryScope } from './contracts';
import { dateBounds } from './time';
export function initialView(c: Capabilities): ViewState {
  const defaults: ViewState = {
    contractVersion: 2,
    routeIds: c.targetRouteIds,
    mode: 'auto',
    windowHours: 24,
    date: c.defaultDate,
    snapshotId: c.snapshotId,
    publishedSnapshotId: c.snapshotId,
    index: 0,
    geometry: { patternIds: [], section: null, bbox: null, referenceMode: 'reference' },
  };
  try {
    const saved = JSON.parse(new URLSearchParams(location.search).get('view') || 'null');
    if (saved?.contractVersion !== 2) return defaults;
    const range = dateBounds(c);
    // Old month / competition links must never override the chosen calendar day.
    const windowHours = [1, 12, 24].includes(saved.windowHours)
      ? saved.windowHours
      : saved.horizon === 'day'
        ? 1
        : 24;
    const geometry: GeometryScope = {
      patternIds: Array.isArray(saved.geometry?.patternIds)
        ? saved.geometry.patternIds.filter((id: unknown) => typeof id === 'string').slice(0, 20)
        : [],
      section:
        saved.geometry?.section &&
        ['patternId', 'fromId', 'toId'].every(
          (key) => typeof saved.geometry.section[key] === 'string',
        )
          ? saved.geometry.section
          : null,
      bbox: null,
      referenceMode: saved.geometry?.referenceMode === 'historical' ? 'historical' : 'reference',
    };
    return {
      ...defaults,
      routeIds: Array.isArray(saved.routeIds)
        ? [...new Set<string>(saved.routeIds.filter((r: string) => c.targetRouteIds.includes(r)))]
        : defaults.routeIds,
      windowHours,
      date:
        typeof saved.date === 'string' &&
        /^\d{4}-\d{2}-\d{2}$/.test(saved.date) &&
        new Date(`${saved.date}T12:00:00Z`).toISOString().slice(0, 10) === saved.date &&
        saved.date >= range.min &&
        saved.date <= range.max
          ? saved.date
          : defaults.date,
      snapshotId:
        typeof saved.snapshotId === 'string' && saved.snapshotId.startsWith('snapshot-')
          ? saved.snapshotId
          : c.snapshotId,
      index: Number.isInteger(saved.index)
        ? Math.min(24 - windowHours, Math.max(0, saved.index))
        : 0,
      scenarioId: typeof saved.scenarioId === 'string' ? saved.scenarioId : undefined,
      scenarioRange:
        saved.forecastId &&
        saved.scenarioRange &&
        Number.isFinite(Date.parse(saved.scenarioRange.start)) &&
        Date.parse(saved.scenarioRange.end) > Date.parse(saved.scenarioRange.start)
          ? saved.scenarioRange
          : undefined,
      scenarioName: typeof saved.scenarioName === 'string' ? saved.scenarioName : undefined,
      scenarioIncidents: Array.isArray(saved.scenarioIncidents) ? saved.scenarioIncidents : [],
      forecastId: typeof saved.forecastId === 'string' ? saved.forecastId : undefined,
      publishedSnapshotId:
        typeof saved.publishedSnapshotId === 'string'
          ? saved.publishedSnapshotId
          : saved.snapshotId || c.snapshotId,
      geometry,
    };
  } catch {
    return defaults;
  }
}
export function saveView(view: ViewState) {
  const p = new URLSearchParams(location.search);
  p.set('view', JSON.stringify({ ...view, geometry: { ...view.geometry, bbox: null } }));
  history.replaceState(null, '', `${location.pathname}?${p}`);
}
export function readUi<T>(key: string, fallback: T): T {
  try {
    return JSON.parse(localStorage.getItem(`potok-${key}`) || 'null') ?? fallback;
  } catch {
    return fallback;
  }
}
export function saveUi(key: string, value: unknown) {
  try {
    localStorage.setItem(`potok-${key}`, JSON.stringify(value));
  } catch {
    /* Storage is optional. */
  }
}

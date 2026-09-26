import type { Capabilities, Scope, ViewState } from './contracts';
export const MOSCOW = 'Europe/Moscow';
export const localDate = (iso: string) =>
  new Intl.DateTimeFormat('sv-SE', { timeZone: MOSCOW }).format(new Date(iso));
export const dateLabel = (iso: string) =>
  new Intl.DateTimeFormat('ru-RU', { timeZone: MOSCOW, day: 'numeric', month: 'short' }).format(
    new Date(iso.length === 10 ? `${iso}T12:00:00+03:00` : iso),
  );
export const hour = (iso: string) =>
  new Intl.DateTimeFormat('ru-RU', { timeZone: MOSCOW, hour: '2-digit', minute: '2-digit' }).format(
    new Date(iso),
  );
export const intervalLabel = (start: string, end: string, grain: 'hour' | 'day') =>
  grain === 'hour'
    ? `${dateLabel(start)} · ${hour(start)}–${hour(end) === '00:00' && end !== start ? '24:00' : hour(end)}`
    : dateLabel(start);
export const windowLabel = (hours: number) =>
  hours === 24 ? 'весь день' : hours === 12 ? '12 часов' : 'час';
export function makeScope(view: ViewState): Scope {
  const [year, month, day] = view.date.split('-').map(Number);
  const start = Date.UTC(year, month - 1, day, -3),
    end = start + 86400000;
  return {
    routeIds: view.routeIds,
    metric: 'successful_validations',
    metricScope: 'route',
    mode: 'auto',
    timeRange: { start: new Date(start).toISOString(), end: new Date(end).toISOString() },
    grain: 'hour',
    aggregation: 'sum',
    snapshotId: view.snapshotId,
    geometry: view.geometry,
  };
}
function dataRange(c: Capabilities) {
  const ranges = [c.historyRange, c.forecastRange].filter((r) => r !== null);
  return ranges.length
    ? {
        start: new Date(Math.min(...ranges.map((r) => Date.parse(r.start)))).toISOString(),
        end: new Date(Math.max(...ranges.map((r) => Date.parse(r.end)))).toISOString(),
      }
    : null;
}
export function dateBounds(c: Capabilities) {
  const range = dataRange(c);
  return range
    ? {
        min: localDate(range.start),
        max: localDate(new Date(Date.parse(range.end) - 1).toISOString()),
      }
    : { min: c.defaultDate, max: c.defaultDate };
}

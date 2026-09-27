import type { ObjectSelection } from './contracts';

export const factorNumber = (value: number | null | undefined) =>
  value == null || !Number.isFinite(value)
    ? 'Нет данных'
    : value.toLocaleString('ru-RU', { maximumFractionDigits: 3 });

export function factorRange(min: number | null | undefined, max: number | null | undefined) {
  if (min == null || max == null || !Number.isFinite(min) || !Number.isFinite(max)) return '';
  return min === max ? factorNumber(min) : `${factorNumber(min)}…${factorNumber(max)}`;
}

export function contextSelection(selected: ObjectSelection, routeId: string, routes: string[]) {
  return routes.includes(routeId) ? { kind: selected.kind, id: selected.id } : null;
}

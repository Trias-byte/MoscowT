import { moscowDateTime } from './time';
export const dateTimeInput = (iso: string) => moscowDateTime(iso).slice(0, 16);
export const inputTimestamp = (value: string) =>
  value.includes('T') ? `${value.slice(0, 16)}:00+03:00` : `${value}T00:00:00+03:00`;
export function forecastPeriod(
  origin: string,
  horizon: 'day' | 'month' | 'year' | '61days',
): [string, string] {
  const start = new Date(`${origin.includes('T') ? origin.slice(0, 16) : origin + 'T00:00'}:00Z`);
  const end = new Date(start);
  if (horizon === 'day') end.setUTCDate(end.getUTCDate() + 1);
  else if (horizon === '61days') end.setUTCDate(end.getUTCDate() + 61);
  else {
    const day = end.getUTCDate();
    end.setUTCDate(1);
    if (horizon === 'year') end.setUTCFullYear(end.getUTCFullYear() + 1);
    else end.setUTCMonth(end.getUTCMonth() + 1);
    const max = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth() + 1, 0)).getUTCDate();
    end.setUTCDate(Math.min(day, max));
  }
  return [start.toISOString().slice(0, 16), end.toISOString().slice(0, 16)];
}

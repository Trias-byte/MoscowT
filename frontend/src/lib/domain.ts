import type { Frame, Provenance } from './contracts';

export const sourceLabel = (source?: Provenance) =>
  source === 'observation'
    ? 'Фактические данные'
    : source === 'forecast'
      ? 'Прогноз'
      : source === 'mixed'
        ? 'Факт и прогноз'
        : 'Нет данных';

export function frameSource(frame?: Frame): Provenance {
  const sources = new Set(frame?.values.map((v) => v.provenance));
  if (sources.has('mixed') || (sources.has('observation') && sources.has('forecast')))
    return 'mixed';
  return sources.has('observation')
    ? 'observation'
    : sources.has('forecast')
      ? 'forecast'
      : 'missing';
}

// A missing hour is not zero: only complete windows have a numeric total.
export function sumWindow(frames: Frame[], startHour: number, hours: number): Frame | undefined {
  const selected = frames.slice(startHour, startHour + hours);
  if (selected.length !== hours) return undefined;
  if (hours === 1) return selected[0];
  const sum = (values: (number | null | undefined)[]) =>
    values.every((v): v is number => v != null) ? values.reduce((a, b) => a + b, 0) : null;
  const values = selected[0].values.map((first) => {
    const rows = selected.map((f) => f.values.find((v) => v.routeId === first.routeId));
    const value = sum(rows.map((v) => v?.value));
    const provenance = frameSource({ ...selected[0], values: rows.filter((v) => v !== undefined) });
    const counts = rows.map((v) => v?.baselineCount);
    const vehicleHours = sum(rows.map((v) => v?.vehicleHours));
    const fleetSources = new Set(rows.map((v) => v?.fleetSource ?? 'missing'));
    const fleetSource = fleetSources.has('missing')
      ? 'missing'
      : fleetSources.size > 1
        ? 'mixed'
        : [...fleetSources][0];
    const fleetCoverages = rows.flatMap((v) => (v?.fleetCoverage == null ? [] : [v.fleetCoverage]));
    return {
      ...first,
      value,
      baseline: sum(rows.map((v) => v?.baseline)),
      baselineCount: counts.every((v): v is number => v != null) ? Math.min(...counts) : null,
      provenance,
      valueOrigins: [...new Set(rows.flatMap((v) => v?.valueOrigins ?? []))],
      qualityFlags: [...new Set(rows.flatMap((v) => v?.qualityFlags ?? []))],
      coverageStatus:
        value === null ? 'missing' : provenance === 'observation' ? 'provided_extract' : provenance,
      vehicleHours,
      fleetVehicles: vehicleHours === null ? null : vehicleHours / hours,
      loadPerVehicleHour:
        value !== null && vehicleHours !== null && vehicleHours > 0 ? value / vehicleHours : null,
      fleetSource,
      fleetSampleDays: Math.min(...rows.map((v) => v?.fleetSampleDays ?? 0)),
      fleetCoverage: fleetCoverages.length ? Math.min(...fleetCoverages) : null,
    };
  });
  return {
    start: selected[0].start,
    end: selected[selected.length - 1].end,
    values,
    aggregate: sum(values.map((v) => v.value)),
    baseline: sum(values.map((v) => v.baseline)),
  };
}

export const COLORS = ['#93cdbd', '#46a995', '#167f78', '#e5a344', '#ce6b54'];
// Rounded historical mean of route/day hourly peaks: 2,077.5 validations.
export const HOURLY_LOAD_THRESHOLDS = [200, 600, 1200, 2000];
export const VEHICLE_LOAD_THRESHOLDS = [5, 20, 34, 50];
export const fleetSourceLabel = (source?: Frame['values'][number]['fleetSource']) =>
  source === 'planned_duty'
    ? 'План по выходам'
    : source === 'stop_timetable_estimate'
      ? 'Оценка по остановочному расписанию'
      : source === 'observed'
        ? 'Оценка по бортовым номерам'
        : source === 'estimated'
          ? 'Оценка по предыдущим 8 неделям'
          : source === 'mixed'
            ? 'Бортовые номера и историческая оценка'
            : 'Нет оценки вагонов';
export const number = (value: number | null | undefined) =>
  value == null
    ? 'Нет данных'
    : new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 }).format(value);
export function valueColor(value: number | null | undefined, thresholds = HOURLY_LOAD_THRESHOLDS) {
  return value == null ? '#adb8b3' : COLORS[thresholds.filter((t) => value >= t).length];
}
export function csvEscape(value: unknown) {
  const s = value == null ? '' : String(value);
  return '"' + (/^[\s]*[=+@-]/.test(s) ? "'" + s : s).replaceAll('"', '""') + '"';
}

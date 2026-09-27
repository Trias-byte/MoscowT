import { moscowDateTime } from './time';

export type ExportPeriod = { start: string; end: string };

export function exportPeriodError(period: ExportPeriod, grain: string): string | null {
  const stamps = [period.start, period.end].map(Date.parse);
  if (stamps.some((stamp) => !Number.isFinite(stamp)))
    return 'Укажите дату начала и дату окончания выгрузки.';
  if (stamps[1] <= stamps[0]) return 'Конец периода должен быть позже начала.';
  const local = [period.start, period.end].map(moscowDateTime);
  if (stamps.some((stamp) => stamp % 3600000 !== 0))
    return 'Данные почасовые. Выберите время с нулевыми минутами, например 23:00.';
  if (grain !== 'hour' && local.some((stamp) => stamp.slice(11, 13) !== '00'))
    return 'Для выгрузки по суткам или месяцам выберите 00:00 на обеих границах.';
  if (grain === 'month' && local.some((stamp) => stamp.slice(8, 10) !== '01'))
    return 'Для выгрузки по месяцам выберите первое число месяца на обеих границах.';
  return null;
}

export { ROUTE_COLORS } from '../constants/dynamics';
import type { ViewData } from './contracts';
import { number } from './domain';

const escape = (value: string) =>
  value.replace(
    /[&<>"']/g,
    (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!,
  );

export function dynamicsTooltip(
  rows: ViewData['series'],
  index: number,
  label: string,
  baseline: boolean,
) {
  return `<div class="dynamics-tooltip"><strong>${escape(label)} · МСК</strong><table><thead><tr><th>Маршрут</th><th>Валидации</th>${baseline ? '<th>Среднее</th>' : ''}</tr></thead><tbody>${rows.map((row) => `<tr><td>№ ${escape(row.routeId)}</td><td>${number(row.points[index] ?? null)}</td>${baseline ? `<td>${number(row.baseline[index] ?? null)}</td>` : ''}</tr>`).join('')}</tbody></table></div>`;
}

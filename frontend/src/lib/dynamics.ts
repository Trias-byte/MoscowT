import type { ViewData } from './contracts';
import { number } from './domain';

export const ROUTE_COLORS = [
  '#187f70',
  '#b45b1f',
  '#6258b8',
  '#c44065',
  '#2788ad',
  '#78831d',
  '#8b5792',
  '#447b31',
  '#b03f32',
  '#596980',
];
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

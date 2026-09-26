import { useMemo } from 'react';
import type { EChartsCoreOption } from 'echarts/core';
import type { Frame, Scope, ViewData } from '../lib/contracts';
import { Chart } from './Chart';
import { intervalLabel, dateLabel, hour } from '../lib/time';
import { number, sourceLabel } from '../lib/domain';
export type ChartTab = 'dynamics' | 'comparison' | 'heatmap' | 'table';
export function Analytics(p: {
  data: ViewData;
  scope: Scope;
  frame?: Frame;
  tab: ChartTab;
  onTab: (tab: ChartTab) => void;
  onSelect: (route: string, index?: number) => void;
}) {
  const frame = p.frame;
  const option = useMemo<EChartsCoreOption>(() => {
    const labels = p.data.frames.map((f) =>
      p.scope.grain === 'hour' ? hour(f.start) : dateLabel(f.start),
    );
    const base = {
      grid: { left: 65, right: 25, top: 32, bottom: 32 },
      xAxis: { type: 'category', data: labels },
      yAxis: { type: 'value' },
    };
    if (p.tab === 'dynamics')
      return {
        ...base,
        legend: { type: 'scroll', top: 0 },
        series: p.data.series.slice(0, 4).flatMap((s) => [
          {
            name: `№ ${s.routeId}`,
            type: 'line',
            data: s.points,
            symbol: 'none',
            connectNulls: false,
          },
          {
            name: `Среднее № ${s.routeId}`,
            type: 'line',
            data: s.baseline,
            lineStyle: { type: 'dashed', opacity: 0.5 },
            symbol: 'none',
            connectNulls: false,
          },
        ]),
      };
    if (p.tab === 'comparison')
      return {
        grid: { left: 65, right: 25, top: 15, bottom: 32 },
        xAxis: { type: 'category', data: (frame?.values ?? []).map((r) => `№ ${r.routeId}`) },
        yAxis: { type: 'value' },
        series: [
          {
            type: 'bar',
            name: 'За выбранный интервал',
            data: (frame?.values ?? []).map((r) => r.value),
            itemStyle: { color: '#218c79' },
          },
        ],
      };
    return {
      grid: { left: 65, right: 45, top: 12, bottom: 35 },
      tooltip: { trigger: 'item' },
      xAxis: { type: 'category', data: labels },
      yAxis: { type: 'category', data: p.data.heatmap.routeIds.map((r) => `№ ${r}`) },
      visualMap: {
        show: false,
        min: 0,
        max: Math.max(1, ...p.data.heatmap.cells.map((c) => c[2] ?? 0)),
        inRange: { color: ['#f1f5df', '#9ed1b3', '#3eab98', '#09796d'] },
      },
      series: [
        {
          type: 'heatmap',
          data: p.data.heatmap.cells.map(([x, y, value]) => ({
            value: [x, y, value],
            itemStyle:
              value === null
                ? {
                    color: '#d3dad5',
                    decal: { symbol: 'rect', dashArrayX: [1, 0], dashArrayY: [2, 3] },
                  }
                : undefined,
          })),
        },
      ],
    };
  }, [p.data, p.tab, p.scope.grain, frame]);
  return (
    <section className="v2-analytics" aria-label="Аналитика маршрутов">
      <div className="v2-tabs" role="tablist">
        {(
          [
            ['dynamics', 'Динамика'],
            ['comparison', 'Сравнение маршрутов'],
            ['heatmap', 'Тепловая матрица'],
            ['table', 'Таблица'],
          ] as const
        ).map(([id, label]) => (
          <button role="tab" aria-selected={p.tab === id} key={id} onClick={() => p.onTab(id)}>
            {label}
          </button>
        ))}
      </div>
      <div className="v2-chart-caption">
        {p.tab === 'comparison'
          ? 'Сумма по маршрутам за выбранный интервал'
          : p.tab === 'dynamics'
            ? 'Весь день по часам · до четырёх маршрутов · пунктир — историческое среднее'
            : p.tab === 'heatmap'
              ? 'Весь день по часам · нажмите ячейку для выбора часа'
              : 'Маршрутные значения выбранного интервала'}{' '}
        · успешные валидации
      </div>
      {p.tab === 'table' ? (
        <div className="v2-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Маршрут</th>
                <th>Значение</th>
                <th>Источник</th>
                <th>Среднее</th>
                <th>Покрытие</th>
                <th>Вагонов в среднем · оценка</th>
                <th>На вагон в час</th>
              </tr>
            </thead>
            <tbody>
              {frame?.values.map((v) => (
                <tr key={v.routeId}>
                  <td>
                    <button onClick={() => p.onSelect(v.routeId)}>№ {v.routeId}</button>
                  </td>
                  <td>{number(v.value)}</td>
                  <td>{sourceLabel(v.provenance)}</td>
                  <td>{number(v.baseline)}</td>
                  <td>
                    {v.coverageStatus === 'missing'
                      ? 'Нет данных'
                      : v.qualityFlags.length
                        ? 'Требует проверки'
                        : v.historySupport === 'no_positive_history'
                          ? 'Нет положительной истории'
                          : v.provenance === 'forecast'
                            ? 'Модельная оценка'
                            : 'Предоставленная выгрузка'}
                  </td>
                  <td>{number(v.fleetVehicles)}</td>
                  <td>{number(v.loadPerVehicleHour)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Chart
          option={option}
          label={`${p.tab === 'heatmap' ? 'Тепловая матрица маршрутов' : 'График успешных валидаций'}; числовая альтернатива во вкладке Таблица`}
          onSelect={(index, value) => {
            if (p.tab === 'heatmap' && Array.isArray(value))
              p.onSelect(p.data.heatmap.routeIds[value[1]], value[0]);
            else if (p.tab === 'comparison') p.onSelect(frame!.values[index].routeId);
          }}
        />
      )}
      <small>
        {frame ? intervalLabel(frame.start, frame.end, p.scope.grain) : 'Нет данных'} · МСК ·{' '}
        {p.data.meta.snapshotId}
      </small>
    </section>
  );
}

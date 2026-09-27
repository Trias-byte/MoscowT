import { STATUS } from '../constants/passengers';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Scope } from '../lib/contracts';
import { platform } from '../lib/platform';
import { number } from '../lib/domain';
import { intervalLabel } from '../lib/time';
import { Chart } from './Chart';
import {
  CATEGORIES,
  Cohorts,
  Seasonality,
  passengerMetadata,
  passengerQuery,
  parameters,
  downloadPassengerCsv,
  type ResearchRow,
  type PassengerMeta,
} from '../lib/passengers';

export function ResearchTable({
  rows,
  columns,
}: {
  rows: ResearchRow[];
  columns: [string, string][];
}) {
  return (
    <div className="passenger-table">
      <table>
        <thead>
          <tr>
            {columns.map(([key, label]) => (
              <th key={key}>{label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map(([key]) => {
                const value = row[key];
                return (
                  <td key={key}>
                    {typeof value === 'number'
                      ? key.includes('share') || key === 'active_fraction'
                        ? `${(value * 100).toLocaleString('ru', { maximumFractionDigits: 1 })}%`
                        : number(value)
                      : typeof value === 'boolean'
                        ? value
                          ? 'Да'
                          : 'Нет'
                        : value == null
                          ? '—'
                          : CATEGORIES[value] || STATUS[value] || value}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function Export({ name, rows, meta }: { name: string; rows: ResearchRow[]; meta: PassengerMeta }) {
  return (
    <button
      className="v2-outline"
      disabled={!rows.length}
      onClick={() => downloadPassengerCsv(name, rows, meta)}
    >
      Скачать CSV
    </button>
  );
}
function Version({ meta }: { meta: PassengerMeta }) {
  return (
    <small className="passenger-version">
      МСК · набор {meta.dataset_id} · история {meta.coverage.start.slice(0, 10)}–
      {meta.coverage.end.slice(0, 10)} (конец не включён)
    </small>
  );
}
export function PassengerComposition({
  scope,
  compact = false,
}: {
  scope: Scope;
  compact?: boolean;
}) {
  const [category, setCategory] = useState('');
  const query = useQuery({
    queryKey: [
      'passengers',
      'query',
      scope.snapshotId,
      scope.routeIds,
      scope.timeRange,
      scope.mode,
      scope.grain,
      category,
    ],
    queryFn: ({ signal }) => passengerQuery(scope, category ? [category] : [], signal),
    staleTime: 60000,
  });
  const result = query.data;
  return (
    <section
      aria-label={compact ? 'Состав пассажиропотока маршрута' : 'Категории билетов'}
      className="passenger-composition"
    >
      <h3>Состав пассажиропотока</h3>
      <p className="v2-hint">
        {intervalLabel(scope.timeRange.start, scope.timeRange.end, scope.grain)} · МСК
      </p>
      {!compact && (
        <label className="v2-field">
          Категория билета
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">Все категории</option>
            {Object.entries(CATEGORIES).map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </label>
      )}
      {query.isPending && <p role="status">Загружаем категории…</p>}
      {query.error && <p role="alert">{query.error.message}</p>}
      {result?.meta.status !== 'ready' && result && (
        <p className="v2-notice">{result.meta.reason}</p>
      )}
      {result?.meta.status === 'ready' && (
        <>
          {!compact && (
            <Chart
              label="Валидации по категориям билетов"
              className="passenger-chart"
              option={{
                grid: { left: 155, right: 25, top: 10, bottom: 28 },
                xAxis: { type: 'value' },
                yAxis: {
                  type: 'category',
                  data: result.summary.map((r) => CATEGORIES[String(r.category)]),
                },
                series: [
                  {
                    type: 'bar',
                    data: result.summary.map((r) => r.boardings),
                    itemStyle: { color: '#218c79' },
                  },
                ],
              }}
            />
          )}
          <ResearchTable
            rows={result.summary}
            columns={[
              ['category', 'Категория'],
              ['boardings', 'Валидации'],
              ['share', 'Доля всех валидаций'],
              ...(!compact && result.summary.some((r) => r.unique_cards != null)
                ? [['unique_cards', 'Карт за этот час'] as [string, string]]
                : []),
            ]}
          />
          {!compact && (
            <>
              <Export name="categories" rows={result.summary} meta={result.meta} />
              <button
                className="v2-text"
                onClick={() => downloadPassengerCsv('route-hours', result.rows, result.meta)}
              >
                Выгрузить детализацию по маршрутам и времени
              </button>
              <Version meta={result.meta} />
            </>
          )}
        </>
      )}
      <p className="v2-hint">
        Категория определяется билетом. Карта не равна человеку; возраст неизвестен. Уникальные
        карты между периодами и группами не складываются.
      </p>
    </section>
  );
}
function SeasonalView({ routes }: { routes: string[] }) {
  const query = useQuery({
    queryKey: ['passengers', 'seasonality', routes],
    queryFn: ({ signal }) =>
      platform(
        `/passengers/seasonality?${parameters({ route_ids: routes })}`,
        Seasonality,
        undefined,
        signal,
      ),
    staleTime: 60000,
  });
  const data = query.data;
  if (!data)
    return (
      <p role={query.error ? 'alert' : 'status'}>
        {query.error?.message || 'Загружаем сезонность…'}
      </p>
    );
  return (
    <>
      <h3>Месячные профили всей сети</h3>
      <p className="v2-hint">
        Январь–октябрь 2025 · все исследуемые маршруты. Средние валидации в день с одинаковыми
        весами дней недели. Фильтр маршрутов применяется к сравнениям ниже.
      </p>
      <Chart
        className="passenger-chart"
        label="Сезонность категорий по всей сети"
        option={{
          grid: { left: 65, right: 20, top: 40, bottom: 25 },
          legend: { type: 'scroll' },
          xAxis: {
            type: 'category',
            data: ['Янв', 'Фев', 'Мар', 'Апр', 'Май', 'Июн', 'Июл', 'Авг', 'Сен', 'Окт'],
          },
          yAxis: { type: 'value' },
          series: Object.entries(CATEGORIES).map(([id, name]) => ({
            name,
            type: 'line',
            data: Array.from(
              { length: 10 },
              (_, i) =>
                data.monthly.find((r) => r.category === id && r.month === i + 1)
                  ?.boardings_per_day_standardized ?? null,
            ),
            connectNulls: false,
          })),
        }}
      />
      <details>
        <summary>Месячные значения и CSV</summary>
        <ResearchTable
          rows={data.monthly}
          columns={[
            ['month', 'Месяц'],
            ['category', 'Категория'],
            ['boardings_per_day_standardized', 'Валидаций в день · стандарт.'],
            ['monthly_unique_cards', 'Уникальных карт за месяц'],
          ]}
        />
        <Export
          name="monthly"
          rows={data.monthly}
          meta={{ ...data.meta, filters: { scope: 'network', period: '2025-01-01/2025-11-01' } }}
        />
      </details>
      <h3>
        {routes.length ? 'Вклад категорий по выбранным маршрутам' : 'Вклад категорий по всей сети'}
      </h3>
      <p className="v2-hint">
        Фиксированные сравнения апреля–мая с июнем–августом и августа с сентябрём–октябрём. Вклад —
        изменение среднего числа валидаций за день.
      </p>
      <ResearchTable
        rows={data.comparisons}
        columns={[
          ['route', 'Маршрут'],
          ['comparison', 'Сравнение'],
          ['category', 'Категория'],
          ['delta_boardings_per_day', 'Изменение в день'],
          ['share_of_net_change', 'Доля общего изменения'],
        ]}
      />
      <Export name="seasonality" rows={data.comparisons} meta={data.meta} />
      <Version meta={data.meta} />
    </>
  );
}
function CohortView() {
  const query = useQuery({
    queryKey: ['passengers', 'cohorts'],
    queryFn: ({ signal }) => platform('/passengers/cohorts', Cohorts, undefined, signal),
    staleTime: 60000,
  });
  const data = query.data;
  if (!data)
    return (
      <p role={query.error ? 'alert' : 'status'}>{query.error?.message || 'Загружаем когорты…'}</p>
    );
  const periods = [...new Set(data.monthly.map((r) => String(r.period_start)))].sort();
  return (
    <>
      <h3>Возвращение и активность карт</h3>
      <p className="v2-notice">
        Вся исследуемая сеть. Когорта: карты с поездками в апреле–мае 2025; наблюдение до конца
        октября. Выбор маршрутов и даты карты не меняет эту сводку. Категория когорты зафиксирована
        на 1 июня.
      </p>
      <Chart
        className="passenger-chart"
        label="Доля активных карт весенней когорты"
        option={{
          grid: { left: 50, right: 20, top: 40, bottom: 25 },
          legend: { type: 'scroll' },
          xAxis: { type: 'category', data: periods.map((p) => p.slice(0, 7)) },
          yAxis: { type: 'value', max: 100, axisLabel: { formatter: '{value}%' } },
          series: Object.entries(CATEGORIES).map(([id, name]) => ({
            name,
            type: 'line',
            data: periods.map((p) => {
              const value = data.monthly.find(
                (r) => r.cohort_category === id && r.period_start === p,
              )?.active_fraction;
              return typeof value === 'number' ? value * 100 : null;
            }),
          })),
        }}
      />
      <details>
        <summary>Месячная активность и CSV</summary>
        <ResearchTable
          rows={data.monthly}
          columns={[
            ['period_start', 'Месяц'],
            ['cohort_category', 'Категория когорты'],
            ['cohort_cards', 'Карт в когорте'],
            ['active_cards', 'Активных карт'],
            ['active_fraction', 'Доля активных'],
          ]}
        />
        <Export name="cohort-monthly" rows={data.monthly} meta={data.meta} />
      </details>
      <h3>Осень: сентябрь–октябрь 2025</h3>
      <ResearchTable
        rows={data.autumn}
        columns={[
          ['autumn_status', 'Статус'],
          ['in_spring_cohort', 'В весенней когорте'],
          ['active_cards', 'Карт'],
          ['boardings', 'Валидаций'],
          ['share_of_autumn_boardings', 'Доля осенних валидаций'],
        ]}
      />
      <Export name="autumn" rows={data.autumn} meta={data.meta} />
      <Version meta={data.meta} />
    </>
  );
}
export function PassengerPanel({ scope }: { scope: Scope }) {
  const [tab, setTab] = useState('categories');
  return (
    <div className="passenger-panel" aria-label="Пассажирская аналитика">
      <div className="passenger-subtabs" role="tablist" aria-label="Разделы пассажирской аналитики">
        {[
          ['categories', 'Категории'],
          ['seasonality', 'Сезонность'],
          ['cohorts', 'Когорты'],
        ].map(([id, label]) => (
          <button role="tab" key={id} aria-selected={tab === id} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </div>
      {tab === 'categories' ? (
        <PassengerComposition scope={scope} />
      ) : tab === 'seasonality' ? (
        <SeasonalView routes={scope.routeIds} />
      ) : (
        <CohortView />
      )}
    </div>
  );
}
export function PassengerPassport() {
  const query = useQuery({
    queryKey: ['passengers', 'metadata'],
    queryFn: ({ signal }) => passengerMetadata(signal),
    staleTime: 60000,
  });
  const data = query.data;
  const experimentRows = data?.available
    ? data.experiments.map((r) => ({
        model: r.model,
        wape_percent: r.mean_wape * 100,
        wins: r.fold_wins,
        accepted: r.meets_acceptance,
      }))
    : [];
  const meta: PassengerMeta | undefined = data?.available
    ? {
        dataset_id: data.id,
        coverage: data.coverage,
        timezone: 'Europe/Moscow',
        filters: { scope: 'network', period: '2025-01-01/2025-11-01' },
        status: 'ready',
        reason: null,
      }
    : undefined;
  return (
    <details className="passenger-passport">
      <summary>Пассажирский набор · категории и инфраструктура</summary>
      {query.error && <p role="alert">{query.error.message}</p>}
      {!data && !query.error && <p>Загружаем паспорт…</p>}
      {data &&
        (!data.available ? (
          <p>{data.reason}</p>
        ) : (
          <>
            <p>
              Январь–октябрь 2025 · {number(data.total_boardings)} успешных валидаций ·{' '}
              {number(data.unique_cards)} уникальных карт за весь период.
            </p>
            <p>
              {data.snapshots.length} месячных срезов инфраструктуры. Классификация билетов:{' '}
              {data.taxonomy_version}. Исходный отчёт: {data.source_checks} проверок; файлы пакета
              проверяются при установке.
            </p>
            <small>{data.id}</small>
            <h3>Исследовательские модели</h3>
            <p>
              Сравнение с собственной базой эксперимента на четырёх окнах по 61 дню. Это отдельный
              эксперимент, не сравнение с текущей рабочей моделью. Новые варианты не прошли критерий
              принятия.
            </p>
            <ResearchTable
              rows={experimentRows}
              columns={[
                ['model', 'Вариант'],
                ['wape_percent', 'Средняя WAPE, %'],
                ['wins', 'Выиграно окон'],
                ['accepted', 'Критерий пройден'],
              ]}
            />
            {meta && (
              <Export
                name="experiments"
                rows={experimentRows}
                meta={{
                  ...meta,
                  filters: {
                    scope: 'research_experiment',
                    baseline: 'own_experimental_baseline',
                    evaluation_windows: 4,
                    horizon_days: 61,
                    evaluation_origins: [...new Set(data.experiment_metrics.map((r) => r.origin))],
                  },
                }}
              />
            )}
            {data.notes.map((note) => (
              <p className="v2-hint" key={note}>
                {note}
              </p>
            ))}
            <details>
              <summary>Справочник билетов</summary>
              <ResearchTable
                rows={data.fares}
                columns={[
                  ['raw_name', 'Исходное название'],
                  ['category', 'Категория'],
                  ['decoded_name', 'Расшифровка'],
                  ['evidence_status', 'Статус расшифровки'],
                ]}
              />
              {meta && <Export name="fares" rows={data.fares} meta={meta} />}
            </details>
            <details>
              <summary>Источники исследования</summary>
              {data.sources.map((source, index) => (
                <p className="v2-hint" key={index}>
                  {typeof source.url === 'string' && /^https?:\/\//.test(source.url) ? (
                    <a href={source.url} target="_blank" rel="noreferrer">
                      {String(source.use)}
                    </a>
                  ) : (
                    String(source.use)
                  )}
                </p>
              ))}
            </details>
            <p className="v2-hint">{data.attribution}</p>
          </>
        ))}
    </details>
  );
}

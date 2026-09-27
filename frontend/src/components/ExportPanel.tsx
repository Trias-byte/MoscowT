import { useState } from 'react';
import { usePlatform } from '../lib/usePlatform';
import { downloadUrl, Job, platform } from '../lib/platform';
import type { Scope } from '../lib/contracts';
import { Modal } from './Dialogs';
import { ids } from './PlatformFields';

export function ExportPanel({
  scope,
  timeRange,
  onClose,
  initialForecastId,
}: {
  scope: Scope;
  timeRange: Scope['timeRange'];
  onClose: () => void;
  initialForecastId?: string | null;
}) {
  const { data, jobs } = usePlatform();
  const [forecastId, setForecastId] = useState<string | null>(null),
    [scenarioId, setScenarioId] = useState(scope.scenarioId || '');
  const [routes, setRoutes] = useState(scope.routeIds.join(',')),
    [period, setPeriod] = useState(timeRange);
  const [grain, setGrain] = useState('hour'),
    [metric, setMetric] = useState('route'),
    [objects, setObjects] = useState('');
  const [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [jobId, setJobId] = useState('');
  const chosenId =
    forecastId ?? initialForecastId ?? data.data?.capabilities.current_snapshot.forecastId ?? '';
  const run = data.data?.forecasts.find((f) => f.id === chosenId);
  const job = jobs.data?.find((j) => j.id === jobId);
  const completed =
    jobs.data?.filter(
      (j) => j.kind === 'scenario_run' && j.status === 'ready' && typeof j.result?.id === 'string',
    ) || [];
  async function create(path: string, payload: unknown) {
    setBusy(true);
    setError('');
    try {
      const j = await platform(path, Job, payload);
      setJobId(j.id);
      await jobs.refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Экспорт данных" onClose={onClose}>
      <div className="platform-body export-body">
        <p>
          Единая выгрузка выбранного периода. Базовый и завершённый сценарный прогноз используют тот
          же расчёт, что карта.
        </p>
        <label>
          Выпуск прогноза
          <select
            value={chosenId}
            onChange={(e) => {
              setForecastId(e.target.value);
              setScenarioId('');
            }}
          >
            <option value="">История</option>
            {data.data?.forecasts.map((f) => (
              <option key={f.id} value={f.id}>
                {f.spec.time_range.start.slice(0, 10)}—{f.spec.time_range.end.slice(0, 10)} ·{' '}
                {f.id.slice(-8)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Результат
          <select value={scenarioId} onChange={(e) => setScenarioId(e.target.value)}>
            <option value="">Базовый выпуск</option>
            {scope.scenarioId && !completed.some((j) => j.result?.id === scope.scenarioId) && (
              <option value={scope.scenarioId}>Сценарий на карте</option>
            )}
            {completed
              .filter(
                (j) =>
                  (j.result?.spec as { forecast_id?: string } | undefined)?.forecast_id ===
                  chosenId,
              )
              .map((j) => (
                <option key={j.id} value={String(j.result!.id)}>
                  {String((j.result!.spec as { name?: string }).name || 'Сценарий')} ·{' '}
                  {String(j.result!.id).slice(-8)}
                </option>
              ))}
          </select>
        </label>
        <label>
          Маршруты
          <input value={routes} onChange={(e) => setRoutes(e.target.value)} />
        </label>
        <fieldset>
          <legend>Период · МСК · конец не включён</legend>
          {(['start', 'end'] as const).map((k) => (
            <input
              key={k}
              aria-label={`Экспорт: ${k}`}
              type="datetime-local"
              step={3600}
              value={period[k].slice(0, 16)}
              onChange={(e) => setPeriod({ ...period, [k]: e.target.value + ':00+03:00' })}
            />
          ))}
        </fieldset>
        <label>
          Шаг выгрузки
          <select value={grain} onChange={(e) => setGrain(e.target.value)}>
            <option value="hour">Часы</option>
            <option value="day">Сутки</option>
            <option value="month">Месяцы</option>
          </select>
        </label>
        <label>
          Детализация выгрузки
          <select value={metric} onChange={(e) => setMetric(e.target.value)}>
            <option value="route">Маршрутные валидации</option>
            <option value="stop">Посадки по остановкам · сценарий</option>
            <option value="segment">Поток через участки · сценарий</option>
          </select>
        </label>
        {metric !== 'route' && (
          <label>
            ID объектов, пусто — все
            <input value={objects} onChange={(e) => setObjects(e.target.value)} />
          </label>
        )}
        {['csv', 'parquet'].map((format) => (
          <button
            key={format}
            disabled={busy || !data.data}
            onClick={() =>
              create('/exports', {
                dataset_id: run?.spec.dataset_id || data.data?.datasets.current_id,
                forecast_id: chosenId || null,
                route_ids: ids(routes),
                time_range: period,
                mode: scenarioId ? 'forecast' : scope.mode,
                grain,
                format,
                metric_scope: metric,
                object_ids: ids(objects),
                snapshot_id: scope.snapshotId,
                scenario_id: scenarioId || null,
              })
            }
          >
            {format === 'csv' ? 'Подготовить CSV' : 'Parquet + manifest'}
          </button>
        ))}
        <details>
          <summary>Полная поставка и конкурсный профиль</summary>
          <button disabled={busy} onClick={() => create('/bundles', {})}>
            Полный архив артефактов
          </button>
          <button
            disabled={busy || !chosenId || !!scenarioId}
            onClick={() => create('/competition-exports', { forecast_id: chosenId })}
          >
            Конкурсный CSV
          </button>
          <p>
            Конкурсный профиль использует исходные маршруты, шаблон и cutoff. Сценарии в него не
            включаются.
          </p>
        </details>
        {(error || job?.error || data.error) && (
          <p role="alert" className="platform-error">
            {error || job?.error || data.error?.message}
          </p>
        )}
        {job && (
          <p role="status">
            {job.status === 'ready'
              ? 'Файл готов'
              : job.status === 'failed'
                ? 'Ошибка выгрузки'
                : 'Выгрузка: ' + job.status}
          </p>
        )}
        {job?.status === 'ready' && (
          <a className="v2-primary" href={downloadUrl(job.id)}>
            Скачать файл
          </a>
        )}
      </div>
    </Modal>
  );
}

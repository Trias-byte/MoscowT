import { useEffect, useRef, useState } from 'react';
import { Job, Loose, platform, upload } from '../lib/platform';
import { usePlatform } from '../lib/usePlatform';
import { dates, ids, JobsPanel, Period } from './PlatformFields';
import { ModelControls } from './ModelControls';
import { ExternalEvidence } from './ExternalEvidence';
import { PassengerPassport } from './Passengers';

export function PlatformPanel({
  onPublished,
  onUploadRoute,
  onHelp,
  open = true,
  onClose,
}: {
  onPublished?: () => void;
  onUploadRoute?: () => void;
  onHelp?: () => void;
  open?: boolean;
  onClose?: () => void;
}) {
  const { data, jobs, refresh } = usePlatform();
  const [datasetId, setDatasetId] = useState(''),
    [routes, setRoutes] = useState('');
  const [name, setName] = useState('История валидаций'),
    [newDataset, setNewDataset] = useState(false);
  const [period, setPeriod] = useState<[string, string]>(['', '']);
  const [completedUpdate, setCompletedUpdate] = useState<{
    model_id?: string;
    forecast_id?: string;
  }>();
  const seenJobs = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (!jobs.data) return;
    const finished = jobs.data.filter((j) => ['ready', 'failed', 'cancelled'].includes(j.status));
    if (!seenJobs.current) {
      seenJobs.current = new Set(finished.map((j) => j.id));
      return;
    }
    for (const job of [...finished].reverse()) {
      if (seenJobs.current.has(job.id)) continue;
      seenJobs.current.add(job.id);
      const result = job.result || job.update;
      if (
        ['import_apply', 'model_refresh'].includes(job.kind) &&
        typeof result?.dataset_id === 'string'
      )
        setDatasetId(result.dataset_id);
      if (job.kind === 'model_refresh' && job.status === 'ready')
        setCompletedUpdate({
          model_id: typeof result?.model_id === 'string' ? result.model_id : undefined,
          forecast_id: typeof result?.forecast_id === 'string' ? result.forecast_id : undefined,
        });
    }
  }, [jobs.data]);
  const [kind, setKind] = useState('labels'),
    [mode, setMode] = useState('append'),
    [complete, setComplete] = useState(false);
  const [error, setError] = useState(''),
    [busy, setBusy] = useState(false),
    [routeId, setRouteId] = useState('');
  const [scheduleId, setScheduleId] = useState(''),
    [reuse, setReuse] = useState(false);
  const selected = data.data?.datasets.versions.find((d) => d.id === datasetId);
  useEffect(() => {
    if (!datasetId && data.data?.datasets.versions.length)
      setDatasetId(data.data.datasets.current_id || data.data.datasets.versions[0].id);
  }, [data.data, datasetId]);
  useEffect(() => {
    if (selected) {
      setRoutes(selected.routes.join(','));
      setName(selected.name || 'История валидаций');
      const start = (selected.forecast_origin || selected.end).slice(0, 10);
      const end = new Date(`${start}T00:00:00Z`);
      end.setUTCMonth(end.getUTCMonth() + 1);
      setPeriod([start, end.toISOString().slice(0, 10)]);
    }
  }, [selected]);
  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await fn();
      await refresh();
      await jobs.refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <aside hidden={!open} className="platform-panel expanded" aria-label="Данные и модели">
      <div className="tool-heading">
        <h2>Данные и модели</h2>
        <button onClick={onClose} aria-label="Закрыть данные и модели">
          ×
        </button>
      </div>
      <div className="platform-body">
        {(error || data.error) && (
          <p role="alert" className="platform-error">
            {error || data.error?.message}
          </p>
        )}
        <details open>
          <summary>1. Наборы данных</summary>
          <p>
            Редакция — сохранённое состояние набора после загрузки или исправления. Обучение
            использует выбранную редакцию; выбор не меняет карту.
          </p>
          <label>
            Набор и редакция
            <select value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
              <option value="">Выберите набор</option>
              {data.data?.datasets.versions.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name || 'История валидаций'} · ред. {d.revision || 1} · {d.start.slice(0, 10)}—
                  {d.end.slice(0, 10)} · {d.id.slice(-6)}
                </option>
              ))}
            </select>
          </label>
          {selected && (
            <div className="dataset-card">
              <b>
                {selected.routes.length} маршрутов · {selected.known_rows.toLocaleString('ru')} /{' '}
                {selected.rows.toLocaleString('ru')} известных часов
              </b>
              <p>
                {selected.total.toLocaleString('ru')} валидаций. Загрузка:{' '}
                {selected.created_at
                  ? new Date(selected.created_at).toLocaleString('ru')
                  : 'Существующий архив'}
              </p>
              <p>
                Последние факты:{' '}
                {selected.last_observation
                  ? new Date(selected.last_observation).toLocaleString('ru', {
                      timeZone: 'Europe/Moscow',
                    })
                  : 'нет'}{' '}
                · МСК.
              </p>
              {selected.route_coverage && (
                <details>
                  <summary>Покрытие маршрутов</summary>
                  {Object.entries(selected.route_coverage).map(([route, coverage]) => (
                    <p key={route}>
                      № {route}: {coverage.known_hours} известных часов, {coverage.missing_hours}{' '}
                      пропусков; последние факты{' '}
                      {coverage.last_observation
                        ? new Date(coverage.last_observation).toLocaleString('ru', {
                            timeZone: 'Europe/Moscow',
                          })
                        : 'отсутствуют'}
                      .
                    </p>
                  ))}
                </details>
              )}
              <button
                disabled={busy}
                onClick={() => act(() => platform(`/datasets/${datasetId}/refresh-model`, Job, {}))}
              >
                Обновить модель и прогноз
              </button>
              <details>
                <summary>Исходные файлы и происхождение</summary>
                <pre>{JSON.stringify(selected.imports, null, 2)}</pre>
              </details>
              <button
                disabled={busy}
                onClick={() => act(() => platform(`/datasets/${datasetId}/activate`, Loose, {}))}
              >
                Использовать по умолчанию для новых импортов
              </button>
            </div>
          )}
          <label>
            Маршруты через запятую
            <input value={routes} onChange={(e) => setRoutes(e.target.value)} />
          </label>
          <label>
            Действие импорта
            <select
              value={newDataset ? 'new' : 'revision'}
              onChange={(e) => setNewDataset(e.target.value === 'new')}
            >
              <option value="revision">Дополнить / исправить выбранный набор</option>
              <option value="new">Создать самостоятельный набор</option>
            </select>
          </label>
          <label>
            Название набора
            <input value={name} onChange={(e) => setName(e.target.value)} maxLength={160} />
          </label>
          <Period label="Период импорта" value={period} onChange={setPeriod} />
          <label>
            Тип истории
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="labels">Почасовые labels</option>
              <option value="events">События валидаторов</option>
            </select>
          </label>
          <label>
            Режим импорта
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="append">Добавить</option>
              <option value="upsert">Исправить переданные часы</option>
              <option value="replace">Заменить период выбранных маршрутов</option>
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={complete}
              onChange={(e) => setComplete(e.target.checked)}
            />{' '}
            Источник полный: отсутствующие часы означают ноль
          </label>
          <input
            aria-label="Файл истории"
            type="file"
            accept=".csv"
            disabled={busy || (!newDataset && !datasetId)}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file)
                act(async () =>
                  platform('/uploads', Job, {
                    blob_id: await upload(file),
                    spec: {
                      kind,
                      mode,
                      time_range: dates(...period),
                      route_ids: ids(routes),
                      complete,
                      dataset_name: name,
                      new_dataset: newDataset,
                      base_dataset_id: newDataset ? null : datasetId,
                    },
                  }),
                );
            }}
          />
          <p>
            CSV: route;date;hour;boardings. Сначала проверка в заданиях, затем применение импорта.
          </p>
          <details>
            <summary>Маршруты и геометрия</summary>
            <input
              aria-label="ID нового маршрута"
              value={routeId}
              onChange={(e) => setRouteId(e.target.value)}
              placeholder="ID маршрута"
            />
            <button
              disabled={busy || !routeId}
              onClick={() =>
                act(() =>
                  platform('/routes', Loose, {
                    route: { id: routeId, number: routeId, name: `Маршрут ${routeId}` },
                  }),
                )
              }
            >
              Зарегистрировать маршрут
            </button>
            <button onClick={onUploadRoute} disabled={!onUploadRoute}>
              Загрузить маршрут
            </button>
            <p>Новый маршрут требует истории и переобучения. Геометрия загружается отдельно.</p>
          </details>
          <details>
            <summary>Расписания</summary>
            <input
              aria-label="Файл расписания"
              type="file"
              accept=".csv"
              disabled={busy}
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file)
                  act(async () => platform('/schedules', Job, { blob_id: await upload(file) }));
              }}
            />
            <label>
              Расписание для публикации
              <select value={scheduleId} onChange={(e) => setScheduleId(e.target.value)}>
                <option value="">Оценка по имеющейся истории</option>
                {data.data?.schedules.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.valid_from}—{s.valid_to} · {s.method}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <input type="checkbox" checked={reuse} onChange={(e) => setReuse(e.target.checked)} />{' '}
              Явно перенести недельный график на другой период как сценарий
            </label>
            <p>CSV сентября 2026 года не является расписанием 2025 года.</p>
          </details>
        </details>
        {data.data && (
          <ModelControls
            data={data.data}
            datasetId={datasetId}
            routeIds={ids(routes)}
            busy={busy}
            act={act}
            scheduleId={scheduleId}
            reuse={reuse}
            onPublished={onPublished}
            completedUpdate={completedUpdate}
          />
        )}
        {data.data && <ExternalEvidence external={data.data.external} />}
        <PassengerPassport />
        <button onClick={onHelp} disabled={!onHelp}>
          О данных и ограничениях
        </button>
        <JobsPanel jobs={jobs.data || []} act={act} />
      </div>
    </aside>
  );
}

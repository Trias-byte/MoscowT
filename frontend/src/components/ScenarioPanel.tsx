import { useEffect, useState } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { z } from 'zod';
import { Job, Loose, platform } from '../lib/platform';
import { usePlatform } from '../lib/usePlatform';
import {
  emptyScenario,
  scenarioInput,
  scenarioModes,
  stable,
  type ScenarioMode,
  type ScenarioInput,
  type ScenarioWorkspace,
  type Incident,
  type ScenarioDraft,
} from '../lib/scenario';
import { ids, JobsPanel } from './PlatformFields';
import { Chart } from './Chart';
const Result = z.object({
  id: z.string(),
  base_total: z.number(),
  total: z.number(),
  base_vehicle_hours: z.number().nullable(),
  vehicle_hours: z.number().nullable(),
  warnings: z.array(z.string()),
  sensitivity: z.record(z.string(), z.array(z.object({ x: z.number(), value: z.number() }))),
  spec: Loose,
});
const number = (v: number | null) =>
  v === null ? 'Нет данных' : v.toLocaleString('ru', { maximumFractionDigits: 1 });
const labels = {
  temperature_2m: 'Температура, °C',
  relative_humidity_2m: 'Влажность, %',
  precipitation: 'Осадки, мм/ч',
  service_ratio: 'Частота относительно базы',
  season: 'Дополнительная сезонная поправка',
};
export function ScenarioPanel({
  open,
  workspace,
  placing,
  setPlacing,
  onApply,
  onClose,
  snapshotId,
  activeForecastId,
  selectedIncident,
  selectIncident,
}: {
  open: boolean;
  workspace: ScenarioWorkspace;
  placing: boolean;
  setPlacing: (v: boolean) => void;
  onApply: (id: string, spec?: ScenarioInput, snapshotId?: string) => void;
  onClose: () => void;
  snapshotId: string;
  activeForecastId?: string | null;
  selectedIncident: string | null;
  selectIncident: (id: string | null) => void;
}) {
  const { data, jobs } = usePlatform();
  const [error, setError] = useState(''),
    [busy, setBusy] = useState(false);
  const { record, setDraft } = workspace;
  const { draft, mode, runs } = record;
  const modes = Object.keys(scenarioModes) as ScenarioMode[];
  const input = scenarioInput(draft, mode);
  const signature = stable(input);
  const pending = runs[mode]?.job;
  const results = useQueries({
    queries: modes.map((m) => ({
      queryKey: ['scenario-result', record.id, m, runs[m]?.resultId],
      enabled: !!runs[m]?.resultId,
      queryFn: () => platform(`/scenarios/${runs[m]!.resultId}`, Result),
    })),
  });
  const result = results[modes.indexOf(mode)];
  const matchesResult = runs[mode]?.signature === signature;
  useEffect(() => {
    if (!draft.forecast_id && data.data) {
      const run =
        data.data.forecasts.find((f) => f.id === activeForecastId) || data.data.forecasts[0];
      if (run)
        setDraft({
          ...draft,
          forecast_id: run.id,
          route_ids: run.spec.route_ids,
          time_range: {
            start: run.spec.time_range.start,
            end:
              new Date(
                Math.min(
                  Date.parse(run.spec.time_range.end),
                  Date.parse(run.spec.time_range.start) + 86400000,
                ),
              )
                .toLocaleString('sv-SE', { timeZone: 'Europe/Moscow' })
                .replace(' ', 'T') + '+03:00',
          },
          schedule: {
            ...draft.schedule,
            base_schedule_id: data.data.capabilities.current_snapshot.scheduleId || null,
          },
        });
    }
  }, [data.data, draft, setDraft, activeForecastId]);
  const missingJobs = workspace.records.flatMap((saved) =>
    Object.values(saved.runs).flatMap((run) =>
      run.job && !jobs.data?.some((j) => j.id === run.job) ? [run.job] : [],
    ),
  );
  const recoveredJobs = useQueries({
    queries: [...new Set(missingJobs)].map((id) => ({
      queryKey: ['scenario-job', id],
      queryFn: () => platform(`/jobs/${id}`, Job),
      refetchInterval: 2000,
    })),
  });
  useEffect(() => {
    for (const saved of workspace.records) {
      for (const [key, run] of Object.entries(saved.runs)) {
        if (!run.job) continue;
        const job =
          jobs.data?.find((j) => j.id === run.job) ||
          recoveredJobs.find((q) => q.data?.id === run.job)?.data;
        if (job?.status === 'ready' && typeof job.result?.id === 'string')
          workspace.setRun(
            saved.id,
            key as ScenarioMode,
            { signature: run.signature, resultId: job.result.id },
            run.job,
          );
        else if (job && ['failed', 'cancelled'].includes(job.status))
          workspace.setRun(
            saved.id,
            key as ScenarioMode,
            { signature: run.signature, error: job.error || 'Расчёт отменён' },
            run.job,
          );
      }
    }
  }, [jobs.data, recoveredJobs, workspace]);
  async function calculate(selectedModes: ScenarioMode[]) {
    const id = record.id;
    for (const selectedMode of selectedModes) {
      if (runs[selectedMode]?.job) continue;
      const spec = scenarioInput(draft, selectedMode);
      try {
        const saved = await platform('/scenarios', z.object({ id: z.string() }), spec);
        const job = await platform(`/scenarios/${saved.id}/runs`, Job, {});
        workspace.setRun(id, selectedMode, { job: job.id, signature: stable(spec) });
      } catch (error) {
        workspace.setRun(id, selectedMode, {
          signature: stable(spec),
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
  }
  async function applyResult(selectedMode: ScenarioMode) {
    const ready = results[modes.indexOf(selectedMode)].data;
    if (!ready) return;
    const spec = ready.spec as unknown as ScenarioInput;
    let targetSnapshot = snapshotId;
    if (spec.forecast_id !== activeForecastId) {
      const map = await platform('/forecast-map-views', z.object({ snapshotId: z.string() }), {
        forecast_id: spec.forecast_id,
        snapshot_id: snapshotId,
      });
      targetSnapshot = map.snapshotId;
    }
    onApply(ready.id, spec, targetSnapshot);
  }
  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError('');
    try {
      await fn();
      await jobs.refetch();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  const patch = (v: Partial<ScenarioDraft>) => setDraft({ ...draft, ...v });
  const schedule = (v: Partial<ScenarioDraft['schedule']>) =>
    patch({ schedule: { ...draft.schedule, ...v } });
  const editIncident = (id: string, v: Partial<Incident>) =>
    patch({ incidents: draft.incidents.map((i) => (i.id === id ? { ...i, ...v } : i)) });
  const forecast = data.data?.forecasts.find((f) => f.id === draft.forecast_id);
  const model = data.data?.models.find((m) => m.id === forecast?.spec.model_id);
  const supportsWeather =
    !!model?.spec.weather_hourly_id && model.spec.feature_groups?.includes('weather');
  const weatherProfile = useQuery({
    queryKey: ['weather-profile', draft.forecast_id, draft.time_range],
    enabled: open && !!supportsWeather && !!draft.forecast_id,
    queryFn: () =>
      platform(
        `/forecast-runs/${draft.forecast_id}/weather-profile`,
        z.object({
          method: z.string(),
          fields: z.record(
            z.string(),
            z.object({ min: z.number(), max: z.number(), mean: z.number() }),
          ),
        }),
        draft.time_range,
      ),
  });
  const incident = draft.incidents.find((i) => i.id === selectedIncident);
  const candidates = useQuery({
    queryKey: [
      'incident-candidates',
      incident?.longitude,
      incident?.latitude,
      incident?.start,
      snapshotId,
    ],
    enabled: !!incident,
    queryFn: () =>
      platform(
        `/accident-candidates?longitude=${incident!.longitude}&latitude=${incident!.latitude}&date=${incident!.start.slice(0, 10)}&snapshot_id=${snapshotId}`,
        z.object({ routes: z.array(z.object({ route_id: z.string(), distance_m: z.number() })) }),
      ),
  });
  return (
    <aside hidden={!open} className="platform-panel scenario-panel expanded" aria-label="Сценарии">
      <div className="tool-heading">
        <h2>Сценарии</h2>
        <button aria-label="Закрыть сценарии" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="platform-body">
        <p>Измените условия и запустите расчёт. Исходный прогноз сохранится. Время — московское.</p>
        {(error || runs[mode]?.error || data.error || result.error) && (
          <p role="alert" className="platform-error">
            {error || runs[mode]?.error || data.error?.message || result.error?.message}
          </p>
        )}
        <label>
          Сохранённый сценарий
          <select
            value={record.id}
            onChange={(e) => {
              workspace.select(e.target.value);
              setPlacing(false);
              selectIncident(null);
              setError('');
            }}
          >
            {workspace.records.map((r) => (
              <option key={r.id} value={r.id}>
                {r.draft.name}
              </option>
            ))}
          </select>
        </label>
        <button
          onClick={() => {
            workspace.create();
            setPlacing(false);
            selectIncident(null);
          }}
        >
          Создать копию сценария
        </button>
        <label>
          Режим сценария
          <select
            value={mode}
            onChange={(e) => {
              workspace.setMode(e.target.value as ScenarioMode);
              setPlacing(false);
            }}
          >
            {modes.map((m) => (
              <option key={m} value={m}>
                {scenarioModes[m]}
              </option>
            ))}
          </select>
        </label>
        <p>
          Каждый режим рассчитывается отдельно относительно одной базы. В совместном режиме
          учитываются все заданные условия.
        </p>
        <label>
          Название сценария
          <input value={draft.name} onChange={(e) => patch({ name: e.target.value })} />
        </label>
        <label>
          Базовый выпуск
          <select
            value={draft.forecast_id}
            onChange={(e) => {
              const run = data.data?.forecasts.find((f) => f.id === e.target.value);
              if (run)
                setDraft({
                  ...structuredClone(emptyScenario),
                  name: draft.name,
                  forecast_id: run.id,
                  route_ids: run.spec.route_ids,
                  time_range: run.spec.time_range,
                });
            }}
          >
            <option value="">Выберите прогноз</option>
            {data.data?.forecasts.map((f) => (
              <option key={f.id} value={f.id}>
                {f.spec.time_range.start.slice(0, 10)}—{f.spec.time_range.end.slice(0, 10)} ·{' '}
                {f.id.slice(-8)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Маршруты сценария
          <RouteInput value={draft.route_ids} onChange={(route_ids) => patch({ route_ids })} />
        </label>
        <fieldset>
          <legend>Период сценария · конец не включён</legend>
          {(['start', 'end'] as const).map((k) => (
            <label key={k}>
              {k === 'start' ? 'Начало' : 'Конец'}
              <input
                type="datetime-local"
                step={3600}
                value={draft.time_range[k].slice(0, 16)}
                onChange={(e) =>
                  patch({ time_range: { ...draft.time_range, [k]: e.target.value + ':00+03:00' } })
                }
              />
            </label>
          ))}
        </fieldset>
        <details open hidden={mode !== 'combined' && mode !== 'weather'}>
          <summary>Погода</summary>
          <p>
            Пустое поле сохраняет погодный профиль базового выпуска. Изменения поступают в модель.
          </p>
          {weatherProfile.data && (
            <p>
              {weatherProfile.data.method === 'available_forecast_release'
                ? 'Основа: сохранённый погодный выпуск.'
                : 'Основа: почасовой профиль прошлых лет, сценарий погоды.'}{' '}
              Ниже показано среднее за выбранные часы.
            </p>
          )}
          {weatherProfile.error && <p role="alert">{weatherProfile.error.message}</p>}
          {!supportsWeather && (
            <p className="scenario-note">
              Для этих настроек выберите ансамбль, обученный с почасовой температурой, влажностью и
              осадками.
            </p>
          )}
          {(Object.keys(draft.weather) as (keyof ScenarioDraft['weather'])[]).map((k) => (
            <label key={k}>
              {labels[k]}
              <input
                type="number"
                disabled={!supportsWeather}
                min={k === 'temperature_2m' ? -60 : 0}
                max={k === 'temperature_2m' ? 60 : k === 'precipitation' ? 200 : 100}
                step={k === 'precipitation' ? 0.1 : 1}
                placeholder="Из базового выпуска"
                value={draft.weather[k] ?? ''}
                onChange={(e) =>
                  patch({
                    weather: {
                      ...draft.weather,
                      [k]: e.target.value === '' ? null : Number(e.target.value),
                    },
                  })
                }
              />
              {weatherProfile.data?.fields[k] && (
                <small>
                  База: {number(weatherProfile.data.fields[k].mean)} (
                  {number(weatherProfile.data.fields[k].min)}…
                  {number(weatherProfile.data.fields[k].max)}). Сценарий:{' '}
                  {draft.weather[k] === null ? 'исходный профиль' : number(draft.weather[k])}.
                  {draft.weather[k] !== null && (
                    <>
                      {' '}
                      Разница со средним:{' '}
                      {number(draft.weather[k] - weatherProfile.data.fields[k].mean)}.
                    </>
                  )}
                </small>
              )}
            </label>
          ))}
          {Boolean(model?.feature_ranges) && (
            <details>
              <summary>Диапазоны обучения</summary>
              <pre>{JSON.stringify(model?.feature_ranges, null, 2)}</pre>
            </details>
          )}
        </details>
        <details open hidden={mode !== 'combined' && mode !== 'season'}>
          <summary>Сезонность</summary>
          <p>
            Календарь, день недели и час уже входят в базовый прогноз. Месячный рисунок зависит от
            доступной истории; неполный год не подтверждает годовую сезонность.
          </p>
          <label>
            Дополнительная сезонная поправка × {draft.coefficients.season.toFixed(2)}
            <input
              aria-label="Сезонная поправка"
              type="range"
              min="0"
              max="2"
              step="0.05"
              value={draft.coefficients.season}
              onChange={(e) =>
                patch({ coefficients: { ...draft.coefficients, season: Number(e.target.value) } })
              }
            />
          </label>
          <p>
            1 — без изменения; 1,10 — +10%; 0,85 — −15%. Это дополнительное допущение, а не
            повторный календарный эффект.
          </p>
        </details>
        <details open hidden={mode !== 'combined' && mode !== 'incidents'}>
          <summary>ДТП на карте ({draft.incidents.length})</summary>
          <p>
            Поставьте точку и подтвердите затронутые маршруты. Близость к линии не означает
            блокировку.
          </p>
          <button aria-pressed={placing} onClick={() => setPlacing(!placing)}>
            {placing ? 'Отменить добавление ДТП' : 'Добавить ДТП на карте'}
          </button>
          {placing && (
            <p role="status">
              Нажмите на карту в месте ДТП. На телефоне панель временно скрывается.
            </p>
          )}
          {draft.incidents.map((i) => (
            <div className="incident-item" key={i.id}>
              <button onClick={() => selectIncident(i.id)}>
                ДТП · {i.start.slice(11, 16)} · № {i.route_ids.join(', ') || 'выберите маршруты'}
              </button>
              <button
                aria-label="Удалить ДТП"
                onClick={() => patch({ incidents: draft.incidents.filter((x) => x.id !== i.id) })}
              >
                Удалить
              </button>
            </div>
          ))}
          {incident && (
            <div className="incident-editor">
              <p>
                Координаты {incident.latitude.toFixed(5)}, {incident.longitude.toFixed(5)}. Маркер
                можно перетащить.
              </p>
              <label>
                Начало ДТП
                <input
                  type="datetime-local"
                  value={incident.start.slice(0, 16)}
                  onChange={(e) =>
                    editIncident(incident.id, { start: e.target.value + ':00+03:00' })
                  }
                />
              </label>
              <label>
                Длительность, минут
                <input
                  type="number"
                  min="1"
                  max="10080"
                  value={incident.duration_minutes}
                  onChange={(e) =>
                    editIncident(incident.id, { duration_minutes: Number(e.target.value) })
                  }
                />
              </label>
              <label>
                Снижение движения, %
                <input
                  type="number"
                  min="0"
                  max="100"
                  value={incident.reduction * 100}
                  onChange={(e) =>
                    editIncident(incident.id, { reduction: Number(e.target.value) / 100 })
                  }
                />
              </label>
              <label>
                Подтверждённые пользователем маршруты
                <RouteInput
                  value={incident.route_ids}
                  onChange={(route_ids) => editIncident(incident.id, { route_ids })}
                />
              </label>
              <p>
                Кандидаты в радиусе 100 м:{' '}
                {candidates.data?.routes
                  .map((r) => `№ ${r.route_id} (${r.distance_m} м)`)
                  .join(', ') || 'не найдены'}
                . Укажите маршруты выше после проверки.
              </p>
              <p>
                60 минут и 50% — начальные допущения. Эти ДТП не добавляются в обучающую историю.
              </p>
            </div>
          )}
        </details>
        <details hidden={mode !== 'combined' && mode !== 'schedule'}>
          <summary>Расписание и бюджет</summary>
          <label>
            Исходное расписание
            <select
              value={draft.schedule.base_schedule_id || ''}
              onChange={(e) => schedule({ base_schedule_id: e.target.value || null })}
            >
              <option value="">Без полного архива</option>
              {data.data?.schedules.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.valid_from}—{s.valid_to} · {s.method}
                </option>
              ))}
            </select>
          </label>
          {!draft.schedule.base_schedule_id && (
            <label>
              Исходный интервал, минут · допущение
              <input
                type="number"
                min="1"
                max="240"
                placeholder="Например, 10"
                value={draft.schedule.base_headway_minutes ?? ''}
                onChange={(e) =>
                  schedule({ base_headway_minutes: e.target.value ? Number(e.target.value) : null })
                }
              />
            </label>
          )}
          <label>
            Альтернативное расписание
            <select
              value={draft.schedule.schedule_id || ''}
              onChange={(e) =>
                schedule({ schedule_id: e.target.value || null, headway_minutes: null })
              }
            >
              <option value="">Изменить интервал вручную</option>
              {data.data?.schedules.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.valid_from}—{s.valid_to} · {s.method}
                </option>
              ))}
            </select>
          </label>
          {!draft.schedule.schedule_id && (
            <label>
              Новый интервал, минут
              <input
                type="number"
                min="1"
                max="240"
                value={draft.schedule.headway_minutes ?? ''}
                placeholder="Без изменения"
                onChange={(e) =>
                  schedule({ headway_minutes: e.target.value ? Number(e.target.value) : null })
                }
              />
            </label>
          )}
          <label>
            <input
              type="checkbox"
              checked={draft.schedule.allow_period_reuse}
              onChange={(e) => schedule({ allow_period_reuse: e.target.checked })}
            />{' '}
            Перенести недельное расписание на другой период как допущение
          </label>
          <fieldset>
            <legend>Часы работы · пусто сохраняет исходные</legend>
            {(['service_start_minute', 'service_end_minute'] as const).map((k) => (
              <label key={k}>
                {k === 'service_start_minute' ? 'От' : 'До'}
                <input
                  type="time"
                  value={
                    draft.schedule[k] === null
                      ? ''
                      : `${String(Math.floor(draft.schedule[k]! / 60)).padStart(2, '0')}:${String(draft.schedule[k]! % 60).padStart(2, '0')}`
                  }
                  onChange={(e) => {
                    const [h, m] = e.target.value.split(':').map(Number);
                    schedule({ [k]: e.target.value ? h * 60 + m : null });
                  }}
                />
              </label>
            ))}
          </fieldset>
          <label>
            Чувствительность спроса ε = {draft.schedule.elasticity.toFixed(2)}
            <input
              type="range"
              min="0"
              max="1"
              step="0.05"
              value={draft.schedule.elasticity}
              onChange={(e) => schedule({ elasticity: Number(e.target.value) })}
            />
          </label>
          <p>
            Q = Qбазы × (f / fбазы)<sup>ε</sup>. ε = 0,3 — демонстрационное допущение. Полная
            остановка даёт нулевое обслуживание. Перенос спроса на соседние маршруты не
            моделируется.
          </p>
          <details>
            <summary>Отдельные отправления</summary>
            {draft.schedule.departures.map((d, index) => (
              <div key={index}>
                <input
                  aria-label="Маршрут отправления"
                  value={d.route_id}
                  onChange={(e) =>
                    schedule({
                      departures: draft.schedule.departures.map((x, i) =>
                        i === index ? { ...x, route_id: e.target.value } : x,
                      ),
                    })
                  }
                />
                <input
                  aria-label="Время отправления"
                  type="datetime-local"
                  value={d.timestamp.slice(0, 16)}
                  onChange={(e) =>
                    schedule({
                      departures: draft.schedule.departures.map((x, i) =>
                        i === index ? { ...x, timestamp: e.target.value + ':00+03:00' } : x,
                      ),
                    })
                  }
                />
                <label>
                  Длительность рейса, минут
                  <input
                    type="number"
                    min="1"
                    max="1440"
                    value={d.duration_minutes}
                    onChange={(e) =>
                      schedule({
                        departures: draft.schedule.departures.map((x, i) =>
                          i === index ? { ...x, duration_minutes: Number(e.target.value) } : x,
                        ),
                      })
                    }
                  />
                </label>
                <select
                  aria-label="Изменение отправления"
                  value={d.change}
                  onChange={(e) =>
                    schedule({
                      departures: draft.schedule.departures.map((x, i) =>
                        i === index ? { ...x, change: Number(e.target.value) as 1 | -1 } : x,
                      ),
                    })
                  }
                >
                  <option value={1}>Добавить</option>
                  <option value={-1}>Убрать</option>
                </select>
                <button
                  onClick={() =>
                    schedule({
                      departures: draft.schedule.departures.filter((_, i) => i !== index),
                    })
                  }
                >
                  Убрать правку
                </button>
              </div>
            ))}
            <button
              onClick={() =>
                schedule({
                  departures: [
                    ...draft.schedule.departures,
                    {
                      route_id: draft.route_ids[0] || '',
                      timestamp: draft.time_range.start,
                      change: 1,
                      duration_minutes: 60,
                    },
                  ],
                })
              }
            >
              Изменить отправление
            </button>
          </details>
          <label>
            Дополнительные вагоно-часы на период
            <input
              type="number"
              min="0"
              value={draft.additional_vehicle_hours}
              onChange={(e) => patch({ additional_vehicle_hours: Number(e.target.value) })}
            />
          </label>
          <p>
            Добавочные часы распределяются равномерно. Загрузить альтернативный CSV можно в «Данных
            и моделях».
          </p>
        </details>
        <div className="scenario-run">
          <button
            className="v2-primary"
            disabled={busy || !!pending || !draft.forecast_id}
            onClick={() => act(() => calculate([mode]))}
          >
            {pending ? 'Сценарий рассчитывается…' : 'Рассчитать сценарий'}
          </button>
          <button
            disabled={busy || !draft.forecast_id || Object.values(runs).some((r) => r.job)}
            onClick={() => act(() => calculate(modes))}
          >
            Рассчитать все режимы отдельно
          </button>
          <button
            onClick={() => {
              setPlacing(false);
              onApply('');
            }}
          >
            Вернуться к базе
          </button>
        </div>
        {pending && (
          <p role="status">Задание сохранено. Можно закрыть панель или обновить страницу.</p>
        )}
        {modes.some((m) => runs[m]) && (
          <section aria-label="Результаты режимов">
            <h3>Результаты режимов</h3>
            <table>
              <thead>
                <tr>
                  <th>Режим</th>
                  <th>База</th>
                  <th>Результат / изменение</th>
                  <th>Состояние</th>
                </tr>
              </thead>
              <tbody>
                {modes.map((m, i) => {
                  const r = results[i].data,
                    run = runs[m];
                  const stale = run && run.signature !== stable(scenarioInput(draft, m));
                  return (
                    <tr key={m}>
                      <th>{scenarioModes[m]}</th>
                      <td>{r ? number(r.base_total) : '—'}</td>
                      <td>{r ? `${number(r.total)} / ${number(r.total - r.base_total)}` : '—'}</td>
                      <td>
                        {run?.job
                          ? 'Расчёт…'
                          : run?.error ||
                            results[i].error?.message ||
                            (stale ? 'Параметры изменены' : r ? 'Готово' : 'Не рассчитан')}
                        {r && (
                          <button
                            disabled={busy || !!stale || !!run?.job}
                            onClick={() => act(() => applyResult(m))}
                          >
                            На карту: {scenarioModes[m]}
                          </button>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        )}
        {result.data && (
          <section aria-label="Сравнение сценария">
            <h3>Сравнение</h3>
            {!matchesResult && (
              <p className="scenario-note" role="status">
                Параметры изменены. Ниже предыдущий результат; запустите расчёт заново.
              </p>
            )}
            <table>
              <thead>
                <tr>
                  <th>Показатель</th>
                  <th>База</th>
                  <th>Сценарий</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Валидации</td>
                  <td>{number(result.data.base_total)}</td>
                  <td>{number(result.data.total)}</td>
                </tr>
                <tr>
                  <td>Вагоно-часы</td>
                  <td>{number(result.data.base_vehicle_hours)}</td>
                  <td>{number(result.data.vehicle_hours)}</td>
                </tr>
                <tr>
                  <td>Валидации / вагоно-час</td>
                  <td>
                    {number(
                      result.data.base_vehicle_hours
                        ? result.data.base_total / result.data.base_vehicle_hours
                        : null,
                    )}
                  </td>
                  <td>
                    {number(
                      result.data.vehicle_hours
                        ? result.data.total / result.data.vehicle_hours
                        : null,
                    )}
                  </td>
                </tr>
              </tbody>
            </table>
            <p>
              Изменение:{' '}
              {result.data.base_total
                ? ((result.data.total / result.data.base_total - 1) * 100).toFixed(1) + '%'
                : 'нет относительной базы'}
              .
            </p>
            {result.data.warnings.map((w, i) => (
              <p className="scenario-note" key={i}>
                {w}
              </p>
            ))}
            <button
              disabled={busy || !matchesResult || !!pending}
              onClick={() => act(() => applyResult(mode))}
            >
              Показать результат на карте
            </button>
            <details>
              <summary>Чувствительность модели и допущений</summary>
              <p>
                Однофакторные отклики. Взаимодействия факторов не складываются; это не доказанная
                причинная зависимость. Остальные параметры зафиксированы на значениях сценария.
                Кривая расписания задаёт одинаковое отношение частот во всех выбранных часах,
                сохраняя ограничения ДТП.
              </p>
              {Object.entries(result.data.sensitivity).map(([key, points]) => (
                <div key={key}>
                  <h4>{labels[key as keyof typeof labels] || key}</h4>
                  <Chart
                    label={labels[key as keyof typeof labels] || key}
                    option={{
                      grid: { left: 60, right: 15, top: 20, bottom: 30 },
                      xAxis: { type: 'category', data: points.map((p) => p.x) },
                      yAxis: { type: 'value' },
                      series: [{ type: 'line', data: points.map((p) => p.value), smooth: false }],
                    }}
                  />
                </div>
              ))}
            </details>
          </section>
        )}
        <JobsPanel jobs={jobs.data || []} act={act} />
      </div>
    </aside>
  );
}

function RouteInput({
  value,
  onChange,
}: {
  value: string[];
  onChange: (routes: string[]) => void;
}) {
  const [text, setText] = useState(value.join(','));
  const canonical = value.join(',');
  useEffect(() => {
    setText((current) => (ids(current).join(',') === canonical ? current : canonical));
  }, [canonical]);
  return (
    <input
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onChange(ids(e.target.value));
      }}
    />
  );
}

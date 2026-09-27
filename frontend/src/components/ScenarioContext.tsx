import { useQuery } from '@tanstack/react-query';
import { z } from 'zod';
import { platform } from '../lib/platform';
import { contextSelection, factorNumber, factorRange } from '../lib/factorContext';
import type { ObjectSelection } from '../lib/contracts';
import type { ScenarioDraft } from '../lib/scenario';

const Stats = z.object({
  min: z.number().nullable(),
  max: z.number().nullable(),
  mean: z.number().nullable(),
  known_hours: z.number(),
  total_hours: z.number(),
});
const Context = z.object({
  route_ids: z.array(z.string()),
  note: z.string(),
  resolution: z.literal('route'),
  weather: z.object({
    method: z.string(),
    reason: z.string().optional(),
    fields: z.record(z.string(), Stats),
  }),
  schedule: z.object({
    method: z.string(),
    routes: z.array(z.object({ route_id: z.string(), departures: Stats, vehicle_hours: Stats })),
  }),
});
function Summary({ value, unit }: { value: z.infer<typeof Stats>; unit: string }) {
  const range = factorRange(value.min, value.max);
  return (
    <span>
      {factorNumber(value.mean)}
      {value.mean === null ? '' : ` ${unit}`}
      {range && value.min !== value.max ? ` (${range})` : ''}
      {` · известно ${value.known_hours} из ${value.total_hours} ч`}
    </span>
  );
}
export function ScenarioContext({
  draft,
  selected,
  routeId,
  snapshotId,
  open,
}: {
  draft: ScenarioDraft;
  selected: ObjectSelection;
  routeId: string;
  snapshotId: string;
  open: boolean;
}) {
  const object = contextSelection(selected, routeId, draft.route_ids);
  const body = {
    route_ids: draft.route_ids,
    time_range: draft.time_range,
    snapshot_id: snapshotId,
    object,
    schedule_id: draft.schedule.base_schedule_id,
    allow_period_reuse: draft.schedule.allow_period_reuse,
  };
  const context = useQuery({
    queryKey: ['scenario-context', draft.forecast_id, body],
    enabled:
      open &&
      !!draft.forecast_id &&
      draft.route_ids.length > 0 &&
      Date.parse(draft.time_range.end) > Date.parse(draft.time_range.start),
    queryFn: () => platform(`/forecast-runs/${draft.forecast_id}/factor-context`, Context, body),
  });
  return (
    <details className="scenario-context">
      <summary>Базовые условия выбранного объекта</summary>
      {!object && (
        <p role="status">
          Выбранный объект вне маршрутов сценария. Ниже — условия маршрутов сценария:{' '}
          {draft.route_ids.join(', ')}.
        </p>
      )}
      {context.error && <p role="alert">{context.error.message}</p>}
      {context.data && (
        <>
          <p>
            Маршруты: {context.data.route_ids.join(', ')}. {context.data.note}
          </p>
          {context.data.weather.reason && <p>{context.data.weather.reason}</p>}
          {Object.entries(context.data.weather.fields).map(([key, value]) => (
            <p key={key}>
              {(
                {
                  temperature_2m: 'Температура',
                  relative_humidity_2m: 'Влажность',
                  precipitation: 'Осадки',
                } as Record<string, string>
              )[key] || key}
              :{' '}
              <Summary
                value={value}
                unit={key === 'temperature_2m' ? '°C' : key === 'precipitation' ? 'мм/ч' : '%'}
              />
            </p>
          ))}
          {context.data.schedule.method === 'unavailable' && (
            <p>Исходное расписание не выбрано: частота и плановые вагоно-часы неизвестны.</p>
          )}
          {context.data.schedule.method === 'weekly_profile_reuse' && (
            <p>Недельный профиль перенесён на выбранный период как допущение.</p>
          )}
          {context.data.schedule.routes.map((row) => (
            <div key={row.route_id}>
              <p>
                Маршрут № {row.route_id}: отправления —{' '}
                <Summary value={row.departures} unit="рейсов/ч" />
              </p>
              <p>
                Плановый ресурс — <Summary value={row.vehicle_hours} unit="вагоно-ч/ч" />
              </p>
            </div>
          ))}
        </>
      )}
    </details>
  );
}

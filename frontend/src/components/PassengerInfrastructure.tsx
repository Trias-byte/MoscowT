import { useQuery } from '@tanstack/react-query';
import { platform } from '../lib/platform';
import { number } from '../lib/domain';
import {
  Infrastructure,
  parameters,
  POI_KINDS,
  downloadPassengerCsv,
  type PoiCollection,
  type PoiFeature,
} from '../lib/passengers';

export function InfrastructureControls(p: {
  enabled: boolean;
  onEnabled: (value: boolean) => void;
  radius: 500 | 1000;
  onRadius: (value: 500 | 1000) => void;
  category: string;
  onCategory: (value: string) => void;
  data?: PoiCollection;
  error?: Error | null;
  loading: boolean;
  onSelect: (feature: PoiFeature) => void;
}) {
  return (
    <section className="infrastructure-controls" aria-label="Слой инфраструктуры">
      <label className="v2-check">
        <input
          type="checkbox"
          checked={p.enabled}
          onChange={(e) => p.onEnabled(e.target.checked)}
        />
        Инфраструктура вокруг остановок
      </label>
      {p.enabled && (
        <>
          <label className="v2-field">
            Радиус инфраструктуры
            <select
              aria-label="Радиус инфраструктуры"
              value={p.radius}
              onChange={(e) => p.onRadius(Number(e.target.value) as 500 | 1000)}
            >
              <option value={500}>500 м</option>
              <option value={1000}>1000 м</option>
            </select>
          </label>
          <label className="v2-field">
            Тип объектов
            <select
              aria-label="Тип объектов"
              value={p.category}
              onChange={(e) => p.onCategory(e.target.value)}
            >
              <option value="">Все типы</option>
              {Object.entries(POI_KINDS)
                .filter(
                  ([key]) =>
                    !['residents', 'population', 'mcc_stations', 'mcd_stations'].includes(key),
                )
                .map(([key, label]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
            </select>
          </label>
          {p.loading && <p role="status">Загружаем объекты…</p>}
          {p.error && <p role="alert">{p.error.message}</p>}
          {p.data && (
            <>
              <p className="v2-hint">
                Срез: {String(p.data.meta.filters.source_snapshot || 'нет')} ·{' '}
                {p.data.features.length} объектов в области карты.
              </p>
              {p.data.meta.reason && <p className="v2-notice">{p.data.meta.reason}</p>}
              {Array.isArray(p.data.meta.filters.unavailable_route_ids) &&
                p.data.meta.filters.unavailable_route_ids.length > 0 && (
                  <p className="v2-hint">
                    Нет совпадающей географии для маршрутов:{' '}
                    {p.data.meta.filters.unavailable_route_ids.join(', ')}.
                  </p>
                )}
              {p.data.features.length > 0 && (
                <label className="v2-field">
                  Объект инфраструктуры
                  <select
                    aria-label="Объект инфраструктуры"
                    value=""
                    onChange={(e) => {
                      const feature = p.data?.features.find((f) => f.id === e.target.value);
                      if (feature) p.onSelect(feature);
                    }}
                  >
                    <option value="">Выберите объект на карте или в списке</option>
                    {p.data.features.map((f) => (
                      <option key={f.id} value={f.id}>
                        {f.properties.name ||
                          POI_KINDS[f.properties.category] ||
                          f.properties.category}{' '}
                        · {f.properties.address || f.id}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </>
          )}
          <p className="v2-hint">
            Объекты показаны точками. Радиус измерен по прямой до исходной геометрии объекта; он не
            определяет цель поездки.
          </p>
          <small>© OpenStreetMap contributors · ODbL 1.0</small>
        </>
      )}
    </section>
  );
}

export function InfrastructureDetails({
  snapshotId,
  date,
  route,
  stopId,
  radius,
}: {
  snapshotId: string;
  date: string;
  route: string;
  stopId?: string;
  radius: 500 | 1000;
}) {
  const params = parameters({ snapshot_id: snapshotId, date, route, stop_id: stopId, radius });
  const query = useQuery({
    queryKey: ['passengers', 'infrastructure', params],
    queryFn: ({ signal }) =>
      platform(`/passengers/infrastructure?${params}`, Infrastructure, undefined, signal),
    staleTime: 60000,
  });
  const data = query.data;
  return (
    <details className="infrastructure-details">
      <summary>Инфраструктура в радиусе {radius} м</summary>
      {!data && (
        <p role={query.error ? 'alert' : 'status'}>
          {query.error?.message || 'Загружаем инфраструктуру…'}
        </p>
      )}
      {data &&
        (data.meta.status !== 'ready' ? (
          <p className="v2-notice">{data.meta.reason}</p>
        ) : (
          <>
            <p>
              Срез {String(data.meta.filters.source_snapshot)} ·{' '}
              {stopId
                ? 'окружение остановки'
                : 'общий охват маршрута без повторного счёта объектов'}
              .
            </p>
            <dl>
              {data.rows.map((r) => (
                <div className="infra-count" key={r.kind}>
                  <dt>{POI_KINDS[r.kind] || r.kind}</dt>
                  <dd>{number(r.value)}</dd>
                </div>
              ))}
            </dl>
            <button
              className="v2-outline"
              onClick={() => downloadPassengerCsv('infrastructure', data.rows, data.meta)}
            >
              Скачать CSV инфраструктуры
            </button>
            <p className="v2-hint">
              Расстояние по прямой. Счётчики описывают объекты, а не пассажиров. Население —
              описательная оценка WorldPop.
            </p>
            <small>{data.attribution}</small>
          </>
        ))}
    </details>
  );
}

export function PoiCard({ feature, onClose }: { feature: PoiFeature; onClose: () => void }) {
  const p = feature.properties;
  const source = /^https:\/\/www\.openstreetmap\.org\/(node|way|relation)\/\d+$/.test(p.source_url)
    ? p.source_url
    : null;
  return (
    <article className="passenger-poi-card" aria-label="Объект инфраструктуры">
      <button className="poi-close" aria-label="Закрыть объект инфраструктуры" onClick={onClose}>
        ×
      </button>
      <small>{POI_KINDS[p.category] || p.category}</small>
      <h3>{p.name || 'Объект без названия'}</h3>
      {p.address && <p>{p.address}</p>}
      <p>
        Срез {p.snapshot} · ближайшая остановка выбранных маршрутов: {number(p.distance_m)} м по
        прямой.
      </p>
      <p className="v2-hint">Близость не устанавливает цель поездки.</p>
      {source && (
        <a href={source} target="_blank" rel="noreferrer">
          Объект OpenStreetMap
        </a>
      )}
    </article>
  );
}

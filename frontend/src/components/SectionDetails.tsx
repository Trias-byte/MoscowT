import type { SectionLoad } from '../lib/sectionLoad';
import { SECTION_MODEL_NOTE } from '../lib/sectionLoad';
import { number, valueColor } from '../lib/domain';

export function SectionDetails({
  load,
  directionName,
  thresholds,
  onRoute,
}: {
  load?: SectionLoad;
  directionName?: string;
  thresholds: number[];
  onRoute: () => void;
}) {
  return (
    <>
      <p className="v2-section-direction" data-testid="section-direction">
        Направление: {load?.directionName ?? directionName ?? 'Нет данных'}
      </p>
      <div className="v2-detail-metric">
        <span>Нагрузка участка · модельная оценка</span>
        <strong data-testid="section-load" style={{ color: valueColor(load?.rate, thresholds) }}>
          {number(load?.rate)}
        </strong>
        <small>Условный поток на вагон в час</small>
      </div>
      <p className="v2-notice" data-testid="section-model-note">
        {SECTION_MODEL_NOTE}
      </p>
      <dl>
        <dt>Вагонов в направлении · оценка</dt>
        <dd>{number(load?.meanVehicles)}</dd>
        <dt>Длительность окна</dt>
        <dd>{load?.hours ?? 0} ч</dd>
      </dl>
      <p className="v2-hint">
        Направления оцениваются отдельно. Утром модель усиливает движение к пересадочным узлам,
        вечером — от них. Фактическое наполнение салона не измерено.
      </p>
      <button className="v2-outline" onClick={onRoute}>
        Показать значения всего маршрута
      </button>
    </>
  );
}

export function RouteSections({
  loads,
  thresholds,
  onSelect,
}: {
  loads: SectionLoad[];
  thresholds: number[];
  onSelect: (load: SectionLoad) => void;
}) {
  if (!loads.length) return null;
  const patterns = [...new Set(loads.map((s) => s.patternId))];
  return (
    <section className="v2-section-list" aria-label="Участки маршрута">
      <h3>Возможные проблемные участки</h3>
      <p className="v2-hint">Модельная оценка · от {thresholds[2]} на вагон в час</p>
      {patterns.map((id) => {
        const all = loads.filter((s) => s.patternId === id);
        const high = all
          .filter((s) => s.rate !== null && s.rate >= thresholds[2])
          .sort((a, b) => b.rate! - a.rate!)
          .slice(0, 3);
        return (
          <div key={id}>
            <h4>{all[0].directionName}</h4>
            {high.length ? (
              high.map((load) => (
                <button
                  key={load.segmentId}
                  className="v2-section-item"
                  onClick={() => onSelect(load)}
                >
                  <i style={{ background: valueColor(load.rate, thresholds) }} />
                  <span>
                    {load.fromName} → {load.toName}
                  </span>
                  <b>{number(load.rate)}</b>
                </button>
              ))
            ) : (
              <p className="v2-hint">
                {all.some((s) => s.rate !== null)
                  ? 'В этом направлении оценка ниже порога.'
                  : 'Недостаточно данных для оценки.'}
              </p>
            )}
          </div>
        );
      })}
      <label className="v2-field">
        Выбрать участок маршрута
        <select
          value=""
          onChange={(event) => {
            const load = loads.find((s) => s.segmentId === event.target.value);
            if (load) onSelect(load);
          }}
        >
          <option value="" disabled>
            Все участки по направлениям
          </option>
          {patterns.map((id) => (
            <optgroup key={id} label={loads.find((s) => s.patternId === id)!.directionName}>
              {loads
                .filter((s) => s.patternId === id)
                .map((s) => (
                  <option key={s.segmentId} value={s.segmentId}>
                    {s.fromName} → {s.toName} · {number(s.rate)}
                  </option>
                ))}
            </optgroup>
          ))}
        </select>
      </label>
    </section>
  );
}

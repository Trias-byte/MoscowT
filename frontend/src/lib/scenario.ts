import { useEffect, useState } from 'react';
export interface Incident {
  id: string;
  longitude: number;
  latitude: number;
  route_ids: string[];
  start: string;
  duration_minutes: number;
  reduction: number;
}
export interface ScenarioDraft {
  engine: 'recompute';
  name: string;
  forecast_id: string;
  route_ids: string[];
  time_range: { start: string; end: string };
  weather: {
    temperature_2m: number | null;
    relative_humidity_2m: number | null;
    precipitation: number | null;
  };
  coefficients: { weather: number; event: number; season: number };
  incidents: Incident[];
  additional_vehicle_hours: number;
  schedule: {
    base_schedule_id: string | null;
    schedule_id: string | null;
    allow_period_reuse: boolean;
    base_headway_minutes: number | null;
    headway_minutes: number | null;
    service_start_minute: number | null;
    service_end_minute: number | null;
    elasticity: number;
    departures: { route_id: string; timestamp: string; change: 1 | -1; duration_minutes: number }[];
  };
}
export const emptyScenario: ScenarioDraft = {
  engine: 'recompute',
  name: 'Новый сценарий',
  forecast_id: '',
  route_ids: [],
  time_range: { start: '2025-11-01T00:00:00+03:00', end: '2025-11-02T00:00:00+03:00' },
  weather: { temperature_2m: null, relative_humidity_2m: null, precipitation: null },
  coefficients: { weather: 1, event: 1, season: 1 },
  incidents: [],
  additional_vehicle_hours: 0,
  schedule: {
    base_schedule_id: null,
    schedule_id: null,
    allow_period_reuse: false,
    base_headway_minutes: null,
    headway_minutes: null,
    service_start_minute: null,
    service_end_minute: null,
    elasticity: 0.3,
    departures: [],
  },
};
export const scenarioModes = {
  combined: 'Все факторы',
  weather: 'Только погода',
  season: 'Только сезонность',
  incidents: 'Только ДТП',
  schedule: 'Только расписание',
} as const;
export type ScenarioMode = keyof typeof scenarioModes;
export type ScenarioInput = ScenarioDraft & { mode: ScenarioMode };
export interface ScenarioRun {
  job?: string;
  signature: string;
  resultId?: string;
  error?: string;
}
export interface ScenarioRecord {
  id: string;
  draft: ScenarioDraft;
  mode: ScenarioMode;
  runs: Partial<Record<ScenarioMode, ScenarioRun>>;
}
export const stable = (value: unknown): string =>
  JSON.stringify(value, (_, v) =>
    v && typeof v === 'object' && !Array.isArray(v)
      ? Object.fromEntries(Object.entries(v).sort(([a], [b]) => a.localeCompare(b)))
      : v,
  );

export function scenarioInput(draft: ScenarioDraft, mode: ScenarioMode): ScenarioInput {
  const spec = structuredClone(draft);
  if (mode !== 'combined') {
    if (mode !== 'weather') spec.weather = structuredClone(emptyScenario.weather);
    if (mode !== 'incidents') spec.incidents = [];
    spec.coefficients = {
      weather: mode === 'weather' ? draft.coefficients.weather : 1,
      event: mode === 'incidents' ? draft.coefficients.event : 1,
      season: mode === 'season' ? draft.coefficients.season : 1,
    };
    if (mode !== 'schedule') {
      spec.additional_vehicle_hours = 0;
      spec.schedule = {
        ...structuredClone(emptyScenario.schedule),
        base_schedule_id: draft.schedule.base_schedule_id,
        allow_period_reuse: draft.schedule.allow_period_reuse,
        elasticity: draft.schedule.elasticity,
      };
    }
  }
  return { ...spec, mode };
}

// Merge only fields edited by this tab; job metadata must never restore an older draft.
export function mergeEdits<T>(latest: T, before: T, after: T): T {
  if (stable(before) === stable(after)) return latest;
  if (
    !before ||
    !after ||
    typeof before !== 'object' ||
    typeof after !== 'object' ||
    Array.isArray(before) ||
    Array.isArray(after)
  )
    return after;
  const next = { ...latest };
  for (const key of Object.keys(after) as (keyof T)[])
    next[key] = mergeEdits(latest[key], before[key], after[key]);
  return next;
}
const PREFIX = 'potok-scenario-record-v2:';
function readRecords(): ScenarioRecord[] {
  const records: ScenarioRecord[] = [];
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)!;
    if (!key.startsWith(PREFIX)) continue;
    try {
      const record = JSON.parse(localStorage.getItem(key)!);
      if (
        record?.id &&
        record.draft?.engine === 'recompute' &&
        record.mode in scenarioModes &&
        record.runs
      )
        records.push(record);
    } catch {
      /* Skip damaged entries. */
    }
  }
  return records.sort((a, b) => a.id.localeCompare(b.id));
}
function saveRecord(record: ScenarioRecord) {
  localStorage.setItem(PREFIX + record.id, JSON.stringify(record));
  window.dispatchEvent(new Event('potok-scenarios'));
}
function initialRecords() {
  const records = readRecords();
  if (records.length) return records;
  let draft = structuredClone(emptyScenario);
  try {
    const old = JSON.parse(localStorage.getItem('potok-scenario-draft-v1') || 'null');
    if (old?.engine === 'recompute' && old.schedule && old.weather && Array.isArray(old.incidents))
      draft = old;
  } catch {
    /* Start a new draft. */
  }
  const record: ScenarioRecord = { id: crypto.randomUUID(), draft, mode: 'combined', runs: {} };
  saveRecord(record);
  return [record];
}
export function useScenarioWorkspace() {
  const [records, setRecords] = useState(initialRecords);
  const [activeId, setActiveId] = useState(
    () => sessionStorage.getItem('potok-active-scenario') || records[0].id,
  );
  useEffect(() => {
    sessionStorage.setItem('potok-active-scenario', activeId);
  }, [activeId]);
  const record = records.find((r) => r.id === activeId) || records[0];
  useEffect(() => {
    const reload = () => setRecords(readRecords());
    window.addEventListener('storage', reload);
    window.addEventListener('potok-scenarios', reload);
    return () => {
      window.removeEventListener('storage', reload);
      window.removeEventListener('potok-scenarios', reload);
    };
  }, []);
  const select = (id: string) => {
    sessionStorage.setItem('potok-active-scenario', id);
    setActiveId(id);
  };
  const update = (id: string, change: (r: ScenarioRecord) => ScenarioRecord) => {
    const latest = readRecords().find((r) => r.id === id);
    if (latest) saveRecord(change(latest));
    setRecords(readRecords());
  };
  return {
    records,
    record,
    select,
    create: () => {
      const next: ScenarioRecord = {
        id: crypto.randomUUID(),
        mode: 'combined',
        runs: {},
        draft: { ...structuredClone(record.draft), name: `Сценарий ${records.length + 1}` },
      };
      saveRecord(next);
      setRecords(readRecords());
      select(next.id);
    },
    setDraft: (value: ScenarioDraft | ((previous: ScenarioDraft) => ScenarioDraft)) => {
      update(record.id, (latest) => ({
        ...latest,
        draft:
          typeof value === 'function'
            ? value(latest.draft)
            : mergeEdits(latest.draft, record.draft, value),
      }));
    },
    setMode: (mode: ScenarioMode) => update(record.id, (latest) => ({ ...latest, mode })),
    setRun: (id: string, mode: ScenarioMode, run: ScenarioRun, expectedJob?: string) =>
      update(id, (latest) =>
        expectedJob && latest.runs[mode]?.job !== expectedJob
          ? latest
          : {
              ...latest,
              runs: { ...latest.runs, [mode]: run },
            },
      ),
  };
}
export type ScenarioWorkspace = ReturnType<typeof useScenarioWorkspace>;
export function visibleIncidents(incidents: Incident[], range: { start: string; end: string }) {
  return incidents.filter(
    (i) =>
      Date.parse(i.start) < Date.parse(range.end) &&
      Date.parse(i.start) + i.duration_minutes * 60000 > Date.parse(range.start),
  );
}

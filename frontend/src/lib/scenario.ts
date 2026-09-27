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
export function useScenarioDraft() {
  const [draft, setDraft] = useState<ScenarioDraft>(() => {
    try {
      const value = JSON.parse(localStorage.getItem('potok-scenario-draft-v1') || 'null');
      if (
        value?.engine === 'recompute' &&
        Array.isArray(value.incidents) &&
        value.schedule &&
        value.weather
      )
        return value;
    } catch {
      /* A damaged browser draft does not prevent startup. */
    }
    return structuredClone(emptyScenario);
  });
  useEffect(() => {
    localStorage.setItem('potok-scenario-draft-v1', JSON.stringify(draft));
  }, [draft]);
  return [draft, setDraft] as const;
}

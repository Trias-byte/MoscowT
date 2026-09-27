import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import { Dataset, Forecast, Job, Loose, Model, Schedule, platform } from './platform';

export const Factor = z
  .object({ id: z.string(), start: z.string(), end: z.string(), rows: z.number() })
  .passthrough();
export function usePlatform() {
  const client = useQueryClient();
  const jobs = useQuery({
    queryKey: ['platform-jobs'],
    queryFn: () => platform('/jobs', z.array(Job)),
    refetchInterval: 2000,
  });
  const signature = jobs.data?.map((j) => `${j.id}:${j.status}`).join(',');
  useEffect(() => {
    void client.invalidateQueries({ queryKey: ['platform'] });
  }, [signature, client]);
  const data = useQuery({
    queryKey: ['platform'],
    queryFn: async () => {
      const [datasets, models, forecasts, schedules, factors, capabilities, external, weather] =
        await Promise.all([
          platform(
            '/datasets',
            z.object({ current_id: z.string().nullable(), versions: z.array(Dataset) }),
          ),
          platform('/models', z.array(Model)),
          platform('/forecast-runs', z.array(Forecast)),
          platform('/schedules', z.array(Schedule)),
          platform(
            '/factor-datasets',
            z.object({
              weather_hourly: z.array(Factor),
              accidents: z.array(Factor),
              accident_links: z.array(Factor),
            }),
          ),
          platform(
            '/capabilities',
            z.object({
              current_snapshot: z.object({
                snapshotId: z.string().optional(),
                forecastId: z.string().optional(),
                scheduleId: z.string().nullable().optional(),
              }),
            }),
          ),
          platform(
            '/external-sources',
            z.object({
              sources: z.array(Loose),
              snapshots: z.array(Loose),
              evaluations: z.array(Loose),
            }),
          ),
          platform(
            '/weather-forecasts',
            z.array(
              z.object({
                id: z.string(),
                available_at: z.string(),
                start: z.string(),
                end: z.string(),
              }),
            ),
          ),
        ]);
      return { datasets, models, forecasts, schedules, factors, capabilities, external, weather };
    },
  });
  return { data, jobs, refresh: () => client.invalidateQueries({ queryKey: ['platform'] }) };
}
export type PlatformData = NonNullable<ReturnType<typeof usePlatform>['data']['data']>;

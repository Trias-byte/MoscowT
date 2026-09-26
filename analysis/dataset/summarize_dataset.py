"""Summarize hourly targets and reference-table consistency."""
from pathlib import Path
import json
import pandas as pd
import numpy as np
import openpyxl

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
DATA = ROOT / 'dataset'
ROUTES = [1,5,7,11,12,17,25,26,28,50]


def main():
    labels = pd.concat([pd.read_csv(DATA/'labels'/f'labels_day_{s}.csv', sep=';').assign(split=s) for s in ['train','test']],ignore_index=True)
    grid = pd.MultiIndex.from_product([ROUTES,pd.date_range('2025-01-01','2025-10-31').strftime('%Y-%m-%d'),range(24)],names=['route','date','hour'])
    complete = labels.set_index(['route','date','hour']).reindex(grid)
    complete['observed_label'] = complete.boardings.notna()
    complete['boardings'] = complete.boardings.fillna(0).astype('int64')
    complete = complete.reset_index()
    complete['month'] = complete.date.str[:7]
    complete['weekday'] = pd.to_datetime(complete.date).dt.dayofweek
    complete['weekend'] = complete.weekday.ge(5)
    daily = complete.groupby(['route','date'],as_index=False).boardings.sum()
    daily['month'] = daily.date.str[:7]
    monthly = daily.groupby('month').agg(total=('boardings','sum'),dates=('date','nunique'))
    monthly['daily_average'] = monthly.total/monthly.dates
    route = complete.groupby('route').agg(total=('boardings','sum'),positive_hours=('observed_label','sum'),max_hour=('boardings','max'))
    route['share_pct'] = route.total/route.total.sum()*100
    route['daily_average'] = route.total/304
    route['positive_days'] = daily[daily.boardings.gt(0)].groupby('route').date.nunique().reindex(route.index,fill_value=0)
    route['zero_days'] = 304-route.positive_days
    hour = complete.groupby(['weekend','hour']).boardings.sum().unstack(0)
    days_by_weekend=complete[['date','weekend']].drop_duplicates().groupby('weekend').size()
    hour=hour.div(days_by_weekend,axis=1)
    weekday = complete.groupby('weekday').boardings.sum()/complete[['date','weekday']].drop_duplicates().groupby('weekday').size()
    month_route=daily.pivot_table(index='route',columns='month',values='boardings',aggfunc='sum',fill_value=0)
    month_route_daily=daily.pivot_table(index='route',columns='month',values='boardings',aggfunc='mean',fill_value=0)
    sub = pd.read_csv(DATA/'test_submission.csv',sep=';')
    workbook = openpyxl.load_workbook(DATA/'spravochniki'/'Хакатон_справочники_трамвай_10_маршрутов.xlsx',read_only=True,data_only=True)
    frames = {}
    for sheet in workbook:
        rows = list(sheet.values)
        h = 0 if sheet.title == 'Порядок_с_координатами' else 1
        frames[sheet.title] = pd.DataFrame(rows[h+1:], columns=rows[h]).dropna(how='all')
    routes=frames['Маршруты GTFS_ROUTES']
    stops=frames['Остановки GTFS_STOPS']
    trips=frames['Порядок_остановок GTFS_TRIPS_ST']
    coords=frames['Порядок_с_координатами']
    schedule=frames['Расписание']
    roster=frames['Наряд']
    joined=trips.merge(stops[['stop_id','stop_name','stop_lat','stop_lon']],on='stop_id',how='left',validate='many_to_one')
    refchecks = dict(
        missing_route_ids=sorted(set(trips.route_id)-set(routes.route_id)),
        missing_stop_ids=sorted(set(trips.stop_id)-set(stops.stop_id)),
        duplicated_trip_stop_sequence=int(trips.duplicated(['trip_id','stop_sequence']).sum()),
        ordered_stops_equal_join=joined.equals(coords),
        coords_range={c:[pd.to_numeric(stops[c]).min(),pd.to_numeric(stops[c]).max()] for c in ['stop_lat','stop_lon']},
        sequence_gaps={k:sorted(set(range(1,pd.to_numeric(g.stop_sequence).max()+1))-set(pd.to_numeric(g.stop_sequence))) for k,g in trips.groupby('trip_id')},
        stops_per_route=trips.groupby('route_short_name').agg(rows=('stop_id','size'),unique_stops=('stop_id','nunique'),trips=('trip_id','nunique')).reset_index().to_dict('records'),
        trips=trips.groupby(['route_short_name','trip_id','direction_id']).agg(stops=('stop_id','size')).reset_index().to_dict('records'),
        schedule_time_range=[schedule.arrival_time.min(),schedule.departure_time.max()],
        schedule_sequence_range=[pd.to_numeric(schedule.stop_sequence).min(),pd.to_numeric(schedule.stop_sequence).max()],
        schedule_matches_trip_keys=int(schedule.merge(trips,on=['trip_id','stop_sequence','stop_id'],how='inner').shape[0]),
        roster_schedule_matching_route_grafic=int(schedule[['route_id','grafic']].drop_duplicates().merge(roster[['route_id','grafic']].drop_duplicates(),on=['route_id','grafic']).shape[0]),
        reference_routes=sorted(pd.to_numeric(routes.route_short_name).tolist()),
        overlap_routes=sorted(set(ROUTES)&set(pd.to_numeric(routes.route_short_name))),
        missing_routes=sorted(set(ROUTES)-set(pd.to_numeric(routes.route_short_name))),
        metadata_only_routes=sorted(set(pd.to_numeric(routes.route_short_name))-set(ROUTES)),
    )
    zero_runs = {}
    for r,g in daily.groupby('route'):
        z=g.boardings.eq(0)
        chunks=(z!=z.shift()).cumsum()
        zero_runs[str(r)]=[dict(start=x.date.min(),end=x.date.max(),days=len(x)) for _,x in g[z].groupby(chunks[z])]
    summary=dict(total=int(labels.boardings.sum()),label_rows=len(labels),full_grid=len(complete),missing_grid_rows=int((~complete.observed_label).sum()),routes=route.reset_index().to_dict('records'),monthly=monthly.reset_index().to_dict('records'),weekday_daily_average=weekday.to_dict(),hourly_weekday_weekend=hour.reset_index().to_dict('records'),hour_counts=labels.groupby('hour').agg(positive_keys=('boardings','size'),total=('boardings','sum')).reset_index().to_dict('records'),zero_day_runs=zero_runs,top_hourly=labels.nlargest(10,'boardings').to_dict('records'),top_daily=daily.nlargest(10,'boardings').to_dict('records'),min_daily_overall=daily.groupby('date').boardings.sum().nsmallest(10).to_dict(),max_daily_overall=daily.groupby('date').boardings.sum().nlargest(10).to_dict(),submission_by_route=sub.groupby('route').prediction.agg(['size','sum','min','max','nunique']).reset_index().to_dict('records'),submission_prediction_changes_by_hour=int((sub.groupby(['route','date']).prediction.nunique()>1).sum()),reference_checks=refchecks)
    (OUT/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,default=lambda x:x.item() if hasattr(x,'item') else str(x)),encoding='utf-8')
    for name,df in [('route_summary',route),('monthly_summary',monthly),('monthly_by_route',month_route),('monthly_daily_average_by_route',month_route_daily),('hourly_profile',hour),('daily_by_route',daily)]:
        df.to_csv(OUT/(name+'.csv'),sep=';',index=name!='daily_by_route')
    print(json.dumps(summary,ensure_ascii=False,indent=2,default=str))


if __name__=='__main__':
    main()

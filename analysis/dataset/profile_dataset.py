"""Read-only profiling of the supplied dataset. No source files are modified."""
from pathlib import Path
from collections import Counter
import json
import math
import time
import tempfile
import numpy as np
import pandas as pd
import openpyxl

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'dataset'
OUT = Path(__file__).resolve().parent
SCRATCH = Path(tempfile.mkdtemp(prefix='moscowt-profile-'))
KEY = ['route', 'date', 'hour']
ROUTES = [1, 5, 7, 11, 12, 17, 25, 26, 28, 50]


def write_json(name, value):
    def clean(x):
        if isinstance(x,dict):
            return {k:clean(v) for k,v in x.items()}
        if isinstance(x,(list,tuple)):
            return [clean(v) for v in x]
        if isinstance(x,float) and not math.isfinite(x):
            return None
        return x
    (OUT / name).write_text(json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False, default=lambda x: x.item() if hasattr(x, 'item') else str(x)), encoding='utf-8')


def small_files():
    result = {}
    for p in [*sorted((DATA / 'labels').glob('*.csv')), DATA / 'test_submission.csv']:
        df = pd.read_csv(p, sep=';')
        target = df.columns[-1]
        grid = pd.MultiIndex.from_product([ROUTES, pd.date_range(df.date.min(), df.date.max()).strftime('%Y-%m-%d'), range(24)], names=KEY)
        indexed = df.set_index(KEY)[target]
        result[p.name] = dict(rows=len(df), columns=df.columns.tolist(), nulls=df.isna().sum().to_dict(), duplicate_keys=int(df.duplicated(KEY).sum()), min_date=df.date.min(), max_date=df.date.max(), dates=df.date.nunique(), routes=sorted(df.route.unique().tolist()), total=int(df[target].sum()), zeros=int(df[target].eq(0).sum()), negative=int(df[target].lt(0).sum()), min=int(df[target].min()), max=int(df[target].max()), quantiles=df[target].quantile([0,.25,.5,.75,.9,.95,.99,1]).to_dict(), grid_size=len(grid), absent_keys=len(grid.difference(indexed.index)), per_route=df.groupby('route').agg(rows=(target,'size'), total=(target,'sum'), min_date=('date','min'),max_date=('date','max'),dates=('date','nunique')).reset_index().to_dict('records'))
    write_json('small_files.json', result)
    books = {}
    for p in sorted((DATA/'spravochniki').glob('*.xlsx')):
        wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
        sheets = {}
        for ws in wb:
            rows = list(ws.values)
            header = 0 if p.name == 'Хакатон_пример_валидаций.xlsx' or ws.title == 'Порядок_с_координатами' else 1
            df = pd.DataFrame(rows[header+1:], columns=rows[header]).dropna(how='all')
            sheets[ws.title] = dict(rows=len(df), columns=df.columns.tolist(), nulls=df.isna().sum().to_dict(), unique=df.nunique().to_dict())
        books[p.name] = sheets
    write_json('workbooks.json', books)
    print('Small files and Excel workbooks profiled', flush=True)


def profile_raw():
    stats = {}
    sets = {}
    grouped = []
    raw_hash_paths = []
    card_hash_paths = []
    device_tran_paths = []
    total_started = time.time()
    for split in ['train', 'test']:
        p = DATA / (split+'.csv')
        counters = {c: Counter() for c in ['tran_no','device_no','validation_result','tran_type_id','place_id','good_type','pass_route','ngpt_route','bus_exit_no','garage_number']}
        nulls, whitespace, anomalies = Counter(), Counter(), Counter()
        years = {c: Counter() for c in ['tran_date_time','begin_date_time','input_date_time']}
        ranges = {c: [None,None] for c in years}
        failed_date_examples = {c: [] for c in years}
        card_lengths = Counter()
        aggregates = Counter()
        all_aggregates = Counter()
        n = success = 0
        last_ts = None
        rh = SCRATCH / (split+'_rows.bin')
        ch = SCRATCH / (split+'_cards.bin')
        dh = SCRATCH / (split+'_device_tran.bin')
        raw_hash_paths.append(rh)
        card_hash_paths.append(ch)
        device_tran_paths.append(dh)
        with rh.open('wb') as row_out, ch.open('wb') as card_out, dh.open('wb') as key_out:
            for idx, df in enumerate(pd.read_csv(p,sep=';',dtype=str,keep_default_na=False,chunksize=250_000)):
                n += len(df)
                for c in df:
                    nulls[c] += int(df[c].eq('').sum())
                    whitespace[c] += int((df[c].str.strip()!=df[c]).sum())
                for c in counters:
                    counters[c].update(df[c].value_counts().to_dict())
                card_lengths.update(df.crd_hashcode.str.len().value_counts().to_dict())
                anomalies['card_non_hex'] += int((~df.crd_hashcode.str.fullmatch('[0-9a-fA-F]+')).sum())
                parsed = {}
                for c in years:
                    years[c].update(df[c].str[:4].value_counts().to_dict())
                    dt = pd.to_datetime(df[c],format='%Y-%m-%d %H:%M:%S',errors='coerce')
                    parsed[c] = dt
                    bad = dt.isna()
                    anomalies[c+'_unparsed'] += int(bad.sum())
                    failed_date_examples[c] = list(dict.fromkeys(failed_date_examples[c]+df.loc[bad,c].head(5).tolist()))[:10]
                    vmin,vmax = df[c].min(),df[c].max()
                    ranges[c] = [vmin if ranges[c][0] is None else min(ranges[c][0],vmin),vmax if ranges[c][1] is None else max(ranges[c][1],vmax)]
                dt = parsed['tran_date_time']
                anomalies['time_decreases_in_file'] += int(dt.diff().lt(pd.Timedelta(0)).sum())
                if last_ts is not None and dt.iloc[0] < last_ts:
                    anomalies['time_decreases_in_file'] += 1
                last_ts = dt.iloc[-1]
                begin_delta = (parsed['begin_date_time']-dt).dt.total_seconds()
                input_delta = (parsed['input_date_time']-dt).dt.total_seconds()
                for label, delta in [('begin_minus_tran',begin_delta),('input_minus_tran',input_delta)]:
                    for bucket,mask in [('negative',delta.lt(0)),('zero',delta.eq(0)),('positive',delta.gt(0)),('abs_gt_1h',delta.abs().gt(3600)),('abs_gt_1d',delta.abs().gt(86400)),('abs_gt_30d',delta.abs().gt(86400*30))]:
                        anomalies[label+'_'+bucket] += int(mask.sum())
                start, end = ('2025-01-01','2025-09-01') if split=='train' else ('2025-09-01','2025-11-01')
                date = df.tran_date_time.str[:10]
                anomalies['before_declared_period'] += int(date.lt(start).sum())
                anomalies['after_declared_period'] += int(date.ge(end).sum())
                good = df.validation_result.eq('1')
                success += int(good.sum())
                route = df.ngpt_route.str.extract(r'^(\d+) трамвай$',expand=False)
                anomalies['route_unparsed'] += int(route.isna().sum())
                group_df = pd.DataFrame({'route':route,'date':date,'hour':dt.dt.hour,'boardings':good.astype('int64')})
                aggregates.update(group_df.groupby(KEY).boardings.sum().to_dict())
                all_aggregates.update(group_df.groupby(KEY).size().to_dict())
                pd.util.hash_pandas_object(df,index=False).to_numpy(dtype='uint64').tofile(row_out)
                pd.util.hash_pandas_object(df.crd_hashcode,index=False).to_numpy(dtype='uint64').tofile(card_out)
                pd.util.hash_pandas_object(df[['device_no','tran_no']],index=False).to_numpy(dtype='uint64').tofile(key_out)
                if idx%8==0:
                    print(f'{split}: {n:,} rows; {time.time()-total_started:.0f}s elapsed',flush=True)
        cols={c:dict(distinct=len(v),top20=v.most_common(20)) for c,v in counters.items()}
        for c in ['validation_result','tran_type_id','place_id','ngpt_route','good_type']:
            cols[c]['all_values']=dict(counters[c])
        stats[split]=dict(rows=n,successful=success,unsuccessful=n-success,columns=list(df.columns),nulls=dict(nulls),whitespace=dict(whitespace),date_ranges=ranges,date_years={c:dict(v) for c,v in years.items()},anomalies=dict(anomalies),failed_date_examples=failed_date_examples,card_hash_lengths=dict(card_lengths),fields=cols)
        sets[split]={c:set(v) for c,v in counters.items()}
        rows=[dict(split=split,route=int(k[0]),date=k[1],hour=int(k[2]),boardings=v,events=all_aggregates[k]) for k,v in aggregates.items()]
        agg=pd.DataFrame(rows).sort_values(KEY)
        agg.to_csv(OUT/(split+'_hourly_from_raw.csv'),sep=';',index=False)
        grouped.append(agg)
        labels=pd.read_csv(DATA/'labels'/('labels_day_'+split+'.csv'),sep=';')
        comparison=agg.merge(labels,on=KEY,how='outer',suffixes=('_raw','_label'),indicator=True)
        mismatch=comparison[comparison.boardings_raw.fillna(0)!=comparison.boardings_label.fillna(0)]
        mismatch.to_csv(OUT/(split+'_label_mismatches.csv'),sep=';',index=False)
        stats[split]['label_comparison']=dict(raw_hour_keys=len(agg),positive_raw_hour_keys=int(agg.boardings.gt(0).sum()),label_keys=len(labels),raw_sum=int(agg.boardings.sum()),label_sum=int(labels.boardings.sum()),differing_keys=len(mismatch),difference_sum=int(agg.boardings.sum()-labels.boardings.sum()),examples=mismatch.head(20).to_dict('records'))
        write_json('raw_profile.json',stats)
        print(f'Completed {split}: {n:,} rows; {time.time()-total_started:.0f}s',flush=True)
        del counters
    stats['combined_fields']={c:dict(distinct=len(sets['train'][c]|sets['test'][c]),overlap=len(sets['train'][c]&sets['test'][c]),new_in_test=len(sets['test'][c]-sets['train'][c])) for c in sets['train']}
    for kind,paths in [('row_fingerprint',raw_hash_paths),('card_fingerprint',card_hash_paths),('device_tran_fingerprint',device_tran_paths)]:
        arrs=[]
        for split,p in zip(['train','test'],paths):
            a=np.fromfile(p,dtype='uint64')
            unique=np.unique(a)
            stats[split][kind]=dict(distinct=len(unique),repeated_rows=len(a)-len(unique))
            arrs.append(unique)
            del a
        # Both arrays are already sorted and unique. Search rather than sorting
        # a second combined copy, keeping peak memory below the full CSV size.
        positions=np.searchsorted(arrs[0],arrs[1])
        if len(arrs[0]):
            overlap=int(np.count_nonzero((positions<len(arrs[0])) & (arrs[0][np.minimum(positions,len(arrs[0])-1)]==arrs[1])))
        else:
            overlap=0
        stats[kind+'_union']=dict(distinct=len(arrs[0])+len(arrs[1])-overlap,intersection=overlap)
        del positions
        del arrs
        print(f'Computed {kind}; {time.time()-total_started:.0f}s',flush=True)
        write_json('raw_profile.json',stats)
    for p in [*raw_hash_paths,*card_hash_paths,*device_tran_paths]:
        p.unlink()
    SCRATCH.rmdir()
    write_json('raw_profile.json',stats)


if __name__=='__main__':
    small_files()
    profile_raw()

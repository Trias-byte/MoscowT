"""Audit time boundaries, target overlap and feature availability; read-only sources."""
from pathlib import Path
from collections import Counter
import argparse
import json
import time
import hashlib
import subprocess
import csv
import io
import numpy as np
import pandas as pd
import openpyxl

OUT = Path(__file__).resolve().parent
DATA = OUT.parents[1] / 'dataset'
ROUTES = [1,5,7,11,12,17,25,26,28,50]
KEY = ['route','date','hour']
CUTOFFS = ['2025-09-01','2025-11-01']


def timestamp(frame):
    return pd.to_datetime(frame.date) + pd.to_timedelta(frame.hour, unit='h')


def scan_raw():
    results={}
    start=time.time()
    for split in ['train','test']:
        path=DATA/f'{split}.csv'
        counts=Counter()
        successful=Counter()
        rows=0
        availability={c:Counter() for c in CUTOFFS}
        invalid=Counter()
        for i,df in enumerate(pd.read_csv(path,sep=';',usecols=['tran_date_time','input_date_time','validation_result','ngpt_route'],dtype=str,keep_default_na=False,chunksize=500_000)):
            event=pd.to_datetime(df.tran_date_time,format='%Y-%m-%d %H:%M:%S',errors='coerce')
            loaded=pd.to_datetime(df.input_date_time,format='%Y-%m-%d %H:%M:%S',errors='coerce')
            ok=df.validation_result.eq('1')
            invalid['event']+=int(event.isna().sum())
            invalid['loaded']+=int(loaded.isna().sum())
            for cutoff,a in availability.items():
                past=event.lt(cutoff)
                late=past&loaded.ge(cutoff)
                a['past_events']+=int(past.sum())
                a['past_successful']+=int((past&ok).sum())
                a['past_events_loaded_at_or_after_cutoff']+=int(late.sum())
                a['past_successful_loaded_at_or_after_cutoff']+=int((late&ok).sum())
                a['past_events_with_unknown_load_time']+=int((past&loaded.isna()).sum())
            frame=pd.DataFrame({'route':df.ngpt_route.str.extract(r'^(\d+) трамвай$',expand=False).astype(int),'date':df.tran_date_time.str[:10],'hour':event.dt.hour,'boardings':ok.astype('int64')})
            counts.update(frame.groupby(KEY).size().to_dict())
            successful.update(frame.groupby(KEY).boardings.sum().to_dict())
            rows+=len(df)
            if i%10==0:
                print(f'{split}: {rows:,} events, {time.time()-start:.0f}s',flush=True)
        aggregate=pd.DataFrame([dict(zip(KEY,k),boardings=v,events=counts[k]) for k,v in successful.items()]).sort_values(KEY)
        # Fresh scan verifies the previous full audit without replacing its outputs.
        old=pd.read_csv(OUT/f'{split}_hourly_from_raw.csv',sep=';')[KEY+['boardings','events']].sort_values(KEY).reset_index(drop=True)
        pd.testing.assert_frame_equal(aggregate.reset_index(drop=True),old,check_dtype=False)
        results[split]=dict(rows=rows,bytes=path.stat().st_size,mtime_ns=path.stat().st_mtime_ns,availability={k:dict(v) for k,v in availability.items()},invalid_dates=dict(invalid))
    return results


def audit_small():
    raw={s:pd.read_csv(OUT/f'{s}_hourly_from_raw.csv',sep=';') for s in ['train','test']}
    labels={s:pd.read_csv(DATA/'labels'/f'labels_day_{s}.csv',sep=';') for s in ['train','test']}
    output={}
    for s,f in labels.items():
        f['timestamp']=timestamp(f)
        output[s+'_labels']=dict(rows=len(f),timestamp_min=str(f.timestamp.min()),timestamp_max=str(f.timestamp.max()),duplicate_keys=int(f.duplicated(KEY).sum()),file_sorted_by_timestamp=bool(f.timestamp.is_monotonic_increasing))
    common=labels['train'].merge(labels['test'],on=KEY)
    output['labels_common_keys']=len(common)
    output['labels_train_strictly_before_test']=bool(labels['train'].timestamp.max()<labels['test'].timestamp.min())
    for s,cut in [('train','2025-09-01'),('test','2025-11-01')]:
        tail=raw[s][raw[s].date.ge(cut)].copy()
        output[s+'_tail']=dict(events=int(tail.events.sum()),successful=int(tail.boardings.sum()),positive_hour_keys=int(tail.boardings.gt(0).sum()),rows=tail.to_dict('records'))
    exposed=raw['train'].merge(labels['test'],on=KEY,suffixes=('_raw_train','_test_label'))
    exposed=exposed[exposed.boardings_raw_train.gt(0)].sort_values(KEY)
    assert (exposed.boardings_raw_train==exposed.boardings_test_label).all()
    exposed.to_csv(OUT/'test_labels_exposed_in_raw_train.csv',sep=';',index=False)
    output['test_target_exposure']=dict(keys=len(exposed),boardings=int(exposed.boardings_raw_train.sum()),share_of_test_volume_pct=float(exposed.boardings_raw_train.sum()/labels['test'].boardings.sum()*100))
    output['raw_files_common_hour_keys']=len(raw['train'].merge(raw['test'],on=KEY))
    all_labels=pd.concat(labels.values(),ignore_index=True).sort_values(['route','timestamp'])
    sparse_lags=[]
    for lag in [1,24,168]:
        source=all_labels.groupby('route').timestamp.shift(lag)
        delta=(all_labels.timestamp-source).dt.total_seconds()/3600
        present=source.notna()
        bad=present&delta.ne(lag)
        sparse_lags.append(dict(row_shift=lag,eligible_rows=int(present.sum()),wrong_clock_lag_rows=int(bad.sum()),wrong_pct=float(bad.sum()/present.sum()*100),min_hours=float(delta.min()),median_hours=float(delta.median()),max_hours=float(delta.max())))
    output['sparse_row_lags']=sparse_lags
    times=pd.date_range('2025-09-01','2025-11-01',freq='h',inclusive='left')
    lag_exposure=[]
    for lag in [1,24,168,720,1464]:
        source=times-pd.Timedelta(hours=lag)
        for mode,origin in [('fixed_61_days',pd.Timestamp('2025-09-01')),('daily_24_hours',times.floor('D')),('rolling_next_hour',times)]:
            # Source intervals must have ended by the forecast origin; zero delay assumed.
            inaccessible=(source+pd.Timedelta(hours=1)>origin)
            lag_exposure.append(dict(mode=mode,lag_hours=lag,test_keys=len(times)*len(ROUTES),unavailable_actual_keys=int(np.count_nonzero(inaccessible))*len(ROUTES),unavailable_pct=float(np.mean(inaccessible)*100)))
    output['lag_availability_ideal_zero_delay']=lag_exposure
    all_raw=pd.concat(raw.values()).groupby(KEY).boardings.sum()
    all_raw=all_raw[all_raw.index.get_level_values('date')<'2025-11-01']
    reference=all_labels.set_index(KEY).boardings
    comparison=pd.concat([all_raw.rename('raw'),reference.rename('label')],axis=1).fillna(0)
    assert (comparison.raw==comparison.label).all()
    output['combined_calendar_reconciliation_mismatches']=int((comparison.raw!=comparison.label).sum())
    output['small_file_sha256']={str(p.relative_to(DATA)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [*sorted((DATA/'labels').glob('*.csv')),DATA/'test_submission.csv']}
    # Equal-horizon folds entirely inside the supplied history, with synchronized routes.
    folds=[]
    for role,origin in [('development','2025-05-01'),('development','2025-06-01'),('development','2025-07-01'),('holdout','2025-09-01')]:
        origin=pd.Timestamp(origin);end=origin+pd.Timedelta(days=61)
        assert end<=pd.Timestamp('2025-11-01')
        folds.append(dict(role=role,origin=str(origin),train_end_exclusive=str(origin),test_end_exclusive=str(end),test_hours=1464,test_keys=14640))
    output['proposed_fixed_origin_folds']=folds
    book=openpyxl.load_workbook(DATA/'spravochniki'/'Хакатон_справочники_трамвай_10_маршрутов.xlsx',read_only=True,data_only=True)
    snapshots={}
    for sheet in book:
        rows=list(sheet.values);h=0 if sheet.title=='Порядок_с_координатами' else 1
        df=pd.DataFrame(rows[h+1:],columns=rows[h])
        if 'actual_date' in df:
            dates=pd.to_datetime(df.actual_date,errors='coerce')
            snapshots[sheet.title]=dict(rows=len(df),min_actual_date=str(dates.min().date()),max_actual_date=str(dates.max().date()),**{'actual_date_at_or_after_'+cut:int(dates.ge(cut).sum()) for cut in CUTOFFS})
    output['reference_snapshots']=snapshots
    prior=json.loads((OUT/'raw_profile.json').read_text())
    output['previous_full_audit']=dict(row_intersection=prior['row_fingerprint_union']['intersection'],card_intersection=prior['card_fingerprint_union']['intersection'],device_tran_intersection=prior['device_tran_fingerprint_union']['intersection'],method='64-bit pandas fingerprints; previous full-file audit, aggregates reverified by fresh scan')
    # Resolve equality at the availability boundary without re-reading every
    # field into pandas. rg narrows candidates; csv validates the input field.
    exact={}
    for split in ['train','test']:
        stamps=[cut+' 00:00:00' for cut in CUTOFFS]
        command=['rg','--no-heading','--no-filename','-F']
        for stamp in stamps:
            command.extend(['-e',';'+stamp+';'])
        command.append(str(DATA/f'{split}.csv'))
        found=subprocess.run(command,capture_output=True,text=True,check=False)
        if found.returncode not in [0,1]:
            raise RuntimeError(found.stderr)
        result={stamp:dict(events=0,successful=0) for stamp in stamps}
        for row in csv.reader(io.StringIO(found.stdout),delimiter=';'):
            if len(row)==14 and row[4] in result and row[2]<row[4]:
                result[row[4]]['events']+=1
                result[row[4]]['successful']+=int(row[6]=='1')
        exact[split]=result
    output['events_loaded_exactly_at_cutoff']=exact
    assert output['labels_common_keys']==0
    assert output['labels_train_strictly_before_test']
    assert not any(output[s+'_labels']['duplicate_keys'] for s in ['train','test'])
    return output


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--reuse-scan',action='store_true',help='Reuse availability scan saved by this audit.')
    args=parser.parse_args()
    scan_path=OUT/'temporal_availability_scan.json'
    if args.reuse_scan:
        scan=json.loads(scan_path.read_text())
        for s,stat in scan.items():
            current=(DATA/f'{s}.csv').stat()
            assert current.st_size==stat['bytes'] and current.st_mtime_ns==stat['mtime_ns'],f'{s} changed'
    else:
        scan=scan_raw()
        scan_path.write_text(json.dumps(scan,ensure_ascii=False,indent=2))
    result=audit_small()
    result['fresh_raw_scan']=scan
    (OUT/'temporal_split_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str,allow_nan=False))
    print(json.dumps({k:v for k,v in result.items() if k not in ['train_tail','test_tail']},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()

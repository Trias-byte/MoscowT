"""Insert measured tables into the dataset description after profiling."""
from pathlib import Path
from collections import Counter
import json
import math
import pandas as pd

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]


def number(x, decimals=0):
    return f'{x:,.{decimals}f}'.replace(',', '\u202f').replace('.', ',')


def percent(value, total, decimals):
    rate=value/total*100
    if 0<rate<10**(-decimals):
        return '<'+number(10**(-decimals),decimals)+' %'
    return number(rate,decimals)+' %'


def table(columns, rows):
    return '\n'.join(['| '+' | '.join(columns)+' |','|'+'|'.join(['---']*len(columns))+'|']+['| '+' | '.join(str(v).replace('|','\\|') for v in row)+' |' for row in rows])


def clean(value):
    if isinstance(value, dict):
        return {k:clean(v) for k,v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value):
        return None
    return value


def main():
    raw=json.loads((OUT/'raw_profile.json').read_text())
    summary=json.loads((OUT/'summary.json').read_text())
    small=json.loads((OUT/'small_files.json').read_text())
    train,test=raw['train'],raw['test']
    total=train['rows']+test['rows']
    good=train['successful']+test['successful']
    nulls={c:train['nulls'][c]+test['nulls'][c] for c in train['columns']}
    values={c:Counter(train['fields'][c]['all_values'])+Counter(test['fields'][c]['all_values']) for c in ['validation_result','tran_type_id','place_id','good_type','ngpt_route']}
    agg={s:pd.read_csv(OUT/(s+'_hourly_from_raw.csv'),sep=';') for s in ['train','test']}
    route5=pd.concat(agg.values()).query('route == 5')
    overview=f'''Проверены все восемь файлов, в том числе **{number(total)} строк сырых операций**. Основная разметка за январь–октябрь 2025 года содержит **{number(summary['total'])} успешных валидаций**. Число ненулевых почасовых значений — {number(summary['label_rows'])}.

Наиболее существенные результаты:

1. Данных достаточно для маршрутного почасового прогноза и его агрегации по времени. Для фактической загрузки салона и остановочного пассажиропотока информации недостаточно.
2. В labels есть девять маршрутов. Для № 5 в сырых файлах найдена только одна неуспешная операция; полноценного обучающего ряда для него нет.
3. Готовая разметка точно воспроизводится после объединения исходных CSV и разбиения по времени события. В labels test уже включены 523 успешные операции начала сентября, физически лежащие в train.
4. Labels хранят только ненулевые часы. Для 10 маршрутов нужна полная сетка из 72 960 исторических часов.
5. Географический справочник покрывает пять из десяти целевых маршрутов. Расписание — только 15 остановок одного рейса, наряд — 15 строк за один день 2026 года.
6. Справочники отражают поздние версии маршрутов и остановок. Их нельзя автоматически считать исторически верными для всего обучающего периода.
7. Служебные даты и некоторые поля заполнены неоднородно; точные масштабы приведены ниже.
'''
    inventory=[]
    rowcounts={'train.csv':train['rows'],'test.csv':test['rows'],'labels/labels_day_train.csv':46234,'labels/labels_day_test.csv':11317,'test_submission.csv':14640}
    descriptions={'README.md':'Правила target, разбиение, метрика и формат ответа','train.csv':'Журнал операций train, 14 полей','test.csv':'Журнал операций test, 14 полей','labels/labels_day_train.csv':'Положительные почасовые target train, 4 поля','labels/labels_day_test.csv':'Положительные почасовые target test, 4 поля','test_submission.csv':'Полная будущая сетка с примером прогноза, 4 поля','spravochniki/Хакатон_пример_валидаций.xlsx':'20 записей, один лист','spravochniki/Хакатон_справочники_трамвай_10_маршрутов.xlsx':'6 листов, маршруты, остановки и примеры работы'}
    bytes_total=0
    for name,description in descriptions.items():
        size=(ROOT/'dataset'/name).stat().st_size
        bytes_total+=size
        inventory.append([f'`{name}`',number(size),number(rowcounts[name]) if name in rowcounts else '—',description])
    file_inventory='## 3. Состав и размеры файлов\n\n'+table(['Файл','Размер, байт','Строк данных','Содержание'],inventory)+f'\n\nОбщий размер: {number(bytes_total)} байт, или {number(bytes_total/10**9,3)} ГБ / {number(bytes_total/2**30,3)} ГиБ.\n'
    quality='## 5. Фактическое качество исходных событий\n\n'
    quality+=table(['Показатель','train','test','Всего'],[
        ['Строк',number(train['rows']),number(test['rows']),number(total)],
        ['Успешных валидаций',number(train['successful']),number(test['successful']),number(good)],
        ['Прочих результатов',number(train['unsuccessful']),number(test['unsuccessful']),number(total-good)],
        ['Доля успехов',number(train['successful']/train['rows']*100,2)+' %',number(test['successful']/test['rows']*100,2)+' %',number(good/total*100,2)+' %'],
    ])
    quality+='\n\n### Заполненность и разные значения\n\n'
    rows=[]
    for c in train['columns']:
        unique='—'
        if c in raw['combined_fields']:
            unique=number(raw['combined_fields'][c]['distinct']-(nulls[c]>0))
        if c=='crd_hashcode': unique=number(raw['card_fingerprint_union']['distinct'])+'*'
        rows.append([f'`{c}`',number(train['nulls'][c]),number(test['nulls'][c]),percent(nulls[c],total,3),unique])
    quality+=table(['Поле','Пусто в train','Пусто в test','Доля пустых, вместе','Разных непустых, вместе'],rows)
    quality+='\n\nПустое значение здесь — пустая строка CSV. `—` означает, что число разных временных значений отдельно не вычислялось; `*` — подсчёт по отпечаткам, см. методику.\n\n'
    whitespace=sum(x['whitespace']['good_type'] for x in [train,test])
    quality+=f"Внешние пробелы у `good_type` встречаются в {number(whitespace)} строках ({number(whitespace/total*100,2)} %). Перед группировкой категорий нужен `strip()`, сохраняя исходный текст для контроля. Пример: `30дн ММ+МГТ СКС ` с завершающим пробелом. Во всех строках `crd_hashcode` — 32 шестнадцатеричных символа; это не подтверждает, что каждый идентификатор соответствует отдельному человеку.\n\n"
    quality+=f"В объединении файлов {number(raw['combined_fields']['device_no']['distinct'])} устройств и {number(raw['combined_fields']['garage_number']['distinct']-1)} непустых гаражных номеров. В test встречается {number(raw['combined_fields']['device_no']['new_in_test'])} устройств, которых нет в train. Эти количества описывают всё окно наблюдений, а не одновременно работающий парк.\n\n"
    quality+='### Время события и служебные даты\n\n'
    quality+=table(['Поле','train: минимум → максимум','test: минимум → максимум'],[[f'`{c}`',' → '.join(train['date_ranges'][c]),' → '.join(test['date_ranges'][c])] for c in ['tran_date_time','input_date_time']])
    quality+='\n\nВсе `tran_date_time` успешно разобраны, все названия `ngpt_route` соответствуют форме «число + трамвай». Файлы не отсортированы по времени события. Последовательность строк нельзя использовать как временной порядок.\n\n'
    metrics=[('begin_minus_tran_positive','Начало поездки позже события'),('begin_minus_tran_abs_gt_1d','Начало поездки отличается от события больше чем на сутки'),('input_minus_tran_negative','Загрузка раньше события'),('input_minus_tran_abs_gt_1d','Загрузка отличается от события больше чем на сутки'),('input_minus_tran_abs_gt_30d','Загрузка отличается от события больше чем на 30 суток')]
    quality+=table(['Проверка','train','test'],[[label,number(train['anomalies'][k]),number(test['anomalies'][k])] for k,label in metrics])
    quality+='\n\nПроверки пересекаются: строки «больше суток» и «больше 30 суток» нельзя складывать. Отсутствующие даты начала поездки в сравнения не входят.\n\n'
    for c in ['begin_date_time','input_date_time']:
        years=Counter(train['date_years'][c])+Counter(test['date_years'][c]); years.pop('',None)
        quality+=f"Для `{c}` встречаются годы от {min(years)} до {max(years)}. "
    quality+='Большие задержки загрузки сами по себе не доказывают ошибку каждого события, однако они и крайние значения делают служебные даты непригодной заменой `tran_date_time`. Небольшое положительное смещение начала поездки также может отражать округление времени; причины в файлах не описаны.\n\n'
    quality+='Форматы всех непустых временных значений разбираются стандартным парсером. Значит, основная проблема служебного времени здесь — смысл и достоверность значений, а не синтаксис. Поздняя загрузка также означает, что доступная сейчас история не подтверждает полноту сведений, которыми диспетчер располагал в момент события. Для оценки работы в реальном времени нужен отдельный контроль задержек источника.\n\n'
    quality+='### Повторы и идентификаторы\n\n'
    quality+=f"Всего {number(raw['combined_fields']['tran_no']['distinct'])} разных `tran_no` на {number(total)} строк. Значит, `tran_no` не является глобальным уникальным ключом. Проверка пары `(device_no, tran_no)` обнаружила {number(total-raw['device_tran_fingerprint_union']['distinct'])} повторных вхождений сверх первого. Эту пару тоже нельзя безоговорочно использовать как ключ события.\n\n"
    quality+=f"Повторы полных строк по отпечаткам: {number(train['row_fingerprint']['repeated_rows'])} в train, {number(test['row_fingerprint']['repeated_rows'])} в test; пересечение полных строк между файлами — {number(raw['row_fingerprint_union']['intersection'])}. Совпадение номера карты или транзакции не является основанием удалять запись.\n\n"
    quality+='### Категории событий\n\nКоды результата и их частоты во всех сырых строках, включая хвосты:\n\n'
    quality+=table(['`validation_result`','Событий','Доля всех событий'],[[k,number(v),percent(v,total,4)] for k,v in values['validation_result'].most_common()])
    quality+='\n\nКроме `1 = успех`, точная расшифровка кодов в наборе не дана.\n\n'
    quality+=table(['`tran_type_id`','Событий','Доля всех событий'],[[k,number(v),percent(v,total,3)] for k,v in values['tran_type_id'].most_common()])
    quality+='\n\nКрупнейшие категории `good_type` ниже посчитаны по всем попыткам, включая неуспешные. Названия в таблице очищены от внешних пробелов, частоты относятся к исходным категориям.\n\n'
    quality+=table(['Билетный продукт','Событий','Доля всех событий'],[[k.strip(),number(v),number(v/total*100,2)+' %'] for k,v in values['good_type'].most_common(10)])
    quality+='\n\nНепустые `place_id`: '+', '.join(f'`{k}` ({number(v)})' for k,v in values['place_id'].most_common() if k)+'. Эти коды описывают площадки/депо.\n'
    reconciliation='### Сверка разметки с сырыми CSV\n\n'
    rows=[]
    for s,d,end in [('train',train,'2025-09-01'),('test',test,'2025-11-01')]:
        a=agg[s]; tail=a[a.date.ge(end)]
        labels=pd.read_csv(ROOT/'dataset'/'labels'/('labels_day_'+s+'.csv'),sep=';')
        within=a[a.date.lt(end)].groupby(['route','date','hour']).boardings.sum()
        reference=labels.set_index(['route','date','hour']).boardings
        comparison=pd.concat([within.rename('raw'),reference.rename('label')],axis=1).fillna(0)
        mismatch=int((comparison.raw!=comparison.label).sum())
        rows.append([s,number(d['successful']),number(labels.boardings.sum()),number(tail.events.sum()),number(tail.boardings.sum()),number(mismatch)])
    reconciliation+=table(['Источник','Успехов во всём CSV','Посадок в labels','Событий за границей','Из них успехов','Расхождений внутри периода'],rows)
    reconciliation+='\n\nГраница train — начало 1 сентября, test — начало 1 ноября. За этими границами присутствуют короткие ночные хвосты. Важная деталь: **labels test не являются агрегатом только файла test.csv**. В них уже перенесены 523 успешные операции начала 1 сентября из train.csv — 15 почасовых ключей. Поэтому прямое сравнение test.csv со своим labels даёт 27 различий: 15 сентябрьских ключей есть только в labels, ещё 12 ноябрьских — только в raw.\n\n'
    sept_tail=agg['train'][agg['train'].date.ge('2025-09-01')]
    union=pd.concat(agg.values()).groupby(['route','date','hour']).boardings.sum()
    all_labels=pd.concat([pd.read_csv(ROOT/'dataset'/'labels'/('labels_day_'+s+'.csv'),sep=';') for s in ['train','test']]).set_index(['route','date','hour']).boardings
    union_history=union[union.index.get_level_values('date')<'2025-11-01']
    joined=pd.concat([union_history.rename('raw'),all_labels.rename('label')],axis=1).fillna(0)
    assert (joined.raw==joined.label).all()
    combined=pd.concat(agg.values())
    reconciliation_metrics=dict(raw_rows=total,raw_success=good,labels_sum=int(all_labels.sum()),calendar_reconciliation_differences=int((joined.raw!=joined.label).sum()),november_tail_success=int(combined.loc[combined.date.ge('2025-11-01'),'boardings'].sum()),november_tail_events=int(combined.loc[combined.date.ge('2025-11-01'),'events'].sum()),route50_no_events_sept21=bool(combined[combined.route.eq(50)&combined.date.eq('2025-09-21')].empty))
    (OUT/'reconciliation.json').write_text(json.dumps(reconciliation_metrics,indent=2),encoding='utf-8')
    reconciliation+='Правильный порядок: **объединить события, агрегировать по tran_date_time, затем разбить агрегаты по календарным периодам**. После исключения 1 ноября вся разметка января–октября совпадает по каждому ключу и по общей сумме: расхождений — 0.\n\n'
    reconciliation+='Проверяемые равенства:\n\n```text\ntrain labels = 46 913 233 - 523 = 46 912 710\ntest labels  = 12 754 520 - 562 + 523 = 12 754 481\nвсе labels   = 59 667 753 - 562 = 59 667 191\n```\n\n'
    reconciliation+='Операции начала 1 ноября из test не следует использовать как полный факт суток или как доступную октябрьскую историю при имитации прогноза, выпущенного на конец октября.\n'
    traffic='## 7. Объёмы и временная структура пассажиропотока\n\nВсе показатели этого раздела рассчитаны по **опубликованным labels за 01.01–31.10.2025**. Нули добавлены для отсутствующих календарных комбинаций. Это единая база сравнения без хвостов сырых CSV.\n\n### Маршруты\n\n'
    traffic+=table(['Маршрут','Посадки за 10 месяцев','Доля','Среднее в сутки','Дней с посадками','Максимум за час'],[[r['route'],number(r['total']),number(r['share_pct'],2)+' %',number(r['daily_average']),r['positive_days'],number(r['max_hour'])] for r in summary['routes']])
    traffic+='\n\nМаршрут № 17 даёт 23,63 % потока; вместе № 17, 12 и 11 — 54,27 %. Максимальный час — 5 827 посадок на № 17 22 октября с 08:00 до 08:59:59. Это сумма по всему маршруту, не загрузка одного вагона.\n\n'
    if len(route5):
        traffic+='По № 5 в исходном журнале найдены только следующие агрегаты попыток:\n\n'+table(['Файл','Дата','Час','Попыток','Успехов'],[[r.split,r.date,r.hour,number(r.events),number(r.boardings)] for r in route5.itertuples()])+'\n\n'
    traffic+='Нулевая история № 5 не доказывает отсутствие будущего спроса. Предсказывать для него ноль можно как отдельное явно указанное допущение, но качество такого решения невозможно подтвердить положительными наблюдениями этого маршрута.\n\n### Месячная динамика\n\n'
    traffic+=table(['Месяц 2025 года','Посадки','Дней','Среднее в сутки'],[[r['month'],number(r['total']),r['dates'],number(r['daily_average'])] for r in summary['monthly']])
    traffic+='\n\nСреднесуточный поток снижается с 216,7 тыс. в марте до 171,3 тыс. в августе и увеличивается до 213,1 тыс. в октябре. Изменение среднего учитывает разную длину месяцев, но не выравнивает их состав по дням недели. По единственному неполному году нельзя отделить устойчивую годовую сезонность от календарных эффектов, изменений сети и прочих причин.\n\n'
    traffic+='У маршрутов изменения неодинаковы: среднесуточный поток № 7 в июле ниже июня на 39,3 %, а № 50 выше на 26,6 %. Для № 17 апрель ниже марта на 15,3 %. Причина не установлена; это основание проверить изменения движения и качество источника, а не автоматически удалить наблюдения.\n\n### Дни недели и часы\n\n'
    traffic+=table(['День недели','Среднее число посадок в сутки, все маршруты'],[[name,number(summary['weekday_daily_average'][str(i)])] for i,name in enumerate(['Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье'])])
    traffic+='\n\nЗдесь дни недели определены календарно; официальные праздники и перенесённые рабочие дни отдельно не размечались. Среднее за понедельник–пятницу включает попавшие в них праздники.\n\n'
    traffic+='Для понедельника–пятницы видны два пика: около **20 805 посадок в 08:00–08:59** и **19 459 в 18:00–18:59** в среднем за соответствующий час по всем маршрутам. В субботу–воскресенье максимум смещается к **14:00–14:59 — 10 200 посадок**. В 02:00 и 03:00 суммарно за весь период всего 575 и 653 посадки соответственно. Ночные часы нужно включать в сетку, но нельзя заполнять средним дневным спросом.\n\n'
    traffic+='### Провалы и крайние значения\n\n'
    traffic+='У восьми активных маршрутов посадки есть во все 304 дня. У № 50 — в 303 дня: 21 сентября нет ни успешных, ни неуспешных операций в сырых файлах. 20 сентября посадок всего 304, тогда как 19 сентября — 21 977, 22 сентября — 24 136. Это локальный провал, который требует проверки операционного контекста и полноты выгрузки.\n\n'
    traffic+='Минимальный общий суточный поток в labels — 54 813 1 января; максимальный — 262 836 7 марта. Наличие экстремума само по себе не означает ошибку: подобные дни нельзя исправлять только по величине отклонения.\n'
    replacements={'MEASURED_OVERVIEW':overview,'FILE_INVENTORY':file_inventory,'RAW_QUALITY':quality,'LABEL_RECONCILIATION':reconciliation,'TRAFFIC_PROFILE':traffic}
    path=OUT/'DATASET_DESCRIPTION.md'
    doc=path.read_text()
    for marker,value in replacements.items():
        start=f'<!-- {marker} -->'; end=f'<!-- END_{marker} -->'
        if end in doc:
            before,remaining=doc.split(start,1); _,after=remaining.split(end,1)
            doc=before+start+'\n'+value+'\n'+end+after
        else:
            assert start in doc,marker
            doc=doc.replace(start,start+'\n'+value+'\n'+end)
    path.write_text(doc,encoding='utf-8')
    (OUT/'raw_profile.json').write_text(json.dumps(clean(raw),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(f'Wrote {path}: {len(doc):,} characters; {total:,} raw records; {good:,} successful events')


if __name__=='__main__':
    main()

#!/usr/bin/env python3
"""Run selected local analysis stages and publish a coherent reviewed-input bundle."""
from __future__ import annotations

import argparse
import copy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
import platform
import sys
import tempfile
import uuid

from check_terminology import check_payload, check_file

VERSION='local-analysis-runner-1.0'
REQUIRED=('borrower','loan','annual_statements','trial_balance','debt','statements')


def decode_json(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Повторяющийся ключ JSON: '+key)
            result[key]=value
        return result
    def constant(value):raise ValueError('Нечисловая константа JSON: '+value)
    return json.loads(raw,object_pairs_hook=unique,parse_constant=constant)


def encode(value):
    return json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n'


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_text(encode(value),encoding='utf-8')


def validate_attempt_target(path):
    if path.exists():
        try:previous=decode_json(path.read_text(encoding='utf-8'))
        except (OSError,ValueError,UnicodeError) as exc:raise UnsafePath('attempt.json уже занят посторонним исходным файлом') from exc
        if not isinstance(previous,dict) or previous.get('schema_version')!=VERSION or previous.get('status') not in {'success','failed','stopped'}:
            raise UnsafePath('attempt.json уже занят посторонним исходным файлом')


def atomic_json(path,value):
    path=Path(path)
    if path.name=='attempt.json':validate_attempt_target(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=path.parent,delete=False) as stream:
        stream.write(encode(value));temporary=stream.name
    os.replace(temporary,path)


class UnsafePath(ValueError):
    pass


def configured_paths(case):
    paths=[case.get('report_input')]+case.get('source_files',[])
    paths.extend(s['file'] for s in case.get('sources',[]) if isinstance(s,dict) and isinstance(s.get('file'),str))
    steps=case.get('steps',{})
    paths.extend(steps.get(key) for key in ('debt_register','loan_schedule'))
    flow=steps.get('cashflow',{})
    if isinstance(flow,dict):
        paths.append(flow.get('input'))
        if isinstance(flow.get('imports',[]),list):
            for item in flow.get('imports',[]):
                if isinstance(item,dict):paths.extend(item.get(k) for k in ('path','profile'))
    if isinstance(steps.get('pdf_extractions',[]),list):
        paths.extend(item.get('path') for item in steps.get('pdf_extractions',[]) if isinstance(item,dict))
    return [path for path in paths if isinstance(path,str)]


def protect_paths(case_path,work,case):
    validate_attempt_target(work/'attempt.json')
    paths=[case_path]+[case_path.parent/path for path in configured_paths(case)]
    if any(path.resolve().is_relative_to(work) for path in paths):
        raise UnsafePath('Входные файлы и объявленные источники должны находиться вне WORKDIR')
    if any((work/name).is_symlink() for name in ('current','backups','attempts','attempt.json','cache')):
        raise UnsafePath('Управляемые пути WORKDIR не могут быть символическими ссылками')


def nonempty(value,label):
    if not isinstance(value,str) or not value.strip():raise ValueError(label+': требуется непустая строка')


def source_ids(sources):
    if not isinstance(sources,list):raise ValueError('sources: требуется массив')
    ids=set()
    for source in sources:
        if not isinstance(source,dict):raise ValueError('source: требуется объект')
        for key in ('id','file','locator'):nonempty(source.get(key),'source.'+key)
        if source['id'] in ids:raise ValueError('sources: повтор идентификатора '+source['id'])
        ids.add(source['id'])
    return ids


def validate_case(case):
    required={'schema_version','case_id','mode','borrower','assessment_date','scope','sources','render_pdf','steps'}
    if not isinstance(case,dict) or required-set(case) or set(case)-required-{'intake','report_input','source_files'}:
        raise ValueError('case: отсутствующие или неподдерживаемые поля')
    if case['schema_version']!=VERSION:raise ValueError('Неподдерживаемая schema_version')
    if case['mode'] not in {'corporate','ip'}:raise ValueError('mode: нужен corporate или ip')
    nonempty(case['case_id'],'case_id');nonempty(case['scope'],'scope')
    borrower=case['borrower']
    if not isinstance(borrower,dict) or set(borrower)!={'name','inn'}:raise ValueError('borrower: нужны name и inn')
    nonempty(borrower['name'],'borrower.name')
    inn=borrower['inn']
    if not isinstance(inn,str) or not inn.isascii() or not inn.isdigit() or len(inn) not in ({12} if case['mode']=='ip' else {10,12}):
        raise ValueError('borrower.inn: нужен явный ИНН выбранного заёмщика')
    if not isinstance(case['assessment_date'],str) or date.fromisoformat(case['assessment_date']).isoformat()!=case['assessment_date']:
        raise ValueError('assessment_date: нужна дата YYYY-MM-DD')
    refs=source_ids(case['sources'])
    if type(case['render_pdf']) is not bool:raise ValueError('render_pdf: нужен логический признак')
    if case.get('report_input') is not None:nonempty(case['report_input'],'report_input')
    if case['render_pdf'] and not case.get('report_input'):raise ValueError('render_pdf требует report_input')
    if not isinstance(case['steps'],dict) or set(case['steps'])-{'cashflow','debt_register','loan_schedule','pdf_extractions'}:
        raise ValueError('steps: неподдерживаемые этапы')
    if not isinstance(case.get('source_files',[]),list):raise ValueError('source_files: требуется массив')
    for path in case.get('source_files',[]):nonempty(path,'source_files')
    if case['mode']=='corporate' and 'intake' in case:
        intake=case['intake']
        if not isinstance(intake,dict) or set(intake)-{'reviewed_by','checks'}:raise ValueError('intake: неверные поля')
        if 'reviewed_by' in intake:nonempty(intake['reviewed_by'],'intake.reviewed_by')
        checks=intake.get('checks',[])
        if not isinstance(checks,list):raise ValueError('intake.checks: требуется массив')
        seen=set()
        for item in checks:
            if not isinstance(item,dict) or set(item)-{'id','status','sources','borrower_request','creditor_question'}:
                raise ValueError('intake.check: неверные поля')
            if item.get('id') not in REQUIRED or item['id'] in seen:raise ValueError('intake: неизвестный или повторный пункт')
            seen.add(item['id'])
            if item.get('status') not in {'confirmed','missing','conflicting'}:raise ValueError('intake: неизвестный статус')
            links=item.get('sources',[])
            if not isinstance(links,list) or any(not isinstance(s,str) or s not in refs for s in links) or len(set(links))!=len(links) or item['status']=='confirmed' and not links:
                raise ValueError('intake: нужны действующие источники подтверждённого пункта')
            for key in ('borrower_request','creditor_question'):
                if key in item:nonempty(item[key],'intake.'+key)
            if item['id']=='loan' and 'borrower_request' in item:raise ValueError('Условия нового займа относятся к вопросам кредитору')


def intake_gaps(case):
    intake=case.get('intake',{})
    if not intake.get('reviewed_by'):
        return [{'id':'analyst_review','status':'unreviewed','message':'Требуется передать результат проверки обязательного минимума аналитиком.'}]
    checks={item['id']:item for item in intake.get('checks',[])}
    gaps=[]
    for key in REQUIRED:
        if checks.get(key,{}).get('status')=='confirmed':continue
        item=copy.deepcopy(checks.get(key,{'id':key,'status':'unreviewed'}))
        if not item.get('borrower_request') and not item.get('creditor_question'):
            item['message']='Требуется уточнить у аналитика конкретный пробел, счёт, период и сторону запроса.'
        gaps.append(item)
    return gaps


def stopped(case,work,gaps,case_path,case_raw):
    relative=Path('attempts')/uuid.uuid4().hex;directory=work/relative;directory.mkdir(parents=True)
    borrower=[g['borrower_request'] for g in gaps if g.get('borrower_request')]
    creditor=[g['creditor_question'] for g in gaps if g.get('creditor_question')]
    text='# Проверка обязательного минимума\n\n'+'\n\n'.join('- '+g['id']+': '+g['status']+'\n  '+g.get('message','Пробел указан аналитиком.')+'\n  '+'\n  '.join(g[key] for key in ('borrower_request','creditor_question') if g.get(key)) for g in gaps)+'\n'
    (directory/'intake.md').write_text(text,encoding='utf-8')
    if borrower:
        request='Добрый день! Чтобы завершить финансовую проверку '+case['borrower']['name']+', просим дополнить документы:\n\n'+'\n'.join(str(i)+'. '+v for i,v in enumerate(borrower,1))+'\n\nСпасибо!\n'
        (directory/'request-to-borrower.txt').write_text(request,encoding='utf-8')
    if creditor:
        (directory/'questions-to-creditor.txt').write_text('\n'.join(str(i)+'. '+v for i,v in enumerate(creditor,1))+'\n',encoding='utf-8')
    for path in directory.iterdir():check_file(path)
    snapshot=Path('snapshots/000-case.json');(directory/snapshot).parent.mkdir();(directory/snapshot).write_bytes(case_raw)
    manifest={'schema_version':VERSION,'status':'stopped','stage':'intake','case_id':case['case_id'],
              'borrower':case['borrower'],'assessment_date':case['assessment_date'],'scope':case['scope'],
              'inputs':[{'path':str(case_path),'sha256':hashlib.sha256(case_raw).hexdigest(),'snapshot':snapshot.as_posix()}],
              'tools':tool_versions(),'gaps':gaps}
    manifest['artifacts']={path.relative_to(directory).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(directory.rglob('*')) if path.is_file()}
    write_json(directory/'manifest.json',manifest);check_file(directory/'manifest.json')
    result={'schema_version':VERSION,'status':'stopped','stage':'intake','gaps':gaps,'attempt_dir':relative.as_posix()}
    atomic_json(work/'attempt.json',result)
    return result


class Inputs:
    def __init__(self,base,stage,work):
        self.base=base;self.stage=stage;self.work=work;self.records=[];self.seen={};self.source_paths={}

    def resolve(self,path):
        source=Path(path)
        return (self.base/source).resolve() if not source.is_absolute() else source.resolve()

    def capture(self,source,raw):
        source=self.resolve(source)
        if source.is_relative_to(self.work):raise UnsafePath('Входные файлы должны находиться вне рабочего каталога: '+str(source))
        if source not in self.seen:
            relative=Path('snapshots')/(str(len(self.records)).zfill(3)+'-'+source.name)
            destination=self.stage/relative;destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(raw)
            self.seen[source]=raw
            self.records.append({'path':str(source),'sha256':hashlib.sha256(raw).hexdigest(),'snapshot':relative.as_posix()})
        return self.seen[source]

    def read(self,path):
        source=self.resolve(path)
        return self.seen[source] if source in self.seen else self.capture(source,source.read_bytes())

    def protect_sources(self,sources,origin):
        if not isinstance(sources,list):return
        for source in sources:
            if isinstance(source,dict) and isinstance(source.get('file'),str):
                path=Path(source['file'])
                resolved=(self.resolve(origin).parent/path).resolve() if not path.is_absolute() else path.resolve()
                if resolved.is_relative_to(self.work):raise UnsafePath('Объявленный исходный документ находится внутри WORKDIR: '+str(resolved))
                if isinstance(source.get('id'),str):
                    old=self.source_paths.get(source['id'])
                    if old is not None and old!={'source':source,'path':resolved}:raise ValueError('Различающееся происхождение source ID '+source['id'])
                    self.source_paths[source['id']]={'source':copy.deepcopy(source),'path':resolved}
        verify_versions(sources,self)

    def json(self,path):
        return decode_json(self.read(path).decode('utf-8-sig'))


def report_context(case,report):
    if not isinstance(report,dict):raise ValueError('report_input: требуется объект')
    expected={'1.0'} if case['mode']=='corporate' else {'ip-investor-3.0','ip-investor-3.1'}
    if report.get('schema_version') not in expected:raise ValueError('report_input не соответствует выбранному порядку')
    name=report.get('company',{}).get('name') if case['mode']=='corporate' else report.get('borrower_name')
    if name!=case['borrower']['name']:raise ValueError('report_input: наименование заёмщика отличается от case')
    if case['mode']=='ip' and report.get('inn')!=case['borrower']['inn']:raise ValueError('report_input: ИНН отличается от case')
    if report.get('assessment_date')!=case['assessment_date']:raise ValueError('report_input: дата проверки отличается от case')


class StageFailure(ValueError):
    def __init__(self,result):
        self.details=result
        super().__init__('Этап вернул ошибку: '+encode(result.get('issues') or result.get('audit',{}).get('issues') or result.get('errors') or result.get('unresolved') or result.get('unresolved_matches') or 'см. детали'))


def merge_sources(left,right):
    source_ids(left);source_ids(right)
    result=copy.deepcopy(left);by_id={source['id']:source for source in result}
    for source in right:
        if source['id'] in by_id:
            if by_id[source['id']]!=source:raise ValueError('Разное происхождение source ID '+source['id'])
        else:
            result.append(copy.deepcopy(source));by_id[source['id']]=source
    return result


def patch_report(report,patch,sources):
    report['sources']=merge_sources(report.get('sources',[]),sources)
    patch=copy.deepcopy(patch)
    report['sources']=merge_sources(report['sources'],patch.pop('sources',[]))
    def update(target,values):
        for key,value in values.items():
            if isinstance(value,dict) and isinstance(target.get(key),dict):update(target[key],value)
            else:target[key]=copy.deepcopy(value)
    update(report,patch)


def save_stage(stage,name,result,stages,success=None):
    write_json(stage/'stages'/(name+'.json'),result)
    if success is False or result.get('ok') is False:raise StageFailure(result)
    stages.append({'name':name,'status':'completed'})


def observed_source(source,inputs):
    origin=inputs.source_paths.get(source['id'])
    raw=inputs.seen.get(origin['path'] if origin is not None else inputs.resolve(source['file']))
    return {'source':copy.deepcopy(source),'observed_sha256':hashlib.sha256(raw).hexdigest() if raw is not None else None}


def verify_versions(sources,inputs,loads=()):
    by_id={source['id']:source for source in sources if isinstance(source,dict) and isinstance(source.get('id'),str)}
    claims=[(source['id'],source['sha256']) for source in by_id.values() if 'sha256' in source]
    claims.extend((load.get('source'),load.get('sha256')) for load in loads if isinstance(load,dict))
    for sid,expected in claims:
        if sid not in by_id:continue
        if not isinstance(expected,str) or len(expected)!=64 or any(c not in '0123456789abcdefABCDEF' for c in expected):raise ValueError('Неверный объявленный SHA256 источника '+sid)
        actual=observed_source(by_id[sid],inputs)['observed_sha256']
        if actual is not None and expected.lower()!=actual:raise ValueError('Объявленный SHA256 источника '+sid+' расходится с реально выбранной версией; обновите подготовленные данные и решения')


def decision_contexts(flow,decisions,case,inputs):
    from check_cashflow import date_value, operation_fingerprint
    rows={row['id']:row for row in flow['operations']}
    accounts={account['account']:account for account in flow['accounts']}
    sources={source['id']:source for source in flow['sources']}
    previous=inputs.work/'current/stages/transfers.json'
    old=[]
    if previous.exists():
        manifest=json.loads((inputs.work/'current/manifest.json').read_text(encoding='utf-8'))
        if manifest.get('artifacts',{}).get('stages/transfers.json')!=hashlib.sha256(previous.read_bytes()).hexdigest():
            raise ValueError('Нарушена целостность сохранённого контекста решений, восстановите согласованный предыдущий комплект')
        old=json.loads(previous.read_text(encoding='utf-8')).get('runner_decision_contexts',[])
    old_contexts={item['signature']:item['context_sha256'] for item in old}
    contexts=[]
    for decision in decisions:
        ids=decision.get('operation_ids',[])
        if len(ids)!=2 or any(uid not in rows for uid in ids):continue
        stable={key:copy.deepcopy(decision[key]) for key in ('pair_id','reason','evidence') if key in decision}
        stable['operation_ids']=sorted(ids)
        stable['reason']=' '.join(str(stable.get('reason','')).split())
        evidence=stable.get('evidence',{})
        if isinstance(evidence,dict):
            if isinstance(evidence.get('sources'),list):evidence['sources']=sorted(evidence['sources'])
            if evidence.get('operation_date'):evidence['operation_date']=date_value(evidence['operation_date']).isoformat()
        signature=hashlib.sha256(encode(stable).encode()).hexdigest()
        relevant={rows[uid]['source'] for uid in ids}
        if isinstance(evidence,dict) and isinstance(evidence.get('sources'),list):relevant.update(evidence['sources'])
        context={'borrower_inn':case['borrower']['inn'],
                 'fingerprints':{uid:operation_fingerprint(rows[uid]) for uid in sorted(ids)},
                 'accounts':[accounts[key] for key in sorted({rows[uid]['account'] for uid in ids})],
                 'sources':[observed_source(sources[key],inputs) for key in sorted(relevant) if key in sources]}
        digest=hashlib.sha256(encode(context).encode()).hexdigest()
        if signature in old_contexts and old_contexts[signature]!=digest:
            raise ValueError('Изменён контекст прежнего решения о переводе: повторно сверьте владельцев и источники, обновите основание и снимки операций')
        contexts.append({'signature':signature,'context_sha256':digest,'operation_ids':sorted(ids)})
    return contexts


def execute_steps(case,inputs,stage,state,stages,report):
    from calculate_cashflow import calculate
    from import_statement_csv import import_statement
    from reconcile_statement_overlaps import reconcile
    from match_own_transfers import match
    outputs={}
    if 'pdf_extractions' in case['steps']:
        from extract_pdf_cached import extract
        selections=case['steps']['pdf_extractions']
        if not isinstance(selections,list):raise ValueError('pdf_extractions: требуется массив')
        results=[]
        for index,item in enumerate(selections,1):
            state['stage']='pdf_extraction_'+str(index)
            if not isinstance(item,dict) or {'path','language','ocr'}-set(item) or set(item)-{'path','language','ocr','dpi','min_text_chars'}:raise ValueError('PDF: нужны path, language, ocr и поддерживаемые параметры')
            raw=inputs.read(item['path'])
            result=extract(inputs.resolve(item['path']),inputs.work/'cache',**{key:value for key,value in item.items() if key!='path'})
            if result['source']['sha256']!=hashlib.sha256(raw).hexdigest():raise ValueError('PDF изменился между фиксацией версии и извлечением')
            if not result['complete']:save_stage(stage,state['stage'],result,stages,success=False)
            text=Path(result['text_path']).read_text(encoding='utf-8')
            relative=Path('stages')/(state['stage']+'.txt');(stage/relative).parent.mkdir(parents=True,exist_ok=True);(stage/relative).write_text(text,encoding='utf-8')
            result['cache_text_path']=result['text_path'];result['text_path']=relative.as_posix()
            result['source']['snapshot']=next(record['snapshot'] for record in inputs.records if record['path']==str(inputs.resolve(item['path'])))
            save_stage(stage,state['stage'],result,stages);results.append(result)
        outputs['pdf_extractions']=results
    if 'cashflow' in case['steps']:
        state['stage']='cashflow'
        cfg=case['steps']['cashflow']
        if not isinstance(cfg,dict) or set(cfg)-{'input','imports','overlaps','transfers'}:raise ValueError('cashflow: неподдерживаемая настройка')
        imports=cfg.get('imports',[])
        if not isinstance(imports,list):raise ValueError('cashflow.imports: требуется массив')
        if not cfg.get('input') and len(imports)!=1:raise ValueError('Для нескольких импортов нужен input с явным общим периодом, счетами и охватом')
        flow=inputs.json(cfg['input']) if cfg.get('input') else None
        if flow is not None:inputs.protect_sources(flow.get('sources',[]),cfg['input'])
        loads=[]
        for index,item in enumerate(imports,1):
            state['stage']='import_'+str(index)
            if not isinstance(item,dict) or set(item)!={'path','profile'}:raise ValueError('import: нужны path и profile')
            raw=inputs.read(item['path']);profile=inputs.json(item['profile']);inputs.protect_sources(profile.get('control_sources',[]),item['profile'])
            imported=import_statement(inputs.resolve(item['path']),profile)
            if imported['source']['sha256']!=hashlib.sha256(raw).hexdigest():raise ValueError('CSV изменился между фиксацией версии и импортом')
            save_stage(stage,state['stage'],imported,stages)
            if flow is None:flow=copy.deepcopy(imported['cashflow'])
            else:
                flow['sources']=merge_sources(flow['sources'],imported['cashflow']['sources'])
                accounts={a['account']:a for a in flow['accounts']}
                for account in imported['cashflow']['accounts']:
                    if account['account'] in accounts and accounts[account['account']]!=account:raise ValueError('Противоречащие сведения о владельце счёта')
                    if account['account'] not in accounts:flow['accounts'].append(copy.deepcopy(account));accounts[account['account']]=account
                flow['operations']+=copy.deepcopy(imported['cashflow']['operations'])
                coverages=[flow['coverage'],imported['cashflow']['coverage']]
                complete=all(c['complete'] for c in coverages)
                if any(not c['complete'] and 'missing_months' not in c for c in coverages):flow['coverage']={'complete':False}
                else:
                    missing=sorted({month for c in coverages for month in c.get('missing_months',[])})
                    flow['coverage']={'complete':complete,**({'missing_months':missing} if missing else {})}
            for load in imported['loads']:
                previous=next((old for old in loads if old['id']==load['id']),None)
                if previous is not None and previous!=load:raise ValueError('Противоречащая версия загрузки')
                if previous is None:loads.append(copy.deepcopy(load))
        state['stage']='cashflow'
        if date.fromisoformat(flow['period']['end'])>date.fromisoformat(case['assessment_date']):raise ValueError('Период банковских операций позже даты проверки')
        if 'overlaps' in cfg:
            state['stage']='overlaps';settings=cfg['overlaps']
            if not isinstance(settings,dict) or set(settings)-{'loads','decisions'}:raise ValueError('overlaps: нужны loads и decisions')
            for load in settings.get('loads',[]):
                previous=next((old for old in loads if old['id']==load['id']),None)
                if previous is not None and previous!=load:raise ValueError('Противоречащая версия загрузки')
                if previous is None:loads.append(copy.deepcopy(load))
            verify_versions(flow['sources'],inputs,loads)
            overlapping=reconcile({'schema_version':'local-statement-overlaps-1.0','cashflow':flow,'loads':loads,'decisions':settings.get('decisions',[])})
            save_stage(stage,'overlaps',overlapping,stages);flow=overlapping['registry'];outputs['overlaps']=overlapping
        if 'transfers' in cfg:
            state['stage']='transfers';settings=cfg['transfers']
            if not isinstance(settings,dict) or set(settings)-{'external_accounts','decisions'}:raise ValueError('transfers: нужны external_accounts и decisions')
            matched=match({'schema_version':'local-own-transfers-1.0','cashflow':flow,'external_accounts':settings.get('external_accounts',[]),'decisions':settings.get('decisions',[])})
            matched['runner_decision_contexts']=decision_contexts(flow,matched['confirmed_pairs'],case,inputs)
            save_stage(stage,'transfers',matched,stages);flow=matched['cashflow_input'];outputs['transfers']=matched
        state['stage']='cashflow';result=calculate(flow);save_stage(stage,'cashflow',result,stages);outputs['cashflow']=result
        if report is not None and case['mode']=='ip':
            if not result['pdf_compatible']:raise StageFailure(result)
            patch_report(report,result['pdf_patch'],result['sources'])
    if 'debt_register' in case['steps']:
        from summarize_debt_register import summarize
        state['stage']='debt_register';raw=inputs.json(case['steps']['debt_register']);inputs.protect_sources(raw.get('sources',[]),case['steps']['debt_register'])
        if raw.get('borrower_id')!=case['borrower']['inn']:raise ValueError('debt_register.borrower_id должен совпадать с ИНН выбранного заёмщика')
        if raw.get('report_date','')>case['assessment_date']:raise ValueError('Дата реестра обязательств позже даты проверки')
        result=summarize(raw);save_stage(stage,'debt_register',result,stages);outputs['debt_register']=result
        if report is not None and case['mode']=='ip':
            report['bki']=copy.deepcopy(result['pdf_patch']['bki'])
            patch_report(report,result['pdf_patch'],[])
    if 'loan_schedule' in case['steps']:
        from calculate_loan_schedule import calculate as calculate_loan
        from decimal import Decimal
        state['stage']='loan_schedule';raw=inputs.json(case['steps']['loan_schedule']);inputs.protect_sources(raw.get('sources',[]),case['steps']['loan_schedule'])
        if report is not None and raw.get('issue_date','')<case['assessment_date']:raise ValueError('Дата выдачи нового займа раньше даты проверки')
        result=calculate_loan(raw);save_stage(stage,'loan_schedule',result,stages);outputs['loan_schedule']=result
        if report is not None and case['mode']=='corporate':patch_report(report,result['corporate_patch'],result['sources'])
        elif report is not None:
            for field,value in [('principal',raw['amount']),('term_months',raw['term_months'])]:
                fact=report.get('loan',{}).get(field)
                if fact and fact.get('value') is not None and Decimal(str(fact['value']))!=Decimal(value):raise ValueError('График нового займа расходится с loan.'+field+' формы ИП')
    return outputs


def tool_versions():
    from importlib.metadata import version,PackageNotFoundError
    def installed(name):
        try:return version(name)
        except PackageNotFoundError:return None
    scripts=Path(__file__).parent
    return {'python':platform.python_version(),'pypdf':installed('pypdf'),'reportlab':installed('reportlab'),
            'scripts':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(scripts.iterdir()) if p.suffix in {'.py','.swift'}}}


def _run(case_path,work,state):
    from pypdf import PdfReader
    from render_dashboard import render as render_company
    from render_ip_dashboard import render as render_ip
    case_path=Path(case_path).resolve();work=Path(work).resolve()
    case_raw=case_path.read_bytes();case=decode_json(case_raw.decode('utf-8-sig'));check_payload(case);validate_case(case)
    protect_paths(case_path,work,case)
    work.mkdir(parents=True,exist_ok=True)
    if (work/'current').exists():
        previous=json.loads((work/'current/manifest.json').read_text(encoding='utf-8'))
        if previous.get('case_id')!=case['case_id'] or previous.get('borrower',{}).get('inn')!=case['borrower']['inn']:
            raise ValueError('WORKDIR относится к другой проверке или заёмщику')
    stages=[]
    if case['mode']=='corporate':
        state['stage']='intake'
        gaps=intake_gaps(case)
        if gaps:return stopped(case,work,gaps,case_path,case_raw)
        stages.append({'name':'intake','status':'confirmed'})
    with tempfile.TemporaryDirectory(prefix='.stage-',dir=work) as temporary:
        stage=Path(temporary)/'bundle';stage.mkdir();inputs=Inputs(case_path.parent,stage,work);inputs.capture(case_path,case_raw)
        try:
            for source in case.get('source_files',[]):inputs.read(source)
            report=None
            if case.get('report_input'):
                state['stage']='report_context'
                report=inputs.json(case['report_input']);inputs.protect_sources(report.get('sources',[]),case['report_input']);report_context(case,report)
            outputs=execute_steps(case,inputs,stage,state,stages,report)
            if report is not None:
                state['stage']='report'
                write_json(stage/'input.json',report)
                if case['render_pdf']:
                    renderer=render_company if case['mode']=='corporate' else render_ip
                    analysis=renderer(report,stage/'report.pdf')
                else:
                    from analyze import analyze
                    from ip_investor_report import prepare
                    analysis=analyze(report) if case['mode']=='corporate' else prepare(report)
                write_json(stage/'analysis.json',analysis);stages.append({'name':'report','status':'completed'})
            state['stage']='terminology'
            for path in stage.rglob('*'):
                if path.is_file() and 'snapshots' not in path.relative_to(stage).parts:check_file(path)
            stages.append({'name':'terminology','status':'passed'})
            pdf=None
            if case['render_pdf']:
                state['stage']='pdf_check'
                pages=len(PdfReader(stage/'report.pdf').pages)
                if pages!=1:raise ValueError('PDF должен состоять из одной страницы')
                stages.append({'name':'pdf_check','status':'passed'})
                pdf={'pages':pages,'visual_review':'pending','visual_review_required':True}
            tools=tool_versions()
            if 'pdf_extractions' in outputs:tools['pdf_extractions']=[result['tools'] for result in outputs['pdf_extractions']]
            generation=hashlib.sha256(encode({'inputs':inputs.records,'tools':tools}).encode()).hexdigest()
            manifest={'schema_version':VERSION,'case_id':case['case_id'],'borrower':case['borrower'],
                      'assessment_date':case['assessment_date'],'mode':case['mode'],'scope':case['scope'],
                      'run_id':generation,'inputs':inputs.records,'tools':tools,'stages':stages,
                      'data_status':'limited' if (outputs.get('cashflow',{}).get('complete') is False or 'debt_register' in outputs and outputs['debt_register']['totals']['total']['value'] is None or case['mode']=='ip' and report is not None and (report.get('bank_scope',{}).get('incomplete') or not report.get('loan',{}).get('term_months') or report['loan']['term_months'].get('value') is None)) else 'prepared',
                      'pdf':pdf}
            manifest['artifacts']={path.relative_to(stage).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(stage.rglob('*')) if path.is_file()}
            write_json(stage/'manifest.json',manifest)
            state['stage']='publish'
            current=work/'current';backup=None
            if current.exists():
                backup=Path('backups')/uuid.uuid4().hex;(work/backup).parent.mkdir(exist_ok=True);current.rename(work/backup)
            result={'schema_version':VERSION,'status':'success','run_id':generation,'current':'current','backup':backup.as_posix() if backup else None}
            try:
                stage.rename(current)
                atomic_json(work/'attempt.json',result)
            except BaseException:
                if current.exists():current.rename(stage)
                if backup:(work/backup).rename(current)
                raise
            return result
        except Exception as exc:
            if not isinstance(exc,UnsafePath):
                relative=Path('attempts')/uuid.uuid4().hex
                try:
                    shutil.copytree(stage,work/relative)
                    write_json(work/relative/'manifest.json',{'schema_version':VERSION,'status':'failed','stage':state['stage'],
                               'case_id':case['case_id'],'inputs':inputs.records,'tools':tool_versions(),'stages':stages})
                    state['attempt_dir']=relative.as_posix()
                except OSError as retention_error:
                    state['retention_error']=str(retention_error)
            raise


def run(case_path,work):
    state={'stage':'case'}
    try:
        return _run(case_path,work,state)
    except Exception as exc:
        result={'schema_version':VERSION,'status':'failed','stage':state['stage'],'message':str(exc)}
        if isinstance(exc,StageFailure):result['details']=exc.details
        for key in ('attempt_dir','retention_error'):
            if key in state:result[key]=state[key]
        work=Path(work).resolve()
        if not isinstance(exc,UnsafePath) and work.exists() and not Path(case_path).resolve().is_relative_to(work):
            atomic_json(work/'attempt.json',result)
        return result



def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case',type=Path);parser.add_argument('workdir',type=Path);args=parser.parse_args(argv)
    try:
        result=run(args.case,args.workdir)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        result={'schema_version':VERSION,'status':'failed','stage':'case','message':str(exc)}
    print(encode(result),end='')
    return 0 if result['status']=='success' else 2


if __name__=='__main__':raise SystemExit(main())

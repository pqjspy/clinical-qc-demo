#!/usr/bin/env python3
"""Freeze, then one explicit local evaluation; no retries or result overwrite."""
import argparse
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

from clinical_qc_demo.contracts import CaseRecord
from clinical_qc_demo.data import load_knowledge, load_records, build_model_input
from clinical_qc_demo.local_model import OllamaClient
from clinical_qc_demo.m4_workflow import analyze_record, OPTIONS
from clinical_qc_demo.m4_evaluation import score
from clinical_qc_demo.workflow import digest

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data/evaluation/m4_v1'
INFERENCE_FILES=['contracts.py','data.py','local_model.py','retrieval.py','workflow.py','m2_contracts.py','pk_check.py','m4_engine.py','m4_workflow.py','m4_evaluation.py']

def write(path,obj):
    with path.open('x',encoding='utf-8') as f:
        json.dump(obj,f,ensure_ascii=False,indent=2)
        f.write('\n')

def hashes():
    files=[ROOT/'src/clinical_qc_demo'/n for n in INFERENCE_FILES]+[Path(__file__),DATA/'inputs.json',DATA/'references.json',DATA/'PROTOCOL.md']
    files+=sorted((ROOT/'data/knowledge').glob('*.json'))
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--freeze',action='store_true')
    parser.add_argument('--demo',action='store_true')
    args=parser.parse_args()
    if args.demo:
        out=ROOT/'runtime/evaluations'/('m4-demo-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f'))
        out.mkdir(parents=True)
        for r in load_records(ROOT):
            result=analyze_record(ROOT,r)
            write(out/(r.case_id+'.json'),result)
            print(r.case_id,result['status'],[(i['family'],i['status']) for i in result['issues']],flush=True)
        print('展示运行目录：',out)
        return
    if args.freeze:
        records=[CaseRecord.model_validate(x) for x in json.loads((DATA/'inputs.json').read_text())]
        refs=json.loads((DATA/'references.json').read_text())
        # Check independent authoring anchors/counts, not model predictions.
        score(records,refs,{})
        t,p,r=load_knowledge(ROOT)
        knowledge=dict(taxonomy=t.model_dump(),protocols=p.model_dump(),rules=r.model_dump())
        manifest=dict(version='m4-v1',frozen_at=datetime.now(timezone.utc).isoformat(),files=hashes(),
                      model=OllamaClient(options=OPTIONS).identity(),knowledge=knowledge,knowledge_sha256=digest(knowledge),
                      input_count=len(records),reference_policy='answers_read_by_scorer_only_after_predictions')
        write(DATA/'manifest.json',manifest)
        print('已冻结18条合成评测；未执行预测。')
        return
    manifest=json.loads((DATA/'manifest.json').read_text())
    if hashes()!=manifest['files']:
        raise SystemExit('冻结文件已变化，拒绝冒充同一次评测。')
    if OllamaClient(options=OPTIONS).identity()!=manifest['model']:
        raise SystemExit('模型或参数与冻结版本不同。')
    out=ROOT/'runtime/evaluations/m4-v1'
    out.mkdir(parents=True,exist_ok=True)
    write(out/'started.json',dict(manifest_sha256=digest(manifest),started_at=datetime.now(timezone.utc).isoformat()))
    records=[CaseRecord.model_validate(x) for x in json.loads((DATA/'inputs.json').read_text())]
    predictions={}; interrupted=None; invalid=None; attempted=[]
    try:
        for i,record in enumerate(records,1):
            if hashes()!=manifest['files']: raise RuntimeError('评测期间冻结文件变化，停止。')
            print(f'{i}/{len(records)} {record.case_id} 开始真实本机运行',flush=True)
            attempted.append(record.case_id)
            result=analyze_record(ROOT,record,knowledge_snapshot=manifest['knowledge'])
            predictions[record.case_id]=result
            write(out/(record.case_id+'.json'),result)
            identity = result.get('model_identity')
            # An unavailable model is a counted workflow failure, not evidence
            # that a different model produced a prediction.
            missing_identity_failure = identity is None and result.get('status') == 'analysis_failed' and not result.get('model_calls')
            if (identity != manifest['model'] and not missing_identity_failure) or result.get('knowledge_sha256') != manifest['knowledge_sha256'] or result['input_sha256'] != digest(build_model_input(record)):
                invalid='运行身份/知识/输入与冻结清单不一致，不发布有效成绩。'
                raise RuntimeError(invalid)
            print(record.case_id,result['status'],round(result['elapsed_ms']/1000,1),'秒',flush=True)
    except (Exception,KeyboardInterrupt) as exc:
        interrupted=str(exc) or type(exc).__name__
    if hashes()!=manifest['files']:
        invalid='冻结文件发生变化，不能加载新参考并冒充原评测。'
    for cid in attempted:
        if cid not in predictions:
            predictions[cid]={'status':'analysis_failed','run_id':None,'risk':'待定','triage_priority':'priority','review_required':True,'issues':[], 'error':{'type':'aborted','message':interrupted},'elapsed_ms':None}
    if invalid:
        write(out/'invalid.json',dict(reason=invalid,attempted=attempted,planned=len(records),interrupted=interrupted))
        raise SystemExit(invalid)
    # Labels are loaded only after predictions; no reference object enters analyzer.
    refs=json.loads((DATA/'references.json').read_text())
    report=score(records,refs,predictions)
    if hashes()!=manifest['files']:
        write(out/'invalid.json',dict(reason='评分期间冻结文件变化，拒绝发布成绩。',attempted=attempted,planned=len(records)))
        raise SystemExit('评分期间冻结文件变化，拒绝发布成绩。')
    report.update(version='m4-v1',mode='live_local',manifest_sha256=digest(manifest),interrupted=interrupted,
                  frozen_files_unchanged=True,model=manifest['model'],
                  finished_at=datetime.now(timezone.utc).isoformat())
    write(out/'report.json',report)
    print(json.dumps({k:report[k] for k in ('planned','attempted','metrics','interrupted')},ensure_ascii=False,indent=2))

if __name__=='__main__': main()

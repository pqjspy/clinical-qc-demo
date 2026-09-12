"""Offline scoring. Never imported by the analyzer; labels stay outside prompts."""
from collections import Counter

LABELS = ['L3-PK-001','L3-VISIT-001','L3-AE-001','L3-DRUG-001','L3-ROLE-001','L3-EDC-001']


def rate(n, d):
    return n / d if d else None


def score(records, references, predictions):
    ids=[r.case_id for r in records]; ref_ids=[r['case_id'] for r in references]
    if len(ids)!=len(set(ids)) or len(ref_ids)!=len(set(ref_ids)) or ids!=ref_ids or not set(predictions)<=set(ids):
        raise ValueError('Inputs/references must have exact unique ordered IDs; predictions cannot add IDs.')
    rows=[]; totals=Counter(); classes={k:Counter() for k in LABELS}
    for record, reference in zip(records, references):
        if record.case_id != reference['case_id']:
            raise ValueError('Reference order/ID mismatch.')
        result=predictions.get(record.case_id)
        hard_failed = result is not None and result['status']=='analysis_failed'
        issues=[] if result is None or hard_failed else result.get('issues',[])
        matched=set(); all_correct=True; details=[]
        for gold in reference['issues']:
            if record.text.count(gold['anchor']) != 1:
                raise ValueError('Gold anchor must be unique in input.')
            start=record.text.index(gold['anchor']); end=start+len(gold['anchor'])
            candidates=[(n,i) for n,i in enumerate(issues) if n not in matched and any(
                e['origin']=='record' and e.get('clause_id') in i['clause_ids'] and e['start_char'] <= start and end <= e['end_char'] for e in i['evidence'])]
            pair=candidates[0] if candidates else None
            n, item=pair if pair else (None,None)
            if pair: matched.add(n)
            emitted=item['findings'] if item else []
            wanted=gold['l3_id']
            right_label=bool(wanted and any(f['l3_id']==wanted for f in emitted))
            if wanted:
                classes[wanted]['support']+=1
                classes[wanted]['tp' if right_label else 'fn']+=1
                totals['gold_findings']+=1
            matched_label=False
            for f in emitted:
                if f['l3_id']==wanted and not matched_label:
                    matched_label=True
                else:
                    classes[f['l3_id']]['fp']+=1
            if gold['risk']=='高':
                totals['high_gold']+=1
                totals['high_missed']+=not right_label
                totals['high_underestimated']+=bool(right_label and not any(f['l3_id']==wanted and f['risk']=='高' for f in emitted))
            correct=bool(item and item['family']==gold['family'] and item['status']==gold['status'] and
                         item['risk']==gold['risk'] and [f['l3_id'] for f in emitted]==([wanted] if wanted else []))
            all_correct &= correct
            details.append(dict(anchor=gold['anchor'],expected=gold,predicted_issue_id=item['issue_id'] if item else None,
                                predicted_status=item['status'] if item else None,correct=correct))
        for n,i in enumerate(issues):
            if n not in matched:
                for f in i['findings']: classes[f['l3_id']]['fp']+=1
        complete=all_correct and len(matched)==len(issues) and result is not None and not hard_failed
        priority=bool(result and result.get('triage_priority')=='priority')
        if reference['priority']:
            totals['priority_gold']+=1; totals['priority_caught']+=priority
        else:
            totals['routine_gold']+=1; totals['unnecessary_priority']+=priority
        if any(g['risk']=='高' for g in reference['issues']):
            totals['high_records']+=1; totals['high_records_transferred']+=priority
        if len(reference['issues'])>1:
            totals['multi_records']+=1; totals['multi_complete']+=complete
        failed=hard_failed or bool(result and result.get('explanation_error'))
        gold_findings=[g for g in reference['issues'] if g['l3_id']]
        gold_unresolved=[g for g in reference['issues'] if g['status'] not in ('proposed_findings','no_finding_for_checked_rule')]
        expected_status=('partial_review_required' if gold_findings else (gold_unresolved[0]['status'] if len(reference['issues'])==1 else 'needs_information')) if gold_unresolved else ('proposed_findings' if gold_findings else 'no_finding_for_checked_rule')
        risk_order={'低':1,'中':2,'高':3}
        expected_risk=max((g['risk'] for g in gold_findings),key=risk_order.get,default='待定' if gold_unresolved else '不适用')
        aggregate_ok=bool(result and result['status']==expected_status and result['risk']==expected_risk and result.get('review_required') is True)
        exact=complete and priority==reference['priority'] and not failed and aggregate_ok
        totals['exact']+=exact; totals['complete']+=complete
        totals['attempted']+=result is not None; totals['hard_failed']+=hard_failed
        totals['explanation_failed']+=bool(result and result.get('explanation_error'))
        totals['review_required']+=bool(result and result.get('review_required'))
        for item in issues:
            for e in item['evidence']:
                totals['emitted_evidence']+=1
                if e['origin']=='record':
                    valid=record.text[e['start_char']:e['end_char']]==e['quote']
                else:
                    rules=result['knowledge_snapshot']['rules']['rules']
                    valid=any(r['rule_id']+'@'+r['version']==e['rule_key'] and r['source']['quote']==e['quote'] and
                              r['source']['document_id']==e['document_id'] and r['source']['section_id']==e['section_id'] for r in rules)
                    valid=valid and e['rule_key'] in item.get('retrieval',{}).get('applicable_rule_keys',[])
                totals['valid_evidence']+=valid
        rows.append(dict(case_id=record.case_id,scenario_group=reference['scenario_group'],text=record.text,
                         run_id=result.get('run_id') if result else None,status=result.get('status') if result else 'unattempted',
                         elapsed_ms=result.get('elapsed_ms') if result else None,exact=exact,
                         issues=details,extra_issue_count=len(issues)-len(matched),
                         error=result.get('error') if result else None,explanation_error=result.get('explanation_error') if result else None))
    per_class=[]
    for label,c in classes.items():
        f1=rate(2*c['tp'],2*c['tp']+c['fp']+c['fn']) or 0.0
        per_class.append(dict(label=label,support=c['support'],tp=c['tp'],fp=c['fp'],fn=c['fn'],f1=f1))
    tp=sum(c['tp'] for c in classes.values());fp=sum(c['fp'] for c in classes.values());fn=sum(c['fn'] for c in classes.values())
    times=sorted(r['elapsed_ms']/1000 for r in rows if r['elapsed_ms'] is not None)
    return dict(synthetic=True,scope='frozen_synthetic_workflow_not_clinical_accuracy',planned=len(records),
                attempted=totals['attempted'],unattempted=len(records)-totals['attempted'],counts=dict(totals),
                metrics=dict(micro_f1=rate(2*tp,2*tp+fp+fn),macro_f1=sum(c['f1'] for c in per_class)/len(LABELS),
                             finding_recall=rate(tp,tp+fn),exact_record_rate=rate(totals['exact'],len(records)),
                             multi_complete_rate=rate(totals['multi_complete'],totals['multi_records']),
                             high_risk_miss_rate=rate(totals['high_missed'],totals['high_gold']),
                             high_risk_underestimate_rate=rate(totals['high_underestimated'],totals['high_gold']),
                             high_record_priority_routing_recall=rate(totals['high_records_transferred'],totals['high_records']),
                             priority_recall=rate(totals['priority_caught'],totals['priority_gold']),
                             unnecessary_priority_rate=rate(totals['unnecessary_priority'],totals['routine_gold']),
                             structural_evidence_validity=rate(totals['valid_evidence'],totals['emitted_evidence'])),
                mean_seconds=sum(times)/len(times) if times else None, per_class=per_class,rows=rows,
                limitations=['同作者合成，共享有限语法；不是严格模板独立泛化测试',
                             '分类结果由Qwen分项与规则共同产生，不是独立LLM分类能力',
                             '证据结构有效不证明解释语义正确；语义groundedness未独立评估',
                             '所有预测和失败保留；展示案例不进入这18条分母'])

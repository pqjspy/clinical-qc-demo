"""M4 software tests, not model/clinical evaluation. Author-controlled routing."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from uuid import uuid4

from clinical_qc_demo.data import load_records, load_knowledge
from clinical_qc_demo.m4_engine import *
from clinical_qc_demo.m4_workflow import analyze_record, validate_drafts, Drafts, decode_routing
from clinical_qc_demo.web_store import Store

ROOT = Path(__file__).resolve().parents[1]

class EngineTests(unittest.TestCase):
    def setUp(self):
        self.records = {r.case_id:r for r in load_records(ROOT)}
        self.t, self.p, self.rules = load_knowledge(ROOT)

    def check(self, cid, family, replace=None):
        record = self.records[cid]
        if replace:
            a,b=replace
            record=record.model_copy(update={'text':record.text.replace(a,b)})
        parts=clauses(record.text)
        ctx=[p['clause_id'] for p in parts if context_only(p['quote'])]
        group=IssueGroup(family=family,clause_ids=[p['clause_id'] for p in parts if p['clause_id'] not in ctx])
        return evaluate_issue(record,group,ctx,parts,self.rules,self.t,1)

    def test_six_demonstration_families(self):
        for cid,fam in [('QC002','pk'),('DEMO-VISIT-01','visit'),('DEMO-AE-01','ae'),('DEMO-DRUG-01','drug'),('DEMO-ROLE-01','role'),('DEMO-EDC-01','edc')]:
            with self.subTest(cid=cid):
                x=self.check(cid,fam)
                self.assertEqual(x['status'],'proposed_findings',x)
                self.assertEqual(len(x['findings']),1)
                for e in x['evidence']:
                    if e['origin']=='record':
                        self.assertEqual(self.records[cid].text[e['start_char']:e['end_char']],e['quote'])

    def test_pk_equality_conflict_and_missing(self):
        self.assertEqual(self.check('DEMO-BOUNDARY-01','pk')['status'],'no_finding_for_checked_rule')
        self.assertEqual(self.check('DEMO-CONFLICT-01','pk')['status'],'rule_conflict')
        self.assertNotIn(self.check('DEMO-MISSING-01','pk')['status'],RESOLVED)
        self.assertEqual(self.check('DEMO-AMBIGUOUS-01','pk')['status'],'ambiguous_evidence')

    def test_visit_inclusive_and_day_one(self):
        x=self.check('DEMO-VISIT-01','visit',('2026-08-01','2026-08-03'))
        self.assertEqual(x['calculation']['actual_day'],35)
        self.assertEqual(x['status'],'no_finding_for_checked_rule')

    def test_register_absence_not_missing(self):
        self.assertEqual(self.check('DEMO-AE-01','ae',('未找到该事件的对应登记','已找到该事件的对应登记'))['status'],'no_finding_for_checked_rule')
        self.assertNotIn(self.check('DEMO-AE-01','ae',('已核对截至当日12:00的完整AE登记台账','未提供完整AE登记台账'))['status'],RESOLVED)

    def test_inventory_no_missing_zero(self):
        x=self.check('DEMO-DRUG-01','drug',('期末盘点108','期末盘点110'))
        self.assertEqual(x['status'],'no_finding_for_checked_rule',x)
        self.assertNotIn(self.check('DEMO-DRUG-01','drug',('入库20','入库未记录'))['status'],RESOLVED)
        self.assertNotIn(self.check('DEMO-DRUG-01','drug',('入库20','入库-20'))['status'],RESOLVED)

    def test_authorization_end_exclusive(self):
        x=self.check('DEMO-ROLE-01','role',('终点为2026-09-01 00:00','终点为2026-09-01 09:00'))
        self.assertEqual(x['status'],'proposed_findings',x)
        x=self.check('DEMO-ROLE-01','role',('终点为2026-09-01 00:00','终点为2026-09-02 00:00'))
        self.assertEqual(x['status'],'no_finding_for_checked_rule',x)

    def test_edc_alignment_and_equal_values(self):
        self.assertEqual(self.check('DEMO-EDC-01','edc',('38.2','37.2'))['status'],'no_finding_for_checked_rule')
        self.assertEqual(self.check('DEMO-EDC-01','edc',('38.2摄氏度','38.2华氏度'))['status'],'ambiguous_evidence')
        self.assertNotIn(self.check('DEMO-EDC-01','edc',('均已对齐','尚未对齐'))['status'],RESOLVED)

    def test_new_assertion_not_ignored(self):
        x=self.check('QC002','pk',('开始离心时间为10:33','开始离心时间为10:33但另一样本为10:40'))
        self.assertNotIn(x['status'],RESOLVED)

    def test_coverage_rejects_drop_duplicate_and_business_context(self):
        parts=clauses(self.records['DEMO-VISIT-01'].text)
        for obj in [dict(issues=[dict(family='visit',clause_ids=[1])],context_clause_ids=[]),
                    dict(issues=[dict(family='visit',clause_ids=[1,1,2,3])],context_clause_ids=[4,5]),
                    dict(issues=[dict(family='visit',clause_ids=[3])],context_clause_ids=[1,2,4,5])]:
            with self.assertRaises(ValueError): validate_groups(parts,Decomposition.model_validate(obj))

    def test_multi_and_partial_summary(self):
        pk=self.check('QC002','pk')
        ae=self.check('DEMO-AE-01','ae')
        ae['issue_id']='I2'
        s=summarize([pk,ae])
        self.assertEqual(len(s['findings']),2)
        self.assertEqual(s['risk'],'高')
        missing=self.check('DEMO-MISSING-01','pk')
        s=summarize([ae,missing])
        self.assertEqual(s['status'],'partial_review_required')
        self.assertEqual(s['unresolved_count'],1)
        self.assertEqual(s['triage_priority'],'priority')

    def test_scope_and_rule_versions(self):
        self.assertEqual(self.check('DEMO-UNKNOWN-01','unknown')['status'],'out_of_scope')
        record=self.records['QC002'].model_copy(update={'protocol_id':'SYN-PROTOCOL-001-v2','event_at':'2026-10-01T10:33:00+08:00','text':self.records['QC002'].text.replace('2026-09-01','2026-10-01')})
        self.records['VERSION']=record
        self.assertEqual(self.check('VERSION','pk')['status'],'no_finding_for_checked_rule')

    def test_citations_do_not_cross_issue(self):
        item=self.check('QC002','pk')
        with self.assertRaises(ValueError):
            validate_drafts(Drafts(items=[dict(issue_id='I1',explanation='错误[I2-F1][I1-R1]')]),[item])

    def test_router_family_local_instance_ids_and_complete_coverage(self):
        raw={'2':{'family':'pk','group':1},'3':{'family':'ae','group':1},'4':{'family':'pk','group':2}}
        p=decode_routing(raw,['2','3','4'],[1])
        self.assertEqual([(i.family,i.clause_ids) for i in p.issues],[('pk',[2]),('ae',[3]),('pk',[4])])
        with self.assertRaises(ValueError): decode_routing(raw,['2','3','4','5'],[1])
        raw['3']['family']='invented'
        with self.assertRaises(ValueError): decode_routing(raw,['2','3','4'],[1])

    def test_negation_swallowed_tail_and_mixed_entities_rejected(self):
        for tail in ['未发生任何不良事件','恶心且同一份PK样本采血于10:06开始离心于10:33']:
            self.assertNotIn(self.check('DEMO-AE-01','ae',('报告恶心','报告'+tail))['status'],RESOLVED)
        self.assertNotIn(self.check('QC002','pk',('开始离心时间','SYN-SUBJ-999开始离心时间'))['status'],RESOLVED)
        for token in ('SYN-subj-999', 'SYN-999'):
            self.assertNotIn(self.check('DEMO-BOUNDARY-01','pk',('开始离心于10:36',token+'开始离心于10:36'))['status'],RESOLVED)
        self.assertEqual(self.check('QC002','pk',('10:33','25:99'))['status'],'ambiguous_evidence')

    def test_dates_and_register_cutoff_checked(self):
        for cid,fam in [('DEMO-AE-01','ae'),('DEMO-DRUG-01','drug'),('DEMO-EDC-01','edc')]:
            for bad in ['2026-10-01','2026-99-99']:
                self.assertNotIn(self.check(cid,fam,('2026-09-01',bad))['status'],RESOLVED)
        self.assertNotIn(self.check('DEMO-AE-01','ae',('09:00','13:00'))['status'],RESOLVED)

    def test_label_tails_cannot_hide_second_issue(self):
        for cid,fam,old,new in [('DEMO-DRUG-01','drug','单位均为片','单位均为片且另有AE登记漏记'),
                                ('DEMO-EDC-01','edc','采集的体温','采集的体温但其实两侧受试者不同'),
                                ('DEMO-ROLE-01','role','执行采血','执行采血且另有AE登记漏记')]:
            self.assertNotIn(self.check(cid,fam,(old,new))['status'],RESOLVED)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        from clinical_qc_demo.contracts import CaseRecord
        self.records=[CaseRecord.model_validate(x) for x in json.loads((ROOT/'data/evaluation/m4_v1/inputs.json').read_text())]
        self.refs=json.loads((ROOT/'data/evaluation/m4_v1/references.json').read_text())

    def test_all_empty_keeps_full_denominators(self):
        from clinical_qc_demo.m4_evaluation import score
        r=score(self.records,self.refs,{})
        self.assertEqual((r['planned'],r['unattempted'],r['counts']['gold_findings']),(18,18,12))
        self.assertEqual(r['counts']['high_gold'],4)
        self.assertEqual(r['counts']['priority_gold'],9)
        self.assertEqual(r['metrics']['micro_f1'],0)
        self.assertIsNone(r['metrics']['structural_evidence_validity'])

    def test_missing_duplicate_reference_rejected(self):
        from clinical_qc_demo.m4_evaluation import score
        with self.assertRaises(ValueError): score(self.records,self.refs[:-1],{})
        with self.assertRaises(ValueError): score(self.records,self.refs[:-1]+[self.refs[0]],{})

    def test_wrong_aggregate_not_exact(self):
        from clinical_qc_demo.m4_evaluation import score
        t,p,r=load_knowledge(ROOT); record=self.records[0]; parts=clauses(record.text)
        ctx=[p['clause_id'] for p in parts if context_only(p['quote'])]
        i=evaluate_issue(record,IssueGroup(family='pk',clause_ids=[p['clause_id'] for p in parts if p['clause_id'] not in ctx]),ctx,parts,r,t,1)
        result=dict(**summarize([i]),knowledge_snapshot={'rules':r.model_dump()},run_id='test',elapsed_ms=1)
        result.update(status='no_finding_for_checked_rule',risk='不适用',review_required=False)
        report=score(self.records[:1],self.refs[:1],{'E001':result})
        self.assertFalse(report['rows'][0]['exact'])


class PersistenceTests(unittest.TestCase):
    setUp = EngineTests.setUp
    check = EngineTests.check
    def test_failed_unpartitioned_run_can_only_be_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for folder in ('data/knowledge','data/inputs','examples'):
                shutil.copytree(ROOT/folder,root/folder)
            store=Store(root)
            jid,_=store.create_job('QC002','reviewer',str(uuid4()))
            store.finish_job(jid,result=dict(run_id=str(uuid4()),schema_version='m4-v1',status='analysis_failed',issues=[],findings=[],risk='待定',triage_priority='priority',review_required=True))
            for action in ('confirm','edit'):
                with self.assertRaises(ValueError):
                    store.review(jid,dict(issue_id=None,action=action,reason='软件测试，非临床审核',attested=True,expected_revision=0,request_key=str(uuid4()),final=None),'reviewer')
            store.review(jid,dict(issue_id=None,action='return',reason='分项失败转人工，未判正常',attested=True,expected_revision=0,request_key=str(uuid4()),final=None),'reviewer')
            self.assertEqual(store.detail(jid)['alerts'][-1]['state'],'transferred')

    def test_multi_review_keeps_other_issue_open_and_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for folder in ('data/knowledge','data/inputs','examples'):
                shutil.copytree(ROOT/folder,root/folder)
            store=Store(root)
            jid,_=store.create_job('QC002','reviewer',str(uuid4()))
            a=self.check('QC002','pk'); b=self.check('DEMO-AE-01','ae'); b['issue_id']='I2'
            a['explanation_draft']={'text':'软件测试草稿'};b['explanation_draft']={'text':'软件测试草稿'}
            result=dict(schema_version='m4-v1',run_id=str(uuid4()),knowledge_snapshot=store.active_bundle()['knowledge'],**summarize([a,b]))
            store.finish_job(jid,result=result)
            before=store.detail(jid)
            def payload(i,rev):
                return dict(issue_id=i,action='confirm',reason='软件测试，非人工临床审核',attested=True,expected_revision=rev,request_key=str(uuid4()),final=None)
            with self.assertRaises(ValueError): store.review(jid,payload(None,0),'reviewer')
            store.review(jid,payload('I1',0),'reviewer')
            self.assertEqual(store.detail(jid)['alerts'][-1]['state'],'pending')
            store.review(jid,payload('I2',1),'reviewer')
            self.assertEqual(store.detail(jid)['alerts'][-1]['state'],'closed')
            self.assertEqual(store.detail(jid)['result_sha256'],before['result_sha256'])
            self.assertEqual(Store(root).detail(jid)['result'],before['result'])

if __name__=='__main__': unittest.main()

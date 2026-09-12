"""Offline software checks; authored routing here is NOT a model evaluation."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import unittest
sys.path.insert(0,str(Path(__file__).parent/'src'))
from bundled import BUNDLE
from cloud_flow import validate_record, analyze, decode, unpack, split_citation_groups, MODEL, RULES, TAXONOMY
from core.m4_engine import clauses, context_only, IssueGroup, evaluate_issue
from review import review_payload

ROOT=Path(__file__).parent
class Tests(unittest.TestCase):
    def test_original_core_unchanged(self):
        for name,h in BUNDLE['implementation_sha256'].items():
            self.assertEqual(hashlib.sha256((ROOT/'src'/'core'/name).read_bytes()).hexdigest(),h)

    def test_twelve_case_software_contracts(self):
        families={'QC002':'pk','DEMO-VISIT-01':'visit','DEMO-AE-01':'ae','DEMO-DRUG-01':'drug',
                  'DEMO-ROLE-01':'role','DEMO-EDC-01':'edc','DEMO-BOUNDARY-01':'pk',
                  'DEMO-MISSING-01':'pk','DEMO-CONFLICT-01':'pk','DEMO-UNKNOWN-01':'unknown','DEMO-AMBIGUOUS-01':'pk'}
        expected={'DEMO-BOUNDARY-01':'no_finding_for_checked_rule','DEMO-MISSING-01':'ambiguous_evidence',
                  'DEMO-CONFLICT-01':'rule_conflict','DEMO-UNKNOWN-01':'out_of_scope','DEMO-AMBIGUOUS-01':'ambiguous_evidence'}
        for raw in BUNDLE['cases']:
            with self.subTest(case=raw['case_id']):
                record=validate_record(raw); parts=clauses(record.text)
                ctx=[p['clause_id'] for p in parts if context_only(p['quote'])]
                if raw['case_id']=='DEMO-MULTI-01':
                    by={'pk':[],'ae':[]}
                    for p in parts:
                        if p['clause_id'] not in ctx:
                            by['pk' if 'PK' in p['quote'] or '离心' in p['quote'] else 'ae'].append(p['clause_id'])
                else: by={families[raw['case_id']]:[p['clause_id'] for p in parts if p['clause_id'] not in ctx]}
                for index,(family,ids) in enumerate(by.items(),1):
                    out=evaluate_issue(record,IssueGroup(family=family,clause_ids=ids),ctx,parts,RULES,TAXONOMY,index)
                    self.assertEqual(out['status'],expected.get(raw['case_id'],'proposed_findings'),out['reason'])
                    for e in out['evidence']:
                        if e['origin']=='record': self.assertEqual(record.text[e['start_char']:e['end_char']],e['quote'])

    def result(self,fail_draft=False):
        record=validate_record(BUNDLE['cases'][0]); calls=[]
        async def chat(model,payload):
            calls.append(payload)
            schema=payload['response_format']['json_schema']
            if len(calls)==1:
                result={k:{'family':'pk','group':1} for k in schema['required']}
            else:
                if fail_draft: raise TimeoutError()
                result={'I1':'同一份样本记录的采血和离心间隔为27分钟[I1-F2]，低于至少30分钟的合成规则要求[I1-R1]，需人工复核。'}
            return {'model':model,'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(result)}}]}
        out=asyncio.run(analyze(record,'test',chat))
        self.assertEqual(len(calls),2)
        return out

    def test_workflow_and_failed_explanation_keeps_findings(self):
        out=self.result(); self.assertEqual(out['status'],'proposed_findings',out)
        out=self.result(True); self.assertEqual(out['status'],'partial_review_required'); self.assertEqual(len(out['findings']),1)

    def test_review_immutability_and_validation(self):
        result=self.result(); before=copy.deepcopy(result)
        raw={'issue_id':'I1','action':'confirm','reason':'已核对证据','attested':True,'expected_revision':0,'request_key':'x'*16,'final':None}
        self.assertEqual(review_payload(result,raw)['final']['origin'],'human_confirmed_original')
        self.assertEqual(result,before)
        for field,value in [('attested',1),('attested',False),('issue_id','I9'),('expected_revision',True)]:
            with self.subTest(field=field,value=value),self.assertRaises(ValueError): review_payload(result,{**raw,field:value})
        with self.assertRaises(ValueError): review_payload(self.result(True),raw)

    def test_json_and_provider_fail_closed(self):
        self.assertEqual(split_citation_groups('依据[I1-F2, I1-F3]。'),'依据[I1-F2][I1-F3]。')
        self.assertEqual(split_citation_groups('[I1-F2-F3]'),'[I1-F2-F3]')
        for text in ('{"a":1,"a":2}','{"a":NaN}','```json\n{}\n```'):
            with self.assertRaises(ValueError): decode(text)
        with self.assertRaises(ValueError): unpack({'choices':[{'finish_reason':'length','message':{'role':'assistant','content':'{}'}}]})
        with self.assertRaises(ValueError): validate_record({**BUNDLE['cases'][0],'text':'患者真实病历'})

    def test_sql_atomic_quota_and_ownership(self):
        db=sqlite3.connect(':memory:'); db.execute('PRAGMA foreign_keys=ON')
        db.executescript((ROOT/'migrations/0001_public.sql').read_text())
        db.executescript((ROOT/'migrations/0002_review_state.sql').read_text())
        db.executescript((ROOT/'migrations/0003_return_state.sql').read_text())
        for v in ('a','b'): db.execute('INSERT INTO visitors VALUES(?,?,?,?,?,?,?)',(v,v,'csrf',v,'2026-09-11','t','expires'))
        args=('job','a','key','QC002','{}','ip','2026-09-11','2026-09-11T00:00:00.000Z','bundle','completed','done','{}')
        db.execute('INSERT INTO jobs(id,visitor_id,request_key,case_id,input,ip_hash,day,created_at,bundle_id,state,stage,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',args)
        with self.assertRaises(sqlite3.IntegrityError): db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',('job','b',1,'k','h','b','t','{}'))
        db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',('job','a',1,'k','h','a','t','{}'))
        with self.assertRaises(sqlite3.IntegrityError): db.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',('job','a',1,'k2','h','a','t','{}'))
        self.assertEqual(db.execute('SELECT revision FROM jobs WHERE id="job"').fetchone()[0],1)
        for n in range(1,6): db.execute('INSERT INTO jobs(id,visitor_id,request_key,case_id,input,ip_hash,day,created_at,bundle_id,state,stage,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(str(n),*args[1:2],str(n),*args[3:]))
        with self.assertRaises(sqlite3.IntegrityError): db.execute('INSERT INTO jobs(id,visitor_id,request_key,case_id,input,ip_hash,day,created_at,bundle_id,state,stage,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('extra','a','extra',*args[3:]))

if __name__=='__main__': unittest.main(verbosity=2)

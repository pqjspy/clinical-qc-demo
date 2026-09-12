"""Scoped HTTP smoke test. --live is explicit, makes at most 3 real analyses."""
import argparse
import copy
import http.cookiejar
import json
from pathlib import Path
import sys
import socket
import urllib.request
import urllib.error
import uuid

p=argparse.ArgumentParser(); p.add_argument('--url',required=True); p.add_argument('--live',action='store_true'); p.add_argument('--resolve',help='Diagnostic IP from verified authoritative DNS; retains TLS hostname/certificate verification'); args=p.parse_args()
BASE=args.url.rstrip('/')
if not (BASE.startswith('http://127.0.0.1:') or BASE.startswith('https://clinical-qc-public-demo.')): raise SystemExit('Unexpected deployment target')
if args.resolve:
    import ipaddress
    from urllib.parse import urlsplit
    ipaddress.ip_address(args.resolve)
    hostname=urlsplit(BASE).hostname; original=socket.getaddrinfo
    def resolve(host,port,*rest,**kwargs):
        return original(args.resolve if host==hostname else host,port,*rest,**kwargs)
    socket.getaddrinfo=resolve
class Client:
    def __init__(self):
        self.jar=http.cookiejar.CookieJar(); self.csrf=''
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(self.jar))
    def call(self,path,data=None,expected=200,csrf=None):
        headers={'User-Agent':'ClinicalQCDeploymentCheck/1.0','Origin':BASE,'Content-Type':'application/json','X-QC-CSRF':self.csrf if csrf is None else csrf}
        request=urllib.request.Request(BASE+path,data=None if data is None else json.dumps(data).encode(),headers=headers)
        try: response=self.opener.open(request,timeout=80)
        except urllib.error.HTTPError as e: response=e
        body=response.read().decode()
        assert response.status==expected,(path,response.status,body[:700])
        return json.loads(body)
    def enter(self):
        user=self.call('/api/session',{'synthetic_only':True}); self.csrf=user['csrf']
a=Client(); b=Client()
assert a.call('/api/health')['ok']
a.call('/api/cases',expected=401)
a.enter(); b.enter()
seed=next(x for x in a.call('/api/cases') if x['case_id']=='QC002')
mine={**seed,'case_id':'SYN-CLOUD-SMOKE-'+uuid.uuid4().hex[:8]}
a.call('/api/cases',mine,expected=403,csrf='invalid')
a.call('/api/cases',mine,expected=201)
assert any(x['case_id']==mine['case_id'] for x in a.call('/api/cases'))
assert not any(x['case_id']==mine['case_id'] for x in b.call('/api/cases'))
a.call('/api/drafts',{},expected=404)
assert a.call('/api/library')['read_only']
print('PASS: health, CSRF, independent visitors, own record saved/reopened, public rules read-only',flush=True)
if args.live:
    for cid in (mine['case_id'],'DEMO-MULTI-01','DEMO-BOUNDARY-01'):
        key=str(uuid.uuid4()); body={'case_id':cid,'request_key':key}
        job=a.call('/api/jobs',body); jid=job['id']
        detail=a.call('/api/jobs/'+jid); result=detail['result']
        assert result and result['mode']=='live_cloud' and result['model_call_count']>=1,result
        assert result['status']!='analysis_failed',result.get('error')
        assert result['coverage']['exact_partition'] is True
        b.call('/api/jobs/'+jid,expected=404); b.call('/api/jobs/'+jid+'/export',expected=404)
        assert a.call('/api/jobs',body)['id']==jid
        issue=result['issues'][0]; f=issue['findings'][0] if issue['findings'] else None
        raw={'issue_id':issue['issue_id'],'action':'edit','reason':'部署验证：已核对原文、适用规则与程序计算。',
             'attested':True,'expected_revision':0,'request_key':str(uuid.uuid4()),'final':{
                'status':issue['status'],'l3_id':f['l3_id'] if f else None,'risk':issue['risk'],
                'suggested_decision':f['suggested_decision'] if f else '本项未命中',
                'explanation':issue['reason'],'suggested_action':f['suggested_action'] if f else '保留记录，其他检查另行核对。'}}
        a.call('/api/jobs/'+jid+'/reviews',raw,expected=201)
        assert a.call('/api/jobs/'+jid+'/reviews',raw)['revision']==1
        b.call('/api/jobs/'+jid+'/reviews',raw,expected=404)
        fresh=a.call('/api/jobs/'+jid)
        assert fresh['result_sha256']==detail['result_sha256'] and len(fresh['reviews'])==1
        assert fresh['result']==result
        if cid==mine['case_id']: assert float(issue['calculation']['actual_minutes'])==27
        if cid=='DEMO-MULTI-01': assert len(result['issues'])==2 and result['risk']=='高'
        if cid=='DEMO-BOUNDARY-01': assert issue['status']=='no_finding_for_checked_rule'
        print(json.dumps({'case':cid,'status':result['status'],'issues':len(result['issues']),
            'elapsed_ms':result['elapsed_ms'],'model_calls':result['model_call_count'],
            'usage':[c.get('usage') for c in result['model_calls']], 'persisted_review':True},ensure_ascii=False),flush=True)
    print('PASS: live cloud AI, 27/30-minute boundary, two issues, evidence, review append/reopen, immutable initial result, foreign access denied')

"""Visitor-isolated synthetic demo. No local accounts or public admin routes."""
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from workers import WorkerEntrypoint, Response
from js import Object
from pyodide.ffi import to_js
from bundled import BUNDLE
from cloud_flow import MODEL, KNOWLEDGE, KNOWLEDGE_HASH, BUNDLE_ID, canonical, digest, now, validate_record, analyze, decode
from review import review_payload

SEEDS={c['case_id']:c for c in BUNDLE['cases']}
COOKIE='qc_guest'
HEADERS={'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store',
    'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
    'Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"}

class HttpError(Exception):
    def __init__(self,status,message): self.status,self.message=status,message

def native(x): return x.to_py() if hasattr(x,'to_py') else x
def token_hash(s): return hashlib.sha256(s.encode()).hexdigest()
def reply(data,status=200,headers=None):
    return Response(canonical(data),status=status,headers={**HEADERS,**(headers or {})})
def check_key(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9-]{16,100}',value): raise HttpError(400,'请求标识无效。')
    return value
def cutoff(): return (datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat(timespec='milliseconds').replace('+00:00','Z')

class Default(WorkerEntrypoint):
    async def sql(self,query,*args,first=False):
        statement=self.env.DB.prepare(query).bind(*args)
        if first: return native(await statement.first())
        return native(await statement.all()).get('results',[])

    async def body(self,request):
        if not (request.headers.get('content-type') or '').lower().startswith('application/json'):
            raise HttpError(415,'需要JSON请求。')
        if request.body is None: raise HttpError(400,'请求内容为空。')
        reader=request.body.getReader(); chunks=[]; size=0
        while True:
            part=await reader.read()
            if part.done: break
            chunk=bytes(native(part.value)); size+=len(chunk)
            if size>40000:
                await reader.cancel()
                raise HttpError(413,'请求过长。')
            chunks.append(chunk)
        try: data=decode(b''.join(chunks).decode('utf-8'))
        except Exception: raise HttpError(400,'无效JSON。')
        if not isinstance(data,dict): raise HttpError(400,'需要JSON对象。')
        return data

    async def visitor(self,request,required=True):
        value=next((p.strip().split('=',1)[1] for p in (request.headers.get('cookie') or '').split(';') if p.strip().startswith(COOKIE+'=')),None)
        row=None
        if value and re.fullmatch(r'[a-f0-9]{64}',value):
            row=await self.sql('SELECT * FROM visitors WHERE token_hash=? AND expires_at>?',token_hash(value),now(),first=True)
        if not row and required: raise HttpError(401,'访客会话已失效，请重新进入体验。')
        return row

    async def identity(self,v):
        count=await self.sql('SELECT COUNT(*) AS n FROM jobs WHERE visitor_id=? AND day=?',v['id'],now()[:10],first=True)
        return {'name':'访客-'+v['id'][:6],'role':'reviewer','csrf':v['csrf'],
                'expires_at':v['expires_at'],'remaining':max(0,6-count['n']),'model':MODEL}

    async def own_job(self,vid,jid):
        row=await self.sql('SELECT * FROM jobs WHERE id=? AND visitor_id=?',jid,vid,first=True)
        if not row: raise HttpError(404,'找不到该运行，或不属于当前访客。')
        return row

    async def detail(self,v,jid):
        row=await self.own_job(v['id'],jid)
        reviews=await self.sql('SELECT revision,actor,created_at,payload FROM reviews WHERE job_id=? AND visitor_id=? ORDER BY revision',jid,v['id'])
        for r in reviews: r['payload']=json.loads(r['payload'])
        alerts=await self.sql('SELECT state,actor,reason,created_at FROM alerts WHERE job_id=? AND visitor_id=? ORDER BY id',jid,v['id'])
        if row['state']=='running' and row['created_at']<cutoff():
            row.update(state='failed',stage='interrupted',error='运行被中断；不会自动重试或产生通过结论。')
        return {'job':{k:row[k] for k in ('id','case_id','created_at','bundle_id','state','stage','error')},
                'result':json.loads(row['result']) if row['result'] else None,
                'result_sha256':row['result_sha256'],'reviews':reviews,'alerts':alerts}

    async def fetch(self,request):
        try: return await self.route(request)
        except HttpError as exc: return reply({'detail':exc.message},exc.status)
        except Exception as exc:
            message=str(exc)
            if any(k in message for k in ('_quota','analysis_busy')):
                return reply({'detail':'免费演示额度已用尽或当前有分析正在运行。请稍后或明日再试，不会转为付费。'},429)
            if any(k in message for k in ('UNIQUE constraint','review_conflict')):
                return reply({'detail':'记录或复核版本发生冲突，请重新打开后提交。'},409)
            if isinstance(exc,ValueError): return reply({'detail':message[:350]},400)
            return reply({'detail':'云端服务暂不可用。请保留输入并稍后重试；没有自动调用付费服务。'},503)

    async def route(self,request):
        url=urlsplit(str(request.url)); path=url.path; method=str(request.method)
        if path=='/api/health' and method=='GET':
            return reply({'ok':True,'version':'cloud-v1','synthetic_only':True,'model':MODEL,'rules':len(KNOWLEDGE['rules']['rules'])})
        if not path.startswith('/api/'): return Response('Not found',status=404)
        body=None
        if method=='POST':
            if request.headers.get('origin')!=url.scheme+'://'+url.netloc: raise HttpError(403,'仅允许本站发起操作。')
            body=await self.body(request)
        elif method!='GET': raise HttpError(405,'不支持该操作。')
        if path=='/api/session' and method=='POST':
            if body!={'synthetic_only':True}: raise HttpError(400,'请确认只使用虚拟数据。')
            v=await self.visitor(request,required=False)
            if v: return reply(await self.identity(v))
            t=now(); token=secrets.token_hex(32); vid=secrets.token_hex(16)
            ip_hash=token_hash(t[:10]+'|'+(request.headers.get('CF-Connecting-IP') or 'local'))
            expires=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat(timespec='milliseconds').replace('+00:00','Z')
            v=dict(id=vid,csrf=secrets.token_hex(24),expires_at=expires)
            await self.sql('INSERT INTO visitors(id,token_hash,csrf,ip_hash,day,created_at,expires_at) VALUES(?,?,?,?,?,?,?)',
                vid,token_hash(token),v['csrf'],ip_hash,t[:10],t,expires)
            secure='; Secure' if url.scheme=='https' else ''
            return reply(await self.identity(v),headers={'Set-Cookie':f'{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800{secure}'})
        v=await self.visitor(request)
        if method=='POST' and not secrets.compare_digest(request.headers.get('X-QC-CSRF') or '',v['csrf']):
            raise HttpError(403,'安全令牌已失效，请重新打开页面。')
        if path=='/api/me' and method=='GET': return reply(await self.identity(v))
        if path=='/api/library' and method=='GET':
            return reply({'id':BUNDLE_ID,'sha256':KNOWLEDGE_HASH,'knowledge':KNOWLEDGE,'read_only':True})
        if path=='/api/cases':
            if method=='GET':
                rows=await self.sql('SELECT payload FROM cases WHERE visitor_id=? ORDER BY created_at',v['id'])
                return reply(list(SEEDS.values())+[json.loads(r['payload']) for r in rows])
            record=validate_record(body)
            if record.case_id in SEEDS: raise HttpError(409,'内置示例不可覆盖，请另存。')
            await self.sql('INSERT INTO cases(visitor_id,id,payload,created_at) VALUES(?,?,?,?)',v['id'],record.case_id,canonical(record.model_dump()),now())
            return reply({'id':record.case_id},201)
        if path=='/api/jobs':
            if method=='GET':
                rows=await self.sql('SELECT j.id,j.case_id,j.created_at,j.bundle_id,j.state,j.stage,j.error,j.risk,j.priority,j.revision AS review_revision, (SELECT json_extract(r.payload,\'$.action\') FROM reviews r WHERE r.job_id=j.id ORDER BY revision DESC LIMIT 1) AS review_action, CASE WHEN j.review_closed=1 THEN \'closed\' WHEN j.review_transferred=1 THEN \'transferred\' ELSE (SELECT a.state FROM alerts a WHERE a.job_id=j.id ORDER BY a.id DESC LIMIT 1) END AS alert_state FROM jobs j WHERE j.visitor_id=? ORDER BY j.created_at DESC LIMIT 100',v['id'])
                for r in rows:
                    r['alert_state']=r['alert_state'] or 'pending'
                    if r['state']=='running' and r['created_at']<cutoff(): r.update(state='failed',stage='interrupted')
                return reply(rows)
            if set(body)!={'case_id','request_key'} or not isinstance(body['case_id'],str): raise HttpError(400,'分析请求格式无效。')
            key=check_key(body['request_key'])
            previous=await self.sql('SELECT id,case_id FROM jobs WHERE visitor_id=? AND request_key=?',v['id'],key,first=True)
            if previous:
                if previous['case_id']!=body['case_id']: raise HttpError(409,'重复请求内容不同。')
                return reply({'id':previous['id'],'created':False})
            source=SEEDS.get(body['case_id'])
            if source is None:
                row=await self.sql('SELECT payload FROM cases WHERE visitor_id=? AND id=?',v['id'],body['case_id'],first=True)
                if not row: raise HttpError(404,'没有此记录。')
                source=json.loads(row['payload'])
            record=validate_record(source); jid=secrets.token_hex(16); t=now()
            ip_hash=token_hash(t[:10]+'|'+(request.headers.get('CF-Connecting-IP') or 'local'))
            try:
                await self.sql('INSERT INTO jobs(id,visitor_id,request_key,case_id,input,ip_hash,day,created_at,bundle_id,state,stage) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    jid,v['id'],key,record.case_id,canonical(source),ip_hash,t[:10],t,BUNDLE_ID,'running','qwen_decompose_issues')
            except Exception:
                previous=await self.sql('SELECT id,case_id FROM jobs WHERE visitor_id=? AND request_key=?',v['id'],key,first=True)
                if previous and previous['case_id']==record.case_id: return reply({'id':previous['id'],'created':False})
                raise
            async def chat(model,payload):
                return native(await self.env.AI.run(model,to_js(payload,dict_converter=Object.fromEntries)))
            result=await analyze(record,jid,chat)
            await self.sql('UPDATE jobs SET state=?,stage=?,result=?,result_sha256=?,risk=?,priority=? WHERE id=? AND visitor_id=? AND result IS NULL',
                'failed' if result['status']=='analysis_failed' else 'completed','finished',canonical(result),digest(result),result['risk'],result['triage_priority'],jid,v['id'])
            return reply({'id':jid,'created':True})
        match=re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(export|reviews|alerts))?',path)
        if match:
            jid,action=match.groups()
            if method=='GET' and action in (None,'export'):
                return reply(await self.detail(v,jid),headers={'Content-Disposition':f'attachment; filename="qc-{jid}.json"'} if action=='export' else None)
            job=await self.own_job(v['id'],jid)
            if action=='reviews' and method=='POST':
                key=check_key(body.get('request_key')); h=digest(body)
                previous=await self.sql('SELECT job_id,request_hash,revision FROM reviews WHERE visitor_id=? AND request_key=?',v['id'],key,first=True)
                if previous:
                    if previous['job_id']!=jid or previous['request_hash']!=h: raise HttpError(409,'重复复核内容不同。')
                    return reply({'revision':previous['revision']})
                if not job['result']: raise HttpError(400,'尚无完整结果，请先完成分析。')
                stored=review_payload(json.loads(job['result']),body); revision=body['expected_revision']+1
                await self.sql('INSERT INTO reviews(job_id,visitor_id,revision,request_key,request_hash,actor,created_at,payload) VALUES(?,?,?,?,?,?,?,?)',
                    jid,v['id'],revision,key,h,'访客-'+v['id'][:6],now(),canonical(stored))
                return reply({'revision':revision},201)
            if action=='alerts' and method=='POST':
                if set(body)!={'state','reason'} or body['state'] not in ('seen','transferred') or not isinstance(body['reason'],str) or not 3<=len(body['reason'].strip())<=1000:
                    raise HttpError(400,'请填写提醒状态及理由。')
                await self.sql('INSERT INTO alerts(job_id,visitor_id,state,actor,reason,created_at) VALUES(?,?,?,?,?,?)',jid,v['id'],body['state'],'访客-'+v['id'][:6],body['reason'],now())
                return reply({'ok':True})
        if path=='/api/audit' and method=='GET':
            rows=await self.sql('SELECT r.job_id,r.revision,r.actor,r.created_at,r.payload FROM reviews r WHERE r.visitor_id=? ORDER BY r.created_at DESC LIMIT 100',v['id'])
            return reply([{'id':r['job_id']+'-'+str(r['revision']),'actor':r['actor'],'created_at':r['created_at'],'action':'review_'+json.loads(r['payload'])['action'],'target':r['job_id'],'detail':json.loads(r['payload'])} for r in rows])
        raise HttpError(404,'公开版不提供此接口；规则库只读。')

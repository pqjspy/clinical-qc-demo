"""Server-side review validation, separate from immutable AI results."""
from typing import Literal
from pydantic import Field
from core.contracts import StrictModel

class HumanFinal(StrictModel):
    status: Literal['proposed_findings','no_finding_for_checked_rule','needs_information']
    l3_id: str|None
    risk: Literal['低','中','高','待定','不适用']
    suggested_decision: Literal['方案偏离','需人工判定','本项未命中','待补充']
    explanation: str=Field(min_length=1,max_length=4000)
    suggested_action: str=Field(min_length=1,max_length=2000)

class Review(StrictModel):
    issue_id: str|None=None
    action: Literal['confirm','edit','return']
    reason: str=Field(min_length=3,max_length=2000)
    attested: Literal[True]
    expected_revision: int=Field(ge=0,le=49)
    request_key: str=Field(min_length=16,max_length=100)
    final: HumanFinal|None=None

def review_payload(original,raw):
    if raw.get('attested') is not True: raise ValueError('必须明确勾选复核确认。')
    p=Review.model_validate(raw).model_dump()
    if len(p['reason'].strip())<3: raise ValueError('请说明复核理由。')
    targets=[i for i in original['issues'] if i['issue_id']==p['issue_id']]
    if original['issues'] and len(targets)!=1: raise ValueError('必须选择本次运行中真实存在的问题项。')
    if not targets and (p['action']!='return' or p['issue_id'] is not None):
        raise ValueError('没有有效问题项的失败运行只能退回。')
    target=targets[0] if targets else original
    final=p['final']
    if p['action']=='confirm':
        if original['status']=='analysis_failed' or target['status'] not in ('proposed_findings','no_finding_for_checked_rule') or final is not None or not target.get('explanation_draft'):
            raise ValueError('未决、失败或解释未完成的初判不能一键确认；请编辑人工意见或退回。')
        final={'status':target['status'],'findings':target['findings'],'risk':target['risk'],
               'explanation':target['explanation_draft']['text'],'origin':'human_confirmed_original'}
    elif p['action']=='edit':
        if not final or not final['explanation'].strip() or not final['suggested_action'].strip():
            raise ValueError('请完整填写人工理由及建议。')
        paths={x['l3_id']:x for x in original['knowledge_snapshot']['taxonomy']['paths']}
        l3=final['l3_id']; status=final['status']
        if l3 and l3 not in paths: raise ValueError('三级分类不在本次规则快照中。')
        if (status=='proposed_findings')!=bool(l3): raise ValueError('只有确定的问题才选择三级分类。')
        allowed={'proposed_findings':({'低','中','高'},{'方案偏离','需人工判定'}),
                 'no_finding_for_checked_rule':({'不适用'},{'本项未命中'}),
                 'needs_information':({'待定'},{'待补充'})}
        risks,decisions=allowed[status]
        if final['risk'] not in risks or final['suggested_decision'] not in decisions:
            raise ValueError('人工状态、风险和建议结论不一致。')
        final={**final,'path':paths.get(l3),'origin':'human_edited_not_ai_prediction'}
    elif final is not None: raise ValueError('退回不能附带已确认结论。')
    return {'action':p['action'],'reason':p['reason'],'final':final,'attested':True,
            'issue_id':p['issue_id'],'scope':'visitor_demo_review_not_clinical_authorization'}

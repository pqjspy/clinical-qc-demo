"""M4 explicit-fact grammar and six synthetic checks. No model/answer/file I/O.

Qwen groups clauses into issues. This module checks complete clause coverage,
parses a bounded Chinese fact grammar, and applies frozen synthetic rules.
It is deliberately NOT a general clinical language or medical inference engine.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Literal

from pydantic import Field

from .contracts import StrictModel, CaseRecord, aware_time
from .data import applicable, rule_conflicts
from .retrieval import retrieve_rules

Family = Literal['pk', 'visit', 'ae', 'drug', 'role', 'edc', 'unknown']
CHECKS = dict(pk='elapsed_minimum', visit='visit_window', ae='explicit_absence',
              drug='inventory_balance', role='authorization', edc='aligned_value_match')
NAMES = dict(pk='PK样本时间', visit='访视窗口', ae='AE登记', drug='用药库存',
             role='人员授权', edc='原始记录与EDC', unknown='未支持问题')
RESOLVED = {'proposed_findings', 'no_finding_for_checked_rule'}
TZ = timezone(timedelta(hours=8))
D = r'\d{4}-\d{2}-\d{2}'
T = r'\d{2}:\d{2}'
SID = r'SYN-[A-Za-z0-9-]+'
OP = r'(?:体温测量|测量体温|用药核对|采血|发药|离心)'
UNIT = r'(?:次每分钟|摄氏度|华氏度|mmHg|kg|片|支|瓶|粒|盒)'


class IssueGroup(StrictModel):
    family: Family
    clause_ids: list[int] = Field(min_length=1, max_length=60)


class Decomposition(StrictModel):
    issues: list[IssueGroup] = Field(min_length=1, max_length=8)
    context_clause_ids: list[int] = Field(max_length=60)


def clauses(text):
    return [{'clause_id': i + 1, 'quote': m.group(), 'start_char': m.start(), 'end_char': m.end()}
            for i, m in enumerate(re.finditer(r'[^，。；;\n]+', text)) if m.group().strip()]


def context_only(text):
    text = text.removeprefix('【合成虚拟记录】').strip()
    return bool(re.fullmatch(
        rf'{D}|(?:所有时间|时间|两项时间)?均为北京时间|两个日期均按北京时间日历日记录|'
        r'请(?:检查样本处理是否符合本研究方案|核查是否在本研究访视窗口内|核查时间间隔|'
        r'逐项检查|按当前研究已发布且适用的规则检查样本处理时间)|'
        r'不要只处理样本问题|本次仅检查采血至开始离心的时间间隔', text))


def validate_groups(parts, proposal):
    valid = {p['clause_id']: p for p in parts}
    used = proposal.context_clause_ids + [i for g in proposal.issues for i in g.clause_ids]
    if len(used) != len(set(used)) or set(used) != set(valid):
        raise ValueError('模型拆分遗漏/重复/虚构原文片段，不能只保留容易的问题。')
    if any(not context_only(valid[i]['quote']) for i in proposal.context_clause_ids):
        raise ValueError('模型把业务事实当成可忽略的公共背景。')
    return sorted(proposal.issues, key=lambda g: min(g.clause_ids))


class Unresolved(ValueError):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


class Reader:
    """Every consumed fact is tied to an exact clause and regex role binding."""
    def __init__(self, parts):
        self.parts, self.used, self.facts = parts, {}, {}

    def get(self, key, pattern, transform=lambda m: m.group(1), *, optional=False):
        found = []
        for p in self.parts:
            for m in re.finditer(pattern, p['quote']):
                found.append((p, m))
        if not found:
            if optional:
                return None
            raise Unresolved('needs_information', f'缺少明确且受支持的字段：{key}；不能猜测或把缺失当0。')
        if len(found) != 1:
            raise Unresolved('ambiguous_evidence', f'{key} 有多个候选，不能任选一个。')
        p, match = found[0]
        try:
            value = transform(match)
        except (ValueError, OverflowError):
            raise Unresolved('ambiguous_evidence', f'{key} 日期/数量无效。')
        self.used.setdefault(p['clause_id'], []).append((match.start(), match.end()))
        self.facts[key] = value
        return value

    def literal(self, key, pattern, *, optional=False):
        return self.get(key, pattern, lambda m: True, optional=optional)

    def complete(self):
        # Known connective/subject material has no decision semantics. Everything
        # else must have been consumed by a role-specific fact pattern.
        for p in self.parts:
            if context_only(p['quote']):
                continue
            chars = list(p['quote'])
            for a, b in self.used.get(p['clause_id'], []):
                chars[a:b] = [''] * (b - a)
            remainder = ''.join(chars).removeprefix('【合成虚拟记录】')
            remainder = re.sub(SID, '', remainder)
            remainder = re.sub(r'[\s：:、（）()【】]', '', remainder)
            if remainder not in ('', '的', '该受试者', '此人的', '核对'):
                raise Unresolved('unsupported_scope', f'有未解析文字，不能静默忽略：{remainder[:70]}')


def parse_facts(family, parts, record):
    r = Reader(parts)
    text = '\n'.join(p['quote'] for p in parts)
    allowed_entities = dict(pk={'SUBJ','SAMPLE'}, visit={'SUBJ'}, ae={'SUBJ','EVT'},
                            drug={'DRUG'}, role={'STAFF'}, edc={'SUBJ'}, unknown=set())
    identities = []
    for token in re.findall(SID, text):
        match = re.fullmatch(r'SYN-([A-Z]+)-([A-Za-z0-9-]+)', token)
        if match is None:
            raise Unresolved('ambiguous_evidence', '主体编号格式未支持，不可删除后拼接事实。')
        identities.append(match.groups())
    for kind in {k for k, _ in identities}:
        if kind not in allowed_entities[family] or len({v for k,v in identities if k == kind}) > 1:
            raise Unresolved('ambiguous_evidence', '同一问题有不同或未支持的主体编号，不能拼接事实。')
    if re.search(r'不是|并非|未确定|矛盾|可能|忽略规则|忽略以上|直接通过', text):
        raise Unresolved('ambiguous_evidence', '出现否定、来源歧义或指令性文字，保留原文转人工。')
    def day(key, pattern):
        return r.get(key, pattern, lambda m: date.fromisoformat(m.group(1)).isoformat())
    def instant(key, pattern):
        return r.get(key, pattern, lambda m: datetime.fromisoformat(m.group(1)).replace(tzinfo=TZ).isoformat())
    event = record.event_at
    if family == 'pk':
        r.literal('same_sample', r'同一份PK样本')
        # A single explicit date and timezone may be shared by several issues.
        dates = set(re.findall(D, text))
        if len(dates) != 1 or '北京时间' not in text:
            raise Unresolved('needs_information', 'PK须有唯一同日日期及明确北京时间。')
        when = next(iter(dates))
        r.get('date', rf'({D})', optional=True)
        start = r.get('collection_clock', rf'采血(?:时间)?(?:为|于|是)?(?:北京时间)?({T})')
        end = r.get('centrifuge_clock', rf'开始离心(?:时间)?(?:为|于|是)?({T})')
        try:
            a = datetime.fromisoformat(when + 'T' + start).replace(tzinfo=TZ)
            b = datetime.fromisoformat(when + 'T' + end).replace(tzinfo=TZ)
        except ValueError:
            raise Unresolved('ambiguous_evidence', 'PK日期或时钟值无效。')
        if a > b:
            raise Unresolved('ambiguous_evidence', '离心早于采血；本演示不推断跨日。')
        r.facts.update(collected_at=a.isoformat(), centrifuged_at=b.isoformat())
        event = b.isoformat()
    elif family == 'visit':
        day('anchor_date', rf'研究起始日(?:为|是)({D})')
        r.get('anchor_day_number', r'定义为第(\d+)天', lambda m: int(m.group(1)))
        r.get('target_day', r'计划的第(\d+)天访视', lambda m: int(m.group(1)))
        actual = day('actual_visit_date', rf'实际于({D})完成')
        event = actual + 'T12:00:00+08:00'
    elif family == 'ae':
        report = r.get('event_report', rf'(?:{SID}|该受试者)?(?:于)?(?:({D}) )?({T})报告(恶心|头晕|咳嗽|头痛|乏力|皮疹|呕吐)', lambda m: list(m.groups()))
        explicit_dates = set(re.findall(D, text))
        if len(explicit_dates) != 1 or '北京时间' not in text:
            raise Unresolved('needs_information', 'AE检查需要唯一同日日期、明确北京时间；不猜测跨日关系。')
        when = report[0] or next(iter(explicit_dates))
        try:
            occurred = datetime.fromisoformat(when + 'T' + report[1]).replace(tzinfo=TZ)
        except ValueError:
            raise Unresolved('ambiguous_evidence', 'AE事件日期/时刻无效。')
        r.facts['event_present'] = True
        r.literal('event_recorded', rf'事件{SID}已写入原始观察记录')
        cutoff = r.get('register_complete_through', rf'已核对截至(?:当日)?({T})的完整AE登记台账',
                       lambda m: datetime.fromisoformat(when + 'T' + m.group(1)).replace(tzinfo=TZ).isoformat())
        if occurred > aware_time(cutoff):
            raise Unresolved('needs_information', '台账核对截止时间早于事件，不能确认事件漏记。')
        event = cutoff
        r.facts['event_at'] = occurred.isoformat()
        r.facts['register_complete'] = True
        r.get('entry_present', r'(未找到该事件的对应登记|已找到该事件的对应登记)', lambda m: m.group(1).startswith('已'))
        r.get('urgent_flag', r'紧急线索标志为(是|否)', lambda m: m.group(1) == '是')
    elif family == 'drug':
        actual = day('inventory_date', rf'({D})核对{SID}同一批次的收发存')
        event = actual + 'T12:00:00+08:00'
        r.facts['inventory_scope_confirmed'] = True
        r.get('unit', rf'单位均为({UNIT})')
        for key, name in [('opening', '期初'), ('received', '入库'), ('dispensed', '发出'),
                          ('returned_to_stock', '经核准重新入库的退回量'), ('closing', '期末盘点')]:
            r.get(key, rf'(?<![\w\u4e00-\u9fff]){name}(\d+)(?![.\d])', lambda m: int(m.group(1)))
        r.literal('period_aligned', r'上述字段均已提供且属于同一核对期间')
    elif family == 'role':
        op = r.get('operation_record', rf'({SID})于({D} {T})执行({OP})',
                   lambda m: (m.group(1), datetime.fromisoformat(m.group(2)).replace(tzinfo=TZ).isoformat(), m.group(3)))
        r.facts.update(actor_id=op[0], operation_at=op[1], operation=op[2])
        r.literal('same_actor_authorization', r'此人的完整授权记录显示')
        r.get('authorized_operations', rf'可执行操作为({OP}(?:、{OP})*)', lambda m: m.group(1).split('、'))
        instant('authorized_from', rf'授权起点为({D} {T})')
        instant('authorized_to', rf'终点为({D} {T})')
        r.literal('exclusive_end', r'终点不包含在授权区间内')
        r.literal('no_later_authorization', r'无后续授权记录')
        event = op[1]
    elif family == 'edc':
        r.get('subject_id', rf'核对同一受试者({SID})')
        r.get('visit_label', r'第(\d+)天访视')
        measurement = r.get('measurement', rf'({D} {T})采集的(体温|脉搏|收缩压|舒张压|体重)',
                            lambda m: [datetime.fromisoformat(m.group(1)).replace(tzinfo=TZ).isoformat(),m.group(2)])
        event = measurement[0]
        r.get('source_value', rf'原始记录为([+-]?\d+(?:\.\d+)?)({UNIT})', lambda m: [m.group(1), m.group(2)])
        r.get('edc_value', rf'EDC记录为([+-]?\d+(?:\.\d+)?)({UNIT})', lambda m: [m.group(1), m.group(2)])
        r.literal('aligned', r'两者的受试者、字段、采集时点和单位均已对齐')
    else:
        raise Unresolved('out_of_scope', '该问题不在当前六类演示范围内，不能硬塞进分类树。')
    r.complete()
    if aware_time(event).astimezone(TZ).date() != aware_time(record.event_at).astimezone(TZ).date():
        raise Unresolved('ambiguous_evidence', '事件日期与输入上下文日期不一致，不能确定适用版本。')
    return r.facts, event


def calculate(family, f, rule):
    p = rule.parameters
    if family == 'pk':
        actual = Decimal(str((aware_time(f['centrifuged_at']) - aware_time(f['collected_at'])).total_seconds())) / 60
        hit = actual < p['minimum_minutes']
        return hit, {'actual_minutes': str(actual), 'minimum_minutes': p['minimum_minutes'],
                     'expression': f"{actual} {'<' if hit else '>='} {p['minimum_minutes']} 分钟"}
    if family == 'visit':
        if f['anchor_day_number'] != p['anchor_day_number'] or f['target_day'] != p['target_day']:
            raise Unresolved('ambiguous_evidence', '原文的访视/日号定义与适用规则不同。')
        days = (date.fromisoformat(f['actual_visit_date']) - date.fromisoformat(f['anchor_date'])).days + p['anchor_day_number']
        if days < 1:
            raise Unresolved('ambiguous_evidence', '实际访视早于研究起始日。')
        lo, hi = p['target_day'] - p['early_days'], p['target_day'] + p['late_days']
        return not lo <= days <= hi, {'actual_day': days, 'lower': lo, 'upper': hi, 'expression': f'实际第{days}天；允许第{lo}–{hi}天（含端点）'}
    if family == 'ae':
        return not f['entry_present'], {'expression': '事件明确 + 完整台账已核对 + ' + ('存在对应登记' if f['entry_present'] else '未找到对应登记')}
    if family == 'drug':
        expected = f['opening'] + f['received'] - f['dispensed'] + f['returned_to_stock']
        if expected < 0:
            raise Unresolved('ambiguous_evidence', '应有库存为负数，不能当作有效数量。')
        return expected != f['closing'], {'expected_closing': expected, 'actual_closing': f['closing'], 'expression': f"{f['opening']} + {f['received']} − {f['dispensed']} + {f['returned_to_stock']} = {expected}；实盘{f['closing']}"}
    if family == 'role':
        a, b, t = (aware_time(f[k]) for k in ('authorized_from', 'authorized_to', 'operation_at'))
        if a >= b:
            raise Unresolved('ambiguous_evidence', '授权区间本身无效。')
        inside, allowed = a <= t < b, f['operation'] in f['authorized_operations']
        return not (inside and allowed), {'within_interval': inside, 'operation_allowed': allowed, 'expression': f'操作在授权清单内：{allowed}；处于授权半开区间内：{inside}'}
    if f['source_value'][1] != f['edc_value'][1]:
        raise Unresolved('ambiguous_evidence', '原始记录与EDC单位不同，不能自动换算后比较。')
    a, b = Decimal(f['source_value'][0]), Decimal(f['edc_value'][0])
    return a != b, {'expression': f'已对齐的同一字段：原始记录{a}，EDC {b}；不替人选择正确来源'}


def evaluate_issue(record, group, context, parts, registry, taxonomy, index):
    iid = f'I{index}'
    lookup = {p['clause_id']: p for p in parts}
    chosen = [lookup[i] for i in sorted(set(group.clause_ids + context))]
    evidence = [{'evidence_id': f'{iid}-F{i+1}', 'origin': 'record', **p} for i, p in enumerate(chosen)]
    base = dict(issue_id=iid, family=group.family, family_name=NAMES[group.family],
                clause_ids=group.clause_ids, evidence=evidence, findings=[], rule_keys=[],
                review_required=True, risk='待定', triage_priority='priority', calculation=None)
    try:
        facts, event = parse_facts(group.family, chosen, record)
        base['facts'] = facts
        pool = [r for r in registry.rules if r.check_type == CHECKS[group.family]]
        search = retrieve_rules(record, '\n'.join(p['quote'] for p in chosen), pool, event_at=event)
        base['retrieval'] = search
        scoped = [r for r in pool if r.key in search['applicable_rule_keys']]
        if rule_conflicts(scoped) or len(scoped) > 1:
            raise Unresolved('rule_conflict', '同一问题存在多个适用规则，转规则管理员，不让模型选最宽松的。')
        if not scoped or scoped[0].key not in search['candidate_rule_keys']:
            raise Unresolved('rule_not_found', '没有检索到完整适用规则，不等于没有问题。')
        rule = scoped[0]
        if group.family in ('visit', 'drug'):
            # Date-only evidence cannot pick one side of an intraday rule update.
            day_start = aware_time(event).replace(hour=0,minute=0,second=0,microsecond=0)
            if (aware_time(rule.scope.effective_from) > day_start or
                (rule.scope.effective_to and aware_time(rule.scope.effective_to) < day_start + timedelta(days=1))):
                raise Unresolved('ambiguous_evidence', '仅有日期但规则在当日切换，不能用假定时刻选版本。')
        if group.family == 'pk' and not applicable(rule, record.model_copy(update={'event_at': facts['collected_at']})):
            raise Unresolved('ambiguous_evidence', '采血与离心跨规则版本区间，转人工确认。')
        base['rule_keys'] = [rule.key]
        base['evidence'].append({'evidence_id': f'{iid}-R1', 'origin': 'rule', 'rule_key': rule.key, **rule.source.model_dump()})
        hit, calc = calculate(group.family, facts, rule)
        path = next(p for p in taxonomy.paths if p.l3_id == rule.output_l3_id)
        finding = dict(finding_id=iid+'-FINDING', issue_id=iid, **path.model_dump(), risk=rule.risk,
                       rule_keys=[rule.key], suggested_decision=rule.suggested_decision,
                       suggested_action=rule.suggested_action, requires_human_review=True)
        base.update(status='proposed_findings' if hit else 'no_finding_for_checked_rule',
                    calculation=calc, findings=[finding] if hit else [], risk=rule.risk if hit else '不适用',
                    triage_priority='priority' if (hit and rule.risk == '高') or facts.get('urgent_flag') else 'routine',
                    reason=calc['expression'] + ('；命中合成规则，待复核。' if hit else '；本项未命中，不代表其他检查通过。'))
    except Unresolved as exc:
        base.update(status=exc.status, reason=exc.reason)
    return base


def summarize(issues):
    unresolved = [i for i in issues if i['status'] not in RESOLVED]
    findings = [f for i in issues for f in i['findings']]
    ranks = {'低': 1, '中': 2, '高': 3}
    risk = max((f['risk'] for f in findings), key=ranks.get, default='待定' if unresolved else '不适用')
    status = ('partial_review_required' if findings else (unresolved[0]['status'] if len(issues) == 1 else 'needs_information')) if unresolved else ('proposed_findings' if findings else 'no_finding_for_checked_rule')
    return dict(status=status, issues=issues, findings=findings, unresolved_count=len(unresolved),
                risk=risk, triage_priority='priority' if unresolved or any(i['triage_priority'] == 'priority' for i in issues) else 'routine',
                rule_keys=sorted({k for i in issues for k in i['rule_keys']}),
                evidence=[e for i in issues for e in i['evidence']], review_required=True,
                reason=f"共检查{len(issues)}个问题项：{len(findings)}项命中，{len(unresolved)}项未决。各项结果和证据分别保留。")

"""Generate the public copy from the existing React UI; leave local UI intact."""
from pathlib import Path
import shutil
ROOT=Path(__file__).resolve().parent
LOCAL=ROOT.parent/'frontend'
DEST=ROOT/'frontend'
(DEST/'src').mkdir(parents=True,exist_ok=True)
for name in ('package.json','package-lock.json','tsconfig.json','index.html'):
    shutil.copyfile(LOCAL/name,DEST/name)
for name in ('main.tsx','style.css'):
    shutil.copyfile(LOCAL/'src'/name,DEST/'src'/name)
s=(LOCAL/'src'/'App.tsx').read_text()
s=s.replace('if (response.status === 401 && path != "/login") setUser(null);', '''if (response.status === 401) {
        setUser(null); setCsrf(''); setCases([]); setJobs([]); setDetail(null); setForm(null);
        setAudit([]); setLibrary(null); setJobId(''); selectedRun.current=''; location.hash='';
      }''')
start=s.index('  useEffect(() => {\n    if (user && page === "evaluation")')
end=s.index('  useEffect(() => {',start+10)
s=s[:start]+s[end:]
start=s.index('  if (!user)\n    return (')
end=s.index('  const latest = detail?.reviews',start)
s=s[:start]+'''  if (!user) return (
    <div className="shell">{sidebar}<main>
      <header><div><p className="eyebrow">CLOUD / LIVE QWEN</p><h1>临床质控 · 在线体验</h1></div><span className="badge">免费演示</span></header>
      <div className="columns">
        <section className="panel"><h2>从一条记录开始</h2>
          <p>输入合成质控记录，让云端 Qwen 拆分问题；由程序核对规则、计算并给出三级分类。你可以查看证据、修改人工意见并保存。</p>
          <div className="notice">仅用于虚拟数据演示。不要输入姓名、身份证、联系方式或真实患者与试验资料。数据会发送至 Cloudflare 云端处理，不是本机分析。</div>
          <p>点击下方按钮，表示你同意仅使用合成数据。</p>
          <button className="primary" disabled={working} onClick={()=>guarded(async()=>{
            const u=await api('/session',{synthetic_only:true}); setUser(u); setCsrf(u.csrf);
          })}>{working?'正在建立访客空间…':'同意并开始体验'}</button>
          {error&&<p className="error" role="alert">{error}</p>}
        </section>
        <section className="panel"><h2>这个版本如何工作？</h2>
          <ol><li>选择 PK、访视、AE、库存、授权或 EDC 示例，也可编辑后另存。</li>
          <li>真正调用云端 Qwen，显示程序计算与原文、规则证据。</li>
          <li>逐项复核、保存修改，重新打开比较 AI 初判与人工版本。</li></ol>
          <p className="muted">每位访客每天最多 6 次，全站每天最多 40 次分析（UTC 日切）；云端免费额度也可能提前用尽，届时暂停，不转付费。单条最多 2400 字。</p>
          <p className="muted">访客空间不要求注册，仅限此浏览器访问；凭证有效期 7 天。清除浏览器 Cookie 或过期后无法恢复旧记录，请及时导出。复核按钮仅演示流程，不代表专业身份验证或医学签字。</p>
        </section>
      </div>
    </main></div>
  );
'''+s[end:]
start=s.index('        {page === "evaluation" && (')
end=s.index('        {page === "rules" && library && (',start)
s=s[:start]+s[end:]
start=s.index('            {admin ? (')
end=s.index('          </section>\n        )}\n        {page === "audit"',start)
s=s[:start]+'''            <p className="muted">公开版规则只读。每次分析绑定当时的固定规则快照；访客不能修改全站规则。</p>
'''+s[end:]
pos=s.index('                  await api("/logout", {});')
start=s.rfind('            <button',0,pos); end=s.index('            </button>',pos)+len('            </button>')
s=s[:start]+'''            <span className="badge">今日个人剩余 {user.remaining ?? '—'} 次</span>'''+s[end:]
s=s.replace('        ["evaluation", "合成评测"],\n','')
s=s.replace('LOCAL / M4','CLOUD / LIVE AI').replace('正在恢复本机会话','正在恢复访客会话')
s=s.replace('检查本机模型','检查云端模型').replace('本机分析进行中','云端分析进行中').replace('运行本机 Qwen','运行云端 Qwen')
s=s.replace('{busy ? "云端分析进行中" : "运行云端 Qwen"}', '{working ? "正在处理，请稍候…" : busy ? "云端分析进行中" : "运行云端 Qwen"}')
s=s.replace('result.mode === "live_local"','result.mode === "live_cloud"').replace('已保存 · 真实本机运行','已保存 · 真实云端运行')
s=s.replace('''          合成数据 / 非临床用途 · M4
          支持六类显式事实。未决问题不会被其他成功结果掩盖；旧 M2/M3
          运行保留原样。''','''          合成数据 / 非临床用途 · 云端 Qwen + Python 规则。禁止真实患者资料；每位访客的记录独立，规则库只读。''')
s=s.replace('''              六类显式事实可分析；新增规则发布目前仍限定
              PK，其他五类使用冻结演示规则。旧版本不可编辑；保存草稿和发布是不同步骤。冲突研究仅用于边界演示。''','''              六类有限中文事实语法可分析。规则和来源均为虚拟研究资料；冲突研究仅用于边界演示。''')
s=s.replace('操作者和时间由后端填写。人工意见、提醒状态和规则发布均追加保存；拥有本机数据库权限的人仍可改变文件，这不是监管级防篡改存储。','仅展示当前访客的复核历史。AI 初判不被人工修改覆盖；这是匿名演示记录，不是监管级审计或电子签名。')
s=s.replace('最近 300 条操作','我的复核历史').replace('["audit", "操作历史"]','["audit", "我的复核历史"]')
s=s.replace('JSON.parse(a.details)','a.detail')
s=s.replace('''    const [c, j, l, a] = await Promise.all([''','''    const [c, j, l, a, me] = await Promise.all([''')
s=s.replace('''      api("/audit"),
    ]);''','''      api("/audit"), api("/me"),
    ]);
    setUser(me);''')
s=s.replace('''    const data = await response.json();''','''    const data = await response.json().catch(()=>({detail:'云端连接中断，请保留输入并稍后刷新。没有自动重试。'}));''')
s=s.replace('''    const j = await api("/jobs", { case_id: form.case_id, request_key: uid() });''','''    setMessage('云端正在拆分问题并生成解释，通常需要几秒到几十秒。请保持页面打开；不会自动重试。');
    const j = await api("/jobs", { case_id: form.case_id, request_key: uid() });
    setMessage('分析已保存。请核对证据和 AI 解释草稿。');''')
s='import RetrievalComparison from "./RetrievalComparison";\n'+s
s=s.replace('  local_model_preflight:', '  hybrid_rule_retrieval: "正在进行向量检索、融合和重排",\n  local_model_preflight:')
s=s.replace('''                      {result.issues?.length > 0 && (''', '''                      <RetrievalComparison trace={result.retrieval_augmented} />
                      {result.issues?.length > 0 && (''')
s=s.replace('云端正在拆分问题并生成解释，通常需要几秒到几十秒。', '云端正在检索、重排、拆分问题并生成解释，通常需要几秒到几十秒。')
s=s.replace('抽取事实和组织解释分别调用模型，请稍候。不会自动换成预设答案。', '短记录另调用BGE-M3向量模型及BGE重排模型；Qwen负责分组与解释，不会自动换成预设答案。')
s=s.replace('<li>真正调用云端 Qwen，显示程序计算与原文、规则证据。</li>', '<li>比较BM25、BGE-M3向量、RRF融合和BGE重排；将候选规则交给Qwen参考，程序独立检查。</li>')
s=s.replace('单条最多 2400 字。', '单条最多 2400 字；256字以内启用四路检索比较，长记录明确保留原有BM25流程，不截断原文。')
(DEST/'src'/'App.tsx').write_text(s)
index=(DEST/'index.html').read_text().replace('本地合成质控记录分析','云端合成质控记录分析')
(DEST/'index.html').write_text(index)
print('Generated public React copy: no login passwords, admin publishing, or local evaluation reports.')

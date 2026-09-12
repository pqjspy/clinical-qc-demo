# 质控工作台 · 云端实时演示

线上地址：<https://clinical-qc-public-demo.pqjspy.workers.dev>

这是一份独立部署副本，不改变 `../frontend`、本地 8911 服务、账号、历史记录或封存评测。

## 功能

- Cloudflare Workers AI 实时调用 `@cf/qwen/qwen3-30b-a3b-fp8`，不是答案回放。与本地 Qwen 型号不同。
- 复用原 Python 六类事实解析、适用范围过滤、BM25、计算及三级分类程序；通过 `prepare.py` 按明确清单生成并校验文件哈希。
- 原文片段完整覆盖校验；缺失、冲突、歧义和未知问题保留为未决，不自动通过。
- AI 生成解释草稿；引用编号校验不等于语义正确。保留原草稿；只有合法的逗号分组引用会被规范成独立方括号，仍逐个校验归属。
- D1 保存访客自己的记录、不可覆盖的 AI 初判、逐项人工修改和复核版本；过期或不同访客不能读取。
- 公开规则库只读，不提供本地账号登录、规则发布或旧模型评测接口。

仅允许合成资料。访客复核是流程演示，不验证医务身份，不构成医学授权、电子签名或正式合规结论。

## 免费边界

部署于已核实的 **Workers Free** 账号，没有升级套餐、添加付款方式、购买 AI Gateway 额度或设置付费回退。

| 限制 | 设置 |
|---|---|
| 每日分析 | 全站 40 次 / 每访客 6 次 / 每 IP 12 次，按 UTC 日期 |
| 同时分析 | 全站最多 3 条，同访客最多 1 条 |
| 单条输入 | 最多 2400 字、40 个片段、8 个问题 |
| 单次模型调用 | 最多 2 次，没有自动重试；各 24 秒超时 |
| 输出预算 | 分项 1100 tokens；解释 1500 tokens |
| 保存容量保险上限 | 2000 访客、2000 自建记录、1500 次分析；每访客 30 条自建记录 |
| 每次运行的复核 | 最多 50 版本；提醒操作最多 30 次 |

Cloudflare 自己的每日免费额度可能比应用限额先用尽，届时请求失败而不是转为付费。**40 次不是对所有输入都能运行的保证**。容量保险上限累计计算；达到后需由项目所有者检查并清理过期演示数据，不能通过升级付费绕过。

## 会话、保存和隐私

- 浏览器获得随机访客凭证；数据库只保存凭证哈希，Cookie 为 HttpOnly / Secure / SameSite=Strict。
- 所有写入检查同源和 CSRF；所有记录、运行、导出、复核接口均按服务端访客身份过滤。
- 会话有效 7 天。清除 Cookie 或凭证过期后不能恢复旧记录；需要保留时请提前导出。
- 数据在云端保存，不是只保存在浏览器。没有声称到期自动擦除，也不提供真实患者资料处理承诺。
- 只保存按日变化的 IP 哈希用于配额；不在模型提示或结果里加入 IP / 访客凭证。
- 迁移不复制 `runtime/`、密码、原有 SQLite 或参考答案。

## 构建与部署

在 `clinical-qc-demo` 目录：

```sh
.venv/bin/python cloud/prepare.py
.venv/bin/python cloud/adapt_frontend.py
.venv/bin/python cloud/test_cloud.py
npm --prefix cloud/frontend ci
npm --prefix cloud/frontend run build
```

在 `cloud` 目录，保证 `uv` 在 PATH 中：

```sh
uv run pywrangler dev --config wrangler.jsonc --port 8922 --ip 127.0.0.1
uv run pywrangler d1 migrations apply DB --remote --config wrangler.jsonc
uv run pywrangler deploy --config wrangler.jsonc
```

始终明确传 `--config wrangler.jsonc`，避免采用上级其他项目的配置。依赖版本由 `uv.lock`、`pylock.toml`、`package-lock.json` 锁定。数据库 ID 在配置中明确固定。

## 验证

`test_cloud.py` 为软件测试：12 个演示输入、原程序哈希、严格输出、复核不可变、SQL 隔离和配额。使用人工指定路由，不是模型准确率评测。

`smoke_http.py --url <线上地址>` 验证会话、CSRF、另存重开和访客隔离，不调用 AI。显式加 `--live` 最多执行 3 条真实分析：27 分钟、PK+AE 双问题、30 分钟边界；同时检查复核追加与跨访客拒绝。

本地预览的 AI 绑定仍访问云端，不是免费离线模型。此次网络中预览代理报连接中断，而公网 Worker 可以调用 AI。

本机 DNS 曾把 `*.workers.dev` 解析到错误地址。诊断时通过 Cloudflare 官方 DNS-over-HTTPS 查询确认地址，再仅对测试请求指定解析地址；始终保留原主机名与 TLS 证书校验，**不改系统 DNS / hosts，不关闭 HTTPS 校验**。`--resolve` 只供这种已核对 DNS 的诊断，不应硬编码过期 IP。

当前验证以编译、软件测试和 HTTP 接口为准；未完成浏览器逐按钮视觉验收，也未用这些少量案例宣称真实业务准确率或最大负载性能。

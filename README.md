# Clinical QC Demo · 临床试验质控 AI 分类与预警工作台

一个用于演示临床试验质控分类、风险提醒与人工复核的中文 Demo，提供本地和云端版本。所有研究、人员、受试者编号、质控记录、方案和规则均为合成虚拟数据，不接入医院系统。

**在线体验：** [云端实时 Demo](https://clinical-qc-public-demo.pqjspy.workers.dev)。云端版本位于独立的 `cloud/` 目录，真实调用 Cloudflare Workers AI 上的 Qwen；支持访客隔离、三级分类、证据及逐项人工复核。使用免费套餐，额度用尽暂停。部署和限制见 [云端说明](cloud/README.md)，已完成的测试见 [部署验证](cloud/DEPLOYMENT_CHECK.md)。本地与云端模型不同，下面的本地评测成绩不代表云端成绩。

云端已部署[真实混合检索 v2](docs/RETRIEVAL_V2_ZH.md)：BGE-M3 向量与余弦相似度、BM25、RRF 融合和 BGE-reranker-base 重排，选出的规则原文供 Qwen 分项时参考，Python 仍使用完整适用规则检查。2026-09-14完成独立检索评测及3条线上流程检查，详细结果（包括一次解释校验失败）见[部署验证](cloud/DEPLOYMENT_CHECK.md)。256字符以内的新分析启用增强检索；更长的完整记录明确保留原有BM25流程，旧结果不重算。

这次新编的18条合成**检索查询**固定分为6条开发集和12条评估集，与下面M4的18条分类评测不同。评估集含11条有目标查询和1条无适用研究查询：BM25的Recall@3为0.9091，Dense、Hybrid、Hybrid+重排均为1.0000；MRR依次为0.7803、1.0000、0.9545、0.9394。重排没有超过Dense，全部方法失败数为0，无目标查询均返回空候选。完整指标、版本过滤及小语料限制见[检索评测说明](docs/RETRIEVAL_V2_ZH.md#真实调用评测)。这些结果不代表质控分类或医院准确率。

本仓库只保存代码、文档及合成测试资料；不包含本地密码、运行数据库、私有备份、模型权重或依赖环境。首次使用需按说明安装依赖，并在本机重新生成演示账户。

**本地交付状态：M5本机交付版已完成。** 启动检查、私有备份、隔离恢复演练、正式五部分方案和演示稿均已提供。本地推理功能仍是M4：本机Qwen负责分项/家族识别与解释，有限中文语法提取明确事实，Python规则给出L3/风险。模型仍有误分，冻结18条分类成绩不变；不是医院部署或通用临床语言理解。

本阶段先读[M5交付与学习指南](docs/M5_START_HERE_ZH.md)，再读[正式技术方案](docs/TECHNICAL_TEST_SOLUTION_ZH.md)和[8分钟演示稿](docs/M5_DEMO_SCRIPT_ZH.md)。[M5验证记录](docs/M5_VERIFICATION_ZH.md)分清真实完成与未验证事项：本机隔离恢复和真实推理已验证，整机断网、新机器安装与Docker未验证。

现在打开 [本地工作台](http://127.0.0.1:8911)，从 [M4操作与学习指南](docs/M4_START_HERE_ZH.md) 的访视案例开始，再做多问题复核。M3原有运行/规则/复核记录仍保留；[M3指南](docs/M3_START_HERE_ZH.md)和[M3报告](docs/M3_VERIFICATION_ZH.md)记录的是此前PK阶段。模型基础仍见 [M2导览](docs/M2_START_HERE_ZH.md)。

| 文档 | 回答什么问题 |
|---|---|
| [项目总览](docs/PROJECT_OVERVIEW_ZH.md) | 最终做什么、怎么用、AI与代码如何分工、分几步完成 |
| [功能与验收对照](docs/REQUIREMENTS_MAP_ZH.md) | 每项功能由什么证据验证，哪些暂不做 |
| [QC002 演示故事](docs/DEMO_STORY_QC002_ZH.md) | 从一条输入到人工确认，界面具体展示什么 |
| [开发与学习路线](docs/BUILD_AND_LEARN_ZH.md) | 每一步交付什么、学什么、怎样算完成 |
| [数据与设计假设](docs/SOURCES_AND_ASSUMPTIONS_ZH.md) | 合成数据、设计范围与使用边界 |
| [M1虚拟规则说明](docs/M1_RULES_ZH.md) | 六种分类与九条版本化规则怎么读 |
| [M1验证记录](docs/M1_VERIFICATION_ZH.md) | 实际通过什么检查，没验证什么 |
| [混合检索 v2 与评测](docs/RETRIEVAL_V2_ZH.md) | 真实向量与重排如何参与、18条新查询的结果及边界 |

M1文件仍包含12条输入、6个L3路径、3份方案和9条版本。网页数据库另含M2练习及M3/M4用户/验收数据和规则，不覆盖M1文件。当前167项项目软件测试及16项云端测试通过；真实模型与浏览器操作另报，不能将测试替身通过数当模型准确率。

[M4验证与错误报告](docs/M4_VERIFICATION_ZH.md)：18条真实本机评测，Micro-F1 0.909，预定义完整工作流结果一致13/18，EDC两条均漏检，另有两条解释引用失败。语义正确性未独立评估，不是医院准确率。

在本项目目录运行：

```bash
cd /Users/cam/Documents/Me/clinical-qc-demo
.venv/bin/python scripts/start_local.py
.venv/bin/python scripts/local_ops.py status
```

需要备份当前记录、已发布规则和人工历史时：

```bash
.venv/bin/python scripts/local_ops.py backup
```

`start_local.py`仅启动本机8911，重复执行不会再起第二个健康服务；日志在 `runtime/web/server.log`，进程信息在 `runtime/web/server-process.json`。不设置开机自启或自动重启。服务重启后须重新登录，原记录不变。要停止时先核对PID的命令确实为本项目 `scripts/serve_m3.py`且无活动任务，再发SIGINT；不要仅凭旧PID文件停止未知进程。前台调试用`serve_m3.py`，现已在初始化数据库前检查端口。

备份每次新建私有目录，不删除旧备份。它含明文演示密码、记录和轨迹，不得作为公开交付附件上传；不含依赖环境和模型权重。核对/隔离恢复步骤见[M5指南](docs/M5_START_HERE_ZH.md)。

页面显示 `Failed to fetch` 时可能是服务已停止，不能据此判断保存成功或失败。不要刷新丢掉草稿；先恢复服务、重新登录并核对记录。本次修复（2026-09-10）确认8911无监听，恢复后在网页将去掉提示语的多问题原文另存为 `SYN-WEB-fec772eb`，没有运行模型或改变冻结评测。

账户密码在本机 `runtime/web/local_accounts.json`；通常使用 `reviewer`。后端固定监听127.0.0.1:8911；前台启动时须保持终端，后台手动启动则不依赖该终端。其他命令：

```bash
.venv/bin/python scripts/run_m2.py --case QC002 --readable
.venv/bin/python scripts/run_m4.py --case DEMO-VISIT-01
.venv/bin/python scripts/validate_m1.py --case QC002 --readable
.venv/bin/python scripts/validate_m1.py
.venv/bin/python -m unittest discover -s tests -v
```

`run_m2.py`和`run_m4.py`使用M1文件知识；网页使用SQLite中已发布的快照。`validate_m1.py`显示作者参考预览，不能当真实AI。每次真实运行保存独立记录，不自动重试或切云模型。Python依赖仍使用 `requirements-m3.lock`，前端见 `frontend/package-lock.json`。M4/M5没有增加依赖。M5完成后先练习演示与解释设计；自由文本能力改进须另立新版本和新评测，不能覆盖首次成绩。

最终交付包括本地网页应用、合成数据与评测、部署说明、演示脚本，以及按测试五个部分组织的正式方案。没有真实模型调用记录时，不能把静态样例或回放标为“AI 已执行”。

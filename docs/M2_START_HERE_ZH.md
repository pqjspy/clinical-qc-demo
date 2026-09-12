# M2：这次让 Qwen 真正读取原文

先只学一件事：**一条文字记录，怎样变成有原文依据的计算和建议？**

这次不是查看M1参考答案，而是实际请求本机Qwen。当前仅支持“同一天、北京时间、单份PK样本、明确采血和开始离心时间”的小范围；没有一次完成六类问题，也还没有网页。

## 1. 从你已经认识的 QC002 开始

用户输入仍是：

> 【合成虚拟记录】2026-09-01，SYN-SUBJ-002同一份PK样本的采血时间为10:06，开始离心时间为10:33，均为北京时间。请检查样本处理是否符合本研究方案。

**原文没有告诉模型最低30分钟、中风险或三级分类。** 它只说了发生什么。

第一步只发这个原文及研究、中心、方案、日期上下文给Qwen。`case_id`不发送，`expected_results.json`也不读取。不是看到“QC002”这个编号就查答案。

## 2. Qwen第一次调用：从文字里指出事实

我们要求它交回JSON，其中每个证据字段都是从原文连续复制的短片段。例如QC002成功运行时返回：

```json
{
  "same_sample_quote": "同一份PK样本",
  "date_quote": "2026-09-01",
  "collected_time_quote": "采血时间为10:06",
  "centrifuged_time_quote": "开始离心时间为10:33"
}
```

这是实际抽取结果的部分字段，不是完整响应。完整响应和本轮逐次结果见 [M2运行报告](M2_RUN_REPORT_ZH.md)。

`quote`就·是引用的原文。为什么不只让它返回`10:33`？因为原文可能还出现“提交时间12:00”。我们需要知道模型选的时间属于哪个事件，不能拿提交时间冒充开始离心时间。

接下来Python会检查：片段是否确实存在、是否唯一、是否包含相应角色、有无否定或额外未覆盖文字。它再从通过检查的片段中解析日期/时间，生成精确位置：`start_char`包含起点，`end_char`不包含终点，按Python Unicode字符计数。

M2检查器只接受明确且有限的表达，额外未知文字会转人工。因此这不是“已经读懂所有中文病历”，也不能保证仅靠位置检查就证实一切语义。**模型负责选原文，代码负责有边界的校验与规范化。**

## 3. 规则检索：去找应该满足什么要求

程序先筛掉其他研究、中心、方案和事件时点不适用的规则，再用BM25对剩下的规则原文排序。不是模型凭记忆说“应该30分钟”。

QC002的原文日期是9月1日、方案是v1，所以适用规则是：

> 本虚拟研究规定，同一PK样本从采血到开始离心的时间间隔至少为30分钟。

规则完整来源有文档ID和段落ID。正分候选全部保留，不是直接拿第一名就判定；冲突检查查看全部适用规则。BM25分数只是词匹配强弱，不是“正确率”或“医学可信度”。

M2只执行PK时间检查，所以检索结果中即使出现授权等规则，也不等于已经执行了那些检查。

## 4. Python计算，规则决定建议

```text
从Qwen选出的原文片段解析到：10:06、10:33
Python计算：10:33 − 10:06 = 27分钟
适用规则的参数：minimum_minutes = 30
比较：27 < 30 → 命中时间不足
```

然后从规则和合法分类树取出：

| 输出 | 来自哪里 |
|---|---|
| 样本管理 → PK 样本管理 → 样本处理时间不足 | 规则对应的合法L3路径，L1/L2由分类树回填 |
| 中风险 | 这条虚拟规则预设的提醒策略，不是模型猜测 |
| 建议方案偏离 | 规则给出的建议，不是最终人工结论 |
| 需人工复核 | Demo工作流的固定要求 |
| 建议措施 | 适用规则保存的文字，不由模型自由编临床处置 |

这些结果不取自M1的参考答案。27分钟也不由LLM算。L4仍只作为独立候选，不能当作自动确认结论。

## 5. Qwen第二次调用：把已有结果组织成解释

第二次给Qwen的是：**已核实的原文片段＋检索到的规则原文＋Python计算＋确定的结果字段**。

它组织一段中文解释，并引用`[F5]`、`[F6]`、`[R1]`等编号：F是记录证据，R是规则证据。编号在本次结果中可追溯，不是网上引用。

模型不能修改规则阈值、计算结果、分类或风险。返回的状态、分类ID、规则ID和证据ID必须与程序给定的一致，否则整次分析标失败，已完成的计算另存供排错。

**这一步叫“受约束的结果组织”，不是让Qwen独立再做一次分类。** 解释文字仍标为“未审核模型草稿”：JSON合法、引用ID合法，不等于每句话都得到证据支持。网页阶段会把它与程序确定的结果分开呈现。

## 6. 你现在可以运行什么

先切换目录，再执行：

```bash
cd /Users/cam/Documents/Me/clinical-qc-demo
.venv/bin/python scripts/run_m2.py --case QC002 --readable
```

这会真实请求本机Qwen，输出原文、模型抽取、规则排序、计算、建议、解释草稿，以及本次日志路径。不要与旧命令混淆：

| 命令入口 | 做什么 |
|---|---|
| `validate_m1.py --case QC002 --readable` | 读取作者参考答案做预览，不调用模型 |
| `run_m2.py --case QC002 --readable` | 真正调用本机Qwen，不读取参考答案 |

第一次加载模型可能较慢。默认每个HTTP请求最多等120秒，不自动重试、下载模型或换云端。若Ollama没运行或模型输出不合法，会明确失败，不能伪装成“未发现问题”。

### 小练习：把27分钟换成40分钟

独立练习文件 [m2_pk_40_minutes.json](../examples/m2_pk_40_minutes.json) 中把离心时间改为10:46，没有附参考答案：

```bash
.venv/bin/python scripts/run_m2.py --input-json examples/m2_pk_40_minutes.json --readable
```

观察它是否实际抽取10:46，算出40分钟，并给出“本条时间不足规则未命中”。**不是整个研究已经合规。**

你还可以看 [v2练习输入](../examples/m2_pk_v2.json)：日期改为10月15日、方案改为v2，但仍是27分钟。应采用20分钟要求。只改日期不改对应方案，程序不会替你猜版本。

```bash
.venv/bin/python scripts/run_m2.py --input-json examples/m2_pk_v2.json --readable
```

其他可试输入：

```bash
.venv/bin/python scripts/run_m2.py --case DEMO-BOUNDARY-01 --readable
.venv/bin/python scripts/run_m2.py --case DEMO-MISSING-01 --readable
.venv/bin/python scripts/run_m2.py --case DEMO-CONFLICT-01 --readable
```

“边界应如何判断”与“模型本次有没有正确抽取”是两个问题。模型可能改错引文标点、误判范围；遇到这种情况应保留错误，不能看见预期答案就替它改成成功。本轮实际成功/失败都在运行报告中列出。

## 7. 先读哪几段代码

1. [run_m2.py](../scripts/run_m2.py)：输入从哪里来，怎样启动一次运行。
2. [workflow.py](../src/clinical_qc_demo/workflow.py) 的`analyze_record`：流程怎么串起来，哪里调用两次模型。
3. [pk_check.py](../src/clinical_qc_demo/pk_check.py)：原文引用怎么校验，27分钟怎么计算。

之后按需要读 [local_model.py](../src/clinical_qc_demo/local_model.py) 的本机HTTP请求、[retrieval.py](../src/clinical_qc_demo/retrieval.py) 的BM25，以及 [m2_contracts.py](../src/clinical_qc_demo/m2_contracts.py) 的输出字段。

实现采用Ollama的JSON Schema输出约束，再由Pydantic复验；这提供结构约束，不替代业务判断。[Ollama结构化输出文档](https://docs.ollama.com/capabilities/structured-outputs)、[Chat API文档](https://docs.ollama.com/api/chat)。

下一步M3才做可操作网页和人工复核/保存。M4再扩展其他五类与多问题，并单独做合成评测；没有用本次展示案例宣称真实医院准确率。

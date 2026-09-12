# 数据与设计假设

本仓库保存项目代码、技术文档和合成示例，不包含真实病历、真实医院规则库或外部私有原始材料。

## 功能范围

使用四级分类树，自动建议目标到L3，L4作为辅助参考；演示六类问题、人工复核与修改留痕、风险提醒、规则版本管理及合成验证。

QC002用于演示样本管理/PK样本管理/样本处理时间不足：程序计算27分钟，按虚拟规则与30分钟比较，产生中风险提醒和待复核建议。这些阈值及风险策略只服务于演示，不是通用临床标准。

## 数据与规则边界

- 研究、人员、受试者、质控记录、方案及规则均为合成虚拟数据。
- 展示案例、模型输入与评测参考分别保存；合成评测不代表真实医院准确率。
- 实际接入时，真实分类树、阈值、研究方案及风险策略需由获授权的业务人员提供并确认。

## 初始设计与后续扩展

本地版本采用中文React界面、Python、Ollama/Qwen、小型合成规则库和12条展示案例；初判经人工确认，本地提醒不外发消息。不包含微调、K8S或多模态能力。

后续增加了独立的Cloudflare云端演示版本，使用云端Qwen和访客隔离。其模型、运行环境和验证范围与本地不同，见[云端说明](../cloud/README.md)。本地运行记录、密码和数据库不随源码上传。

## 核实过的技术文档

本稿仅依据官方文档确认拟用框架的能力，不固定未实测的性能或版本：

- [Ollama Structured Outputs](https://docs.ollama.com/capabilities/structured-outputs)：JSON Schema约束与Pydantic复验。
- [FastAPI](https://fastapi.tiangolo.com/)：Python API与类型化输入输出。
- [React Learn](https://react.dev/learn)：组件化交互界面。
- [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/)：本机安装与运行条件。

这些能力不等于我们已实现或通过安全/临床验证。计划与实际运行证据必须分开。

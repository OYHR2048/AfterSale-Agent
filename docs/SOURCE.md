# 来源、选择性复用与许可

## 本地 Adaptive RAG

来源是用户提供的 Adaptive-rag 电商客服工程（本地二次开发版），MIT，Copyright (c) 2026 JEen-wang。未找到可核对的新二次开发 Git commit；不编造提交或 upstream URL。保留完整许可于 LICENSES/Adaptive-rag.txt；来源文件内容 SHA-256 记录在 reports/source_manifest.json。

| 原文件 | 新文件 | 处理 |
|---|---|---|
| app/retrieval/tokenizer.py | aftersale/retrieval/tokenizer.py | 直接移植；更改包导入；运行时把 jieba 缓存放外部目录 |
| app/retrieval/bm25.py | aftersale/retrieval/bm25.py | 直接移植检索和索引类；使用简化 RetrievedChunk |
| app/retrieval/fusion.py | aftersale/retrieval/fusion.py | 直接移植 RRF；默认常量本地化 |
| app/schemas/retrieval.py | aftersale/models.py 中 RetrievedChunk | 选择性字段；去除业务分类枚举等依赖 |
| knowledge/{return_policy,return_custom,logistics_exception,shipping,refund_timeline}.md | aftersale/knowledge 同名文件 | 保留虚构标记、原政策内容及版本 |

已阅读 README、ARCHITECTURE、FEATURE_AUDIT、SOURCE、LICENSE、依赖、权限、售后工具、待确认动作与订单数据库实现。借鉴动作保存和事务思想，但新身份/确认/执行模块重新实现：不沿用 anonymous/u_demo 所有者豁免、不复制完整 ORM/旧 graph、不复制真实环境、运行 DB 或模型权重。`operations.md` 是新制定的虚构补充政策，明确换货库存、取消限制和人工工单，不冒充原政策本来支持。

## agent-service-toolkit

主外部源码来自 [JoshuaC215/agent-service-toolkit](https://github.com/JoshuaC215/agent-service-toolkit)，核对提交 [88ebfa1fadd1a207aa92115635abf7ed20d66a88](https://github.com/JoshuaC215/agent-service-toolkit/tree/88ebfa1fadd1a207aa92115635abf7ed20d66a88)，MIT，Copyright (c) 2024 Joshua Carroll。完整许可保留于 LICENSES/agent-service-toolkit.txt。

- `src/memory/sqlite.py` 的 `get_sqlite_saver` 工厂移植到 `aftersale/persistence.py`，传入 Path，移除全局 settings；继续使用 AsyncSqliteSaver.from_conn_string。
- `src/agents/interrupt_agent.py` 的 StateGraph 节点内 interrupt 模式改为售后等待节点；没有复制出生日期任务、递归重试或 InMemoryStore。
- `src/agents/langgraph_supervisor_agent.py` 已检查；固定 web_search 文本、full_history 交接和额外 langgraph-supervisor 依赖不适用。新 graph 使用 StateGraph 显式路由和独立结构化上下文；这是新领域实现，不声称继承了示例的外部搜索能力。
- 阅读直接相关的 service.py、test_auth、test_threads_sqlite、test_service_lifespan 与其他服务测试参考；事件流/生命周期与线程恢复的思路有帮助，但原服务有众多 Agent、Streamlit、AG-UI、多模型厂商和数据库后端，不整包移植，也不移植测试中 SDK 专有对象。新 API、SSE 和测试围绕本地业务约束实现。
- InMemoryStore 与 SQLite checkpoint 的生命周期不同。项目没有使用它作为持久化长期记忆，必要事实/摘要保存在图和业务表。

参考文件仅保存在开发工作目录，不进入公开仓库。没有拼接其他外部源码项目。第三方 Python 包通过 PyPI 安装，版本在 requirements.lock、许可元数据在 reports/dependencies.json；其各自许可证不被本项目 MIT 替换。

## 官方接口核对

[LangGraph interrupts 文档](https://docs.langchain.com/oss/python/langgraph/interrupts) 用于核对 thread_id、interrupt、Command(resume=...) 和节点重执行行为；实际 API 与兼容性以安装版本及测试为准。没有从网页复制完整教程。模型适配器遵循兼容 Chat Completions 的 JSON 协议，MockTransport 验证请求/输出校验；未调用真实模型 API。

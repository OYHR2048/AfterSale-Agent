# 复用选择

- 原 Adaptive RAG：移植 tokenizer、BM25、RRF 与五份虚构政策，改写导入及简化 schema；不复制完整工程、ORM、模型或运行数据库。
- 原权限层与确认层：阅读并借鉴动作绑定/事务思路；原 anonymous/u_demo 归属豁免不适用，改成严格所有者匹配。
- agent-service-toolkit 指定提交：移植 AsyncSqliteSaver 工厂，移除全局 settings 依赖。借鉴 interrupt / Command 同线程恢复。示例出生日期递归、固定搜索、full_history supervisor 与 InMemoryStore 不移植：领域不匹配、上下文混合或不持久化。
- 自行实现：三个固定角色的受限协议、证据规范化、确定性资格规则、确认版本、SQLite 事务、SSE、共用执行器的单 Agent 对照。

具体版权与文件清单见 SOURCE.md。依赖单独选择，不合并旧 pyproject.toml。

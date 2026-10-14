# 原 RAG 适配

独立演示默认使用移植的分词/BM25/RRF 和选择性政策，完全不依赖旧项目运行路径。

设置 AFTERSALE_RAG_URL 后 search_policy 向指定只读地址 POST `{"query":"...","top_k":6}`，接受数组或 `{"chunks":[...]}`。每个片段需 document_id、policy_version、content；只有与本地 trusted corpus 全文和版本相符的片段进入方案。分块片段需要中间适配服务重新组织成匹配文档，或者按新协议提供受信任政策集；不默认扩大本地信任边界。

本轮只读检查确认原 RAG 的 admin 路由只有 tools/skills，并没有现成的检索专用 HTTP API。不能把聊天接口当作无副作用检索：它会保存会话及运行数据。原服务接入需要未来在单独授权下增加只读桥接端点，或实现此协议的薄适配服务；本项目不会擅自修改原 API、环境或数据库。远程路径不承担业务数据、用户身份或写入。接口失败最多一次重试并结束，界面明确标记 remote_validated。

本轮未连接原 RAG HTTP 服务，只有本地移植能力实测。适配器请求/验证行为由 MockTransport 测试，不能当作端到端接入成功。未来要接入完整 RAG，应专门验证 chunk 合并、版本、授权和延迟。

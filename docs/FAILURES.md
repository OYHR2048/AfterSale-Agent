# 实际失败与修复

1. 独立虚拟环境 ensurepip 初次失败：Windows 沙箱临时目录不可写。后续把 TEMP/TMP、pip cache 固定在授权工作目录后成功；没有改系统 Python。
2. 普通沙箱内 asyncio 创建 loopback socketpair 卡住；栈定位到 Windows proactor self pipe。离线测试在获得执行批准后运行，没有更改防火墙或系统设置。
3. 初次闭环失败：langgraph-checkpoint-sqlite 3.0.0 与 aiosqlite 0.22.1 的 is_alive API 不兼容。固定 aiosqlite 0.21.0，pip check 和功能测试通过。
4. API 首轮失败：同步 FastAPI endpoint 在 worker thread 内 create_task，没有 event loop。改为 async endpoint 进行调度。
5. 政策变化防护首次回归失败：文本指纹使用规范化换行，校验却读取原始 CRLF 字节。统一引用校验的文本规范化，同时整个政策集指纹继续能发现文件变化。补充政策变更拒绝写入测试。
6. 真正子进程 os._exit 发现模拟异常测试没覆盖的窗口：业务已提交，checkpoint pending writes 尚未刷盘，恢复仍看到旧 interrupt。新增基于原子业务记录的双投影对账，实际子进程重启/重放通过。
7. 内置浏览器工具初始化失败（kernel assets 路径错误）。改用已安装 Playwright + headless Edge 检查自己的 loopback 页面。确认前零记录、确认后结果和刷新恢复实测；没有安装浏览器扩展或改系统设置。

仍保留的失败路径：工具超过重试/超时预算会结束并报告 failed；越权订单隐藏为不可访问；政策变化时失败关闭，更新政策需重新加载并同步资格规则；真实模型/远程检索协议错误不能降级后冒充真实成功。

# AfterSale-Agent

基于现有 RAG 检索模块和虚构政策的电商售后异常处理项目。Python、LangGraph StateGraph、FastAPI、Pydantic、SQLite，三个固定角色和一个普通后端执行器。

**默认演示使用规则决策替身和词面 RAG，不调用付费模型。所有订单、库存、申请和工单均为本地模拟数据；申请受理不代表审核通过或退款到账。**

支持正常退换、定制质量问题与无理由的区别、换货缺库存、已发货不能取消、物流异常/冲突转工单；补充、修改、拒绝、具体版本确认；持久化 checkpoint、等待期间重启、重复确认和提交后恢复。页面展示实际委派、工具、方案、政策版本和 SSE 时间线。

## 干净环境安装

在项目根目录打开 PowerShell，使用 Python 3.12。环境、缓存和业务数据放到公开目录外：

```powershell
python -m venv ../aftersale-runtime/.venv
$python = '../aftersale-runtime/.venv/Scripts/python.exe'
$env:PYTHONDONTWRITEBYTECODE = '1'
& $python -m pip install -r requirements.lock
$env:AFTERSALE_RUNTIME_DIR = '../aftersale-runtime/data'
$env:AFTERSALE_MODE = 'demo'
& $python -m uvicorn aftersale.api:app --host 127.0.0.1 --port 8018
```

打开 [本地页面](http://127.0.0.1:8018)。服务第一次运行会在运行目录生成 `auth.json`；另开终端读取其中 alice 的随机开发 token，粘贴到页面登录。alice 拥有 ORD10001–ORD10008/ORD10010，bob 拥有 ORD10009。这是可测试的开发身份机制，不是生产登录。API 文档在 [/docs](http://127.0.0.1:8018/docs)。

Linux/macOS 的解释器路径改为 `../aftersale-runtime/.venv/bin/python`；本轮实际验证 Windows + Python 3.12，其他平台未实测。固定 lock 含开发依赖。源码开发可按 pyproject 安装 `.[dev]`；wheel 支持独立包安装。不要把 .venv、auth.json、运行 DB、真实 .env 放进本目录；程序会拒绝位于公开项目内的运行目录。

## 演示

1. 输入 `ORD10002 定制杯质量问题，照片显示破损，超过七天，优先换货`。先检查资格，Supervisor 补查库存，再等待确认。可在证据面板查看政策版本与原文。
2. 确认按钮绑定当前 proposal_id/version。确认前没有业务记录；确认后返回 requested 和实际数据库单号，不声称退款到账。
3. `ORD10004 质量问题，照片显示错字，换货` 展示缺库存后退货退款方案；`ORD10005 取消` 转人工拦截咨询；`ORD10006 物流停滞` 创建真实落库的模拟工单。
4. `ORD10002 质量问题` 等待凭证声明；输入 `照片显示杯身破损` 补充。用户声明不等于已审核图片，本期没有上传/视觉识别。
5. 拒绝则撤销，修改则生成新版本，旧版本确认不能执行。关闭服务、用同一运行目录重启，登录后从已保存任务恢复。已完成的订单若要重做演示，选择新运行目录，保留旧数据库。

single 对照和 multi 共享数据、工具、权限、确认和执行器。默认顺序委派。独立专业上下文和补查循环见 [架构](docs/ARCHITECTURE.md)。

## 测试与固定评测

```powershell
& $python -m pytest -q --basetemp ../aftersale-runtime/tests-run
& $python -m ruff check --no-cache aftersale evaluation tests scripts
& $python -m ruff format --check --no-cache aftersale evaluation tests scripts
& $python -m evaluation.run --split dev --runtime-dir ../aftersale-runtime/eval-dev --output ../aftersale-runtime/dev.json
& $python -m evaluation.run --split test --runtime-dir ../aftersale-runtime/eval-test --output ../aftersale-runtime/test.json
& $python -m scripts.audit_public --output ../aftersale-runtime/public-audit.json
```

每次评测选新运行目录，避免旧记录污染。47 项测试通过；40 条人工预期是 10 dev/30 test，安全、恢复与数据库结果采用确定性断言。规则替身比较结果不代表真实模型质量，不能证明多 Agent 优于单 Agent；实际结果与验证分类见 [EVALUATION](docs/EVALUATION.md) 和 [验证摘要](reports/validation.json)。

## 真实模型与外部检索

启用模型前提供环境变量 AFTERSALE_MODEL_API_KEY、AFTERSALE_MODEL_BASE_URL、AFTERSALE_MODEL，并显式 AFTERSALE_MODE=model、AFTERSALE_ALLOW_MODEL=true。支持 JSON 决策协议，有任务级步骤/调用上限，失败关闭。真实模型本轮未调用；MockTransport 测试只验证协议与失败计数。

真实比较入口还要求明确限额：

```powershell
& $python -m evaluation.run --mode model --allow-model --model-call-budget 30 --limit 1 --split dev --runtime-dir ../aftersale-runtime/model-one --output ../aftersale-runtime/model-one.json
```

该命令会产生 API 费用，只应在自行确认服务商与预算后使用；调用量上限不是美元预算。默认不会执行它。模型 tracing 显式关闭，无自动上传。

AFTERSALE_RAG_URL 是只读检索协议入口，默认关闭。原 RAG 无现成检索专用 HTTP 路由，需要未来授权添加桥接端点；本轮没有修改或启动原服务。受信任文本/版本校验和限制见 [适配文档](docs/RAG_ADAPTER.md)。独立演示不依赖原 RAG、个人磁盘路径或模型权重。

## 阅读与限制

[SPEC](docs/SPEC.md) → [ARCHITECTURE](docs/ARCHITECTURE.md) → graph.py / tools.py / executor.py / service.py → tests → [EVALUATION](docs/EVALUATION.md)。[复用与许可证](docs/SOURCE.md)、[安全边界](docs/SECURITY.md)、[实际失败案例](docs/FAILURES.md)。

固定三个角色，不是动态集群。每单一个 SKU/一件且最多一笔业务记录；不支持部分退款、多 worker、真实支付、生产身份、复杂长期记忆或图片审核。规则演示解析只支持列明的中文场景；6 个短政策文档全文检索只证明最低独立能力。资格规则与政策由开发者同步维护，不自动解释任意新政策。方案有效期默认 15 分钟；不会自动重启执行未确认写入。

MIT；保留来源版权，未自动推送、发布或部署。

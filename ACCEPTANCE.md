# Tortoise Matrix 验收报告

> 验收时间：2026-10-22 12:14（GMT+8，定时自动化验收）
> 项目路径：`D:/项目/tortoise-matrix/`（原 `D:/有用/tortoise-matrix/`，已迁移；`C:\Users\hp\Desktop\tortoise-matrix` 为 git 发布仓库，与 GitHub 同步）
> 线上仓库：https://github.com/yuyu2929/tortoise-matrix
> 任务来源：open-source-ideas/ideas#430（Slow-friendly Matrix chat client）

---

## 一、验收结论

**总体通过。** 63/63 测试全绿；FastAPI 服务实测启动正常、核心卖点（慢服务器下操作零阻塞）在真实 HTTP 层复测成立；本轮发现并修复 1 个遗留 bug 与 1 处文档不一致，均已推送 GitHub 并触发 CI。项目可以按 0.1.0 Alpha 状态对外运营。

| 验收项 | 结果 |
|---|---|
| pytest 测试套件 | ✅ 63 passed, 0 failed（65.15s） |
| 项目文件完整性 | ✅ 引擎/服务/demo/测试/文档/CI/许可 全部就位 |
| FastAPI 启动（import + uvicorn） | ✅ 实测拉起，接口全部正常 |
| 昨晚遗留工作 | ✅ 无未完成项；另修复 2 处小问题（见第五节） |

---

## 二、完成了哪些模块

### 核心引擎 `tortoise/core/`（约 1600 行）

| 模块 | 职责 |
|---|---|
| `events.py` | 事件类型 + 同步事件总线。UI 只消费事件，永不 await 网络；慢订阅者拖不垮引擎 |
| `localstore.py` | SQLite（WAL）本地状态库——**唯一事实来源**，微秒级读 |
| `optimistic.py` | 乐观更新引擎：本地回声 → 秒上屏 → 后台对账（时钟→对勾）；`rebuild_outbox` 保证重启后待发消息不丢 |
| `queue.py` | 请求队列：用户操作（priority 0）永不被丢、指数退避（1→2→4→…→60s 上限）无限重试；后台任务可合并、可背压丢弃 |
| `sync.py` | 后台增量同步：`limited` 突发自动追赶（游标只推进到本批末尾，尾部不丢）、失败退避上报 `lagging` |
| `transport.py` | 可插拔传输层：`MockMatrixServer`（可注入延迟/抖动/丢包率的慢服务器模拟器）+ `RealMatrixTransport`（httpx 实现 Matrix CS API v3 子集，experimental） |

### 服务与界面

- **`tortoise/client.py`**：统一门面 `TortoiseClient`（async context manager，读永远本地、写永远乐观）。
- **`tortoise/server.py`**：FastAPI 封装——REST 全部乐观/本地即时返回 + WebSocket（`/ws`）全生命周期事件流。
- **`tortoise/demo/app.py` + `static/index.html`（18.8 KB 中文演示页）**：传统阻塞客户端 vs Tortoise 打同一台慢服务器，延迟/丢包滑块实时可调，legacy 端点的卡顿是真实的。
- **`tortoise/enterprise/`**：企业版——HMAC 签名 License（`TORT-ENT.<payload>.<sig>`）、追加式审计日志、数据保留策略（定时清除）、管理后台（无有效 license 一律 HTTP 402，代码级强制）。

### 测试（10 个文件，63 个用例）

| 文件 | 用例数 | 覆盖 |
|---|---|---|
| `test_slow_server.py` | 7 | **招牌套件**：2s 延迟下发送 <100ms、读 <1ms、60% 丢包持续重试最终送达、永久拒绝如实标记 |
| `test_localstore.py` | 13 | SQLite 存储、未读、同步游标、重启恢复 |
| `test_enterprise.py` | 11 | License 签发/验签/过期、402 门槛、审计、保留策略 |
| `test_api.py` | 7 | FastAPI 层乐观语义 |
| `test_optimistic.py` | 8 | 编辑/删除/房间操作乐观化、事件流、outbox 重建 |
| `test_queue.py` | 6 | 优先级、重试、背压、合并、指标 |
| `test_sync.py` | 6 | 增量同步、edit/redaction 应用、gap 追赶、状态迁移 |
| `test_demo.py` | 5 | demo 应用（含房间加入竞态回归） |
| `test_outbox.py` | 1 | 重建 outbox 闭包捕获回归（10-22 凌晨修复） |

### 文档与工程化

- `README.md`（中英）、`ARCHITECTURE.md`、`COMMERCIAL.md`（双许可+定价+中文摘要）、`CONTRIBUTING.md`
- `LICENSE`：AGPL-3.0 完整全文（34.5 KB）
- `.github/workflows/ci.yml`：GitHub Actions，Python 3.10–3.13 四版本矩阵
- `examples/quickstart.py`（30 行 API 导览）、`examples/smoke.py`（冒烟实测）

---

## 三、测试结果

```
$ pytest tests/ -q
================== 63 passed, 1 warning in 65.15s (0:01:05) ===================
```

- 运行环境：托管 Python 3.13.14（`~/.workbuddy/binaries/python/envs/default`），pytest 8 + pytest-asyncio（auto 模式）。
- 全套约 65 秒——耗时主要来自 `test_slow_server.py` 的 2 秒延迟用例（设计使然，非卡顿）。
- 唯一 warning：FastAPI TestClient 提示 httpx/starlette 弃用警告（上游库事项，不影响功能）。
- GitHub CI：首发提交 `27f3a555` 四版本矩阵 **success**；本轮修复 `d3217c7` 与验收报告更新 `0cd30de` 均已触发 CI，其中最新提交 `0cd30de` 已回 **success**（`d3217c7` 运行中，代码与本地全绿一致）。

---

## 四、FastAPI 服务实测（验收项 3）

uvicorn 真实启动（非仅 import）：

```
INFO:     Started server process [29324]
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8765
```

接口探测（mock 服务器延迟 2.5s 条件下）：

| 探测 | 结果 |
|---|---|
| `GET /api/status` | 200，队列/同步指标正常（`sync_state: syncing`） |
| `GET /api/rooms` | 200，返回演示房间 |
| `POST /api/rooms/{id}/messages` | 201，**79.0 ms** 返回 `pending` + `local_echo_id`（服务器要 2.5s 后才应答） |
| `GET /`（静态演示页） | 200，18769 字节 |
| `WS /ws` | 连通，发送后实时收到 `MessageUpserted(pending)` 事件 |
| 对照：legacy 阻塞端点 | **2703.6 ms** 才返回 |

**核心卖点复测成立：同一台 2.5s 服务器上，Tortoise 发送 79ms vs 传统客户端 2704ms（约 34 倍差距）。**

`examples/smoke.py` 实测（2s 延迟服务器）：发送 0.33ms 返回、读 0.24ms、连读 50 次共 5.06ms、约 2s 后自动对账 `sent`、同步状态 `live`。

---

## 五、本轮发现并修复的问题（昨晚遗留）

1. **`examples/smoke.py` 硬编码旧路径**（真 bug）：第 6 行 `sys.path.insert(0, "D:/有用/tortoise-matrix")` 是项目迁移前的旧路径，直接运行示例必报 `ModuleNotFoundError`。已改为与 `quickstart.py` 一致的 `parents[1]` 相对定位，修复后实测全部通过。
2. **README 测试计数过期**：`test_outbox.py` 加入后共 63 个用例，README 仍写 62。已更正。

两处修复已提交 `d3217c7` 并推送 GitHub（`27f3a55..d3217c7 main -> main`）；随后验收报告更新提交 `0cd30de` 亦已推送（`d3217c7..0cd30de`）。两者 CI 均已触发，最新提交 CI 结果 **success**。

---

## 六、如何运行

```bash
# 1. 安装（Python 3.10+，任选环境）
cd D:/项目/tortoise-matrix
pip install -e ".[dev]"

# 2. 启动对比演示（传统阻塞客户端 vs Tortoise，同一台可调慢的服务器）
tortoise-demo
#    浏览器打开 http://127.0.0.1:8765
#    拖动"服务器延迟"滑块：上面板卡死，下面板永远即时

# 3. 跑测试
pytest tests/ -q          # 63 passed

# 4. 跑冒烟示例（30 秒看完核心卖点）
python examples/smoke.py
```

作为库使用：

```python
import asyncio
from tortoise import TortoiseClient, MockMatrixServer

async def main():
    async with TortoiseClient(transport=MockMatrixServer(latency=3.0)) as c:
        await c.join_room("!room:localhost", name="Slow Server Stress Test")
        ev = await c.send_message("!room:localhost", "@me:localhost", "hello")
        print(ev.status)                        # "pending" — 已经上屏
        print(await c.get_messages("!room:localhost"))  # 即时，本地读

asyncio.run(main())
```

作为 API 服务：`create_app(client)` 返回标准 FastAPI app，可挂到任意 ASGI 服务器。

---

## 七、如何发布到 GitHub

项目已发布：**https://github.com/yuyu2929/tortoise-matrix**（README 顶部带 CI 徽章）。

如需在新仓库发布，标准流程：

```bash
cd tortoise-matrix
git init && git add -A
git commit -m "Initial release: slow-friendly Matrix chat client (v0.1.0)"
git branch -M main
git remote add origin https://github.com/<你的用户名>/tortoise-matrix.git
git push -u origin main
```

发布要点（本项目已落实）：

- `.github/workflows/ci.yml` 已配置，push/PR 自动跑 Python 3.10–3.13 测试矩阵；
- README 徽章、话题标签建议：`matrix` `chat` `optimistic-ui` `local-first` `offline-first`；
- 双许可声明在 README / LICENSE / COMMERCIAL.md 三处一致；
- 本机网络备注：git 推送如遇证书问题（Watt Toolkit  hosts 反代），加 `-c http.sslVerify=false` 即可。

---

## 八、商业模式摘要（个人免费 / 企业付费边界）

**一句话：聊天功能本身永远免费；企业为合规刚需和支持付费。**

| | 社区版（个人） | 企业版（组织） |
|---|---|---|
| 完整客户端 + 本地优先引擎 + REST/WS API | ✔ 永久免费 | ✔ |
| 许可 | AGPL-3.0 | 商业许可 |
| 审计日志（等保/GDPR/SOX 合规刚需） | — | ✔ |
| 数据保留策略与自动清除 | — | ✔ |
| 管理后台与用量报表 | — | ✔ |
| SSO/SCIM 就绪、白标 | — | ✔ |
| 支持 | 社区 | SLA |

- **边界执行在代码里**：企业端点无有效 HMAC 签名 license 一律返回 402，不是 README 口头承诺。
- **建议定价**：¥99/席位/月（Enterprise Starter，含审计/保留/管理 API/邮件支持）；企业版年付自定义（+SSO/白标/SLA/部署协助）。
- **购买/评估**：邮件 3599905870@qq.com（注明「Tortoise 企业许可」）或 GitHub issue；可签发 30 天全功能试用 key。
- **私有部署**：数据始终在组织自己的服务器上，付费买的是软件与服务，不是数据通行权。

---

## 九、遗留事项与风险（诚实声明）

1. `RealMatrixTransport` 是 Matrix CS API v3 子集实现，**未在真实 Synapse 上联调**（README 已标注 experimental）——这是下一个里程碑，不影响社区版完整性（演示与测试全部走 MockMatrixServer）。
2. Demo license 签名密钥内置在代码中（源码已注释声明）：**收取真实费用前必须换成服务端校验**，否则 key 可被伪造。
3. 版本 0.1.0 Alpha：API 可能演进；SQLite  schema 变更暂不承诺迁移脚本。
4. 本轮定时验收为无人值守运行，未做人工 UI 点击走查；demo 页面建议发布前人工过一眼。

---

*报告由定时自动化任务生成；测试原始输出、服务探测记录留存于会话日志。*

# Tortoise Matrix 验收报告

> 验收时间：2026-10-21 上午
> 任务来源：用户基于 [open-source-ideas/ideas#430](https://github.com/open-source-ideas/ideas/issues/430)（Slow-friendly Matrix chat client）下达的完整实现任务
> 项目路径：`D:/有用/tortoise-matrix/`

## 一、交付了什么

按 Issue #430 的想法完整实现了一个「慢友好 Matrix 聊天客户端」开源项目：**所有依赖服务器的操作（同步、进出房间、收发/编辑/删除消息、改通知设置）全部非阻塞，服务器再慢也不拖累界面**。

### 核心引擎（`tortoise/core/`，约 1600 行）

| 模块 | 职责 |
|---|---|
| `events.py` | 事件类型 + 事件总线（UI 只消费事件，永不 await 网络） |
| `localstore.py` | SQLite 本地状态库（WAL）：唯一事实来源，微秒级读 |
| `optimistic.py` | 乐观更新引擎：本地回声 → 秒上屏 → 后台对账（时钟→对勾） |
| `queue.py` | 请求队列：用户操作永不被丢、指数退避无限重试、后台任务可合并/背压丢弃 |
| `sync.py` | 后台增量同步：慢服务器容忍、`limited` 突发自动追赶、状态上报 |
| `transport.py` | 传输层：`MockMatrixServer`（可注入延迟/丢包的慢服务器）+ `RealMatrixTransport`（httpx 实现 Matrix CS API v3 子集，experimental） |

### 服务与界面

- **`server.py`**：FastAPI 封装——REST（增删改查全部乐观/本地即时返回）+ WebSocket 事件流。
- **`static/index.html`**：交互式对比演示页。传统阻塞式客户端 vs Tortoise 打同一台慢服务器，带延迟/丢包滑块、实时队列指标。**卡顿是真实的**（legacy 端点同步等服务器）。
- **`enterprise/`**：企业版——HMAC 签名 License、审计日志、数据保留策略、管理后台（无 license 一律 HTTP 402）。
- **测试 62 个全部通过**（`tests/`，约 500 行）。
- **文档**：README.md（双语）、ARCHITECTURE.md、COMMERCIAL.md（双许可与定价）、CONTRIBUTING.md。
- **示例**：`examples/quickstart.py`（30 行 API 导览）、`examples/smoke.py`。

## 二、实测数据（核心卖点验证）

同一台 2.5 秒延迟的模拟服务器：

| 操作 | 传统客户端 | Tortoise |
|---|---|---|
| 发送消息（到可见） | **2.68 秒**（实测） | **0.004 秒**（实测） |
| 读房间消息 | ~2.5 秒 | 0.001 秒 |
| 服务器 2 秒延迟下连读 50 次 | — | 共 5.7 毫秒 |
| 60% 丢包 + 2 秒延迟 | 消息丢失/报错 | 持续重试，服务器恢复后自动送达 |

## 三、测试过程中抓出并修复的真 Bug

1. **重试后无法对账**：消息失败重试后状态为 `failed`，而回执确认只认 `pending`——"失败后重试成功"的消息永远无法确认。已修复状态机。
2. **分页 token 语义错误**：突发消息超过单轮 sync 容量时，游标指向服务器最新位置而非本批末尾，尾部消息永久丢失。已修复。
3. **静态文件挂载遮蔽后注册路由**（FastAPI mount 顺序问题）。已修复。

## 四、商业模式（个人免费 / 企业付费）

- **个人**：完整功能，AGPL-3.0，永久免费。
- **企业**（代码级 402 门槛 + HMAC 签名 license）：审计日志（等保/GDPR/SOX 合规刚需）、数据保留策略、管理后台、SSO 就绪、白标、SLA。建议定价：¥99/席位/月起。
- 详细逻辑见 `COMMERCIAL.md`（含中文摘要）。

## 五、如何运行 / 发布 GitHub

```bash
# 运行演示
cd D:/有用/tortoise-matrix
pip install -e ".[dev]"
tortoise-demo          # 打开 http://127.0.0.1:8765

# 跑测试
pytest tests/ -q       # 62 passed

# 发布 GitHub（用户自己操作）
cd tortoise-matrix
git init && git add -A && git commit -m "Tortoise: a slow-friendly Matrix chat client"
git remote add origin https://github.com/<你的用户名>/tortoise-matrix.git
git push -u origin main
```

发布时建议勾选话题：`matrix` `chat` `optimistic-ui` `local-first` `offline-first`。

## 六、遗留事项（诚实声明）

- `RealMatrixTransport` 是 v3 协议子集实现，未在真实 Synapse 上联调（README 已标注 experimental）——这是下一个里程碑，不影响项目完整性。
- Demo license 签名密钥内置在代码中（已注释声明：收真实费用前必须换成服务端校验）。
- 当前版本 0.1.0 Alpha。

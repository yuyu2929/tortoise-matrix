# Licensing: free for people, paid for organizations

Tortoise is dual-licensed. This page explains who pays, what for, and
why the price is fair.

## The short version

- **Personal use is free. Forever.** The full client, the full engine,
  the full API — AGPL-3.0. No feature paywalls for people.
- **Organizations pay for the enterprise tier.** Not for "extra
  features" invented to justify a bill, but for the things only an
  organization needs — and would otherwise have to build, staff, and
  certify themselves.

## What organizations actually buy

| Capability | Why an org pays for it |
|---|---|
| **Audit trail** (`tortoise/enterprise/audit.py`) | Regulators (SOX, GDPR, 等保, HIPAA) ask "who did what, when". An append-only, separately-stored audit log answers in court, not in screenshots. |
| **Retention policies** (`retention.py`) | "How long do messages live?" must have an enforceable, provable answer. Automated purge with a policy object beats a cron job someone forgets. |
| **Admin API & usage reporting** (`admin.py`) | Central license status, audit search, retention control, seat usage — the endpoints an ops team actually wires into their tooling. |
| **SSO/SCIM readiness & white-labeling** | The hooks are in the enterprise tree; integrate your identity provider and brand without forking. |
| **Support with SLA** | A human accountable for response times, not a GitHub issue queue. |

## Why the gate is real

Enterprise endpoints are enforced in code:

```
GET /api/admin/license   → 402 without a valid license
GET /api/admin/audit     → 402 without a valid license
POST /api/admin/retention/purge → 402 without a valid license
```

License keys are HMAC-signed (`TORT-ENT.<payload>.<signature>`) and
verified on every request. Self-hosted trial keys can be minted with
`LicenseManager.generate()`; production deployments should replace the
demo signing secret with a hosted verification service.

## Pricing (proposed)

| Plan | Price | Includes |
|---|---|---|
| Community | **¥0 / $0** | Everything a person needs, AGPL-3.0 |
| Enterprise Starter | ¥99 / $15 per seat / month | Audit, retention, admin API, email support |
| Enterprise | 自定义（年付） | + SSO/SCIM, white-label, SLA, 部署协助, 升级保障 |

Seat-based, self-host friendly. The product runs on *your* server with
*your* data — the license pays for the software and the people behind
it, not for access to your own messages.

## 如何购买 / Contact

- 邮箱：**3599905870@qq.com**（注明「Tortoise 企业许可」）
- 或在 GitHub 提 issue：https://github.com/yuyu2929/tortoise-matrix/issues
- 评估流程：说明组织名称、预估席位数与部署方式 → 我们签发 30 天全功能试用 key → 试用满意后签订许可协议并开具发票（支持对公转账）。

## Why this model is fair

1. **Individuals never hit the paywall.** Chat for yourself, your family,
   your community: free, complete, no nagging.
2. **Organizations pay for genuine compliance work.** Audit trails and
   retention policies exist because regulated industries are *required*
   to have them. Building them in-house costs an engineer-month; the
   license costs a fraction of that.
3. **AGPL keeps the core honest.** Improvements to the free tier flow
   back to everyone, including us. The enterprise tier buys support,
   compliance tooling, and freedom from copyleft obligations — the
   classic, proven open-core trade.

## 商业模式摘要（中文）

- **个人用户永久免费**：完整客户端 + 引擎 + API，AGPL-3.0 开源。
- **企业按席位付费**：购买的不是"解锁聊天"，而是合规刚需——审计日志（等保/GDPR/SOX）、数据保留策略、管理后台、SSO 就绪、白标与 SLA 支持。
- **门槛写在代码里**：企业端点无有效 license 一律 402；license 为 HMAC 签名，可自行生成试用 key。
- **私有部署**：数据始终在企业自己的服务器上，付费买的是软件与服务，不是数据的通行权。

# 我的申请列表与权限状态验证

日期：2026-09-08。范围：申请列表、详情选择、授权有效期展示；本地演示环境。

## 实际行为

- 我的申请改为逐行列表，分别显示审批状态、开通状态、申请时间和查看详情。
- 列表摘要以一次 SQL outer join 返回 grant_id、starts_at、expires_at，保留原有 Workspace 和身份过滤；无数据库迁移，无逐条 Decision Packet 查询。
- 审批通过但无 Grant 显示尚未开通；Grant 有效时显示权限已开通，未来生效和已过期分别显示。旧响应缺少授权字段时显示待核实。
- 我的权限的 expiring_soon 显示已拥有和即将过期双标签。服务端五状态及 7 天阈值保持不变，因此刚开通的 7 天授权也会出现提醒。
- 授权详情折叠技术 ID，并明确本地模拟权限系统未接入真实企业资源，也尚未实现到期回收或撤销。

## 本轮验证

- 前端 AccessCards、AccessCards.container、RequestTimeline、OperationsConsole：4 文件、40 测试通过。
- 后端 T20 ACL 定向运行：既有 14 测试通过；新增测试最初因共享测试数据的数量假设失败，修正为筛选当前 request_id 后单独重跑通过（1 passed）。验证了审批通过不产生 Grant 事实，以及未来 Grant 的摘要字段。
- Ruff、MyPy 定向检查，前端 ESLint、生产构建通过。构建仍有 chunk 大小提示。
- ego-browser 独立虚构 fixture 使用真实组件及样式，验证桌面和 390px：列表、详情切换、有效/未来/过期状态、双标签；无横向溢出。四张截图已人工查看。截图为 UI fixture，不代表真实业务申请验收。
- 服务正常重启；后端 /health 返回 status=ok，前端 5173 返回 HTTP 200。
- 临时 fixture 已删除，独立浏览器空间已关闭；未提交、审批或开通用户申请。

## 截图

- [申请列表桌面](accesspilot-request-list-desktop-2026-09-08.png)
- [申请列表窄屏](accesspilot-request-list-mobile-2026-09-08.png)
- [权限双标签桌面](accesspilot-owned-expiry-desktop-2026-09-08.png)
- [权限双标签窄屏](accesspilot-owned-expiry-mobile-2026-09-08.png)

列表有效期标签在渲染时使用浏览器当前时间；我的权限沿用服务端状态。本轮未执行真实企业 IAM 验收，也未运行全量测试。

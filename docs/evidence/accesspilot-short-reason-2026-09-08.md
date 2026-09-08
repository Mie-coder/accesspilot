# 简短理由续答修复与验证

日期：2026-09-08。路径：轻量交付。仅修改理由 Cursor 续答及测试；不提交、不推送、不部署，不处理授权到期查询问题。

## 行为

服务端正在追问 justification 且 Cursor 仍有效时，接受“演示需要”“季度汇报”“给新人培训”等自由文本，不再要求理由包含关键词。Legacy 与 Flow 2 使用同一判断，直接保存理由，不调用结构化模型。已有问句、帮助、读操作、安全边界继续优先；身份、期限、权限或确认等显式指令不作为理由原样保存。无活动 Cursor 时，“演示需要”不会重启已退出的收集。

该规则基于当前追问接收陈述性文本，不代表增加了通用语义意图模型，也不代表理由已获审批。

## 证据

- TDD：新增三条简短理由用例，修复前 3 failed / 18 passed，均因理由为 null、意图为 help；修复后通过。
- 首轮 Legacy/Flow 2/上下文提取：84 passed，0 skipped。
- 扩展回归发现“请帮助 / help / 功能”被误收，已修复。重启共享数据库意外打断一轮测试，该轮作废；服务恢复后重新完整运行选定范围。
- 最终：`test_t18_cursor.py`、`test_t32_request_graph.py`、`test_service.py`，115 passed、0 failed、0 skipped。通过唯一临时 PostgreSQL 库运行，未重置演示数据库。
- 对本次两个源码文件及两个测试文件执行 Ruff，通过；对两个源码文件执行 MyPy，通过。
- Flow 2 编译图与 Legacy 对照测试验证三类简短理由、120 天保持、confirmed=false、模型零调用及无下游提交写入。
- 本地服务保留数据重启后，通过 5173 前端代理和独立新登录会话，真实执行“申请 insighthub.dashboard_view 权限”→“120天”→“演示需要”。最终 awaiting_confirmation，duration_days=120，justification=演示需要，confirmed=false。
- 真实接口结果见 [JSON](accesspilot-short-reason-live-2026-09-08.json)。当前本地演示运行 Legacy；Flow 2 为自动化测试证据。本轮未执行新的浏览器 UI 操作。

## 保留范围

用户现有会话、草稿、业务数据与其他未提交改动保留。独立测试会话停在待确认，未调用确认、提交、审批或发放动作。无数据库/API 合同变化，无前端或设计改动，视觉与部署验收不适用。

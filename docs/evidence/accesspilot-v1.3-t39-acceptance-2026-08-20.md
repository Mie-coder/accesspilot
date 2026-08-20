# AccessPilot v1.3 T39 验收证据包（Verified）

**Ticket：** T39 — 「对话 / 轨迹」只读 UI 与最近三轮
**实现基线：** `0aa953c`（T38）；T39 当前为未提交工作树 revision
**日期：** 2026-08-20
**状态：** `Verified`；独立验收 P0/P1/P2=0/0/1，P2 不阻塞

## 1. 范围与边界

本 Ticket 沿用既有 AccessPilot 设计语言，把前端 Legacy 安全事件原型升级为 T36 真实轨迹事件的只读投影，并补齐最近三轮、逐轮引擎、可访问 Tab 与完整状态展示。

- 轨迹只消费已经由 API/SSE 返回的 `WorkspaceEvent`，不读取 checkpoint、debug/value stream 或隐藏运行时状态；
- 切换视图只改变展示，不发起 API、不 resume、不执行工具、不确认或提交申请；
- 默认生产入口在 T40 前仍为 Legacy，浏览器不得伪装成 LangGraph；
- 不新增恢复、重放、编辑、提交、执行工具或测试 fixture 控制面。

## 2. 三条验收标准

### AC1 — 可访问、零副作用且保持对话状态的 Tab

顶部「对话 / 轨迹」实现 roving `tabindex`、`aria-selected`、`aria-controls`、`tabpanel` 与 `hidden`，支持 ArrowLeft/Right、Home、End 切换并同步焦点。两个 panel 始终挂载，只改变可见性，因此 ChatThread 的未发送草稿与本地状态不会因切换被卸载。

组件测试和真实页面 smoke 均确认 Tab 切换不调用 append、resume、工具、确认或提交；独立浏览器检查中 API resource count 保持 `4 → 4`，服务端没有新增请求。

### AC2 — 最近三轮与真实事件驱动

前端先按每轮受支持事件的最大全局数据库 `id` 选择最近三轮，再按 `id` 对组内事件排序；SSE `seq` 和 `event_key` 不参与跨轮排序，最新轮默认展开。乱序输入和晚到事件均有测试反证。

每轮引擎只由该轮 `turn.started.orchestrator` 决定：真实图显示 LangGraph、历史 Legacy 显示 `Legacy · ConversationService`、缺少可信起始事实显示 Unknown。`flow_version` 或图事件本身不能单独伪造 LangGraph。

T36 的 node、route、model、retrieval、tool、draft/State、input required/resumed、terminal/error/interrupted 均按真实事件映射。只有 `retrieval.*` 的 pgvector 路径显示 RAG；Legacy `tool.summary search_policies` 及其他只读工具仍显示 Legacy/Tool，不从 intent 猜测未发生节点。

### AC3 — 全状态、安全详情与无操作控制面

组件稳定呈现空、加载/重连、运行中、等待 HITL、可恢复错误、完成、中断和未知事件状态，且终态优先级有测试锁定。未知事件内容不渲染、不参与最近三轮选组，只以安全计数提示忽略。

安全详情对每个 event type 新建显式白名单对象，`JSON.stringify` 不接触 raw payload，也不存在 default/raw fallback。测试反证禁止的 prompt/system prompt、hidden reasoning、token/cookie/CSRF/auth、raw checkpoint/debug/value、异常栈和内部预算字段不会显示。轨迹内部没有任何按钮，并固定说明“这是执行事实的只读投影，不是模型思维链”。

## 3. TDD 与独立验收

实现前新增测试得到可信 `8 failed / 8 passed`：失败分别覆盖只读说明/加载态、按错误的首事件截轮、缺少逐轮 engine、T36 全事件不支持、状态徽标缺失、未知事件处理、Legacy search_policies 误标 RAG，以及 Tab 无 roving 键盘与 ChatThread 被卸载。最小实现后转为 `16 passed`。

独立验收重新读取 Spec、Ticket、设计语言卡、T36 Schema/producer 与完整差异，未发现可达 P0/P1；实测三条 AC、完整 Web 回归、静态门禁和真实 Legacy 页面后判定 PASS。

## 4. 实测证据

实现阶段：

- T39 定向：`16 passed`；
- WorkbenchRuntime、ChatThread、streaming-runtime 受影响回归：`14 passed`；
- ESLint、两套 TypeScript、Vite 生产 build、`git diff --check`：通过；
- 视觉：1440×900、1024×768、390×844；1024 首轮发现 engine 徽标孤字换行，修复后重拍无溢出或遮挡；
- 真实 ego-browser smoke：Tab 请求资源数 `34 → 34`、键盘路径与对话草稿保活通过。

独立验收：

- T39 + 受影响测试 7 个文件：`38 passed`；
- 完整 Web Vitest 19 个文件：`103 passed`；
- ESLint、`tsconfig.app`、`tsconfig.node`、Vite 生产 build、`git diff --check`：通过；
- 1440×900、1024×768、390×844 均满足 `scrollWidth === clientWidth`，无水平溢出，移动端右栏正常下排；
- 键盘焦点、Tab aria/hidden、对话保活与 API resource `4 → 4`：通过；
- Vite 仅保留既有单 chunk 超过 500 kB warning。

浏览器默认启动脚本可能执行数据库 bootstrap 并使用本地已配置的外部 embedding，因此独立验收改用无 `.env`、无模型凭证、跳过 bootstrap 的 Legacy uvicorn；没有把环境限制记成 LangGraph 浏览器通过。T40 尚未切流，真实浏览器只验证 Legacy 页面与可恢复错误；LangGraph 全事件由组件测试证明。ego screenshot API 超时后使用 Playwright fallback 做三尺寸像素检查，没有拦截网络或新增 fixture。

## 5. 非阻塞 P2

开启系统 `prefers-reduced-motion: reduce` 后，`.agent-trajectory-turn > summary::before` 的折叠箭头仍保留 `transform 150ms`；当前 reduced-motion 规则只压缩 animation，不清除 transition。公开影响仅为展开/折叠轨迹时仍有短旋转，未破坏功能、内容、键盘或安全边界。

最小后续修复是在 reduced-motion media query 中为该伪元素设置 `transition: none`，或统一把 transition duration 压缩到近零。按仓库规则 P2 不阻塞 T39 收口，留待后续维护或 T41 最终体验门禁决定是否处理。

## 6. 修改文件

- `apps/web/src/AgentTrajectory.tsx`
- `apps/web/src/AgentTrajectory.test.tsx`
- `apps/web/src/App.tsx`
- `apps/web/src/App.test.tsx`
- `apps/web/src/styles.css`
- 本证据包与 v1.3 Spec、Ticket、Change Ledger 当前状态记录

## 7. 当前停点

T39 已 `Verified`，用户已授权仅本地提交。未进入 T40；未授权推送、合并、部署或修改正式简历。

# AccessPilot T17 最终验收评审记录

**评审日期：** 2026-08-11
**评审类型：** `final-acceptance`
**范围：** 产品化评测、流式观测、身份/Demo 隔离、政策与安全边界、作品集证据
**权限：** 本地只读验收；不修改源码或测试，不推送、合并、部署，不包含密钥、私有地址或原始内部内容

## 1. 验收合同与边界

T17 继承 v1.1 产品功能书和 Spec，固定验证以下场景：

- 产品/Demo 身份隔离；
- 权限名称实体解析与重新校验；
- 当前轮 SSE 顺序、唯一终态和 Workspace 重连；
- 取消后的 `turn.interrupted`；
- 政策三态、自审批禁止和复合安全输入；
- 模型预算耗尽后的确定性降级；
- 草稿/Workspace 隔离。

本 Ticket 不接入真实 SSO、OIDC、企业目录、IAM 或政策数据；不把本机确定性适配器的延迟外推为 provider token 性能、p50/p95、生产 SLA 或真实模型质量。

## 2. 可复现来源

- 命令：`./scripts/run-evals.sh`（内部调用固定场景 runner）。
- 适配器：本机 direct-ASGI `deterministic_offline`。
- 脱敏 JSON：`docs/evidence/accesspilot-v1.1-evaluation-2026-08-11.json`。
- JSON source revision：`a45b5f7`，表示 T16 基线加 T17 工作树运行来源。评测运行时无法预先写入包含本评审文档修改的未来提交哈希，因此不将该值当作 T17 文档提交哈希。
- 前后对比与作品集清单：`docs/evidence/accesspilot-v1.1-portfolio-evidence.md`。

## 3. 评测结果

| 场景 | 覆盖 | 结果 |
| --- | --- | ---: |
| T17-01 | identity_demo | 3/3 |
| T17-02 | resolution | 4/4 |
| T17-03 | current_turn_stream | 2/2 |
| T17-04 | workspace_reconnect | 2/2 |
| T17-05 | turn_interrupted | 2/2 |
| T17-06 | policy_states | 4/4 |
| T17-07 | self_approval | 4/4 |
| T17-08 | budget_degradation | 2/2 |
| T17-09 | draft_isolation | 3/3 |
| T17-10 | compound_security | 4/4 |
| **合计** | **10 场景 / 30 案例** | **30/30** |

安全与流式观测：复合攻击 4/4 阻断；计费模型调用 1 次；重连回放 2 条、重复 0 条。以上是固定 fixture 的本机一次运行，不是线上比率或容量承诺。

## 4. 流式单样本观测

| 观测 | 数值 | 解释 |
| --- | ---: | --- |
| 首事件 | 7.666417 ms | 从本机 ASGI 请求开始到首个有效事件 |
| 首个非空 `message.delta` | 35.480167 ms | 首个非空回答增量，不等同 provider token |
| 完成 | 39.017833 ms | 收到唯一 `message.completed` |
| 终态 | `message.completed` | 本样本无错误或取消 |

`provider_token_latency`、`policy_recall@k`、`production_sla` 为 N/A。没有 p50、p95、吞吐、真实供应商延迟或线上成功率样本，不能从这些数值推导相关结论。

## 5. Ticket 验收映射

### AC-1 固定场景覆盖

10 个固定场景覆盖身份、实体解析、流式顺序与重连、`turn.interrupted`、政策状态、自审批、预算降级、草稿隔离和复合攻击，30/30 通过。

### AC-2 全量质量与浏览器

- 后端全量：339 passed、1 warning；
- 前端：14 个测试文件、48 tests passed；
- Ruff 通过；MyPy 41 个源文件通过；
- Alembic 无漂移，head `0006`；
- TypeScript app+node、ESLint、正式构建通过；构建保留 561.02 KB chunk warning，记录为 P2；
- 1440×900、1024×768、390×844 无横向溢出；
- 歧义候选支持键盘选择并重新校验；政策中心显示 8 条目录；自审批回答引用 `POL-006`；390px 断线显示 `role=status` 重连提示。

### AC-3 前后对比与简历候选

前后对比、作品集证据清单和 5 条中文简历候选已记录在 `docs/evidence/accesspilot-v1.1-portfolio-evidence.md`。候选只引用已验证行为；正式简历未修改。

## 6. 外部评审与安全边界

T17 专项没有获得 DeepSeek 或 Claude 的明确外发授权，因此未发送评审包。本记录不将未发送写成通过或失败，也不把 T14 的 DeepSeek 脱敏评审结论外推到 T17。外部评审如日后获授权，只能接收不含源码、密钥、私有地址、数据库凭据、系统提示词和隐藏推理的脱敏摘要。

## 7. 已知限制与后续

- 本次流式测量使用 deterministic_offline 单样本；真实 provider token 流和供应商延迟尚未测量。
- 政策 `recall@k`、引用正确率的统计分母尚未建立，不能伪造召回数字。
- 561.02 KB chunk warning 是 P2 拆包候选，不阻塞本地验收。
- 生产 SSO/IAM、密钥管理、集中日志、监控告警、高可用和线上 SLA 不属于本 Ticket。

## 8. 禁止声明

本项目及后续作品集材料不得：

1. 填写未实测的延迟、准确率、召回率、成本、成功率、p50/p95 或 SLA 数字；
2. 将服务器多片 `message.delta` 写成 provider token 流，或将 deterministic_offline 单样本写成真实模型质量；
3. 宣称已接入真实 SSO、企业目录、IAM、真实政策数据、生产授权或 production-ready；
4. 外发源码、`.env`、API key、数据库凭据、私有地址、系统提示词、隐藏推理、原始异常栈或内部模型预算；
5. 在没有专项授权时调用或声称 DeepSeek/Claude 评审，或把独立 reviewer 结论冒充外部模型结论；
6. 未经用户另行确认修改正式简历；
7. 推送、合并、部署或把本地验收写成最终发布。

## 9. 结论

T17 固定评测、质量门禁、三档浏览器行为和脱敏证据达到当前 Ticket 验收条件。结论为：**T17 可进入本地提交；v1.1 本地迭代完成。** 该结论不代表推送、合并、部署、真实供应商性能或生产发布。

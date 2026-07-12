# AccessPilot 访问申请流程

## 黄金路径

```mermaid
flowchart TD
    A[员工发起申请] --> B[Agent 多轮收集信息]
    B --> C[生成 RequestDraft]
    C --> D{必填字段完整?}
    D -- 否 --> B
    D -- 是 --> E[用户确认申请内容]
    E -- 修改 --> B
    E -- 确认 --> F[提交 Access Request]
    F --> G[风险审查 Agent 检索政策]
    G --> H[创建 Approval Case]
    H --> I[直属经理审批]
    I -- 驳回 --> R[rejected]
    I -- 通过且需要双审批 --> J[数据所有者审批]
    I -- 通过且无需双审批 --> K[权限开通]
    J -- 驳回 --> R
    J -- 通过 --> K
    K -- 成功 --> L[创建 Access Grant]
    L --> M[completed：员工获得权限]
    K -- 超时或失败 --> N[recoverable_error]
    N -- 查询状态后安全重试 --> K
```

## 阶段说明

| 阶段 | 业务含义 | 关键约束 |
| --- | --- | --- |
| `collecting` | Agent 收集申请信息 | 一次只追问必要的缺失字段 |
| `validating` | 校验字段和申请资格 | 未通过校验不得提交 |
| `awaiting_confirmation` | 等待申请人确认草稿 | 未确认不得调用提交工具 |
| `awaiting_approval` | 等待人工审批 | 高风险权限按顺序审批经理和数据所有者 |
| `provisioning` | 执行实际权限开通 | 使用幂等键，避免重复授权 |
| `completed` | 权限已经实际开通 | 必须存在 Access Grant |
| `rejected` | 任一级审批拒绝 | 不得进入权限开通 |
| `recoverable_error` | 工具或下游系统暂时失败 | 记录审计事件，允许安全恢复 |

## 关键边界

- 审批通过不等于权限已经开通。
- 风险审查 Agent 只读取申请和政策，不修改业务状态。
- 开通失败不能标记为 `completed`。

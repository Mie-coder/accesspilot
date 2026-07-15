# AccessPilot 数据库 ER 图

## 如何阅读

ER 图用来表示“数据库有哪些表、每张表保存什么、表之间如何关联”。

- `PK`：主键，唯一识别一条记录。
- `FK`：外键，指向另一张表的记录。
- `UK`：唯一约束，防止重复数据。
- `||--o{`：一对零个或多个。
- `||--o|`：一对零个或一个。

## 完整关系图

```mermaid
erDiagram
    WORKSPACES {
        uuid id PK "工作区主键"
        string token_hash UK "Cookie Token 哈希"
        jsonb draft "未提交申请草稿"
        string fault_mode "故障注入模式"
        timestamptz created_at "创建时间"
    }

    EMPLOYEES {
        string employee_id PK "虚构员工编号"
        string name "姓名"
        string department "部门"
        string manager_id FK "直属上级"
        array roles "角色列表"
    }

    SYSTEMS {
        string code PK "系统编码"
        string name UK "系统名称"
    }

    ENTITLEMENTS {
        string code PK "权限编码"
        string system_code FK "所属系统"
        string name "权限名称"
        string risk_level "风险等级"
        string approval_policy "审批策略"
        string owner_id FK "数据所有者"
        boolean self_service_allowed "是否允许自助申请"
        array eligible_departments "允许部门"
        array eligible_roles "允许角色"
        int max_duration_days "最长天数"
    }

    ACCESS_REQUESTS {
        uuid id PK "正式申请主键"
        uuid workspace_id FK "所属工作区"
        string requester_id FK "申请人"
        string entitlement_code FK "目标权限"
        string project_code "项目编码"
        text data_scope "数据范围"
        text business_reason "申请用途"
        date start_date "开始日期"
        int duration_days "期限天数"
        string request_status "申请状态"
        timestamptz confirmed_at "用户确认时间"
        timestamptz created_at "记录创建时间"
    }

    APPROVAL_CASES {
        uuid id PK "审批流程主键"
        uuid workspace_id FK "所属工作区"
        uuid request_id FK,UK "一份申请最多一份审批流"
        string approval_status "整体审批状态"
        timestamptz created_at "创建时间"
    }

    APPROVAL_STEPS {
        uuid id PK "审批节点主键"
        uuid workspace_id FK "所属工作区"
        uuid approval_case_id FK "所属审批流"
        int step_order "执行顺序"
        string approver_id FK "明确审批人"
        string approver_role "审批角色"
        string step_status "节点状态"
        text comment "审批意见"
        timestamptz decided_at "决策时间"
        timestamptz created_at "创建时间"
    }

    ACCESS_GRANTS {
        uuid id PK "实际授权主键"
        uuid workspace_id FK "所属工作区"
        uuid request_id FK,UK "一份申请最多一条授权"
        string idempotency_key UK "幂等键"
        timestamptz starts_at "生效时间"
        timestamptz expires_at "失效时间"
        timestamptz created_at "创建时间"
    }

    AUDIT_EVENTS {
        uuid id PK "审计事件主键"
        uuid workspace_id FK "所属工作区"
        uuid request_id FK "可选关联申请"
        string actor_type "employee agent system"
        string actor_id "操作者标识"
        string event_type "事件类型"
        jsonb details "结构化详情"
        timestamptz created_at "发生时间"
    }

    POLICY_CHUNKS {
        uuid id PK "政策分块主键"
        string policy_code "政策编号"
        string title "政策标题"
        int chunk_index "分块顺序"
        text content "政策正文"
        jsonb metadata "来源元数据"
        vector_512 embedding "512 维向量，允许为空"
        timestamptz created_at "创建时间"
    }

    WORKSPACES ||--o{ ACCESS_REQUESTS : "隔离申请"
    WORKSPACES ||--o{ APPROVAL_CASES : "隔离审批流"
    WORKSPACES ||--o{ APPROVAL_STEPS : "隔离审批节点"
    WORKSPACES ||--o{ ACCESS_GRANTS : "隔离授权"
    WORKSPACES ||--o{ AUDIT_EVENTS : "隔离审计事件"

    EMPLOYEES o|--o{ EMPLOYEES : "管理下属"
    EMPLOYEES ||--o{ ACCESS_REQUESTS : "发起申请"
    EMPLOYEES ||--o{ APPROVAL_STEPS : "执行审批"
    EMPLOYEES o|--o{ ENTITLEMENTS : "负责数据"

    SYSTEMS ||--o{ ENTITLEMENTS : "包含权限"
    ENTITLEMENTS ||--o{ ACCESS_REQUESTS : "被申请"
    ACCESS_REQUESTS ||--o| APPROVAL_CASES : "创建审批流"
    APPROVAL_CASES ||--|{ APPROVAL_STEPS : "包含节点"
    ACCESS_REQUESTS ||--o| ACCESS_GRANTS : "开通后产生"
    ACCESS_REQUESTS o|--o{ AUDIT_EVENTS : "记录时间线"
```

`POLICY_CHUNKS` 故意不连接业务表：它是全局虚构政策知识库，风险审查 Agent 通过向量检索读取它，不用外键绑定某一份申请。

## 一笔申请如何穿过这些表

1. Agent 收集信息时，可修改的草稿保存在 `WORKSPACES.draft`。
2. 用户确认后，才生成不可随意修改的 `ACCESS_REQUESTS`。
3. 提交申请时创建一个 `APPROVAL_CASES`，再按策略创建一个或多个 `APPROVAL_STEPS`。
4. 所有必需审批节点通过后，IAM 模拟器尝试开通权限。
5. 只有 IAM 真正开通成功才创建 `ACCESS_GRANTS`；如果超时，只写入 `AUDIT_EVENTS`，不伪造授权记录。

## 必须记住的数据库约束

- `APPROVAL_CASES.request_id` 唯一：一份申请不能出现两条审批流。
- `(approval_case_id, step_order)` 唯一：同一审批流不能出现两个“第 1 步”。
- `ACCESS_GRANTS.request_id` 和 `idempotency_key` 唯一：重试不能重复授权。
- 业务子表同时校验 `workspace_id` 和父记录 ID：Workspace A 不能引用 Workspace B 的申请。
- `POLICY_CHUNKS.embedding` 允许为空，但检索时必须报可恢复错误，不得编造政策结论。

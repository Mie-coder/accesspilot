# AccessPilot 学习术语表

AccessPilot 教学中已经理解并固定使用的业务和技术术语。

## Terms

**entitlement_id**:
要申请的具体权限的稳定、唯一编号，例如 `raw_customer_export`。它不是系统名称，也不是自然语言描述。
_Avoid_: 权限名称、系统编号

**None**:
Python 中表示“当前没有值”；在 `ParsedReply` 中表示用户本轮没有提供该字段，模型不得猜测。
_Avoid_: 空字符串、默认权限

**模型输出校验**:
把模型返回的普通 JSON 字符串转换成领域模型，并拒绝未知字段或错误类型的边界检查。
_Avoid_: 相信模型、直接使用 JSON

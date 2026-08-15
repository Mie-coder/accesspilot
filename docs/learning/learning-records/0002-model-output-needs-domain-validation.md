# 模型输出必须经过领域校验

> AccessPilot 跨课程学习记录。

学习者已理解 DeepSeek 返回的是普通 JSON 字符串，不是可信业务对象；只有通过 `ParsedReply.model_validate_json()` 检查字段和类型后，结果才能进入 AccessPilot。未知字段（例如 `approved`）必须让整份输出失败，而不是被静默接受。

## Evidence

学习者能够判断 JSON 字符串需要转换为 `ParsedReply`，并正确指出包含未知字段 `approved` 时校验应失败。

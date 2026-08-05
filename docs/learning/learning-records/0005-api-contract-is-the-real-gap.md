# 真正断点是外部 API 契约

> AccessPilot 跨课程学习记录。

学习者能正确追踪函数、列表、条件以及嵌套字典取值，并能得到 `content` 的内容；困惑点是为什么 DeepSeek 返回 `choices → message → content` 这种结构。后续应明确区分“Python 语法”和“第三方 API 规定的数据契约”，先展示官方响应来源再解释提取代码。

## Evidence

学习者正确读出嵌套响应中的 `{"duration_days": 14}`，同时明确提出“不知道 response 为什么会这么返回”。

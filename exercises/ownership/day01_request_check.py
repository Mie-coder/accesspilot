"""AccessPilot 课堂练习 · 第 1 天：检查申请缺失的信息。

本次目标：用 if 判断 justification 是否缺失。
当前已理解：这份申请缺少 duration_days 和 justification。

项目对应：apps/api/src/accesspilot/domain/models.py
正式代码中的 RequestDraft.missing_fields() 负责列出缺失字段。
本文件先练习其中的判断；随后逐步学习列表、函数和返回值，
再对照正式实现，追踪项目如何据此追问缺失信息。

这是独立的 Python 小练习，不需要启动服务、数据库或调用模型。
"""

# 示例数据：身份已由服务端确定；None 表示对应信息尚未提供。
draft = {
    "employee_id": "EMP-001",
    "entitlement_id": "insighthub.customer_export",
    "duration_days": None,
    "justification": None,
}

# 已讲过的例子：检查申请期限。
if draft["duration_days"] is None:
    print("缺少申请期限")


# ── 轮到你：检查申请理由 ──
# 参照上面的例子，写两行代码：
# 当 justification 为 None 时，打印“缺少申请理由”。
# 注意：冒号后换行，下一行缩进 4 个空格。
# 在下面作答，写好后按 Command + S 保存：



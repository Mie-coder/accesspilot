"""权限申请领域数据模型。"""

from datetime import date

from pydantic import BaseModel


class RequestDraft(BaseModel):
    # 系统名称
    system_name: str | None = None  
    # 权限名称
    entitlement_name: str | None = None
    # 项目编码
    project_code: str | None = None
    # 数据范围
    data_scope: str | None = None
    # 申请理由
    business_reason: str | None = None
    # 开始日期
    start_date: date | None = None
    # 持续天数
    duration_days: int | None = None

    def missing_fields(self) -> list[str]:
        required_fields = (
            "system_name",
            "entitlement_name",
            "project_code",
            "data_scope",
            "business_reason",
            "start_date",
            "duration_days",
        )

        return [
            field_name
            for field_name in required_fields
            if getattr(self, field_name) is None
        ]
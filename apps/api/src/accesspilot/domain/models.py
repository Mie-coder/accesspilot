"""权限申请领域数据模型。"""

from pydantic import BaseModel, ConfigDict, StrictInt, field_validator
from pydantic_core import PydanticCustomError


class RequestDraft(BaseModel):
    """多轮对话中可逐步补全、尚未产生审批或授权事实的申请草稿。"""

    model_config = ConfigDict(extra="forbid")

    employee_id: str | None = None
    entitlement_id: str | None = None
    duration_days: StrictInt | None = None
    justification: str | None = None
    confirmed: bool = False

    @field_validator("duration_days")
    @classmethod
    def require_positive_duration(cls, value: int | None) -> int | None:
        """申请期限填了就必须有效；没填则交给 missing_fields 处理。"""

        if value is not None and value <= 0:
            raise PydanticCustomError(
                "duration_not_positive",
                "申请期限必须是正整数",
            )
        return value

    @field_validator("employee_id", "entitlement_id", "justification")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        """去掉用户输入两端空白；纯空白内容仍按业务缺失处理。"""

        if value is None:
            return None
        normalized_value = value.strip()
        return normalized_value or None

    def missing_fields(self) -> list[str]:
        """按固定顺序返回尚未收集的业务字段。"""

        required_fields = (
            "employee_id",
            "entitlement_id",
            "duration_days",
            "justification",
        )

        return [
            field_name
            for field_name in required_fields
            if getattr(self, field_name) is None
        ]

    def can_enter_approval(self) -> bool:
        """只有业务字段完整且用户明确确认后，草稿才具备送审资格。"""

        # 这里只表达送审守卫，不代表审批通过，更不代表权限已开通。
        return not self.missing_fields() and self.confirmed

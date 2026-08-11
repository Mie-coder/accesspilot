"""权限申请领域数据模型。"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator
from pydantic_core import PydanticCustomError


class RequestDraft(BaseModel):
    """多轮对话中可逐步补全、尚未产生审批或授权事实的申请草稿。"""

    model_config = ConfigDict(extra="forbid")

    employee_id: str | None = None
    entitlement_id: str | None = None
    duration_days: StrictInt | None = None
    justification: str | None = None
    # 确认必须是 JSON 布尔值；"yes"、1 等宽松值不能越过提交守卫。
    confirmed: StrictBool = False

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


class ParsedReply(BaseModel):
    """模型从单句用户回复中提取出的草稿增量。"""

    model_config = ConfigDict(extra="forbid")

    employee_id: str | None = None
    entitlement_id: str | None = None
    duration_days: StrictInt | None = None
    justification: str | None = None
    confirmed: StrictBool | None = None


CursorExpectedField = Literal[
    "entitlement_id",
    "duration_days",
    "justification",
    "confirmation",
    "none",
]


class ConversationCursor(BaseModel):
    """服务端保存的、绑定当前草稿 revision 的待补字段合同。

    T18 仍使用 Workspace + actor 作用域，``auth_session_id`` 为 T19
    预留。``consumed_at`` 只用于审计；只要它不为空，游标就不再可消费。
    """

    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID | str
    actor_id: str
    auth_session_id: str | None = None
    draft_revision: int = Field(ge=0)
    expected_field: CursorExpectedField
    last_question_kind: str = Field(min_length=1, max_length=80)
    issued_at: datetime
    consumed_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        """被消费或显式失效的 Cursor 不能再次解释用户输入。"""

        return self.consumed_at is None

    def consume(self, *, at: datetime | None = None) -> "ConversationCursor":
        """返回带消费时间的新快照，不就地修改持久化对象。"""

        return self.model_copy(
            update={
                "consumed_at": at or datetime.now(UTC),
            }
        )

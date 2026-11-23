from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import update

from .knowledge.ingestion import upsert_case_source
from .models import ExperimentCase
from .observability.audit import record_audit

DetailName = Literal["title", "goal", "description"]
DetailValue = Annotated[str, StringConstraints(max_length=20000)]


class ProjectDetails(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: dict[DetailName, DetailValue] = Field(default_factory=dict)
    expected: dict[DetailName, str | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check(self):
        if self.expected.keys() != self.changes.keys():
            raise ValueError("只提交修改的字段，并附上这些字段上次读到的原值。")
        if "title" in self.changes:
            self.changes["title"] = self.changes["title"].strip()
            if not 1 <= len(self.changes["title"]) <= 255:
                raise ValueError("工程名称须为 1 至 255 个字符。")
        return self


class DetailsConflict(ValueError):
    pass


def project_details(db, case, request):
    if request.changes:
        command = update(ExperimentCase).where(ExperimentCase.id == case.id)
        for field, previous in request.expected.items():
            command = command.where(getattr(ExperimentCase, field) == previous)
        if db.execute(command.values(**request.changes)).rowcount != 1:
            raise DetailsConflict("工程资料已变更。请重新读取并核对后再保存；本次修改仍可继续编辑。")
        db.refresh(case)
        upsert_case_source(db, case)
        record_audit(db, action="case.details.update", resource_type="case", resource_id=str(case.id))
    return {key: getattr(case, key) for key in ("title", "goal", "description")}

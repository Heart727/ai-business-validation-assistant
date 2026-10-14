from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.input_guard import require_demo_text


_OBSERVATION_LABELS = frozenset({"证据", "推断", "缺失信息"})


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    industry: str
    offering: str
    target_customer: str
    business_problem: str
    current_metrics: str
    budget_range: str
    goal: str
    time_limit: str
    # Historical records did not contain this field; do not invent a threshold.
    success_criteria: str | None = None


class DayAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: int = Field(ge=1, le=7)
    action: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    decision_rule: str = Field(min_length=1)


class AnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    executive_summary: str = Field(min_length=1, max_length=1200)
    observations: list[str] = Field(min_length=1, max_length=8)
    risks: list[str] = Field(max_length=8)
    seven_day_actions: list[DayAction] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def require_seven_distinct_days(self):
        if sorted(item.day for item in self.seven_day_actions) != list(range(1, 8)):
            raise ValueError("seven_day_actions must contain days 1 through 7 once")
        return self

    @model_validator(mode="after")
    def require_separated_observations(self):
        present = set()
        for observation in self.observations:
            text = observation.lstrip()
            for label in _OBSERVATION_LABELS:
                prefix = next(
                    (candidate for candidate in (f"{label}：", f"{label}:") if text.startswith(candidate)),
                    None,
                )
                if prefix and text[len(prefix):].strip():
                    present.add(label)
        if present != _OBSERVATION_LABELS:
            raise ValueError("observations must separate evidence, inference, and missing information")
        return self


class PipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    status: Literal["processing", "completed", "retryable_failed"]
    analysis: AnalysisOutput | None = None
    error_code: str | None = None
    access_token: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def validate_state_payload(self):
        if self.status == "completed" and (self.analysis is None or self.error_code is not None):
            raise ValueError("completed results require analysis and cannot contain an error")
        if self.status == "retryable_failed" and (
            self.analysis is not None or not self.error_code
        ):
            raise ValueError("retryable failures require an error code and cannot contain analysis")
        if self.status == "processing" and (self.analysis is not None or self.error_code is not None):
            raise ValueError("processing results cannot contain analysis or an error")
        return self


class LegacyScenarioDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=80)
    industry: str = Field(min_length=1, max_length=60)
    offering: str = Field(min_length=1, max_length=300)
    target_customer: str = Field(min_length=1, max_length=300)
    business_problem: str = Field(min_length=1, max_length=600)
    current_metrics: str = Field(min_length=1, max_length=600)
    budget_range: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=400)
    time_limit: str = Field(min_length=1, max_length=100)
    fictional_only: Literal[True]

    @field_validator("fictional_only", mode="before")
    @classmethod
    def require_explicit_confirmation(cls, value):
        if type(value) is not bool or value is not True:
            raise ValueError("Explicit fictional-only confirmation required")
        return value

    @field_validator("title")
    @classmethod
    def require_fictional_title(cls, value):
        if not value.startswith("虚构"):
            raise ValueError("Title must identify the fictional sample")
        return value

    @field_validator(
        "title", "industry", "offering", "target_customer", "business_problem",
        "current_metrics", "budget_range", "goal", "time_limit",
    )
    @classmethod
    def screen_demo_fields(cls, value):
        return require_demo_text(value)

    def to_scenario(self, scenario_id: str) -> Scenario:
        return Scenario(id=scenario_id, **self.model_dump(exclude={"fictional_only"}))


class ScenarioDraft(LegacyScenarioDraft):
    success_criteria: str = Field(min_length=1, max_length=300)

    @field_validator("success_criteria")
    @classmethod
    def screen_success_criteria(cls, value):
        return require_demo_text(value)


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(min_length=1, max_length=80)
    request_id: UUID
    draft: ScenarioDraft | None = None
    request_token: UUID | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def edited_draft_requires_request_token(self):
        if self.draft is not None and self.request_token is None:
            raise ValueError("Edited drafts require a private recovery nonce")
        return self


class RetryScenarioRequest(ScenarioRequest):
    # Only /api/retry can accept an old draft, and it may not create a new row.
    draft: ScenarioDraft | LegacyScenarioDraft | None = None


class DayExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    day: int = Field(ge=1, le=7, strict=True)
    status: Literal["pending", "completed", "skipped"]
    observed_metric: str = Field(default="", max_length=400)
    conclusion: str = Field(default="", max_length=400)

    @field_validator("observed_metric", "conclusion")
    @classmethod
    def screen_demo_fields(cls, value):
        return require_demo_text(value)

    @model_validator(mode="after")
    def completed_requires_evidence(self):
        if self.status == "completed" and (not self.observed_metric or not self.conclusion):
            raise ValueError("Completed execution requires a metric and conclusion")
        return self


class DayExecution(DayExecutionInput):
    updated_at: str


class RecordAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    access_token: str = Field(min_length=1, max_length=128, repr=False)


class ExecutionRequest(RecordAccess):
    execution: DayExecutionInput


class RecordDetails(BaseModel):
    request_id: str
    scenario: Scenario | None
    analysis: AnalysisOutput
    executions: list[DayExecution]

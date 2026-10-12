"""Deterministic in-memory integrations for tests and local acceptance checks."""

import json

from app.schemas import AnalysisOutput, DayAction, DayExecution, Scenario
from app.services.dify import DifyError
from app.services.feishu import FeishuError, FeishuRecord


class InMemoryFeishu:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.records: dict[str, FeishuRecord] = {}
        self.create_count = 0
        self.failures: dict[str, list[str]] = {}

    def fail_next(self, operation: str, code: str) -> None:
        self.failures.setdefault(operation, []).append(code)

    def _raise_next_failure(self, operation: str) -> None:
        queued = self.failures.get(operation, [])
        if queued:
            raise FeishuError(queued.pop(0))

    async def get_tenant_token(self) -> str:
        self.events.append("token")
        self._raise_next_failure("token")
        return "mock-tenant-token"

    async def find_by_request_id(self, token: str, request_id: str) -> FeishuRecord | None:
        assert token == "mock-tenant-token"
        self.events.append("find")
        self._raise_next_failure("find")
        return self.records.get(request_id)

    async def create_processing_record(
        self, token: str, request_id: str, scenario: Scenario,
        request_token_digest: str | None = None,
    ) -> FeishuRecord:
        assert token == "mock-tenant-token"
        self.events.append("create")
        self._raise_next_failure("create")
        self.create_count += 1
        record = FeishuRecord(
            f"mock-rec-{self.create_count}", request_id, "processing",
            scenario=scenario, request_token_digest=request_token_digest,
        )
        self.records[request_id] = record
        return record

    async def update_record(self, token: str, record_id: str, fields: dict[str, object]) -> None:
        assert token == "mock-tenant-token"
        self.events.append(f"update:{fields.get('处理状态', 'execution')}")
        self._raise_next_failure("update")
        request_id, record = next(
            (key, value) for key, value in self.records.items() if value.record_id == record_id
        )
        analysis = record.analysis
        if fields.get("处理状态") == "completed":
            analysis = AnalysisOutput.model_validate({
                "executive_summary": fields["经营摘要"],
                "observations": fields["关键观察"],
                "risks": fields["风险"],
                "seven_day_actions": fields["7天验证动作"],
            })
        executions = {item.day: item for item in record.executions}
        for day in range(1, 8):
            key = f"第{day}天执行记录"
            if key in fields:
                executions[day] = DayExecution.model_validate_json(fields[key])
        self.records[request_id] = FeishuRecord(
            record_id, request_id, fields.get("处理状态", record.status), analysis,
            record.scenario, [executions[day] for day in sorted(executions)],
            record.request_token_digest,
        )


class DeterministicDify:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.events: list[str] = []
        self.error: DifyError | None = None

    async def run_workflow(self, scenario: Scenario, request_id: str) -> AnalysisOutput:
        self.events.append("dify")
        self.calls.append((scenario.id, request_id))
        if self.error is not None:
            raise self.error

        metrics = scenario.current_metrics or "未提供量化基线"
        return AnalysisOutput.model_validate({
            "executive_summary": f"{scenario.title}：围绕既定目标验证一项小范围经营假设。",
            "observations": [
                f"证据：虚构样例记录为：{metrics}。",
                f"推断：可以通过一周的小范围行动检验“{scenario.goal}”。",
                "缺失信息：真实客户反馈与历史对照数据尚未提供。",
            ],
            "risks": ["该结果由本地模拟服务生成，不代表真实 AI 输出或经营结论。"],
            "seven_day_actions": [
                DayAction(
                    day=day,
                    action=f"第 {day} 天按固定样例目标执行一项低成本验证动作。",
                    metric="记录执行次数、有效反馈数和异常情况。",
                    decision_rule="若样本或记录不足，先补齐基线再决定继续、调整或停止。",
                )
                for day in range(1, 8)
            ],
        })

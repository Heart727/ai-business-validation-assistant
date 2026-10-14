import asyncio

import pytest

from app.schemas import AnalysisOutput, PipelineResult
from app.services.dify import DifyError
from app.services.feishu import FeishuError, FeishuRecord
from app.services.pipeline import PipelineError, PipelineService
from app.services.scenarios import ScenarioCatalog, UnknownScenario
from tests.test_schemas import valid_output


class RecordingFeishu:
    def __init__(self, events):
        self.events = events
        self.records = {}
        self.create_count = 0
        self.update_record_ids = []
        self.fail_on = None

    async def get_tenant_token(self):
        self.events.append("token")
        if self.fail_on == "token":
            raise FeishuError("feishu_auth_failed")
        return "tenant-token"

    async def find_by_request_id(self, token, request_id):
        assert token == "tenant-token"
        self.events.append("find")
        if self.fail_on == "find":
            raise FeishuError("feishu_lookup_failed")
        return self.records.get(request_id)

    async def create_processing_record(self, token, request_id, scenario):
        assert token == "tenant-token"
        self.events.append("create")
        if self.fail_on == "create":
            raise FeishuError("feishu_create_failed")
        self.create_count += 1
        record = FeishuRecord(f"rec-{self.create_count}", request_id, "processing")
        self.records[request_id] = record
        return record

    async def update_record(self, token, record_id, fields):
        assert token == "tenant-token"
        self.events.append(f"update:{fields['处理状态']}")
        self.update_record_ids.append(record_id)
        if self.fail_on == "update":
            raise FeishuError("feishu_update_failed")
        request_id, record = next(
            (key, value) for key, value in self.records.items() if value.record_id == record_id
        )
        analysis = record.analysis
        if fields["处理状态"] == "completed":
            analysis = AnalysisOutput.model_validate({
                "executive_summary": fields["经营摘要"],
                "observations": fields["关键观察"],
                "risks": fields["风险"],
                "seven_day_actions": fields["7天验证动作"],
            })
        self.records[request_id] = FeishuRecord(
            record_id, request_id, fields["处理状态"], analysis
        )


class RecordingDify:
    def __init__(self, events):
        self.events = events
        self.calls = []
        self.error = None
        self.delay = 0
        self.analysis = AnalysisOutput.model_validate(valid_output())

    async def run_workflow(self, scenario, request_id):
        self.events.append("dify")
        self.calls.append((scenario.id, request_id))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.analysis


@pytest.fixture
def pipeline_parts(scenario):
    events = []
    feishu = RecordingFeishu(events)
    dify = RecordingDify(events)
    catalog = ScenarioCatalog([scenario])
    pipeline = PipelineService(feishu=feishu, dify=dify, scenarios=catalog)
    return pipeline, feishu, dify, events, scenario.id


@pytest.mark.asyncio
async def test_pipeline_creates_before_analysis_and_updates_same_record(pipeline_parts):
    pipeline, feishu, dify, events, scenario_id = pipeline_parts

    result = await pipeline.submit(scenario_id, "request-1")

    assert events == ["token", "find", "create", "find", "dify", "update:completed"]
    assert result == PipelineResult(
        request_id="request-1", status="completed", analysis=dify.analysis
    )
    assert feishu.create_count == 1
    assert feishu.update_record_ids == ["rec-1"]
    assert feishu.records["request-1"].analysis == dify.analysis


@pytest.mark.asyncio
async def test_dify_failure_marks_same_row_retryable_then_retry_reuses_it(pipeline_parts):
    pipeline, feishu, dify, events, scenario_id = pipeline_parts
    dify.error = DifyError("dify_timeout")

    failed = await pipeline.submit(scenario_id, "request-1")

    assert failed.status == "retryable_failed"
    assert failed.error_code == "dify_timeout"
    assert feishu.records["request-1"].status == "retryable_failed"
    assert feishu.create_count == 1
    assert feishu.update_record_ids == ["rec-1"]

    dify.error = None
    events.clear()
    retried = await pipeline.submit(scenario_id, "request-1")

    assert events == ["token", "find", "dify", "update:completed"]
    assert retried.status == "completed"
    assert feishu.create_count == 1
    assert feishu.update_record_ids == ["rec-1", "rec-1"]
    assert len(dify.calls) == 2


@pytest.mark.asyncio
async def test_completed_request_returns_saved_analysis_without_calling_dify(pipeline_parts):
    pipeline, feishu, dify, events, scenario_id = pipeline_parts
    first = await pipeline.submit(scenario_id, "request-1")
    dify.calls.clear()
    events.clear()

    repeated = await pipeline.submit(scenario_id, "request-1")

    assert repeated == first
    assert events == ["token", "find"]
    assert dify.calls == []
    assert feishu.create_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,code,expected_events",
    [
        ("token", "feishu_auth_failed", ["token"]),
        ("find", "feishu_lookup_failed", ["token", "find"]),
        ("create", "feishu_create_failed", ["token", "find", "create"]),
    ],
)
async def test_feishu_read_or_create_failure_never_calls_dify(
    pipeline_parts, failure, code, expected_events
):
    pipeline, feishu, dify, events, scenario_id = pipeline_parts
    feishu.fail_on = failure

    with pytest.raises(PipelineError) as caught:
        await pipeline.submit(scenario_id, "request-1")

    assert caught.value.code == code
    assert str(caught.value) == code
    assert events == expected_events
    assert dify.calls == []


@pytest.mark.asyncio
async def test_unknown_scenario_is_rejected_before_any_external_call(pipeline_parts):
    pipeline, _, _, events, _ = pipeline_parts

    with pytest.raises(UnknownScenario):
        await pipeline.submit("not-in-catalog", "request-1")

    assert events == []


@pytest.mark.asyncio
async def test_total_pipeline_timeout_marks_known_record_retryable(
    pipeline_parts, monkeypatch
):
    pipeline, feishu, dify, events, scenario_id = pipeline_parts
    monkeypatch.setattr("app.services.pipeline.PIPELINE_TIMEOUT_SECONDS", 0.06)
    monkeypatch.setattr("app.services.pipeline.PIPELINE_CLEANUP_SECONDS", 0.02)
    dify.delay = 0.1

    result = await pipeline.submit(scenario_id, "request-1")

    assert result.status == "retryable_failed"
    assert result.error_code == "pipeline_timeout"
    assert events == ["token", "find", "create", "find", "dify", "find", "update:retryable_failed", "find"]
    assert feishu.records["request-1"].status == "retryable_failed"
    assert feishu.create_count == 1


@pytest.mark.asyncio
async def test_feishu_update_failure_is_exposed_as_safe_service_code(pipeline_parts):
    pipeline, feishu, _, _, scenario_id = pipeline_parts
    feishu.fail_on = "update"

    with pytest.raises(PipelineError) as caught:
        await pipeline.submit(scenario_id, "request-1")

    assert caught.value.code == "feishu_update_failed"
    assert "tenant-token" not in str(caught.value)
    assert feishu.create_count == 1


def test_pipeline_result_rejects_completed_state_without_analysis():
    with pytest.raises(ValueError):
        PipelineResult(request_id="request-1", status="completed")


def test_pipeline_result_rejects_retry_state_without_error_code():
    with pytest.raises(ValueError):
        PipelineResult(request_id="request-1", status="retryable_failed")

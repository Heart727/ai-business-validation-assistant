"""Coordinates idempotent analysis requests across Feishu and Dify."""

import asyncio
import hashlib
import hmac

from app.schemas import AnalysisOutput, PipelineResult, ScenarioDraft
from app.services.dify import DifyClient, DifyError
from app.services.feishu import FeishuClient, FeishuError, FeishuRecord
from app.services.scenarios import ScenarioCatalog


PIPELINE_TIMEOUT_SECONDS = 50.0
PIPELINE_CLEANUP_SECONDS = 5.0


class PipelineError(Exception):
    """An application-safe service failure code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PipelineService:
    def __init__(self, feishu: FeishuClient, dify: DifyClient, scenarios: ScenarioCatalog) -> None:
        self.feishu = feishu
        self.dify = dify
        self.scenarios = scenarios

    async def submit(
        self, scenario_id: str, request_id: str, draft: ScenarioDraft | None = None,
        request_token: str | None = None,
    ) -> PipelineResult:
        scenario = self.scenarios.get(scenario_id)
        if draft is not None:
            scenario = draft.to_scenario(scenario_id)
        token_digest = hashlib.sha256(request_token.encode()).hexdigest() if request_token else None
        loop = asyncio.get_running_loop()
        total_seconds = max(PIPELINE_TIMEOUT_SECONDS, 0.001)
        cleanup_seconds = min(PIPELINE_CLEANUP_SECONDS, total_seconds / 2)
        deadline = loop.time() + total_seconds
        work_deadline = deadline - cleanup_seconds
        token: str | None = None
        record: FeishuRecord | None = None

        try:
            async with asyncio.timeout_at(work_deadline):
                token = await self.feishu.get_tenant_token()
                record = await self.feishu.find_by_request_id(token, request_id)
                if record is None:
                    if token_digest is None:
                        record = await self.feishu.create_processing_record(token, request_id, scenario)
                    else:
                        record = await self.feishu.create_processing_record(
                            token, request_id, scenario, token_digest,
                        )
                elif record.request_token_digest is not None and (
                    token_digest is None or not hmac.compare_digest(record.request_token_digest, token_digest)
                ):
                    raise PipelineError("record_access_denied")
                elif record.request_token_digest is None and token_digest is not None:
                    # Public historical request IDs cannot be upgraded into write access.
                    raise PipelineError("record_access_denied")
                if record.scenario is not None:
                    exclude = {"id"} if record.scenario.id == "legacy" else set()
                    if record.scenario.model_dump(exclude=exclude) != scenario.model_dump(exclude=exclude):
                        raise PipelineError("request_content_conflict")
                elif draft is not None:
                    # A legacy record with no verifiable snapshot must not accept changed input.
                    raise PipelineError("request_content_conflict")

                if record.status == "completed":
                    if record.analysis is None:
                        raise PipelineError("feishu_invalid_response")
                    return PipelineResult(
                        request_id=request_id,
                        status="completed",
                        analysis=record.analysis,
                    )

                try:
                    analysis = await self.dify.run_workflow(scenario, request_id)
                except DifyError as error:
                    await self._update_retryable(token, record, error.code)
                    return PipelineResult(
                        request_id=request_id,
                        status="retryable_failed",
                        error_code=error.code,
                    )

                await self.feishu.update_record(
                    token,
                    record.record_id,
                    self._completed_fields(analysis),
                )
                return PipelineResult(
                    request_id=request_id,
                    status="completed",
                    analysis=analysis,
                )
        except TimeoutError:
            if token is None or record is None:
                raise PipelineError("pipeline_timeout") from None
            try:
                async with asyncio.timeout_at(deadline):
                    await self._update_retryable(token, record, "pipeline_timeout")
            except TimeoutError:
                raise PipelineError("pipeline_timeout") from None
            except FeishuError as error:
                raise PipelineError(error.code) from None
            return PipelineResult(
                request_id=request_id,
                status="retryable_failed",
                error_code="pipeline_timeout",
            )
        except FeishuError as error:
            raise PipelineError(error.code) from None

    async def _update_retryable(self, token: str, record: FeishuRecord, error_code: str) -> None:
        await self.feishu.update_record(
            token,
            record.record_id,
            {"处理状态": "retryable_failed", "错误码": error_code},
        )

    @staticmethod
    def _completed_fields(analysis: AnalysisOutput) -> dict[str, object]:
        return {
            "处理状态": "completed",
            "经营摘要": analysis.executive_summary,
            "关键观察": analysis.observations,
            "风险": analysis.risks,
            "7天验证动作": [action.model_dump(mode="json") for action in analysis.seven_day_actions],
            "错误码": "",
        }

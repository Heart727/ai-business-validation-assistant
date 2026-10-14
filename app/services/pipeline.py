"""Coordinates idempotent analysis requests across Feishu and Dify."""

import asyncio
import hashlib
import hmac

from app.schemas import AnalysisOutput, PipelineResult, ScenarioDraft, LegacyScenarioDraft
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
        self._inflight: dict[str, tuple[asyncio.Lock, int]] = {}

    async def submit(
        self, scenario_id: str, request_id: str, draft: LegacyScenarioDraft | None = None,
        request_token: str | None = None,
        *, allow_create: bool = True,
    ) -> PipelineResult:
        # This lock only coordinates this process. Feishu client_token protects
        # record creation when distinct Vercel instances race. It is not a model
        # execution lock across processes or an exactly-once billing guarantee.
        deadline = asyncio.get_running_loop().time() + max(PIPELINE_TIMEOUT_SECONDS, 0.001)
        lock, users = self._inflight.get(request_id, (asyncio.Lock(), 0))
        self._inflight[request_id] = (lock, users + 1)
        try:
            async with asyncio.timeout_at(deadline):
                async with lock:
                    return await self._submit_unlocked(scenario_id, request_id, draft, request_token,
                                                       allow_create=allow_create, deadline=deadline)
        except TimeoutError:
            raise PipelineError("pipeline_timeout") from None
        finally:
            _, current_users = self._inflight[request_id]
            if current_users == 1:
                del self._inflight[request_id]
            else:
                self._inflight[request_id] = (lock, current_users - 1)

    async def _submit_unlocked(
        self, scenario_id: str, request_id: str, draft: LegacyScenarioDraft | None,
        request_token: str | None, *, allow_create: bool, deadline: float,
    ) -> PipelineResult:
        scenario = self.scenarios.get(scenario_id)
        if draft is not None:
            scenario = draft.to_scenario(scenario_id)
        token_digest = hashlib.sha256(request_token.encode()).hexdigest() if request_token else None
        loop = asyncio.get_running_loop()
        total_seconds = max(PIPELINE_TIMEOUT_SECONDS, 0.001)
        cleanup_seconds = min(PIPELINE_CLEANUP_SECONDS, total_seconds / 2)
        work_deadline = deadline - cleanup_seconds
        token: str | None = None
        record: FeishuRecord | None = None

        try:
            async with asyncio.timeout_at(work_deadline):
                token = await self.feishu.get_tenant_token()
                record = await self.feishu.find_by_request_id(token, request_id)
                if record is None:
                    if not allow_create:
                        raise PipelineError("record_not_found")
                    if token_digest is None:
                        record = await self.feishu.create_processing_record(token, request_id, scenario)
                    else:
                        record = await self.feishu.create_processing_record(
                            token, request_id, scenario, token_digest,
                        )
                    # Idempotency can replay the original processing response
                    # after a different instance has already saved its report.
                    record = await self.feishu.find_by_request_id(token, request_id)
                    if record is None:
                        raise PipelineError("feishu_lookup_failed")
                if record.request_token_digest is not None and (
                    token_digest is None or not hmac.compare_digest(record.request_token_digest, token_digest)
                ):
                    raise PipelineError("record_access_denied")
                elif record.request_token_digest is None and token_digest is not None:
                    # Public historical request IDs cannot be upgraded into write access.
                    raise PipelineError("record_access_denied")
                if record.scenario is not None:
                    exclude = {"id"} if record.scenario.id == "legacy" else set()
                    if draft is None and record.scenario.success_criteria is None:
                        exclude.add("success_criteria")
                    if record.scenario.model_dump(exclude=exclude) != scenario.model_dump(exclude=exclude):
                        raise PipelineError("request_content_conflict")
                    scenario = record.scenario
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
                    completed = await self._update_retryable(token, record, error.code)
                    if completed is not None:
                        return PipelineResult(request_id=request_id, status="completed", analysis=completed)
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
                    completed = await self._update_retryable(token, record, "pipeline_timeout")
                    if completed is not None:
                        return PipelineResult(request_id=request_id, status="completed", analysis=completed)
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

    async def _update_retryable(self, token: str, record: FeishuRecord, error_code: str) -> AnalysisOutput | None:
        current = await self.feishu.find_by_request_id(token, record.request_id)
        if current is not None and current.analysis is not None:
            return current.analysis
        await self.feishu.update_record(
            token,
            record.record_id,
            {"处理状态": "retryable_failed", "错误码": error_code},
        )
        current = await self.feishu.find_by_request_id(token, record.request_id)
        if current is not None and current.analysis is not None:
            # Handles completion between the preceding read and failure write.
            # Report fields and per-day execution fields remain independent.
            try:
                await self.feishu.update_record(token, current.record_id, self._completed_fields(current.analysis))
            except FeishuError:
                # The complete report was already read from durable storage;
                # failed status housekeeping must not hide it from its owner.
                pass
            return current.analysis
        return None

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

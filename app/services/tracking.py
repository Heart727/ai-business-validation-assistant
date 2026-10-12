"""Record-scoped access and seven independent execution updates."""

import hashlib
import hmac
from datetime import datetime, timezone

from app.config import Settings
from app.schemas import DayExecution, DayExecutionInput, RecordDetails
from app.services.feishu import FeishuClient, FeishuError, FeishuRecord
from app.services.pipeline import PipelineError


class TrackingService:
    def __init__(self, settings: Settings, feishu: FeishuClient | None):
        self.settings = settings
        self.feishu = feishu

    def issue_token(self, request_id: str) -> str:
        secret = self.settings.feishu_app_secret
        if not secret:
            raise PipelineError("service_unavailable")
        return hmac.new(
            secret.encode(), f"business-validation-record-v1:{request_id}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def authorize(self, request_id: str, access_token: str) -> None:
        if not hmac.compare_digest(self.issue_token(request_id).encode(), access_token.encode()):
            raise PipelineError("record_access_denied")

    async def _load(self, request_id: str, access_token: str) -> tuple[str, FeishuRecord]:
        self.authorize(request_id, access_token)
        if self.feishu is None:
            raise PipelineError("service_unavailable")
        try:
            token = await self.feishu.get_tenant_token()
            record = await self.feishu.find_by_request_id(token, request_id)
        except FeishuError as error:
            raise PipelineError(error.code) from None
        if record is None:
            raise PipelineError("record_not_found")
        if record.status != "completed" or record.analysis is None:
            raise PipelineError("record_not_completed")
        return token, record

    @staticmethod
    def _details(record: FeishuRecord) -> RecordDetails:
        return RecordDetails(
            request_id=record.request_id, scenario=record.scenario,
            analysis=record.analysis, executions=record.executions,
        )

    async def get_record(self, request_id: str, access_token: str) -> RecordDetails:
        _, record = await self._load(request_id, access_token)
        return self._details(record)

    async def save_execution(
        self, request_id: str, access_token: str, execution: DayExecutionInput,
    ) -> RecordDetails:
        token, record = await self._load(request_id, access_token)
        saved = DayExecution(
            **execution.model_dump(),
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        try:
            await self.feishu.update_record(
                token, record.record_id,
                {f"第{execution.day}天执行记录": saved.model_dump_json()},
            )
            updated = await self.feishu.find_by_request_id(token, request_id)
        except FeishuError as error:
            raise PipelineError(error.code) from None
        if updated is None or updated.analysis is None:
            raise PipelineError("feishu_invalid_response")
        return self._details(updated)

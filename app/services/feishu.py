"""Small, fail-closed gateway for Feishu Bitable records."""

import json
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.schemas import AnalysisOutput, DayExecution, Scenario


_RUNTIME_STATUSES = frozenset({"processing", "completed", "retryable_failed"})
_UPDATE_LIST_FIELDS = frozenset({"关键观察", "风险", "7天验证动作"})
_EXECUTION_FIELDS = {day: f"第{day}天执行记录" for day in range(1, 8)}
_UPDATE_TEXT_FIELDS = frozenset({"经营摘要", "错误码", *_EXECUTION_FIELDS.values()})
_UPDATE_FIELDS = frozenset({"处理状态", *_UPDATE_LIST_FIELDS, *_UPDATE_TEXT_FIELDS})


class FeishuError(Exception):
    """An application-safe Feishu failure code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FeishuRecord:
    record_id: str
    request_id: str
    status: str
    analysis: AnalysisOutput | None = None
    scenario: Scenario | None = None
    executions: list[DayExecution] = field(default_factory=list)
    request_token_digest: str | None = None


def _invalid() -> FeishuError:
    return FeishuError("feishu_invalid_response")


def _require_success(payload: object, error_code: str) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise _invalid()
    code = payload.get("code")
    if type(code) is not int:
        raise _invalid()
    if code != 0:
        raise FeishuError(error_code)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _invalid()
    return data


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid()
    return value


def _record(item: object, request_id: str) -> FeishuRecord:
    if not isinstance(item, dict):
        raise _invalid()
    record_id = _text(item.get("record_id"))
    fields = item.get("fields")
    if not record_id or not isinstance(fields, dict):
        raise _invalid()
    if fields.get("请求ID") != request_id:
        raise _invalid()
    status = _text(fields.get("处理状态"))
    if status not in _RUNTIME_STATUSES:
        raise _invalid()
    analysis = None
    if status == "completed":
        try:
            analysis = AnalysisOutput.model_validate({
                "executive_summary": _text(fields.get("经营摘要")),
                "observations": json.loads(_text(fields.get("关键观察"))),
                "risks": json.loads(_text(fields.get("风险"))),
                "seven_day_actions": json.loads(_text(fields.get("7天验证动作"))),
            })
        except (ValueError, ValidationError):
            raise _invalid() from None
    scenario = None
    token_digest = None
    snapshot = fields.get("资料快照")
    if snapshot:
        try:
            snapshot_data = json.loads(_text(snapshot))
            if isinstance(snapshot_data, dict) and "scenario" in snapshot_data:
                scenario = Scenario.model_validate(snapshot_data["scenario"])
                token_digest = _text(snapshot_data.get("request_token_digest"))
                if len(token_digest) != 64 or any(c not in "0123456789abcdef" for c in token_digest):
                    raise _invalid()
            else:
                scenario = Scenario.model_validate(snapshot_data)
        except (ValueError, ValidationError):
            raise _invalid() from None
    elif all(isinstance(fields.get(name), str) for name in (
        "场景名称", "行业", "产品服务", "目标客群", "经营问题",
        "现有指标", "预算范围", "目标", "时间限制",
    )):
        scenario = Scenario(
            id="legacy", title=fields["场景名称"], industry=fields["行业"],
            offering=fields["产品服务"], target_customer=fields["目标客群"],
            business_problem=fields["经营问题"], current_metrics=fields["现有指标"],
            budget_range=fields["预算范围"], goal=fields["目标"], time_limit=fields["时间限制"],
        )
    executions = []
    for day, name in _EXECUTION_FIELDS.items():
        if fields.get(name):
            try:
                execution = DayExecution.model_validate_json(_text(fields[name]))
            except (ValueError, ValidationError):
                raise _invalid() from None
            if execution.day != day:
                raise _invalid()
            executions.append(execution)
    return FeishuRecord(record_id, request_id, status, analysis, scenario, executions, token_digest)


class FeishuClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.http_client = http_client

    async def _request(self, method: str, path: str, error_code: str, **kwargs: object) -> object:
        try:
            response = await self.http_client.request(
                method, f"{self.settings.feishu_base_url.rstrip('/')}{path}",
                timeout=5.0, follow_redirects=False, **kwargs,
            )
        except httpx.RequestError:
            raise FeishuError(error_code) from None
        if not 200 <= response.status_code < 300:
            raise FeishuError(error_code)
        try:
            return response.json()
        except ValueError:
            raise _invalid() from None

    def _records_path(self, error_code: str) -> str:
        app_token = self.settings.feishu_app_token
        table_id = self.settings.feishu_table_id
        if not app_token or not table_id:
            raise FeishuError(error_code)
        return (f"/open-apis/bitable/v1/apps/{quote(app_token, safe='')}"
                f"/tables/{quote(table_id, safe='')}/records")

    @staticmethod
    def _headers(token: str, error_code: str) -> dict[str, str]:
        if not token:
            raise FeishuError(error_code)
        return {"Authorization": f"Bearer {token}"}

    async def get_tenant_token(self) -> str:
        app_id, app_secret = self.settings.feishu_app_id, self.settings.feishu_app_secret
        if not app_id or not app_secret:
            raise FeishuError("feishu_auth_failed")
        payload = await self._request(
            "POST", "/open-apis/auth/v3/tenant_access_token/internal", "feishu_auth_failed",
            json={"app_id": app_id, "app_secret": app_secret},
        )
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise _invalid()
        if payload["code"] != 0:
            raise FeishuError("feishu_auth_failed")
        token = payload.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise _invalid()
        return token

    async def create_processing_record(
        self, token: str, request_id: str, scenario: Scenario,
        request_token_digest: str | None = None,
    ) -> FeishuRecord:
        error_code = "feishu_create_failed"
        path = self._records_path(error_code)
        fields = {
            "请求ID": request_id, "演示标记": "DEMO", "场景名称": scenario.title,
            "行业": scenario.industry, "产品服务": scenario.offering,
            "目标客群": scenario.target_customer, "经营问题": scenario.business_problem,
            "现有指标": scenario.current_metrics, "预算范围": scenario.budget_range,
            "目标": scenario.goal, "时间限制": scenario.time_limit,
            "处理状态": "processing", "经营摘要": "", "关键观察": "[]",
            "风险": "[]", "7天验证动作": "[]", "错误码": "",
            "资料快照": scenario.model_dump_json(),
        }
        if request_token_digest is not None:
            fields["资料快照"] = json.dumps({
                "scenario": scenario.model_dump(), "request_token_digest": request_token_digest,
            }, ensure_ascii=False)
        data = _require_success(await self._request(
            "POST", path, error_code, headers=self._headers(token, error_code), json={"fields": fields},
        ), error_code)
        record = _record(data.get("record"), request_id)
        if record.status != "processing":
            raise _invalid()
        return record

    async def find_by_request_id(self, token: str, request_id: str) -> FeishuRecord | None:
        error_code = "feishu_lookup_failed"
        path = self._records_path(error_code)
        filter_value = f"CurrentValue.[请求ID] = {json.dumps(request_id, ensure_ascii=False)}"
        data = _require_success(await self._request(
            "GET", path, error_code, headers=self._headers(token, error_code),
            params={"filter": filter_value, "page_size": 2},
        ), error_code)
        items, has_more = data.get("items"), data.get("has_more")
        if "items" not in data and type(data.get("total")) is int and data["total"] == 0 and has_more is False:
            items = []
        if not isinstance(items, list) or type(has_more) is not bool:
            raise _invalid()
        if len(items) > 1:
            raise FeishuError("feishu_duplicate_request_id")
        if has_more:
            raise _invalid()
        return _record(items[0], request_id) if items else None

    async def update_record(self, token: str, record_id: str, fields: dict[str, object]) -> None:
        error_code = "feishu_update_failed"
        path = self._records_path(error_code)
        if not record_id or not isinstance(fields, dict) or not fields or set(fields) - _UPDATE_FIELDS:
            raise FeishuError(error_code)
        status = fields.get("处理状态")
        if "处理状态" in fields and (not isinstance(status, str) or status not in _RUNTIME_STATUSES):
            raise FeishuError(error_code)
        if any(not isinstance(fields[key], list) for key in _UPDATE_LIST_FIELDS.intersection(fields)):
            raise FeishuError(error_code)
        if any(not isinstance(fields[key], str) for key in _UPDATE_TEXT_FIELDS.intersection(fields)):
            raise FeishuError(error_code)
        serialized = {
            key: json.dumps(value, ensure_ascii=False) if key in _UPDATE_LIST_FIELDS else value
            for key, value in fields.items()
        }
        serialized["演示标记"] = "DEMO"
        data = _require_success(await self._request(
            "PUT", f"{path}/{quote(record_id, safe='')}", error_code,
            headers=self._headers(token, error_code), json={"fields": serialized},
        ), error_code)
        record = data.get("record")
        if not isinstance(record, dict) or record.get("record_id") != record_id:
            raise _invalid()

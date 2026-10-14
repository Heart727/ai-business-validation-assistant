import json
import traceback

import httpx
import pytest

from app.services.feishu import FeishuClient, FeishuError
from tests.conftest import REQUEST_ID
from tests.test_schemas import valid_output


TOKEN = "tenant-token"
BASE = "/open-apis/bitable/v1/apps/test-app-token/tables/test-table-id/records"


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_token_posts_configured_credentials(settings):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/open-apis/auth/v3/tenant_access_token/internal"
        assert json.loads(request.content) == {"app_id": "test-app-id", "app_secret": "test-app-secret"}
        assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 5.0)
        return httpx.Response(200, json={"code": 0, "tenant_access_token": TOKEN})

    async with mock_client(handler) as http_client:
        assert await FeishuClient(settings, http_client).get_tenant_token() == TOKEN


@pytest.mark.asyncio
async def test_create_uses_configured_table_and_fixed_scenario_fields(settings, scenario):
    def handler(request):
        assert request.method == "POST" and request.url.path == BASE
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        fields = json.loads(request.content)["fields"]
        assert fields == {
            "请求ID": REQUEST_ID, "演示标记": "DEMO", "场景名称": scenario.title,
            "行业": scenario.industry, "产品服务": scenario.offering,
            "目标客群": scenario.target_customer, "经营问题": scenario.business_problem,
            "现有指标": scenario.current_metrics, "预算范围": scenario.budget_range,
            "目标": scenario.goal, "时间限制": scenario.time_limit,
            "成功判定标准": scenario.success_criteria,
            "处理状态": "processing", "经营摘要": "", "关键观察": "[]",
            "风险": "[]", "7天验证动作": "[]", "错误码": "",
            "资料快照": scenario.model_dump_json(),
        }
        return httpx.Response(200, json={"code": 0, "data": {"record": {"record_id": "rec-1", "fields": fields}}})

    async with mock_client(handler) as http_client:
        record = await FeishuClient(settings, http_client).create_processing_record(TOKEN, REQUEST_ID, scenario)
    assert (record.record_id, record.request_id, record.status, record.analysis) == ("rec-1", REQUEST_ID, "processing", None)
    assert record.scenario == scenario


@pytest.mark.asyncio
async def test_create_rejects_record_that_is_not_processing(settings, scenario):
    def handler(request):
        fields = json.loads(request.content)["fields"]
        fields["处理状态"] = "retryable_failed"
        return httpx.Response(200, json={"code": 0, "data": {"record": {"record_id": "rec-1", "fields": fields}}})

    async with mock_client(handler) as http_client:
        with pytest.raises(FeishuError, match="^feishu_invalid_response$"):
            await FeishuClient(settings, http_client).create_processing_record(TOKEN, REQUEST_ID, scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize("items,expected", [([], None), ([{"record_id": "rec-1", "fields": {"请求ID": REQUEST_ID, "处理状态": "processing"}}], "rec-1")])
async def test_find_filters_by_request_id_and_handles_zero_or_one(settings, items, expected):
    def handler(request):
        assert request.method == "GET" and request.url.path == BASE
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert request.url.params["filter"] == f'CurrentValue.[请求ID] = "{REQUEST_ID}"'
        assert int(request.url.params["page_size"]) >= 2
        return httpx.Response(200, json={"code": 0, "data": {"items": items, "has_more": False}})

    async with mock_client(handler) as http_client:
        result = await FeishuClient(settings, http_client).find_by_request_id(TOKEN, REQUEST_ID)
    assert (result.record_id if result else None) == expected


@pytest.mark.asyncio
async def test_find_accepts_feishu_empty_result_without_items(settings):
    payload = {"code": 0, "data": {"has_more": False, "total": 0}}
    async with mock_client(lambda request: httpx.Response(200, json=payload)) as http_client:
        assert await FeishuClient(settings, http_client).find_by_request_id(TOKEN, REQUEST_ID) is None


@pytest.mark.asyncio
async def test_find_uses_encoded_filter_and_escapes_request_id(settings):
    request_id = 'id"\\end'

    def handler(request):
        assert request.url.params["filter"] == 'CurrentValue.[请求ID] = "id\\"\\\\end"'
        assert b"%5B" in request.url.raw_path
        assert b"%E8%AF%B7%E6%B1%82ID" in request.url.raw_path
        return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})

    async with mock_client(handler) as http_client:
        assert await FeishuClient(settings, http_client).find_by_request_id(TOKEN, request_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["retryable_failed", "failed"])
async def test_find_accepts_only_runtime_failure_status(settings, status):
    item = {"record_id": "rec-1", "fields": {"请求ID": REQUEST_ID, "处理状态": status}}
    async with mock_client(lambda request: httpx.Response(200, json={"code": 0, "data": {"items": [item], "has_more": False}})) as http_client:
        client = FeishuClient(settings, http_client)
        if status == "failed":
            with pytest.raises(FeishuError, match="^feishu_invalid_response$"):
                await client.find_by_request_id(TOKEN, REQUEST_ID)
        else:
            record = await client.find_by_request_id(TOKEN, REQUEST_ID)
            assert record.status == "retryable_failed"


@pytest.mark.asyncio
async def test_find_rejects_duplicate_or_truncated_results(settings):
    for items, has_more, code in [
        ([{"record_id": "one", "fields": {"请求ID": REQUEST_ID, "处理状态": "processing"}}] * 2, False, "feishu_duplicate_request_id"),
        ([], True, "feishu_invalid_response"),
    ]:
        async with mock_client(lambda request: httpx.Response(200, json={"code": 0, "data": {"items": items, "has_more": has_more}})) as http_client:
            with pytest.raises(FeishuError) as error:
                await FeishuClient(settings, http_client).find_by_request_id(TOKEN, REQUEST_ID)
        assert error.value.code == code


@pytest.mark.asyncio
async def test_update_uses_authorization_and_configured_table(settings):
    def handler(request):
        assert request.method == "PUT" and request.url.path == BASE + "/rec-1"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert json.loads(request.content) == {"fields": {"处理状态": "completed", "演示标记": "DEMO"}}
        return httpx.Response(200, json={"code": 0, "data": {"record": {"record_id": "rec-1"}}})

    async with mock_client(handler) as http_client:
        await FeishuClient(settings, http_client).update_record(TOKEN, "rec-1", {"处理状态": "completed"})


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"处理状态": "failed"}, {"处理状态": "unknown"}, {"处理状态": None},
    {"演示标记": "LIVE"}, {"演示标记": "DEMO"}, {"演示标记": None},
    {"请求ID": "other"}, {"风险": "not a list"},
])
async def test_update_rejects_invalid_fields_before_request(settings, fields):
    def handler(request):
        pytest.fail("Invalid update reached Feishu")

    async with mock_client(handler) as http_client:
        with pytest.raises(FeishuError, match="^feishu_update_failed$"):
            await FeishuClient(settings, http_client).update_record(TOKEN, "rec-1", fields)


@pytest.mark.asyncio
async def test_update_keeps_demo_marker_for_valid_retryable_failure(settings):
    def handler(request):
        assert json.loads(request.content) == {"fields": {"处理状态": "retryable_failed", "错误码": "dify_timeout", "演示标记": "DEMO"}}
        return httpx.Response(200, json={"code": 0, "data": {"record": {"record_id": "rec-1"}}})

    async with mock_client(handler) as http_client:
        await FeishuClient(settings, http_client).update_record(TOKEN, "rec-1", {"处理状态": "retryable_failed", "错误码": "dify_timeout"})


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"code": 99991663, "msg": "permission denied", "data": {}}),
    httpx.Response(401, text="secret upstream body"),
    httpx.Response(429, text="secret upstream body"),
    httpx.Response(200, text="not-json"),
    httpx.Response(200, json={"code": 0, "data": {}}),
])
async def test_token_failures_are_sanitized(settings, response):
    async with mock_client(lambda request: response) as http_client:
        with pytest.raises(FeishuError) as error:
            await FeishuClient(settings, http_client).get_tenant_token()
    assert error.value.code in {"feishu_auth_failed", "feishu_invalid_response"}
    assert "permission denied" not in str(error.value)
    assert "secret upstream body" not in str(error.value)
    assert settings.feishu_app_secret not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,expected", [("create", "feishu_create_failed"), ("find", "feishu_lookup_failed"), ("update", "feishu_update_failed")])
async def test_record_nonzero_code_is_sanitized(settings, scenario, method, expected):
    async with mock_client(lambda request: httpx.Response(200, json={"code": 123, "msg": "private upstream text", "data": {}})) as http_client:
        client = FeishuClient(settings, http_client)
        with pytest.raises(FeishuError) as error:
            if method == "create":
                await client.create_processing_record(TOKEN, REQUEST_ID, scenario)
            elif method == "find":
                await client.find_by_request_id(TOKEN, REQUEST_ID)
            else:
                await client.update_record(TOKEN, "rec-1", {"处理状态": "retryable_failed"})
    assert error.value.code == expected
    assert "private upstream text" not in str(error.value)


@pytest.mark.asyncio
async def test_find_rejects_unexpected_record_shape(settings):
    async with mock_client(lambda request: httpx.Response(200, json={"code": 0, "data": {"items": [{"record_id": "rec-1", "fields": {"请求ID": "wrong", "处理状态": "processing"}}], "has_more": False}})) as http_client:
        with pytest.raises(FeishuError) as error:
            await FeishuClient(settings, http_client).find_by_request_id(TOKEN, REQUEST_ID)
    assert error.value.code == "feishu_invalid_response"


async def invoke(client, method, scenario):
    if method == "auth":
        return await client.get_tenant_token()
    if method == "create":
        return await client.create_processing_record(TOKEN, REQUEST_ID, scenario)
    if method == "find":
        return await client.find_by_request_id(TOKEN, REQUEST_ID)
    return await client.update_record(TOKEN, "rec-1", {"处理状态": "retryable_failed"})


@pytest.mark.asyncio
@pytest.mark.parametrize("method,code", [
    ("auth", "feishu_auth_failed"), ("create", "feishu_create_failed"),
    ("find", "feishu_lookup_failed"), ("update", "feishu_update_failed"),
])
@pytest.mark.parametrize("failure", [401, 429, 500, 302, "timeout", "connection", "json"])
async def test_all_operations_sanitize_failures(settings, scenario, method, code, failure):
    private = "private-body-token-secret"

    def handler(request):
        assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 5.0)
        if failure == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if failure == "connection":
            raise httpx.ConnectError(private, request=request)
        if failure == "json":
            return httpx.Response(200, text=private)
        return httpx.Response(failure, text=private, headers={"Location": "https://elsewhere.invalid"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as http_client:
        with pytest.raises(FeishuError) as error:
            await invoke(FeishuClient(settings, http_client), method, scenario)
    assert error.value.code == ("feishu_invalid_response" if failure == "json" else code)
    rendered = "".join(traceback.format_exception(error.value))
    assert private not in rendered
    assert settings.feishu_app_secret not in rendered
    assert TOKEN not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["auth", "create", "find", "update"])
@pytest.mark.parametrize("payload", [None, [], {}, {"code": False}, {"code": "0"}, {"code": 0.0}, {"code": 0, "data": []}, {"code": 0, "data": {}}])
async def test_malformed_success_is_rejected(settings, scenario, method, payload):
    async with mock_client(lambda request: httpx.Response(200, json=payload)) as http_client:
        with pytest.raises(FeishuError) as error:
            await invoke(FeishuClient(settings, http_client), method, scenario)
    assert error.value.code == "feishu_invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    {}, {"items": []}, {"items": {}, "has_more": False},
    {"has_more": False}, {"has_more": False, "total": 1},
    {"items": [], "has_more": "false"},
    {"items": [None], "has_more": False},
    {"items": [{"record_id": "", "fields": {"请求ID": REQUEST_ID, "处理状态": "processing"}}], "has_more": False},
    {"items": [{"record_id": "rec-1", "fields": {"请求ID": REQUEST_ID, "处理状态": "unknown"}}], "has_more": False},
    {"items": [{"record_id": "rec-1", "fields": {"请求ID": REQUEST_ID, "处理状态": "completed"}}], "has_more": False},
])
async def test_lookup_fails_closed_on_malformed_data(settings, data):
    async with mock_client(lambda request: httpx.Response(200, json={"code": 0, "data": data})) as http_client:
        with pytest.raises(FeishuError) as error:
            await FeishuClient(settings, http_client).find_by_request_id(TOKEN, REQUEST_ID)
    assert error.value.code == "feishu_invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", [None, "bad-json", "bad-schema"])
async def test_completed_analysis_is_parsed_and_validated(settings, broken):
    analysis = valid_output()
    fields = {"请求ID": REQUEST_ID, "处理状态": "completed", "经营摘要": analysis["executive_summary"],
              "关键观察": json.dumps(analysis["observations"], ensure_ascii=False),
              "风险": "[]", "7天验证动作": json.dumps(analysis["seven_day_actions"], ensure_ascii=False)}
    if broken:
        fields["7天验证动作"] = "private invalid json" if broken == "bad-json" else "[]"
    payload = {"code": 0, "data": {"items": [{"record_id": "rec-1", "fields": fields}], "has_more": False}}
    async with mock_client(lambda request: httpx.Response(200, json=payload)) as http_client:
        client = FeishuClient(settings, http_client)
        if broken:
            with pytest.raises(FeishuError, match="^feishu_invalid_response$"):
                await client.find_by_request_id(TOKEN, REQUEST_ID)
        else:
            record = await client.find_by_request_id(TOKEN, REQUEST_ID)
            assert record.analysis.model_dump() == analysis


@pytest.mark.asyncio
async def test_update_serializes_lists_as_unicode_json_without_mutating_input(settings):
    fields = {"关键观察": ["中文观察"], "风险": [], "7天验证动作": valid_output()["seven_day_actions"]}

    def handler(request):
        sent = json.loads(request.content)["fields"]
        assert sent["关键观察"] == '["中文观察"]'
        assert sent["风险"] == "[]"
        assert json.loads(sent["7天验证动作"]) == fields["7天验证动作"]
        return httpx.Response(200, json={"code": 0, "data": {"record": {"record_id": "rec-1"}}})

    async with mock_client(handler) as http_client:
        await FeishuClient(settings, http_client).update_record(TOKEN, "rec-1", fields)
    assert fields["关键观察"] == ["中文观察"]


@pytest.mark.asyncio
async def test_settings_control_endpoint_and_table(settings, scenario):
    settings = settings.model_copy(update={"feishu_base_url": "https://feishu.example", "feishu_app_token": "configured-app", "feishu_table_id": "configured-table"})

    def handler(request):
        assert request.url.host == "feishu.example"
        assert request.url.path == "/open-apis/bitable/v1/apps/configured-app/tables/configured-table/records"
        return httpx.Response(200, json={"code": 0, "data": {"record": {"record_id": "rec-1", "fields": json.loads(request.content)["fields"]}}})

    async with mock_client(handler) as http_client:
        await FeishuClient(settings, http_client).create_processing_record(TOKEN, REQUEST_ID, scenario)


@pytest.mark.asyncio
async def test_missing_settings_fail_before_network(settings, scenario):
    def handler(request):
        pytest.fail("Missing credentials must not make a request")

    async with mock_client(handler) as http_client:
        for method, key, code in [("auth", "feishu_app_secret", "feishu_auth_failed"), ("create", "feishu_table_id", "feishu_create_failed")]:
            client = FeishuClient(settings.model_copy(update={key: None}), http_client)
            with pytest.raises(FeishuError, match=f"^{code}$"):
                await invoke(client, method, scenario)

from uuid import uuid4
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from tests.conftest import REQUEST_ID


def analyze(client, scenarios):
    response = client.post("/api/analyze", json={
        "scenario_id": scenarios.all()[0].id, "request_id": REQUEST_ID,
        "request_token": "371ca6c6-67ec-48bc-93df-c0a0d036c133",
    })
    assert response.status_code == 200
    return {"request_id": REQUEST_ID,
            "access_token": response.json().get("access_token", "not-issued")}


def execution(day, metric="虚构指标：今日30单，预订占比30%", conclusion="虚构结论：继续收集样本"):
    return {"day": day, "status": "completed",
            "observed_metric": metric, "conclusion": conclusion}


def test_save_two_days_and_edit_one_preserves_other_day_without_ai(full_mock_stack):
    app, scenarios, feishu, dify = full_mock_stack
    with TestClient(app) as client:
        access = analyze(client, scenarios)
        first = client.post("/api/actions", json={**access, "execution": execution(1)})
        second = client.post("/api/actions", json={**access, "execution": execution(2)})
        edited = client.post("/api/actions", json={
            **access, "execution": execution(1, metric="虚构指标：修正为31单"),
        })
        restored = client.post("/api/record", json=access)
    assert first.status_code == second.status_code == edited.status_code == restored.status_code == 200
    saved = restored.json()
    assert len(saved["executions"]) == 2
    by_day = {item["day"]: item for item in saved["executions"]}
    assert by_day[1]["observed_metric"] == "虚构指标：修正为31单"
    assert by_day[2]["observed_metric"] == execution(2)["observed_metric"]
    assert by_day[1]["updated_at"]
    assert saved["scenario"]["id"] == scenarios.all()[0].id
    assert len(dify.calls) == feishu.create_count == 1


def test_forged_or_other_record_token_fails_before_feishu(full_mock_stack):
    app, scenarios, feishu, _ = full_mock_stack
    with TestClient(app) as client:
        access = analyze(client, scenarios)
        before = len(feishu.events)
        forged = client.post("/api/record", json={**access, "access_token": "0" * 64})
        changed_uuid = client.post("/api/actions", json={
            **access, "request_id": str(uuid4()), "execution": execution(1),
        })
    assert forged.status_code == changed_uuid.status_code == 403
    assert len(feishu.events) == before


def test_completed_day_requires_real_input_fields_and_valid_day(full_mock_stack):
    app, scenarios, _, dify = full_mock_stack
    with TestClient(app) as client:
        access = analyze(client, scenarios)
        empty = client.post("/api/actions", json={
            **access, "execution": execution(1, metric=" "),
        })
        invalid_day = client.post("/api/actions", json={
            **access, "execution": execution(8),
        })
        sensitive = client.post("/api/actions", json={
            **access, "execution": execution(1, metric="联系13800138000"),
        })
    assert empty.status_code == invalid_day.status_code == sensitive.status_code == 422
    assert "13800138000" not in sensitive.text
    assert len(dify.calls) == 1


def test_legacy_request_cannot_be_upgraded_to_execution_access_without_ownership(full_mock_stack):
    app, scenarios, _, _ = full_mock_stack
    body = {"scenario_id": scenarios.all()[0].id, "request_id": REQUEST_ID}
    with TestClient(app) as client:
        legacy = client.post("/api/analyze", json=body)
        upgrade = client.post("/api/retry", json={
            **body, "request_token": "371ca6c6-67ec-48bc-93df-c0a0d036c133",
        })
    assert legacy.status_code == 200
    assert legacy.json().get("access_token") is None
    assert upgrade.status_code == 403


@pytest.mark.parametrize("missing,expected_status,code", [
    (True, 404, "record_not_found"),
    (False, 409, "record_not_completed"),
])
def test_missing_or_incomplete_report_cannot_accept_execution(
    full_mock_stack, missing, expected_status, code,
):
    app, scenarios, feishu, dify = full_mock_stack
    with TestClient(app) as client:
        access = analyze(client, scenarios)
        if missing:
            feishu.records.pop(REQUEST_ID)
        else:
            feishu.records[REQUEST_ID] = replace(
                feishu.records[REQUEST_ID], status="processing", analysis=None,
            )
        response = client.post("/api/actions", json={**access, "execution": execution(1)})
    assert response.status_code == expected_status
    assert response.json() == {"detail": {"code": code}}
    assert feishu.create_count == len(dify.calls) == 1


def test_all_seven_days_are_independent_and_last_day_survives_other_edits(full_mock_stack):
    app, scenarios, _, dify = full_mock_stack
    with TestClient(app) as client:
        access = analyze(client, scenarios)
        for day in range(1, 8):
            response = client.post("/api/actions", json={
                **access, "execution": execution(day, metric=f"虚构指标：第{day}天"),
            })
            assert response.status_code == 200
        client.post("/api/actions", json={
            **access, "execution": execution(4, metric="虚构指标：第4天修正"),
        }).raise_for_status()
        restored = client.post("/api/record", json=access).json()
    by_day = {item["day"]: item for item in restored["executions"]}
    assert sorted(by_day) == list(range(1, 8))
    assert by_day[7]["observed_metric"] == "虚构指标：第7天"
    assert by_day[4]["observed_metric"] == "虚构指标：第4天修正"
    assert len(dify.calls) == 1

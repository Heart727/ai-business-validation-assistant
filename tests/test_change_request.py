import asyncio
import json
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services.feishu import FeishuClient, FeishuRecord
from app.services.pipeline import PipelineError, PipelineService
from app.services.scenarios import ScenarioCatalog
from tests.conftest import REQUEST_ID
from tests.test_intake import REQUEST_TOKEN, draft_for
from tests.test_pipeline import RecordingDify, RecordingFeishu


def test_new_draft_requires_success_criteria_before_creating_record(full_mock_stack):
    app, scenarios, feishu, dify = full_mock_stack
    scenario = scenarios.all()[0]
    draft = draft_for(scenario)
    draft.pop('success_criteria', None)
    with TestClient(app) as client:
        response = client.post('/api/analyze', json={
            'scenario_id': scenario.id, 'request_id': REQUEST_ID,
            'request_token': REQUEST_TOKEN, 'draft': draft,
        })
    assert response.status_code == 422
    assert 'success_criteria' in str(response.json()['detail']['fields'])
    assert feishu.create_count == 0
    assert dify.calls == []


def test_criterion_is_sent_to_model_and_saved_in_same_snapshot(full_mock_stack):
    app, scenarios, feishu, dify = full_mock_stack
    scenario = scenarios.all()[0]
    captured = []
    original = dify.run_workflow
    async def capture(value, request_id):
        captured.append(value)
        return await original(value, request_id)
    dify.run_workflow = capture
    criterion = '虚构门槛：七天预订占比达到40%，预算不超过300元'
    with TestClient(app) as client:
        response = client.post('/api/analyze', json={
            'scenario_id': scenario.id, 'request_id': REQUEST_ID,
            'request_token': REQUEST_TOKEN,
            'draft': draft_for(scenario, success_criteria=criterion),
        })
    assert response.status_code == 200
    assert captured[0].success_criteria == criterion
    assert feishu.records[REQUEST_ID].scenario.success_criteria == criterion


@pytest.mark.asyncio
async def test_overlapping_requests_create_one_record_and_run_model_once(scenario):
    events = []
    class YieldingFeishu(RecordingFeishu):
        async def find_by_request_id(self, token, request_id):
            found = await super().find_by_request_id(token, request_id)
            await asyncio.sleep(0)
            return found
        async def update_record(self, token, record_id, fields):
            from app.schemas import AnalysisOutput
            self.update_record_ids.append(record_id)
            self.records[REQUEST_ID] = FeishuRecord(record_id, REQUEST_ID, fields['处理状态'],
                AnalysisOutput.model_validate({'executive_summary': fields['经营摘要'],
                    'observations': fields['关键观察'], 'risks': fields['风险'],
                    'seven_day_actions': fields['7天验证动作']}))
    feishu = YieldingFeishu(events)
    dify = RecordingDify(events)
    dify.delay = .005
    pipeline = PipelineService(feishu, dify, ScenarioCatalog([scenario]))
    results = await asyncio.gather(*(pipeline.submit(scenario.id, REQUEST_ID) for _ in range(8)))
    assert all(item == results[0] for item in results)
    assert feishu.create_count == 1
    assert len(dify.calls) == 1


@pytest.mark.asyncio
async def test_create_passes_stable_v4_client_token_to_feishu(settings, scenario):
    operations = []
    def handler(request):
        operation = request.url.params.get('client_token')
        assert operation is not None
        assert UUID(operation).version == 4
        operations.append(operation)
        fields = json.loads(request.content)['fields']
        return httpx.Response(200, json={'code': 0, 'data': {
            'record': {'record_id': 'rec-demo', 'fields': fields},
        }})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        gateway = FeishuClient(settings, http)
        await gateway.create_processing_record('tenant', REQUEST_ID, scenario)
        await gateway.create_processing_record('tenant', REQUEST_ID, scenario)
    assert operations[0] == operations[1]


@pytest.mark.asyncio
async def test_idempotent_create_response_cannot_grant_foreign_nonce_access(scenario):
    import hashlib
    events = []
    class ForeignFeishu(RecordingFeishu):
        async def create_processing_record(self, token, request_id, supplied, token_digest=None):
            row = FeishuRecord('rec-foreign', request_id, 'processing', scenario=supplied,
                              request_token_digest=hashlib.sha256(b'other-owner').hexdigest())
            self.records[request_id] = row
            return row
        async def update_record(self, token, record_id, fields):
            pass
    pipeline = PipelineService(ForeignFeishu(events), RecordingDify(events), ScenarioCatalog([scenario]))
    with pytest.raises(PipelineError, match='^record_access_denied$'):
        await pipeline.submit(scenario.id, REQUEST_ID, request_token=REQUEST_TOKEN)
    assert 'dify' not in events


@pytest.mark.parametrize('criterion', ['', ' ', 'x' * 301, '电话13800138000'])
def test_invalid_criterion_is_rejected_before_model_or_storage(full_mock_stack, criterion):
    app, scenarios, feishu, dify = full_mock_stack
    scenario = scenarios.all()[0]
    with TestClient(app) as client:
        result = client.post('/api/analyze', json={'scenario_id': scenario.id,
            'request_id': REQUEST_ID, 'request_token': REQUEST_TOKEN,
            'draft': draft_for(scenario, success_criteria=criterion)})
    assert result.status_code == 422
    assert '13800138000' not in result.text
    assert feishu.create_count == len(dify.calls) == 0


def test_old_draft_can_only_recover_existing_authenticated_record(full_mock_stack):
    import hashlib
    from app.schemas import Scenario, AnalysisOutput
    from tests.test_schemas import valid_output
    app, scenarios, feishu, dify = full_mock_stack
    scenario = scenarios.all()[0]
    old = draft_for(scenario)
    old.pop('success_criteria')
    body = {'scenario_id': scenario.id, 'request_id': REQUEST_ID,
            'request_token': REQUEST_TOKEN, 'draft': old}
    with TestClient(app) as client:
        absent = client.post('/api/retry', json=body)
        assert absent.status_code == 404
        assert feishu.create_count == 0
        feishu.records[REQUEST_ID] = FeishuRecord('rec-legacy', REQUEST_ID, 'completed',
            AnalysisOutput.model_validate(valid_output()),
            Scenario(id=scenario.id, **{k: v for k, v in old.items() if k != 'fictional_only'}),
            request_token_digest=hashlib.sha256(REQUEST_TOKEN.encode()).hexdigest())
        recovered = client.post('/api/retry', json=body)
        new = client.post('/api/analyze', json=body)
    assert recovered.status_code == 200
    assert recovered.json()['access_token']
    assert new.status_code == 422
    assert feishu.create_count == len(dify.calls) == 0


def test_same_request_cannot_silently_change_success_criterion(full_mock_stack):
    app, scenarios, feishu, dify = full_mock_stack
    scenario = scenarios.all()[0]
    body = {'scenario_id': scenario.id, 'request_id': REQUEST_ID,
            'request_token': REQUEST_TOKEN, 'draft': draft_for(scenario)}
    with TestClient(app) as client:
        assert client.post('/api/analyze', json=body).status_code == 200
        changed = client.post('/api/retry', json={**body, 'draft': {
            **body['draft'], 'success_criteria': '虚构门槛：七天目标达到90%'}})
    assert changed.status_code == 409
    assert feishu.records[REQUEST_ID].scenario.success_criteria == body['draft']['success_criteria']
    assert feishu.create_count == len(dify.calls) == 1


@pytest.mark.asyncio
async def test_stale_create_replay_reads_completed_record_before_model(scenario):
    events = []
    dify = RecordingDify(events)
    class StaleCreate(RecordingFeishu):
        async def create_processing_record(self, token, request_id, value):
            stale = await super().create_processing_record(token, request_id, value)
            self.records[request_id] = FeishuRecord(stale.record_id, request_id, 'completed', dify.analysis, value)
            return stale
    feishu = StaleCreate(events)
    result = await PipelineService(feishu, dify, ScenarioCatalog([scenario])).submit(scenario.id, REQUEST_ID)
    assert result.status == 'completed'
    assert len(dify.calls) == 0
    assert feishu.records[REQUEST_ID].status == 'completed'


@pytest.mark.asyncio
async def test_other_instance_completed_result_survives_late_model_failure(scenario):
    events = []
    feishu = RecordingFeishu(events)
    class LateFailure(RecordingDify):
        async def run_workflow(self, value, request_id):
            from app.services.dify import DifyError
            existing = feishu.records[request_id]
            feishu.records[request_id] = FeishuRecord(existing.record_id, request_id, 'completed', self.analysis, value)
            raise DifyError('dify_timeout')
    dify = LateFailure(events)
    result = await PipelineService(feishu, dify, ScenarioCatalog([scenario])).submit(scenario.id, REQUEST_ID)
    assert result.status == 'completed'
    assert feishu.records[REQUEST_ID].status == 'completed'


@pytest.mark.asyncio
async def test_legacy_fixed_sample_retry_uses_original_snapshot_without_new_threshold(scenario):
    events = []
    feishu, dify = RecordingFeishu(events), RecordingDify(events)
    old = scenario.model_copy(update={'success_criteria': None})
    feishu.records[REQUEST_ID] = FeishuRecord('rec-old', REQUEST_ID, 'retryable_failed', scenario=old)
    captured = []
    original = dify.run_workflow
    async def capture(value, request_id):
        captured.append(value)
        return await original(value, request_id)
    dify.run_workflow = capture
    result = await PipelineService(feishu, dify, ScenarioCatalog([scenario])).submit(scenario.id, REQUEST_ID)
    assert result.status == 'completed'
    assert captured[0].success_criteria is None


@pytest.mark.asyncio
async def test_complete_payload_remains_readable_when_status_was_raced(settings):
    from tests.test_schemas import valid_output
    analysis = valid_output()
    fields = {'请求ID': REQUEST_ID, '处理状态': 'retryable_failed',
        '经营摘要': analysis['executive_summary'], '关键观察': json.dumps(analysis['observations']),
        '风险': json.dumps(analysis['risks']), '7天验证动作': json.dumps(analysis['seven_day_actions'])}
    def handler(request):
        return httpx.Response(200, json={'code': 0, 'data': {'items': [{'record_id': 'rec-race', 'fields': fields}], 'has_more': False}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        record = await FeishuClient(settings, http).find_by_request_id('tenant', REQUEST_ID)
    assert record.status == 'completed' and record.analysis is not None


@pytest.mark.asyncio
async def test_completed_report_is_returned_even_if_status_repair_fails(scenario):
    from app.services.feishu import FeishuError
    from app.services.dify import DifyError
    events = []
    dify = RecordingDify(events)
    dify.error = DifyError('dify_timeout')
    class RepairFailure(RecordingFeishu):
        async def update_record(self, token, record_id, fields):
            if fields['处理状态'] == 'retryable_failed':
                self.records[REQUEST_ID] = FeishuRecord(record_id, REQUEST_ID, 'retryable_failed', dify.analysis)
            else:
                raise FeishuError('feishu_update_failed')
    feishu = RepairFailure(events)
    result = await PipelineService(feishu, dify, ScenarioCatalog([scenario])).submit(scenario.id, REQUEST_ID)
    assert result.status == 'completed' and result.analysis == dify.analysis

"""Reproduce the change-request acceptance with isolated mock integrations.

No network, platform credentials, real model calls or Feishu writes are used.
"""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import Settings
from app.main import create_app
from app.services.dify import DifyError
from app.services.pipeline import PipelineService
from app.services.scenarios import ScenarioCatalog
from tests.fakes import DeterministicDify, InMemoryFeishu


async def verify():
    scenarios = ScenarioCatalog.load(ROOT / 'data/scenarios.json')
    feishu, dify = InMemoryFeishu(), DeterministicDify()
    pipeline = PipelineService(feishu, dify, scenarios)
    settings = Settings(demo_mode=True, feishu_app_id='mock-app', feishu_app_secret='mock-secret',
        feishu_app_token='mock-base', feishu_table_id='mock-table', dify_workflow_api_key='mock-key')
    app = create_app(settings, pipeline, scenarios)
    scenario = scenarios.all()[0]
    body = {'scenario_id': scenario.id, 'request_id': str(uuid4()),
        'request_token': str(uuid4()), 'draft': {
            **scenario.model_dump(exclude={'id'}), 'fictional_only': True}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://mock.test') as client:
        incomplete = {**body['draft']};incomplete.pop('success_criteria')
        missing = await client.post('/api/analyze', json={**body, 'draft': incomplete})
        assert missing.status_code == 422 and feishu.create_count == len(dify.calls) == 0
        dify.error = DifyError('dify_upstream_failed')
        failed = await client.post('/api/analyze', json=body)
        assert failed.status_code == 200 and failed.json()['status'] == 'retryable_failed'
        row = feishu.records[body['request_id']]
        original_id = row.record_id
        assert row.scenario.success_criteria == body['draft']['success_criteria']
        assert row.status == 'retryable_failed'
        dify.error = None
        recovered = await client.post('/api/retry', json=body)
        repeated = await client.post('/api/analyze', json=body)
        assert recovered.status_code == repeated.status_code == 200
        assert recovered.json() == repeated.json()
        assert feishu.records[body['request_id']].record_id == original_id
        assert feishu.create_count == 1 and len(dify.calls) == 2
    class YieldingDify(DeterministicDify):
        async def run_workflow(self, scenario, request_id):
            await asyncio.sleep(.01)
            return await super().run_workflow(scenario, request_id)
    parallel_feishu, parallel_dify = InMemoryFeishu(), YieldingDify()
    parallel = PipelineService(parallel_feishu, parallel_dify, scenarios)
    parallel_id = str(uuid4())
    results = await asyncio.gather(*(parallel.submit(scenario.id, parallel_id) for _ in range(8)))
    assert all(result == results[0] for result in results)
    assert parallel_feishu.create_count == len(parallel_dify.calls) == 1
    report = {
        'mode': 'mock_no_network', 'new_field': 'success_criteria',
        'required_field_rejected': missing.status_code,
        'failed_call_kept_input_and_record': True,
        'retry_and_repeat_same_record': True,
        'original_request_records': feishu.create_count,
        'simulated_model_attempts': len(dify.calls),
        'parallel_requests': 8, 'parallel_records': parallel_feishu.create_count,
        'parallel_model_calls': len(parallel_dify.calls),
        'note': '独立模拟集成验收；无真实AI调用、无网络、未写入飞书；跨实例幂等创建另用真实飞书核验。',
    }
    (ROOT / 'docs/change-request-acceptance.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('required field:422 before storage/model; simulated model failure:input/row retained; retry/repeat:one row; 8 concurrent requests:one row/one model call. MOCK ONLY.')


if __name__ == '__main__':
    asyncio.run(verify())

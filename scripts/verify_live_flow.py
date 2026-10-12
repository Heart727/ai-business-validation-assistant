"""Ten fictional inputs through local FastAPI and REAL Dify/Feishu services.

Requires configured .env with DEMO_MODE=true. Consumes model quota and writes
DEMO records. Never disables or bypasses production rate limits.
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.schemas import AnalysisOutput, RecordDetails


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def verify(limit, output, state_path):
    import index
    if not index.settings.live_demo_ready:
        raise RuntimeError("live_configuration_required")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    report = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {
        "verified_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "mode": "local_fastapi_real_dify_and_feishu",
        "note": "虚构验收数据；执行日志仅验证保存能力，不代表实际经营成果。",
        "cases": [], "error_cases": [],
    }
    done = {case["scenario_id"] for case in report["cases"] if case["status"] == "completed"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=index.app), base_url="http://live-acceptance.local",
        timeout=70,
    ) as api:
        for number, scenario in enumerate(index.scenarios.all()[:limit], 1):
            if scenario.id in done:
                print(f"{number}/10 {scenario.id}: already verified", flush=True)
                continue
            draft = {
                **scenario.model_dump(exclude={"id"}),
                "title": f"虚构验收：{scenario.title.removeprefix('虚构样例：').strip()}",
                "current_metrics": scenario.current_metrics + f"；虚构补充：本次样本数为{12 + number}",
                "fictional_only": True,
            }
            if scenario.id not in state:
                state[scenario.id] = {
                    "request_id": str(uuid4()), "request_token": str(uuid4()), "draft": draft,
                }
                write_json(state_path, state)
            saved_state = state[scenario.id]
            body = {"scenario_id": scenario.id, **saved_state}
            started = time.monotonic()
            response = await api.post("/api/analyze", json=body)
            payload = response.json()
            if response.status_code != 200 or payload.get("status") != "completed":
                code = payload.get("error_code") or payload.get("detail", {}).get("code") or "unknown"
                print(f"{number}/10 {scenario.id}: failed ({code})", flush=True)
                return False
            analysis = AnalysisOutput.model_validate(payload["analysis"])
            access = {"request_id": payload["request_id"], "access_token": payload["access_token"]}
            details_response = await api.post("/api/record", json=access)
            if details_response.status_code != 200:
                raise RuntimeError("readback_failed")
            details = RecordDetails.model_validate(details_response.json())
            expected = {key: value for key, value in saved_state["draft"].items() if key != "fictional_only"}
            if details.scenario.model_dump(exclude={"id"}) != expected:
                raise RuntimeError("input_snapshot_mismatch")
            repeated = await api.post("/api/retry", json=body)
            if repeated.status_code != 200 or repeated.json() != payload:
                raise RuntimeError("idempotent_retry_mismatch")
            execution = {
                "day": 1, "status": "completed",
                "observed_metric": "虚构验收指标：测试记录7次，有效反馈3次",
                "conclusion": "仅验证执行记录保存功能，并未执行真实经营实验",
            }
            log_response = await api.post("/api/actions", json={**access, "execution": execution})
            if log_response.status_code != 200:
                raise RuntimeError("execution_save_failed")
            restored = await api.post("/api/record", json=access)
            restored_data = RecordDetails.model_validate(restored.json())
            if not any(item.day == 1 and item.observed_metric == execution["observed_metric"]
                       for item in restored_data.executions):
                raise RuntimeError("execution_readback_failed")
            token = await index.pipeline.feishu.get_tenant_token()
            record = await index.pipeline.feishu.find_by_request_id(token, payload["request_id"])
            report["cases"].append({
                "scenario_id": scenario.id, "request_id": payload["request_id"],
                "record_id": record.record_id, "status": "completed",
                "input": details.scenario.model_dump(),
                "analysis": analysis.model_dump(),
                "execution": restored_data.executions[0].model_dump(),
                "checks": ["edited_input_saved", "seven_distinct_days", "structured_observations",
                           "same_request_same_result", "execution_saved_and_restored"],
                "elapsed_seconds": round(time.monotonic() - started, 2),
            })
            if not report["error_cases"]:
                invalid = await api.post("/api/analyze", json={
                    **body, "draft": {**body["draft"], "current_metrics": "电话13800138000"},
                })
                conflict = await api.post("/api/retry", json={
                    **body, "draft": {**body["draft"], "goal": "虚构验收：与原请求不同的目标"},
                })
                denied = await api.post("/api/record", json={**access, "access_token": "0" * 64})
                expected_errors = [("contact_input_rejected", invalid, 422),
                                   ("changed_input_conflict", conflict, 409),
                                   ("forged_access_denied", denied, 403)]
                for name, result, expected_status in expected_errors:
                    if result.status_code != expected_status:
                        raise RuntimeError("failure_boundary_mismatch")
                    report["error_cases"].append({
                        "scenario": name, "status": result.status_code,
                        "error_code": result.json()["detail"]["code"],
                    })
            write_json(output, report)
            print(f"{number}/10 {scenario.id}: completed; saved, retried, log restored", flush=True)
    return len(report["cases"]) == limit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "live-acceptance.json")
    parser.add_argument("--state-file", type=Path, default=ROOT / ".live-acceptance-state.json")
    args = parser.parse_args()
    if not 1 <= args.limit <= 10:
        parser.error("--limit must be between 1 and 10")
    try:
        passed = asyncio.run(verify(args.limit, args.output, args.state_file))
    except Exception as error:
        print(f"live acceptance failed ({type(error).__name__}); credentials and upstream bodies omitted")
        return 1
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

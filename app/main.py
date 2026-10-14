"""FastAPI routes for the fictional-scenario public demo."""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.config import Settings
from app.schemas import AnalysisOutput, ExecutionRequest, PipelineResult, RecordAccess, RecordDetails, ScenarioDraft, ScenarioRequest, LegacyScenarioDraft, RetryScenarioRequest
from app.services.pipeline import PipelineError, PipelineService
from app.services.scenarios import ScenarioCatalog, UnknownScenario
from app.services.tracking import TrackingService


_PUBLIC_PIPELINE_CODES = frozenset({
    "feishu_auth_failed",
    "feishu_lookup_failed",
    "feishu_create_failed",
    "feishu_update_failed",
    "feishu_invalid_response",
    "feishu_duplicate_request_id",
    "pipeline_timeout",
    "request_content_conflict",
    "record_access_denied",
    "record_not_found",
    "record_not_completed",
})


def create_app(
    settings: Settings,
    pipeline: PipelineService,
    scenarios: ScenarioCatalog,
) -> FastAPI:
    app = FastAPI(title="AI 经营分析与 7 天验证助手", version="0.1.0")
    tracking = TrackingService(settings, getattr(pipeline, "feishu", None))

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, error):
        # Never echo rejected input (contacts, secrets or recovery tokens).
        allowed = set(ScenarioRequest.model_fields) | set(ScenarioDraft.model_fields) | {
            "execution", "day", "status", "observed_metric", "conclusion", "access_token",
        }
        fields = sorted({
            ".".join(str(part) for part in item["loc"][1:] if str(part) in allowed)
            for item in error.errors()
        } - {""})
        return JSONResponse(status_code=422, content={
            "detail": {"code": "invalid_input", "fields": fields},
        })

    def public_error(error: PipelineError):
        code = error.code if error.code in _PUBLIC_PIPELINE_CODES else "service_unavailable"
        status = {
            "feishu_duplicate_request_id": 409, "request_content_conflict": 409,
            "record_access_denied": 403, "record_not_found": 404,
            "record_not_completed": 409, "pipeline_timeout": 504,
        }.get(code, 503)
        return HTTPException(status_code=status, detail={"code": code})
    preview_path = Path(__file__).resolve().parent.parent / "data" / "example_analysis.json"
    try:
        preview_data = json.loads(preview_path.read_text(encoding="utf-8"))
        preview = AnalysisOutput.model_validate(preview_data["analysis"])
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        raise RuntimeError("Static analysis preview is invalid") from None
    static_path = Path(__file__).resolve().parent.parent / "static"

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "live_demo_enabled": settings.live_demo_ready}

    @app.get("/api/scenarios")
    async def list_scenarios() -> list[dict[str, str]]:
        return [item.model_dump() for item in scenarios.all()]

    @app.get("/api/preview", response_model=AnalysisOutput)
    async def get_preview() -> AnalysisOutput:
        return preview

    async def submit(body: ScenarioRequest) -> PipelineResult:
        try:
            scenarios.get(body.scenario_id)
        except UnknownScenario:
            raise HTTPException(status_code=400, detail={"code": "unknown_scenario"}) from None

        if not settings.live_demo_ready:
            raise HTTPException(status_code=503, detail={"code": "demo_disabled"})

        try:
            if body.draft is None and body.request_token is None:
                result = await pipeline.submit(body.scenario_id, str(body.request_id))
            else:
                compatibility = isinstance(body.draft, LegacyScenarioDraft) and not isinstance(body.draft, ScenarioDraft)
                result = await pipeline.submit(
                    body.scenario_id, str(body.request_id), body.draft,
                    str(body.request_token) if body.request_token else None,
                    **({"allow_create": False} if compatibility else {}),
                )
            if result.status == "completed" and body.request_token is not None:
                result = result.model_copy(update={"access_token": tracking.issue_token(str(body.request_id))})
            return result
        except UnknownScenario:
            raise HTTPException(status_code=400, detail={"code": "unknown_scenario"}) from None
        except PipelineError as error:
            raise public_error(error) from None

    @app.post("/api/analyze", response_model=PipelineResult)
    async def analyze(body: ScenarioRequest) -> PipelineResult:
        return await submit(body)

    @app.post("/api/retry", response_model=PipelineResult)
    async def retry(body: RetryScenarioRequest) -> PipelineResult:
        return await submit(body)

    async def record_operation(body: RecordAccess, execution=None):
        if not settings.live_demo_ready:
            raise HTTPException(status_code=503, detail={"code": "demo_disabled"})
        try:
            if execution is None:
                return await tracking.get_record(str(body.request_id), body.access_token)
            return await tracking.save_execution(str(body.request_id), body.access_token, execution)
        except PipelineError as error:
            raise public_error(error) from None

    @app.post("/api/record", response_model=RecordDetails)
    async def get_record(body: RecordAccess) -> RecordDetails:
        return await record_operation(body)

    @app.post("/api/actions", response_model=RecordDetails)
    async def save_action(body: ExecutionRequest) -> RecordDetails:
        return await record_operation(body, body.execution)

    app.mount("/static", StaticFiles(directory=static_path), name="static")

    @app.get("/", include_in_schema=False)
    async def homepage() -> FileResponse:
        return FileResponse(static_path / "index.html")

    return app

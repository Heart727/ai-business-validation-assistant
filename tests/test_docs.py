import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_project_file(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_readme_has_reproducible_safe_local_and_public_setup():
    readme = read_project_file("README.md")

    assert "uvicorn index:app" in readme
    assert "DEMO_MODE=false" in readme
    assert "DIFY_WORKFLOW_API_KEY" in readme
    assert "FEISHU_APP_SECRET" in readme
    assert "DEEPSEEK_API_KEY" in readme
    assert "5 次" in readme and "10 分钟" in readme
    assert "10 份虚构" in readme
    assert "pytest" in readme
    assert "静态预览" in readme
    for screenshot in (
        "live-demo-form.png", "live-demo-analysis.png", "live-demo-tracking.png",
        "live-demo-snapshot.png",
    ):
        assert f"docs/screenshots/{screenshot}" in readme
    assert "实时分析" in readme
    assert "live_demo_enabled=true" in readme
    assert not re.search(r"(?:sk-[A-Za-z0-9]{20,}|cli_[A-Za-z0-9]{10,}|dify-[A-Za-z0-9_-]{20,})", readme)
    assert "FEISHU_APP_SECRET=" not in readme
    assert "DIFY_WORKFLOW_API_KEY=" not in readme


def test_environment_example_lists_only_server_side_names_and_safe_defaults():
    example = read_project_file(".env.example")
    expected = {
        "DEMO_MODE", "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_TOKEN",
        "FEISHU_TABLE_ID", "FEISHU_BASE_URL", "DIFY_BASE_URL", "DIFY_WORKFLOW_API_KEY",
    }
    names = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line}

    assert expected <= names
    assert "DEMO_MODE=false" in example
    assert "DEEPSEEK_API_KEY" not in names
    assert all(not line.split("=", 1)[1].strip() for line in example.splitlines()
               if "_SECRET=" in line or "_API_KEY=" in line)
    assert ".env" in read_project_file(".gitignore")


def test_feishu_and_dify_guides_match_the_application_contract():
    feishu = read_project_file("docs/feishu-table-fields.md")
    dify = read_project_file("docs/dify-workflow.md")

    for field in ("请求ID", "演示标记", "处理状态", "经营摘要", "关键观察", "风险", "7天验证动作", "错误码"):
        assert field in feishu
    assert "DEMO" in feishu
    assert "bitable" in feishu.lower()
    assert "scenario_json" in dify
    assert "request_id" in dify
    assert "analysis_json" in dify
    assert "DeepSeek" in dify
    assert "七" in dify
    assert "证据" in dify and "推断" in dify and "缺失信息" in dify


def test_failure_guide_documents_input_output_recovery_and_limits():
    failures = read_project_file("docs/failure-cases.md")
    case_study = read_project_file("docs/case-study.md")

    for code in (
        "unknown_scenario", "dify_timeout", "dify_invalid_output", "feishu_auth_failed",
        "feishu_lookup_failed", "feishu_create_failed", "feishu_update_failed",
        "feishu_invalid_response",
    ):
        assert code in failures
    for label in ("输入", "用户看到", "恢复", "限制"):
        assert label in failures
    assert "流程" in case_study and "验证" in case_study
    assert "公开实时演示" in case_study
    assert "10 份真实集成验收" in read_project_file("README.md")
    assert "feishu_auth_failed` 仅表示租户令牌获取失败" in failures
    assert "feishu_update_failed` 无法确认最终记录状态" in failures
    assert "feishu_invalid_response` 用于飞书返回内容不可解析或结构不符" in failures
    assert "离线模式和手机端的旧截图" in case_study


def test_vercel_entry_and_screenshots_are_present():
    config = json.loads(read_project_file("vercel.json"))

    assert config["functions"]["index.py"]["maxDuration"] == 60
    assert (ROOT / "docs/screenshots/static-preview-desktop.png").is_file()
    assert (ROOT / "docs/screenshots/static-preview-mobile.png").is_file()
    for screenshot in (
        "live-demo-form.png", "live-demo-analysis.png", "live-demo-tracking.png",
        "live-demo-snapshot.png",
    ):
        assert (ROOT / "docs/screenshots" / screenshot).is_file()

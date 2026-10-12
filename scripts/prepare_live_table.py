"""Idempotently add the eight text fields needed by intake and tracking."""

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services.feishu import FeishuClient


async def prepare(apply: bool):
    settings = Settings.from_env()
    if not settings.live_demo_ready:
        raise RuntimeError("live_configuration_required")
    async with httpx.AsyncClient(timeout=15) as client:
        gateway = FeishuClient(settings, client)
        token = await gateway.get_tenant_token()
        path = (
            f"{settings.feishu_base_url.rstrip('/')}/open-apis/bitable/v1/apps/"
            f"{quote(settings.feishu_app_token, safe='')}/tables/"
            f"{quote(settings.feishu_table_id, safe='')}/fields"
        )
        headers = {"Authorization": f"Bearer {token}"}
        response = await client.get(path, headers=headers, params={"page_size": 100})
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuntimeError("field_list_failed")
        data = payload.get("data", {})
        if data.get("has_more"):
            raise RuntimeError("unexpected_field_pagination")
        existing = {field["field_name"]: field["type"] for field in data.get("items", [])}
        names = ["资料快照", *[f"第{day}天执行记录" for day in range(1, 8)]]
        for name in names:
            if name in existing:
                if existing[name] != 1:
                    raise RuntimeError("field_type_mismatch")
                print(f"present: {name}")
                continue
            if not apply:
                print(f"missing: {name}")
                continue
            response = await client.post(path, headers=headers, json={"field_name": name, "type": 1})
            response.raise_for_status()
            if response.json().get("code") != 0:
                raise RuntimeError("field_create_failed")
            print(f"created: {name}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(prepare(args.apply))
    except Exception as error:
        # Never log provider response bodies, headers or credentials.
        print(f"table preparation failed ({type(error).__name__})")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Feishu image upload (tenant app) → img_key for card embedding."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

_TOKEN_CACHE: dict[str, Any] = {"token": "", "expire_at": 0.0}


def feishu_app_configured() -> bool:
    return bool(
        os.getenv("FEISHU_APP_ID", "").strip()
        and os.getenv("FEISHU_APP_SECRET", "").strip()
    )


def _tenant_access_token() -> str:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise ValueError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET，无法上传图片")

    now = time.time()
    if _TOKEN_CACHE["token"] and now < float(_TOKEN_CACHE["expire_at"]) - 60:
        return str(_TOKEN_CACHE["token"])

    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"获取飞书 tenant_access_token 失败: {data}")
    token = data["tenant_access_token"]
    expire = int(data.get("expire", 7200))
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expire_at"] = now + expire
    return token


def upload_image_png(png_bytes: bytes, *, image_type: str = "message") -> str:
    """
    Upload PNG and return image_key.
    Requires app permission: im:resource (上传图片).
    """
    if not png_bytes:
        raise ValueError("空图片")
    token = _tenant_access_token()
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/images",
        headers={"Authorization": f"Bearer {token}"},
        data={"image_type": image_type},
        files={"image": ("table.png", png_bytes, "image/png")},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"飞书上传图片失败: {data}")
    key = (data.get("data") or {}).get("image_key")
    if not key:
        raise RuntimeError(f"飞书上传图片未返回 image_key: {data}")
    return str(key)


def upload_png_list(images: list[bytes]) -> list[str]:
    keys: list[str] = []
    for png in images:
        keys.append(upload_image_png(png))
    return keys

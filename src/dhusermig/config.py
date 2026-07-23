from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass


@dataclass
class RunConfig:
    gms_url: str
    token: str | None = None
    batch_size: int = 50


def resolve_config(
    gms_url: str | None, token: str | None, batch_size: int = 50
) -> RunConfig:
    url = gms_url or os.environ.get("DATAHUB_GMS_URL")
    if not url:
        raise ValueError("GMS URL required (pass --gms-url or set DATAHUB_GMS_URL)")
    tok = token if token is not None else os.environ.get("DATAHUB_GMS_TOKEN")
    return RunConfig(gms_url=url, token=tok, batch_size=batch_size)


def gms_fingerprint(gms_url: str) -> str:
    return "sha256:" + hashlib.sha256(gms_url.encode()).hexdigest()

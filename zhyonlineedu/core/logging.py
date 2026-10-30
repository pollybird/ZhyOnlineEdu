"""结构化日志：JSON 行 + request_id 注入。"""

from __future__ import annotations

import json
import logging
from typing import Any

from flask import g

_MARKER = "_zhy_json"


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            request_id = str(getattr(g, "request_id", "") or "-")
        except RuntimeError:  # 无 Flask 应用上下文（启动/worker 场景）
            request_id = "-"
        record.request_id = request_id
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(env: str) -> None:
    """幂等：重复调用只调整级别，不叠加 handler。"""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if env == "dev" else logging.INFO)
    if any(getattr(handler, _MARKER, False) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    setattr(handler, _MARKER, True)
    root.addHandler(handler)

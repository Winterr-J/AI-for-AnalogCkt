"""Qwen Flash endpoint and error contract shared by preparation and Batch V2."""

from __future__ import annotations

import re


QWEN_FLASH_MODEL = "qwen-flash-2025-07-28"
_BEIJING_ENDPOINT = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"\.cn-beijing\.maas\.aliyuncs\.com/compatible-mode/v1\Z"
)


def validate_qwen_endpoint(value: object) -> str:
    """Accept only a canonical Beijing workspace Chat API base URL."""
    if not isinstance(value, str) or not _BEIJING_ENDPOINT.fullmatch(value):
        raise ValueError("Qwen requires an HTTPS Beijing workspace Chat API endpoint")
    return value


def qwen_failure_kind(error: Exception) -> str:
    """Classify documented Model Studio failures without parsing secret text."""
    status = getattr(error, "status_code", None)
    body = getattr(error, "body", None)
    detail = body.get("error", body) if isinstance(body, dict) else body
    code = (detail.get("code") if isinstance(detail, dict) else None) or getattr(error, "code", "")
    code = str(code or "").casefold()
    message = str(detail.get("message", "") if isinstance(detail, dict) else "").casefold()
    hard_billing = {
        "budgetlimitexceeded", "arrearage", "prepaidbilloverdue",
        "postpaidbilloverdue", "commoditynotpurchased",
    }
    if code in hard_billing or (status in {400, 429} and any(
            phrase in message for phrase in (
                "free allocated quota exceeded", "budget limit exceeded",
                "insufficient account balance"))):
        return "quota"
    if status == 429:
        # AllocationQuota / insufficient_quota are TPS/TPM throttling on Bailian.
        return "rate_limit"
    if status in {500, 502, 503, 504}:
        return "service_unavailable"
    if isinstance(error, (TimeoutError, ConnectionError)) or type(error).__name__ in {
        "ReadTimeout", "ConnectTimeout", "ConnectError", "APITimeoutError",
        "APIConnectionError", "RemoteProtocolError",
    }:
        return "network_timeout"
    return "terminal"

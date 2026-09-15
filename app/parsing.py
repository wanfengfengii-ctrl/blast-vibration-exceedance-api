"""请求体原始 JSON 解析。

标准 JSON 解析会丢失数字的词法形式：``5.000``、``5e0`` 解析后都变成
float ``5.0``，无法再判断“至多两位小数、不得使用科学计数法”。
因此在解析层通过 ``parse_float`` 钩子按数字的原始字面量校验。
"""

import json
import re

# 普通十进制数字：整数部分必填（不允许前导零），小数部分至多 2 位；
# 不含指数，也不接受 NaN / Infinity。
_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d{1,2})?")


class MalformedPayloadError(ValueError):
    """请求体不是合法 JSON，或其中数字写法不符合口径。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _parse_float(raw: str) -> float:
    if not _NUMBER_RE.fullmatch(raw):
        raise MalformedPayloadError(
            "invalid_number_format",
            f"数字写法不合法：{raw}（振速须为普通十进制数字、至多两位小数，"
            "不得使用科学计数法）",
        )
    return float(raw)


def _parse_constant(raw: str) -> float:
    # NaN / Infinity / -Infinity：Python json 默认放行，这里必须拒绝
    raise MalformedPayloadError(
        "invalid_number_format",
        f"数字写法不合法：{raw}（振速必须是有限数值）",
    )


def parse_json_payload(raw: bytes) -> object:
    """解析请求体；浮点字面量（含 5e0、5.000、NaN 等）按词法严格校验。"""
    if not raw:
        raise MalformedPayloadError("invalid_json", "请求体为空，期望 application/json")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MalformedPayloadError("invalid_json", "请求体不是合法的 UTF-8 JSON") from exc
    try:
        return json.loads(
            text, parse_float=_parse_float, parse_constant=_parse_constant
        )
    except MalformedPayloadError:
        raise
    except json.JSONDecodeError as exc:
        raise MalformedPayloadError("invalid_json", "请求体不是合法的 JSON") from exc

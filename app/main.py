"""FastAPI 应用：爆破振速持续超限复核。"""

import json
import re

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from .analysis import BatchValidationError, analyze
from .models import AnalyzeRequest, AnalyzeResponse
from .parsing import MalformedPayloadError, parse_json_payload

app = FastAPI(
    title="爆破振速复核 API",
    description="接收同一测点的振速采样，识别持续超限事件并给出放行 / 复核结论。",
    version="1.0.0",
)


class StrictJsonRoute(APIRoute):
    """用严格 JSON 解析替换默认解析：

    在数字被解析成 float、词法形式丢失之前拦截 5.000 / 5e0 / NaN 等写法。
    """

    def get_route_handler(self):
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request):
            raw = await request.body()
            data = parse_json_payload(raw)  # 词法不合法抛 MalformedPayloadError -> 422
            # 用重新序列化后的规范字节替换请求体，交由 FastAPI/Pydantic 正常校验
            request._body = json.dumps(data, allow_nan=False).encode("utf-8")
            return await original_route_handler(request)

        return custom_route_handler


def _validation_errors(exc: RequestValidationError) -> list[dict]:
    # 不回显输入值，只给定位与原因
    return [
        {"loc": list(err.get("loc", [])), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]


# excess_dose_mm 经 response_model 序列化后是两位小数字符串（"0.60"）
_EXCESS_DOSE_RE = re.compile(r'"excess_dose_mm":"(-?\d+(?:\.\d+)?)"')


class FixedDecimalJSONResponse(JSONResponse):
    """把 excess_dose_mm 的定点小数字符串还原为 JSON 数字字面量。

    Pydantic 将 Decimal 序列化为字符串 "0.60" 以保住两位小数；此处去掉
    引号，使线上输出为固定两位小数的 JSON 数字 0.60（解析后仍是数值）。
    仅作用于本路由的 200 响应体，键名唯一、取值已由模型校验，匹配安全。
    """

    def render(self, content) -> bytes:
        text = super().render(content).decode("utf-8")
        return _EXCESS_DOSE_RE.sub(r'"excess_dose_mm":\1', text).encode("utf-8")


@app.exception_handler(BatchValidationError)
async def batch_validation_exception_handler(
    _request: Request, exc: BatchValidationError
) -> JSONResponse:
    # 整批不合法：422 且不输出任何部分结果
    return JSONResponse(
        status_code=422,
        content={"detail": exc.message, "code": exc.code},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "detail": "请求数据不合法",
            "code": "invalid_request",
            "errors": _validation_errors(exc),
        },
    )


@app.exception_handler(MalformedPayloadError)
async def malformed_payload_exception_handler(
    _request: Request, exc: MalformedPayloadError
) -> JSONResponse:
    # 非 JSON、或数字词法不合法（5.000 / 5e0 / NaN 等）
    return JSONResponse(
        status_code=422,
        content={"detail": exc.message, "code": exc.code},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


router = APIRouter(route_class=StrictJsonRoute)


@router.post(
    "/api/v1/analyze",
    response_model=AnalyzeResponse,
    # include_exposure 缺省 / false 时 excess_dose_mm 为 None，
    # 序列化时剔除，保持原有响应结构不变
    response_model_exclude_none=True,
    # excess_dose_mm 以固定两位小数的 JSON 数字输出
    response_class=FixedDecimalJSONResponse,
)
async def analyze_samples(payload: AnalyzeRequest) -> AnalyzeResponse:
    # 字段结构 / 单条采样取值问题由 FastAPI/Pydantic 直接返回 422；
    # 重复时间戳、间隔不为 1 秒等整批级错误由 BatchValidationError 转 422。
    return analyze(payload.samples, include_exposure=payload.include_exposure)


app.include_router(router)

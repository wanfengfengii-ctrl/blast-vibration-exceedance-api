"""FastAPI 应用：爆破振速持续超限复核。"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .analysis import BatchValidationError, analyze
from .models import AnalyzeRequest, AnalyzeResponse

app = FastAPI(
    title="爆破振速复核 API",
    description="接收同一测点的振速采样，识别持续超限事件并给出放行 / 复核结论。",
    version="1.0.0",
)


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
    # 不回显输入值（可能含 NaN/Infinity 等无法 JSON 序列化的内容），只给定位与原因
    errors = [
        {"loc": list(err.get("loc", [])), "msg": err.get("msg"), "type": err.get("type")}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": "请求数据不合法", "code": "invalid_request", "errors": errors},
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/analyze", response_model=AnalyzeResponse)
async def analyze_samples(request: AnalyzeRequest) -> AnalyzeResponse:
    # 请求体字段 / 单条采样格式错误由 RequestValidationError 统一转 422；
    # 重复时间戳、间隔不为 1 秒等整批级错误由 BatchValidationError 转 422。
    return analyze(request.samples)

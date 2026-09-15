"""请求 / 响应数据模型。"""

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictStr,
    WithJsonSchema,
    field_validator,
)

# 带 Z、精确到秒的 RFC3339（date-time 主体部分，零填充）
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

# 振速合法范围（mm/s）与超限阈值
VIBRATION_MIN = 0.00
VIBRATION_MAX = 200.00
VIBRATION_THRESHOLD = 5.00

# 一个合格事件所需的最少连续超限采样数
MIN_RUN_LENGTH = 3

# 两个合格区段之间允许合并的最大低值采样间隔
MERGE_GAP = 2


class Sample(BaseModel):
    """单个测点振速采样。"""

    model_config = ConfigDict(extra="forbid")

    timestamp: StrictStr = Field(..., description="带 Z、精确到秒的 RFC3339 时间戳")
    vibration: StrictFloat = Field(..., description="振速，单位 mm/s，至多两位小数")

    @field_validator("timestamp")
    @classmethod
    def _validate_timestamp(cls, value: str) -> str:
        # 必须以 Z 结尾（不接受 +00:00 / +08:00 等偏移写法）
        if not value.endswith("Z"):
            raise ValueError("时间戳必须以 Z 结尾")
        head = value[:-1]
        if not _TIMESTAMP_RE.fullmatch(head):
            raise ValueError("时间戳须为 yyyy-MM-ddTHH:mm:ssZ 形式")
        try:
            parsed = datetime.fromisoformat(head)
        except ValueError as exc:
            raise ValueError("时间戳不是合法的 RFC3339 时间") from exc
        # 精确到秒：不允许出现秒以下的小数秒
        if parsed.microsecond != 0:
            raise ValueError("时间戳必须精确到秒")
        # 规范化为零填充标准形式，保证输出时间戳格式统一。
        # 用 isoformat 而非 strftime：部分平台（如 glibc）的 strftime
        # 对 1000 年以前的年份不零填充（"1-01-01"），会导致后续
        # strptime("%Y") 往返解析失败；isoformat 始终输出 4 位年份。
        # 已校验精确到秒，isoformat 不会带出小数秒。
        return parsed.isoformat() + "Z"

    @field_validator("vibration")
    @classmethod
    def _validate_vibration(cls, value: float) -> float:
        # 拒绝 NaN / Infinity（Pydantic 对 float 默认会接受）
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("振速必须是有限数值")
        # 至多两位小数
        if round(value, 2) != value:
            raise ValueError("振速至多保留两位小数")
        if not VIBRATION_MIN <= value <= VIBRATION_MAX:
            raise ValueError("振速超出 0.00~200.00 mm/s 范围")
        return value


class AnalyzeRequest(BaseModel):
    """一次请求提交同一测点的一批采样，可附带相邻测点的参考采样批。"""

    model_config = ConfigDict(extra="forbid")

    samples: list[Sample] = Field(..., min_length=1)
    reference_samples: list[Sample] | None = Field(
        None,
        min_length=1,
        description="相邻测点的参考采样批；提供时响应附带波形对齐结果 alignment，省略时响应结构不变",
    )
    include_exposure: StrictBool = Field(
        False,
        description="为 true 时每个事件附带累计超限量 excess_dose_mm；省略或 false 时响应结构不变",
    )


class Event(BaseModel):
    """一次持续超限事件。"""

    start: str
    end: str
    duration_seconds: int
    peak_vibration: float
    peak_timestamp: str
    # 仅在请求 include_exposure=true 时填充；为 None 时响应中不输出该字段。
    # 用 Decimal 承载定点两位小数（如 0.60），渲染层再还原为 JSON 数字字面量；
    # Pydantic 默认把 Decimal 的 JSON Schema 标为 string，这里修正为 number。
    excess_dose_mm: Annotated[Decimal, WithJsonSchema({"type": "number"})] | None = None


class Alignment(BaseModel):
    """主批与参考批的波形对齐结果。

    lag_seconds：使两批波形对齐的参考时间平移量（秒，-5 ~ 5）；
    correlation：去均值归一化互相关系数，定点六位小数，
    渲染层还原为 JSON 数字字面量（同 excess_dose_mm 的处理）；
    paired_sample_count：参与相关计算的配对采样数。
    """

    lag_seconds: int
    correlation: Annotated[Decimal, WithJsonSchema({"type": "number"})]
    paired_sample_count: int


class AnalyzeResponse(BaseModel):
    """分析结果。

    conclusion 取值：
    - "放行"：没有任何合格超限事件
    - "复核"：存在至少一个合格超限事件

    alignment 仅在请求附带 reference_samples 时填充；为 None 时响应中不输出。
    """

    conclusion: str
    event_count: int
    events: list[Event]
    alignment: Alignment | None = None

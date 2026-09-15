"""采样批校验与持续超限事件识别。"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .models import (
    MERGE_GAP,
    MIN_RUN_LENGTH,
    VIBRATION_THRESHOLD,
    Alignment,
    AnalyzeResponse,
    Event,
)

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# 累计超限量用十进制定点运算：阈值、零与两位小数精度
_DEC_THRESHOLD = Decimal("5.00")
_DEC_ZERO = Decimal("0")
_DEC_CENT = Decimal("0.01")

# 波形对齐：参考时间按 -5 ~ +5 秒逐个整数平移
MAX_LAG_SECONDS = 5
# 候选时移的交集下限：不少于 3 个配对采样，且覆盖较短序列的 80%（4/5）
MIN_PAIRED_SAMPLES = 3
_COVERAGE_NUM = 4
_COVERAGE_DEN = 5
# 互相关系数按十进制定点四舍五入至六位小数后比较、输出
_CORR_QUANTUM = Decimal("0.000001")


class BatchValidationError(ValueError):
    """整批采样不合法（重复时间戳 / 间隔不为 1 秒等），整批拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class AlignmentError(ValueError):
    """主批与参考批在 ±5 秒时移内无可对齐区段，整请求拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Point:
    ts: datetime
    timestamp: str
    vibration: float


def _to_points(samples) -> list[Point]:
    points = [
        Point(datetime.strptime(s.timestamp, TS_FORMAT), s.timestamp, s.vibration)
        for s in samples
    ]
    # 传输乱序：先按时间升序重排，后续一切校验与识别都基于重排结果
    points.sort(key=lambda p: p.ts)
    return points


def validate_series(points: list[Point]) -> None:
    """时间戳重复、相邻间隔不为 1 秒均整批拒绝，不输出部分结果。"""
    seen: set[datetime] = set()
    for p in points:
        if p.ts in seen:
            raise BatchValidationError(
                "duplicate_timestamp", f"时间戳重复：{p.timestamp}"
            )
        seen.add(p.ts)

    one_second = timedelta(seconds=1)
    for prev, cur in zip(points, points[1:]):
        if cur.ts - prev.ts != one_second:
            raise BatchValidationError(
                "non_second_interval",
                f"相邻采样间隔必须恰为 1 秒：{prev.timestamp} 与 {cur.timestamp}",
            )


def _excess_dose_mm(points: list[Point], start_i: int, end_i: int) -> Decimal:
    """累计超限量：从事件首个超限采样到末个超限采样，逐秒累加
    max(振速 − 5.00, 0) × 1 秒。

    合并区段中夹着的低值采样经 max(·, 0) 后贡献为零；全程十进制定点
    运算并固定保留两位小数（如 0.60），避免浮点累计漂移
    （如 0.10+0.20+0.30）与尾零丢失。
    """
    total = _DEC_ZERO
    for k in range(start_i, end_i + 1):
        # 振速已校验至多两位小数，格式化为两位即得精确十进制值
        vibration = Decimal(f"{points[k].vibration:.2f}")
        total += max(vibration - _DEC_THRESHOLD, _DEC_ZERO)  # × 1 秒
    return total.quantize(_DEC_CENT)


def detect_events(points: list[Point], include_exposure: bool = False) -> list[Event]:
    """识别持续超限事件。

    1. 取振速 >= 5.00 的最大连续区段，仅保留长度 >= 3 的合格区段；
    2. 相邻合格区段之间只隔 1~2 个采样时合并（中间为低值采样），
       间隔 >= 3 个采样时分开（若中间夹有不足 3 个的高值短段，间隔必然 >= 3，自然分开）；
    3. 事件起止取两端超限采样，持续秒数 = 首尾时间差 + 1 秒；
    4. 峰值取最大振速，并列时取最早时刻。
    """
    n = len(points)
    high = [p.vibration >= VIBRATION_THRESHOLD for p in points]

    # 最大连续高值区段，过滤出长度达标的区段
    qualified: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if not high[i]:
            i += 1
            continue
        j = i
        while j < n and high[j]:
            j += 1
        if j - i >= MIN_RUN_LENGTH:
            qualified.append((i, j - 1))
        i = j

    # 按间隔合并：gap 为两区段端点之间的采样个数，1~2 合并、>=3 分开
    merged: list[list[int]] = []
    for start_i, end_i in qualified:
        if not merged:
            merged.append([start_i, end_i])
        else:
            gap = start_i - merged[-1][1] - 1
            if gap <= MERGE_GAP:
                merged[-1][1] = end_i
            else:
                merged.append([start_i, end_i])

    events: list[Event] = []
    for start_i, end_i in merged:
        first, last = points[start_i], points[end_i]

        # 升序扫描 + 严格大于，峰值并列时自然保留最早时刻
        peak = first
        for k in range(start_i, end_i + 1):
            if points[k].vibration > peak.vibration:
                peak = points[k]

        duration_seconds = int((last.ts - first.ts).total_seconds()) + 1
        events.append(
            Event(
                start=first.timestamp,
                end=last.timestamp,
                duration_seconds=duration_seconds,
                peak_vibration=peak.vibration,
                peak_timestamp=peak.timestamp,
                excess_dose_mm=(
                    _excess_dose_mm(points, start_i, end_i)
                    if include_exposure
                    else None
                ),
            )
        )
    return events


def _normalized_cross_correlation(pairs: list[tuple[float, float]]) -> Decimal | None:
    """去均值归一化互相关：sum(x·y) / sqrt(sum(x²)·sum(y²))，x/y 均已去均值。

    全程十进制定点运算（振速至多两位小数，均值与乘积在默认精度内精确），
    结果四舍五入至六位小数。任一侧交集方差为零（常量波形）时返回 None。
    """
    xs = [Decimal(f"{x:.2f}") for x, _ in pairs]
    ys = [Decimal(f"{y:.2f}") for _, y in pairs]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    sxx = sum(d * d for d in dx)
    syy = sum(d * d for d in dy)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum(a * b for a, b in zip(dx, dy))
    corr = sxy / (sxx * syy).sqrt()
    quantized = corr.quantize(_CORR_QUANTUM, rounding=ROUND_HALF_UP)
    # -0.000000 规范化为 0.000000，避免输出负零
    return quantized if quantized != 0 else abs(quantized)


def align_series(points: list[Point], ref_points: list[Point]) -> Alignment:
    """把参考批时间按 -5 ~ +5 秒逐个整数平移，与主批取时间交集做波形对齐。

    时移 lag 表示「参考时间 + lag 后与主批对齐」；候选须满足交集不少于
    3 点、覆盖较短序列 80%、两侧交集方差均非零。相关系数经定点六位小数
    舍入后比较，最高者胜出；同分依次取 |lag| 较小、数值较小的时移。
    无任何合格候选时抛 AlignmentError（unalignable_series）。
    """
    ref_by_ts = {p.ts: p.vibration for p in ref_points}
    shorter = min(len(points), len(ref_points))
    best: tuple[Decimal, int, int] | None = None  # (correlation, lag, paired)
    for lag in range(-MAX_LAG_SECONDS, MAX_LAG_SECONDS + 1):
        shift = timedelta(seconds=lag)
        pairs = [
            (p.vibration, ref_by_ts[p.ts - shift])
            for p in points
            if p.ts - shift in ref_by_ts
        ]
        paired = len(pairs)
        if paired < MIN_PAIRED_SAMPLES:
            continue
        # 覆盖较短序列 80%：paired / shorter >= 4/5，整数交叉相乘避免浮点
        if paired * _COVERAGE_DEN < shorter * _COVERAGE_NUM:
            continue
        corr = _normalized_cross_correlation(pairs)
        if corr is None:  # 任一侧常量波形，丢弃候选
            continue
        # 决胜键：系数高者优先，同分依次取 |lag| 小、lag 数值小
        if best is None or (corr, -abs(lag), -lag) > (
            best[0],
            -abs(best[1]),
            -best[1],
        ):
            best = (corr, lag, paired)
    if best is None:
        raise AlignmentError(
            "unalignable_series",
            "主批与参考批在 ±5 秒时移内无可对齐区段（交集不足或波形无变化）",
        )
    corr, lag, paired = best
    return Alignment(
        lag_seconds=lag, correlation=corr, paired_sample_count=paired
    )


def analyze(
    samples, reference_samples=None, include_exposure: bool = False
) -> AnalyzeResponse:
    points = _to_points(samples)
    # 批级错误按 samples、reference_samples 的顺序报告
    validate_series(points)
    ref_points = None
    if reference_samples is not None:
        ref_points = _to_points(reference_samples)
        validate_series(ref_points)
    events = detect_events(points, include_exposure=include_exposure)
    alignment = align_series(points, ref_points) if ref_points is not None else None
    return AnalyzeResponse(
        conclusion="复核" if events else "放行",
        event_count=len(events),
        events=events,
        alignment=alignment,
    )

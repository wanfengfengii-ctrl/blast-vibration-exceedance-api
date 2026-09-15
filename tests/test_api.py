"""API 测试：本地用 TestClient，容器内用 BASE_URL 指向 api 服务做黑盒验证。"""

import os
from datetime import datetime, timedelta

import httpx
import pytest

BASE = datetime(2026, 9, 15, 10, 0, 0)
PATH = "/api/v1/analyze"


def ts_at(i: int) -> str:
    return (BASE + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")


def seq(values, shuffled: bool = False) -> dict:
    """按秒生成一批采样；shuffled 模拟传输乱序。"""
    items = [
        {"timestamp": ts_at(i), "vibration": float(v)}
        for i, v in enumerate(values)
    ]
    if shuffled:
        # 固定的逆序 + 交叉，避免随机性
        items = items[::-1]
        if len(items) > 3:
            items[0], items[1] = items[1], items[0]
    return {"samples": items}


@pytest.fixture
def post(client):
    base_url = os.environ.get("BASE_URL")

    def _post(payload):
        if base_url:
            return httpx.post(f"{base_url}{PATH}", json=payload, timeout=10)
        return client.post(PATH, json=payload)

    return _post


def test_health(get):
    resp = get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------- 阈值与持续时长 ----------

def test_three_samples_at_threshold_is_event(post):
    resp = post(seq([5.00, 5.00, 5.00]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["conclusion"] == "复核"
    assert body["event_count"] == 1
    event = body["events"][0]
    assert event["start"] == ts_at(0)
    assert event["end"] == ts_at(2)
    # 首尾时间差 2 秒 + 1 秒
    assert event["duration_seconds"] == 3
    assert event["peak_vibration"] == 5.0
    assert event["peak_timestamp"] == ts_at(0)


def test_threshold_is_inclusive_but_needs_three(post):
    # 4.99 全部低于阈值
    assert post(seq([4.99, 4.99, 4.99])).json()["conclusion"] == "放行"
    # 恰好两个 5.00 不构成事件
    assert post(seq([4.99, 5.00, 5.00])).json()["conclusion"] == "放行"
    assert post(seq([5.00, 5.00, 4.99])).json()["conclusion"] == "放行"
    # 单个高值也不构成事件
    assert post(seq([5.00])).json()["conclusion"] == "放行"


def test_duration_counts_overlimit_span_plus_one(post):
    # 两侧低值是边界，中间 5 个超限采样：00:01 ~ 00:05，持续 5 秒
    resp = post(seq([1.00, 5.00, 5.00, 5.00, 5.00, 5.00, 1.00]))
    event = resp.json()["events"][0]
    assert event["start"] == ts_at(1)
    assert event["end"] == ts_at(5)
    assert event["duration_seconds"] == 5


def test_peak_is_max_value(post):
    resp = post(seq([5.00, 7.55, 5.00]))
    event = resp.json()["events"][0]
    assert event["peak_vibration"] == 7.55
    assert event["peak_timestamp"] == ts_at(1)


def test_peak_tie_takes_earliest_timestamp(post):
    # 合并区段内两个 8.00 峰值（ts1、ts5），取最早 ts1
    resp = post(seq([5.00, 8.00, 5.00, 4.99, 5.00, 8.00, 5.00]))
    assert resp.json()["event_count"] == 1
    event = resp.json()["events"][0]
    assert event["peak_vibration"] == 8.0
    assert event["peak_timestamp"] == ts_at(1)


def test_all_clear_conclusion_release(post):
    resp = post(seq([0.00, 1.20, 4.99, 3.33]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["conclusion"] == "放行"
    assert body["event_count"] == 0
    assert body["events"] == []


# ---------- 乱序重排 ----------

def test_unsorted_samples_are_sorted_before_analysis(post):
    resp = post(seq([5.00, 5.00, 5.00, 1.00, 1.00], shuffled=True))
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    assert event["start"] == ts_at(0)
    assert event["end"] == ts_at(2)
    assert event["duration_seconds"] == 3


# ---------- 区段合并 / 分开 ----------

def test_merge_when_gap_is_one_low_sample(post):
    resp = post(seq([6.00, 6.00, 6.00, 4.99, 7.00, 7.00, 7.00]))
    body = resp.json()
    assert body["event_count"] == 1
    event = body["events"][0]
    # 起止取两端超限采样，不含中间低值
    assert event["start"] == ts_at(0)
    assert event["end"] == ts_at(6)
    assert event["duration_seconds"] == 7
    assert event["peak_vibration"] == 7.0


def test_merge_when_gap_is_two_low_samples(post):
    resp = post(seq([6.00, 6.00, 6.00, 1.00, 1.00, 7.00, 7.00, 7.00]))
    assert resp.json()["event_count"] == 1
    event = resp.json()["events"][0]
    assert event["start"] == ts_at(0)
    assert event["end"] == ts_at(7)
    assert event["duration_seconds"] == 8


def test_separate_when_gap_is_three_low_samples(post):
    resp = post(seq([6.00, 6.00, 6.00, 1.00, 1.00, 1.00, 7.00, 7.00, 7.00]))
    body = resp.json()
    assert body["event_count"] == 2
    first, second = body["events"]
    assert (first["start"], first["end"]) == (ts_at(0), ts_at(2))
    assert first["duration_seconds"] == 3
    assert (second["start"], second["end"]) == (ts_at(6), ts_at(8))
    assert second["duration_seconds"] == 3
    # 事件按开始时间升序列出
    assert first["start"] < second["start"]


def test_short_high_run_between_qualified_runs_keeps_them_separate(post):
    # 合格段(3) + 1 低值 + 不足 3 的高值段(2) + 1 低值 + 合格段(3)
    # 两个合格段端点之间隔着 4 个采样，必须分开，短段不得并入
    resp = post(seq([6, 6, 6, 1, 6, 6, 1, 6, 6, 6]))
    body = resp.json()
    assert body["event_count"] == 2
    assert body["events"][0]["end"] == ts_at(2)
    assert body["events"][1]["start"] == ts_at(7)


def test_three_events_listed_by_start_time(post):
    vals = [5, 5, 5, 1, 1, 1, 5, 5, 5, 1, 1, 1, 5, 5, 5]
    resp = post(seq(vals))
    events = resp.json()["events"]
    starts = [e["start"] for e in events]
    assert starts == sorted(starts)
    assert len(events) == 3
    assert [e["duration_seconds"] for e in events] == [3, 3, 3]


# ---------- 整批拒绝（422，无部分结果） ----------

def test_duplicate_timestamp_rejected(post):
    payload = seq([1.0, 1.0, 1.0])
    payload["samples"][2]["timestamp"] = ts_at(1)
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "duplicate_timestamp"
    assert "events" not in resp.json()


def test_duplicate_timestamp_found_after_sorting(post):
    # 乱序提交，重复时间戳（两个 ts2）不相邻，重排后仍须识别
    payload = {
        "samples": [
            {"timestamp": ts_at(2), "vibration": 1.0},
            {"timestamp": ts_at(0), "vibration": 1.0},
            {"timestamp": ts_at(2), "vibration": 1.0},
            {"timestamp": ts_at(1), "vibration": 1.0},
        ]
    }
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "duplicate_timestamp"


def test_interval_not_one_second_rejected(post):
    payload = seq([1.0, 1.0, 1.0])
    payload["samples"][2]["timestamp"] = ts_at(3)  # 0,1,3 -> 间隔 2 秒
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "non_second_interval"


def test_interval_gap_revealed_after_sorting(post):
    # 提交顺序 0,3,1，重排后 0,1,3 仍不连续
    payload = {
        "samples": [
            {"timestamp": ts_at(0), "vibration": 1.0},
            {"timestamp": ts_at(3), "vibration": 1.0},
            {"timestamp": ts_at(1), "vibration": 1.0},
        ]
    }
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "non_second_interval"


def test_non_finite_values_rejected(client):
    base_url = os.environ.get("BASE_URL")
    for token in ("NaN", "Infinity", "-Infinity"):
        raw = (
            '{"samples": ['
            '{"timestamp": "%s", "vibration": 1.0}, '
            '{"timestamp": "%s", "vibration": %s}]}'
            % (ts_at(0), ts_at(1), token)
        )
        headers = {"content-type": "application/json"}
        if base_url:
            resp = httpx.post(f"{base_url}{PATH}", content=raw, headers=headers, timeout=10)
        else:
            resp = client.post(PATH, content=raw, headers=headers)
        assert resp.status_code == 422, token


def test_value_range_boundaries(post):
    # 边界值合法
    assert post(seq([0.00])).status_code == 200
    assert post(seq([200.00])).status_code == 200
    # 越界拒绝
    assert post(seq([-0.01])).status_code == 422
    assert post(seq([200.01])).status_code == 422


def test_more_than_two_decimals_rejected(post, post_raw):
    # 5.005 数值在范围内，但超过两位小数
    assert post(seq([5.005])).status_code == 422
    # 词法层面的三位小数：解析成 float 后与 5.0 无区别，必须在 JSON 层拦截
    assert post_raw('{"samples":[{"timestamp":"%s","vibration":5.000}]}' % ts_at(0)).status_code == 422
    # 科学计数法同样不允许（5e0 数值等于 5.0）
    assert post_raw('{"samples":[{"timestamp":"%s","vibration":5e0}]}' % ts_at(0)).status_code == 422
    assert post_raw('{"samples":[{"timestamp":"%s","vibration":1.2E1}]}' % ts_at(0)).status_code == 422


def test_plain_number_forms_accepted(post, post_raw):
    # 整数、一位/两位小数均合法
    assert post(seq([5])).status_code == 200
    assert post(seq([5.0])).status_code == 200
    assert post(seq([5.00])).status_code == 200
    assert post_raw('{"samples":[{"timestamp":"%s","vibration":200}]}' % ts_at(0)).status_code == 200


def test_malformed_json_rejected(post_raw):
    assert post_raw("{not json").status_code == 422


@pytest.mark.parametrize(
    "bad_ts",
    [
        "2026-09-15T10:00:00+00:00",   # 偏移而非 Z
        "2026-09-15T10:00:00.500Z",    # 小数秒
        "2026-09-15 10:00:00Z",        # 缺 T
        "2026-09-15T10:00Z",           # 未精确到秒
        "2026-02-30T10:00:00Z",        # 不存在的日期
        "2026-13-01T10:00:00Z",        # 非法月份
        "not-a-timestamp",
    ],
)
def test_bad_timestamp_format_rejected(post, bad_ts):
    resp = post({"samples": [{"timestamp": bad_ts, "vibration": 1.0}]})
    assert resp.status_code == 422


def test_empty_batch_rejected(post):
    assert post({"samples": []}).status_code == 422


def test_missing_samples_field_rejected(post):
    assert post({}).status_code == 422


def test_extra_fields_rejected(post):
    assert post({"samples": seq([1.0])["samples"], "point": "A-01"}).status_code == 422
    payload = seq([1.0])
    payload["samples"][0]["unit"] = "mm/s"
    assert post(payload).status_code == 422


def test_invalid_batch_returns_no_partial_results(post):
    payload = seq([5.0, 5.0, 5.0, 5.0, 5.0])  # 本可产生事件
    payload["samples"][-1]["timestamp"] = ts_at(9)  # 但时间序列不合法
    resp = post(payload)
    assert resp.status_code == 422
    body = resp.json()
    assert "events" not in body
    assert "conclusion" not in body


def test_wrong_field_types_rejected(post):
    # 布尔不得被当作振速
    resp = post({"samples": [{"timestamp": ts_at(0), "vibration": True}]})
    assert resp.status_code == 422
    # 字符串数字不得被强转
    resp = post({"samples": [{"timestamp": ts_at(0), "vibration": "5.0"}]})
    assert resp.status_code == 422
    # 时间戳必须是字符串
    resp = post({"samples": [{"timestamp": 1757930400, "vibration": 5.0}]})
    assert resp.status_code == 422


# ---------- 累计超限量 include_exposure ----------

def test_exposure_omitted_by_default_matches_legacy_contract(post):
    resp = post(seq([6.00, 7.00, 5.00]))
    assert resp.status_code == 200
    body = resp.json()
    # 顶层结构与事件字段均与旧契约一致，不出现 excess_dose_mm
    assert set(body) == {"conclusion", "event_count", "events"}
    event = body["events"][0]
    assert set(event) == {
        "start", "end", "duration_seconds", "peak_vibration", "peak_timestamp",
    }


def test_exposure_omitted_when_flag_is_false(post):
    payload = seq([6.00, 7.00, 5.00])
    payload["include_exposure"] = False
    event = post(payload).json()["events"][0]
    assert "excess_dose_mm" not in event


def test_exposure_accumulates_over_continuous_event(post):
    # (0.10 + 1.20 + 2.30 + 0.00) × 1 秒 = 3.60
    payload = seq([5.10, 6.20, 7.30, 5.00])
    payload["include_exposure"] = True
    event = post(payload).json()["events"][0]
    assert event["excess_dose_mm"] == 3.6


def test_exposure_no_float_accumulation_drift(post):
    # 浮点累加 0.10+0.20+0.30 会得到 0.5999999999999996，定点运算必须为 0.60
    payload = seq([5.10, 5.20, 5.30])
    payload["include_exposure"] = True
    resp = post(payload)
    assert resp.json()["events"][0]["excess_dose_mm"] == 0.6
    # 原始 JSON 文本固定两位小数，且是数字而非字符串
    assert '"excess_dose_mm":0.60' in resp.text


def test_exposure_rendered_as_number_with_two_decimal_places(post):
    # 3.60 / 9.00 / 7.10 等尾零不得丢失；字段仍为 JSON 数字
    cases = [
        ([5.10, 6.20, 7.30, 5.00], "3.60"),
        ([6.00, 6.00, 6.00, 4.99, 7.00, 7.00, 7.00], "9.00"),
        ([5.00, 7.00, 5.10, 4.99, 6.20, 8.80, 5.00], "7.10"),
        ([5.00, 5.00, 5.00], "0.00"),
    ]
    for values, expected in cases:
        payload = seq(values)
        payload["include_exposure"] = True
        resp = post(payload)
        assert resp.status_code == 200, values
        assert f'"excess_dose_mm":{expected}' in resp.text, values
        assert '"excess_dose_mm":"' not in resp.text  # 不得带引号退化为字符串
        event = resp.json()["events"][0]
        assert isinstance(event["excess_dose_mm"], (int, float))
        assert event["excess_dose_mm"] == float(expected)


def test_exposure_merged_event_with_one_low_gap_counts_only_overlimit(post):
    # 1 个低值间隔的合并事件：低值采样贡献为零，1.00×3 + 2.00×3 = 9.00
    payload = seq([6.00, 6.00, 6.00, 4.99, 7.00, 7.00, 7.00])
    payload["include_exposure"] = True
    body = post(payload).json()
    assert body["event_count"] == 1
    assert body["events"][0]["excess_dose_mm"] == 9.0


def test_exposure_merged_event_with_two_low_gaps_counts_only_overlimit(post):
    # 2 个低值间隔的合并事件：同样只累计超限采样
    payload = seq([6.00, 6.00, 6.00, 1.00, 1.00, 7.00, 7.00, 7.00])
    payload["include_exposure"] = True
    body = post(payload).json()
    assert body["event_count"] == 1
    assert body["events"][0]["excess_dose_mm"] == 9.0


def test_exposure_present_for_every_event_when_enabled(post):
    payload = seq([5.50, 5.50, 5.50, 1.00, 1.00, 1.00, 6.00, 6.00, 6.00])
    payload["include_exposure"] = True
    events = post(payload).json()["events"]
    assert len(events) == 2
    assert [e["excess_dose_mm"] for e in events] == [1.5, 3.0]


def test_exposure_with_unsorted_samples(post):
    # 乱序重排后累计：0.00 + 3.00 + 0.00 = 3.00
    payload = seq([5.00, 8.00, 5.00, 1.00, 1.00], shuffled=True)
    payload["include_exposure"] = True
    event = post(payload).json()["events"][0]
    assert event["start"] == ts_at(0)
    assert event["end"] == ts_at(2)
    assert event["excess_dose_mm"] == 3.0


def test_exposure_flag_does_not_change_existing_fields(post):
    base = seq([6.00, 6.00, 6.00, 4.99, 7.00, 7.00, 7.00])
    legacy = post(base).json()
    exposed = post(dict(base, include_exposure=True)).json()
    assert legacy["conclusion"] == exposed["conclusion"]
    assert legacy["event_count"] == exposed["event_count"]
    for old, new in zip(legacy["events"], exposed["events"]):
        for key in ("start", "end", "duration_seconds",
                    "peak_vibration", "peak_timestamp"):
            assert old[key] == new[key]


def test_exposure_with_no_events_keeps_release_conclusion(post):
    payload = seq([1.00, 2.00, 4.99])
    payload["include_exposure"] = True
    body = post(payload).json()
    assert body == {"conclusion": "放行", "event_count": 0, "events": []}


def test_non_boolean_exposure_flag_rejected_batch(post):
    # 开关类型错误按现有请求校验整批拒绝，不产生事件结果
    for bad in ("true", "false", 1, 0):
        payload = seq([6.00, 6.00, 6.00])  # 本可产生事件
        payload["include_exposure"] = bad
        resp = post(payload)
        assert resp.status_code == 422, bad
        body = resp.json()
        assert "events" not in body
        assert "conclusion" not in body


# ---------- 参考批波形对齐 reference_samples / alignment ----------

# 无周期性的类爆破波形：快速爬升后衰减，错位平移时不会意外高相关
WAVE = [0.50, 1.20, 8.60, 6.10, 4.20, 2.80, 1.90, 1.10, 0.90, 0.60]


def ref_seq(values, start: int = 0, shuffled: bool = False) -> list:
    """生成参考批采样：从 BASE + start 秒起按秒递增；shuffled 模拟乱序。"""
    items = [
        {"timestamp": ts_at(start + i), "vibration": float(v)}
        for i, v in enumerate(values)
    ]
    if shuffled:
        items = items[::-1]
        if len(items) > 3:
            items[0], items[1] = items[1], items[0]
    return items


def test_alignment_positive_lag(post):
    # 参考批同一波形早 3 秒出现：参考时间 +3 秒后与主批完全对齐
    payload = seq(WAVE)
    payload["reference_samples"] = ref_seq(WAVE, start=-3)
    resp = post(payload)
    assert resp.status_code == 200
    alignment = resp.json()["alignment"]
    assert alignment["lag_seconds"] == 3
    assert alignment["correlation"] == 1.0
    assert alignment["paired_sample_count"] == 10
    # 定点六位小数的 JSON 数字，而非字符串
    assert '"correlation":1.000000' in resp.text


def test_alignment_negative_lag(post):
    # 参考批同一波形晚 2 秒出现：参考时间 -2 秒后对齐
    payload = seq(WAVE)
    payload["reference_samples"] = ref_seq(WAVE, start=2)
    resp = post(payload)
    assert resp.status_code == 200
    alignment = resp.json()["alignment"]
    assert alignment["lag_seconds"] == -2
    assert alignment["correlation"] == 1.0
    assert alignment["paired_sample_count"] == 10


def test_alignment_deterministic_with_shuffled_input(post):
    # 两批均乱序提交，重排后结果与顺序提交一致
    shuffled = seq(WAVE, shuffled=True)
    shuffled["reference_samples"] = ref_seq(WAVE, start=-3, shuffled=True)
    ordered = seq(WAVE)
    ordered["reference_samples"] = ref_seq(WAVE, start=-3)
    assert post(shuffled).json()["alignment"] == post(ordered).json()["alignment"]


def test_alignment_omitted_or_null_leaves_response_unchanged(post):
    # 省略 reference_samples：不出现 alignment，结构与旧契约一致
    body = post(seq(WAVE)).json()
    assert set(body) == {"conclusion", "event_count", "events"}
    # 显式 null 视同省略
    payload = seq(WAVE)
    payload["reference_samples"] = None
    assert "alignment" not in post(payload).json()


def test_alignment_unalignable_when_no_overlap(post):
    # 参考批在 ±5 秒时移内与主批毫无交集
    payload = seq(WAVE)
    payload["reference_samples"] = ref_seq(WAVE, start=20)
    resp = post(payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "unalignable_series"
    # 不输出任何部分结果
    assert "events" not in body
    assert "conclusion" not in body
    assert "alignment" not in body


def test_alignment_unalignable_when_overlap_below_80_percent(post):
    # 错位 8 秒：最大交集（lag=-5）为 7 点，不足较短序列 10 点的 80%
    payload = seq(WAVE)
    payload["reference_samples"] = ref_seq(WAVE, start=8)
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "unalignable_series"


def test_alignment_exactly_80_percent_coverage_accepted(post):
    # 交集 8 点恰好覆盖较短序列 10 点的 80%：达标，lag=0 完全线性相关胜出
    payload = seq(WAVE)
    # 参考批 t=2..11：前 8 点为主批同时刻的线性变换 y=2x+1，末 2 点无关
    payload["reference_samples"] = ref_seq(
        [18.20, 13.20, 9.40, 6.60, 4.80, 3.20, 2.80, 2.20, 0.50, 0.60], start=2
    )
    resp = post(payload)
    assert resp.status_code == 200
    alignment = resp.json()["alignment"]
    assert alignment["lag_seconds"] == 0
    assert alignment["correlation"] == 1.0
    assert alignment["paired_sample_count"] == 8


def test_alignment_unalignable_when_intersection_too_small(post):
    # 错位 9 秒：lag=-5 时交集仅 1 点，不足 3 点下限
    payload = seq(WAVE[:5])
    payload["reference_samples"] = ref_seq(WAVE[:5], start=9)
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "unalignable_series"


def test_alignment_constant_waveform_rejected(post):
    # 主批常量：交集方差为零，所有候选被丢弃
    payload = seq([5.00, 5.00, 5.00, 5.00, 5.00])
    payload["reference_samples"] = ref_seq([1.00, 2.00, 3.00, 4.00, 5.00])
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "unalignable_series"
    # 参考批常量亦然
    payload = seq([1.00, 2.00, 3.00, 4.00, 5.00])
    payload["reference_samples"] = ref_seq([5.00, 5.00, 5.00, 5.00, 5.00])
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "unalignable_series"


def test_alignment_tie_prefers_smaller_absolute_lag(post):
    # 主批等差序列、参考批为其线性变换且晚 2 秒：
    # lag=-2（9 对）与 lag=-1（8 对）的相关系数均为 1.000000，
    # 同分时 |lag| 较小者胜出
    payload = seq([1, 2, 3, 4, 5, 6, 7, 8, 9])
    payload["reference_samples"] = ref_seq(
        [3, 5, 7, 9, 11, 13, 15, 17, 19], start=2
    )
    alignment = post(payload).json()["alignment"]
    assert alignment["lag_seconds"] == -1
    assert alignment["correlation"] == 1.0
    assert alignment["paired_sample_count"] == 8


def test_alignment_tie_prefers_smaller_lag_value(post):
    # 周期 2 波形：lag=-1 与 lag=+1 相关系数同为 1.000000、|lag| 相同，
    # 数值较小的时移胜出
    payload = seq([3, 7, 3, 7, 3, 7, 3, 7, 3, 7])
    payload["reference_samples"] = ref_seq([15, 7, 15, 7, 15, 7, 15, 7, 15, 7])
    alignment = post(payload).json()["alignment"]
    assert alignment["lag_seconds"] == -1
    assert alignment["correlation"] == 1.0
    assert alignment["paired_sample_count"] == 9


def test_alignment_correlation_rounded_to_six_decimal_places(post):
    # corr = 1/sqrt(4/3) ≈ 0.8660254，定点四舍五入至六位小数
    payload = seq([1.00, 2.00, 3.00])
    payload["reference_samples"] = ref_seq([1.00, 1.00, 2.00])
    resp = post(payload)
    assert resp.status_code == 200
    alignment = resp.json()["alignment"]
    assert alignment["lag_seconds"] == 0
    assert alignment["paired_sample_count"] == 3
    assert alignment["correlation"] == 0.866025
    assert '"correlation":0.866025' in resp.text
    assert '"correlation":"' not in resp.text  # 不得带引号退化为字符串


def test_alignment_negative_correlation_rendered(post):
    # 完全负相关：-1.000000 同样以六位小数 JSON 数字输出
    payload = seq([1.00, 2.00, 3.00, 4.00, 5.00])
    payload["reference_samples"] = ref_seq([5.00, 4.00, 3.00, 2.00, 1.00])
    resp = post(payload)
    assert resp.status_code == 200
    alignment = resp.json()["alignment"]
    assert alignment["correlation"] == -1.0
    assert '"correlation":-1.000000' in resp.text


def test_alignment_does_not_change_events_or_exposure(post):
    # 附带参考批时，事件边界、累计超限量与原响应结构均保持不变
    values = [6.00, 6.00, 6.00, 4.99, 7.00, 7.00, 7.00]
    legacy = post(dict(seq(values), include_exposure=True)).json()
    with_ref = dict(seq(values), include_exposure=True)
    with_ref["reference_samples"] = ref_seq(WAVE[:7], start=-2)
    resp = post(with_ref)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conclusion"] == legacy["conclusion"]
    assert body["event_count"] == legacy["event_count"]
    assert body["events"] == legacy["events"]  # 含 excess_dose_mm
    assert set(body["alignment"]) == {
        "lag_seconds", "correlation", "paired_sample_count",
    }
    assert -5 <= body["alignment"]["lag_seconds"] <= 5


# ---------- 参考批自身的整批校验 ----------

def test_reference_duplicate_timestamp_rejected(post):
    payload = seq([1.0, 1.0, 1.0])
    ref = ref_seq([1.0, 1.0, 1.0])
    ref[2]["timestamp"] = ts_at(1)  # 参考批内重复
    payload["reference_samples"] = ref
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "duplicate_timestamp"


def test_reference_non_second_interval_rejected(post):
    payload = seq([1.0, 1.0, 1.0])
    ref = ref_seq([1.0, 1.0, 1.0])
    ref[2]["timestamp"] = ts_at(5)  # 0,1,5 -> 间隔不为 1 秒
    payload["reference_samples"] = ref
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "non_second_interval"


def test_primary_batch_error_reported_before_reference(post):
    # 主批重复、参考批间隔错误：批级错误按 samples、reference_samples 顺序报告
    payload = seq([1.0, 1.0, 1.0])
    payload["samples"][2]["timestamp"] = ts_at(0)  # 主批重复
    ref = ref_seq([1.0, 1.0, 1.0])
    ref[2]["timestamp"] = ts_at(5)  # 参考批间隔错误
    payload["reference_samples"] = ref
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "duplicate_timestamp"


def test_reference_number_lexical_rules_enforced(post_raw):
    # 参考批沿用同一套数字词法：三位小数 / 科学计数法同样拒绝
    for bad in ("5.000", "5e0"):
        raw = (
            '{"samples":[{"timestamp":"%s","vibration":1.0}],'
            '"reference_samples":[{"timestamp":"%s","vibration":%s}]}'
            % (ts_at(0), ts_at(0), bad)
        )
        resp = post_raw(raw)
        assert resp.status_code == 422, bad
        assert resp.json()["code"] == "invalid_number_format"


def test_reference_sample_value_range_enforced(post):
    payload = seq([1.0])
    payload["reference_samples"] = [{"timestamp": ts_at(0), "vibration": 200.01}]
    assert post(payload).status_code == 422


def test_reference_empty_batch_rejected(post):
    payload = seq([1.0])
    payload["reference_samples"] = []
    assert post(payload).status_code == 422


# ---------- datetime 边界（9999 年末 / 0001 年初） ----------

def seq_at(values, base: datetime, start: int = 0) -> list:
    """从 base + start 秒起按秒生成采样；isoformat 保证年份始终零填充。"""
    return [
        {
            "timestamp": (base + timedelta(seconds=start + i)).isoformat() + "Z",
            "vibration": float(v),
        }
        for i, v in enumerate(values)
    ]


def test_alignment_near_datetime_max(post):
    # 9999 年末：lag 为负时参考键换算不得溢出 datetime 上限（曾 500）
    end = datetime(9999, 12, 31, 23, 59, 50)  # 10 个采样正好到 23:59:59
    payload = {
        "samples": seq_at(WAVE, end),
        "reference_samples": seq_at(WAVE, end, start=-3),  # 早 3 秒
    }
    resp = post(payload)
    assert resp.status_code == 200
    alignment = resp.json()["alignment"]
    assert alignment["lag_seconds"] == 3
    assert alignment["correlation"] == 1.0
    assert alignment["paired_sample_count"] == 10


def test_alignment_near_datetime_min(post):
    # 0001 年初：lag 为正时参考键换算不得溢出 datetime 下限（曾 500）
    start = datetime(1, 1, 1, 0, 0, 0)
    payload = {
        "samples": seq_at(WAVE, start),
        "reference_samples": seq_at(WAVE, start, start=2),  # 晚 2 秒
    }
    resp = post(payload)
    assert resp.status_code == 200
    alignment = resp.json()["alignment"]
    assert alignment["lag_seconds"] == -2
    assert alignment["correlation"] == 1.0
    assert alignment["paired_sample_count"] == 10


def test_event_at_year_one_timestamps_zero_padded(post):
    # 0001 年的时间戳规范化后仍为 4 位零填充，事件识别与输出正常
    resp = post({"samples": seq_at([5.00, 8.00, 5.00], datetime(1, 1, 1))})
    assert resp.status_code == 200
    event = resp.json()["events"][0]
    assert event["start"] == "0001-01-01T00:00:00Z"
    assert event["end"] == "0001-01-01T00:00:02Z"
    assert event["duration_seconds"] == 3


def test_unalignable_near_datetime_max_returns_422_not_500(post):
    # 边界附近不可对齐时仍走正常 422，而非服务器错误
    end = datetime(9999, 12, 31, 23, 59, 50)
    payload = {
        "samples": seq_at(WAVE, end),
        "reference_samples": seq_at(WAVE, datetime(9999, 12, 30, 0, 0, 0)),
    }
    resp = post(payload)
    assert resp.status_code == 422
    assert resp.json()["code"] == "unalignable_series"

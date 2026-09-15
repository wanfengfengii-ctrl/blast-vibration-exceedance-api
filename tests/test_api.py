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

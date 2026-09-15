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


def test_more_than_two_decimals_rejected(post):
    # 5.005 数值在范围内，但超过两位小数
    assert post(seq([5.005])).status_code == 422
    # 字符串形式同样拒绝
    resp = post(
        {"samples": [{"timestamp": ts_at(0), "vibration": 5.001}]}
    )
    assert resp.status_code == 422


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

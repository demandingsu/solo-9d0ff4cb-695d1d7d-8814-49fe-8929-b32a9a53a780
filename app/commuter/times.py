"""核心时刻逻辑：跨日时刻解析、服务日判定、末班配对。

该模块不依赖网络，方便单元测试。时刻表抓取由 :mod:`commuter.scraper` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

# 服务日类型
WEEKDAY = "weekday"   # 工作日（周一至周五始发）
WEEKEND = "weekend"   # 周末（周六、周日始发）
DAILY = "daily"       # 每日始发
SERVICE_KINDS = (WEEKDAY, WEEKEND, DAILY)
SERVICE_LABELS = {
    WEEKDAY: "工作日",
    WEEKEND: "周末",
    DAILY: "每日",
}

# 允许 24:xx / 25:xx … 形式的跨日时刻，上限 47:59（最多跨两天）
_TIME_RE = __import__("re").compile(r"^\s*(\d{1,2}):([0-5]\d)\s*$")


def parse_clock(value: str) -> timedelta | None:
    """把 ``HH:MM`` 解析为距服务日零点的时间差。

    支持 24:00 之后的跨日时刻（例如 ``25:10`` 表示次日 01:10）。
    无法解析时返回 ``None``，由调用方记为缺少关键时刻。
    """
    if value is None:
        return None
    match = _TIME_RE.match(str(value))
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 47:
        return None
    return timedelta(hours=hour, minutes=minute)


def service_kind_for_day(day: date) -> str:
    """按始发服务日是星期几返回工作日/周末类型。"""
    return WEEKEND if day.weekday() >= 5 else WEEKDAY


def service_runs_on(service_kind: str, service_day: date) -> bool:
    """判断某类班次是否在给定始发服务日运行。"""
    if service_kind == DAILY:
        return True
    return service_kind == service_kind_for_day(service_day)


@dataclass
class StopRecord:
    """某车次在某站的一条停站记录（由分页表格解析得到）。"""

    trip_no: str
    stop_seq: int | None
    station: str
    clock: str          # 原始时刻文本，如 "24:30"
    service_kind: str   # weekday / weekend / daily

    def clock_delta(self) -> timedelta | None:
        return parse_clock(self.clock)


@dataclass
class TripOption:
    """核定通过的一个可选班次（某车次 + 某个始发服务日）。"""

    trip_no: str
    service_kind: str
    service_day: date
    board_seq: int
    alight_seq: int
    depart_dt: datetime          # 实际发车时间（含跨日换算）
    arrive_dt: datetime          # 实际到站时间（含跨日换算）
    depart_clock: str
    arrive_clock: str


@dataclass
class Determination:
    """末班核定结果。"""

    latest: TripOption | None = None
    options: list[TripOption] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # 同一服务日内同号记录无法唯一配对的车次号；非空时整次查询不得给出确定末班
    ambiguous: list[str] = field(default_factory=list)


def _station_key(name: str) -> str:
    return "".join(str(name).split())


def determine_last_trip(
    board_records: Iterable[StopRecord],
    alight_records: Iterable[StopRecord],
    board_station: str,
    alight_station: str,
    earliest_depart: datetime,
    latest_arrive: datetime,
) -> Determination:
    """按车次号与停站顺序配对两站记录，核定可乘坐的最晚一班。

    两站时刻表都可能跨多个分页，同一车次号在同一站内会出现多条记录
    （工作日 / 周末各一条，且两站分页的行顺序可能不同）。本函数保留
    同号全部停站记录，按 **车次号 + 始发服务日类型** 分组配对，结论
    不依赖任何分页行顺序：

      1. 同一服务日组内，上车站记录与下车站记录都必须恰好只有一条，
         且上车站停站顺序早于下车站，才算唯一配对；任一侧出现多条
         同号同服务日记录即视为有歧义，该服务日组不予核定；
      2. 两站均有可解析的关键时刻，两站服务日类型按分组天然一致；
      3. 在“最早出门 ~ 到岗截止”覆盖的始发服务日内，该班次当天运行；
      4. 实际发车不早于最早出门、实际到站不晚于到岗截止。

    只要任一车次在任一服务日组无法唯一配对（``ambiguous`` 非空），
    整次查询返回 ``latest=None``，由调用方展示“无法核定”并指出有
    歧义的车次，不得给出确定末班。

    24:xx 跨日时刻按 ``始发服务日零点 + 时间差`` 换算成真实日期时间。
    """
    result = Determination()
    board_key, alight_key = _station_key(board_station), _station_key(alight_station)

    # 保留同号全部停站记录，按 (车次号, 服务日类型) 分组。
    # 分页重复抓取产生的完全相同行先去重，避免把重复行误判成歧义。
    board_groups: dict[tuple[str, str], list[StopRecord]] = {}
    alight_groups: dict[tuple[str, str], list[StopRecord]] = {}
    board_seen: set[tuple] = set()
    alight_seen: set[tuple] = set()

    def _dedup_key(rec: StopRecord) -> tuple:
        return (
            rec.trip_no,
            rec.stop_seq,
            _station_key(rec.station),
            (rec.clock or "").strip(),
            rec.service_kind,
        )

    for rec in board_records:
        if _station_key(rec.station) != board_key:
            continue
        key = _dedup_key(rec)
        if key in board_seen:
            continue
        board_seen.add(key)
        board_groups.setdefault((rec.trip_no, rec.service_kind), []).append(rec)
    for rec in alight_records:
        if _station_key(rec.station) != alight_key:
            continue
        key = _dedup_key(rec)
        if key in alight_seen:
            continue
        alight_seen.add(key)
        alight_groups.setdefault((rec.trip_no, rec.service_kind), []).append(rec)

    board_trips = {trip_no for trip_no, _ in board_groups}
    alight_trips = {trip_no for trip_no, _ in alight_groups}
    if not board_trips:
        result.problems.append(f"上车站「{board_station}」没有解析到任何车次记录。")
    if not alight_trips:
        result.problems.append(f"下车站「{alight_station}」没有解析到任何车次记录。")

    # 只在某一侧出现的车次，无法确认能否到岗，需提示数据不全
    for trip_no in sorted(board_trips - alight_trips):
        result.problems.append(
            f"车次 {trip_no} 在「{alight_station}」站时刻表中没有记录，无法核定该班次。"
        )
    for trip_no in sorted(alight_trips - board_trips):
        result.problems.append(
            f"车次 {trip_no} 在「{board_station}」站时刻表中没有记录，无法核定该班次。"
        )

    # 从最早出门当天到截止当天都可能是始发服务日（跨日车次的零点在该区间内）
    first_day = earliest_depart.date()
    last_day = latest_arrive.date()
    candidate_days = [
        first_day + timedelta(days=i)
        for i in range((last_day - first_day).days + 1)
    ]

    # 通过结构校验的配对规格，歧义判定全部结束后再展开成班次选项
    pair_specs: list[tuple[str, str, StopRecord, StopRecord]] = []

    # 分组键排序保证配对过程与输入（分页）顺序完全无关，结论可复现
    for group_key in sorted(set(board_groups) & set(alight_groups)):
        trip_no, service_kind = group_key
        label = SERVICE_LABELS.get(service_kind, service_kind)
        b_recs = board_groups[group_key]
        a_recs = alight_groups[group_key]

        # 同一服务日同号记录必须两侧都唯一，否则无法确定哪两条属于同一行程
        if len(b_recs) > 1 or len(a_recs) > 1:
            result.ambiguous.append(trip_no)
            result.problems.append(
                f"车次 {trip_no} 的{label}服务日记录在两站分页中存在 "
                f"{len(b_recs)} 条「{board_station}」、{len(a_recs)} 条"
                f"「{alight_station}」停站记录，无法唯一配对，该{label}班次不予核定。"
            )
            continue

        b_rec, a_rec = b_recs[0], a_recs[0]

        if b_rec.stop_seq is None or a_rec.stop_seq is None:
            result.problems.append(
                f"车次 {trip_no}（{label}）缺少停站顺序，无法确认上下车先后。"
            )
            continue
        if b_rec.stop_seq >= a_rec.stop_seq:
            result.problems.append(
                f"车次 {trip_no}（{label}）的停站顺序不满足"
                f"「{board_station}」先于「{alight_station}」。"
            )
            continue

        b_delta, a_delta = b_rec.clock_delta(), a_rec.clock_delta()
        missing = []
        if b_delta is None:
            missing.append(f"「{board_station}」发车时刻")
        if a_delta is None:
            missing.append(f"「{alight_station}」到站时刻")
        if missing:
            result.problems.append(
                f"车次 {trip_no}（{label}）缺少关键时刻（{'、'.join(missing)}），无法核定。"
            )
            continue

        # 停站顺序在下、时刻却更早（如 23:50 发车、01:00 到站且未用 25:00 书写），
        # 说明到站落在次日，补 24 小时；补完仍早于发车则时刻本身矛盾。
        if a_delta < b_delta:
            a_delta += timedelta(hours=24)
        if a_delta < b_delta:
            result.problems.append(
                f"车次 {trip_no}（{label}）两站时刻矛盾"
                f"（到站早于发车且无法按跨日解释），无法核定。"
            )
            continue

        pair_specs.append((trip_no, service_kind, b_rec, a_rec))

    # 存在任何无法唯一配对的同号记录：整次查询不给出确定末班
    if result.ambiguous:
        result.ambiguous = sorted(set(result.ambiguous))
        result.problems.append(
            "车次 " + "、".join(result.ambiguous)
            + " 的同号停站记录无法唯一配对，本次查询无法核定末班，"
            "请勿采信任何确定末班结论。"
        )
        result.options = []
        result.latest = None
        return result

    for trip_no, service_kind, b_rec, a_rec in pair_specs:
        b_delta, a_delta = b_rec.clock_delta(), a_rec.clock_delta()
        if a_delta < b_delta:
            a_delta += timedelta(hours=24)
        for service_day in candidate_days:
            if not service_runs_on(service_kind, service_day):
                continue
            base = datetime.combine(service_day, datetime.min.time())
            depart_dt = base + b_delta
            arrive_dt = base + a_delta
            if depart_dt < earliest_depart or arrive_dt > latest_arrive:
                continue
            result.options.append(
                TripOption(
                    trip_no=trip_no,
                    service_kind=service_kind,
                    service_day=service_day,
                    board_seq=b_rec.stop_seq,
                    alight_seq=a_rec.stop_seq,
                    depart_dt=depart_dt,
                    arrive_dt=arrive_dt,
                    depart_clock=b_rec.clock.strip(),
                    arrive_clock=a_rec.clock.strip(),
                )
            )

    result.options.sort(key=lambda opt: opt.depart_dt, reverse=True)
    result.latest = result.options[0] if result.options else None
    return result

"""快照结构、迁移与完整性检查。"""

from __future__ import annotations

from typing import Any

from ..domain.date_precision import (
    DATE_PRECISIONS,
    observed_date_from_entry,
)
from ..domain.stages import STAGE_BY_KEY
from ..errors import DomainError


CURRENT_SCHEMA_VERSION = 1


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "revision": 0,
        "plots": {},
        "trees": {},
        "observations": {},
        "comparisons": {},
        "briefs": {},
        "events": [],
    }


def ensure_state_shape(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise DomainError(
            "state_corrupt",
            "数据快照顶层不是对象",
            500,
        )
    expected_containers = (
        "plots",
        "trees",
        "observations",
        "comparisons",
        "briefs",
        "events",
    )
    if state.get("schema_version") != CURRENT_SCHEMA_VERSION:
        raise DomainError(
            "state_schema_unsupported",
            "数据快照版本不受支持",
            500,
            {"actual": state.get("schema_version")},
        )
    revision = state.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise DomainError("state_corrupt", "数据快照修订号无效", 500)
    for key in expected_containers:
        value = state.get(key)
        if key == "events":
            if not isinstance(value, list):
                raise DomainError("state_corrupt", f"{key} 必须是数组", 500)
            continue
        if not isinstance(value, dict):
            raise DomainError("state_corrupt", f"{key} 必须是对象", 500)
        for identifier, record in value.items():
            if not isinstance(identifier, str) or not isinstance(record, dict):
                raise DomainError("state_corrupt", f"{key} 中存在非法记录", 500)
            if record.get("id") != identifier:
                raise DomainError(
                    "state_corrupt",
                    f"{key}.{identifier} 的标识不一致",
                    500,
                )
            if key == "observations":
                _check_observation_record(record)
            elif key == "comparisons":
                _check_comparison_record(record)
    return state


def _check_observation_record(record: dict[str, Any]) -> None:
    """恢复后验证阶段条目的日期精度没有被截断或破坏。"""

    entries = record.get("entries")
    if not isinstance(entries, list):
        raise DomainError("state_corrupt", "季节志条目不是数组", 500)
    for entry in entries:
        if not isinstance(entry, dict):
            raise DomainError("state_corrupt", "季节志条目不是对象", 500)
        stage = entry.get("stage")
        if stage not in STAGE_BY_KEY:
            raise DomainError(
                "state_corrupt",
                f"季节志 {record.get('id')} 含未知阶段 {stage!r}",
                500,
            )
        precision = entry.get("precision", "day")
        # 历史条目缺省 precision，按单日接受；其它值必须在精度表内。
        if precision not in DATE_PRECISIONS:
            raise DomainError(
                "state_corrupt",
                f"阶段 {stage} 的日期精度不受支持：{precision!r}",
                500,
            )
        if not isinstance(entry.get("observed_on"), str):
            raise DomainError(
                "state_corrupt",
                f"阶段 {stage} 缺少观察日期",
                500,
            )
        if precision == "range":
            if not isinstance(entry.get("observed_end_on"), str):
                raise DomainError(
                    "state_corrupt",
                    f"区间阶段 {stage} 缺少结束日期",
                    500,
                )
        elif entry.get("observed_end_on") is not None:
            raise DomainError(
                "state_corrupt",
                f"非区间阶段 {stage} 残留了结束日期",
                500,
            )
        # 解析区间结构（日期格式、起止先后、跨字段一致性）。
        observed_date_from_entry(entry)


def _check_comparison_record(record: dict[str, Any]) -> None:
    """恢复后验证比较结果仍带着不确定范围，而不是被压成虚构精确值。"""

    rows = record.get("stage_offsets")
    if not isinstance(rows, list):
        raise DomainError("state_corrupt", "对比图谱阶段结果不是数组", 500)
    for row in rows:
        if not isinstance(row, dict):
            raise DomainError("state_corrupt", "对比阶段结果不是对象", 500)
        stage = row.get("stage")
        if stage not in STAGE_BY_KEY:
            raise DomainError(
                "state_corrupt",
                f"对比图谱 {record.get('id')} 含未知阶段 {stage!r}",
                500,
            )
        has_min = "offset_min_days" in row
        has_max = "offset_max_days" in row
        has_exact = "offset_exact" in row
        if not (has_min and has_max and has_exact):
            # 历史比较结果只有 offset_days：三个新字段必须整体缺失，
            # 按精确偏移的旧记录接受；混合形态属于损坏。
            if has_min or has_max or has_exact:
                raise DomainError(
                    "state_corrupt",
                    f"对比阶段 {stage} 的偏移范围字段不完整",
                    500,
                )
            legacy_offset = row.get("offset_days")
            if not (
                isinstance(legacy_offset, int) and not isinstance(legacy_offset, bool)
            ):
                raise DomainError(
                    "state_corrupt",
                    f"对比阶段 {stage} 缺少有效的偏移范围",
                    500,
                )
            continue

        offset_min = row["offset_min_days"]
        offset_max = row["offset_max_days"]
        offset_exact = row["offset_exact"]
        if not isinstance(offset_exact, bool):
            raise DomainError(
                "state_corrupt",
                f"对比阶段 {stage} 的精确标记必须是布尔值",
                500,
            )
        # 上下界均空表示两边同方向开放（都“不早于”或都“不晚于”），
        # 是合法的“方向待定”不确定行，不能当作损坏。
        for bound in (offset_min, offset_max):
            if bound is not None and (
                isinstance(bound, bool) or not isinstance(bound, int)
            ):
                raise DomainError(
                    "state_corrupt",
                    f"对比阶段 {stage} 的偏移边界必须是整数或空",
                    500,
                )
        if offset_min is not None and offset_max is not None:
            if offset_min > offset_max:
                raise DomainError(
                    "state_corrupt",
                    f"对比阶段 {stage} 的偏移下确界大于上确界",
                    500,
                )
        if offset_exact:
            # 精确行必须给出确定且相等的上下界；不确定行不得携带虚构精确值。
            if offset_min != offset_max or offset_min is None:
                raise DomainError(
                    "state_corrupt",
                    f"对比阶段 {stage} 标记为精确但偏移范围不一致",
                    500,
                )
            exact_offset = row.get("offset_days")
            if not (
                isinstance(exact_offset, int) and not isinstance(exact_offset, bool)
            ) or exact_offset != offset_min:
                raise DomainError(
                    "state_corrupt",
                    f"对比阶段 {stage} 的精确偏移与范围不一致",
                    500,
                )
        elif isinstance(row.get("offset_days"), int) and not isinstance(
            row.get("offset_days"), bool
        ):
            raise DomainError(
                "state_corrupt",
                f"对比阶段 {stage} 是不确定偏移却携带虚构的精确天数",
                500,
            )


def check_relationships(state: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for tree in state["trees"].values():
        if tree["plot_id"] not in state["plots"]:
            problems.append(f"植株 {tree['id']} 引用了不存在的园区")
    for observation in state["observations"].values():
        if observation["tree_id"] not in state["trees"]:
            problems.append(f"季节志 {observation['id']} 引用了不存在的植株")
        if observation["plot_id"] not in state["plots"]:
            problems.append(f"季节志 {observation['id']} 引用了不存在的园区")
    for comparison in state["comparisons"].values():
        if comparison["left_observation_id"] not in state["observations"]:
            problems.append(f"对比图谱 {comparison['id']} 缺少左侧季节志")
        if comparison["right_observation_id"] not in state["observations"]:
            problems.append(f"对比图谱 {comparison['id']} 缺少右侧季节志")
    for brief in state["briefs"].values():
        if brief["plot"]["id"] not in state["plots"]:
            problems.append(f"简报 {brief['id']} 引用了不存在的园区")
    return problems

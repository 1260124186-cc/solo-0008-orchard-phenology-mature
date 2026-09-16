"""物候观察日期的精度模型。

观察不一定能落到单日：观察员可能只记得某个区间，或“不晚于 / 不早于”
的边界。系统因此保留四种精度，并在后续顺序、季节窗口与对齐比较中始终
携带不确定范围，而不是虚构一个精确日期：

- ``day``：单日，``observed_on`` 即当日，区间两端相同；
- ``range``：闭区间，``observed_on`` 为最早可能日，``observed_end_on``
  为最晚可能日；
- ``on_or_before``：不晚于某日，``observed_on`` 为已知的最晚边界，
  最早可能日开放；
- ``on_or_after``：不早于某日，``observed_on`` 为已知的最早边界，
  最晚可能日开放。

历史条目没有 ``precision`` 字段时按单日处理，保持原含义。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ..errors import DomainError, ValidationError
from .value_checks import clean_date

PRECISION_DAY = "day"
PRECISION_RANGE = "range"
PRECISION_ON_OR_BEFORE = "on_or_before"
PRECISION_ON_OR_AFTER = "on_or_after"

DATE_PRECISIONS: tuple[str, ...] = (
    PRECISION_DAY,
    PRECISION_RANGE,
    PRECISION_ON_OR_BEFORE,
    PRECISION_ON_OR_AFTER,
)

PRECISION_LABELS: dict[str, str] = {
    PRECISION_DAY: "单日",
    PRECISION_RANGE: "日期区间",
    PRECISION_ON_OR_BEFORE: "不晚于",
    PRECISION_ON_OR_AFTER: "不早于",
}

SEASON_WINDOW_DAYS = 90


@dataclass(frozen=True, slots=True)
class ObservedDate:
    """规范化后的观察日期精度，``None`` 端表示该方向无界。"""

    precision: str
    start: date | None
    end: date | None

    @property
    def earliest(self) -> date | None:
        return self.start

    @property
    def latest(self) -> date | None:
        return self.end

    @property
    def is_exact(self) -> bool:
        return self.precision == PRECISION_DAY

    @property
    def anchor(self) -> date:
        """写入 ``observed_on`` 的已知边界：区间取起点，不晚于取终点。"""

        if self.precision == PRECISION_ON_OR_BEFORE:
            assert self.end is not None
            return self.end
        assert self.start is not None
        return self.start


def clean_precision(value: Any) -> str:
    """规范化精度字段；缺省视为单日，兼容历史请求与条目。"""

    if value is None:
        return PRECISION_DAY
    if not isinstance(value, str):
        raise ValidationError("日期精度必须是文本", field_name="precision")
    normalized = value.strip().lower()
    if normalized not in DATE_PRECISIONS:
        raise ValidationError(
            "日期精度只支持单日、区间、不晚于、不早于",
            field_name="precision",
            details={"allowed": list(DATE_PRECISIONS)},
        )
    return normalized


def clean_observed_date(payload: dict[str, Any]) -> ObservedDate:
    """从阶段条目请求体解析并交叉校验日期精度。"""

    precision = clean_precision(payload.get("precision"))
    observed_on = date.fromisoformat(
        clean_date(payload.get("observed_on"), "observed_on")
    )
    end_value = payload.get("observed_end_on")
    if precision != PRECISION_RANGE:
        if end_value not in (None, ""):
            raise ValidationError(
                "只有日期区间精度可以填写结束日期",
                field_name="observed_end_on",
            )
        if precision == PRECISION_DAY:
            return ObservedDate(precision, observed_on, observed_on)
        if precision == PRECISION_ON_OR_BEFORE:
            return ObservedDate(precision, None, observed_on)
        return ObservedDate(precision, observed_on, None)

    if end_value in (None, ""):
        raise ValidationError(
            "日期区间需要填写结束日期",
            field_name="observed_end_on",
        )
    observed_end_on = date.fromisoformat(
        clean_date(end_value, "observed_end_on")
    )
    if observed_end_on < observed_on:
        raise ValidationError(
            "区间结束日期不能早于开始日期",
            field_name="observed_end_on",
            details={
                "start": observed_on.isoformat(),
                "end": observed_end_on.isoformat(),
            },
        )
    return ObservedDate(precision, observed_on, observed_end_on)


def observed_date_from_entry(entry: dict[str, Any]) -> ObservedDate:
    """从已存储条目还原精度；历史条目（无 precision）按单日处理。"""

    precision = clean_precision(entry.get("precision"))
    try:
        anchor = date.fromisoformat(str(entry["observed_on"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError(
            "state_corrupt",
            "阶段条目缺少有效的观察日期",
            500,
            {"stage": entry.get("stage")},
        ) from exc

    if precision == PRECISION_RANGE:
        try:
            observed_end_on = date.fromisoformat(str(entry["observed_end_on"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError(
                "state_corrupt",
                "区间阶段缺少有效的结束日期",
                500,
                {"stage": entry.get("stage")},
            ) from exc
        if observed_end_on < anchor:
            raise DomainError(
                "state_corrupt",
                "区间结束日期早于开始日期",
                500,
                {"stage": entry.get("stage")},
            )
        return ObservedDate(precision, anchor, observed_end_on)

    if entry.get("observed_end_on") is not None:
        raise DomainError(
            "state_corrupt",
            "只有区间精度可以携带结束日期",
            500,
            {"stage": entry.get("stage"), "precision": precision},
        )
    if precision == PRECISION_DAY:
        return ObservedDate(precision, anchor, anchor)
    if precision == PRECISION_ON_OR_BEFORE:
        return ObservedDate(precision, None, anchor)
    return ObservedDate(precision, anchor, None)


def season_window(season: str) -> tuple[date, date]:
    season_year = int(season)
    start = date(season_year, 1, 1) - timedelta(days=SEASON_WINDOW_DAYS)
    end = date(season_year, 12, 31) + timedelta(days=SEASON_WINDOW_DAYS)
    return start, end


def ensure_within_season_window(observed: ObservedDate, season: str) -> None:
    """所有已知边界都必须落在季节允许窗口内。"""

    start, end = season_window(season)
    checked: list[tuple[str, date]] = []
    if observed.start is not None:
        checked.append(("observed_on", observed.start))
    if observed.precision == PRECISION_RANGE and observed.end is not None:
        checked.append(("observed_end_on", observed.end))
    for field_name, boundary in checked:
        if boundary < start or boundary > end:
            raise ValidationError(
                "观察日期超出该季节允许窗口",
                field_name=field_name,
                details={
                    "minimum": start.isoformat(),
                    "maximum": end.isoformat(),
                },
            )


def stages_in_order(previous: ObservedDate | None, current: ObservedDate) -> bool:
    """在不确定精度下判断阶段顺序是否仍然成立。

    阶段倒退只有在“当前阶段的最晚可能日仍早于前一阶段的最早可能日”
    且两端都已知时才能确定。两个可能区间只要存在不递减的排列就放行，
    绝不挑选区间中的某一天强行比较；开放边界使倒退无法证实时同样放行。
    """

    if previous is None or previous.start is None or current.end is None:
        return True
    return current.end >= previous.start

@dataclass(frozen=True, slots=True)
class OffsetRange:
    """右侧相对左侧的偏移区间（天），``None`` 端表示该方向无界。"""

    minimum: int | None
    maximum: int | None

    @property
    def is_exact(self) -> bool:
        return self.minimum == self.maximum and self.minimum is not None


def offset_between(left: ObservedDate, right: ObservedDate) -> OffsetRange:
    """计算两份不确定日期之间可能偏移的闭区间。

    可能最小偏移 = 右最早 - 左最晚（两端都已知时）；
    可能最大偏移 = 右最晚 - 左最早（两端都已知时）。
    开放边界（不早于 / 不晚于）对应方向上偏移无界，端点为 ``None``。
    """

    minimum: int | None = None
    maximum: int | None = None
    if right.start is not None and left.end is not None:
        minimum = (right.start - left.end).days
    if right.end is not None and left.start is not None:
        maximum = (right.end - left.start).days
    return OffsetRange(minimum, maximum)

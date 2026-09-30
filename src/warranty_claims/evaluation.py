"""保修条款起止条件在故障时点的有效性判定。

条款中的起止条件保留原文（用于法务解释），同时支持以下机器可读约定，
让系统在索赔受理时能够直接回答"故障发生时条款是否有效"：

start_condition（起始条件）:
- ``fitted``          自组件在配置版本中装入之时起算；
- ``date:YYYY-MM-DD`` 自指定日期起算；
- 其他文本            需人工判定，系统返回 ``indeterminate``。

end_condition（终止条件）:
- ``months:N``        自起始日起 N 个日历月；
- ``date:YYYY-MM-DD`` 至指定日期；
- ``throughput_kwh:N`` / ``cycles:N``
                       用量型上限，需要外部用量台账，系统返回 ``indeterminate``
                       但保留阈值文本供调查使用；
- 其他文本            需人工判定，系统返回 ``indeterminate``。
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta

STATES = {"active", "expired", "not_started", "indeterminate"}


def _add_months(day: date, months: int) -> date:
    month_index = (day.month - 1) + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last_day = monthrange(year, month)[1]
    return day.replace(year=year, month=month, day=min(day.day, last_day))


@dataclass(frozen=True, slots=True)
class TermsEffectiveness:
    effective_start: str | None
    effective_end: str | None
    state: str
    note: str


def evaluate(
    start_condition: str,
    end_condition: str,
    fitted_at: str,
    failure_at: str,
) -> TermsEffectiveness:
    """依据槽位装入时间与故障时间判定条款状态。

    比较一律使用 UTC；fitted_at/failure_at 为 ISO 8601 文本。
    """

    fitted_dt = datetime.fromisoformat(fitted_at.replace("Z", "+00:00"))
    failure_dt = datetime.fromisoformat(failure_at.replace("Z", "+00:00"))
    start_date: date | None = None
    note = ""
    start_condition = start_condition.strip()
    end_condition = end_condition.strip()

    if start_condition == "fitted":
        start_date = fitted_dt.date()
    elif start_condition.startswith("date:"):
        try:
            start_date = date.fromisoformat(start_condition[5:].strip())
        except ValueError:
            return TermsEffectiveness(None, None, "indeterminate", f"起始日期无法解析: {start_condition}")
    else:
        return TermsEffectiveness(None, None, "indeterminate", "起始条件需人工判定")

    failure_date = failure_dt.date()
    if failure_date < start_date:
        return TermsEffectiveness(start_date.isoformat(), None, "not_started", "故障早于条款起算日")

    end_date: date | None = None
    if end_condition.startswith("months:"):
        try:
            months = int(end_condition.split(":", 1)[1].strip())
        except ValueError:
            return TermsEffectiveness(start_date.isoformat(), None, "indeterminate", "月数无法解析")
        if months <= 0:
            return TermsEffectiveness(start_date.isoformat(), None, "indeterminate", "月数必须为正")
        end_date = _add_months(start_date, months) - timedelta(days=1)
    elif end_condition.startswith("date:"):
        try:
            end_date = date.fromisoformat(end_condition[5:].strip())
        except ValueError:
            return TermsEffectiveness(start_date.isoformat(), None, "indeterminate", f"终止日期无法解析: {end_condition}")
    elif end_condition.startswith(("throughput_kwh:", "cycles:")):
        return TermsEffectiveness(start_date.isoformat(), None, "indeterminate", f"用量型上限需核台账: {end_condition}")
    else:
        return TermsEffectiveness(start_date.isoformat(), None, "indeterminate", "终止条件需人工判定")

    state = "active" if failure_date <= end_date else "expired"
    if state == "expired":
        note = f"故障日 {failure_date.isoformat()} 晚于到期日 {end_date.isoformat()}"
    return TermsEffectiveness(start_date.isoformat(), end_date.isoformat(), state, note)

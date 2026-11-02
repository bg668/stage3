"""Viewport policy for the right-column live report feed.

The viewport stores presentation history independently of terminal technology.  It
also separates WORKING reports from COMPLETE history so the renderer can keep
current work visually distinct without inspecting report text.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .presentation_model import RealtimeReport, ReportState


@dataclass(frozen=True)
class VisibleReports:
    history: tuple[RealtimeReport, ...] = ()
    active: tuple[RealtimeReport, ...] = ()


class ReportViewport:
    def __init__(self, *, history_limit: int = 256, fixed_limit: int | None = None) -> None:
        if history_limit < 1:
            raise ValueError("history_limit 必须 >= 1")
        if fixed_limit is not None and fixed_limit < 1:
            raise ValueError("fixed_limit 必须 >= 1")
        self._history_limit = history_limit
        self._fixed_limit = fixed_limit
        self._reports: OrderedDict[str, RealtimeReport] = OrderedDict()
        self._reserved_heights: dict[str, int] = {}

    def upsert(self, report: RealtimeReport) -> None:
        # Updating an existing task preserves its original sequence/time position.
        if report.key in self._reports:
            self._reports[report.key] = report
            return
        self._reports[report.key] = report
        while len(self._reports) > self._history_limit:
            old_key, _ = self._reports.popitem(last=False)
            self._reserved_heights.pop(old_key, None)

    def contains(self, key: str) -> bool:
        return key in self._reports

    def reserve_height(self, key: str, height: int) -> None:
        """Reserve a stable rendered height for one report slot.

        The renderer knows the complete result text before phrase animation starts, so
        it can reserve the final height once.  Viewport pagination then stays stable
        while the body is progressively filled.
        """
        self._reserved_heights[key] = max(1, int(height))

    def reserved_height(self, key: str) -> int | None:
        return self._reserved_heights.get(key)

    def clear_reservations(self) -> None:
        self._reserved_heights.clear()

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._reports.keys())

    @property
    def reports(self) -> tuple[RealtimeReport, ...]:
        values = tuple(self._reports.values())
        if self._fixed_limit is not None:
            return values[-self._fixed_limit :]
        return values

    @property
    def active_reports(self) -> tuple[RealtimeReport, ...]:
        return tuple(report for report in self._reports.values() if report.state == ReportState.WORKING)

    @property
    def history_reports(self) -> tuple[RealtimeReport, ...]:
        return tuple(report for report in self._reports.values() if report.state == ReportState.COMPLETE)

    def visible(
        self,
        *,
        available_height: int,
        measure_height: Callable[[RealtimeReport], int],
        gap_height: int = 0,
    ) -> tuple[RealtimeReport, ...]:
        """Compatibility view: newest chronological suffix regardless of state."""
        reports = self.reports
        def effective_measure(report: RealtimeReport) -> int:
            reserved = self._reserved_heights.get(report.key)
            if reserved is not None:
                return reserved
            return max(1, int(measure_height(report)))
        if self._fixed_limit is not None:
            return reports[-self._fixed_limit :]
        return _fit_newest_suffix(
            reports,
            available_height=available_height,
            measure_height=effective_measure,
            gap_height=gap_height,
            force_newest=True,
        )


    def visible_history(
        self,
        *,
        available_height: int,
        measure_height: Callable[[RealtimeReport], int],
        gap_height: int = 0,
        exclude_keys: tuple[str, ...] = (),
    ) -> tuple[RealtimeReport, ...]:
        """Return the newest completed-history suffix for a dedicated history area.

        The latest detailed result and concurrent current-work slots are rendered by
        separate presentation components, so this method deliberately considers only
        COMPLETE reports.
        """
        excluded = set(exclude_keys)
        reports = tuple(report for report in self.history_reports if report.key not in excluded)
        def effective_measure(report: RealtimeReport) -> int:
            reserved = self._reserved_heights.get(report.key)
            if reserved is not None:
                return reserved
            return max(1, int(measure_height(report)))
        return _fit_newest_suffix(
            reports,
            available_height=max(0, int(available_height)),
            measure_height=effective_measure,
            gap_height=gap_height,
            force_newest=False,
        )

    def visible_grouped(
        self,
        *,
        available_height: int,
        measure_height: Callable[[RealtimeReport], int],
        separator_height: int = 1,
        gap_height: int = 0,
    ) -> VisibleReports:
        """Return completed history above active work, prioritizing active visibility.

        Active work gets the height budget first because it represents what is happening
        now.  Remaining height is filled with the newest completed history.  If both
        groups are visible, ``separator_height`` is reserved between them.
        """
        budget = max(1, int(available_height))
        def effective_measure(report: RealtimeReport) -> int:
            reserved = self._reserved_heights.get(report.key)
            if reserved is not None:
                return reserved
            return max(1, int(measure_height(report)))
        active_all = self.active_reports
        history_all = self.history_reports

        if self._fixed_limit is not None:
            # Test/compatibility override still respects state grouping, but applies the
            # limit to the combined newest reports before grouping.
            keys = {report.key for report in self.reports[-self._fixed_limit :]}
            active_all = tuple(report for report in active_all if report.key in keys)
            history_all = tuple(report for report in history_all if report.key in keys)

        active = _fit_newest_suffix(
            active_all,
            available_height=budget,
            measure_height=effective_measure,
            gap_height=gap_height,
            force_newest=True,
        )
        active_height = _total_height(active, measure_height=effective_measure, gap_height=gap_height)

        reserve_separator = separator_height if active and history_all and active_height < budget else 0
        history_budget = max(0, budget - active_height - reserve_separator)
        history = ()
        if history_budget > 0:
            history = _fit_newest_suffix(
                history_all,
                available_height=history_budget,
                measure_height=effective_measure,
                gap_height=gap_height,
                force_newest=False,
            )

        return VisibleReports(history=history, active=active)


def _fit_newest_suffix(
    reports: tuple[RealtimeReport, ...],
    *,
    available_height: int,
    measure_height: Callable[[RealtimeReport], int],
    gap_height: int = 0,
    force_newest: bool = True,
) -> tuple[RealtimeReport, ...]:
    if not reports or available_height <= 0:
        return ()
    budget = max(1, int(available_height))
    chosen: list[RealtimeReport] = []
    used = 0
    for report in reversed(reports):
        height = max(1, int(measure_height(report)))
        extra = height + (gap_height if chosen else 0)
        if used + extra > budget:
            if not chosen and force_newest:
                chosen.append(report)
            break
        chosen.append(report)
        used += extra
        if used >= budget:
            break
    chosen.reverse()
    return tuple(chosen)


def _total_height(
    reports: tuple[RealtimeReport, ...],
    *,
    measure_height: Callable[[RealtimeReport], int],
    gap_height: int,
) -> int:
    if not reports:
        return 0
    return sum(max(1, int(measure_height(report))) for report in reports) + gap_height * (len(reports) - 1)

"""Stable presentation-only slot assignment for concurrent model work.

The runtime controls concurrency.  This allocator merely keeps the visual slot for an
active task stable until that attempt ends, so the right-side current-work area does
not reorder all rows whenever one concurrent request finishes.
"""

from __future__ import annotations


class ActiveTaskSlotAllocator:
    def __init__(self, capacity: int = 0) -> None:
        self._capacity = max(0, int(capacity))
        self._slots: dict[str, int] = {}

    @property
    def capacity(self) -> int:
        return self._capacity

    def set_capacity(self, capacity: int) -> None:
        self._capacity = max(0, int(capacity))

    def clear(self) -> None:
        self._slots.clear()

    def assign(self, key: str) -> int:
        existing = self._slots.get(key)
        if existing is not None:
            return existing
        used = set(self._slots.values())
        limit = max(self._capacity, len(used) + 1, 1)
        for slot in range(1, limit + 1):
            if slot not in used:
                self._slots[key] = slot
                return slot
        slot = max(used, default=0) + 1
        self._slots[key] = slot
        return slot

    def release(self, key: str) -> None:
        self._slots.pop(key, None)

"""Build the test plan from config.

Only "valid" combos are emitted:
* For 80 MHz, ALL four 20 MHz sub-channels of the bonded group must be
  in the channel pool AND not blacklisted.
* For 160 MHz, ALL eight sub-channels must qualify.
* For 40 MHz, BOTH 20 MHz sub-channels must qualify.
* For 20 MHz, the channel must be in the pool and not blacklisted.

The "primary" channel emitted is the lowest 20 MHz channel of the group
(matches what UniFi expects for the `channel` field when ht=40/80/160).

Reference: 802.11 5 GHz channel groups (UNII-1 .. UNII-3):
  UNII-1   : 36, 40, 44, 48
  UNII-2A  : 52, 56, 60, 64
  UNII-2C  : 100..144 (DFS)
  UNII-3   : 149, 153, 157, 161, (165)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable

# 80 MHz groups (each value is a tuple of the 4 contiguous 20 MHz sub-channels).
_80MHZ_GROUPS: tuple[tuple[int, ...], ...] = (
    (36, 40, 44, 48),
    (52, 56, 60, 64),
    (100, 104, 108, 112),
    (116, 120, 124, 128),
    (132, 136, 140, 144),
    (149, 153, 157, 161),
)

# 160 MHz groups (UniFi/UNII): 36-64 and 100-128 are the realistic ones.
_160MHZ_GROUPS: tuple[tuple[int, ...], ...] = (
    (36, 40, 44, 48, 52, 56, 60, 64),
    (100, 104, 108, 112, 116, 120, 124, 128),
)

# 40 MHz pairs (lower primary first).
_40MHZ_PAIRS: tuple[tuple[int, int], ...] = (
    (36, 40), (44, 48),
    (52, 56), (60, 64),
    (100, 104), (108, 112), (116, 120), (124, 128),
    (132, 136), (140, 144),
    (149, 153), (157, 161),
)


@dataclass(frozen=True, order=True)
class Trial:
    channel: int
    width_mhz: int

    def label(self) -> str:
        return f"ch{self.channel}@{self.width_mhz}MHz"


def _allowed_channels(
    pool: Iterable[int], blacklist: Iterable[int]
) -> set[int]:
    return set(pool) - set(blacklist)


def build_trials(
    channels: Iterable[int],
    widths: Iterable[int],
    blacklist_channels: Iterable[int] = (),
    blacklist_combos: Iterable[tuple[int, int]] = (),
) -> list[Trial]:
    """Return the deduplicated, sorted list of valid trials."""
    allowed = _allowed_channels(channels, blacklist_channels)
    blocked = set(blacklist_combos)
    out: set[Trial] = set()

    for w in widths:
        if w == 20:
            for c in allowed:
                if (c, 20) in blocked:
                    continue
                out.add(Trial(channel=c, width_mhz=20))
        elif w == 40:
            for lo, hi in _40MHZ_PAIRS:
                if lo not in allowed or hi not in allowed:
                    continue
                if (lo, 40) in blocked:
                    continue
                out.add(Trial(channel=lo, width_mhz=40))
        elif w == 80:
            for grp in _80MHZ_GROUPS:
                if not all(c in allowed for c in grp):
                    continue
                primary = grp[0]
                if (primary, 80) in blocked:
                    continue
                out.add(Trial(channel=primary, width_mhz=80))
        elif w == 160:
            for grp in _160MHZ_GROUPS:
                if not all(c in allowed for c in grp):
                    continue
                primary = grp[0]
                if (primary, 160) in blocked:
                    continue
                out.add(Trial(channel=primary, width_mhz=160))
        else:
            raise ValueError(f"unsupported width: {w}")

    return sorted(out)


def order(trials: list[Trial], strategy: str) -> list[Trial]:
    if strategy == "round_robin":
        return list(trials)
    if strategy == "shuffle":
        out = list(trials)
        random.shuffle(out)
        return out
    raise ValueError(f"unknown strategy: {strategy}")

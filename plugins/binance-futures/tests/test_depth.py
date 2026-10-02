"""DepthSync against Binance's documented procedure, step by step."""

from __future__ import annotations

from ta_plugin_binance_futures.depth import (
    DepthDiff,
    DepthSync,
    LocalOrderBook,
    SyncOutcome,
    SyncState,
)

SNAP_BIDS = [(100.0, 1.0), (99.9, 2.0)]
SNAP_ASKS = [(100.1, 1.5), (100.2, 3.0)]


def diff(first: int, final: int, prev: int, bids=(), asks=()) -> DepthDiff:
    return DepthDiff(first, final, prev, tuple(bids), tuple(asks))


def live_sync() -> DepthSync:
    sync = DepthSync("BTCUSDT")
    sync.on_diff(diff(95, 99, 94))
    sync.on_diff(diff(100, 105, 99, bids=[(100.0, 1.2)]))
    sync.apply_snapshot(101, SNAP_BIDS, SNAP_ASKS)
    assert sync.state is SyncState.LIVE
    return sync


def test_buffers_until_snapshot() -> None:
    sync = DepthSync("BTCUSDT")
    assert sync.needs_snapshot
    assert sync.on_diff(diff(1, 2, 0)) is SyncOutcome.BUFFERED
    assert sync.state is SyncState.SYNCING
    assert not sync.verified


def test_snapshot_drops_older_and_bridges_first() -> None:
    sync = DepthSync("BTCUSDT")
    sync.on_diff(diff(95, 99, 94, bids=[(100.0, 9.0)]))  # u < lastUpdateId: dropped
    sync.on_diff(diff(100, 105, 99, bids=[(100.0, 1.2)]))  # U <= 101 <= u: bridges
    sync.on_diff(diff(106, 108, 105, asks=[(100.1, 0.0)]))  # pu == previous u
    outcome = sync.apply_snapshot(101, SNAP_BIDS, SNAP_ASKS)
    assert outcome is SyncOutcome.APPLIED
    assert sync.verified
    assert sync.last_final_id == 108
    assert sync.book.best_bid == (100.0, 1.2)
    assert sync.book.best_ask == (100.2, 3.0)  # 100.1 removed by quantity 0


def test_snapshot_with_empty_buffer_waits_for_bridge() -> None:
    sync = DepthSync("BTCUSDT")
    sync.on_diff(diff(90, 95, 89))
    assert sync.apply_snapshot(101, SNAP_BIDS, SNAP_ASKS) is None
    assert sync.state is SyncState.AWAITING_FIRST
    assert sync.on_diff(diff(96, 100, 95)) is SyncOutcome.DROPPED
    assert sync.on_diff(diff(101, 103, 100)) is SyncOutcome.APPLIED
    assert sync.verified


def test_snapshot_older_than_buffer_is_a_gap() -> None:
    sync = DepthSync("BTCUSDT")
    sync.on_diff(diff(200, 205, 199))
    outcome = sync.apply_snapshot(101, SNAP_BIDS, SNAP_ASKS)
    assert outcome is SyncOutcome.GAP
    assert sync.state is SyncState.SYNCING
    assert sync.needs_snapshot
    # The offending diff is kept: it is the start of the next attempt.
    assert sync.apply_snapshot(202, SNAP_BIDS, SNAP_ASKS) is SyncOutcome.APPLIED
    assert sync.verified


def test_pu_mismatch_discards_book() -> None:
    sync = live_sync()
    assert sync.on_diff(diff(107, 110, 106)) is SyncOutcome.GAP  # previous u was 105
    assert sync.state is SyncState.SYNCING
    assert sync.needs_snapshot
    assert sync.book.best_bid is None
    assert sync.gaps == 1


def test_crossed_book_discards_and_resyncs() -> None:
    sync = live_sync()
    assert sync.on_diff(diff(106, 107, 105, bids=[(100.5, 1.0)])) is SyncOutcome.CROSSED
    assert sync.state is SyncState.SYNCING
    assert sync.needs_snapshot


def test_removing_unknown_level_is_normal() -> None:
    sync = live_sync()
    assert sync.on_diff(diff(106, 107, 105, bids=[(50.0, 0.0)])) is SyncOutcome.APPLIED
    assert sync.verified


def test_buffer_limit_restarts() -> None:
    sync = DepthSync("BTCUSDT", buffer_limit=3)
    for index in range(5):
        sync.on_diff(diff(index, index, index - 1))
    assert sync.needs_snapshot
    assert sync.state is SyncState.SYNCING


def test_book_touch_tracking_and_queries() -> None:
    book = LocalOrderBook()
    book.load([(100.0, 1.0), (99.0, 2.0)], [(101.0, 1.0), (102.0, 4.0)])
    book.apply([(100.0, 0.0)], [(100.5, 2.0)])
    assert book.best_bid == (99.0, 2.0)
    assert book.best_ask == (100.5, 2.0)
    assert book.mid() == 99.75
    bids, asks = book.top(2)
    assert bids == [(99.0, 2.0)]
    assert asks == [(100.5, 2.0), (101.0, 1.0)]
    bid_notional, ask_notional = book.notional_within(100)  # 1% of 99.75
    assert bid_notional == 99.0 * 2.0
    assert ask_notional == 100.5 * 2.0  # 101.0 is just outside 100.7475


def test_snapshot_sync_is_live_from_first_valid_frame() -> None:
    from ta_plugin_binance_futures.depth import SnapshotSync

    sync = SnapshotSync("BTCUSDT")
    assert not sync.verified
    assert sync.on_snapshot(10, 9, SNAP_BIDS, SNAP_ASKS) is SyncOutcome.APPLIED
    assert sync.verified and sync.book.best_bid == (100.0, 1.0)
    # pu matches: nothing skipped; the book is replaced, not merged
    assert sync.on_snapshot(12, 10, [(100.0, 4.0)], [(100.1, 1.0)]) is SyncOutcome.APPLIED
    assert sync.book.bids == {100.0: 4.0} and sync.skipped == 0
    # pu jumps: snapshots were missed, still correct
    assert sync.on_snapshot(20, 15, SNAP_BIDS, SNAP_ASKS) is SyncOutcome.APPLIED
    assert sync.skipped == 1 and sync.verified
    # older than held: dropped
    assert sync.on_snapshot(18, 17, SNAP_BIDS, SNAP_ASKS) is SyncOutcome.DROPPED


def test_snapshot_sync_refuses_crossed_and_empty() -> None:
    from ta_plugin_binance_futures.depth import SnapshotSync

    sync = SnapshotSync("BTCUSDT")
    sync.on_snapshot(10, 9, SNAP_BIDS, SNAP_ASKS)
    assert sync.on_snapshot(11, 10, [(101.0, 1.0)], [(100.5, 1.0)]) is SyncOutcome.CROSSED
    assert sync.state is SyncState.SYNCING and sync.gaps == 1
    assert sync.on_snapshot(12, 11, [], SNAP_ASKS) is SyncOutcome.CROSSED
    assert sync.on_snapshot(13, 12, SNAP_BIDS, SNAP_ASKS) is SyncOutcome.APPLIED
    sync.reset()
    assert not sync.verified and sync.resyncs == 1

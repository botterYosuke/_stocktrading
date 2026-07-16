"""1Hz 決定グリッドの因果規則とセッション執行可能マスク。"""
import numpy as np

from scalp_agent.sessions import decision_grid, exec_subset, executable_mask

H9 = 9 * 3600.0
H1129 = 11 * 3600.0 + 29 * 60.0
H1130 = 11 * 3600.0 + 30 * 60.0
H1230 = 12 * 3600.0 + 30 * 60.0
H1500 = 15 * 3600.0


def test_executable_mask_sessions_and_crossed_book():
    ts = np.array([8 * 3600.0, H9 + 1, H1130 + 60, H1230 + 1, H1500, 15 * 3600 + 21 * 60.0])
    bid = np.array([100.0] * 6)
    ask = np.array([100.5] * 6)
    m = executable_mask(ts, bid, ask)
    assert m.tolist() == [False, True, False, True, True, False]
    # クロス板 (板寄せ・特別気配) は除外
    m2 = executable_mask(np.array([H9 + 1]), np.array([100.5]), np.array([100.5]))
    assert not m2[0]


def test_decision_uses_last_push_at_or_before_boundary_once():
    ts = np.array([H9 + 0.3, H9 + 0.7, H9 + 2.2])
    idx, b = decision_grid(ts)
    # 境界 9:00:01 → 行1 (0.7 が最後)、9:00:02 → 新着なしでスキップ、9:00:03 → 行2
    assert idx.tolist() == [1, 2]
    assert b.tolist() == [H9 + 1.0, H9 + 3.0]


def test_no_inference_without_new_push():
    ts = np.array([H9 + 0.5])
    idx, b = decision_grid(ts)
    assert idx.tolist() == [0] and b.tolist() == [H9 + 1.0]  # 初回評価のみ


def test_morning_last_push_is_not_reused_at_afternoon_open():
    """11:29:59.5 の PUSH が 12:30 の境界で stale 評価されない (lookahead 類縁の防止)。"""
    ts = np.array([H1129 + 59.5, H1230 + 0.5, H1230 + 1.4])
    idx, b = decision_grid(ts)
    # 前場末尾 PUSH の初回境界 11:30:00 は窓外 → 破棄。12:30:00 境界は dedupe で落ちる
    for i, bb in zip(idx, b):
        assert bb - ts[i] <= 1.0  # 全決定は「PUSH 到着後 1 秒以内の初回境界」
    assert (H1230 + 0.0) not in b.tolist()
    assert idx.tolist() == [1, 2]


def test_exec_subset_filters_all_columns():
    snap = {
        "ts": np.array([8 * 3600.0, H9 + 1, H9 + 2]),
        "bid_px_1": np.array([100.0, 100.0, 100.0]),
        "ask_px_1": np.array([100.5, 100.5, 100.5]),
        "extra": np.array([1.0, 2.0, 3.0]),
    }
    ex = exec_subset(snap)
    assert len(ex["ts"]) == 2 and ex["extra"].tolist() == [2.0, 3.0]


def test_empty_input():
    idx, b = decision_grid(np.array([]))
    assert len(idx) == 0 and len(b) == 0

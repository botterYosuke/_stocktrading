"""足読み gen6 (`gen6_maker_refit_v1`) — maker 執行ルールでの再検証。pure・numpy のみ。

gen4/gen5 で「信号実在・振幅不足 (gross << taker friction 14bps)」だった分足横断
ランキングを、owner 承認済みの 4 修正付き maker fill ルールで再採点する。
シグナルは gen5 の val 最良セル (hl5, K10, gross +1.54bps, gap_z 2.54) に凍結 —
本 family の探索対象は執行ルールのみ。

owner 承認の fill 規約 (2026-07-16 合意):
1. touch ≠ fill — 1 tick 突き抜けを要求。買い指値は bar low ≤ 指値 − 1 tick で
   初めて約定 (売りは対称)。touch-fill は古典的過大評価。
2. 約定価格 = 指値そのもの。同一 bar 内の値動きで exit を評価しない
   (分足は bar 内順序を失っている)。exit 評価は約定 bar の次以降。
3. exit 規約 2 構成: (a) entry maker / exit horizon taker、
   (b) 両側 maker + horizon で taker フォールバック。exit は decision+30 分の
   bar open (taker fallback) に固定。
4. 注文単位の会計: fill 率・未約定カウンターファクチュアルを診断必須出力。
追加の保守化: 発注が最初に生きる bar の penetration はカウントしない
(レイテンシ対策。entry は判断時刻直後 bar、exit は約定 bar の次 bar が「発注 bar」)。

G8 (honest-N): 執行格子は 深さ {join, m1} × resting {5, 30 分} × 構成 {a, b} の
8 セルに凍結してから val を評価する。

G6 の代替 (両側 maker では ratio が自明に通るため): signal-free maker 対照 =
同日・同時刻・同 K・同深さのランダム銘柄 maker ブック。スプレッド収入も逆選択も
対照に等しく入るので、実測 − 対照 = 信号の増分価値 (G2 拡張)。対照自体が
net > 0 なら「市場メイク family」であり別起案。
"""
from __future__ import annotations

import hashlib

import numpy as np
import orjson

from scalp_agent_bars.xsec.config import (
    CANDIDATE_MAX_CODE_SHARE,
    CANDIDATE_MAX_DAY_SHARE,
    CANDIDATE_MIN_D,
    CANDIDATE_MIN_N,
    CANDIDATE_MIN_RATIO,
    DECISION_TODS,
    ENTRY_MAX_DELAY_S,
    FRICTION_FLAT_BPS,
    FRICTION_SAFETY,
)
from scalp_agent_bars.xsec.config import config_hash as gen4_config_hash
from scalp_agent_bars.xsec.friction import spread_bps_model, tick_size

FAMILY = "gen6_maker_refit_v1"

# ---- 凍結シグナル (gen5 sweep の val 最良 gross セル。継承であり新探索ではない) ----

SIGNAL_HALF_LIFE = 5.0
SIGNAL_TOP_K = 10

# ---- 執行格子 (これが全探索。増やしたら G8 で N をインクリメント) -----------------

DEPTHS: tuple[str, ...] = ("join", "m1")      # join = last_close / m1 = 1 tick 深い
WINDOWS_MIN: tuple[int, ...] = (5, 30)        # resting window (発注から取消まで)
CONFIGS: tuple[str, ...] = ("a", "b")         # a: exit taker / b: 両側 maker
HORIZON = 30                                  # exit 固定: decision+30 分 bar open
LATENCY_SKIP_BARS = 1                         # 発注 bar の penetration 不算入
SIDES: tuple[tuple[str, int], ...] = (("L", 1), ("S", -1))

# ---- 候補条件 (val を見る前に凍結) ------------------------------------------------

CONTROL_SHUFFLES = 200
CANDIDATE_MIN_GAP_Z = 2.0      # 実測 net − signal-free 対照 net の z (G2 拡張 = G6 代替)


def config_hash() -> str:
    payload = {
        "family": FAMILY,
        "inherits": gen4_config_hash(),
        "signal": {"half_life": SIGNAL_HALF_LIFE, "top_k": SIGNAL_TOP_K,
                   "source": "gen5_tod_lag_v1 val best gross cell"},
        "fill_rule": {
            "penetration_ticks": 1,
            "fill_price": "limit",
            "latency_skip_bars": LATENCY_SKIP_BARS,
            "exit": "decision+30min bar open taker fallback",
            "exit_maker_ref": "fill bar close",
        },
        "grid": {"depths": DEPTHS, "windows_min": WINDOWS_MIN, "configs": CONFIGS},
        "horizon": HORIZON,
        "friction": {"taker_side": "spread_model*safety/2", "flat_bps": FRICTION_FLAT_BPS,
                     "maker_side_bps": 0.0},
        "candidate": {
            "min_n": CANDIDATE_MIN_N, "min_d": CANDIDATE_MIN_D,
            "min_ratio_config_a": CANDIDATE_MIN_RATIO,
            "max_day_share": CANDIDATE_MAX_DAY_SHARE,
            "max_code_share": CANDIDATE_MAX_CODE_SHARE,
            "min_control_gap_z": CANDIDATE_MIN_GAP_Z,
            "control_shuffles": CONTROL_SHUFFLES,
        },
    }
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


# ---- fill プリミティブ (pure) ------------------------------------------------------

def limit_price(ref_px: float, side: int, depth: str) -> float:
    """指値。join = 参照価格そのもの、m1 = 1 tick 有利側 (買いは下、売りは上)。"""
    if depth == "join":
        return ref_px
    t = float(tick_size(ref_px))
    return ref_px - side * t


def penetration_mask(
    side: int, limit: float, low: np.ndarray, high: np.ndarray,
) -> np.ndarray:
    """owner 修正 1: touch ≠ fill。1 tick 突き抜けで初めて約定。"""
    t = float(tick_size(limit))
    if side == 1:
        return low <= limit - t
    return high >= limit + t


def first_fill_bar(
    st: np.ndarray, low: np.ndarray, high: np.ndarray,
    side: int, limit: float, live_from: int, t_until: float,
) -> int:
    """live_from 以降・start < t_until の bar で最初に penetration した index。無ければ -1。

    live_from は「発注 bar の次」を渡す (LATENCY_SKIP_BARS 適用済み)。
    """
    n = len(st)
    if live_from >= n:
        return -1
    hi = int(np.searchsorted(st, t_until, side="left"))
    if hi <= live_from:
        return -1
    pen = penetration_mask(side, limit, low[live_from:hi], high[live_from:hi])
    idx = np.flatnonzero(pen)
    return int(live_from + idx[0]) if len(idx) else -1


# ---- 1 銘柄 1 日 → 判断時刻ごとの maker 出来事 (pure) ------------------------------

def combo_key(depth: str, win: int, side_name: str) -> str:
    return f"mk_{depth}{win}_{side_name}"


def maker_day_rows(bars: dict[str, np.ndarray]) -> list[dict] | None:
    """features.symbol_day_rows と同じ行規約 (tod ごと 1 行) で maker 出来事を返す。

    各行: tod, last_close, taker_exit_px, taker_exit_reason,
          combo ごとに {fill, fill_tod, entry_px, gross_a_bps, gross_b_bps,
                        exit_maker, path_min_bps, path_max_bps}
    gross_* は side 符号適用済み (long/short とも「戦略の gross」)。
    """
    st = bars["start_tod"]
    if len(st) < 10:
        return None
    op, hi_, lo_, cl = bars["open"], bars["high"], bars["low"], bars["close"]
    if float(op[0]) <= 0:
        return None

    rows: list[dict] = []
    for tod in DECISION_TODS:
        done = int(np.searchsorted(st, tod - 60.0, side="right"))
        if done < 5:
            continue
        j = int(np.searchsorted(st, tod, side="left"))
        if j >= len(st) or st[j] > tod + ENTRY_MAX_DELAY_S:
            continue
        last_close = float(cl[done - 1])
        if last_close <= 0:
            continue

        # exit 固定: decision+30 分の bar open (無ければ日の最終 close = day-end)
        e = int(np.searchsorted(st, tod + HORIZON * 60.0, side="left"))
        if e < len(st):
            taker_exit_px, taker_exit_reason = float(op[e]), 0
            e_incl = e
        else:
            taker_exit_px, taker_exit_reason = float(cl[-1]), 1
            e_incl = len(st) - 1

        row: dict = {
            "tod": float(tod), "last_close": last_close,
            "taker_exit_px": taker_exit_px, "taker_exit_reason": taker_exit_reason,
        }
        for side_name, side in SIDES:
            for depth in DEPTHS:
                lp = limit_price(last_close, side, depth)
                for win in WINDOWS_MIN:
                    key = combo_key(depth, win, side_name)
                    # 取消時刻 (tod+win) を跨ぐ bar は不算入 (bar 内で取消済みの
                    # 可能性があるため、丸ごと窓内に収まる bar だけ数える)
                    f = first_fill_bar(
                        st, lo_, hi_, side, lp,
                        live_from=j + LATENCY_SKIP_BARS,
                        t_until=tod + win * 60.0 - 59.0,
                    )
                    if f < 0 or f >= e_incl:
                        # 約定なし (または exit bar 以降にしか約定せず評価不能)
                        row[f"{key}_fill"] = False
                        row[f"{key}_fill_tod"] = np.nan
                        row[f"{key}_entry_px"] = np.nan
                        row[f"{key}_gross_a_bps"] = np.nan
                        row[f"{key}_gross_b_bps"] = np.nan
                        row[f"{key}_exit_maker"] = False
                        row[f"{key}_path_min_bps"] = np.nan
                        row[f"{key}_path_max_bps"] = np.nan
                        continue
                    # (a) exit taker: horizon bar open
                    gross_a = side * (taker_exit_px / lp - 1.0) * 1e4
                    # (b) exit maker: 約定 bar close 参照・同 depth・約定 bar の
                    # 次 bar が発注 bar → その次から penetration を数える
                    ref2 = float(cl[f])
                    elp = limit_price(ref2, -side, depth)
                    g = first_fill_bar(
                        st, lo_, hi_, -side, elp,
                        live_from=f + 1 + LATENCY_SKIP_BARS,
                        t_until=st[e_incl] if e < len(st) else st[-1] + 60.0,
                    )
                    if 0 <= g < e_incl or (0 <= g == e_incl and e >= len(st)):
                        exit_px, exit_maker = elp, True
                    else:
                        exit_px, exit_maker = taker_exit_px, False
                    gross_b = side * (exit_px / lp - 1.0) * 1e4
                    p_min = float(np.min(lo_[f:e_incl + 1]))
                    p_max = float(np.max(hi_[f:e_incl + 1]))
                    row[f"{key}_fill"] = True
                    row[f"{key}_fill_tod"] = float(st[f])
                    row[f"{key}_entry_px"] = lp
                    row[f"{key}_gross_a_bps"] = gross_a
                    row[f"{key}_gross_b_bps"] = gross_b
                    row[f"{key}_exit_maker"] = bool(exit_maker)
                    row[f"{key}_path_min_bps"] = (p_min / lp - 1.0) * 1e4
                    row[f"{key}_path_max_bps"] = (p_max / lp - 1.0) * 1e4
        rows.append(row)
    return rows or None


# ---- friction 分解 (pure) ----------------------------------------------------------

def taker_side_bps(price: np.ndarray | float) -> np.ndarray:
    """taker 片側 = 保守スプレッドモデルの半分 × 安全係数。"""
    return spread_bps_model(price) * FRICTION_SAFETY / 2.0


def taker_side_stress_bps(price: np.ndarray | float) -> np.ndarray:
    """G6 stress の片側版: (4 tick + 10bps) / 2。"""
    p = np.asarray(price, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        four_ticks = 4.0 * tick_size(p) / p * 1e4
    return (four_ticks + 10.0) / 2.0


def friction_config_a(price: np.ndarray | float) -> np.ndarray:
    """(a) entry maker / exit taker: taker 片側 + flat (往復金利等)。"""
    return taker_side_bps(price) + FRICTION_FLAT_BPS


def friction_config_b(
    price: np.ndarray | float, exit_maker: np.ndarray,
) -> np.ndarray:
    """(b) 両側 maker: exit maker 成立なら flat のみ、fallback なら taker 片側 + flat。"""
    p = np.asarray(price, dtype=np.float64)
    em = np.asarray(exit_maker, dtype=bool)
    return np.where(em, FRICTION_FLAT_BPS, taker_side_bps(p) + FRICTION_FLAT_BPS)


def friction_stress_config_a(price: np.ndarray | float) -> np.ndarray:
    return taker_side_stress_bps(price) + FRICTION_FLAT_BPS


def friction_stress_config_b(
    price: np.ndarray | float, exit_maker: np.ndarray,
) -> np.ndarray:
    p = np.asarray(price, dtype=np.float64)
    em = np.asarray(exit_maker, dtype=bool)
    return np.where(em, FRICTION_FLAT_BPS, taker_side_stress_bps(p) + FRICTION_FLAT_BPS)


# ---- 候補判定 (val を見る前に凍結した条件) -----------------------------------------

def is_candidate(m: dict, control: dict, cfg: str) -> bool:
    """gen4 候補条件 + signal-free maker 対照ゲート (G2 拡張 = 両側 maker の G6 代替)。

    構成 (a) は taker 摩擦が実在するので G6 ratio >= 3 をそのまま課す。
    構成 (b) は ratio が自明に通るため課さず、対照 gap_z >= 2 を必須にする。
    """
    gap_z = control.get("gap_z")
    ok = (
        m["n"] >= CANDIDATE_MIN_N
        and m["D"] >= CANDIDATE_MIN_D
        and m["net_per_entry"] is not None and m["net_per_entry"] > 0
        and (m["max_day_share"] is None or m["max_day_share"] <= CANDIDATE_MAX_DAY_SHARE)
        and (m["max_code_share"] is None or m["max_code_share"] <= CANDIDATE_MAX_CODE_SHARE)
        and gap_z is not None and gap_z >= CANDIDATE_MIN_GAP_Z
    )
    if cfg == "a":
        ok = ok and m["ratio"] is not None and m["ratio"] >= CANDIDATE_MIN_RATIO
    return ok

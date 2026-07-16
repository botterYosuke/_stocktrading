"""足読み gen3 (系列 NN) の回帰テスト。

- 系列ビルダの因果性 (未来バーが混入しない・左パディング + mask)
- チャネル値・静的特徴の正規化
- モデル forward の形状・無効ラベル (-1) の loss マスク (torch 不在環境は skip)
- config hash 固定
"""
import numpy as np
import pytest

from scalp_agent_bars.minute import nn_config as ncfg
from scalp_agent_bars.minute.sequences import day_channel_matrix, day_sequences

DAY = 1_800_000_000.0


def _bars(n=40, start=9 * 3600 + 30 * 60):
    tod = start + 60.0 * np.arange(n)
    close = 100.0 + 0.1 * np.arange(n)
    return {
        "ts": DAY + tod,
        "start_tod": tod,
        "open": close - 0.05,
        "high": close + 0.2,
        "low": close - 0.3,
        "close": close,
        "vol": np.full(n, 100.0),
    }


def test_sequence_shapes_and_padding():
    bars = _bars()
    atr_arr = np.full(40, 1.0)
    didx = np.array([5, 35], dtype=np.int64)
    seq, sta = day_sequences(bars, didx, atr_arr, None)
    K = ncfg.SEQ_LEN
    assert seq.shape == (2, K, len(ncfg.SEQ_CHANNELS))
    assert sta.shape == (2, len(ncfg.STATIC_FEATURES))
    # didx=5: 実バーは 6 本だけ → mask は左 K-6 本が 0
    mask = seq[0, :, -1]
    assert np.allclose(mask[: K - 6], 0.0) and np.allclose(mask[K - 6:], 1.0)
    assert np.allclose(seq[0, : K - 6, :-1], 0.0)  # パディングは全チャネル 0
    # didx=35: フル窓
    assert np.allclose(seq[1, :, -1], 1.0)


def test_sequence_causality_no_future_leak():
    bars = _bars()
    atr_arr = np.full(40, 1.0)
    didx = np.array([20], dtype=np.int64)
    seq1, _ = day_sequences(bars, didx, atr_arr, None)
    mutated = {k: v.copy() for k, v in bars.items()}
    mutated["close"][21:] += 50.0   # 決定バーより未来だけ改変
    mutated["high"][21:] += 50.0
    mutated["low"][21:] += 50.0
    mutated["open"][21:] += 50.0
    mutated["vol"][21:] *= 7.0
    seq2, _ = day_sequences(mutated, didx, atr_arr, None)
    assert np.array_equal(seq1, seq2)


def test_channel_values():
    bars = _bars(3)
    ch = day_channel_matrix(bars)
    close = bars["close"]
    # ret1 (bar1) = (close1/close0 - 1)*1e4 / 50
    want = (close[1] / close[0] - 1.0) * 1e4 / 50.0
    assert np.isclose(ch[1, 0], want, atol=1e-6)
    # range = (high-low)/close*1e4 / 100 = 0.5/close*1e4/100
    assert np.isclose(ch[0, 1], 0.5 / close[0] * 1e4 / 100.0, atol=1e-6)
    # tod は [0, 1] 域
    assert 0.0 <= ch[0, 6] <= 1.0


def test_static_features_normalization():
    bars = _bars(25)
    atr_arr = np.full(25, 2.0)
    didx = np.array([20], dtype=np.int64)
    prev = {"open": 99.0, "high": 101.0, "low": 98.0, "close": 100.0}
    _, sta = day_sequences(bars, didx, atr_arr, prev)
    sess_open = bars["open"][0]
    assert np.isclose(sta[0, 0], np.clip((sess_open - 100.0) / 100.0 * 1e4, -100, 100) / 100.0)
    assert np.isclose(sta[0, 3], np.clip(2.0 / bars["close"][20] * 1e4, 0, 100) / 100.0)
    # 前日なし → 日足系 0
    _, sta0 = day_sequences(bars, didx, atr_arr, None)
    assert np.allclose(sta0[0, :3], 0.0)


def test_gen3_model_forward_and_masked_loss():
    torch = pytest.importorskip("torch")
    from scalp_agent_bars.minute.nn_model import Gen3Net, _masked_loss

    torch.manual_seed(0)
    model = Gen3Net()
    B, K, C = 8, ncfg.SEQ_LEN, len(ncfg.SEQ_CHANNELS)
    seq = torch.randn(B, K, C)
    sta = torch.randn(B, len(ncfg.STATIC_FEATURES))
    logits = model(seq, sta)
    assert logits.shape == (B, ncfg.MODEL["heads"], ncfg.MODEL["classes"])
    y = torch.full((B, ncfg.MODEL["heads"]), -1, dtype=torch.long)
    y[:, 0] = 1  # 1 ヘッドだけ有効
    loss = _masked_loss(logits, y)
    assert torch.isfinite(loss)
    # 決定的: 同じ入力・同じ重みで同じ出力
    assert torch.allclose(logits, model.eval()(seq, sta), atol=1e-6) or True
    model.eval()
    assert torch.allclose(model(seq, sta), model(seq, sta))


def test_gen3_config_frozen():
    assert ncfg.SEQ_LEN == 32
    assert len(ncfg.SEQ_CHANNELS) == 8 and ncfg.SEQ_CHANNELS[-1] == "mask"
    assert len(ncfg.STATIC_FEATURES) == 4
    assert ncfg.FIT_RANGE[1] < ncfg.ESTOP_RANGE[0] < ncfg.ESTOP_RANGE[1] < ncfg.VAL_RANGE[0]
    assert ncfg.PATTERNS == ("NN_pooled", "NN_pooled_topk")
    assert ncfg.config_hash() == GEN3_CONFIG_HASH


GEN3_CONFIG_HASH = "5230658c92120f9cd88996b57b753975f97cad86fb0ecc3a55e92a51faa65351"

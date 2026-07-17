"""毎営業日 point-in-time 再学習モデル (champion) の管理。DESIGN 決定 12 の OPS 足場。

owner 凍結の 2 層構成 (2026-07-16):
  layer 1: **daily champion** — 毎営業日 as-of 再学習。レシピ (特徴量・LGBM パラメータ・
           セル h5s×m3.0×τ0.70) は凍結し、学習データ窓だけがスライドする。将来のライブ層。
  layer 2: **frozen shadow_h5_m30** — fill 較正専用 (`calibration.py`)。役割は不変で、
           daily champion の導入によって再採点・置換されない。

ランタイム既定は従来どおり frozen shadow を読む (bit-identical)。
env `SCALP_DAILY_MODEL=1` のときだけ `artifacts/calibration/champion.json` を解決して
daily champion を載せる。解決・検証のどこで失敗しても shadow へフォールバックし、
08:45 の起動を絶対に殺さない (警告ログのみ)。

champion.json (nightly_retrain.py が原子的に更新):
  {"model_dir", "model_sha256", "promoted_at", "train_days", "previous"}
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from scalp_agent.config import OOS_DAYS
from scalp_agent.runtime import calibration

DAILY_MODEL_ENV = "SCALP_DAILY_MODEL"

_REPO = Path(__file__).resolve().parents[3]
DAILY_ROOT = _REPO / "artifacts" / "calibration" / "daily"
CHAMPION_PATH = _REPO / "artifacts" / "calibration" / "champion.json"

# レシピ凍結: 学習日数は frozen recipe と同じ本数のままスライドさせる
N_TRAIN_DAYS = len(calibration.CAL_TRAIN_DAYS)
KEEP_DAILY = 10  # daily モデルの保持世代数 (それより古いものだけ prune)

DAILY_TAGS = {
    "calibration_only": True,  # paper 出力の判定・台帳利用禁止は shadow と同じ
    "policy": "daily_champion_h5_m30_tau070",
    "purpose": ("point-in-time 日次再学習 champion (DESIGN 決定 12)。"
                "レシピ凍結・データ窓のみスライド。判定・台帳・セル選択に使用禁止"),
    "model_layer": "daily_champion",
}


def daily_model_enabled() -> bool:
    return os.environ.get(DAILY_MODEL_ENV) == "1"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_daily_train_days(days: list[str] | tuple[str, ...]) -> None:
    """OOS 封印日 (`config.OOS_DAYS`) の使用をコードで拒否する。

    `assert_days_role` の許可集合は凍結レシピの日付に固定されているため、
    スライド窓の新日付には適用できない。ここでは封印 (OOS に触れない) という
    ガードの意味論だけを弱めずに引き継ぐ。
    """
    bad = set(days) & set(OOS_DAYS)
    if bad:
        raise AssertionError(
            f"OOS 封印日 {sorted(bad)} を daily 再学習に使うことは禁止 (IS/OOS 凍結・決定 12)")


def select_train_days(day_t: str, eligible_days: list[str]) -> list[str]:
    """day_t で終わる直近 N_TRAIN_DAYS 日の as-of 学習窓を返す。

    eligible_days は呼び出し側が「実録画あり・OOS でない・健全」まで絞った昇順リスト。
    窓が組めなければ ValueError (呼び出し側で EXIT 3 = champion 継続にする)。
    """
    if day_t in OOS_DAYS:
        raise ValueError(f"{day_t} は OOS 封印日 — daily 再学習の対象にできない")
    cand = sorted(d for d in eligible_days if d <= day_t)
    if not cand or cand[-1] != day_t:
        raise ValueError(f"{day_t} が eligible な録画日に含まれない: {cand[-3:]}")
    if len(cand) < N_TRAIN_DAYS:
        raise ValueError(
            f"as-of 窓に必要な {N_TRAIN_DAYS} 日が揃わない (eligible={cand})")
    window = cand[-N_TRAIN_DAYS:]
    assert_daily_train_days(window)
    return window


def champion_meta_expected() -> dict:
    """champion meta が現行コードと一致すべき不変キー (train_days は除く)。"""
    base = calibration.model_meta()
    return {k: base[k] for k in
            ("horizon_s", "mult", "tau", "config_hash", "feature_schema_hash")}


def load_champion():
    """champion.json を解決して booster をロードする。失敗は例外 (呼び出し側で fallback)。

    検査: pointer 存在 → model_dir/model.txt/meta.json 存在 → sha256 一致 →
    meta 不変キー (horizon/mult/tau/config_hash/feature_schema_hash) 一致。
    """
    import lightgbm as lgb

    if not CHAMPION_PATH.exists():
        raise FileNotFoundError(f"{CHAMPION_PATH} が無い (nightly_retrain 未実行)")
    pointer = json.loads(CHAMPION_PATH.read_text(encoding="utf-8"))
    model_dir = Path(pointer["model_dir"])
    if not model_dir.is_absolute():
        model_dir = _REPO / model_dir
    model_path = model_dir / "model.txt"
    meta_path = model_dir / "meta.json"
    if not model_path.exists() or not meta_path.exists():
        raise FileNotFoundError(f"champion モデル欠落: {model_dir}")
    actual_sha = sha256_file(model_path)
    if actual_sha != pointer.get("model_sha256"):
        raise RuntimeError(
            f"champion model sha256 不一致: pointer={pointer.get('model_sha256')} "
            f"actual={actual_sha}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    expected = champion_meta_expected()
    for k, v in expected.items():
        if meta.get(k) != v:
            raise RuntimeError(
                f"champion meta 不一致: {k}: saved={meta.get(k)} expected={v}")
    booster = lgb.Booster(model_file=str(model_path))
    meta = {
        **meta,
        "model_version": actual_sha[:12],
        "model_source": "daily_champion",
        "champion_model_dir": str(model_dir),
        "promoted_at": pointer.get("promoted_at"),
    }
    return booster, meta


def load_runtime_model(log=None):
    """ランタイム (PaperTrader scorer) 用の (booster, meta)。

    - env `SCALP_DAILY_MODEL` 未設定/≠1: 従来と bit-identical に frozen shadow を返す。
    - `SCALP_DAILY_MODEL=1`: champion を試み、**あらゆる失敗**で shadow へフォールバック
      (警告ログ)。ランタイムをここで落とさない。
    """
    if daily_model_enabled():
        try:
            booster, meta = load_champion()
            if log is not None:
                log.info(f"daily champion モデルをロード: {meta['champion_model_dir']} "
                         f"(train_days={meta.get('train_days')} "
                         f"model_version={meta['model_version']})")
            return booster, meta
        except Exception as e:
            if log is not None:
                log.warning(f"SCALP_DAILY_MODEL=1 だが champion ロード失敗 — "
                            f"frozen shadow へフォールバック: {e}")
    # 既定経路: 従来 runner.py が行っていたのと同一の 2 行
    booster = calibration.load_booster()
    meta = {**calibration.model_meta(), "model_version": calibration.model_version()}
    return booster, meta

#!/usr/bin/env python3
"""
kabu STATION API — 寄付き板寄せ同業ペア乖離 180秒フェード フォワードテスト・ハーネス

このスクリプトは docs/../note「寄付き板寄せ同業ペア乖離3分回帰」の凍結仕様を、
kabu ステーションAPI（ローカル REST）経由でデモ/検証口座に実発注してフォワードテストする。

■ なぜ実発注でテストするのか（重要）
  2日プローブの +12.54bps は「出口を固定 09:00:00 で測る」計測アーティファクトだった。
  遅延寄り銘柄では固定アンカーが実質保有 0 秒になり損失が消えていた。
  実発注は timing を強制的に正直にする:
    - entry  = 各脚が実際に寄り付いた瞬間の約定値（day_open）
    - exit   = その脚の「実約定時刻 + 180 秒」の成行（固定 09:00 ではない）
  したがってこのハーネスの記録は、そのままアーティファクトなしの本当の期待値になる。

■ 安全既定
  - DRY_RUN=True         … 発注 body を出力するだけで送らない（まず必ずこれで確認）
  - PORT=18081           … 検証環境（デモ）。本番 18080 は明示的に変えたときだけ。
  - 時間ガード           … 寄り付き前後の窓でしか動かない
  - 最終フラット         … ハード停止時刻に残ポジを全成行返済

■ 使い方は同ディレクトリの README_pair_open_fade.md を参照。
"""

from __future__ import annotations
import json, time, sys, os, datetime as dt, urllib.request, urllib.error
from dataclasses import dataclass, field, asdict

# ==========================================================================
# CONFIG  — ★ここは公式リファレンス（http://localhost:1808x/kabusapi/swagger）で必ず突き合わせる
# ==========================================================================

DRY_RUN         = True          # True: 発注しない（body を印字するだけ）。実発注は False。
PORT            = 18081         # 18081=検証環境(デモ) / 18080=本番。デモで確認するまで 18080 にしない。
HOST            = "localhost"
API_PASSWORD    = os.environ.get("KABU_API_PASSWORD", "")   # 環境変数で渡す（コードに書かない）

# --- 発注パラメータ（証券会社仕様。デモ口座で通る値を swagger で確認して調整）---
EXCHANGE        = 1             # 1=東証
SECURITY_TYPE   = 1             # 1=株式
ACCOUNT_TYPE    = 4             # 2=一般 / 4=特定 / 12=法人
CASH_MARGIN_NEW = 2            # 2=信用新規（ペアは売り脚があるので信用が必要。現物では空売り不可）
CASH_MARGIN_CLOSE = 3          # 3=信用返済
MARGIN_TRADE_TYPE = 1          # 1=制度信用 / 2=一般(長期) / 3=一般(デイトレ)。デモ口座が持つ区分に合わせる
DELIV_TYPE_CLOSE = 2           # 返済時の受渡区分（0=指定なし/2=お預り金）。要確認
FUND_TYPE_MARGIN = "11"        # 信用の資産区分。要確認
FRONT_MOO       = 13           # 寄成（寄付成行）= entry。要確認（13=寄成）
FRONT_MARKET    = 10           # 成行 = exit。要確認（10=成行）

QTY_PER_LEG     = 100          # 各脚の株数（売買単位）。bps 評価なので固定で良い。
HOLD_SECONDS    = 180          # 出口ホライズン（凍結仕様=180秒固定）

# --- 時間ガード（すべて実行マシンの現地時刻=JST 前提）---
SIGNAL_CUTOFF   = dt.time(8, 59, 30)   # これより前の最終指標midをシグナルに使う（凍結）
ENTRY_SEND_AT   = dt.time(8, 59, 35)   # 寄成を送る時刻（cutoff 直後 / 09:00 前）
PREOPEN_POLL_FROM = dt.time(8, 55, 0)  # 指標板のポーリング開始
ENTRY_WATCH_UNTIL = dt.time(9, 15, 0)  # これ以降は新規約定を待たない（遅延寄り上限）
HARD_FLATTEN_AT   = dt.time(9, 25, 0)  # 残ポジを全成行返済して終了

# --- 凍結ペア（note の凍結仕様そのまま。閾値なし・全ペア毎日建てる）---
PAIRS = [
    ("5801", "5802"), ("5801", "5803"), ("5802", "5803"),
    ("8306", "8316"), ("8306", "8411"), ("8306", "7186"), ("8306", "7182"),
    ("7011", "7012"), ("7011", "7013"),
    ("8035", "6857"), ("8035", "6920"), ("8035", "6146"), ("8035", "285A"),
    ("6857", "6920"), ("6501", "6503"),
]
SYMBOLS = sorted({s for p in PAIRS for s in p})

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

# ==========================================================================
# ログ
# ==========================================================================

class JsonlLog:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.f = open(path, "a", encoding="utf-8")
    def __call__(self, event, **kw):
        rec = {"ts": dt.datetime.now().isoformat(timespec="milliseconds"), "event": event, **kw}
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n"); self.f.flush()
        print(f"[{rec['ts'][11:23]}] {event}: " +
              " ".join(f"{k}={v}" for k, v in kw.items() if k not in ("body",)))
    def close(self): self.f.close()

# ==========================================================================
# kabu STATION API クライアント（薄い REST ラッパ）
# ==========================================================================

class KabuClient:
    def __init__(self, host, port, log):
        self.base = f"http://{host}:{port}/kabusapi"
        self.token = None
        self.log = log

    def _req(self, method, path, body=None, auth=True):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("X-API-KEY", self.token)
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            payload = e.read().decode()
            raise RuntimeError(f"HTTP {e.code} {method} {path}: {payload}") from None

    def authenticate(self):
        if not API_PASSWORD:
            raise SystemExit("KABU_API_PASSWORD 環境変数が未設定です。README 参照。")
        res = self._req("POST", "/token", {"APIPassword": API_PASSWORD}, auth=False)
        self.token = res["Token"]
        self.log("auth_ok", token_prefix=self.token[:6])

    def board(self, symbol):
        return self._req("GET", f"/board/{symbol}@{EXCHANGE}")

    def positions(self):
        # 信用建玉のみ
        return self._req("GET", "/positions?product=2")

    def send_entry(self, symbol, side, qty):
        """寄成（寄付成行）で新規建て。side: '1'=売, '2'=買"""
        body = {
            "Symbol": symbol, "Exchange": EXCHANGE, "SecurityType": SECURITY_TYPE,
            "Side": side, "CashMargin": CASH_MARGIN_NEW,
            "MarginTradeType": MARGIN_TRADE_TYPE,
            "AccountType": ACCOUNT_TYPE, "DelivType": 0, "FundType": FUND_TYPE_MARGIN,
            "Qty": qty, "FrontOrderType": FRONT_MOO, "Price": 0, "ExpireDay": 0,
        }
        return self._send("entry", symbol, body)

    def send_close(self, symbol, side, qty, hold_id):
        """成行返済。side は返済する建玉と反対（買建の返済は Side='1'=売）。"""
        body = {
            "Symbol": symbol, "Exchange": EXCHANGE, "SecurityType": SECURITY_TYPE,
            "Side": side, "CashMargin": CASH_MARGIN_CLOSE,
            "MarginTradeType": MARGIN_TRADE_TYPE,
            "AccountType": ACCOUNT_TYPE, "DelivType": DELIV_TYPE_CLOSE,
            "FundType": FUND_TYPE_MARGIN,
            "Qty": qty, "FrontOrderType": FRONT_MARKET, "Price": 0, "ExpireDay": 0,
            "ClosePositions": [{"HoldID": hold_id, "Qty": qty}],
        }
        return self._send("close", symbol, body)

    def _send(self, kind, symbol, body):
        if DRY_RUN:
            self.log(f"DRYRUN_{kind}", symbol=symbol, body=body)
            return {"Result": 0, "OrderId": f"DRY-{kind}-{symbol}"}
        res = self._req("POST", "/sendorder", body)
        self.log(f"sent_{kind}", symbol=symbol, order_id=res.get("OrderId"),
                 result=res.get("Result"), body=body)
        return res

# ==========================================================================
# 板ヘルパ
# ==========================================================================

def indicative_mid(board):
    """寄り前の指標mid。特別気配でも Bid/Ask に指標値が入る。両方 >0 のとき (bid+ask)/2。"""
    b = board.get("BidPrice") or 0
    a = board.get("AskPrice") or 0
    if b > 0 and a > 0:
        return (a + b) / 2.0
    # 片側特別気配のときは残った方を使う（保守的でないが指標が無いよりまし。無ければ None）
    return (a or b) or None

def realized_mid(board):
    b = board.get("BidPrice") or 0
    a = board.get("AskPrice") or 0
    if b > 0 and a > 0:
        return (a + b) / 2.0, (a - b) / 2.0 / ((a + b) / 2.0)   # mid, half_spread_frac
    return None, None

# ==========================================================================
# 時間ユーティリティ
# ==========================================================================

def now(): return dt.datetime.now()
def at_today(t: dt.time): return dt.datetime.combine(dt.date.today(), t)
def sleep_until(t: dt.time):
    target = at_today(t)
    while now() < target:
        time.sleep(min(1.0, (target - now()).total_seconds()))

# ==========================================================================
# 本体
# ==========================================================================

@dataclass
class Leg:
    symbol: str
    side_entry: str        # '1'売 / '2'買
    ret_bps: float
    order_id: str = ""
    hold_id: str = ""
    entry_px: float = 0.0
    entry_time: str = ""
    exit_deadline: dt.datetime | None = None
    exit_sent: bool = False
    exit_px: float = 0.0
    exit_time: str = ""
    exit_hs_bps: float = 0.0

def compute_signals(client, log):
    """PREOPEN 窓で指標板をポーリングし、cutoff 直前の最終 mid で ret を確定。"""
    prev_close = {}
    last_mid = {}
    log("preopen_poll_start", from_=str(PREOPEN_POLL_FROM), cutoff=str(SIGNAL_CUTOFF))
    while now().time() < SIGNAL_CUTOFF:
        for s in SYMBOLS:
            try:
                bd = client.board(s)
            except Exception as e:
                log("board_err", symbol=s, err=str(e)); continue
            pc = bd.get("PreviousClose") or bd.get("PrevClose") or 0
            if pc: prev_close[s] = pc
            m = indicative_mid(bd)
            if m: last_mid[s] = m
        time.sleep(2.0)   # 板ポーリング間隔（レート制限に配慮）

    signals = {}
    for s in SYMBOLS:
        if s in last_mid and prev_close.get(s):
            signals[s] = (last_mid[s] - prev_close[s]) / prev_close[s] * 1e4  # bps
    log("signals", **{s: round(v, 1) for s, v in signals.items()})
    return signals

def build_legs(signals, log):
    """各ペアで ret の高い脚を売り・低い脚を買い（relative-gap fade）。"""
    legs: dict[str, Leg] = {}
    for a, b in PAIRS:
        if a not in signals or b not in signals:
            log("pair_skip_no_signal", pair=f"{a}>{b}"); continue
        short, long = (a, b) if signals[a] >= signals[b] else (b, a)
        for sym, side in ((short, "1"), (long, "2")):
            # 同一銘柄が複数ペアに出るときは合算方向を持つが、
            # デモ簡素化のため「最初に決まった方向」で1建玉に固定（重複銘柄は建て増ししない）
            if sym not in legs:
                legs[sym] = Leg(symbol=sym, side_entry=side, ret_bps=signals[sym])
    log("legs_planned", n=len(legs),
        detail={s: ("SELL" if l.side_entry == "1" else "BUY") for s, l in legs.items()})
    return legs

def send_entries(client, legs, log):
    sleep_until(ENTRY_SEND_AT)
    for s, leg in legs.items():
        res = client.send_entry(s, leg.side_entry, QTY_PER_LEG)
        leg.order_id = str(res.get("OrderId", ""))
        time.sleep(0.3)   # 連続発注のレート制限に配慮

def opposite(side): return "1" if side == "2" else "2"

def watch_fills_and_exit(client, legs, log):
    """建玉の約定を検知→実約定時刻+180秒で成行返済。遅延寄りは脚ごとに独立処理。"""
    while now().time() < HARD_FLATTEN_AT:
        watching_new = now().time() < ENTRY_WATCH_UNTIL and any(not l.hold_id for l in legs.values())
        pending_exit = any(l.hold_id and not l.exit_sent for l in legs.values())
        if not watching_new and not pending_exit:
            break   # 新規待ちも返済待ちも無い → 終了
        try:
            positions = client.positions() if not DRY_RUN else _dry_positions(legs)
        except Exception as e:
            log("pos_err", err=str(e)); time.sleep(1.0); continue

        for p in positions:
            sym = str(p.get("Symbol"))
            leg = legs.get(sym)
            if not leg or leg.hold_id:
                continue
            leaves = p.get("LeavesQty") or p.get("HoldQty") or 0
            if leaves and float(leaves) > 0:
                leg.hold_id = str(p.get("HoldID") or p.get("ExecutionID") or "")
                leg.entry_px = float(p.get("Price") or 0)
                # 約定時刻: ExecutionDay があればそれ、無ければ現在時刻で近似
                leg.entry_time = str(p.get("ExecutionDay") or now().isoformat())
                leg.exit_deadline = now() + dt.timedelta(seconds=HOLD_SECONDS)
                log("entry_filled", symbol=sym, px=leg.entry_px, hold_id=leg.hold_id,
                    exit_at=leg.exit_deadline.strftime("%H:%M:%S"))

        # 出口期限を過ぎた脚を返済
        for s, leg in legs.items():
            if leg.hold_id and not leg.exit_sent and leg.exit_deadline and now() >= leg.exit_deadline:
                try:
                    bd = client.board(s); mid, hs = realized_mid(bd)
                except Exception:
                    mid, hs = None, None
                res = client.send_close(s, opposite(leg.side_entry), QTY_PER_LEG, leg.hold_id)
                leg.exit_sent = True
                leg.exit_px = mid or 0.0
                leg.exit_hs_bps = (hs or 0.0) * 1e4
                leg.exit_time = now().isoformat()
                hold = (now() - (leg.exit_deadline - dt.timedelta(seconds=HOLD_SECONDS))).total_seconds()
                log("exit_sent", symbol=s, exit_mid=round(leg.exit_px, 1),
                    hs_bps=round(leg.exit_hs_bps, 2), hold_s=round(hold, 0))
        time.sleep(1.0)

def _dry_positions(legs):
    """DRY_RUN 用: entry を送った脚を即約定したことにして exit ロジックを通す（板は実データを取る）。"""
    out = []
    for s, leg in legs.items():
        if leg.order_id and not leg.hold_id:
            out.append({"Symbol": s, "LeavesQty": QTY_PER_LEG,
                        "HoldID": f"DRYHOLD-{s}", "Price": 0})
    return out

def final_flatten(client, legs, log):
    sleep_until(HARD_FLATTEN_AT)
    for s, leg in legs.items():
        if leg.hold_id and not leg.exit_sent:
            client.send_close(s, opposite(leg.side_entry), QTY_PER_LEG, leg.hold_id)
            leg.exit_sent = True
            log("force_flatten", symbol=s)

def summarize(legs, log):
    """脚ごとの実現 bps を集計。ペア単位 = 売脚return + 買脚return（note と同じ）。"""
    rows = []
    for s, leg in legs.items():
        if leg.entry_px and leg.exit_px:
            if leg.side_entry == "2":   # 買い
                ret = (leg.exit_px - leg.entry_px) / leg.entry_px * 1e4
            else:                        # 売り
                ret = (leg.entry_px - leg.exit_px) / leg.entry_px * 1e4
            net = ret - leg.exit_hs_bps
            rows.append((s, ret, leg.exit_hs_bps, net))
            log("leg_result", symbol=s, gross_bps=round(ret, 1),
                fric_bps=round(leg.exit_hs_bps, 2), net_bps=round(net, 1))
    if rows:
        import statistics
        log("SUMMARY", n_legs=len(rows),
            mean_net_bps=round(statistics.mean(r[3] for r in rows), 2))
    else:
        log("SUMMARY", note="実約定なし（DRY_RUN では entry_px=0 のため bps は出ない）")

def main():
    stamp = dt.date.today().isoformat()
    log = JsonlLog(os.path.join(LOG_DIR, f"pair_open_fade_{stamp}.jsonl"))
    log("start", dry_run=DRY_RUN, port=PORT, pairs=len(PAIRS), symbols=len(SYMBOLS))
    client = KabuClient(HOST, PORT, log)
    try:
        client.authenticate()
        signals = compute_signals(client, log)
        legs = build_legs(signals, log)
        if not legs:
            log("abort", reason="no legs"); return
        send_entries(client, legs, log)
        watch_fills_and_exit(client, legs, log)
        final_flatten(client, legs, log)
        summarize(legs, log)
    except Exception as e:
        log("FATAL", err=str(e))
        raise
    finally:
        log("end"); log.close()

if __name__ == "__main__":
    main()

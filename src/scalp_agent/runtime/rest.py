"""kabusapi REST (read-only 経路のみ): token 発行・PUSH 銘柄登録/解除。

使うエンドポイントは POST /token・PUT /register・PUT /unregister のみ
(GET /board も使わず PUSH のみで駆動。発注・取消系は一切呼ばない・書かない)。

token 単一性 (SKILL S4): 新規発行は既存 token を失効させる。発行後は
TOKEN_SHARE_PATH に書き、他プロセスは場中に POST /token を発行せず
このファイルを読むこと (参照実装と同じ機械全体の規約)。

共存契約 (2026-07-16 owner 確定): 同一 kabu 本体を agree_biggap 実弾トレーダーと
共有する。register 50 枠は板読み 40 / agree_biggap 10 に分割し、解除は必ず
自分が登録した銘柄の指定解除 (PUT /unregister) で行う。全銘柄一括解除の
エンドポイントは他プロセスの登録を消すため両者とも禁止
(禁止文字列は grep 監査対象のためリテラルでは書かない)。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

BOARD_DIR = os.environ.get("KABU_BOARD_DIR", r"S:/jp/stocks_board_kabu_push")
TOKEN_SHARE_PATH = os.path.join(BOARD_DIR, "current_token.json")
# 共存契約: 自分が register している銘柄を公開する。register は機械全体で参照
# カウントが無いため、同居プロセス (agree_biggap) はこのファイルの銘柄を
# 「解除禁止」として扱う (重複銘柄を解除されると PUSH が silent に止まる)
REGISTERED_SHARE_PATH = os.path.join(BOARD_DIR, "current_registered.json")
TOKEN_REFETCH_BACKOFF_S = 30.0  # POST /token の最小間隔 (連打禁止)


def mask_token(tok: str) -> str:
    if not tok:
        return "***"
    return "***" + tok[-4:] if len(tok) >= 4 else "***"


def _rest_call(url: str, *, method: str, body: dict | None, headers: dict) -> dict:
    import urllib.request
    data = None
    hdr = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def fetch_token(base: str, api_password: str) -> str:
    """POST /token — トークンを取得しメモリ保持のみ (R3/R10)。"""
    payload = _rest_call(
        f"{base}/token", method="POST",
        body={"APIPassword": api_password}, headers={},
    )
    code = payload.get("ResultCode")
    tok = payload.get("Token")
    if code not in (0, None) or not tok:
        raise RuntimeError(f"token 取得失敗 ResultCode={code} msg={payload.get('Message')}")
    return tok


def _regist_list(payload: dict) -> list[str]:
    """RegistList は「局側で今 登録されている銘柄」の全量スナップショット (R6)。"""
    return [str(x.get("Symbol")) for x in (payload.get("RegistList") or [])
            if x.get("Symbol")]


def register_symbols(base: str, token: str, codes: list[str]) -> list[str]:
    """PUT /register — 1 コールで一括登録し RegistList 全量を返す (R6)。

    RegistList は機械全体のスナップショットなので、同居プロセスの登録分も
    混ざって返る。呼び出し側は件数でなく「自分の codes が居るか」で検証する。
    """
    body = {"Symbols": [{"Symbol": c, "Exchange": 1} for c in codes]}
    payload = _rest_call(
        f"{base}/register", method="PUT", body=body,
        headers={"X-API-KEY": token},
    )
    return _regist_list(payload)


def unregister_symbols(base: str, token: str, codes: list[str]) -> list[str]:
    """PUT /unregister — 指定銘柄だけ解除し、残った登録銘柄の全量を返す (R6)。

    共存契約: 全銘柄一括解除エンドポイントは同居プロセスの登録を消すため使わない。
    """
    if not codes:
        return []
    body = {"Symbols": [{"Symbol": c, "Exchange": 1} for c in codes]}
    payload = _rest_call(
        f"{base}/unregister", method="PUT", body=body,
        headers={"X-API-KEY": token},
    )
    return _regist_list(payload)


class TokenManager:
    """token の取得・共有ファイル更新・register を面倒みる (参照実装の移植)。"""

    def __init__(self, base: str, api_password: str, log):
        self.base = base
        self.api_password = api_password
        self.log = log
        self.token: str | None = None
        self._last_token_mono: float | None = None

    def fetch_and_share(self) -> str:
        now = time.monotonic()
        if self._last_token_mono is not None:
            wait = TOKEN_REFETCH_BACKOFF_S - (now - self._last_token_mono)
            if wait > 0:
                self.log.info(f"token 再取得 backoff: {wait:.0f}s 待機")
                time.sleep(wait)
        tok = fetch_token(self.base, self.api_password)
        self._last_token_mono = time.monotonic()
        try:
            tmp = TOKEN_SHARE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"token": tok, "issued_at": datetime.now().isoformat()}, fh)
            os.replace(tmp, TOKEN_SHARE_PATH)
            self.log.info(f"token OK: {mask_token(tok)} (共有ファイル更新: {TOKEN_SHARE_PATH})")
        except Exception as e:
            self.log.warning(f"token 共有ファイル書き込み失敗 (継続): {e}")
        self.token = tok
        return tok

    def ensure_registered(self, codes: list[str], force_new_token: bool = False) -> None:
        """token 取得 (未取得/強制時) → 自ユニバース unregister → register。

        401 は token 再取得後 1 回再試行、4002006 (登録上限) は自ユニバース解除後
        1 回だけ再試行 (kabusapi SKILL R6/R7 準拠)。共存契約により解除は常に
        自分の universe 指定のみ。4002006 が自ユニバース解除でも解けない場合は
        同居プロセスが枠契約 (板読み 40 / agree_biggap 10) を超過している。
        """
        import urllib.error

        def _unregister_own() -> None:
            try:
                unregister_symbols(self.base, self.token, codes)
            except Exception as e:
                # フレッシュセッション (何も登録されていない) では 400 が返るのが正常
                self.log.info(f"unregister(自ユニバース) スキップ (未登録なら正常): {e}")

        if force_new_token:
            self.token = None
        if self.token is None:
            self.fetch_and_share()
            _unregister_own()
        try:
            registered = register_symbols(self.base, self.token, codes)
        except urllib.error.HTTPError as he:
            if he.code == 401:
                self.log.warning("register 401 (auth) — token 再取得 + 自ユニバース解除 + 再登録")
                self.fetch_and_share()
                _unregister_own()
                registered = register_symbols(self.base, self.token, codes)
            else:
                body = ""
                try:
                    body = he.read().decode("utf-8")
                except Exception:
                    pass
                if "4002006" in body:  # 登録上限 → 自ユニバース解除して 1 回だけ再試行
                    self.log.warning("register 4002006 (上限) — 自ユニバース解除後に再試行 "
                                     "(解けなければ同居プロセスの枠超過を疑う)")
                    unregister_symbols(self.base, self.token, codes)
                    registered = register_symbols(self.base, self.token, codes)
                else:
                    raise
        missing = [c for c in codes if c not in set(registered)]
        if missing:
            self.log.warning(f"register 未登録 {len(missing)}/{len(codes)} 銘柄: "
                             f"{','.join(missing[:10])} (機械全体 {len(registered)}/50)")
        else:
            self.log.info(f"register OK: {len(codes)} 銘柄 (機械全体 {len(registered)}/50)")
        try:
            tmp = REGISTERED_SHARE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"date": datetime.now().date().isoformat(),
                           "codes": sorted(codes)}, fh)
            os.replace(tmp, REGISTERED_SHARE_PATH)
        except Exception as e:
            self.log.warning(f"登録銘柄 共有ファイル書き込み失敗 (継続): {e}")

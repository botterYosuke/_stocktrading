"""kabusapi REST (read-only 経路のみ): token 発行・PUSH 銘柄登録/解除。

使うエンドポイントは POST /token・PUT /register・PUT /unregister/all のみ
(GET /board も使わず PUSH のみで駆動。発注・取消系は一切呼ばない・書かない)。

token 単一性 (SKILL S4): 新規発行は既存 token を失効させる。発行後は
TOKEN_SHARE_PATH に書き、他プロセスは場中に POST /token を発行せず
このファイルを読むこと (参照実装と同じ機械全体の規約)。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

BOARD_DIR = os.environ.get("KABU_BOARD_DIR", r"S:/jp/stocks_board_kabu_push")
TOKEN_SHARE_PATH = os.path.join(BOARD_DIR, "current_token.json")
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


def register_symbols(base: str, token: str, codes: list[str]) -> int:
    """PUT /register — 1 コールで一括登録し RegistList 件数を返す (R6)。"""
    body = {"Symbols": [{"Symbol": c, "Exchange": 1} for c in codes]}
    payload = _rest_call(
        f"{base}/register", method="PUT", body=body,
        headers={"X-API-KEY": token},
    )
    return len(_regist_list(payload))


def unregister_all(base: str, token: str) -> None:
    """PUT /unregister/all — 全銘柄登録解除 (枠を空ける・R6)。"""
    _rest_call(
        f"{base}/unregister/all", method="PUT", body={},
        headers={"X-API-KEY": token},
    )


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
        """token 取得 (未取得/強制時) → unregister/all → register。

        401 は token 再取得後 1 回再試行、4002006 (登録上限) は unregister/all 後
        1 回再試行 (kabusapi SKILL R6/R7 準拠・参照実装と同じ)。
        """
        import urllib.error
        if force_new_token:
            self.token = None
        if self.token is None:
            self.fetch_and_share()
            try:
                unregister_all(self.base, self.token)
            except Exception as e:
                self.log.warning(f"unregister/all 失敗 (継続): {e}")
        try:
            n = register_symbols(self.base, self.token, codes)
        except urllib.error.HTTPError as he:
            if he.code == 401:
                self.log.warning("register 401 (auth) — token 再取得 + unregister/all + 再登録")
                self.fetch_and_share()
                try:
                    unregister_all(self.base, self.token)
                except Exception as e:
                    self.log.warning(f"unregister/all 失敗 (継続): {e}")
                n = register_symbols(self.base, self.token, codes)
            else:
                body = ""
                try:
                    body = he.read().decode("utf-8")
                except Exception:
                    pass
                if "4002006" in body:  # 登録上限 → 全解除して 1 回だけ再試行
                    self.log.warning("register 4002006 (上限) — unregister/all 後に再試行")
                    unregister_all(self.base, self.token)
                    n = register_symbols(self.base, self.token, codes)
                else:
                    raise
        if n < len(codes):
            self.log.warning(f"register 件数 {n} < universe {len(codes)} (一部未登録)")
        else:
            self.log.info(f"register OK: {n} 銘柄")

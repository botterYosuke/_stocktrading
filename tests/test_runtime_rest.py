"""TokenManager の共存契約 (2026-07-16) の回帰テスト。

同一 kabu 本体を agree_biggap と共有するため:
- 解除は自ユニバース指定の /unregister のみ (unregister/all を呼ばない)
- register 検証は RegistList 件数でなく「自 universe が全員居るか」
  (RegistList は機械全体スナップショットで同居プロセスの登録が混ざる)
- token 発行時は共有ファイル TOKEN_SHARE_PATH を原子的更新
"""
import json
import logging
import urllib.error

import pytest

from scalp_agent.runtime import rest


class FakeApi:
    """_rest_call を置き換える機械全体レジストリの疑似 kabu 局。"""

    def __init__(self, cotenant: list[str] | None = None,
                 fail_first_register_with: int | None = None):
        self.registered: list[str] = list(cotenant or [])
        self.calls: list[tuple[str, str]] = []
        self._fail_code = fail_first_register_with
        self.token_serial = 0

    def __call__(self, url, *, method, body, headers):
        # url は f"{base}{path}" 形式 (base="http://fake")
        path = url.replace("http://fake", "")
        self.calls.append((method, path))
        if path == "/token":
            self.token_serial += 1
            return {"ResultCode": 0, "Token": f"tok{self.token_serial:04d}"}
        if path == "/register":
            if self._fail_code is not None:
                code = self._fail_code
                self._fail_code = None
                body_bytes = json.dumps({"Code": code}).encode()
                raise urllib.error.HTTPError(url, 401 if code == 401 else 400,
                                             "err", hdrs=None,
                                             fp=__import__("io").BytesIO(body_bytes))
            for s in body["Symbols"]:
                if s["Symbol"] not in self.registered:
                    self.registered.append(s["Symbol"])
            return {"RegistList": [{"Symbol": c} for c in self.registered]}
        if path == "/unregister":
            drop = {s["Symbol"] for s in body["Symbols"]}
            self.registered = [c for c in self.registered if c not in drop]
            return {"RegistList": [{"Symbol": c} for c in self.registered]}
        raise AssertionError(f"未許可エンドポイント呼び出し: {method} {path}")


@pytest.fixture
def token_share(tmp_path, monkeypatch):
    share = tmp_path / "current_token.json"
    monkeypatch.setattr(rest, "TOKEN_SHARE_PATH", str(share))
    monkeypatch.setattr(rest, "REGISTERED_SHARE_PATH",
                        str(tmp_path / "current_registered.json"))
    return share


def _manager(fake, monkeypatch):
    monkeypatch.setattr(rest, "_rest_call", fake)
    return rest.TokenManager("http://fake", "pw", logging.getLogger("test"))


def test_ensure_registered_never_calls_unregister_all(token_share, monkeypatch):
    fake = FakeApi(cotenant=["9999"])
    tm = _manager(fake, monkeypatch)
    tm.ensure_registered(["7203", "9984"])
    paths = [p for _, p in fake.calls]
    assert "/unregister/all" not in paths
    assert paths.count("/token") == 1
    # 同居プロセスの登録 (9999) は無傷
    assert "9999" in fake.registered
    # 自分の登録銘柄を共有ファイルに公開 (agree_biggap の解除禁止リスト)
    shared = json.loads((token_share.parent / "current_registered.json")
                        .read_text(encoding="utf-8"))
    assert shared["codes"] == ["7203", "9984"]
    assert shared["date"]


def test_register_ok_with_cotenant_registrations(token_share, monkeypatch, caplog):
    """RegistList に他者分が混ざっても自 universe 全員が居れば OK 判定。"""
    fake = FakeApi(cotenant=["9999", "8888"])
    tm = _manager(fake, monkeypatch)
    with caplog.at_level(logging.INFO):
        tm.ensure_registered(["7203", "9984"])
    assert any("register OK: 2 銘柄" in r.message for r in caplog.records)
    assert not any("未登録" in r.message for r in caplog.records)


def test_register_missing_own_code_warns_even_if_total_is_large(
        token_share, monkeypatch, caplog):
    """機械全体の件数が universe 数を超えていても自銘柄欠落は警告する
    (旧実装の件数比較では他者分がマスクして見逃す)。"""
    fake = FakeApi(cotenant=["9999", "8888", "7777"])

    real_register = fake.__call__

    def drop_7203(url, *, method, body, headers):
        if url.endswith("/register"):
            body = {"Symbols": [s for s in body["Symbols"] if s["Symbol"] != "7203"]}
        return real_register(url, method=method, body=body, headers=headers)

    tm = _manager(drop_7203, monkeypatch)
    with caplog.at_level(logging.WARNING):
        tm.ensure_registered(["7203", "9984"])
    assert any("未登録 1/2" in r.message and "7203" in r.message
               for r in caplog.records)


def test_register_401_refetches_token_and_updates_share_file(
        token_share, monkeypatch):
    fake = FakeApi(fail_first_register_with=401)
    tm = _manager(fake, monkeypatch)
    monkeypatch.setattr(rest, "TOKEN_REFETCH_BACKOFF_S", 0.0)
    tm.ensure_registered(["7203"])
    paths = [p for _, p in fake.calls]
    assert paths.count("/token") == 2          # 初回 + 401 再発行
    assert "/unregister/all" not in paths
    shared = json.loads(token_share.read_text(encoding="utf-8"))
    assert shared["token"] == tm.token == "tok0002"


def test_register_4002006_unregisters_own_universe_only(token_share, monkeypatch):
    fake = FakeApi(cotenant=["9999"], fail_first_register_with=4002006)
    tm = _manager(fake, monkeypatch)
    tm.ensure_registered(["7203"])
    paths = [p for _, p in fake.calls]
    assert "/unregister/all" not in paths
    assert paths.count("/unregister") == 2     # token 発行直後 + 4002006 リトライ前
    assert "9999" in fake.registered           # 他者分は無傷
    assert "7203" in fake.registered

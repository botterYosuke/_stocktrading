"""read-only 不変条件の grep 監査。

runtime パッケージは発注・取消系エンドポイントを一切呼ばない・import しない・
URL 文字列としても書かない (参照実装 kabu_board_paper_trader.py と同じ保証)。
禁止文字列はこのテスト内でも連結で構築し、リテラルとして書かない。
"""
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "src" / "scalp_agent" / "runtime"

FORBIDDEN = [
    "send" + "order",
    "send" + "oco",
    "cancel" + "order",
    "/wallet/",
    "/positions",
    "/orders",
]


def test_runtime_sources_contain_no_order_endpoints():
    files = sorted(RUNTIME_DIR.glob("*.py"))
    assert files, "runtime パッケージが見つからない"
    for p in files:
        text = p.read_text(encoding="utf-8").lower()
        for word in FORBIDDEN:
            assert word not in text, f"{p.name} に禁止文字列 {word!r} が含まれる"


def test_runtime_allowed_endpoints_only():
    """URL パスは /token・/register・/unregister/all・/websocket のみ。"""
    allowed = {"/token", "/register", "/unregister/all", "/kabusapi"}
    import re
    for p in sorted(RUNTIME_DIR.glob("*.py")):
        text = p.read_text(encoding="utf-8")
        for m in re.finditer(r'f?"[^"]*\{base\}(/[a-z/]+)"', text):
            assert m.group(1) in allowed, f"{p.name}: 未許可エンドポイント {m.group(1)}"

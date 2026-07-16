"""統合ランタイム: 板 PUSH 録画 + ペーパートレード (仮想約定) の単一プロセス実装。

移植元は `_bellwether/scripts/kabu_board_paper_trader.py` (凍結・参照のみ)。
仮想約定・特徴量・決定グリッドはオフライン正本 (labels/execution/features/sessions)
と同じ規則を逐次形で実装し、等価性は tests/ の回帰テストで固定する。

read-only 不変条件: 本パッケージは発注・取消系エンドポイントを一切呼ばない・
import しない・URL 文字列としても書かない (tests/test_runtime_readonly.py が監査)。
"""

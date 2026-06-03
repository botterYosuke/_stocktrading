# CLAUDE.md — sacrificial-lamb

## 実装完了後の必須アクション

実装・修正・フェーズが完了したとき（「完成した」「done」「finished」「実装した」「修正した」「コミットした」「マージする」「フェーズ終了」などのフレーズが出たとき）は、**必ず** `post-impl-skill-update` スキルを発動すること。

`post-impl-skill-update`スキルは：
- 今回使用したスキルの振り返り
- 使えばよかった（使い忘れた）スキルの特定
- スキルの description（トリガー条件）や内容の改善

を行い、スキルエコシステムを育てる。

## Claude Code の記録責務

戦略研究の記録規約（3 project 構成 / write_note・edit_note の使い分け / 読み出し
規約 / scope 外）は **[AGENTS.md §Research Records](../AGENTS.md#research-records-basic-memory-mcp)
が正典**。Claude Code はその規約に従いつつ、以下を必ず実行する:

- **司令塔ループ §6**: `strategy-commander.md` / `strategy-command/SKILL.md` の MCP
  呼び出しに従い、bs-wiki log note と bs-docs proposals を更新する
- **実装者**: `blacksheep-replay/SKILL.md` §17 (B-5 / B-6) の MCP 経由記録に従う
- **禁止**: 旧 `wiki/log.md` への直接 prepend（monolithic log は廃止）、`docs/plan/*.md`
  への raw Write/Edit（必ず edit_note 経由）
- **空応答を記録喪失と即断しない**: BM MCP の list_directory / recent_activity が空でも、
  索引未構築の可能性がある。`basic-memory reindex --full` で復旧してから判断すること
  （2026-05-29 に同じ誤診が発生）。詳細は AGENTS.md §Research Records pitfalls。

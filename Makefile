install:
	pip install -r requirements.txt

# 単日（当日 as_of）の予測 → signals/signals_<翌営業日>.json + manifest.json
predict:
	python model_manager.py

# 日付レンジの point-in-time 生成（再開可能・モデルキャッシュ付き）
# 例: make generate START=2021-06-04 END=2021-06-08
generate:
	python daily_generator.py --start $(START) --end $(END) --out signals

# 1日分の15分パネル smoke（print のみ・canonical dir に書かない）
# 例: make panel-smoke AS_OF=2024-01-31
# （実行者は uv run python panel_smoke.py --as-of 2024-01-31 --cache-dir S:/j-quants でも可）
panel-smoke:
	python panel_smoke.py --as-of $(AS_OF)

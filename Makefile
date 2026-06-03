install:
	pip install -r requirements.txt

# 単日（当日 as_of）の予測 → signals/signals_<翌営業日>.json + manifest.json
predict:
	python model_manager.py

# 日付レンジの point-in-time 生成（再開可能・モデルキャッシュ付き）
# 例: make generate START=2021-06-04 END=2021-06-08
generate:
	python daily_generator.py --start $(START) --end $(END) --out signals

# CPU_Checker

## description

実行中マシンの `/proc`（または `top`）から、どのプロセスがどれだけ CPU を使っているかを HTML にする。ROS 2 の公式ログ（`~/.ros/log`）があれば、Node ↔ PID と Composable の所属も付ける。

- キーは PID。Composable は 1 PID に複数 Node
- `%CPU` は 1 コア = 100%
- `collect` はマシン上の全プロセスを取る。launch ログは不要
- `report` の `--launch` 省略時は `~/.ros/log` の最新 `launch.log` を使う。無ければスキップ

コマンドは 2 つ。`collect` は実行中にコア情報を取る。`report` はあとから HTML を出す。

## usage

カレントは `CPU_Checker`。venv を有効化し、`PYTHONPATH=src` を付ける。

### setup

```bash
cd CPU_Checker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### run

コア情報まで取るなら（ROS の有無は問わない）:

```bash
source .venv/bin/activate
PYTHONPATH=src python3 -m cpu_checker collect --out logs/samples.csv
```

Ctrl+C で止める。`--duration 120` で秒数指定もできる。

レポート（全プロセス。ROS ログがあれば Node 名を付ける）:

```bash
PYTHONPATH=src python3 -m cpu_checker report --top logs/samples.csv --out output/report.html
```

`collect` なしでも、公式ログだけなら所属関係は出る。`%CPU` だけなら `--top` に `top -b` のログを渡す。ROS の PID だけに絞るときは `--ros-only`。

```bash
PYTHONPATH=src python3 -m cpu_checker report --out output/report.html
```

特定の起動にするとき:

```bash
PYTHONPATH=src python3 -m cpu_checker report \
  --launch ~/.ros/log/<セッション> \
  --top logs/samples.csv \
  --out output/report.html
```

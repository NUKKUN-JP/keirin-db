"""
split_monthly_v1.py
GitHub Actions ワークフロー用: DB の月別ファイル化と再構築。

使い方:
  python split_monthly_v1.py rebuild  # keirin_months/*.jsonl → 一体型 keirin_data_scored_v2.jsonl を再構築
  python split_monthly_v1.py split    # 一体型 → keirin_months/keirin_YYYYMM.jsonl に分割

設計:
  - fetch_keirin_data_v19.py は従来通り一体型ファイルで動作する (変更不要)
  - ワークフローは fetch の前に rebuild、後に split を実行する
  - リポジトリには月別ファイルだけをコミットする (1ファイル約8MBで頭打ち → 100MB上限を永久回避)
  - アプリ側は「手元にない月 + 今月分」だけをダウンロードすればよい (通信量最小)
"""

import os
import sys
import json
import glob

SAVE_DIR = os.getcwd()
BIG_PATH = os.path.join(SAVE_DIR, "keirin_data_scored_v2.jsonl")
MONTH_DIR = os.path.join(SAVE_DIR, "keirin_months")


def _month_key(line):
    """1行から YYYYMM を取り出す。date フィールド優先、なければ race_id から復元"""
    try:
        rec = json.loads(line)
    except Exception:
        return ""
    d = str(rec.get("date", ""))
    if len(d) >= 6 and d[:6].isdigit():
        return d[:6]
    rid = str(rec.get("race_id", ""))
    # race_id 形式: 場コード2桁 + 日付8桁 + レース番号2桁
    if len(rid) >= 10 and rid[2:8].isdigit():
        return rid[2:8]
    return ""


def rebuild():
    """月別ファイル群から一体型DBを再構築 (fetch 実行前に呼ぶ)"""
    files = sorted(glob.glob(os.path.join(MONTH_DIR, "keirin_*.jsonl")))
    if not files:
        if os.path.exists(BIG_PATH):
            size_mb = round(os.path.getsize(BIG_PATH) / 1024.0 / 1024.0, 1)
            print("[rebuild] 月別ファイルなし → 既存の一体型をそのまま使用 ("
                  + str(size_mb) + " MB)")
        else:
            with open(BIG_PATH, 'w', encoding='utf-8') as f:
                pass
            print("[rebuild] 月別ファイルも一体型もなし → 空のDBから開始")
        return
    total = 0
    with open(BIG_PATH, 'w', encoding='utf-8') as out:
        for fp in files:
            with open(fp, 'r', encoding='utf-8') as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    out.write(stripped + "\n")
                    total += 1
    print("[rebuild] " + str(len(files)) + " ヶ月分 → 一体型 " + str(total) + " レースを再構築")


def split():
    """一体型DBを月別ファイルに分割 (fetch 実行後に呼ぶ)"""
    if not os.path.exists(BIG_PATH):
        print("[split] 一体型DBが存在しないためスキップ")
        return
    if not os.path.isdir(MONTH_DIR):
        os.makedirs(MONTH_DIR)
    handles = {}
    counts = {}
    skipped = 0
    try:
        with open(BIG_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                mk = _month_key(stripped)
                if not mk:
                    skipped += 1
                    continue
                if mk not in handles:
                    path = os.path.join(MONTH_DIR, "keirin_" + mk + ".jsonl")
                    handles[mk] = open(path, 'w', encoding='utf-8')
                    counts[mk] = 0
                handles[mk].write(stripped + "\n")
                counts[mk] += 1
    finally:
        for h in handles.values():
            h.close()
    for mk in sorted(counts.keys()):
        print("[split] keirin_" + mk + ".jsonl: " + str(counts[mk]) + " レース")
    if skipped > 0:
        print("[split] 日付不明でスキップ: " + str(skipped) + " 行")
    print("[split] 合計 " + str(len(counts)) + " ヶ月分に分割完了")


if __name__ == "__main__":
    mode = ""
    if len(sys.argv) >= 2:
        mode = sys.argv[1].strip().lower()
    if mode == "rebuild":
        rebuild()
    elif mode == "split":
        split()
    else:
        print("使い方: python split_monthly_v1.py [rebuild|split]")
        sys.exit(1)

# -*- coding: utf-8 -*-
"""
supplement_holes_v1.py
既存の fetch_keirin_data_v21.py をモジュールとしてインポートし、
指定された期間（または全期間）のデータ欠損（穴）のみを対象に補足取得を実行する専用スクリプト。
"""
import os
import sys
from datetime import datetime, timedelta

# 既存のロジックを再利用するためインポート
import fetch_keirin_data_v21 as fd

def get_all_dates_in_db():
    """DB内に存在するすべての日付を抽出する"""
    dates = set()
    if not os.path.exists(fd.JSONL_PATH):
        return dates
    import json
    with open(fd.JSONL_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip(): continue
            try:
                obj = json.loads(line)
                if 'date' in obj:
                    dates.add(obj['date'])
            except json.JSONDecodeError:
                pass
    return dates

def main():
    # 既存データのIDを読み込み
    fd.load_existing_ids()
    
    start_env = os.environ.get("SUPPLEMENT_START", "").strip()
    end_env = os.environ.get("SUPPLEMENT_END", "").strip()

    target_dates = []
    
    # 期間指定がある場合はその範囲の日付リストを生成
    if start_env and end_env:
        start_dt = datetime.strptime(start_env, "%Y%m%d")
        end_dt = datetime.strptime(end_env, "%Y%m%d")
        curr = start_dt
        while curr <= end_dt:
            target_dates.append(curr.strftime("%Y%m%d"))
            curr += timedelta(days=1)
    else:
        # 指定がない場合はDB内の全日付をスキャン
        print("[INFO] 期間指定なし。DB内の全日付をスキャンして穴を探索します。")
        target_dates = sorted(list(get_all_dates_in_db()))

    if not target_dates:
        print("[INFO] 対象日付が存在しません。終了します。")
        sys.exit(0)

    total_supplemented = 0
    print(f"[開始] 欠損補足処理: {len(target_dates)} 日分をスキャン対象とします。")
    print(f"対象DB: {fd.JSONL_PATH}")
    
    for date_str in target_dates:
        # 連続失敗等による中断フラグが立っている場合は打ち切り
        if fd._abort_event.is_set():
            print("\n[中断] 連続エラー検知のため、以降の補足処理を打ち切ります。")
            break
        
        # 該当日の穴を検索
        holes = fd.find_holes_in_db(date_str)
        if not holes:
            continue
            
        # 穴埋め処理の実行（内部で upsert=True による上書きが行われる）
        success = fd.supplement_holes_for_date(date_str)
        total_supplemented += success

    print(f"\n[完了] 補足取得成功: 合計 {total_supplemented} 件")

if __name__ == "__main__":
    main()

"""
fetch_today_basic_v1.py
GitHub Actions 用: 当日のレース基本データ (出走表) を事前取得して
today_cache/races_YYYYMMDD.json として保存する。

アプリ (託宣) は託宣ボタン押下時にこのファイルをダウンロードするだけで
全会場スクレイピングを省略でき、即計算を開始できる。

必要ファイル: predict_v14_wind_unified.py をリポジトリ直下に配置すること。
(import失敗時にログへ出るモジュール名のファイルも順次追加)

環境変数:
  KEIRIN_DATE         取得日 YYYYMMDD (省略時=今日。TZ=Asia/Tokyo前提)
  KEIRIN_NOTIFY_FAIL  '1' なら取得0件時にLINE通知
  LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID
"""

import os
import sys
import json
import glob
from datetime import datetime

OUT_DIR = "today_cache"
KEEP_DAYS = 3


def send_line(text):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()
    if not token or not user_id:
        print("[line] 未設定のため通知スキップ")
        return
    try:
        import requests
        url = "https://api.line.me/v2/bot/message/push"
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + token}
        body = {"to": user_id,
                "messages": [{"type": "text", "text": text}]}
        r = requests.post(url, headers=headers,
                          data=json.dumps(body), timeout=30)
        print("[line] 送信 HTTP" + str(r.status_code))
    except Exception as e:
        print("[line] 送信失敗: " + str(e)[:80])


def cleanup_old(today_dt):
    """KEEP_DAYS日より古い事前取得ファイルを削除 (リポジトリ肥大化防止)"""
    files = glob.glob(os.path.join(OUT_DIR, "races_*.json"))
    for fp in files:
        base = os.path.basename(fp)
        ds = base[6:14]
        if not ds.isdigit():
            continue
        try:
            fdt = datetime.strptime(ds, "%Y%m%d")
        except Exception:
            continue
        if (today_dt - fdt).days > KEEP_DAYS:
            try:
                os.remove(fp)
                print("[cleanup] 削除: " + base)
            except Exception:
                pass


def main():
    date_str = os.environ.get("KEIRIN_DATE", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    try:
        tdt = datetime.strptime(date_str, "%Y%m%d")
    except Exception:
        print("[error] 日付不正: " + date_str)
        sys.exit(1)

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    out_path = os.path.join(OUT_DIR, "races_" + date_str + ".json")

    # 既に取得済みなら即終了 (6:00成功時、6:30の保険実行は何もしない)
    if os.path.exists(out_path):
        races = []
        try:
            f = open(out_path, "r", encoding="utf-8")
            races = json.load(f)
            f.close()
        except Exception:
            races = []
        if isinstance(races, list) and races:
            print("[skip] 取得済み: " + str(len(races)) + "R")
            cleanup_old(tdt)
            return

    try:
        import predict_v14_wind_unified as engine
    except Exception as e:
        print("[error] engine読み込み失敗: " + str(e))
        print("  → predict_v14_wind_unified.py (と依存ファイル) を"
              "リポジトリ直下に置いてください")
        sys.exit(1)

    all_races = []
    venues = []
    for pc in engine.CODES:
        pn = engine.CODES[pc]
        try:
            res = engine.check_venue_open(pc, pn, tdt)
        except Exception as e:
            print("[warn] " + pn + " 開催確認失敗: " + str(e)[:60])
            continue
        if not res:
            continue
        try:
            pc2, pn2, bd, dy = res
            vr = engine.fetch_venue_races(pc2, pn2, bd, dy, tdt, date_str)
        except Exception as e:
            print("[warn] " + pn + " 取得失敗: " + str(e)[:60])
            continue
        if vr:
            all_races.extend(vr)
            venues.append(pn)
            print("[ok] " + pn + ": " + str(len(vr)) + "R")

    if not all_races:
        print("[fail] 開催会場なし or 取得0件 (メンテナンス中の可能性)")
        if os.environ.get("KEIRIN_NOTIFY_FAIL", "") == "1":
            send_line("【競輪】" + date_str
                      + " 当日基本データの事前取得に失敗しました (0件)。"
                      + "アプリ側は従来のスクレイピングにフォールバックします。")
        return

    f = open(out_path, "w", encoding="utf-8")
    json.dump(all_races, f, ensure_ascii=False)
    f.close()
    print("[done] " + date_str + ": " + str(len(venues)) + "会場 "
          + str(len(all_races)) + "R → " + out_path)
    cleanup_old(tdt)


if __name__ == "__main__":
    main()

"""
fetch_today_basic_v3.py
GitHub Actions 用: 当日のレース基本データ (出走表) を事前取得して
today_cache/races_YYYYMMDD.json として保存する。

アプリ (託宣) は託宣ボタン押下時にこのファイルをダウンロードするだけで
全会場スクレイピングを省略でき、即計算を開始できる。

v2からの変更点:
  - 会場の健全性判定を「レース数」から「歯抜け(欠番)検知」に強化
    (7Rのうち5Rだけ取れている、のようなケースも検知して再取得)
  - engine側(r2)のリトライ+二次取得と合わせた二重の保険

v1からの変更点 (自己修復):
  - 取得レース数が MIN_RACES (5) 未満の会場は取りこぼしとみなし、その場で再取得
  - 保存済みファイルがある場合も、少レース会場だけ再取得して差し替え
    (6:00の取得で青森1Rのような取りこぼしが起きても、6:30の保険実行が自動修復)

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
MIN_RACES = 5   # 1会場あたりの最低レース数。未満なら取りこぼしとみなす
RETRY_WAIT = 5  # 再取得前の待機秒


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


def venue_is_weak(race_list):
    """会場の取得が不完全か判定: レース数不足 or 歯抜け(欠番)あり"""
    if not race_list:
        return True
    if len(race_list) < MIN_RACES:
        return True
    nos = set()
    for r in race_list:
        rid = str(r.get("race_id", ""))
        if len(rid) >= 2 and rid[-2:].isdigit():
            nos.add(int(rid[-2:]))
    if not nos:
        return True
    mx = max(nos)
    i = 1
    while i < mx:
        if i not in nos:
            return True
        i += 1
    return False


def fetch_one_venue(engine, pc, pn, tdt, date_str):
    """1会場分の取得。少レース時は1回だけ再取得して多い方を採用"""
    import time
    try:
        res = engine.check_venue_open(pc, pn, tdt)
    except Exception as e:
        print("[warn] " + pn + " 開催確認失敗: " + str(e)[:60])
        return None
    if not res:
        return None
    pc2, pn2, bd, dy = res
    try:
        vr = engine.fetch_venue_races(pc2, pn2, bd, dy, tdt, date_str)
    except Exception as e:
        print("[warn] " + pn + " 取得失敗: " + str(e)[:60])
        vr = []
    if vr and not venue_is_weak(vr):
        return vr
    # 取りこぼし疑い (少レース or 歯抜け) → 待機して再取得
    print("[retry] " + pn + ": " + str(len(vr) if vr else 0)
          + "R (少レース/歯抜け) のため再取得します")
    time.sleep(RETRY_WAIT)
    try:
        vr2 = engine.fetch_venue_races(pc2, pn2, bd, dy, tdt, date_str)
    except Exception as e:
        print("[warn] " + pn + " 再取得失敗: " + str(e)[:60])
        vr2 = []
    if vr2 and (not vr or len(vr2) > len(vr)):
        return vr2
    return vr if vr else None


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

    try:
        import predict_v14_wind_unified as engine
    except Exception as e:
        print("[error] engine読み込み失敗: " + str(e))
        print("  → predict_v14_wind_unified.py (と依存ファイル) を"
              "リポジトリ直下に置いてください")
        sys.exit(1)

    # 既存ファイルがある場合: 少レース会場だけ再取得して自己修復
    if os.path.exists(out_path):
        races = []
        try:
            f = open(out_path, "r", encoding="utf-8")
            races = json.load(f)
            f.close()
        except Exception:
            races = []
        if isinstance(races, list) and races:
            by_pc = {}
            for r in races:
                rid = str(r.get("race_id", ""))
                if len(rid) >= 2:
                    by_pc.setdefault(rid[:2], []).append(r)
            weak = [pc for pc in by_pc if venue_is_weak(by_pc[pc])]
            if not weak:
                print("[skip] 取得済み: " + str(len(races)) + "R (全会場正常)")
                cleanup_old(tdt)
                return
            print("[heal] 少レース会場を再取得: "
                  + ", ".join([engine.CODES.get(pc, pc) + "("
                               + str(len(by_pc[pc])) + "R)" for pc in weak]))
            healed = False
            for pc in weak:
                pn = engine.CODES.get(pc, pc)
                vr = fetch_one_venue(engine, pc, pn, tdt, date_str)
                better = False
                if vr:
                    if len(vr) > len(by_pc[pc]):
                        better = True
                    elif not venue_is_weak(vr) and venue_is_weak(by_pc[pc]):
                        better = True
                if better:
                    by_pc[pc] = vr
                    healed = True
                    print("[ok] " + pn + ": " + str(len(vr)) + "R に修復")
            if not healed:
                print("[heal] 修復できる会場はありませんでした (現状維持)")
                cleanup_old(tdt)
                return
            all_races = []
            for pc in by_pc:
                all_races.extend(by_pc[pc])
            f = open(out_path, "w", encoding="utf-8")
            json.dump(all_races, f, ensure_ascii=False)
            f.close()
            print("[done] 修復保存: " + str(len(all_races)) + "R → " + out_path)
            cleanup_old(tdt)
            return

    all_races = []
    venues = []
    for pc in engine.CODES:
        pn = engine.CODES[pc]
        vr = fetch_one_venue(engine, pc, pn, tdt, date_str)
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

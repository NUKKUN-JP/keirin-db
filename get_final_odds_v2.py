# -*- coding: utf-8 -*-
"""
get_final_odds_v2.py（2026-10-14）
v2: 同じ1回の問い合わせで、風向（WINTICKET の番号と、あれば方位の文字）・風速・天候も odds/WIND_YYYYMM.json.gz に保存（託宣の16方位と突き合わせて、番号と方位の対応表を作るため）
v1: BANKVISION の不安定度のため
WINTICKET から、レースごとの「3連単の確定オッズ（全組み合わせ）」を集める。GitHub Actions で動かす前提。

- 読むもの: リポジトリの bvdb/BVDB_YYYYMM.json.gz（月別の BVDB）。各レースの開催ID（cup_id）・日（day）・R（race_no）を使う
- 取る住所: https://api.winticket.jp/v1/keirin/cups/{開催ID}/schedules/{日}/races/{R}?pf=web の trifecta
- 書くもの: odds/FO_YYYYMM.json.gz（{レースID: {"1-2-3": オッズ, ...}}。BANKVISION の final_odds.json と同じ形）
- 続きから: 書き出し済みのレースは飛ばす。100レースごとに保存するので、途中で止まっても続きから
- 相手の負担: 1レースごとに SLEEP 秒（既定 2 秒）空ける。429・477・403 が返ったらその場で止める（時間を空けて再開）
- 1回の実行の長さ: MAX_MIN 分（既定 330 分＝5時間半。GitHub Actions の上限6時間の内側）で区切る

使い方（環境変数）
  MONTHS   取る月をカンマ区切り（例 202501,202502）。省略すると bvdb/ にある全部の月
  BVDB_DIR BVDB の置き場所（既定 bvdb）
  OUT_DIR  書き出し先（既定 odds）
  SLEEP    1レースごとの間隔（秒。既定 2）
  MAX_MIN  1回の実行の上限（分。既定 330）
"""
import glob
import gzip
import json
import os
import sys
import time

try:
    import requests
    HAS_REQUESTS = True
except Exception:
    HAS_REQUESTS = False
    from urllib.request import Request, urlopen

API_ROOT = "https://api.winticket.jp/v1/keirin"
HEADERS = {"User-Agent": "Mozilla/5.0 (personal research; keirin-db)", "Accept": "application/json"}
BVDB_DIR = os.environ.get("BVDB_DIR", "bvdb")
OUT_DIR = os.environ.get("OUT_DIR", "odds")
SLEEP = float(os.environ.get("SLEEP", "2"))
MAX_MIN = float(os.environ.get("MAX_MIN", "330"))
STOP_CODES = (403, 429, 477)
T0 = time.time()


class Stop(Exception):
    pass


def http_json(url):
    for i in range(3):
        try:
            if HAS_REQUESTS:
                r = requests.get(url, headers=HEADERS, timeout=30)
                code = r.status_code
                if code == 200:
                    return r.json()
                if code in STOP_CODES:
                    raise Stop("WINTICKET から " + str(code) + " が返りました。拒否されているので止めます。時間を空けて再開してください")
                if code == 404:
                    return None
            else:
                f = urlopen(Request(url, headers=HEADERS), timeout=30)
                raw = f.read()
                f.close()
                return json.loads(raw.decode("utf-8"))
        except Stop:
            raise
        except Exception as e:
            code = getattr(e, "code", None)
            if code in STOP_CODES:
                raise Stop("WINTICKET から " + str(code) + " が返りました。拒否されているので止めます。時間を空けて再開してください")
            if code == 404:
                return None
        time.sleep(SLEEP * (i + 2))
    return None


def trifecta_of(data):
    out = {}
    arr = data.get("trifecta") if isinstance(data, dict) else None
    if not isinstance(arr, list):
        return out
    for o in arr:
        if not isinstance(o, dict):
            continue
        k = o.get("key")
        if not isinstance(k, list) or len(k) != 3:
            continue
        try:
            nums = [int(x) for x in k]
        except Exception:
            continue
        odds = o.get("odds")
        if odds is None:
            s = "".join(ch for ch in str(o.get("oddsStr") or "") if ch.isdigit() or ch == ".")
            odds = float(s) if s else None
        try:
            odds = float(odds)
        except Exception:
            continue
        if odds > 0:
            out[str(nums[0]) + "-" + str(nums[1]) + "-" + str(nums[2])] = odds
    return out


def wind_of(data):
    """レースの風（WINTICKET の生の値をそのまま）。race の下にある"""
    race = data.get("race") if isinstance(data, dict) else None
    if not isinstance(race, dict):
        return None
    keep = {}
    for k, v in race.items():
        kl = str(k).lower()
        if ("wind" in kl or "weather" in kl) and (isinstance(v, (str, int, float)) or v is None):
            keep[k] = v
    return keep


def load_out(path):
    if not os.path.exists(path):
        return {}
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_out(path, data):
    tmp = path + ".part"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def month_files():
    months = [m.strip() for m in os.environ.get("MONTHS", "").split(",") if m.strip()]
    files = []
    if months:
        for m in months:
            p = os.path.join(BVDB_DIR, "BVDB_" + m + ".json.gz")
            if os.path.exists(p):
                files.append((m, p))
            else:
                print("[注意] " + p + " がありません")
    else:
        for p in sorted(glob.glob(os.path.join(BVDB_DIR, "BVDB_[0-9][0-9][0-9][0-9][0-9][0-9].json.gz"))):
            files.append((os.path.basename(p)[5:11], p))
    return files


def main():
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    total_new = 0
    for m, p in month_files():
        out_path = os.path.join(OUT_DIR, "FO_" + m + ".json.gz")
        out = load_out(out_path)
        wind_path = os.path.join(OUT_DIR, "WIND_" + m + ".json.gz")
        wind = load_out(wind_path)
        with gzip.open(p, "rt", encoding="utf-8") as f:
            races = json.load(f).get("races", [])
        # オッズか風のどちらかがまだのレース（v1 で取ったレースも、風を取りに行く）
        todo = [r for r in races if not r.get("cancel") and r.get("result") and (str(r.get("race_id")) not in out or str(r.get("race_id")) not in wind)]
        print("[" + m + "] レース " + str(len(races)) + "・取得済み " + str(len(out)) + "・残り " + str(len(todo)))
        n = 0
        for r in todo:
            if (time.time() - T0) / 60.0 > MAX_MIN:
                save_out(out_path, out)
                save_out(wind_path, wind)
                print("[区切り] 実行時間の上限（" + str(int(MAX_MIN)) + " 分）。続きは次の実行で")
                return total_new
            url = API_ROOT + "/cups/" + str(r.get("cup_id")) + "/schedules/" + str(r.get("day")) + "/races/" + str(int(r.get("race_no"))) + "?pf=web"
            try:
                data = http_json(url)
            except Stop:
                save_out(out_path, out)   # 止まる前に、ここまでの分を保存
                save_out(wind_path, wind)
                raise
            tri = trifecta_of(data)
            if str(r.get("race_id")) not in out or tri:
                out[str(r.get("race_id"))] = tri   # 取れなかったレースも空で記録（何度も取りに行かない）
            wind[str(r.get("race_id"))] = wind_of(data) or {}
            n += 1
            total_new += 1
            if n % 100 == 0:
                save_out(out_path, out)
                save_out(wind_path, wind)
                print("   " + str(n) + "/" + str(len(todo)))
            time.sleep(SLEEP)
        save_out(out_path, out)
        save_out(wind_path, wind)
        print("[" + m + "] 完了（今回 " + str(n) + " レース）")
    return total_new


if __name__ == "__main__":
    try:
        k = main()
        print("[完了] 今回取ったレース " + str(k))
    except Stop as e:
        print("[停止] " + str(e))
        sys.exit(0)

# -*- coding: utf-8 -*-
"""
build_player_rsrank_finish.py

各選手 × rs_rank (raw_scoreの順位 1〜N) × 会場 × 天気 × 風速 × 風向
ごとに着順分布を集計。

【入力】
  keirin_data_scored_v2.jsonl
  venue_profile.json
  venue_home_direction.json

【出力】
  player_rsrank_finish_7car.jsonl  (7車立てのみ)
  player_rsrank_finish_9car.jsonl  (9車立てのみ)

【出力フォーマット】
  各行 JSON:
  {
    "player_key": "...",
    "name": "...",
    "venue": "松戸",
    "sky": "晴",
    "wind_speed_cat": "弱風",
    "wind_dir_cat": "H追B向",
    "rs_rank": 1,
    "rank1": 5, "rank2": 3, "rank3": 2, "rank4": 1, "rank5": 0,
    "rank6": 0, "rank7": 0, "rank8": 0, "rank9": 0,
    "total": 11
  }

【区分定義】
  天気: 晴 / 曇 / 雨 / 雪
  風速: 無風 (≤0.5m) / 弱風 (0.6-2.0) / 中風 (2.1-3.5) / 強風 (3.6+)
  風向: H追B向 / H向B追 / HB横 / BH横 (ホーム方角と風向の関係)

【除外】
  - 個人戦、 落車、 欠車、 失格 含むレース
  - rank が 1〜N (車立て数) の範囲外
"""
import os
import json
import re
from collections import defaultdict

SAVE_DIR = "/storage/emulated/0/Download"
DB_PATH = os.path.join(SAVE_DIR, "keirin_data_scored_v2.jsonl")
VENUE_PROFILE_PATH = os.path.join(SAVE_DIR, "venue_profile.json")
VENUE_HOME_DIR_PATH = os.path.join(SAVE_DIR, "venue_home_direction.json")
OUT_7 = os.path.join(SAVE_DIR, "player_rsrank_finish_7car.jsonl")
OUT_9 = os.path.join(SAVE_DIR, "player_rsrank_finish_9car.jsonl")

# === cutoff/パス オーバーライド (walk-forward用。元ロジックは不変) ===
_TAKUSEN_DATA = os.path.join(SAVE_DIR, "takusen", "data")
if os.path.exists(os.path.join(_TAKUSEN_DATA, "keirin_data_scored_v2.jsonl")):
    DB_PATH = os.path.join(_TAKUSEN_DATA, "keirin_data_scored_v2.jsonl")
_st = os.path.join(_TAKUSEN_DATA, "static")
for _cand in (os.path.join(_st, "venue_home_direction.json"),
              os.path.join(_TAKUSEN_DATA, "venue_home_direction.json")):
    if os.path.exists(_cand):
        VENUE_HOME_DIR_PATH = _cand
        break
for _cand in (os.path.join(_st, "venue_profile.json"),
              os.path.join(_TAKUSEN_DATA, "venue_profile.json")):
    if os.path.exists(_cand):
        VENUE_PROFILE_PATH = _cand
        break
_ENV_DB = os.environ.get("KEIRIN_DB", "")
if _ENV_DB:
    DB_PATH = _ENV_DB
# 出力先: KEIRIN_OUT_DIR を指定するとそのフォルダに7car/9carを出す
_ENV_OUTDIR = os.environ.get("KEIRIN_OUT_DIR", "")
if _ENV_OUTDIR:
    OUT_7 = os.path.join(_ENV_OUTDIR, "player_rsrank_finish_7car.jsonl")
    OUT_9 = os.path.join(_ENV_OUTDIR, "player_rsrank_finish_9car.jsonl")
# 個別指定も可
_ENV_OUT7 = os.environ.get("KEIRIN_OUT7", "")
if _ENV_OUT7:
    OUT_7 = _ENV_OUT7
_ENV_OUT9 = os.environ.get("KEIRIN_OUT9", "")
if _ENV_OUT9:
    OUT_9 = _ENV_OUT9
KEIRIN_CUTOFF = os.environ.get("KEIRIN_CUTOFF", "").strip()

# 16方位 → 角度
DIR_TO_DEG = {
    "北":0, "北北東":22.5, "北東":45, "東北東":67.5, "東":90, "東南東":112.5,
    "南東":135, "南南東":157.5, "南":180, "南南西":202.5, "南西":225,
    "西南西":247.5, "西":270, "西北西":292.5, "北西":315, "北北西":337.5,
    "N":0, "NNE":22.5, "NE":45, "ENE":67.5, "E":90, "ESE":112.5,
    "SE":135, "SSE":157.5, "S":180, "SSW":202.5, "SW":225,
    "WSW":247.5, "W":270, "WNW":292.5, "NW":315, "NNW":337.5,
}


def parse_weather(weather_str):
    """weather文字列をパース。 返り値 (sky, ws, wd)"""
    if not weather_str: return None, None, None
    sky_m = re.search(r"天気[::]\s*([^\s]+)", weather_str)
    ws_m = re.search(r"風速[::]\s*(\d+(?:\.\d+)?)m", weather_str)
    wd_m = re.search(r"風向[::]\s*([^\s\(（]+)", weather_str)
    sky = sky_m.group(1).strip() if sky_m else None
    ws = float(ws_m.group(1)) if ws_m else None
    wd = wd_m.group(1).strip() if wd_m else None
    if sky in ("--", "取得失敗", "不明"): sky = None
    if wd in ("--", ""): wd = None
    return sky, ws, wd


def categorize_sky(sky):
    """天気カテゴリ正規化"""
    if not sky: return None
    if "晴" in sky: return "晴"
    if "曇" in sky: return "曇"
    if "雨" in sky: return "雨"
    if "雪" in sky: return "雪"
    return None


def categorize_wind_speed(ws):
    """風速 → 区分"""
    if ws is None: return None
    if ws <= 0.5: return "無風"
    if ws <= 2.0: return "弱風"
    if ws <= 3.5: return "中風"
    return "強風"


def categorize_wind_dir(wd, venue, venue_home_dir):
    """風向 + 会場ホーム方角 → 4区分"""
    if not wd: return None
    deg = DIR_TO_DEG.get(wd)
    if deg is None: return None
    hd_str = venue_home_dir.get(venue)
    if not hd_str: return None
    hd_deg = DIR_TO_DEG.get(hd_str)
    if hd_deg is None: return None
    delta = (deg - hd_deg) % 360
    if delta < 45 or delta >= 315: return "H向B追"
    if 135 <= delta < 225: return "H追B向"
    if 45 <= delta < 135: return "HB横"
    return "BH横"


def main():
    print("=" * 70)
    print("  build_player_rsrank_finish.py")
    print("=" * 70)

    # venue_home_dir
    if not os.path.exists(VENUE_HOME_DIR_PATH):
        print("  [エラー] venue_home_direction.json なし"); return
    with open(VENUE_HOME_DIR_PATH, "r", encoding="utf-8") as f:
        venue_home_dir = json.load(f)
    print("  venue_home_dir: " + str(len(venue_home_dir)) + " 会場")

    # 集計用辞書
    # キー: (n_cars, player_key, name, venue, sky, ws_cat, wd_cat, rs_rank)
    # 値: [rank1, rank2, ..., rank9, total]
    bucket = defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0])  # 10要素: rank1-9 + total

    scanned = 0
    used_7 = 0
    used_9 = 0
    n_skip_personal = 0  # 個人戦
    n_skip_bad = 0  # 落車・欠車・失格
    n_skip_size = 0  # 7/9車立てでない

    with open(DB_PATH, "r", encoding="utf-8") as f:
        for line in f:
            scanned += 1
            if scanned % 5000 == 0:
                print("  scanned " + str(scanned))
            try:
                rec = json.loads(line)
            except:
                continue

            # cutoff: 指定日より後は集計対象外 (look-ahead除去)
            if KEIRIN_CUTOFF:
                _rdate = str(rec.get("date", ""))
                if _rdate and _rdate > KEIRIN_CUTOFF:
                    continue

            venue = rec.get("place", "")
            if not venue: continue

            players = rec.get("players", {})
            result_list = rec.get("result", [])
            line_str = rec.get("line", "")
            weather = rec.get("weather", "")

            if not isinstance(players, dict) or not players: continue
            if not isinstance(result_list, list) or not result_list: continue
            if not weather: continue

            # 個人戦除外 (ラインなし)
            if not line_str:
                n_skip_personal += 1; continue

            n_cars = len(players)
            if n_cars not in (7, 9):
                n_skip_size += 1; continue

            # 落車・欠車・失格除外
            bad = False
            for bs, pdata in players.items():
                if not isinstance(pdata, dict): continue
                fi = pdata.get("full_info", "")
                if "落車" in fi or "欠車" in fi or "失格" in fi or "棄権" in fi:
                    bad = True; break
            if bad:
                n_skip_bad += 1; continue

            # 天候パース
            sky_raw, ws, wd = parse_weather(weather)
            sky_cat = categorize_sky(sky_raw)
            ws_cat = categorize_wind_speed(ws)
            wd_cat = categorize_wind_dir(wd, venue, venue_home_dir)
            # ドーム会場 (前橋・小倉) は常に無風扱い
            if venue in ("前橋", "小倉"):
                ws_cat = "無風"
                wd_cat = "無風"
            # 無風時の風向は不要 (常に「無風」 として扱う)
            if ws_cat == "無風": wd_cat = "無風"
            # データなしは "不明" で集計しないと数が減りすぎる
            if sky_cat is None: sky_cat = "不明"
            if ws_cat is None: ws_cat = "不明"
            if wd_cat is None: wd_cat = "不明"

            # rs_rank の決定: raw_score 降順
            score_list = []  # [(bike_str, name, raw_score)]
            for bs, pdata in players.items():
                if not isinstance(pdata, dict): continue
                rs = pdata.get("raw_score")
                if rs is None: continue
                fi = pdata.get("full_info", "")
                nm = fi.split("/")[0].strip() if fi else ""
                pkey = pdata.get("player_key", "")
                if not pkey: pkey = nm  # player_key が None の場合は name をキーに
                score_list.append((str(bs), nm, pkey, float(rs)))
            if len(score_list) != n_cars: continue
            # 降順ソート
            score_list.sort(key=lambda x: -x[3])
            # rs_rank: 1-indexed
            bike_to_rsrank = {}
            for idx, (bs, nm, pkey, rs) in enumerate(score_list):
                bike_to_rsrank[bs] = idx + 1

            # 着順を bike → rank で取得
            # result は配列: [{"rank": 1, "bike": 1, ...}, ...]
            bike_to_rank = {}
            for entry in result_list:
                if not isinstance(entry, dict): continue
                rk = entry.get("rank")
                bk = entry.get("bike")
                if rk is None or bk is None: continue
                try:
                    rk_int = int(rk)
                    bk_str = str(bk)
                    bike_to_rank[bk_str] = rk_int
                except (ValueError, TypeError):
                    continue

            if not bike_to_rank: continue

            # 各選手の集計
            for bs, nm, pkey, rs in score_list:
                rk = bike_to_rank.get(bs)
                if rk is None: continue
                if rk < 1 or rk > n_cars: continue
                rs_rank = bike_to_rsrank[bs]
                key = (n_cars, pkey, nm, venue, sky_cat, ws_cat, wd_cat, rs_rank)
                arr = bucket[key]
                if 1 <= rk <= 9:
                    arr[rk - 1] += 1
                arr[9] += 1  # total

            if n_cars == 7: used_7 += 1
            elif n_cars == 9: used_9 += 1

    print("")
    print("=" * 70)
    print("  集計完了")
    print("=" * 70)
    print("  scanned: " + str(scanned))
    print("  used 7車: " + str(used_7) + " R")
    print("  used 9車: " + str(used_9) + " R")
    print("  個人戦除外: " + str(n_skip_personal))
    print("  落車等除外: " + str(n_skip_bad))
    print("  サイズ除外: " + str(n_skip_size))
    print("  bucket数: " + str(len(bucket)))

    # 出力
    print("")
    print("=" * 70)
    print("  出力中...")
    print("=" * 70)
    n7_out = 0
    n9_out = 0
    f7 = open(OUT_7, "w", encoding="utf-8")
    f9 = open(OUT_9, "w", encoding="utf-8")
    for key, arr in bucket.items():
        n_cars, pkey, nm, venue, sky_cat, ws_cat, wd_cat, rs_rank = key
        row = {
            "player_key": pkey,
            "name": nm,
            "venue": venue,
            "sky": sky_cat,
            "wind_speed_cat": ws_cat,
            "wind_dir_cat": wd_cat,
            "rs_rank": rs_rank,
            "rank1": arr[0], "rank2": arr[1], "rank3": arr[2],
            "rank4": arr[3], "rank5": arr[4], "rank6": arr[5],
            "rank7": arr[6], "rank8": arr[7], "rank9": arr[8],
            "total": arr[9],
        }
        if n_cars == 7:
            f7.write(json.dumps(row, ensure_ascii=False) + "\n")
            n7_out += 1
        elif n_cars == 9:
            f9.write(json.dumps(row, ensure_ascii=False) + "\n")
            n9_out += 1
    f7.close()
    f9.close()
    
    print("  " + OUT_7 + " (" + str(n7_out) + " 行)")
    print("  " + OUT_9 + " (" + str(n9_out) + " 行)")
    
    # サンプル
    print("")
    print("=" * 70)
    print("  サンプル: 7車立て、 rs_rank=1 で件数多い順 TOP10")
    print("=" * 70)
    samples = []
    for key, arr in bucket.items():
        n_cars, pkey, nm, venue, sky, ws, wd, rsr = key
        if n_cars != 7: continue
        if rsr != 1: continue
        if arr[9] < 3: continue
        samples.append((arr, key))
    samples.sort(key=lambda x: -x[0][9])
    for arr, key in samples[:10]:
        n_cars, pkey, nm, venue, sky, ws, wd, rsr = key
        rate1 = 100.0 * arr[0] / arr[9] if arr[9] > 0 else 0
        rate3 = 100.0 * (arr[0]+arr[1]+arr[2]) / arr[9] if arr[9] > 0 else 0
        print("  {:8s} {:6s} {:4s}|{:8s}|{:6s} n={:3d} 1着={:.1f}% 3着内={:.1f}%".format(
            nm[:8], venue[:6], sky[:4], ws + ("|" + wd if ws != "無風" else ""), "", arr[9], rate1, rate3))


if __name__ == "__main__":
    main()

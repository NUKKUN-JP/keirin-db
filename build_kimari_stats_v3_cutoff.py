# -*- coding: utf-8 -*-
"""
build_kimari_stats_v3.py — 会場×風向×風速別 決まり手集計 + 3着位置連動

v2 からの変更:
  1. 「同1番手」→「同先頭」、「別1番手」→「別先頭」 に統一
  2. 1着決まり手 × 2着位置決まり手 × 3着位置 の三重連動カテゴリ追加

セルキー:
  無風時:   "{会場}|無風"
  風あり時: "{会場}|{風向}|{風速}"

1着決まり手 正規化 (3種):
  "逃切り" → 逃
  "まくり" → 捲
  "追込み" → 差

2着決まり手 正規化 (4種):
  "逃残" / "逃残り" / "逃げ" / "逃切り" → 逃
  "捲残" / "捲残り" / "まくり"          → 捲
  "追込み"                              → 差
  "マーク"                              → マ

連動カテゴリ (2着):
  同先頭 / 同{X}番手 / 別先頭 / 別{X}番手 / 単騎 + 決まり手(逃/捲/差/マ)

3着位置 (決まり手なし):
  同先頭 / 同{X}番手 / 別先頭 / 別{X}番手 / 単騎

入力:
  /storage/emulated/0/Download/keirin_data_scored_v2.jsonl
  /storage/emulated/0/Download/venue_home_direction.json

出力:
  /storage/emulated/0/Download/kimari_stats_v3.json

Pydroid3制約: f-string不可、for-else不可
"""

import os
import re
import json
import datetime
from collections import defaultdict


# ============================================================
# 設定
# ============================================================
DOWNLOAD_DIR = "/storage/emulated/0/Download"
SAVE_DIR = DOWNLOAD_DIR if os.path.exists(DOWNLOAD_DIR) else os.getcwd()

DB_PATH = os.path.join(SAVE_DIR, "keirin_data_scored_v2.jsonl")
PATH_VENUE_HOME_DIR = os.path.join(SAVE_DIR, "venue_home_direction.json")
OUTPUT_PATH = os.path.join(SAVE_DIR, "kimari_stats_v3.json")

# === cutoff/パス オーバーライド (walk-forward用。元ロジックは不変) ===
_TAKUSEN_DATA = os.path.join(SAVE_DIR, "takusen", "data")
if os.path.exists(os.path.join(_TAKUSEN_DATA, "keirin_data_scored_v2.jsonl")):
    DB_PATH = os.path.join(_TAKUSEN_DATA, "keirin_data_scored_v2.jsonl")
_st = os.path.join(_TAKUSEN_DATA, "static")
for _cand in (os.path.join(_st, "venue_home_direction.json"),
              os.path.join(_TAKUSEN_DATA, "venue_home_direction.json")):
    if os.path.exists(_cand):
        PATH_VENUE_HOME_DIR = _cand
        break
_ENV_DB = os.environ.get("KEIRIN_DB", "")
if _ENV_DB:
    DB_PATH = _ENV_DB
_ENV_VHD = os.environ.get("KEIRIN_VHD", "")
if _ENV_VHD:
    PATH_VENUE_HOME_DIR = _ENV_VHD
_ENV_OUT = os.environ.get("KEIRIN_OUT", "")
if _ENV_OUT:
    OUTPUT_PATH = _ENV_OUT
KEIRIN_CUTOFF = os.environ.get("KEIRIN_CUTOFF", "").strip()

DOME_VENUES = {"前橋", "小倉", "千葉"}


# ============================================================
# パース
# ============================================================
def parse_line_chunks(line_str):
    if not isinstance(line_str, str):
        return []
    line_str = line_str.replace("ー", "-").replace("−", "-").replace("―", "-")
    chunks = []
    parts = line_str.split("-")
    i = 0
    while i < len(parts):
        p = parts[i].strip()
        digits = []
        j = 0
        while j < len(p):
            ch = p[j]
            if ch.isdigit():
                digits.append(ch)
            j = j + 1
        if digits:
            chunks.append(digits)
        i = i + 1
    return chunks


def is_kojinsen(chunks):
    if not chunks:
        return True
    i = 0
    while i < len(chunks):
        if len(chunks[i]) > 1:
            return False
        i = i + 1
    return True


# ============================================================
# 風判定
# ============================================================
_WIND_JP_TO_DEG = {
    "北": 0, "北北東": 22.5, "北東": 45, "東北東": 67.5,
    "東": 90, "東南東": 112.5, "南東": 135, "南南東": 157.5,
    "南": 180, "南南西": 202.5, "南西": 225, "西南西": 247.5,
    "西": 270, "西北西": 292.5, "北西": 315, "北北西": 337.5,
}
_HOME_DIR_TO_DEG = {
    "N": 0, "北": 0, "NE": 45, "北東": 45,
    "E": 90, "東": 90, "SE": 135, "南東": 135,
    "S": 180, "南": 180, "SW": 225, "南西": 225,
    "W": 270, "西": 270, "NW": 315, "北西": 315,
}


def parse_wind_dir_jp(weather_str):
    if not weather_str:
        return None
    m = re.search(r'風向[き]?\s*[:：]\s*([東西南北]+)', weather_str)
    if m:
        return m.group(1)
    return None


def parse_wind_speed(weather_str):
    if not weather_str:
        return None
    m = re.search(r'風速\s*[:：]\s*(\d+(?:\.\d+)?)\s*m', weather_str)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def classify_wind_at_direction(wind_from_deg, running_deg):
    diff = abs(wind_from_deg - running_deg) % 360
    diff = min(diff, 360 - diff)
    if diff <= 45:
        return "向かい風"
    if diff >= 135:
        return "追い風"
    return "横風"


def get_wind_cross_direction_jp(wind_from_deg, home_running_deg):
    rel = (wind_from_deg - home_running_deg) % 360
    if 45 < rel < 135:
        return "HB横"
    if 225 < rel < 315:
        return "BH横"
    return None


def classify_speed(wind_speed):
    if wind_speed <= 0.5:
        return "無風"
    if wind_speed <= 2.0:
        return "弱風"
    if wind_speed <= 3.5:
        return "中風"
    return "強風"


def get_wind_pattern(venue, weather_str, venue_home_dir):
    if venue in DOME_VENUES:
        return "無風", "無風"
    if not weather_str:
        return None, None
    if "風速:--" in weather_str or "風向:--" in weather_str:
        return "無風", "無風"
    wind_speed = parse_wind_speed(weather_str)
    if wind_speed is None:
        return None, None
    speed_cls = classify_speed(wind_speed)
    if speed_cls == "無風":
        return "無風", "無風"
    wind_jp = parse_wind_dir_jp(weather_str)
    if not wind_jp or wind_jp not in _WIND_JP_TO_DEG:
        return None, None
    wind_from_deg = _WIND_JP_TO_DEG[wind_jp]
    if not venue_home_dir:
        return None, None
    home_dir_jp = venue_home_dir.get(venue)
    if not home_dir_jp:
        return None, None
    home_deg = _HOME_DIR_TO_DEG.get(home_dir_jp)
    if home_deg is None:
        return None, None
    home_running = (home_deg - 90) % 360
    back_running = (home_running + 180) % 360
    home_cls = classify_wind_at_direction(wind_from_deg, home_running)
    back_cls = classify_wind_at_direction(wind_from_deg, back_running)
    if home_cls == "追い風" and back_cls == "向かい風":
        return "H追B向", speed_cls
    if home_cls == "向かい風" and back_cls == "追い風":
        return "H向B追", speed_cls
    if home_cls == "横風" and back_cls == "横風":
        cross = get_wind_cross_direction_jp(wind_from_deg, home_running)
        if cross:
            return cross, speed_cls
        return None, None
    return None, None


def make_cell_key(venue, wind_pat, speed_cls):
    """セルキー生成 (天気なし)
    無風時:   "{会場}|無風"
    風あり時: "{会場}|{風向}|{風速}"
    """
    if wind_pat == "無風" or speed_cls == "無風":
        return venue + "|無風"
    return venue + "|" + wind_pat + "|" + speed_cls


# ============================================================
# 決まり手の正規化
# ============================================================
def normalize_kimari_1st(finish):
    if finish == "逃切り":
        return "逃"
    if finish == "まくり":
        return "捲"
    if finish == "追込み":
        return "差"
    return None


def normalize_kimari_2nd(finish):
    if finish in ("逃残", "逃残り", "逃げ", "逃切り"):
        return "逃"
    if finish in ("捲残", "捲残り", "まくり"):
        return "捲"
    if finish == "追込み":
        return "差"
    if finish == "マーク":
        return "マ"
    return None


# ============================================================
# ライン位置判定
# ============================================================
def make_bike_line_map(chunks):
    bike_to_pos = {}
    line_id = 0
    i = 0
    while i < len(chunks):
        line_id = line_id + 1
        chunk = chunks[i]
        j = 0
        while j < len(chunk):
            try:
                bike = int(chunk[j])
                bike_to_pos[bike] = (line_id, j + 1)
            except Exception:
                pass
            j = j + 1
        i = i + 1
    return bike_to_pos


def get_relative_pos(chunks, bike_1st, bike_other):
    """1着に対する他の選手の相対位置を返す (2着・3着共通)
    
    返り値:
      "同先頭"     : 1着と同ライン、先頭の選手
      "同{X}番手"  : 1着と同ライン、X番手 (Xは2,3,4...)
      "別先頭"     : 別ラインの先頭
      "別{X}番手"  : 別ラインの番手 (Xは2,3,4...)
      "単騎"       : 1着が単騎、または他が単騎
      None         : 判定不可
    
    特殊ルール:
      - 1着が単騎、他も単騎 → 「単騎」
      - 1着が単騎、他がライン → 他は別線扱い
      - 1着がライン、他が単騎 → 「単騎」
    """
    bm = make_bike_line_map(chunks)
    p1 = bm.get(bike_1st)
    p2 = bm.get(bike_other)
    if p1 is None or p2 is None:
        return None
    line1, pos1 = p1
    line2, pos2 = p2
    
    def line_size(line_id):
        cnt = 0
        for b in bm:
            if bm[b][0] == line_id:
                cnt = cnt + 1
        return cnt
    
    size1 = line_size(line1)
    size2 = line_size(line2)
    
    if size1 == 1:
        if size2 == 1:
            return "単騎"
        else:
            if pos2 == 1:
                return "別先頭"
            return "別" + str(pos2) + "番手"
    
    if size2 == 1:
        return "単騎"
    
    if line1 == line2:
        if pos2 == 1:
            return "同先頭"
        return "同" + str(pos2) + "番手"
    else:
        if pos2 == 1:
            return "別先頭"
        return "別" + str(pos2) + "番手"


def get_2nd_relative_pos(chunks, bike_1st, bike_2nd):
    """2着の相対位置 (互換ラッパー)"""
    return get_relative_pos(chunks, bike_1st, bike_2nd)


# ============================================================
# データ構造
# ============================================================
def init_cell():
    return {
        "n": 0,
        "kimari_1st_count": {},    # {"逃": n, "捲": n, "差": n}
        "kimari_link_count": {},   # {"逃": {"同2番手マ": n, ...}, ...}
        "kimari_link3_count": {},  # {"逃": {"同2番手マ": {"同3番手": n, "別先頭": n, ...}}, ...}
    }


def add_to_cell(cell, k1, link_label, pos_3rd):
    """セル追加
    
    引数:
      k1: 1着決まり手 ("逃"/"捲"/"差")
      link_label: 2着位置×決まり手 ("同先頭逃" 等)
      pos_3rd: 3着位置 ("同2番手" 等) or None
    """
    cell["n"] = cell["n"] + 1
    if k1 not in cell["kimari_1st_count"]:
        cell["kimari_1st_count"][k1] = 0
    cell["kimari_1st_count"][k1] = cell["kimari_1st_count"][k1] + 1
    
    if link_label is not None:
        if k1 not in cell["kimari_link_count"]:
            cell["kimari_link_count"][k1] = {}
        sub = cell["kimari_link_count"][k1]
        if link_label not in sub:
            sub[link_label] = 0
        sub[link_label] = sub[link_label] + 1
        
        # 3着位置の連動集計
        if pos_3rd is not None:
            if k1 not in cell["kimari_link3_count"]:
                cell["kimari_link3_count"][k1] = {}
            link3_k1 = cell["kimari_link3_count"][k1]
            if link_label not in link3_k1:
                link3_k1[link_label] = {}
            link3_sub = link3_k1[link_label]
            if pos_3rd not in link3_sub:
                link3_sub[pos_3rd] = 0
            link3_sub[pos_3rd] = link3_sub[pos_3rd] + 1


def finalize_cell(cell):
    n = cell["n"]
    if n == 0:
        return
    
    k1c = cell.get("kimari_1st_count", {})
    k1_dist = {}
    for k in k1c:
        k1_dist[k] = round(k1c[k] / n, 4)
    cell["kimari_1st_dist"] = k1_dist
    
    klc = cell.get("kimari_link_count", {})
    klink_dist = {}
    for k1 in klc:
        sub = klc[k1]
        sub_total = 0
        for lab in sub:
            sub_total = sub_total + sub[lab]
        sub_dist = {}
        if sub_total > 0:
            for lab in sub:
                sub_dist[lab] = round(sub[lab] / sub_total, 4)
        klink_dist[k1] = {
            "n": sub_total,
            "dist": sub_dist,
        }
    cell["kimari_link_dist"] = klink_dist
    
    # 3着位置の確率化
    klc3 = cell.get("kimari_link3_count", {})
    klink3_dist = {}
    for k1 in klc3:
        klink3_dist[k1] = {}
        for link_label in klc3[k1]:
            sub = klc3[k1][link_label]
            sub_total = 0
            for pos in sub:
                sub_total = sub_total + sub[pos]
            sub_dist = {}
            if sub_total > 0:
                for pos in sub:
                    sub_dist[pos] = round(sub[pos] / sub_total, 4)
            klink3_dist[k1][link_label] = {
                "n": sub_total,
                "dist": sub_dist,
            }
    cell["kimari_link3_dist"] = klink3_dist
    
    del cell["kimari_1st_count"]
    del cell["kimari_link_count"]
    del cell["kimari_link3_count"]


# ============================================================
# ロード
# ============================================================
def load_venue_home_direction():
    if not os.path.exists(PATH_VENUE_HOME_DIR):
        return {}
    f = open(PATH_VENUE_HOME_DIR, "r", encoding="utf-8")
    d = json.load(f)
    f.close()
    return d


def iter_races(path):
    f = open(path, "r", encoding="utf-8")
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if KEIRIN_CUTOFF:
            rdate = str(rec.get("date", ""))
            if rdate and rdate > KEIRIN_CUTOFF:
                continue
        yield rec
    f.close()


# ============================================================
# メイン
# ============================================================
def main():
    print("=" * 78)
    print("  build_kimari_stats_v3.py")
    print("  会場×風向×風速別 決まり手集計 (v3: 同先頭 + 3着位置連動)")
    print("=" * 78)
    
    if not os.path.exists(DB_PATH):
        print("[エラー] DBがありません")
        return
    
    venue_home_dir = load_venue_home_direction()
    print("  venue_home_direction: " + str(len(venue_home_dir)) + " 件")
    
    cells = {}  # cell_key -> cell
    
    counters = {
        "total": 0,
        "skip_not_7": 0,
        "skip_kojinsen": 0,
        "skip_no_rank": 0,
        "skip_no_finish": 0,
        "skip_no_kimari_1st": 0,  # 1着が「マーク」等
        "skip_no_wind": 0,
        "valid_used": 0,
    }
    
    print("")
    print("集計中...")
    progress = 0
    for rec in iter_races(DB_PATH):
        counters["total"] = counters["total"] + 1
        progress = progress + 1
        if progress % 5000 == 0:
            print("  " + str(progress) + " レース処理...")
        
        # 7車立て確認
        players = rec.get("players", {})
        bikes = set()
        if isinstance(players, dict):
            for k in players.keys():
                try:
                    n = int(k)
                    if 1 <= n <= 9:
                        bikes.add(n)
                except Exception:
                    pass
        if len(bikes) != 7:
            counters["skip_not_7"] = counters["skip_not_7"] + 1
            continue
        
        # ライン解析
        line_str = rec.get("line", "")
        chunks = parse_line_chunks(line_str)
        if not chunks:
            counters["skip_no_rank"] = counters["skip_no_rank"] + 1
            continue
        if is_kojinsen(chunks):
            counters["skip_kojinsen"] = counters["skip_kojinsen"] + 1
            continue
        
        # 1着・2着・3着決まり手と車番取得
        result = rec.get("result", [])
        finish_1st = None
        finish_2nd = None
        bike_1st = None
        bike_2nd = None
        bike_3rd = None
        i = 0
        while i < len(result):
            r = result[i]
            rk = r.get("rank")
            if rk == 1:
                finish_1st = r.get("finish", "")
                bike_1st = r.get("bike")
            elif rk == 2:
                finish_2nd = r.get("finish", "")
                bike_2nd = r.get("bike")
            elif rk == 3:
                bike_3rd = r.get("bike")
            i = i + 1
        
        if not finish_1st or bike_1st is None:
            counters["skip_no_finish"] = counters["skip_no_finish"] + 1
            continue
        
        k1 = normalize_kimari_1st(finish_1st)
        if k1 is None:
            counters["skip_no_kimari_1st"] = counters["skip_no_kimari_1st"] + 1
            continue
        
        # 連動カテゴリ (2着)
        link_label = None
        if finish_2nd and bike_2nd is not None:
            k2 = normalize_kimari_2nd(finish_2nd)
            if k2 is not None:
                rel = get_relative_pos(chunks, bike_1st, bike_2nd)
                if rel:
                    link_label = rel + k2
        
        # 3着位置 (決まり手なし、位置のみ)
        pos_3rd = None
        if bike_3rd is not None:
            pos_3rd = get_relative_pos(chunks, bike_1st, bike_3rd)
        
        # 風判定
        venue = rec.get("place", "")
        weather_str = rec.get("weather", "")
        wind_pat, speed_cls = get_wind_pattern(venue, weather_str, venue_home_dir)
        if not venue or not wind_pat or not speed_cls:
            counters["skip_no_wind"] = counters["skip_no_wind"] + 1
            continue
        
        # セルキー生成 + 追加
        ck = make_cell_key(venue, wind_pat, speed_cls)
        if ck not in cells:
            cells[ck] = init_cell()
        add_to_cell(cells[ck], k1, link_label, pos_3rd)
        counters["valid_used"] = counters["valid_used"] + 1
    
    print("")
    print("---- スキャン完了 ----")
    print("  総レース             : " + str(counters["total"]))
    print("  集計対象             : " + str(counters["valid_used"]))
    print("  スキップ(非7車)      : " + str(counters["skip_not_7"]))
    print("  スキップ(個人戦)     : " + str(counters["skip_kojinsen"]))
    print("  スキップ(着順無し)   : " + str(counters["skip_no_rank"]))
    print("  スキップ(finish空)   : " + str(counters["skip_no_finish"]))
    print("  スキップ(1着繰上等)  : " + str(counters["skip_no_kimari_1st"]))
    print("  スキップ(風判定不可) : " + str(counters["skip_no_wind"]))
    
    print("")
    print("確率変換中...")
    for ck in cells:
        finalize_cell(cells[ck])
    
    now = datetime.datetime.now()
    output = {
        "meta": {
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "source_db": DB_PATH,
            "definition": "会場×風向×風速別 決まり手集計 + 3着位置連動 (v3)",
            "total_races_scanned": counters["total"],
            "total_races_used": counters["valid_used"],
        },
        "cells": cells,
    }
    
    print("")
    print("保存中: " + OUTPUT_PATH)
    f = open(OUTPUT_PATH, "w", encoding="utf-8")
    json.dump(output, f, ensure_ascii=False, indent=1)
    f.close()
    size = os.path.getsize(OUTPUT_PATH)
    print("ファイルサイズ: " + str(round(size/1024/1024, 2)) + " MB")
    
    # サンプル数分布
    print("")
    print("=" * 78)
    print("  サンプル数分布")
    print("=" * 78)
    print("  総セル数: " + str(len(cells)))
    
    n_bins = {">=100": 0, "50-99": 0, "30-49": 0, "10-29": 0, "5-9": 0, "1-4": 0}
    for ck in cells:
        n = cells[ck]["n"]
        if n >= 100:
            n_bins[">=100"] = n_bins[">=100"] + 1
        elif n >= 50:
            n_bins["50-99"] = n_bins["50-99"] + 1
        elif n >= 30:
            n_bins["30-49"] = n_bins["30-49"] + 1
        elif n >= 10:
            n_bins["10-29"] = n_bins["10-29"] + 1
        elif n >= 5:
            n_bins["5-9"] = n_bins["5-9"] + 1
        else:
            n_bins["1-4"] = n_bins["1-4"] + 1
    parts = []
    for bk in [">=100", "50-99", "30-49", "10-29", "5-9", "1-4"]:
        parts.append(bk + ":" + str(n_bins[bk]))
    print("  n分布: " + " / ".join(parts))
    
    # 上位10セル (n降順)
    print("")
    print("  上位10セル (n降順):")
    sorted_cells = sorted(cells.items(), key=lambda kv: -kv[1]["n"])
    j = 0
    while j < min(10, len(sorted_cells)):
        ck, c = sorted_cells[j]
        print("    " + (ck + "                              ")[:36] + " n=" + str(c["n"]))
        j = j + 1
    
    # 無風基準確認: 風あり時のセルに対応する無風セル存在率
    print("")
    print("  無風基準存在チェック (風あり時セル数 と 対応無風セル):")
    wind_cells = []
    no_wind_set = set()
    for ck in cells:
        parts = ck.split("|")
        if len(parts) == 3:  # 風あり: 会場|風向|風速
            wind_cells.append((ck, parts))
        elif len(parts) == 2:  # 無風: 会場|無風
            no_wind_set.add(parts[0] + "|無風")
    
    found = 0
    missing = 0
    j = 0
    while j < len(wind_cells):
        ck, parts = wind_cells[j]
        base = parts[0] + "|無風"
        if base in no_wind_set:
            found = found + 1
        else:
            missing = missing + 1
        j = j + 1
    print("    風ありセル: " + str(len(wind_cells)))
    print("    無風基準あり: " + str(found))
    print("    無風基準なし: " + str(missing))
    
    print("")
    print("=" * 78)
    print("完了")
    print("出力: " + OUTPUT_PATH)
    print("=" * 78)


if __name__ == "__main__":
    main()

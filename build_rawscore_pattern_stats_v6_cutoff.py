# -*- coding: utf-8 -*-
"""
build_rawscore_pattern_stats_v6.py — 周長×カント×直線×風向×風速別 7パターン統計 + 決まり手

v5 からの追加機能:
  各セルに 1着決まり手分布 と 1着決まり手×連動カテゴリ分布 を追加

1着決まり手 正規化 (3種):
  "逃切り" → 逃
  "まくり" → 捲
  "追込み" → 差
  ("マーク"/"逃残"/"捲残"/"--" は除外)

2着決まり手 正規化 (4種):
  "逃残" / "逃残り" / "逃げ" / "逃切り" → 逃
  "捲残" / "捲残り" / "まくり"          → 捲
  "追込み"                              → 差
  "マーク"                              → マ

連動カテゴリ ("同2番手マ", "別先頭差" 等):
  同{X}番手 / 別先頭 / 別{X}番手 / 単騎 + 2着決まり手(逃/捲/差/マ)

セルキー:
  無風時:   "{周長}|{カント}|{直線}|無風"
  風あり時: "{周長}|{カント}|{直線}|{風向}|{風速}"

入力:
  /storage/emulated/0/Download/keirin_data_scored_v2.jsonl
  /storage/emulated/0/Download/venue_home_direction.json
  /storage/emulated/0/Download/bank_data.json

出力:
  /storage/emulated/0/Download/rawscore_pattern_stats_v6.json

Pydroid3制約: f-string不可、for-else不可
"""

import os
import re
import json
import datetime


# ============================================================
# 設定
# ============================================================
DOWNLOAD_DIR = "/storage/emulated/0/Download"
SAVE_DIR = DOWNLOAD_DIR if os.path.exists(DOWNLOAD_DIR) else os.getcwd()

DB_PATH = os.path.join(SAVE_DIR, "keirin_data_scored_v2.jsonl")
PATH_VENUE_HOME_DIR = os.path.join(SAVE_DIR, "venue_home_direction.json")
PATH_BANK_DATA = os.path.join(SAVE_DIR, "bank_data.json")
OUTPUT_PATH = os.path.join(SAVE_DIR, "rawscore_pattern_stats_v6.json")

# === cutoff/パス オーバーライド (walk-forward用。元ロジックは不変) ===
_TAKUSEN_DATA = os.path.join(SAVE_DIR, "takusen", "data")
if os.path.exists(os.path.join(_TAKUSEN_DATA, "keirin_data_scored_v2.jsonl")):
    DB_PATH = os.path.join(_TAKUSEN_DATA, "keirin_data_scored_v2.jsonl")
_st = os.path.join(_TAKUSEN_DATA, "static")
if os.path.exists(os.path.join(_st, "venue_home_direction.json")):
    PATH_VENUE_HOME_DIR = os.path.join(_st, "venue_home_direction.json")
elif os.path.exists(os.path.join(_TAKUSEN_DATA, "venue_home_direction.json")):
    PATH_VENUE_HOME_DIR = os.path.join(_TAKUSEN_DATA, "venue_home_direction.json")
if os.path.exists(os.path.join(_st, "bank_data.json")):
    PATH_BANK_DATA = os.path.join(_st, "bank_data.json")
elif os.path.exists(os.path.join(_TAKUSEN_DATA, "bank_data.json")):
    PATH_BANK_DATA = os.path.join(_TAKUSEN_DATA, "bank_data.json")
_ENV_DB = os.environ.get("KEIRIN_DB", "")
if _ENV_DB:
    DB_PATH = _ENV_DB
_ENV_VHD = os.environ.get("KEIRIN_VHD", "")
if _ENV_VHD:
    PATH_VENUE_HOME_DIR = _ENV_VHD
_ENV_BANK = os.environ.get("KEIRIN_BANK", "")
if _ENV_BANK:
    PATH_BANK_DATA = _ENV_BANK
_ENV_OUT = os.environ.get("KEIRIN_OUT", "")
if _ENV_OUT:
    OUTPUT_PATH = _ENV_OUT
KEIRIN_CUTOFF = os.environ.get("KEIRIN_CUTOFF", "").strip()

SCENES = ["周回中", "赤板", "打鐘", "ホーム", "バック"]
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
# 決まり手の正規化
# ============================================================
def normalize_kimari_1st(finish):
    """1着決まり手を 3種類に正規化
    返り値: "逃" / "捲" / "差" / None (除外対象)
    """
    if finish == "逃切り":
        return "逃"
    if finish == "まくり":
        return "捲"
    if finish == "追込み":
        return "差"
    return None  # マーク/逃残/捲残/-- は除外


def normalize_kimari_2nd(finish):
    """2着決まり手を 4種類に正規化
    返り値: "逃" / "捲" / "差" / "マ" / None
    """
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
    """車番 → (ライン番号, ライン内位置) のマップ
    ライン番号: 1から (周回中順)
    位置: 1=先頭, 2=2番手, 3=3番手, ... ; 単騎ラインは位置=1
    
    返り値: {bike_int: (line_id, pos), ...}
    """
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


def get_2nd_relative_pos(chunks, bike_1st, bike_2nd):
    """2着の1着に対する相対位置を返す
    
    返り値:
      "同{X}番手"  : 1着と同ライン (Xは2着の位置=2,3,4...)
      "別先頭"     : 別ラインの先頭
      "別{X}番手"  : 別ラインの番手 (Xは2着の位置=2,3,4...)
      "単騎"       : 2着が単騎 (1着のラインが単騎以外) または 1着が単騎(2着もただし別線扱い基本)
    
    特殊ルール (あさいさん指示):
      - 1着が単騎、2着も単騎 → 「単騎」
      - 1着が単騎、2着がラインあり → 2着視点では別線扱い
      - 1着がラインあり、2着が単騎 → 「単騎」
    """
    bm = make_bike_line_map(chunks)
    p1 = bm.get(bike_1st)
    p2 = bm.get(bike_2nd)
    if p1 is None or p2 is None:
        return None
    line1, pos1 = p1
    line2, pos2 = p2
    
    # ライン人数を確認
    def line_size(line_id):
        cnt = 0
        for b in bm:
            if bm[b][0] == line_id:
                cnt = cnt + 1
        return cnt
    
    size1 = line_size(line1)
    size2 = line_size(line2)
    
    # 1着が単騎の場合
    if size1 == 1:
        if size2 == 1:
            return "単騎"  # 1着も2着も単騎
        else:
            # 1着単騎、2着はライン → 2着は別線扱い
            if pos2 == 1:
                return "別先頭"
            return "別" + str(pos2) + "番手"
    
    # 1着がライン
    if size2 == 1:
        return "単騎"  # 2着が単騎
    
    # 1着・2着ともライン
    if line1 == line2:
        # 同線
        return "同" + str(pos2) + "番手"
    else:
        # 別線
        if pos2 == 1:
            return "別先頭"
        return "別" + str(pos2) + "番手"


# ============================================================
# 整合性チェック
# ============================================================
def lap_to_items(lap_scene):
    items = []
    i = 0
    while i < len(lap_scene):
        e = lap_scene[i]
        bike = e.get("bike")
        x = e.get("x", 99)
        y = e.get("y", 99)
        if bike is not None:
            items.append((bike, x, y))
        i = i + 1
    return items


def check_race_valid(rec):
    lap = rec.get("lap", {})
    if not isinstance(lap, dict):
        return False
    i = 0
    while i < len(SCENES):
        s = SCENES[i]
        if s not in lap or not lap[s]:
            return False
        i = i + 1
    players = rec.get("players", {})
    true_bikes = set()
    if isinstance(players, dict):
        for k in players.keys():
            try:
                n = int(k)
                if 1 <= n <= 9:
                    true_bikes.add(n)
            except Exception:
                pass
    if not true_bikes:
        return False
    i = 0
    while i < len(SCENES):
        s = SCENES[i]
        items = lap_to_items(lap[s])
        sb = set()
        j = 0
        while j < len(items):
            sb.add(items[j][0])
            j = j + 1
        if sb != true_bikes:
            return False
        i = i + 1
    return True


# ============================================================
# raw_score 計算
# ============================================================
def extract_score_points(full_info):
    if not full_info or full_info == "未取得":
        return None
    m = re.search(r'([\d.]+)点$', full_info)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def extract_finishes(h_str):
    if not h_str or h_str == "なし" or not isinstance(h_str, str):
        return []
    tokens = h_str.strip().split()
    if not tokens:
        return []
    last = tokens[-1]
    parts = re.split(r'[・.]', last)
    out = []
    i = 0
    while i < len(parts):
        p = parts[i].strip()
        if p:
            try:
                out.append(int(p))
            except Exception:
                pass
        i = i + 1
    return out


def extract_grade(h_str):
    if not h_str or h_str == "なし" or not isinstance(h_str, str):
        return ""
    m = re.search(r'(GP|G1|G2|G3|F1|F2)', h_str)
    if m:
        return m.group(1)
    return ""


def calc_raw_score(p_dict):
    if not isinstance(p_dict, dict):
        return None
    full = p_dict.get('full_info', '')
    pts = extract_score_points(full)
    if pts is None or pts == 0.0:
        return None
    h1 = p_dict.get('h1', '')
    h2 = p_dict.get('h2', '')
    h3 = p_dict.get('h3', '')
    all_ranks = []
    i = 0
    hist_strs = [h1, h2, h3]
    while i < len(hist_strs):
        for r in extract_finishes(hist_strs[i]):
            rr = r
            if rr >= 8:
                rr = 7
            if rr < 1:
                continue
            all_ranks.append(rr)
        i = i + 1
    if not all_ranks:
        return None
    avg_rank = sum(all_ranks) / len(all_ranks)
    rank_penalty = avg_rank * 5.0
    g2 = extract_grade(h2)
    if g2 in ("GP", "G1", "G2", "G3"):
        gb = 5
    elif g2 == "F1":
        gb = 3
    elif g2 == "F2":
        gb = 1
    else:
        gb = 0
    return pts - rank_penalty + gb


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
    """風速クラスを返す (集計用、補正なし)
    無風(≤0.5) / 弱風(0.6-2.0) / 中風(2.1-3.5) / 強風(3.6+)
    """
    if wind_speed <= 0.5:
        return "無風"
    if wind_speed <= 2.0:
        return "弱風"
    if wind_speed <= 3.5:
        return "中風"
    return "強風"


def get_wind_pattern(venue, weather_str, venue_home_dir):
    """5分類: HB横/BH横/H追B向/H向B追/無風 + 風速クラスを返す
    返り値: (wind_pat, speed_cls) または (None, None)
    """
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


def make_bank_wind_key(bank_attrs, wind_pat, speed_cls):
    """セルキー生成
    無風時:        "{周長}|{カント}|{直線}|無風"
    風あり時:      "{周長}|{カント}|{直線}|{風向}|{風速}"
    """
    base = bank_attrs["circ"] + "|" + bank_attrs["cant"] + "|" + bank_attrs["straight"]
    if wind_pat == "無風" or speed_cls == "無風":
        return base + "|無風"
    return base + "|" + wind_pat + "|" + speed_cls


# ============================================================
# データ構造 (会場×風別 セル)
# ============================================================
def init_cell():
    by_rsrank = {}
    i = 1
    while i <= 7:
        by_rsrank[str(i)] = {
            "scene_x_count": {
                "周回中": {}, "赤板": {}, "打鐘": {},
                "ホーム": {}, "バック": {},
            },
            "rank_count": {},
        }
        i = i + 1
    return {
        "n": 0,
        "by_rsrank": by_rsrank,
        "trifecta_count": {},
        # 決まり手集計 (v6 新規)
        "kimari_1st_count": {},      # {"逃": n, "捲": n, "差": n}
        "kimari_link_count": {},     # {"逃": {"同2番手マ": n, ...}, "捲": {...}, "差": {...}}
        "kimari_total": 0,           # 決まり手有効レース数 (1着が逃/捲/差 のみ)
    }


def add_to_cell(cell, rsrank_to_bike, bike_to_rsrank, bike_scene_pos, rank_map,
                kimari_1st_normalized, kimari_link_label):
    """セルに1レース追加
    
    kimari_1st_normalized: 正規化済み 1着決まり手 ("逃"/"捲"/"差") or None
    kimari_link_label: 連動カテゴリ ("同2番手マ", "別先頭差" 等) or None
    """
    cell["n"] = cell["n"] + 1
    for rsrank_str in cell["by_rsrank"]:
        rsrank = int(rsrank_str)
        bike = rsrank_to_bike.get(rsrank)
        if bike is None:
            continue
        entry = cell["by_rsrank"][rsrank_str]
        rk = rank_map.get(bike)
        if rk is not None:
            rkey = str(rk)
            if rkey not in entry["rank_count"]:
                entry["rank_count"][rkey] = 0
            entry["rank_count"][rkey] = entry["rank_count"][rkey] + 1
        pos = bike_scene_pos.get(bike, {})
        i = 0
        while i < len(SCENES):
            s = SCENES[i]
            p = pos.get(s)
            if p:
                xkey = str(p["x"])
                d = entry["scene_x_count"][s]
                if xkey not in d:
                    d[xkey] = 0
                d[xkey] = d[xkey] + 1
            i = i + 1
    
    bike_1 = None
    bike_2 = None
    bike_3 = None
    for bike in rank_map:
        rk = rank_map[bike]
        if rk == 1:
            bike_1 = bike
        elif rk == 2:
            bike_2 = bike
        elif rk == 3:
            bike_3 = bike
    if bike_1 is not None and bike_2 is not None and bike_3 is not None:
        rs1 = bike_to_rsrank.get(bike_1)
        rs2 = bike_to_rsrank.get(bike_2)
        rs3 = bike_to_rsrank.get(bike_3)
        if rs1 is not None and rs2 is not None and rs3 is not None:
            key = str(rs1) + "-" + str(rs2) + "-" + str(rs3)
            if key not in cell["trifecta_count"]:
                cell["trifecta_count"][key] = 0
            cell["trifecta_count"][key] = cell["trifecta_count"][key] + 1
    
    # 決まり手集計 (1着が逃/捲/差 のレースのみ)
    if kimari_1st_normalized is not None:
        cell["kimari_total"] = cell["kimari_total"] + 1
        k1 = kimari_1st_normalized
        if k1 not in cell["kimari_1st_count"]:
            cell["kimari_1st_count"][k1] = 0
        cell["kimari_1st_count"][k1] = cell["kimari_1st_count"][k1] + 1
        
        if kimari_link_label is not None:
            if k1 not in cell["kimari_link_count"]:
                cell["kimari_link_count"][k1] = {}
            sub = cell["kimari_link_count"][k1]
            if kimari_link_label not in sub:
                sub[kimari_link_label] = 0
            sub[kimari_link_label] = sub[kimari_link_label] + 1


def finalize_cell(cell):
    n = cell["n"]
    if n == 0:
        return
    for rsrank_str in cell["by_rsrank"]:
        entry = cell["by_rsrank"][rsrank_str]
        rd = {}
        for k in entry["rank_count"]:
            rd[k] = round(entry["rank_count"][k] / n, 4)
        entry["rank_dist"] = rd
        sxd = {}
        for s in entry["scene_x_count"]:
            total = 0
            for x in entry["scene_x_count"][s]:
                total = total + entry["scene_x_count"][s][x]
            xd = {}
            if total > 0:
                for x in entry["scene_x_count"][s]:
                    xd[x] = round(entry["scene_x_count"][s][x] / total, 4)
            sxd[s] = xd
        entry["scene_x_dist"] = sxd
        del entry["scene_x_count"]
        del entry["rank_count"]
    
    tc = cell.get("trifecta_count", {})
    tri_list = []
    for k in tc:
        rate = round(tc[k] / n, 4)
        tri_list.append({"key": k, "n": tc[k], "rate": rate})
    tri_list.sort(key=lambda t: -t["n"])
    cell["top_trifectas"] = tri_list[:10]
    del cell["trifecta_count"]
    
    # 決まり手の確率化
    kt = cell.get("kimari_total", 0)
    k1c = cell.get("kimari_1st_count", {})
    k1_dist = {}
    if kt > 0:
        for k in k1c:
            k1_dist[k] = round(k1c[k] / kt, 4)
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
    
    # count データは残す (集計用、サイズ気になるなら削除可)
    del cell["kimari_1st_count"]
    del cell["kimari_link_count"]


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


def load_bank_data():
    if not os.path.exists(PATH_BANK_DATA):
        return {}
    f = open(PATH_BANK_DATA, "r", encoding="utf-8")
    d = json.load(f)
    f.close()
    return d


def get_bank_attrs(venue, bank_data):
    """会場の周長・カント・直線区分"""
    bd = bank_data.get(venue, {})
    if not bd:
        return None
    circ = bd.get("circ_class", bd.get("circumference_class"))
    cant = bd.get("cant_class", bd.get("cant_classification"))
    straight = bd.get("straight_class", bd.get("str_class"))
    if not circ or not cant or not straight:
        return None
    return {"circ": str(circ), "cant": str(cant), "straight": str(straight)}


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
    print("  build_rawscore_pattern_stats_v6.py")
    print("  周長×カント×直線×風向×風速 + 決まり手連動カテゴリ 統計 (v6)")
    print("=" * 78)
    
    if not os.path.exists(DB_PATH):
        print("[エラー] DBがありません")
        return
    
    venue_home_dir = load_venue_home_direction()
    bank_data = load_bank_data()
    print("  venue_home_direction: " + str(len(venue_home_dir)) + " 件")
    print("  bank_data           : " + str(len(bank_data)) + " 件")
    
    by_winner_rsrank = {}
    i = 1
    while i <= 7:
        by_winner_rsrank[str(i)] = {
            "n": 0,
            "by_bank_wind": {},
        }
        i = i + 1
    
    counters = {
        "total": 0,
        "valid_int": 0,
        "skip_integrity": 0,
        "skip_kojinsen": 0,
        "skip_not_7": 0,
        "skip_no_rank": 0,
        "skip_no_rawscore": 0,
        "skip_no_wind": 0,
        "skip_no_bank": 0,
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
        
        if not check_race_valid(rec):
            counters["skip_integrity"] = counters["skip_integrity"] + 1
            continue
        
        line_str = rec.get("line", "")
        chunks = parse_line_chunks(line_str)
        if not chunks:
            counters["skip_integrity"] = counters["skip_integrity"] + 1
            continue
        if is_kojinsen(chunks):
            counters["skip_kojinsen"] = counters["skip_kojinsen"] + 1
            continue
        
        players_dict = rec.get("players", {})
        bikes = set()
        if isinstance(players_dict, dict):
            for k in players_dict.keys():
                try:
                    n = int(k)
                    if 1 <= n <= 9:
                        bikes.add(n)
                except Exception:
                    pass
        if len(bikes) != 7:
            counters["skip_not_7"] = counters["skip_not_7"] + 1
            continue
        
        result = rec.get("result", [])
        rank_map = {}
        i = 0
        while i < len(result):
            r = result[i]
            bk = r.get("bike")
            rk = r.get("rank")
            if bk is not None and rk is not None:
                rank_map[bk] = rk
            i = i + 1
        all_ranked = True
        for b in bikes:
            if b not in rank_map:
                all_ranked = False
                break
        if not all_ranked:
            counters["skip_no_rank"] = counters["skip_no_rank"] + 1
            continue
        
        rs_map = {}
        rs_failed = False
        sorted_bikes = sorted(bikes)
        i = 0
        while i < len(sorted_bikes):
            bike = sorted_bikes[i]
            pdata = players_dict.get(str(bike), {})
            rs = calc_raw_score(pdata)
            if rs is None:
                rs_failed = True
                break
            rs_map[bike] = rs
            i = i + 1
        if rs_failed:
            counters["skip_no_rawscore"] = counters["skip_no_rawscore"] + 1
            continue
        
        venue = rec.get("place", "")
        weather_str = rec.get("weather", "")
        wind_pat, speed_cls = get_wind_pattern(venue, weather_str, venue_home_dir)
        if not venue or not wind_pat or not speed_cls:
            counters["skip_no_wind"] = counters["skip_no_wind"] + 1
            continue
        
        # バンク区分 (周長×カント×直線)
        bank_attrs = get_bank_attrs(venue, bank_data)
        if bank_attrs is None:
            counters["skip_no_bank"] = counters["skip_no_bank"] + 1
            continue
        
        counters["valid_int"] = counters["valid_int"] + 1
        
        sorted_by_rs = sorted(rs_map.items(), key=lambda kv: -kv[1])
        rsrank_to_bike = {}
        bike_to_rsrank = {}
        i = 0
        while i < len(sorted_by_rs):
            rsrank = i + 1
            bike = sorted_by_rs[i][0]
            rsrank_to_bike[rsrank] = bike
            bike_to_rsrank[bike] = rsrank
            i = i + 1
        
        winner_bike = None
        for bike in rank_map:
            if rank_map[bike] == 1:
                winner_bike = bike
                break
        if winner_bike is None:
            continue
        winner_rsrank = None
        for rsrank in rsrank_to_bike:
            if rsrank_to_bike[rsrank] == winner_bike:
                winner_rsrank = rsrank
                break
        if winner_rsrank is None:
            continue
        
        lap = rec["lap"]
        bike_scene_pos = {}
        i = 0
        while i < len(SCENES):
            s = SCENES[i]
            items = lap_to_items(lap[s])
            j = 0
            while j < len(items):
                b, x, y = items[j]
                if b not in bike_scene_pos:
                    bike_scene_pos[b] = {}
                bike_scene_pos[b][s] = {"x": x, "y": y}
                j = j + 1
            i = i + 1
        
        wkey = str(winner_rsrank)
        vw_key = make_bank_wind_key(bank_attrs, wind_pat, speed_cls)
        pat = by_winner_rsrank[wkey]
        pat["n"] = pat["n"] + 1
        if vw_key not in pat["by_bank_wind"]:
            pat["by_bank_wind"][vw_key] = init_cell()
        
        # 決まり手抽出 (1着・2着 finish 取得)
        finish_1st = None
        finish_2nd = None
        bike_1st = None
        bike_2nd = None
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
            i = i + 1
        
        k1_norm = normalize_kimari_1st(finish_1st)
        k2_norm = normalize_kimari_2nd(finish_2nd)
        link_label = None
        if k1_norm is not None and k2_norm is not None and bike_1st is not None and bike_2nd is not None:
            rel_pos = get_2nd_relative_pos(chunks, bike_1st, bike_2nd)
            if rel_pos is not None:
                link_label = rel_pos + k2_norm
        
        add_to_cell(pat["by_bank_wind"][vw_key],
                    rsrank_to_bike, bike_to_rsrank, bike_scene_pos, rank_map,
                    k1_norm, link_label)
        counters["valid_used"] = counters["valid_used"] + 1
    
    print("")
    print("---- スキャン完了 ----")
    print("  総レース             : " + str(counters["total"]))
    print("  整合性レース         : " + str(counters["valid_int"]))
    print("  集計対象             : " + str(counters["valid_used"]))
    print("  スキップ(整合性)     : " + str(counters["skip_integrity"]))
    print("  スキップ(個人戦)     : " + str(counters["skip_kojinsen"]))
    print("  スキップ(非7車)      : " + str(counters["skip_not_7"]))
    print("  スキップ(着順なし)   : " + str(counters["skip_no_rank"]))
    print("  スキップ(rawscore計算不可): " + str(counters["skip_no_rawscore"]))
    print("  スキップ(風判定不可) : " + str(counters["skip_no_wind"]))
    print("  スキップ(バンク不明) : " + str(counters["skip_no_bank"]))
    
    total = counters["valid_used"]
    print("")
    print("確率変換中...")
    for wkey in by_winner_rsrank:
        pat = by_winner_rsrank[wkey]
        n_pat = pat["n"]
        pat["occurrence_rate"] = round(n_pat / total, 4) if total > 0 else 0
        for vw in pat["by_bank_wind"]:
            finalize_cell(pat["by_bank_wind"][vw])
    
    now = datetime.datetime.now()
    output = {
        "meta": {
            "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "source_db": DB_PATH,
            "definition": "周長×カント×直線×風向×風速 + 決まり手連動カテゴリ 7パターン",
            "scenes": SCENES,
            "total_races_scanned": counters["total"],
            "total_races_used": total,
        },
        "by_winner_rsrank": by_winner_rsrank,
    }
    
    print("")
    print("保存中: " + OUTPUT_PATH)
    f = open(OUTPUT_PATH, "w", encoding="utf-8")
    json.dump(output, f, ensure_ascii=False, indent=1)
    f.close()
    size = os.path.getsize(OUTPUT_PATH)
    print("ファイルサイズ: " + str(round(size/1024/1024, 2)) + " MB")
    
    # サマリ表示
    print("")
    print("=" * 78)
    print("  サンプル数分布 (winner_rsrank別)")
    print("=" * 78)
    
    i = 1
    while i <= 7:
        wkey = str(i)
        pat = by_winner_rsrank[wkey]
        n_pat = pat["n"]
        cells = pat["by_bank_wind"]
        print("")
        print("【rs" + wkey + "が1着】 総n=" + str(n_pat) + 
              "  出現率=" + str(round(pat["occurrence_rate"]*100, 2)) + "%" +
              "  周長×カント×直線×風×風速セル数=" + str(len(cells)))
        
        n_bins = {">=50": 0, "30-49": 0, "10-29": 0, "5-9": 0, "1-4": 0}
        for vw in cells:
            n = cells[vw]["n"]
            if n >= 50:
                n_bins[">=50"] = n_bins[">=50"] + 1
            elif n >= 30:
                n_bins["30-49"] = n_bins["30-49"] + 1
            elif n >= 10:
                n_bins["10-29"] = n_bins["10-29"] + 1
            elif n >= 5:
                n_bins["5-9"] = n_bins["5-9"] + 1
            else:
                n_bins["1-4"] = n_bins["1-4"] + 1
        parts = []
        for bk in [">=50", "30-49", "10-29", "5-9", "1-4"]:
            parts.append(bk + ":" + str(n_bins[bk]))
        print("  n分布: " + " / ".join(parts))
        
        top_cells = sorted(cells.items(), key=lambda kv: -kv[1]["n"])[:5]
        print("  上位5セル:")
        j = 0
        while j < len(top_cells):
            vw, c = top_cells[j]
            print("    " + (vw + "                              ")[:32] + " n=" + str(c["n"]))
            j = j + 1
        i = i + 1
    
    print("")
    print("=" * 78)
    print("Step A (v3) 完了")
    print("出力: " + OUTPUT_PATH)
    print("=" * 78)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
build_kimari_player_stats.py — 選手別 決まり手集計

build_kimari_stats_v3.py の風判定・ライン判定・正規化を流用。
集計キー: "氏名|期"
決まり手(1着2着以下すべて): 逃/捲/差/マ
役割: そのレースの自分のライン内位置 先頭/番手/3番手/4番手.../単騎

出力2ファイル:
  (A) 出走表用(全期間・役割別)         kimari_player_role.jsonl
      1行1選手: {"k":"氏名|期","name":..,"period":..,
                 "by_role":{"先頭":{"逃":n,"捲":n,"差":n,"マ":n,"_n":総数}, ...},
                 "total":{"逃":n,...,"_n":総数}}
  (B) 分析グラフ用(多次元・疎)          kimari_player_cond.jsonl
      1行1選手: {"k":..,"name":..,"period":..,
                 "cells":{ "車立て|役割|条件セル": {"逃":n,"捲":n,"差":n,"マ":n,"_n":総数}, ...}}
      条件セル = build_kimari_stats_v3 と同じ "{会場}|無風" or "{会場}|{風向}|{風速}"
      複合キー例: "7|先頭|別府|H追B向|弱風"

入力:
  /storage/emulated/0/Download/keirin_data_scored_v2.jsonl
  /storage/emulated/0/Download/venue_home_direction.json

Pydroid3制約: f-string不可、for-else不可
"""

import os
import re
import json
import datetime

DOWNLOAD_DIR = "/storage/emulated/0/Download"
# 入力DBの解決順:
#   1. 環境変数 KEIRIN_DB (Actions等で本番DBを明示)
#   2. takusen/data/keirin_data_scored_v2.jsonl (アプリ本番DBと一致)
#   3. Download直下 (旧来の場所・後方互換)
#   4. カレント (Actionsでリポジトリ直下に一体型DBがある場合)
_DATA_DIR = os.path.join(DOWNLOAD_DIR, "takusen", "data")
if not os.path.isdir(_DATA_DIR):
    if os.path.isdir(os.path.join(os.getcwd(), "takusen", "data")):
        _DATA_DIR = os.path.join(os.getcwd(), "takusen", "data")
    elif os.path.isdir(DOWNLOAD_DIR):
        _DATA_DIR = DOWNLOAD_DIR
    else:
        _DATA_DIR = os.getcwd()

# 出力先 (FINAL名でアプリ読込名に直接一致させる。リネーム廃止)
SAVE_DIR = _DATA_DIR

_ENV_DB = os.environ.get("KEIRIN_DB", "").strip()
if _ENV_DB:
    DB_PATH = _ENV_DB
else:
    DB_PATH = os.path.join(_DATA_DIR, "keirin_data_scored_v2.jsonl")
    if not os.path.exists(DB_PATH):
        # フォールバック: Download直下 / カレント
        _alt1 = os.path.join(DOWNLOAD_DIR, "keirin_data_scored_v2.jsonl")
        _alt2 = os.path.join(os.getcwd(), "keirin_data_scored_v2.jsonl")
        if os.path.exists(_alt1):
            DB_PATH = _alt1
        elif os.path.exists(_alt2):
            DB_PATH = _alt2

# venue_home_direction.json も同様に探す (無ければNone扱い)
PATH_VENUE_HOME_DIR = os.path.join(SAVE_DIR, "venue_home_direction.json")
if not os.path.exists(PATH_VENUE_HOME_DIR):
    _vh_alt = os.path.join(DOWNLOAD_DIR, "venue_home_direction.json")
    if os.path.exists(_vh_alt):
        PATH_VENUE_HOME_DIR = _vh_alt

# 出力: アプリ読込名 kimari_player_role_FINAL.jsonl に直接出力。
# アプリ(predict_today_v31)は DICTS_DIR = takusen/data/dicts/ から読むため、
# 既定の出力先も dicts/ サブフォルダにする (build_all_dicts_v1 と同じ場所)。
# KEIRIN_OUT_ROLE で上書き可 (統合ランナー用)。
_DICTS_DIR = os.path.join(SAVE_DIR, "dicts")
if not os.path.isdir(_DICTS_DIR):
    # dicts/ が無ければ作る (初回や別環境向け)
    try:
        os.makedirs(_DICTS_DIR)
    except Exception:
        _DICTS_DIR = SAVE_DIR
_env_role = os.environ.get("KEIRIN_OUT_ROLE", "").strip()
if _env_role:
    OUT_ROLE = _env_role
else:
    OUT_ROLE = os.path.join(_DICTS_DIR, "kimari_player_role_FINAL.jsonl")
_env_cond = os.environ.get("KEIRIN_OUT_COND", "").strip()
if _env_cond:
    OUT_COND = _env_cond
else:
    OUT_COND = os.path.join(_DICTS_DIR, "kimari_player_cond.jsonl")

DOME_VENUES = {"前橋", "小倉", "千葉"}


# ========== ライン解析 (v3流用) ==========
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
            if p[j].isdigit():
                digits.append(p[j])
            j = j + 1
        if digits:
            chunks.append(digits)
        i = i + 1
    return chunks


def make_bike_role_map(chunks):
    """車番 -> 役割ラベル(先頭/番手/3番手.../単騎)"""
    bike_to_role = {}
    i = 0
    while i < len(chunks):
        chunk = chunks[i]
        size = len(chunk)
        j = 0
        while j < len(chunk):
            try:
                bike = int(chunk[j])
            except Exception:
                j = j + 1
                continue
            if size == 1:
                bike_to_role[bike] = "単騎"
            else:
                if j == 0:
                    bike_to_role[bike] = "先頭"
                elif j == 1:
                    bike_to_role[bike] = "番手"
                else:
                    bike_to_role[bike] = str(j + 1) + "番手"
            j = j + 1
        i = i + 1
    return bike_to_role


# ========== 風判定 (v3流用) ==========
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
    if wind_pat == "無風" or speed_cls == "無風":
        return venue + "|無風"
    return venue + "|" + wind_pat + "|" + speed_cls


# ========== 決まり手正規化 (1着/2着以下を統合して4分類) ==========
def normalize_kimari_any(finish):
    if finish in ("逃切り", "逃残", "逃残り", "逃げ"):
        return "逃"
    if finish in ("まくり", "捲残", "捲残り"):
        return "捲"
    if finish == "追込み":
        return "差"
    if finish == "マーク":
        return "マ"
    return None


# ========== player_key ==========
def make_player_key(full_info):
    """ '氏名/府県/年齢/期/点' -> '氏名|期' """
    if not full_info or not isinstance(full_info, str):
        return None, None, None
    parts = full_info.split("/")
    if len(parts) < 4:
        return None, None, None
    name = parts[0].strip()
    period = parts[3].strip()  # 例 "94期"
    if not name or not period:
        return None, None, None
    return name + "|" + period, name, period


# ========== ロード ==========
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
        yield rec
    f.close()


# ========== 集計用ヘルパ ==========
def add4(d, kim):
    # d: {"逃":n,"捲":n,"差":n,"マ":n,"_n":n}
    if kim not in d:
        d[kim] = 0
    d[kim] = d[kim] + 1
    d["_n"] = d.get("_n", 0) + 1


def main():
    print("=" * 70)
    print("  build_kimari_player_stats.py  選手別決まり手集計")
    print("=" * 70)
    if not os.path.exists(DB_PATH):
        print("[エラー] DBがありません: " + DB_PATH)
        return

    venue_home_dir = load_venue_home_direction()
    print("  venue_home_direction: " + str(len(venue_home_dir)) + " 件")

    # 出走表用: pk -> {"name","period","by_role":{role:{4分類}}, "total":{4分類}}
    role_stats = {}
    # 分析用: pk -> {"name","period","cells":{ "車立て|役割|条件セル":{4分類} }}
    cond_stats = {}

    counters = {"total": 0, "used_rows": 0, "skip_no_line": 0, "skip_no_wind": 0}

    progress = 0
    for rec in iter_races(DB_PATH):
        counters["total"] = counters["total"] + 1
        progress = progress + 1
        if progress % 10000 == 0:
            print("  " + str(progress) + " レース...")

        players = rec.get("players", {})
        if not isinstance(players, dict):
            continue
        # 出走人数(車立て)
        bikes = []
        for k in players.keys():
            try:
                nb = int(k)
                bikes.append(nb)
            except Exception:
                pass
        n_field = len(bikes)
        if n_field < 2:
            continue

        line_str = rec.get("line", "")
        chunks = parse_line_chunks(line_str)
        if not chunks:
            counters["skip_no_line"] = counters["skip_no_line"] + 1
            continue
        bike_role = make_bike_role_map(chunks)

        # 風判定(条件セル)。分析用のみ使用。判定不可でも出走表用は集計する
        venue = rec.get("place", "")
        weather_str = rec.get("weather", "")
        wind_pat, speed_cls = get_wind_pattern(venue, weather_str, venue_home_dir)
        cond_cell = None
        if venue and wind_pat and speed_cls:
            cond_cell = make_cell_key(venue, wind_pat, speed_cls)

        # 結果(各着の車番と決まり手)
        result = rec.get("result", [])
        if not isinstance(result, list):
            continue

        i = 0
        while i < len(result):
            r = result[i]
            i = i + 1
            if not isinstance(r, dict):
                continue
            finish = r.get("finish")
            bike = r.get("bike")
            if bike is None:
                continue
            kim = normalize_kimari_any(finish)
            if kim is None:
                continue  # "--" など決まり手なしは除外

            # 車番 -> 選手 full_info
            pdict = players.get(str(bike))
            if not isinstance(pdict, dict):
                continue
            full = pdict.get("full_info", "")
            pk, name, period = make_player_key(full)
            if pk is None:
                continue

            role = bike_role.get(bike)
            if role is None:
                continue

            # (A) 出走表用: total と by_role
            if pk not in role_stats:
                role_stats[pk] = {"name": name, "period": period,
                                  "by_role": {}, "total": {}}
            rs = role_stats[pk]
            add4(rs["total"], kim)
            if role not in rs["by_role"]:
                rs["by_role"][role] = {}
            add4(rs["by_role"][role], kim)

            # (B) 分析用: 車立て|役割|条件セル
            if cond_cell is not None:
                if pk not in cond_stats:
                    cond_stats[pk] = {"name": name, "period": period, "cells": {}}
                cs = cond_stats[pk]
                ckey = str(n_field) + "|" + role + "|" + cond_cell
                if ckey not in cs["cells"]:
                    cs["cells"][ckey] = {}
                add4(cs["cells"][ckey], kim)

            counters["used_rows"] = counters["used_rows"] + 1

    print("")
    print("---- 集計完了 ----")
    print("  総レース    : " + str(counters["total"]))
    print("  集計行(着)  : " + str(counters["used_rows"]))
    print("  選手数(役割): " + str(len(role_stats)))
    print("  選手数(条件): " + str(len(cond_stats)))

    # 出力(A) 出走表用
    print("")
    print("保存中(出走表用): " + OUT_ROLE)
    f = open(OUT_ROLE, "w", encoding="utf-8")
    for pk in role_stats:
        rs = role_stats[pk]
        row = {"k": pk, "name": rs["name"], "period": rs["period"],
               "by_role": rs["by_role"], "total": rs["total"]}
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    f.close()
    print("  サイズ: " + str(round(os.path.getsize(OUT_ROLE) / 1024.0 / 1024.0, 2)) + " MB")

    # 出力(B) 分析用
    print("保存中(分析用): " + OUT_COND)
    f = open(OUT_COND, "w", encoding="utf-8")
    for pk in cond_stats:
        cs = cond_stats[pk]
        row = {"k": pk, "name": cs["name"], "period": cs["period"], "cells": cs["cells"]}
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    f.close()
    print("  サイズ: " + str(round(os.path.getsize(OUT_COND) / 1024.0 / 1024.0, 2)) + " MB")

    # メタ情報
    meta = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_db": DB_PATH,
        "players_role": len(role_stats),
        "players_cond": len(cond_stats),
        "rows_used": counters["used_rows"],
        "normalize": {"逃": ["逃切り", "逃残", "逃残り", "逃げ"],
                      "捲": ["まくり", "捲残", "捲残り"],
                      "差": ["追込み"], "マ": ["マーク"]},
        "role_def": "ライン内位置 先頭/番手/3番手.../単騎",
        "cond_cell": "build_kimari_stats_v3互換 {会場}|無風 or {会場}|{風向}|{風速}",
    }
    f = open(os.path.join(SAVE_DIR, "kimari_player_meta.json"), "w", encoding="utf-8")
    json.dump(meta, f, ensure_ascii=False, indent=1)
    f.close()

    # サンプル表示
    print("")
    print("  サンプル(出走表用 先頭2選手):")
    shown = 0
    for pk in role_stats:
        if shown >= 2:
            break
        rs = role_stats[pk]
        print("    " + pk + " total=" + json.dumps(rs["total"], ensure_ascii=False))
        roles = list(rs["by_role"].keys())
        print("      役割: " + ", ".join(roles))
        shown = shown + 1

    print("")
    print("完了")
    print("  " + OUT_ROLE)
    print("  " + OUT_COND)


if __name__ == "__main__":
    main()

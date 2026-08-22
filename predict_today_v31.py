# -*- coding: utf-8 -*-
"""
predict_today_v31.py — 予測システム v31 (周長×カント×直線×風向×風速別 適合率)

【v31の変更点】
  - Pydroid3で__file__が未定義でも動くよう takusen/code をimportパスに明示追加

【v30の変更点】
  - フォルダ体系を Download/takusen/ に一本化
      コード: takusen/code/
      DB:     takusen/data/keirin_data_scored_v2.jsonl
              (なければ旧keirin_db→従来位置に自動フォールバック)
      辞書:   takusen/data/dicts/*_FINAL.*  (DB由来・再生成対象)
      静的:   takusen/data/static/*
      キャッシュ: takusen/data/cache_today/

【v29の変更点】
  - データファイルをFINAL体系に一新
  - 未使用だった role_style_stats / overperf / profiles_v3 / bank_wind_role_stats を削除
  - check_dict_files() 追加 (必須ファイルの存在チェック)

ロジック:
  1. 過去全レースから「rawscore○位が1着」7パターンを
     周長×カント×直線×風向×風速 別に集計済み
     (rawscore_pattern_stats_FINAL.json)
  2. 今日のレースで風速を -1m 補正してから風速クラス判定
  3. 該当セルとの「適合率」を計算
  4. 過去出現率TOP4 + 適合率1位を表示

セルキー (集計時=生風速、予想時=-2m補正後):
  無風時:   "{周長}|{カント}|{直線}|無風"
  風あり時: "{周長}|{カント}|{直線}|{風向}|{風速}"

風速分類:
  無風(≤0.5) / 弱風(0.6-2.0) / 中風(2.1-3.5) / 強風(3.6+)

機能:
  1. 日付指定でレース取得
  2. ライン情報なし会場の再取得
  3. 天候リフレッシュ
  4. 会場選択 → レース選択
  5. ヘッダー・ライン構成・raw_score 表示
  6. 7パターン適合率 + 買い目フォーメーション

データソース (FINAL体系):
  - キャッシュ: takusen/data/cache_today/cache_<date>.json
  - レース取得engine: predict_v14_wind_unified.py
  - DB: takusen/data/keirin_data_scored_v2.jsonl
  - 7パターン統計: dicts/rawscore_pattern_stats_FINAL.json
  - 決まり手セル統計: dicts/kimari_stats_FINAL.json
  - 選手プロファイル: dicts/player_profiles_FINAL/ + player_profile_index_FINAL.json
  - ライン先頭率: dicts/player_line_lead_rate_FINAL.json
  - 会場ホーム方角: static/venue_home_direction.json
  - バンクデータ: static/bank_data.json

Pydroid3制約: f-string不可、for-else不可、完全コード提供
"""

import os
import sys
import json
import re
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# パス設定
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd()
DOWNLOAD_DIR = "/storage/emulated/0/Download"
SAVE_DIR = DOWNLOAD_DIR if os.path.exists(DOWNLOAD_DIR) else os.getcwd()

# engine モジュール (レース取得)
# Pydroid3は__file__を定義しないことがあるため takusen/code を明示追加
_TAKUSEN_CODE = "/storage/emulated/0/Download/takusen/code"
for _path in [SCRIPT_DIR, _TAKUSEN_CODE, DOWNLOAD_DIR, os.getcwd()]:
    if _path and os.path.exists(_path) and _path not in sys.path:
        sys.path.insert(0, _path)

try:
    import predict_v14_wind_unified as engine
except ImportError:
    print("[エラー] predict_v14_wind_unified.py が必要")
    print("  → " + DOWNLOAD_DIR + " または同ディレクトリに配置してください")
    sys.exit(1)

# ============================================================
# 託宣 (takusen) フォルダ体系
#   Download/takusen/code  : 実行コード (本ファイル含む)
#   Download/takusen/data  : DB・辞書・キャッシュ等の全データ
#   Download/takusen/docs  : ドキュメント
# ============================================================
# 置き場所。端末では従来どおり Download/takusen を使う。
#   GitHub Actions のように別の場所で動かすときは、環境変数
#   TAKUSEN_DIR でリポジトリ内のフォルダを指せるようにする。
#   環境変数が無ければ従来と完全に同じ挙動になる。
TAKUSEN_DIR = (os.environ.get("TAKUSEN_DIR", "").strip()
               or "/storage/emulated/0/Download/takusen")
DATA_DIR = os.path.join(TAKUSEN_DIR, "data")
DICTS_DIR = os.path.join(DATA_DIR, "dicts")
STATIC_DIR = os.path.join(DATA_DIR, "static")
CACHE_DIR = os.path.join(DATA_DIR, "cache_today")
KEIRIN_DB_DIR = DATA_DIR  # 旧名互換

# DB: takusen/data → 旧keirin_db → 従来Download直下 の順で探す
_DB_CANDIDATES = [
    os.path.join(DATA_DIR, "keirin_data_scored_v2.jsonl"),
    os.path.join(SAVE_DIR, "keirin_db", "keirin_data_scored_v2.jsonl"),
    os.path.join(SAVE_DIR, "keirin_data_scored_v2.jsonl"),
]
PATH_DB = _DB_CANDIDATES[-1]
for _c in _DB_CANDIDATES:
    if os.path.exists(_c):
        PATH_DB = _c
        break

# 選手プロファイル系 (FINAL体系)
PROFILES_DIR = os.path.join(DICTS_DIR, "player_profiles_FINAL")
PATH_INDEX = os.path.join(DICTS_DIR, "player_profile_index_FINAL.json")
PATH_LINE_LEAD = os.path.join(DICTS_DIR, "player_line_lead_rate_FINAL.json")

# rawscore順位パターン統計・決まり手セル統計 (FINAL体系)
PATH_RAWSCORE_PATTERN_STATS = os.path.join(DICTS_DIR, "rawscore_pattern_stats_FINAL.json")
PATH_KIMARI_STATS = os.path.join(DICTS_DIR, "kimari_stats_FINAL.json")

# 補助データ (静的・再生成不要)
PATH_VENUE_HOME_DIR = os.path.join(STATIC_DIR, "venue_home_direction.json")
PATH_BANK_DATA = os.path.join(STATIC_DIR, "bank_data.json")

# 【v29で削除】未使用だった以下の定義は撤去済み:
#   role_style_stats_v1 / player_overperformance_v1 /
#   player_profiles_v3 / player_profile_index_v3 / bank_wind_role_stats_v1


def check_dict_files():
    """必須データファイルの存在チェック。欠けているパスのリストを返す"""
    targets = [
        PATH_DB,
        PATH_INDEX,
        PROFILES_DIR,
        PATH_LINE_LEAD,
        PATH_RAWSCORE_PATTERN_STATS,
        PATH_KIMARI_STATS,
        PATH_VENUE_HOME_DIR,
        PATH_BANK_DATA,
    ]
    missing = []
    for t in targets:
        if not os.path.exists(t):
            missing.append(t)
    return missing

# 会場 → 都道府県マッピング (地元判定用)
VENUE_PREF = {
    "函館": "北海道", "青森": "青森", "いわき平": "福島", "弥彦": "新潟",
    "前橋": "群馬", "取手": "茨城", "宇都宮": "栃木", "大宮": "埼玉",
    "西武園": "埼玉", "京王閣": "東京", "立川": "東京", "川崎": "神奈川",
    "平塚": "神奈川", "小田原": "神奈川", "伊東": "静岡", "伊東温泉": "静岡",
    "静岡": "静岡", "松阪": "三重", "名古屋": "愛知", "岐阜": "岐阜",
    "大垣": "岐阜", "豊橋": "愛知", "富山": "富山", "四日市": "三重",
    "福井": "福井", "奈良": "奈良", "向日町": "京都", "和歌山": "和歌山",
    "岸和田": "大阪", "玉野": "岡山", "広島": "広島", "防府": "山口",
    "高松": "香川", "小松島": "徳島", "高知": "高知", "松山": "愛媛",
    "小倉": "福岡", "久留米": "福岡", "武雄": "佐賀", "佐世保": "長崎",
    "別府": "大分", "熊本": "熊本",
}


def is_local_player(origin, venue):
    if not origin or not venue:
        return False
    venue_pref = VENUE_PREF.get(venue, "")
    if not venue_pref:
        return False
    origin_norm = origin.replace("県", "").replace("府", "").replace("都", "").strip()
    venue_norm = venue_pref.replace("県", "").replace("府", "").replace("都", "").strip()
    return origin_norm == venue_norm


# ============================================================
# パラメータ
# ============================================================
SCENES = ["周回中", "赤板", "打鐘", "ホーム", "バック"]
DOME_VENUES = {"前橋", "小倉", "千葉"}

MIN_N_PLAYER_ROLE = 10
MIN_N_POP_ROLE = 50


# ============================================================
# キャッシュ・ファイル基本
# ============================================================
def cache_path(date_str):
    if not os.path.exists(CACHE_DIR):
        try:
            os.makedirs(CACHE_DIR)
        except Exception:
            pass
    return os.path.join(CACHE_DIR, "cache_" + date_str + ".json")


def load_cache(date_str):
    p = cache_path(date_str)
    if not os.path.exists(p):
        return None
    try:
        f = open(p, "r", encoding="utf-8")
        data = json.load(f)
        f.close()
        return data
    except Exception:
        return None


def save_cache(date_str, races):
    p = cache_path(date_str)
    try:
        f = open(p, "w", encoding="utf-8")
        json.dump(races, f, ensure_ascii=False)
        f.close()
        return True
    except Exception as e:
        print("[警告] キャッシュ保存失敗: " + str(e))
        return False


def parse_post_time(post_time_str, target_date):
    """発走時刻文字列をdatetimeに変換"""
    if not post_time_str:
        return None
    s = post_time_str.strip()
    m = re.match(r'(\d{1,2}):(\d{2})', s)
    if not m:
        return None
    try:
        h = int(m.group(1))
        mi = int(m.group(2))
        if h < 0 or h > 23 or mi < 0 or mi > 59:
            return None
        return target_date.replace(hour=h, minute=mi, second=0, microsecond=0)
    except Exception:
        return None


SKIP_GRACE_MINUTES = 0


# ============================================================
# データロード (v2)
# ============================================================
_cache = {}


def load_index():
    if "index" in _cache:
        return _cache["index"]
    if not os.path.exists(PATH_INDEX):
        return None
    f = open(PATH_INDEX, "r", encoding="utf-8")
    _cache["index"] = json.load(f)
    f.close()
    return _cache["index"]


def load_profile(player_id):
    fp = os.path.join(PROFILES_DIR, player_id + ".json")
    if not os.path.exists(fp):
        return None
    f = open(fp, "r", encoding="utf-8")
    prof = json.load(f)
    f.close()
    return prof


def load_line_lead_data():
    if "lld" in _cache:
        return _cache["lld"]
    if not os.path.exists(PATH_LINE_LEAD):
        return None
    f = open(PATH_LINE_LEAD, "r", encoding="utf-8")
    data = json.load(f)
    f.close()
    _cache["lld"] = data.get("players", {})
    return _cache["lld"]


def load_rawscore_pattern_stats():
    """v9: 7パターン統計 (rawscore○位が1着)"""
    if "rps" in _cache:
        return _cache["rps"]
    if not os.path.exists(PATH_RAWSCORE_PATTERN_STATS):
        return None
    f = open(PATH_RAWSCORE_PATTERN_STATS, "r", encoding="utf-8")
    data = json.load(f)
    f.close()
    _cache["rps"] = data
    return _cache["rps"]


def load_kimari_stats():
    """会場×風向×風速×天気別 決まり手集計"""
    if "ks" in _cache:
        return _cache["ks"]
    if not os.path.exists(PATH_KIMARI_STATS):
        _cache["ks"] = None
        return None
    try:
        f = open(PATH_KIMARI_STATS, "r", encoding="utf-8")
        data = json.load(f)
        f.close()
        _cache["ks"] = data
    except Exception:
        _cache["ks"] = None
    return _cache["ks"]


def load_venue_home_direction():
    if not os.path.exists(PATH_VENUE_HOME_DIR):
        return {}
    try:
        f = open(PATH_VENUE_HOME_DIR, "r", encoding="utf-8")
        d = json.load(f)
        f.close()
        return d
    except Exception:
        return {}


def load_bank_data():
    if not os.path.exists(PATH_BANK_DATA):
        return {}
    try:
        f = open(PATH_BANK_DATA, "r", encoding="utf-8")
        d = json.load(f)
        f.close()
        return d
    except Exception:
        return {}


# ============================================================
# パース (v42互換)
# ============================================================
def parse_full_info(full_info):
    """選手 full_info を分解
    "伊藤 翼/神奈川/38歳/94期/77.76点" → dict
    """
    out = {"name": "", "origin": "", "age": None, "period": "", "score": None}
    if not full_info or not isinstance(full_info, str):
        return out
    parts = full_info.split('/')
    if len(parts) >= 1:
        out["name"] = parts[0].strip()
    if len(parts) >= 2:
        out["origin"] = parts[1].strip()
    if len(parts) >= 3:
        m = re.search(r'(\d+)', parts[2])
        if m:
            try:
                out["age"] = int(m.group(1))
            except Exception:
                pass
    if len(parts) >= 4:
        out["period"] = parts[3].strip()
    if len(parts) >= 5:
        m = re.search(r'([\d.]+)', parts[4])
        if m:
            try:
                out["score"] = float(m.group(1))
            except Exception:
                pass
    return out


def make_player_id(full_info):
    if not full_info or not isinstance(full_info, str):
        return None
    parts = full_info.split("/")
    if len(parts) < 4:
        return None
    name = parts[0].strip().replace(" ", "").replace("　", "")
    m = re.match(r'(\d+)', parts[3].strip())
    if not m:
        return None
    return name + "_" + m.group(1)


def parse_line_chunks(line_str):
    if not line_str or not isinstance(line_str, str):
        return None
    line_str = line_str.replace("ー", "-").replace("−", "-").replace("―", "-")
    parts = line_str.split("-")
    chunks = []
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
    if not chunks:
        return None
    return chunks


def is_all_solo(chunks):
    if not chunks:
        return False
    i = 0
    while i < len(chunks):
        if len(chunks[i]) != 1:
            return False
        i = i + 1
    return True


def line_best_waku(chunk):
    if not chunk:
        return 99
    nums = []
    i = 0
    while i < len(chunk):
        try:
            nums.append(int(chunk[i]))
        except Exception:
            pass
        i = i + 1
    if not nums:
        return 99
    return min(nums)


# ============================================================
# raw_score 計算 (v42 から)
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


def extract_h_venue_md(h_str):
    if not h_str or h_str == "なし" or not isinstance(h_str, str):
        return None
    tokens = h_str.strip().split()
    if len(tokens) < 3:
        return None
    venue = tokens[0]
    i = 1
    while i < len(tokens):
        t = tokens[i]
        m = re.match(r'^(\d{1,2})/(\d{1,2})$', t)
        if m:
            try:
                mm = int(m.group(1))
                dd = int(m.group(2))
                if 1 <= mm <= 12 and 1 <= dd <= 31:
                    return (venue, mm, dd)
            except Exception:
                pass
        i = i + 1
    return None


def md_to_full_date(mm, dd, today_yyyymmdd):
    try:
        y = int(today_yyyymmdd[:4])
        today_m = int(today_yyyymmdd[4:6])
        today_d = int(today_yyyymmdd[6:8])
    except Exception:
        return None
    if (mm, dd) > (today_m, today_d):
        y = y - 1
    return "{:04d}{:02d}{:02d}".format(y, mm, dd)


def days_diff(date_a_yyyymmdd, date_b_yyyymmdd):
    try:
        from datetime import date as _date
        a = _date(int(date_a_yyyymmdd[:4]), int(date_a_yyyymmdd[4:6]),
                  int(date_a_yyyymmdd[6:8]))
        b = _date(int(date_b_yyyymmdd[:4]), int(date_b_yyyymmdd[4:6]),
                  int(date_b_yyyymmdd[6:8]))
        return (a - b).days
    except Exception:
        return None


def is_long_layoff(h2_str, today_yyyymmdd):
    if not h2_str or h2_str == "なし" or not today_yyyymmdd:
        return False
    info = extract_h_venue_md(h2_str)
    if not info:
        return False
    _, mm, dd = info
    h2_date = md_to_full_date(mm, dd, today_yyyymmdd)
    if not h2_date:
        return False
    diff = days_diff(today_yyyymmdd, h2_date)
    if diff is None:
        return False
    return diff >= 60


def extract_grade(h_str):
    if not h_str or h_str == "なし" or not isinstance(h_str, str):
        return ""
    m = re.search(r'(GP|G1|G2|G3|F1|F2)', h_str)
    if m:
        return m.group(1)
    full2half = {"Ｇ": "G", "Ｆ": "F", "１": "1", "２": "2", "３": "3"}
    s = h_str
    for k in full2half:
        s = s.replace(k, full2half[k])
    m = re.search(r'(GP|G1|G2|G3|F1|F2)', s)
    if m:
        return m.group(1)
    return ""


def load_db_records_by_day_venue(date_yyyymmdd, venue, db_path):
    """DB の指定日×会場のレコード一覧を返す"""
    if not os.path.exists(db_path):
        return []
    out = []
    f = open(db_path, "r", encoding="utf-8")
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if str(rec.get("date", "")) == date_yyyymmdd and rec.get("place") == venue:
            out.append(rec)
    f.close()
    return out


def find_prev_score_for_player(player_name, h2_str, today_yyyymmdd, db_path):
    """h2 のレースから当該選手の競走得点を引く (フォールバック用)"""
    if not h2_str or h2_str == "なし":
        return None
    info = extract_h_venue_md(h2_str)
    if not info:
        return None
    venue, mm, dd = info
    h2_date = md_to_full_date(mm, dd, today_yyyymmdd)
    if not h2_date:
        return None
    recs = load_db_records_by_day_venue(h2_date, venue, db_path)
    if not recs:
        return None
    nm_norm = player_name.replace(" ", "").replace("　", "").strip()
    i = 0
    while i < len(recs):
        rec = recs[i]
        players = rec.get("players", {})
        if isinstance(players, dict):
            for bs in players:
                pd = players[bs]
                if not isinstance(pd, dict):
                    continue
                fi = pd.get("full_info", "")
                nm = fi.split("/")[0].strip().replace(" ", "").replace("　", "")
                if nm == nm_norm:
                    pts = extract_score_points(fi)
                    if pts is not None and pts > 0:
                        return pts
        i = i + 1
    return None


def calc_raw_score_from_player(p_dict, today_yyyymmdd=None, db_path=None):
    """raw_score を計算
    返り値: (raw_score, is_fallback)
    """
    if not isinstance(p_dict, dict):
        return (None, False)
    full = p_dict.get('full_info', '')
    pts = extract_score_points(full)
    is_fallback = False
    
    if (pts is None or pts == 0.0) and today_yyyymmdd and db_path:
        info_p = parse_full_info(full)
        nm = info_p["name"]
        h2 = p_dict.get('h2', '')
        if nm and h2:
            prev_pts = find_prev_score_for_player(nm, h2, today_yyyymmdd, db_path)
            if prev_pts is not None and prev_pts > 0:
                pts = prev_pts
                is_fallback = True
    
    if pts is None or pts == 0.0:
        return (None, is_fallback)
    
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
        return (None, is_fallback)
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
    return (round(pts - rank_penalty + gb, 2), is_fallback)


def get_raw_score_with_fallback(p_dict, today_yyyymmdd=None, db_path=None):
    """raw_score は常に再計算"""
    return calc_raw_score_from_player(p_dict, today_yyyymmdd, db_path)


# ============================================================
# 風判定 (v42 から)
# ============================================================
_DIR_TO_DEG = {
    "N": 0, "NE": 45, "E": 90, "SE": 135,
    "S": 180, "SW": 225, "W": 270, "NW": 315,
}

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

_REL_DEG_TO_ARROW = {
    0: "↑", 45: "↗", 90: "→", 135: "↘",
    180: "↓", 225: "↙", 270: "←", 315: "↖",
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


def get_wind_arrow(venue, weather_str, venue_home_dir):
    if not venue or not weather_str or not venue_home_dir:
        return ""
    home_dir = venue_home_dir.get(venue)
    if not home_dir:
        return ""
    home_deg = _HOME_DIR_TO_DEG.get(home_dir)
    if home_deg is None:
        return ""
    wind_jp = parse_wind_dir_jp(weather_str)
    if not wind_jp or wind_jp not in _WIND_JP_TO_DEG:
        return ""
    wind_from_deg = _WIND_JP_TO_DEG[wind_jp]
    wind_to_deg = (wind_from_deg + 180) % 360
    rotation = 180 - home_deg
    rel_deg = (wind_to_deg + rotation) % 360
    snapped = int(round(rel_deg / 45.0)) * 45 % 360
    return _REL_DEG_TO_ARROW.get(snapped, "")


def judge_wind_advantage(venue, weather_str, venue_home_dir):
    if not venue or not weather_str:
        return None
    if not venue_home_dir:
        return None
    if "風速:--" in weather_str or "風向:--" in weather_str:
        return "無風"
    wind_speed = parse_wind_speed(weather_str)
    if wind_speed is not None and wind_speed <= 0.5:
        return "無風"
    if venue in DOME_VENUES:
        return "無風"
    stand_en = venue_home_dir.get(venue)
    if not stand_en or stand_en not in _DIR_TO_DEG:
        return None
    wind_jp = parse_wind_dir_jp(weather_str)
    if not wind_jp or wind_jp not in _WIND_JP_TO_DEG:
        return None
    stand_deg = _DIR_TO_DEG[stand_en]
    running_deg = (stand_deg - 90) % 360
    wind_deg = _WIND_JP_TO_DEG[wind_jp]
    diff = abs(running_deg - wind_deg) % 360
    diff = min(diff, 360 - diff)
    if diff < 90:
        return "捲り有利"
    return "逃げ有利"


# ============================================================
# 風パターン分類 (Phase 1/Phase 2 と同じロジック)
# ============================================================
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


def classify_speed_adjusted(wind_speed_raw):
    """予想時用: 公式風速から -1m 補正してから風速クラスを返す
    無風(≤0.5) / 弱風(0.6-2.0) / 中風(2.1-3.5) / 強風(3.6+)
    """
    adj = wind_speed_raw - 1.0
    if adj <= 0.5:
        return "無風", adj
    if adj <= 2.0:
        return "弱風", adj
    if adj <= 3.5:
        return "中風", adj
    return "強風", adj


def get_wind_pattern(venue, weather_str, venue_home_dir):
    """v15: 風向 + 風速クラスを返す (予想用、-1m補正済み)
    返り値: (wind_pat, speed_cls, adj_speed) or (None, None, None)
      wind_pat: H追B向/H向B追/HB横/BH横/無風
      speed_cls: 無風/弱風/中風/強風
      adj_speed: 補正後風速 (float)
    """
    if venue in DOME_VENUES:
        return "無風", "無風", 0.0
    if not weather_str:
        return None, None, None
    if "風速:--" in weather_str or "風向:--" in weather_str:
        return "無風", "無風", 0.0
    wind_speed = parse_wind_speed(weather_str)
    if wind_speed is None:
        return None, None, None
    speed_cls, adj_speed = classify_speed_adjusted(wind_speed)
    if speed_cls == "無風":
        return "無風", "無風", adj_speed
    wind_jp = parse_wind_dir_jp(weather_str)
    if not wind_jp or wind_jp not in _WIND_JP_TO_DEG:
        return None, None, None
    wind_from_deg = _WIND_JP_TO_DEG[wind_jp]
    if not venue_home_dir:
        return None, None, None
    home_dir_jp = venue_home_dir.get(venue)
    if not home_dir_jp:
        return None, None, None
    home_deg = _HOME_DIR_TO_DEG.get(home_dir_jp)
    if home_deg is None:
        return None, None, None
    home_running = (home_deg - 90) % 360
    back_running = (home_running + 180) % 360
    home_cls = classify_wind_at_direction(wind_from_deg, home_running)
    back_cls = classify_wind_at_direction(wind_from_deg, back_running)
    if home_cls == "追い風" and back_cls == "向かい風":
        return "H追B向", speed_cls, adj_speed
    if home_cls == "向かい風" and back_cls == "追い風":
        return "H向B追", speed_cls, adj_speed
    if home_cls == "横風" and back_cls == "横風":
        cross = get_wind_cross_direction_jp(wind_from_deg, home_running)
        if cross:
            return cross, speed_cls, adj_speed
        return None, None, None
    return None, None, None


def make_bank_wind_key(bank_attrs, wind_pat, speed_cls):
    """セルキー生成
    無風時:   "{周長}|{カント}|{直線}|無風"
    風あり時: "{周長}|{カント}|{直線}|{風向}|{風速}"
    """
    base = bank_attrs["circ"] + "|" + bank_attrs["cant"] + "|" + bank_attrs["straight"]
    if wind_pat == "無風" or speed_cls == "無風":
        return base + "|無風"
    return base + "|" + wind_pat + "|" + speed_cls


def make_bank_wind_keys_fallback(bank_attrs, wind_pat, speed_cls):
    """セルキーを、細かい方から順に並べて返す。

    強風のセルは元々少ない。辞書を調べたところ、
      無風 20セル / 31137件   弱風 76セル / 39828件
      中風 72セル /  6398件   強風 52セル /  1379件
    強風は全体の1.8%しかなく、52セル中18セルは合計7件未満
    (rsrank1つあたり1件未満)で実質空だった。
    該当が無いと適合率が全員0になり、酒場が使えなくなる。

    そこで風向を保ったまま風速だけ一段ゆるめる。
      強風 -> 中風 -> 弱風 -> 無風
    最後の手段として無風セルを使う。

    返り値: [(キー, 由来の説明), ...]  先頭が本来のキー。
    """
    base = (bank_attrs["circ"] + "|" + bank_attrs["cant"] + "|"
            + bank_attrs["straight"])
    if wind_pat == "無風" or speed_cls == "無風":
        return [(base + "|無風", "")]
    order = ["強風", "中風", "弱風"]
    out = [(base + "|" + wind_pat + "|" + speed_cls, "")]
    if speed_cls in order:
        i = order.index(speed_cls) + 1
        while i < len(order):
            out.append((base + "|" + wind_pat + "|" + order[i],
                        order[i] + "で代用"))
            i = i + 1
    out.append((base + "|無風", "無風で代用"))
    return out


def parse_weather_kind(weather_str):
    """weather文字列から天気種類抽出"""
    if not weather_str:
        return ""
    m = re.search(r'天気\s*[:：]\s*([^\s]+)', weather_str)
    if m:
        return m.group(1)
    return ""


def make_kimari_cell_key(venue, wind_pat, speed_cls):
    """決まり手セルキー生成 (天気なし)
    無風時:   "{会場}|無風"
    風あり時: "{会場}|{風向}|{風速}"
    """
    if wind_pat == "無風" or speed_cls == "無風":
        return venue + "|無風"
    return venue + "|" + wind_pat + "|" + speed_cls


def make_kimari_baseline_key(venue):
    """無風基準キー生成"""
    return venue + "|無風"


# ============================================================
# バンク区分取得
# ============================================================
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


# ============================================================
# パターン1/2 並び生成
# ============================================================
def compute_chunk_rate(chunk, players_dict, line_lead_data):
    if not chunk:
        return 0.0
    max_rate = 0.0
    i = 0
    while i < len(chunk):
        bike_str = chunk[i]
        pdata = players_dict.get(bike_str, {})
        if isinstance(pdata, dict):
            full_info = pdata.get("full_info", "")
            pid = make_player_id(full_info)
            if pid:
                rate_info = line_lead_data.get(pid)
                if rate_info:
                    r = rate_info.get("lead_rate", 0.0)
                    if r > max_rate:
                        max_rate = r
        i = i + 1
    return max_rate


def build_pattern2_order(chunks_orig, players_dict, line_lead_data):
    """v42互換のパターン2並び
    返り値: (new_order, is_exception, top1_rate, top2_rate)
    """
    rate_lines = []
    i = 0
    while i < len(chunks_orig):
        c = chunks_orig[i]
        if len(c) > 1:
            rate_lines.append({
                "chunk": c,
                "rate": compute_chunk_rate(c, players_dict, line_lead_data),
                "best_waku": line_best_waku(c),
                "orig_idx": i,
            })
        i = i + 1
    if len(rate_lines) < 2:
        return list(chunks_orig), False, None, None
    rate_sorted = sorted(rate_lines, key=lambda x: (-x["rate"], x["orig_idx"]))
    top1 = rate_sorted[0]
    top2 = rate_sorted[1]
    if top1["best_waku"] <= top2["best_waku"]:
        new_l1 = top1
        new_l2 = top2
    else:
        new_l1 = top2
        new_l2 = top1
    used = [new_l1["chunk"], new_l2["chunk"]]
    new_order = [new_l1["chunk"], new_l2["chunk"]]
    i = 0
    while i < len(chunks_orig):
        c = chunks_orig[i]
        if c not in used:
            new_order.append(c)
        i = i + 1
    is_exception = False
    if new_order == list(chunks_orig):
        new_order = [new_l2["chunk"], new_l1["chunk"]]
        i = 0
        while i < len(chunks_orig):
            c = chunks_orig[i]
            if c not in used:
                new_order.append(c)
            i = i + 1
        is_exception = True
    return new_order, is_exception, top1["rate"], top2["rate"]


def assign_roles_from_order(non_solo_order, original_chunks):
    """非単騎順序リストと元chunksから新役割マップを生成
    新役割: {ライン人数}L{ライン順位}{ライン内位置}
    """
    role_map = {}
    pos_labels = ["L", "S", "T", "F", "F", "F"]
    line_idx = 0
    i = 0
    while i < len(non_solo_order):
        chunk = non_solo_order[i]
        if len(chunk) > 1:
            line_idx = line_idx + 1
            size = len(chunk)
            j = 0
            while j < len(chunk):
                bs = chunk[j]
                if j < len(pos_labels):
                    pos = pos_labels[j]
                else:
                    pos = "F"
                role = str(size) + "L" + str(line_idx) + pos
                role_map[bs] = role
                j = j + 1
        i = i + 1
    solo_idx = 0
    i = 0
    while i < len(original_chunks):
        chunk = original_chunks[i]
        if len(chunk) == 1:
            solo_idx = solo_idx + 1
            role_map[chunk[0]] = "T" + str(solo_idx)
        i = i + 1
    return role_map


def canonicalize_T(canonical):
    if not canonical:
        return None
    if canonical.startswith("T"):
        try:
            n = int(canonical[1:])
            if n >= 2:
                return "T2plus"
        except Exception:
            pass
    return canonical


# ============================================================
# v9: rawscore順位パターン 適合率計算
# ============================================================
SCENES_V9 = ["周回中", "赤板", "打鐘", "ホーム", "バック"]
SCENE_WEIGHTS = {
    "周回中": 0.10,
    "赤板":   0.15,
    "打鐘":   0.25,
    "ホーム": 0.20,
    "バック": 0.30,
}


def cosine_similarity(dist_a, dist_b):
    """2つの位置分布のコサイン類似度
    
    dist_a, dist_b: {x_key: probability}
    """
    keys = set(list(dist_a.keys()) + list(dist_b.keys()))
    if not keys:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for k in keys:
        a = dist_a.get(k, 0.0)
        b = dist_b.get(k, 0.0)
        dot = dot + a * b
        norm_a = norm_a + a * a
        norm_b = norm_b + b * b
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))


def get_player_scene_x_dist(profile, role):
    """選手プロファイル v2 から役割別の シーン×x位置 出現率分布を取得
    
    返り値: {scene: {x_key: prob}} or None
    """
    if not profile:
        return None
    cr = profile.get("by_canonical_role", {}).get(role)
    if not cr:
        return None
    by_scene = cr.get("by_scene_x_rank_pct", {})
    if not by_scene:
        return None
    result = {}
    for scene in SCENES_V9:
        sd = by_scene.get(scene, {})
        # n値を取り出し、出現率に変換
        total = 0
        for x_key in sd:
            d = sd[x_key]
            total = total + d.get("n", 0)
        x_dist = {}
        if total > 0:
            for x_key in sd:
                d = sd[x_key]
                x_dist[x_key] = d.get("n", 0) / total
        result[scene] = x_dist
    return result


def compute_pattern_match(today_rsrank_dists, pattern_data):
    """今日のレースと1つのパターンとの適合率
    
    today_rsrank_dists: {rsrank: {scene: {x_key: prob}}}
        rsrank=1〜7 の各選手のシーン×x位置分布
    pattern_data: パターン統計の by_rsrank
        {"1": {"scene_x_dist": {scene: {x_key: prob}}, "rank_dist": ...}, ...}
    
    返り値: 適合率 (0〜1)
    """
    sim_sum = 0.0
    weight_sum = 0.0
    
    for rsrank_str in ["1", "2", "3", "4", "5", "6", "7"]:
        rsrank = int(rsrank_str)
        if rsrank not in today_rsrank_dists:
            continue
        today_scene_dists = today_rsrank_dists[rsrank]
        pattern_scene_dists = pattern_data.get(rsrank_str, {}).get("scene_x_dist", {})
        
        for scene in SCENES_V9:
            today_d = today_scene_dists.get(scene, {})
            pat_d = pattern_scene_dists.get(scene, {})
            if not today_d or not pat_d:
                continue
            sim = cosine_similarity(today_d, pat_d)
            weight = SCENE_WEIGHTS.get(scene, 1.0)
            sim_sum = sim_sum + sim * weight
            weight_sum = weight_sum + weight
    
    if weight_sum == 0:
        return 0.0
    return sim_sum / weight_sum




# ============================================================
# 予想 メイン処理 (v9: rawscore順位パターン適合率)
# ============================================================
def predict_for_race(race, venue_home_dir, bank_data):
    """1レースの予想を生成 (v13: 会場×風別 rawscore順位パターン適合率)
    
    返り値: 
      None: ライン不明など
      {"valid": False, "reason": str}: 計算不可
      {"valid": True, ...}: 予想結果
    """
    rps = load_rawscore_pattern_stats()
    if rps is None:
        return {"valid": False, "reason": "rawscore_pattern_stats_v5.json なし"}
    
    line_str = race.get("line", "")
    chunks = parse_line_chunks(line_str)
    if not chunks:
        return None
    
    today_yyyymmdd = race.get("date", "")
    players_dict = race.get("players", {})
    venue = race.get("place", "")
    weather_str = race.get("weather", "")
    
    # 風判定 (-2m補正版、風向+風速)
    wind_pat, speed_cls, adj_speed = get_wind_pattern(venue, weather_str, venue_home_dir)
    if not wind_pat or not speed_cls:
        return {"valid": False, "reason": "風判定不可: " + str(weather_str)}
    
    # バンク区分 (周長×カント×直線)
    bank_attrs = get_bank_attrs(venue, bank_data)
    if bank_attrs is None:
        return {"valid": False, "reason": "バンクデータ不明: " + venue}
    
    bank_wind_key = make_bank_wind_key(bank_attrs, wind_pat, speed_cls)
    
    # 全選手の raw_score 計算
    bikes = []
    for bs in players_dict:
        try:
            bikes.append(int(bs))
        except Exception:
            pass
    bikes.sort()
    if len(bikes) != 7:
        return {"valid": False, "reason": "7車立てでない (" + str(len(bikes)) + "車)"}
    
    rs_map = {}
    rs_failed = []
    for bike in bikes:
        bike_str = str(bike)
        pdata = players_dict.get(bike_str, {})
        rs, is_fb = get_raw_score_with_fallback(pdata, today_yyyymmdd, PATH_DB)
        if rs is None:
            rs_failed.append(bike)
        else:
            rs_map[bike] = rs
    
    if rs_failed:
        return {"valid": False, "reason": "raw_score 計算失敗: " + str(rs_failed)}
    
    # raw_score 順位確定
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
    
    # 役割マップ (パターン1 のみ使用)
    lld = load_line_lead_data()
    order2, is_exception, top1_rate, top2_rate = build_pattern2_order(
        chunks, players_dict, lld) if lld else (list(chunks), False, None, None)
    role_map1 = assign_roles_from_order(list(chunks), chunks)
    role_map2 = assign_roles_from_order(order2, chunks)
    patterns_same = (list(chunks) == order2)
    
    # === 周長×カント×直線×風×風速セル取得 (該当無しチェック) ===
    by_winner = rps.get("by_winner_rsrank", {})
    cell_n_by_wkey = {}
    cell_by_wkey = {}
    any_cell_exists = False
    # 該当セルが無いとき、風速を一段ゆるめて探す。
    #   どの段で見つけたかを cell_src_by_wkey に残す。
    key_cands = make_bank_wind_keys_fallback(bank_attrs, wind_pat, speed_cls)
    cell_src_by_wkey = {}
    for wkey in ["1", "2", "3", "4", "5", "6", "7"]:
        pat_stat = by_winner.get(wkey, {})
        by_bank_wind = pat_stat.get("by_bank_wind", {})
        got = None
        src = ""
        ci = 0
        while ci < len(key_cands):
            kk, note = key_cands[ci]
            ci = ci + 1
            c2 = by_bank_wind.get(kk)
            if c2 is not None and c2.get("n", 0) > 0:
                got = c2
                src = note
                break
        if got is not None:
            cell_by_wkey[wkey] = got
            cell_n_by_wkey[wkey] = got["n"]
            cell_src_by_wkey[wkey] = src
            any_cell_exists = True
        else:
            cell_by_wkey[wkey] = None
            cell_n_by_wkey[wkey] = 0
            cell_src_by_wkey[wkey] = ""
    
    if not any_cell_exists:
        return {
            "valid": False,
            "reason": "該当データ無し (" + bank_wind_key + ")",
            "bank_wind_key": bank_wind_key,
        }
    
    def compute_today_dists(role_map):
        today_rsrank_dists = {}
        per_rsrank_meta = {}
        for rsrank in rsrank_to_bike:
            bike = rsrank_to_bike[rsrank]
            bike_str = str(bike)
            pdata = players_dict.get(bike_str, {})
            full_info = pdata.get("full_info", "")
            pid = make_player_id(full_info)
            role = role_map.get(bike_str, "?")
            canonical_t = canonicalize_T(role)
            
            profile = load_profile(pid) if pid else None
            scene_dists = get_player_scene_x_dist(profile, canonical_t)
            
            per_rsrank_meta[rsrank] = {
                "bike": bike,
                "bike_str": bike_str,
                "name": parse_full_info(full_info)["name"],
                "style": pdata.get("style", "?"),
                "role": role,
                "raw_score": rs_map[bike],
                "has_profile": scene_dists is not None,
            }
            if scene_dists is not None:
                today_rsrank_dists[rsrank] = scene_dists
        return today_rsrank_dists, per_rsrank_meta
    
    def compute_pattern_results(role_map):
        """全7パターンとの適合率を計算 (会場×風セル使用)"""
        today_dists, meta = compute_today_dists(role_map)
        pattern_results = []
        for wkey in ["1", "2", "3", "4", "5", "6", "7"]:
            pat_stat = by_winner.get(wkey, {})
            cell = cell_by_wkey.get(wkey)
            
            if cell is None:
                # 該当セル無し: 適合率0、買い目もなし
                pattern_results.append({
                    "winner_rsrank": int(wkey),
                    "occurrence_rate": pat_stat.get("occurrence_rate", 0),
                    "match_score": 0.0,
                    "cell_n": 0,
                    "pattern_stat": {"by_rsrank": {}, "top_trifectas": []},
                    "has_cell": False,
                })
                continue
            
            by_rsrank = cell.get("by_rsrank", {})
            match = compute_pattern_match(today_dists, by_rsrank)
            pattern_results.append({
                "winner_rsrank": int(wkey),
                "occurrence_rate": pat_stat.get("occurrence_rate", 0),
                "match_score": match,
                "cell_n": cell.get("n", 0),
                "pattern_stat": cell,  # cell が by_rsrank, top_trifectas を持つ
                "has_cell": True,
                # どの風速のセルを使ったか。"" なら本来のセル。
                "cell_src": cell_src_by_wkey.get(wkey, ""),
            })
        # 適合率順 (該当セルあり優先、降順)
        pattern_results.sort(key=lambda r: (-r["match_score"], -r["cell_n"]))
        return pattern_results, meta
    
    pattern_results1, meta1 = compute_pattern_results(role_map1)
    if patterns_same:
        pattern_results2 = pattern_results1
        meta2 = meta1
    else:
        pattern_results2, meta2 = compute_pattern_results(role_map2)
    
    # rr 計算 (A方式: rs1 を 10 とした比率)
    rs1_bike = rsrank_to_bike.get(1)
    rs1_raw = rs_map.get(rs1_bike) if rs1_bike else None
    rr_by_rsrank = {}
    if rs1_raw and rs1_raw > 0:
        for rsrank in rsrank_to_bike:
            bk = rsrank_to_bike[rsrank]
            rs = rs_map.get(bk)
            if rs is not None:
                rr_by_rsrank[rsrank] = round(rs / rs1_raw * 10, 2)
    
    return {
        "valid": True,
        "bank_wind_key": bank_wind_key,
        "venue": venue,
        "wind_pat": wind_pat,
        "speed_cls": speed_cls,
        "weather_kind": parse_weather_kind(weather_str),
        "rsrank_to_bike": rsrank_to_bike,
        "bike_to_rsrank": bike_to_rsrank,
        "rs_map": rs_map,
        "rr_by_rsrank": rr_by_rsrank,
        "patterns_same": patterns_same,
        "pattern1": {
            "order": list(chunks), "role_map": role_map1,
            "pattern_results": pattern_results1, "meta": meta1,
        },
        "pattern2": {
            "order": order2, "role_map": role_map2,
            "pattern_results": pattern_results2, "meta": meta2,
            "is_exception": is_exception,
            "top1_rate": top1_rate, "top2_rate": top2_rate,
        },
    }



# ============================================================
# 表示
# ============================================================
def fmt_pct(v):
    return str(round(v*100, 1)) + "%"


def format_bar(rate, max_rate, width=14):
    """バーグラフ生成 (█文字)"""
    if max_rate <= 0:
        return ""
    n_bars = int(round(rate / max_rate * width))
    if n_bars < 0:
        n_bars = 0
    if n_bars > width:
        n_bars = width
    return "█" * n_bars


def display_kimari_graph(venue, wind_pat, speed_cls):
    """ヘッダー直下に決まり手グラフ表示 (天気なし)
    
    引数:
      venue: 会場
      wind_pat: 風向 (補正後)
      speed_cls: 風速クラス (補正後)
    
    返り値: None (印刷のみ)
    """
    ks = load_kimari_stats()
    if ks is None:
        return
    cells = ks.get("cells", {})
    
    # 今回のセルキー
    cell_key = make_kimari_cell_key(venue, wind_pat, speed_cls)
    cell = cells.get(cell_key)
    
    # 無風基準セルキー
    base_key = make_kimari_baseline_key(venue)
    base_cell = cells.get(base_key)
    
    # 今回のセルが存在しない場合
    if cell is None:
        print("")
        print("  決まり手セル: " + cell_key + " (該当データ無し)")
        return
    
    cell_n = cell.get("n", 0)
    base_n = base_cell.get("n", 0) if base_cell else 0
    
    print("")
    print("  決まり手セル: " + cell_key + " (n=" + str(cell_n) + ")")
    if base_cell and cell_key != base_key:
        print("  無風基準セル: " + base_key + " (n=" + str(base_n) + ")")
    
    # --- 【1着決まり手 比率】 ---
    k1_dist = cell.get("kimari_1st_dist", {})
    if not k1_dist:
        return
    
    k1_base = base_cell.get("kimari_1st_dist", {}) if base_cell else {}
    
    print("")
    print("  【1着決まり手 比率】")
    
    # 最大値計算 (バーグラフ正規化用)
    max_v = 0.0
    for k in ("逃", "捲", "差"):
        v = k1_dist.get(k, 0)
        if v > max_v:
            max_v = v
        v2 = k1_base.get(k, 0)
        if v2 > max_v:
            max_v = v2
    
    for k in ("逃", "捲", "差"):
        v = k1_dist.get(k, 0) * 100
        vb = k1_base.get(k, 0) * 100
        bar = format_bar(v, max_v * 100, 14)
        line = "    " + k + "  " + ("{:5.1f}%".format(v))
        if base_cell and cell_key != base_key:
            line = line + " (基準" + "{:5.1f}".format(vb) + "%)"
        line = line + " " + bar
        print(line)
    
    # --- 【1着決まり手別 連動カテゴリ】 ---
    klink = cell.get("kimari_link_dist", {})
    if not klink:
        return
    
    klink_base = base_cell.get("kimari_link_dist", {}) if base_cell else {}
    
    print("")
    print("  【1着決まり手別 連動カテゴリ】")
    
    # 3着位置データ
    klink3 = cell.get("kimari_link3_dist", {})
    klink3_base = base_cell.get("kimari_link3_dist", {}) if base_cell else {}
    
    for k1 in ("逃", "捲", "差"):
        sub = klink.get(k1, {})
        sub_dist = sub.get("dist", {})
        sub_n = sub.get("n", 0)
        sub_base = klink_base.get(k1, {})
        sub_base_dist = sub_base.get("dist", {})
        sub_base_n = sub_base.get("n", 0)
        
        if not sub_dist:
            continue
        
        print("")
        print("    1着=" + k1 + " (n=" + str(sub_n) + ")")
        
        # サブ内 最大値 (2着位置×決まり手 用)
        max_sv = 0.0
        for lab in sub_dist:
            v = sub_dist[lab]
            if v > max_sv:
                max_sv = v
        for lab in sub_base_dist:
            v = sub_base_dist[lab]
            if v > max_sv:
                max_sv = v
        
        # 2着位置×決まり手 出現率降順
        sorted_labs = sorted(sub_dist.items(), key=lambda kv: -kv[1])
        
        # 3着位置データ取得
        sub_link3 = klink3.get(k1, {})
        sub_link3_base = klink3_base.get(k1, {})
        
        ii = 0
        while ii < len(sorted_labs):
            lab, rate = sorted_labs[ii]
            v = rate * 100
            vb = sub_base_dist.get(lab, 0) * 100
            bar = format_bar(v, max_sv * 100, 12)
            lab_disp = (lab + "        ")[:8]
            line = "      " + lab_disp + " " + ("{:5.1f}%".format(v))
            if base_cell and cell_key != base_key:
                line = line + " (基準" + "{:5.1f}".format(vb) + "%)"
            line = line + " " + bar
            print(line)
            
            # 3着位置分布 (このlab に対応するもの)
            link3_data = sub_link3.get(lab, {})
            link3_dist = link3_data.get("dist", {})
            link3_n = link3_data.get("n", 0)
            link3_base_data = sub_link3_base.get(lab, {})
            link3_base_dist = link3_base_data.get("dist", {})
            
            if link3_dist:
                # 3着位置の最大値
                max_3v = 0.0
                for pos in link3_dist:
                    if link3_dist[pos] > max_3v:
                        max_3v = link3_dist[pos]
                for pos in link3_base_dist:
                    if link3_base_dist[pos] > max_3v:
                        max_3v = link3_base_dist[pos]
                
                # 3着位置 出現率降順
                sorted_3pos = sorted(link3_dist.items(), key=lambda kv: -kv[1])
                jj = 0
                while jj < len(sorted_3pos):
                    pos, prate = sorted_3pos[jj]
                    pv = prate * 100
                    pvb = link3_base_dist.get(pos, 0) * 100
                    pbar = format_bar(pv, max_3v * 100, 10)
                    pos_disp = (pos + "      ")[:6]
                    pline = "          └3着 " + pos_disp + " " + ("{:5.1f}%".format(pv))
                    if base_cell and cell_key != base_key:
                        pline = pline + " (基準" + "{:5.1f}".format(pvb) + "%)"
                    pline = pline + " " + pbar
                    print(pline)
                    jj = jj + 1
            ii = ii + 1


def display_race(race, venue_home_dir, bank_data):
    """レース表示メイン"""
    venue = race.get('place', '')
    race_no = race.get('race_no', '?')
    post = race.get('post_time', '--:--')
    line_str = race.get('line', '')
    weather = race.get('weather', '')
    today_yyyymmdd = race.get('date', '')
    
    if not line_str:
        # ライン情報なし → 何も表示せずスキップ (フィルタ対象)
        return "skip_no_line"
    
    chunks_check = parse_line_chunks(line_str)
    if chunks_check is None:
        return "skip_no_line"
    
    # 個人戦は事前スキップ (何も表示しない)
    is_kojinsen_check = True
    i = 0
    while i < len(chunks_check):
        if len(chunks_check[i]) > 1:
            is_kojinsen_check = False
            break
        i = i + 1
    if is_kojinsen_check:
        return "skip_kojinsen"
    
    # === rs1 適合率の事前チェック (4位未満 = 1〜3位ならスキップ) ===
    pre_result = predict_for_race(race, venue_home_dir, bank_data)
    if pre_result is None:
        return "skip_predict_none"
    if not pre_result.get("valid", False):
        return "skip_predict_invalid"
    
    pre_p1_results = pre_result["pattern1"]["pattern_results"]
    pre_with_cell = [r for r in pre_p1_results if r.get("has_cell", False)]
    # rs1 (=winner_rsrank=1) の適合率順位を確認
    rs1_match_rank = None
    ii = 0
    while ii < len(pre_with_cell):
        if pre_with_cell[ii]["winner_rsrank"] == 1:
            rs1_match_rank = ii + 1
            break
        ii = ii + 1
    
    # rs1 の適合率順位が 4位以下 (= 4位/5位/6位/7位) のみ表示
    # 1位/2位/3位 はスキップ
    if rs1_match_rank is None or rs1_match_rank < 4:
        return "skip_rs1_top3"
    
    # === 通常表示処理 ===
    print("")
    print("=" * 78)
    print("■レース情報")
    print(str(venue) + " R" + str(race_no) + "  発走 " + str(post))
    
    if weather:
        wl = "  天候: " + weather
        if venue_home_dir:
            arrow = get_wind_arrow(venue, weather, venue_home_dir)
            if arrow:
                wind_jp = parse_wind_dir_jp(weather)
                if wind_jp:
                    target = "風向:" + wind_jp
                    wl = wl.replace(target, target + "(" + arrow + ")")
            wjudge = judge_wind_advantage(venue, weather, venue_home_dir)
            if wjudge:
                wl = wl + "【" + wjudge + "】"
        print(wl)
    
    # バンクスペック (簡素表示: 周長+カント)
    if bank_data:
        bd = bank_data.get(venue, {})
        if bd:
            bl = bd.get("circumference", bd.get("bank_length", "?"))
            cant = bd.get("cant", bd.get("cant_degree", "?"))
            print("  バンク: 周長" + str(bl) + "m  カント" + str(cant) + "°")
    
    chunks = chunks_check
    
    players = race.get('players', {})
    if not isinstance(players, dict) or not players:
        print("  [警告] 選手情報なし")
        return None
    
    # raw_score 計算
    bike_data = {}
    rs_list = []
    for bs in players:
        try:
            bike = int(bs)
        except Exception:
            continue
        p = players[bs]
        if not isinstance(p, dict):
            continue
        info = parse_full_info(p.get('full_info', ''))
        rs, is_fb = get_raw_score_with_fallback(p, today_yyyymmdd, PATH_DB)
        bike_data[bike] = {
            "name": info["name"],
            "raw_score": rs,
            "style": p.get('style', ''),
        }
        if rs is not None:
            rs_list.append((bike, rs))
    
    rs_list.sort(key=lambda x: -x[1])
    rs_rank = {}
    i = 0
    while i < len(rs_list):
        b = rs_list[i][0]
        rs_rank[b] = i + 1
        i = i + 1
    
    # ライン + score順位 表示
    line_disp = "-".join("".join(c) for c in chunks)
    rank_chunks = []
    i = 0
    while i < len(chunks):
        c = chunks[i]
        rstr = ""
        j = 0
        while j < len(c):
            bs = c[j]
            try:
                bi = int(bs)
            except Exception:
                bi = None
            rk = rs_rank.get(bi) if bi is not None else None
            if rk is not None:
                rstr = rstr + str(rk)
            else:
                rstr = rstr + "?"
            j = j + 1
        rank_chunks.append(rstr)
        i = i + 1
    rank_disp = "-".join(rank_chunks)
    print("  ライン:      " + line_disp)
    print("  score順位:   " + rank_disp)
    print("=" * 78)
    
    # 既に計算済みの result を使用
    result = pre_result
    if result is None:
        print("")
        print("  [予想エラー: ライン解析失敗]")
        return None
    
    if not result.get("valid", False):
        print("")
        print("  [予想スキップ] " + str(result.get("reason", "?")))
        return None
    
    rsrank_to_bike = result["rsrank_to_bike"]
    
    # 周長×カント×直線×風×風速キー表示
    print("")
    print("■該当判定")
    print("  周長×カント×直線×風×風速: " + result.get("bank_wind_key", "?"))
    
    # 決まり手グラフ表示
    r_venue = result.get("venue", "")
    r_wind = result.get("wind_pat", "")
    r_speed = result.get("speed_cls", "")
    if r_venue and r_wind and r_speed:
        display_kimari_graph(r_venue, r_wind, r_speed)
    
    # パターン1 の結果のみ使用
    p1_results = result["pattern1"]["pattern_results"]
    
    # cell あるもののみで適合率順位を作る
    p1_results_with_cell = [r for r in p1_results if r.get("has_cell", False)]
    
    # rsrank -> 適合率順位 / score / cell_n のマップ
    rsrank_to_match_rank = {}
    rsrank_to_match_score = {}
    rsrank_to_cell_n = {}
    ii = 0
    while ii < len(p1_results_with_cell):
        wr = p1_results_with_cell[ii]["winner_rsrank"]
        rsrank_to_match_rank[wr] = ii + 1
        rsrank_to_match_score[wr] = p1_results_with_cell[ii]["match_score"]
        rsrank_to_cell_n[wr] = p1_results_with_cell[ii]["cell_n"]
        ii = ii + 1
    # cell 無しは適合率順位なし
    for pr in p1_results:
        wr = pr["winner_rsrank"]
        if wr not in rsrank_to_cell_n:
            rsrank_to_cell_n[wr] = 0
    
    # 過去出現率順 (全7パターン表示)
    by_occurrence = sorted(p1_results, 
                          key=lambda pr: -pr["occurrence_rate"])
    display_patterns = by_occurrence  # 全7パターン
    
    # 適合率上位3 (★マーク用、cell ありの中で)
    top3_match_rsranks = set()
    ii = 0
    while ii < min(3, len(p1_results_with_cell)):
        top3_match_rsranks.add(p1_results_with_cell[ii]["winner_rsrank"])
        ii = ii + 1
    
    # === 上位パターン表示 ===
    print("")
    print("  【上位パターン (過去出現率順)】")
    
    # 丸数字 (車番1〜9)
    maru_nums = ["", "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨"]
    
    def get_maru(n):
        try:
            ni = int(n)
            if 1 <= ni <= 9:
                return maru_nums[ni]
        except Exception:
            pass
        return str(n)
    
    # rr マップ取得
    rr_map = result.get("rr_by_rsrank", {})
    
    # 各表示パターンの気配値計算と display_patterns に付与
    ii = 0
    while ii < len(display_patterns):
        pr = display_patterns[ii]
        wr = pr["winner_rsrank"]
        has_cell = pr.get("has_cell", False)
        past_rank = ii + 1  # 過去出現率順位 (1〜7)
        match_rank = rsrank_to_match_rank.get(wr, 99) if has_cell else None
        rr_val = rr_map.get(wr, 0)
        # 気配値 = (7 - 過去順位) + (7 - 適合順位) + rr
        if match_rank is not None and match_rank <= 7:
            keihaichi = (7 - past_rank) + (7 - match_rank) + rr_val
        else:
            keihaichi = (7 - past_rank) + rr_val  # 適合不明
        pr["past_rank"] = past_rank
        pr["rr"] = rr_val
        pr["keihaichi"] = round(keihaichi, 2)
        ii = ii + 1
    
    # === 買い目フォーメーション ===
    
    def make_formations(pattern_stat, rsrank_to_bike_map):
        """三連単 TOP6 → 2着ごと集約フォーメーション (6点上限)"""
        tris = pattern_stat.get("top_trifectas", [])
        if not tris:
            return []
        
        groups = []  # [(1着str, 2着str, [3着str, ...])]
        group_map = {}  # 2着 → index in groups
        total_points = 0
        jj = 0
        while jj < len(tris):
            t = tris[jj]
            if total_points >= 6:
                break
            parts = t["key"].split("-")
            if len(parts) != 3:
                jj = jj + 1
                continue
            try:
                rs1 = int(parts[0])
                rs2 = int(parts[1])
                rs3 = int(parts[2])
            except Exception:
                jj = jj + 1
                continue
            b1 = rsrank_to_bike_map.get(rs1)
            b2 = rsrank_to_bike_map.get(rs2)
            b3 = rsrank_to_bike_map.get(rs3)
            if b1 is None or b2 is None or b3 is None:
                jj = jj + 1
                continue
            
            key2 = str(b2)
            key3 = str(b3)
            if key2 not in group_map:
                group_map[key2] = len(groups)
                groups.append((str(b1), key2, []))
            idx = group_map[key2]
            b1s, b2s, third_list = groups[idx]
            if key3 not in third_list:
                third_list.append(key3)
                total_points = total_points + 1
            jj = jj + 1
        
        forms = []
        for g in groups:
            b1s, b2s, third_list = g
            forms.append(b1s + "-" + b2s + "-" + "".join(third_list))
        return forms
    
    # 並び表示
    order1 = result["pattern1"]["order"]
    line_str1 = "-".join("".join(c) for c in order1)
    
    print("")
    print("■買い目フォーメーション")
    print("")
    print("    ライン:      " + line_str1)
    print("    score順位:   " + rank_disp)
    
    # 気配値降順でソート
    sorted_disp = sorted(display_patterns, 
                        key=lambda pr: -pr.get("keihaichi", 0))
    
    SEPARATOR = "  " + "─" * 50
    
    ii = 0
    while ii < len(sorted_disp):
        pr = sorted_disp[ii]
        wr = pr["winner_rsrank"]
        has_cell = pr.get("has_cell", False)
        past_rank = pr.get("past_rank", 0)
        rr_val = pr.get("rr", 0)
        keihaichi = pr.get("keihaichi", 0)
        winner_bike = rsrank_to_bike[wr]
        bs = str(winner_bike)
        pdata = race.get("players", {}).get(bs, {})
        info = parse_full_info(pdata.get("full_info", ""))
        winner_name = info["name"]
        winner_maru = get_maru(winner_bike)
        occ_str = fmt_pct(pr["occurrence_rate"])
        
        print("")
        print(SEPARATOR)
        # 勝者行: ■勝者 + 気配値
        print("    ■勝者:車番" + winner_maru + " " + winner_name + 
              "   気配値 " + "{:.2f}".format(keihaichi))
        print(SEPARATOR)
        # 過去/適合/rawr の 位以降を揃える: "{:>2}位  " 形式
        # 過去
        print("     過去  : " + "{:>2}".format(past_rank) + "位  rs" + 
              str(wr) + "が1着 " + occ_str)
        # 適合
        if has_cell:
            mr = rsrank_to_match_rank.get(wr, 99)
            ms = rsrank_to_match_score.get(wr, 0)
            cell_n = pr.get("cell_n", 0)
            print("     適合  : " + "{:>2}".format(mr) + "位  " + 
                  fmt_pct(ms) + " (n=" + str(cell_n) + ")")
        else:
            print("     適合  : 該当データ無し")
        # rawr: rs順位 + rr値
        print("     rawr  : " + "{:>2}".format(wr) + "位  " + 
              "{:>5.2f}".format(rr_val))
        print("")
        # 買い目
        print("     買い目")
        if not has_cell:
            print("       (該当データ無し)")
            ii = ii + 1
            continue
        forms = make_formations(pr["pattern_stat"], rsrank_to_bike)
        if not forms:
            print("       (買い目データ不足)")
            ii = ii + 1
            continue
        kk = 0
        while kk < len(forms):
            print("       " + forms[kk])
            kk = kk + 1
        ii = ii + 1
    
    return result




# ============================================================
# メイン
# ============================================================
def main():
    print("=" * 78)
    print("  predict_today_v28.py")
    print("  予測システム v28 (rawscore順位×x位置 7パターン適合率)")
    print("=" * 78)
    print("")
    
    # 辞書ロード
    print("辞書読み込み中...")
    venue_home_dir = load_venue_home_direction()
    bank_data = load_bank_data()
    lld = load_line_lead_data()
    idx = load_index()
    rps = load_rawscore_pattern_stats()
    
    if lld is None:
        print("[エラー] player_line_lead_rate_v1.json が必要")
        return
    if idx is None:
        print("[エラー] player_profile_index_v2.json が必要")
        return
    if rps is None:
        print("[エラー] rawscore_pattern_stats_v5.json が必要")
        print("  → build_rawscore_pattern_stats_v5.py で作成してください")
        return
    
    print("  venue_home_direction : " + str(len(venue_home_dir)) + " 会場")
    print("  bank_data            : " + str(len(bank_data)) + " 会場")
    print("  rawscore_pattern     : " + str(len(rps.get("by_winner_rsrank", {}))) + " パターン")
    ks = load_kimari_stats()
    if ks is not None:
        print("  kimari_stats         : " + str(len(ks.get("cells", {}))) + " セル")
    else:
        print("  kimari_stats         : なし (決まり手グラフ非表示)")
    print("  line_lead_data       : " + str(len(lld)) + " 名")
    print("  player_index v2      : " + str(len(idx.get("players", {}))) + " 名")
    print("")
    
    # 日付入力
    try:
        ds = input("日付 (YYYYMMDD、Enter で今日): ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not ds:
        ds = datetime.now().strftime("%Y%m%d")
        print("  → 今日: " + ds)
    try:
        tdt = datetime.strptime(ds, "%Y%m%d")
    except Exception:
        print("[エラー] 日付形式が不正")
        return
    
    now = datetime.now()
    is_today = (tdt.date() == now.date())
    
    # =============================================
    # キャッシュ確認 → 取得 (v42 完全移植)
    # =============================================
    cached = load_cache(ds)
    all_races = None
    
    if cached is not None:
        # ライン情報なしのレースを検出
        no_line_races = []
        i = 0
        while i < len(cached):
            r = cached[i]
            if not r.get('line', '').strip():
                no_line_races.append(r)
            i = i + 1
        no_line_venues = list(set(r.get('place', '') for r in no_line_races if r.get('place')))
        
        print("")
        print("[キャッシュ利用可] " + str(len(cached)) + " R")
        if no_line_venues:
            print("  ※ ライン情報なし: " + str(len(no_line_races)) + "R (" +
                  ", ".join(no_line_venues) + ")")
        else:
            print("  ライン情報: 全レース取得済")
        
        # ライン情報なし会場の再取得 (デフォルトy)
        do_refetch_missing = False
        if no_line_venues:
            try:
                ans0 = input("  → ライン情報なし会場のみ再取得しますか? (Y/n): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans0 = ""
            if ans0 in ("", "y", "yes"):
                do_refetch_missing = True
        
        # === ライン情報なし会場のみ再取得 ===
        if do_refetch_missing and no_line_venues:
            print("")
            print("  [ライン情報なし会場] 再取得中...")
            t0 = time.time()
            updated_count = 0
            
            ex = ThreadPoolExecutor(max_workers=engine.VENUE_WORKERS)
            futures = {}
            for pc in engine.CODES:
                pn = engine.CODES[pc]
                if pn in no_line_venues:
                    futures[ex.submit(engine.check_venue_open, pc, pn, tdt)] = (pc, pn)
            
            open_targets = []
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                    if res:
                        open_targets.append(res)
                except Exception:
                    pass
            ex.shutdown(wait=True)
            
            new_races_by_venue = {}
            i = 0
            while i < len(open_targets):
                pc, pn, bd, dy = open_targets[i]
                print("    " + pn + " 再取得中...", end="", flush=True)
                try:
                    rs = engine.fetch_venue_races(pc, pn, bd, dy, tdt, ds)
                    if rs:
                        new_races_by_venue[pn] = rs
                        print(" " + str(len(rs)) + "R")
                        updated_count = updated_count + len(rs)
                    else:
                        print(" 0R")
                except Exception as e:
                    print(" エラー: " + str(e)[:50])
                i = i + 1
            
            if new_races_by_venue:
                cached = [r for r in cached if r.get('place', '') not in new_races_by_venue]
                for pn in new_races_by_venue:
                    cached.extend(new_races_by_venue[pn])
                save_cache(ds, cached)
                print("  ({:.1f}秒) {} R 更新、キャッシュ保存".format(
                    time.time() - t0, updated_count))
        
        # ライン情報なしの会場をチェック (通知のみ、キャッシュからは削除しない)
        still_no_line_races = []
        i = 0
        while i < len(cached):
            r = cached[i]
            if not r.get('line', '').strip():
                still_no_line_races.append(r)
            i = i + 1
        still_no_line_venues = list(set(r.get('place', '') for r in still_no_line_races if r.get('place')))
        if still_no_line_venues and not do_refetch_missing:
            print("  ※ ライン情報なし → 以下の会場は表示時にスキップされます:")
            print("     " + ", ".join(still_no_line_venues))
            print("     (キャッシュには残るため、次回起動時に再度確認されます)")
        
        all_races = cached
        print("[キャッシュ利用] " + str(len(all_races)) + " R 読み込み")
    else:
        print("")
        print("[キャッシュなし] 開催会場を取得します")
        t1 = time.time()
        open_venues = []
        
        ex = ThreadPoolExecutor(max_workers=engine.VENUE_WORKERS)
        futures = {}
        for pc in engine.CODES:
            pn = engine.CODES[pc]
            futures[ex.submit(engine.check_venue_open, pc, pn, tdt)] = (pc, pn)
        
        for fut in as_completed(futures):
            try:
                res = fut.result()
                if res:
                    open_venues.append(res)
            except Exception:
                pass
        ex.shutdown(wait=True)
        
        open_venues.sort(key=lambda x: x[0])
        print("→ " + str(len(open_venues)) + " 会場 ({:.1f}秒)".format(time.time() - t1))
        if not open_venues:
            print("")
            print("本日開催なし")
            return
        
        # レース取得
        print("")
        print("レース取得...")
        all_races = []
        i = 0
        while i < len(open_venues):
            pc, pn, bd, dy = open_venues[i]
            print("  " + pn + " ...", end="", flush=True)
            try:
                rs = engine.fetch_venue_races(pc, pn, bd, dy, tdt, ds)
            except Exception as e:
                print(" 取得エラー: " + str(e)[:50])
                i = i + 1
                continue
            if rs:
                print(" " + str(len(rs)) + "R")
                all_races.extend(rs)
            else:
                print(" 0R")
            i = i + 1
        
        if all_races:
            save_cache(ds, all_races)
            print("")
            print("キャッシュ保存: " + cache_path(ds))
        # 取得後再代入
        cached = all_races
    
    if not all_races:
        print("")
        print("対象レースなし")
        return
    
    # =============================================
    # 会場別グルーピング・会場選択 (v42移植)
    # =============================================
    venue_to_races = {}
    i = 0
    while i < len(all_races):
        r = all_races[i]
        v = r.get('place', '不明')
        if v not in venue_to_races:
            venue_to_races[v] = []
        venue_to_races[v].append(r)
        i = i + 1
    
    def get_first_post_time(rl):
        """会場の第1レース発走時刻を返す"""
        if not rl:
            return None, ""
        try:
            sorted_r = sorted(rl, key=lambda r: int(r.get('race_no', 99)))
        except Exception:
            sorted_r = rl
        r1 = sorted_r[0]
        return parse_post_time(r1.get('post_time', ''), tdt), r1.get('post_time', '')
    
    venue_info = []
    for v in venue_to_races:
        rl = venue_to_races[v]
        pt_dt, pt_str = get_first_post_time(rl)
        venue_info.append((v, rl, pt_dt, pt_str))
    venue_info.sort(key=lambda x: (x[2] is None, x[2] if x[2] else now))
    
    print("")
    print("=" * 70)
    print("  本日開催会場 (" + str(len(venue_info)) + " 会場、第1R発走時刻順)")
    print("=" * 70)
    i = 0
    while i < len(venue_info):
        v, rl, pt_dt, pt_str = venue_info[i]
        post_str = pt_str if pt_str else "--:--"
        if is_today:
            alive = 0
            j = 0
            while j < len(rl):
                pt = parse_post_time(rl[j].get('post_time', ''), tdt)
                if pt is None or now <= pt + timedelta(minutes=SKIP_GRACE_MINUTES):
                    alive = alive + 1
                j = j + 1
            print("  {0:2d}. {1:8s}  R1発走 {2}  {3}R (うち未発走 {4}R)".format(
                i + 1, v, post_str, len(rl), alive))
        else:
            print("  {0:2d}. {1:8s}  R1発走 {2}  {3}R".format(
                i + 1, v, post_str, len(rl)))
        i = i + 1
    venue_list = [info[0] for info in venue_info]
    print("")
    
    # 会場番号入力
    try:
        sel = input("会場番号を選択 (1-" + str(len(venue_list)) + "、Enter=全会場): ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    
    selected_venues = None
    races = None
    if sel == "":
        selected_venues = venue_list
        races = []
        for v in venue_list:
            races.extend(venue_to_races[v])
        races.sort(key=lambda r: (r.get('place', ''), int(r.get('race_no', 0))))
        print("")
        print("=" * 70)
        print("  選択: 全会場 (" + str(len(venue_list)) + " 会場、合計 " +
              str(len(races)) + "R)")
        print("=" * 70)
    else:
        try:
            sel_idx = int(sel) - 1
            if sel_idx < 0 or sel_idx >= len(venue_list):
                print("[エラー] 範囲外")
                return
        except ValueError:
            print("[エラー] 数値が必要")
            return
        
        selected_venue = venue_list[sel_idx]
        selected_venues = [selected_venue]
        races = sorted(venue_to_races[selected_venue],
                       key=lambda r: int(r.get('race_no', 0)))
        
        print("")
        print("=" * 70)
        print("  選択: " + selected_venue + " (全 " + str(len(races)) + "R)")
        print("=" * 70)
        
        # =============================================
        # 天候リフレッシュ (v42移植)
        # =============================================
        if is_today:
            print("  [天候リフレッシュ] " + selected_venue + " を再取得中...", end="", flush=True)
            try:
                place_code = None
                for pc in engine.CODES:
                    if engine.CODES[pc] == selected_venue:
                        place_code = pc
                        break
                if place_code:
                    res = engine.check_venue_open(place_code, selected_venue, tdt)
                    if res:
                        pc, pn, bd, dy = res
                        new_races = engine.fetch_venue_races(pc, pn, bd, dy, tdt, ds)
                        if new_races:
                            new_by_rno = {}
                            i = 0
                            while i < len(new_races):
                                new_by_rno[new_races[i].get('race_no')] = new_races[i]
                                i = i + 1
                            updated = 0
                            i = 0
                            while i < len(races):
                                r = races[i]
                                rno = r.get('race_no')
                                if rno in new_by_rno:
                                    new_r = new_by_rno[rno]
                                    new_w = new_r.get('weather', '')
                                    if new_w and new_w != r.get('weather', ''):
                                        r['weather'] = new_w
                                        updated = updated + 1
                                i = i + 1
                            print(" " + str(updated) + "R 更新")
                            # キャッシュも更新
                            i = 0
                            while i < len(cached):
                                r = cached[i]
                                if r.get('place', '') == selected_venue:
                                    rno = r.get('race_no')
                                    if rno in new_by_rno:
                                        new_w = new_by_rno[rno].get('weather', '')
                                        if new_w:
                                            r['weather'] = new_w
                                i = i + 1
                            try:
                                save_cache(ds, cached)
                            except Exception:
                                pass
                        else:
                            print(" 取得失敗")
                    else:
                        print(" 開催情報なし")
                else:
                    print(" 会場コード不明")
            except Exception as e:
                print(" エラー: " + str(e)[:50])
    
    # =============================================
    # レース番号入力 (v42移植)
    # =============================================
    target_races = []
    if len(selected_venues) > 1:
        try:
            sel_r = input("レース番号 (Enter=全R): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if sel_r == "":
            target_races = races
        else:
            print("[警告] 全会場選択時はR指定不可、全Rを表示します")
            target_races = races
    else:
        try:
            sel_r = input("レース番号 (Enter=全R、数字=指定R): ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if sel_r == "":
            target_races = races
        else:
            try:
                r_no = int(sel_r)
                target_races = []
                i = 0
                while i < len(races):
                    if races[i].get('race_no') == r_no:
                        target_races.append(races[i])
                        break
                    i = i + 1
                if not target_races:
                    print("[エラー] R" + str(r_no) + " が見つかりません")
                    return
            except ValueError:
                print("[エラー] 数値が必要")
                return
    
    # =============================================
    # 発走済みフィルタ (v42移植)
    # =============================================
    if is_today and len(target_races) > 1:
        try:
            skip_past = input("発走済みのレースをスキップしますか? (y/Enter=Yes, n=No): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            skip_past = ""
        if skip_past != "n":
            alive = []
            n_past = 0
            i = 0
            while i < len(target_races):
                r = target_races[i]
                pt = parse_post_time(r.get('post_time', ''), tdt)
                if pt is not None and now > pt + timedelta(minutes=SKIP_GRACE_MINUTES):
                    n_past = n_past + 1
                else:
                    alive.append(r)
                i = i + 1
            if n_past > 0:
                print("  → " + str(n_past) + "R 発走済みをスキップ")
            target_races = alive
    
    if not target_races:
        print("")
        print("表示対象レースなし")
        return
    
    # =============================================
    # 各レース表示
    # =============================================
    n_displayed = 0
    skip_counts = {
        "skip_no_line": 0,
        "skip_kojinsen": 0,
        "skip_predict_none": 0,
        "skip_predict_invalid": 0,
        "skip_rs1_top3": 0,
    }
    i = 0
    while i < len(target_races):
        result = display_race(target_races[i], venue_home_dir, bank_data)
        if isinstance(result, str) and result in skip_counts:
            skip_counts[result] = skip_counts[result] + 1
        elif result is None:
            # 表示中に何らかでNoneになった
            pass
        else:
            # dict (predict結果) が返った = 表示成功
            n_displayed = n_displayed + 1
        i = i + 1
    
    print("")
    print("=" * 70)
    print("  完了 (表示 " + str(n_displayed) + "R / 対象 " + str(len(target_races)) + "R)")
    if skip_counts["skip_no_line"] > 0:
        print("  ライン情報なし   : " + str(skip_counts["skip_no_line"]) + "R")
    if skip_counts["skip_kojinsen"] > 0:
        print("  個人戦           : " + str(skip_counts["skip_kojinsen"]) + "R")
    if skip_counts["skip_predict_none"] > 0:
        print("  予想None         : " + str(skip_counts["skip_predict_none"]) + "R")
    if skip_counts["skip_predict_invalid"] > 0:
        print("  予想invalid      : " + str(skip_counts["skip_predict_invalid"]) + "R")
    if skip_counts["skip_rs1_top3"] > 0:
        print("  rs1適合率3位以内 : " + str(skip_counts["skip_rs1_top3"]) + "R")
    print("=" * 70)


if __name__ == "__main__":
    main()

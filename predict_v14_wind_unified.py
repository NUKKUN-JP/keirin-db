"""
predict_v14_wind_unified.py
v14辞書ベース予想エンジン 【統合版: P13_CD1_VML + D3a_VML】

===============================================================
統合システムの仕組み:
  1レースに対して2つのロジックで予想
    A: P13_CD1_VML (堅実) - MAX=3, kind除外C, D1除外, VML除外
    B: D3a_VML (攻め)    - MAX=6, 3着拡張, VML除外
  
  買い目を3つのカテゴリに分類:
    ★★★ 共通 (A ∩ B) = 「堅実/攻め」 両方で出た買い目 (最重要)
    ★★ 堅実のみ (A − B) = P13_CD1_VML だけで出た買い目
    ★ 攻めのみ (B − A) = D3a_VML だけで出た買い目

実績 (OOS 2025/07-12):
  P13_CD1_VML: +274,790円, ROI 213.0%, 月別6/6
  D3a_VML:     +346,690円, ROI 177.7%, 月別5/6
  
推奨運用:
  ★★★ 共通だけ買う = 最も信頼、的中率高
  ★★+★★★ 堅実重視 = P13_CD1_VML 単独運用
  ★+★★★ 攻め重視 = D3a_VML 単独運用
  全部買う = 最大損益狙い (投資多)
===============================================================
"""
import json, os, re, time, threading
import pandas as pd
import requests
from io import StringIO
from collections import defaultdict
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

SAVE_DIR = "/storage/emulated/0/Download"
if not os.path.exists(SAVE_DIR): SAVE_DIR = os.getcwd()

WIND_DICT_PATH = os.path.join(SAVE_DIR, "scenario_dict_wind_3lv_full.json")
STD_DICT_PATH = os.path.join(SAVE_DIR, "scenario_dict.json")
PLAYER_PROFILE_PATH = os.path.join(SAVE_DIR, "player_profile.jsonl")

MAX_BETS = 3            # 堅実 (P13_CD1_VML) のMAX
MAX_BETS_D3A = 6        # 攻め (D3a_VML) のMAX
D3_TOP3_DIFF = 0.05     # D3a 3着拡張: 元3着より top3 が +0.05 以上強い候補を追加
BET_UNIT = 100
MIN_SCENARIO_PCT = 3.0

# === P13フィルタ閾値 (確定値) ===
TOP3_THRESHOLD = 0.40   # アプローチA: 3着候補の top3_rate
TOP2_THRESHOLD = 0.25   # アプローチB-2: 番手2着候補の top2_rate

# === 損失kind除外 (P13_C) ===
# 訓練期間のOOS分析で、ROI低すぎる (role, kind) の組み合わせを除外
# 番手×追込み、3番手×まくり、ライン先頭×まくり・逃切り は採用継続
EXCLUDED_ROLE_KIND = [
    ("ライン先頭", "追込み"),
    ("3番手", "追込み"),
    ("単騎", "追込み"),
    ("単騎", "まくり"),
    ("単騎", "逃切り"),
]

# === 3着rel除外 (D1) ===
# ワーストシナリオ分析で発見した地雷パターン
# (1着role, 3着rel) の組み合わせで除外
# role は前方一致 ("L1" は "L1先頭" などにマッチ)
EXCLUDED_3RD_REL = [
    ("L1先頭", "別ライン番手"),  # 訓練時 損失ワースト (まくり&逃切りで合計 -25,000円超)
    ("L2先頭", "別ライン番手"),  # 訓練時 -2,650円
]

# === VML 会場限定除外 (追込み多発会場で先頭まくり/逃切り除外) ===
# OOS期間で番手×追込み1着率 30%以上の会場
# 訓練×OOS共通会場 (大垣/武雄/佐世保/四日市/松阪) + 防府・函館
# これら会場では「ライン先頭・まくり」「ライン先頭・逃切り」 が
# 番手追込みに差されて外れることが多い
EXCLUDED_VENUES_KIND = {
    "大垣":   [("ライン先頭", "まくり"), ("ライン先頭", "逃切り")],
    "武雄":   [("ライン先頭", "まくり"), ("ライン先頭", "逃切り")],
    "佐世保": [("ライン先頭", "まくり"), ("ライン先頭", "逃切り")],
    "四日市": [("ライン先頭", "まくり"), ("ライン先頭", "逃切り")],
    "防府":   [("ライン先頭", "まくり"), ("ライン先頭", "逃切り")],
    "函館":   [("ライン先頭", "まくり"), ("ライン先頭", "逃切り")],
    "松阪":   [("ライン先頭", "まくり"), ("ライン先頭", "逃切り")],
}

# === 並列度設定 (HTTP 403 が出たら下げる) ===
VENUE_WORKERS = 8
RACE_WORKERS = 4

CODES = {"11":"函館","12":"青森","13":"いわき平","21":"弥彦","22":"前橋","23":"取手","24":"宇都宮","25":"大宮","26":"西武園",
"27":"京王閣","28":"立川","31":"松戸","34":"川崎","35":"平塚","36":"小田原","37":"伊東",
"38":"静岡","42":"名古屋","43":"岐阜","44":"大垣","45":"豊橋","46":"富山","47":"松阪","48":"四日市",
"51":"福井","53":"奈良","54":"向日町","55":"和歌山","56":"岸和田",
"61":"玉野","62":"広島","63":"防府","71":"高松","73":"小松島","74":"高知","75":"松山",
"81":"小倉","83":"久留米","84":"武雄","85":"佐世保","86":"別府","87":"熊本"}

PREFS = ["北海道","青森","岩手","宮城","秋田","山形","福島","茨城","栃木","群馬","埼玉","千葉","東京","神奈川","新潟","富山","石川","福井","山梨","長野","岐阜","静岡","愛知","三重","滋賀","京都","大阪","兵庫","奈良","和歌山","鳥取","島根","岡山","広島","山口","徳島","香川","愛媛","高知","福岡","佐賀","長崎","熊本","大分","宮崎","鹿児島","沖縄"]

# =====================================================================
# 共通HTTPセッション
# =====================================================================
_session_local = threading.local()

def get_session():
    if not hasattr(_session_local, "session"):
        s = requests.Session()
        s.headers.update({'User-Agent': 'Mozilla/5.0'})
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20, pool_maxsize=20, max_retries=0)
        s.mount('https://', adapter)
        s.mount('http://', adapter)
        _session_local.session = s
    return _session_local.session


# =====================================================================
# ライン正規化・役割判定
# =====================================================================

def parse_lines(s):
    if not s: return []
    r = []
    for g in s.split('-'):
        if not g: continue
        m = [int(c) for c in g if c.isdigit()]
        if m: r.append(m)
    return r


def normalize_lines(lines):
    non_solo = [l for l in lines if len(l) >= 2]
    solo = [l for l in lines if len(l) == 1]
    solo.sort(key=lambda l: l[0])
    return non_solo + solo


def get_line_config(lines):
    non_solo = [len(l) for l in lines if len(l) >= 2]
    solo = [len(l) for l in lines if len(l) == 1]
    parts = [str(s) for s in non_solo] + [str(s) for s in solo]
    return "-".join(parts)


def get_bike_by_role(lines, role_label):
    normalized = normalize_lines(lines)
    non_solo_lines = [l for l in normalized if len(l) >= 2]
    solo_lines = [l for l in normalized if len(l) == 1]

    m = re.match(r'T(\d+)', role_label)
    if m:
        idx = int(m.group(1)) - 1
        if idx < len(solo_lines):
            return solo_lines[idx][0]
        return None

    m = re.match(r'L(\d+)(先頭|番手|の3番手|3番手|4番手以降)', role_label)
    if not m: return None
    li = int(m.group(1)) - 1
    pos_label = m.group(2)
    if li >= len(non_solo_lines): return None
    line = non_solo_lines[li]
    pi = {"先頭": 0, "番手": 1, "3番手": 2, "の3番手": 2,
          "4番手以降": 3}.get(pos_label)
    if pi is None or pi >= len(line): return None
    return line[pi]


def get_role_for_bike(lines, bike):
    normalized = normalize_lines(lines)
    non_solo_lines = [l for l in normalized if len(l) >= 2]
    solo_lines = [l for l in normalized if len(l) == 1]

    for li, line in enumerate(non_solo_lines):
        for pi, b in enumerate(line):
            if b == bike:
                if pi == 0: return "L" + str(li + 1) + "先頭"
                if pi == 1: return "L" + str(li + 1) + "番手"
                if pi == 2: return "L" + str(li + 1) + "の3番手"
                return "L" + str(li + 1) + "4番手以降"
    for si, line in enumerate(solo_lines):
        if line[0] == bike:
            return "T" + str(si + 1)
    return None


def is_all_singles(lines):
    if not lines: return False
    return all(len(l) == 1 for l in lines)


def role_simple_from_full(role_full):
    if role_full is None: return None
    if role_full.startswith("T"): return "単騎"
    if "先頭" in role_full: return "ライン先頭"
    if "の3番手" in role_full or "3番手" in role_full: return "3番手"
    if "番手" in role_full: return "番手"
    if "4番手以降" in role_full: return "4番手以降"
    return None


# =====================================================================
# 風カテゴリ抽出
# =====================================================================

DIR_16_TO_8 = {
    "北": "北", "北北東": "北",
    "北東": "北東", "東北東": "北東",
    "東": "東", "東南東": "東",
    "南東": "南東", "南南東": "南東",
    "南": "南", "南南西": "南",
    "南西": "南西", "西南西": "南西",
    "西": "西", "西北西": "西",
    "北西": "北西", "北北西": "北西",
}

INDOOR_VENUES = {"前橋", "小倉"}


def extract_wind_category(weather_str, place=None):
    if place and place in INDOOR_VENUES:
        return "屋内"
    if not weather_str: return "取得失敗"

    sky = None
    wind_speed_raw = "--"
    wind_dir_raw = "--"
    m = re.search(r'天気:(\S+)', weather_str)
    if m: sky = m.group(1)
    m = re.search(r'風速:(\S+)', weather_str)
    if m: wind_speed_raw = m.group(1)
    m = re.search(r'風向:(\S+)', weather_str)
    if m: wind_dir_raw = m.group(1)

    if sky == "取得失敗": return "取得失敗"
    if sky == "不明": return "屋内"
    if wind_speed_raw == "--": return "無風"
    if wind_dir_raw == "--": return "無風"

    m2 = re.match(r'([\d.]+)', wind_speed_raw)
    if not m2: return "無風"
    try: speed = float(m2.group(1))
    except Exception: return "無風"
    if speed < 0.5: return "無風"

    dir8 = DIR_16_TO_8.get(wind_dir_raw)
    if dir8 is None: return "無風"

    if speed < 2.5: speed_str = "弱"
    elif speed < 5.5: speed_str = "中"
    else: speed_str = "強"
    return dir8 + "_" + speed_str


# =====================================================================
# プロファイル
# =====================================================================

def parse_full_info(fi):
    if not fi or fi == '未取得': return None
    parts = fi.split('/')
    if len(parts) < 5: return None
    return {"name": parts[0].strip(), "pref": parts[1].strip(),
            "ki": parts[3].strip()}


def get_player_id(p):
    info = parse_full_info(p.get('full_info', ''))
    if info is None: return None
    return info['name'] + '/' + info['ki'] + '/' + info['pref']


def load_player_profiles():
    profiles = {}
    if not os.path.exists(PLAYER_PROFILE_PATH):
        print("  [警告] player_profile.jsonl が見つかりません")
        return profiles
    with open(PLAYER_PROFILE_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: p = json.loads(line)
            except Exception: continue
            pid = p.get("player_id")
            if pid: profiles[pid] = p
    return profiles


def get_role_bucket(player_profile, role_simple):
    if not player_profile: return None
    return player_profile.get("by_role", {}).get(role_simple, {})


def get_role_rate(player_profile, role_simple, rate_key, min_sample=5):
    bucket = get_role_bucket(player_profile, role_simple)
    if bucket is None: return None
    if bucket.get("sample", 0) < min_sample: return None
    ratios = bucket.get("ratios", {})
    d = ratios.get(rate_key, {})
    return d.get("rate")


def get_senko_nige_rate(player_profile, min_sample=5):
    bucket = get_role_bucket(player_profile, "ライン先頭")
    if bucket is None: return None
    if bucket.get("sample", 0) < min_sample: return None
    nige_origin = bucket.get("ratios", {}).get("nige_1st_by_origin_ratio", {})
    if not nige_origin: return None
    senko = nige_origin.get("先行逃")
    if not senko: return None
    return senko.get("rate")


def get_makuri_rate(player_profile, min_sample=5):
    bucket = get_role_bucket(player_profile, "ライン先頭")
    if bucket is None: return None
    if bucket.get("sample", 0) < min_sample: return None
    f1 = bucket.get("ratios", {}).get("finish_when_1st_ratio", {})
    if not f1: return None
    mk = f1.get("まくり")
    if not mk: return 0.0
    return mk.get("rate")


def get_oikomi_rate(player_profile, min_sample=5):
    bucket = get_role_bucket(player_profile, "ライン先頭")
    if bucket is None: return None
    if bucket.get("sample", 0) < min_sample: return None
    f1 = bucket.get("ratios", {}).get("finish_when_1st_ratio", {})
    if not f1: return None
    o = f1.get("追込み")
    if not o: return 0.0
    return o.get("rate")


# === P13用: top3_rate と top2_rate (番手) を取得 ===

def get_top3_rate(player_profile, role_simple, min_sample=10):
    """指定ロールバケットの top3_rate (3着内率) を取得"""
    bucket = get_role_bucket(player_profile, role_simple)
    if bucket is None: return None
    if bucket.get("sample", 0) < min_sample: return None
    return (bucket.get("ratios", {}).get("top3_rate", {}) or {}).get("rate")


def get_top2_bant_rate(player_profile, min_sample=10):
    """番手バケットの top2_rate (2着内率) を取得"""
    if not player_profile: return None
    b = player_profile.get("by_role", {}).get("番手", {})
    if b.get("sample", 0) < min_sample: return None
    return (b.get("ratios", {}).get("top2_rate", {}) or {}).get("rate")


def build_bike_info(race, player_profiles):
    lines = parse_lines(race.get("line", ""))
    bike_info = {}
    for bn, p in race.get("players", {}).items():
        try: bk = int(bn)
        except Exception: continue
        pid = get_player_id(p)
        pp = player_profiles.get(pid) if pid else None
        role_full = get_role_for_bike(lines, bk)
        role_simple = role_simple_from_full(role_full)
        bike_info[bk] = {
            "bike": bk,
            "role_full": role_full,
            "role_simple": role_simple,
            "senko_nige_rate": get_senko_nige_rate(pp),
            "makuri_rate": get_makuri_rate(pp),
            "oikomi_rate": get_oikomi_rate(pp),
            "rank1_lead": get_role_rate(pp, "ライン先頭", "rank1_rate"),
            "rank1_bant": get_role_rate(pp, "番手", "rank1_rate"),
            "chigire_rate": get_role_rate(pp, role_simple,
                "chigire_rate") if role_simple and role_simple != "ライン先頭" else None,
            # === P13用 ===
            "top3_lead": get_top3_rate(pp, "ライン先頭"),
            "top3_bant": get_top3_rate(pp, "番手"),
            "top3_3rd":  get_top3_rate(pp, "3番手"),
            "top3_solo": get_top3_rate(pp, "単騎"),
            "top2_bant": get_top2_bant_rate(pp),
        }
    return bike_info


def get_top3_for_role(bike, bike_info):
    """車番のロールに応じた top3_rate を返す (P13: アプローチA用)"""
    info = bike_info.get(bike, {})
    role_simple = info.get("role_simple")
    if role_simple == "ライン先頭": return info.get("top3_lead")
    if role_simple == "番手": return info.get("top3_bant")
    if role_simple == "3番手": return info.get("top3_3rd")
    if role_simple == "単騎": return info.get("top3_solo")
    return None


# =====================================================================
# v13互換 発動条件判定
# =====================================================================

def check_activation_v13(role_1st, kind_1st, bike_1st, lines, bike_info,
                          level="both"):
    info = bike_info.get(bike_1st, {})
    if not info: return (False, "プロファイルなし")
    strong_ok = False
    medium_ok = False
    detail = ""

    if role_1st.startswith("L") and "先頭" in role_1st and kind_1st == "逃切り":
        sr = info.get("senko_nige_rate")
        r1 = info.get("rank1_lead")
        if sr is not None and r1 is not None:
            detail = "senko={:.2f} r1={:.2f}".format(sr, r1)
            if sr >= 0.60 and r1 >= 0.30: strong_ok = True
            if sr >= 0.55 and r1 >= 0.25: medium_ok = True

    elif role_1st.startswith("L") and "先頭" in role_1st and kind_1st == "まくり":
        mk = info.get("makuri_rate")
        r1 = info.get("rank1_lead")
        if mk is not None and r1 is not None:
            detail = "makuri={:.2f} r1={:.2f}".format(mk, r1)
            if mk >= 0.35 and r1 >= 0.25: strong_ok = True
            if mk >= 0.30 and r1 >= 0.20: medium_ok = True

    elif role_1st == "L1先頭" and kind_1st == "追込み":
        o = info.get("oikomi_rate")
        r1 = info.get("rank1_lead")
        if o is not None and r1 is not None:
            detail = "oikomi={:.2f} r1={:.2f}".format(o, r1)
            if o >= 0.35 and r1 >= 0.20: strong_ok = True
            if o >= 0.30 and r1 >= 0.15: medium_ok = True

    elif (role_1st.startswith("L") and "番手" in role_1st and
          "の3番手" not in role_1st and kind_1st == "追込み"):
        m = re.match(r'L(\d+)', role_1st)
        if m:
            li_num = int(m.group(1))
            lead_role = "L" + str(li_num) + "先頭"
            lead_bike = get_bike_by_role(lines, lead_role)
            if lead_bike:
                lead_info = bike_info.get(lead_bike, {})
                sr = lead_info.get("senko_nige_rate")
                mk = lead_info.get("makuri_rate")
                active = max(sr or 0, mk or 0)
                r1b = info.get("rank1_bant")
                cr = info.get("chigire_rate")
                if r1b is not None and (cr is None or cr < 0.30):
                    detail = "active={:.2f} r1b={:.2f}".format(active, r1b)
                    if active >= 0.50 and r1b >= 0.30: strong_ok = True
                    if active >= 0.45 and r1b >= 0.25: medium_ok = True

    elif "の3番手" in role_1st or role_1st.endswith("3番手"):
        m = re.match(r'L(\d+)', role_1st)
        if m:
            li_num = int(m.group(1))
            lead_role = "L" + str(li_num) + "先頭"
            lead_bike = get_bike_by_role(lines, lead_role)
            if lead_bike:
                lead_info = bike_info.get(lead_bike, {})
                sr = lead_info.get("senko_nige_rate")
                r1 = lead_info.get("rank1_lead")
                if sr is not None and r1 is not None:
                    detail = "lead_sr={:.2f} lead_r1={:.2f}".format(sr, r1)
                    if sr >= 0.50 and r1 >= 0.25: strong_ok = True
                    if sr >= 0.45 and r1 >= 0.20: medium_ok = True

    elif role_1st.startswith("T"):
        sr = info.get("senko_nige_rate")
        r1 = info.get("rank1_lead")
        if sr is not None or r1 is not None:
            sr_v = sr if sr is not None else 0
            r1_v = r1 if r1 is not None else 0
            detail = "senko={:.2f} r1={:.2f}".format(sr_v, r1_v)
            if sr_v >= 0.45 or r1_v >= 0.25: strong_ok = True
            if sr_v >= 0.35 or r1_v >= 0.15: medium_ok = True

    if level == "strong":
        return (strong_ok, detail + (" [強]" if strong_ok else " [-]"))
    if level == "medium":
        return (medium_ok, detail + (" [中]" if medium_ok else " [-]"))
    lv = "強" if strong_ok else ("中" if medium_ok else "-")
    return (strong_ok or medium_ok, detail + " [" + lv + "]")


# =====================================================================
# 関係性解決
# =====================================================================

def list_candidates_by_rel(rel_label, lines, bike_1st, bike_2nd=None):
    """関係ラベルにマッチする候補車番リストを返す (D3a拡張用)"""
    normalized = normalize_lines(lines)
    non_solo_lines = [l for l in normalized if len(l) >= 2]
    solo_lines = [l for l in normalized if len(l) == 1]
    candidates = []

    bike1_li = -1
    for li, line in enumerate(non_solo_lines):
        if bike_1st in line: bike1_li = li; break
    bike2_li = -1
    if bike_2nd is not None:
        for li, line in enumerate(non_solo_lines):
            if bike_2nd in line: bike2_li = li; break

    if rel_label.startswith("1着同ライン"):
        if bike1_li < 0: return []
        target_line = non_solo_lines[bike1_li]
        pos = rel_label.replace("1着同ライン", "")
        idx_map = {"先頭": 0, "番手": 1, "3番手": 2}
        idx = idx_map.get(pos)
        if idx is not None and idx < len(target_line):
            bk = target_line[idx]
            if bk != bike_1st and bk != bike_2nd:
                candidates.append(bk)
        return candidates

    if rel_label.startswith("2着同ライン"):
        if bike2_li < 0: return []
        target_line = non_solo_lines[bike2_li]
        pos = rel_label.replace("2着同ライン", "")
        idx_map = {"先頭": 0, "番手": 1, "3番手": 2}
        idx = idx_map.get(pos)
        if idx is not None and idx < len(target_line):
            bk = target_line[idx]
            if bk != bike_1st and bk != bike_2nd:
                candidates.append(bk)
        return candidates

    if rel_label.startswith("別ライン"):
        pos = rel_label.replace("別ライン", "")
        pi_target = {"先頭": 0, "番手": 1, "3番手": 2}.get(pos)
        if pi_target is None: return []
        exclude = set([bike1_li])
        if bike2_li >= 0: exclude.add(bike2_li)
        for li, line in enumerate(non_solo_lines):
            if li in exclude: continue
            if pi_target < len(line):
                bk = line[pi_target]
                if bk != bike_1st and bk != bike_2nd:
                    candidates.append(bk)
        return candidates

    m = re.match(r'T(\d+)', rel_label)
    if m:
        idx = int(m.group(1)) - 1
        if idx < len(solo_lines):
            bk = solo_lines[idx][0]
            if bk != bike_1st and bk != bike_2nd:
                candidates.append(bk)
        return candidates

    return []


def get_3rd_expansion_candidates(rel_3rd, lines, bike_1st, bike_2nd):
    """D3a用: 3着候補プールを生成"""
    exclude = {bike_1st, bike_2nd}
    candidates = []
    seen = set()

    def add(bk):
        if bk is not None and bk not in exclude and bk not in seen:
            candidates.append(bk)
            seen.add(bk)

    for bk in list_candidates_by_rel(rel_3rd, lines, bike_1st, bike_2nd):
        add(bk)

    normalized = normalize_lines(lines)
    non_solo_lines = [l for l in normalized if len(l) >= 2]
    solo_lines = [l for l in normalized if len(l) == 1]

    bike1_li = -1
    for li, line in enumerate(non_solo_lines):
        if bike_1st in line: bike1_li = li; break
    bike2_li = -1
    for li, line in enumerate(non_solo_lines):
        if bike_2nd in line: bike2_li = li; break

    for li, line in enumerate(non_solo_lines):
        if li == bike1_li or li == bike2_li: continue
        for pi in [0, 1, 2]:
            if pi < len(line): add(line[pi])

    if bike1_li >= 0 and len(non_solo_lines[bike1_li]) >= 3:
        add(non_solo_lines[bike1_li][2])

    if bike2_li >= 0 and bike2_li != bike1_li:
        line = non_solo_lines[bike2_li]
        for pi in [0, 1, 2]:
            if pi < len(line): add(line[pi])

    for line in solo_lines:
        add(line[0])

    return candidates


def get_top3_rate_for_bike_d3(bike, bike_info):
    """車番の役割別 top3_rate を返す (D3a拡張用)"""
    info = bike_info.get(bike, {})
    rs = info.get("role_simple")
    if rs == "ライン先頭": return info.get("top3_lead")
    if rs == "番手": return info.get("top3_bant")
    if rs == "3番手": return info.get("top3_3rd")
    if rs == "単騎": return info.get("top3_solo")
    return None


def resolve_relation(rel_label, lines, bike_1st, bike_2nd=None):
    normalized = normalize_lines(lines)
    non_solo_lines = [l for l in normalized if len(l) >= 2]
    solo_lines = [l for l in normalized if len(l) == 1]

    m = re.match(r'T(\d+)', rel_label)
    if m:
        idx = int(m.group(1)) - 1
        if idx < len(solo_lines):
            return solo_lines[idx][0]
        return None

    bike1_li = -1
    for li, line in enumerate(non_solo_lines):
        if bike_1st in line:
            bike1_li = li; break

    bike2_li = -1
    if bike_2nd is not None:
        for li, line in enumerate(non_solo_lines):
            if bike_2nd in line:
                bike2_li = li; break

    if rel_label.startswith("1着同ライン"):
        if bike1_li < 0: return None
        target_line = non_solo_lines[bike1_li]
        pos = rel_label.replace("1着同ライン", "")
        if pos == "先頭" and len(target_line) >= 1:
            if target_line[0] != bike_1st: return target_line[0]
            return None
        if pos == "番手" and len(target_line) >= 2:
            if target_line[1] != bike_1st: return target_line[1]
            return None
        if pos == "3番手" and len(target_line) >= 3:
            if target_line[2] != bike_1st: return target_line[2]
            return None
        if pos == "4番手以降" and len(target_line) >= 4:
            if target_line[3] != bike_1st: return target_line[3]
            return None
        return None

    if rel_label.startswith("2着同ライン"):
        if bike2_li < 0: return None
        target_line = non_solo_lines[bike2_li]
        pos = rel_label.replace("2着同ライン", "")
        if pos == "先頭" and len(target_line) >= 1:
            if target_line[0] != bike_1st and target_line[0] != bike_2nd:
                return target_line[0]
            return None
        if pos == "番手" and len(target_line) >= 2:
            if target_line[1] != bike_1st and target_line[1] != bike_2nd:
                return target_line[1]
            return None
        if pos == "3番手" and len(target_line) >= 3:
            if target_line[2] != bike_1st and target_line[2] != bike_2nd:
                return target_line[2]
            return None
        return None

    if rel_label.startswith("別ライン"):
        pos = rel_label.replace("別ライン", "")
        pi_target = {"先頭": 0, "番手": 1, "3番手": 2, "4番手以降": 3}.get(pos)
        if pi_target is None: return None
        exclude = set([bike1_li])
        if bike2_li >= 0: exclude.add(bike2_li)
        for li, line in enumerate(non_solo_lines):
            if li in exclude: continue
            if pi_target < len(line):
                bk = line[pi_target]
                if bk != bike_1st and bk != bike_2nd:
                    return bk
        return None

    return None


# =====================================================================
# 予想 (★P13フィルタ追加)
# =====================================================================

def is_excluded_role_kind(role_simple, kind):
    """P13_C: (role, kind) が除外対象か判定"""
    for ex_role, ex_kind in EXCLUDED_ROLE_KIND:
        if ex_role == role_simple and ex_kind == kind:
            return True
    return False


def is_excluded_3rd_rel(role_1st_raw, rel_3rd):
    """D1: (1着role, 3着rel) 除外ルール判定
    
    role_1st_raw: シナリオの role_1st (例: "L1先頭", "L2先頭")
    role_pat は前方一致 ("L1" は "L1先頭" にマッチ)
    """
    for role_pat, rel_pat in EXCLUDED_3RD_REL:
        if role_1st_raw.startswith(role_pat) and rel_3rd == rel_pat:
            return True
    return False


def is_excluded_venue_kind(place, role_simple, kind):
    """VML: (会場, role_simple, kind) 除外判定
    
    指定会場で特定の (role × kind) なら除外
    例: 大垣でライン先頭×まくり → 除外
    """
    if place not in EXCLUDED_VENUES_KIND:
        return False
    for ex_role, ex_kind in EXCLUDED_VENUES_KIND[place]:
        if ex_role == role_simple and ex_kind == kind:
            return True
    return False


def _run_entry(entry, lines, bike_info, place=""):
    bets = []
    scenarios_fired = []
    seen = set()
    # フィルタで除外したシナリオの記録 (デバッグ用)
    filtered_out = []

    for sce in entry["scenarios"]:
        if len(bets) >= MAX_BETS: break
        if sce.get("pct", 0) < MIN_SCENARIO_PCT: continue

        p = sce["pattern"]
        role_1st = p["1st"]["role"]
        kind_1st = p["1st"]["kind"]
        rel_2nd = p["2nd"]["rel"]
        kind_2nd = p["2nd"]["kind"]
        rel_3rd = p["3rd"]["rel"]

        bike_1st = get_bike_by_role(lines, role_1st)
        if bike_1st is None: continue

        fired, detail = check_activation_v13(
            role_1st, kind_1st, bike_1st, lines, bike_info, level="both")
        if not fired: continue

        # ★ P13_C フィルタ: 損失kind除外
        info_1st = bike_info.get(bike_1st, {})
        role_simple_1st = info_1st.get("role_simple", "?")
        if is_excluded_role_kind(role_simple_1st, kind_1st):
            filtered_out.append({
                "rank": sce["rank"],
                "pct": sce["pct"],
                "bet": (bike_1st, None, None),
                "reason": "kind除外: " + role_simple_1st + "×" + kind_1st +
                           " (ROI低 or サンプル少)",
            })
            continue

        # ★ D1 除外: (1着role × 3着rel) ワーストパターン除外
        if is_excluded_3rd_rel(role_1st, rel_3rd):
            filtered_out.append({
                "rank": sce["rank"],
                "pct": sce["pct"],
                "bet": (bike_1st, None, None),
                "reason": "D1除外: " + role_1st + "→3着=" + rel_3rd +
                           " (地雷パターン)",
            })
            continue

        # ★ VML 除外: 追込み多発会場で先頭まくり/逃切り除外
        if is_excluded_venue_kind(place, role_simple_1st, kind_1st):
            filtered_out.append({
                "rank": sce["rank"],
                "pct": sce["pct"],
                "bet": (bike_1st, None, None),
                "reason": "VML除外: " + place + "で" + role_simple_1st +
                           "×" + kind_1st + " (追込み多発会場)",
            })
            continue

        bike_2nd = resolve_relation(rel_2nd, lines, bike_1st)
        if bike_2nd is None: continue
        bike_3rd = resolve_relation(rel_3rd, lines, bike_1st, bike_2nd)
        if bike_3rd is None: continue

        # ★ P13 フィルタ - アプローチB-2: 番手2着候補の top2_rate チェック
        info_2nd = bike_info.get(bike_2nd, {})
        b2_filter_info = ""
        if info_2nd.get("role_simple") == "番手":
            top2 = info_2nd.get("top2_bant")
            if top2 is not None:
                if top2 < TOP2_THRESHOLD:
                    filtered_out.append({
                        "rank": sce["rank"],
                        "pct": sce["pct"],
                        "bet": (bike_1st, bike_2nd, bike_3rd),
                        "reason": "B-2フィルタ: 番手2着 top2={:.2f} < {:.2f}".format(
                            top2, TOP2_THRESHOLD),
                    })
                    continue
                b2_filter_info = "B2[2着top2={:.2f}]".format(top2)

        # ★ P13 フィルタ - アプローチA: 3着候補の top3_rate チェック
        t3 = get_top3_for_role(bike_3rd, bike_info)
        a_filter_info = ""
        if t3 is not None:
            if t3 < TOP3_THRESHOLD:
                filtered_out.append({
                    "rank": sce["rank"],
                    "pct": sce["pct"],
                    "bet": (bike_1st, bike_2nd, bike_3rd),
                    "reason": "Aフィルタ: 3着 top3={:.2f} < {:.2f}".format(
                        t3, TOP3_THRESHOLD),
                })
                continue
            a_filter_info = "A[3着top3={:.2f}]".format(t3)

        bet = (bike_1st, bike_2nd, bike_3rd)
        if bet not in seen:
            seen.add(bet)
            bets.append(bet)
            filter_tags = []
            if a_filter_info: filter_tags.append(a_filter_info)
            if b2_filter_info: filter_tags.append(b2_filter_info)
            scenarios_fired.append({
                "rank": sce["rank"],
                "pct": sce["pct"],
                "pattern": role_1st + "・" + kind_1st + " → " +
                           rel_2nd + "・" + kind_2nd + " → " + rel_3rd,
                "bike_1st": bike_1st,
                "bike_2nd": bike_2nd,
                "bike_3rd": bike_3rd,
                "detail": detail,
                "filter_info": " ".join(filter_tags) if filter_tags else "",
            })
    return bets, scenarios_fired, filtered_out


def _run_entry_d3a(entry, lines, bike_info, place=""):
    """攻め (D3a_VML) のロジック
    - kind除外なし
    - D1除外なし
    - VML会場除外あり
    - 3着拡張あり (top3差>=0.05)
    - MAX=6
    """
    bets = []
    scenarios_fired = []
    seen = set()
    filtered_out = []

    for sce in entry["scenarios"]:
        if len(bets) >= MAX_BETS_D3A: break
        if sce.get("pct", 0) < MIN_SCENARIO_PCT: continue

        p = sce["pattern"]
        role_1st = p["1st"]["role"]
        kind_1st = p["1st"]["kind"]
        rel_2nd = p["2nd"]["rel"]
        kind_2nd = p["2nd"]["kind"]
        rel_3rd = p["3rd"]["rel"]

        bike_1st = get_bike_by_role(lines, role_1st)
        if bike_1st is None: continue

        fired, detail = check_activation_v13(
            role_1st, kind_1st, bike_1st, lines, bike_info, level="both")
        if not fired: continue

        info_1st = bike_info.get(bike_1st, {})
        role_simple_1st = info_1st.get("role_simple", "?")
        
        # ★ VML 除外のみ (kind除外Cなし、D1除外なし)
        if is_excluded_venue_kind(place, role_simple_1st, kind_1st):
            continue

        bike_2nd = resolve_relation(rel_2nd, lines, bike_1st)
        if bike_2nd is None: continue
        bike_3rd_orig = resolve_relation(rel_3rd, lines, bike_1st, bike_2nd)
        if bike_3rd_orig is None: continue

        # ★ B-2 フィルタ (番手2着の top2_bant)
        info_2nd = bike_info.get(bike_2nd, {})
        if info_2nd.get("role_simple") == "番手":
            top2 = info_2nd.get("top2_bant")
            if top2 is not None and top2 < TOP2_THRESHOLD:
                continue

        # ★ A フィルタ (3着 top3)
        t3_orig = get_top3_for_role(bike_3rd_orig, bike_info)
        if t3_orig is not None and t3_orig < TOP3_THRESHOLD:
            continue

        # ★ D3a 拡張: 3着候補プールから top3差 >= D3_TOP3_DIFF の候補を追加
        orig_top3 = get_top3_rate_for_bike_d3(bike_3rd_orig, bike_info)
        bike_3rd_candidates = [bike_3rd_orig]
        if orig_top3 is not None:
            pool = get_3rd_expansion_candidates(rel_3rd, lines, bike_1st, bike_2nd)
            for bk in pool:
                if bk == bike_3rd_orig: continue
                cand_top3 = get_top3_rate_for_bike_d3(bk, bike_info)
                if cand_top3 is None: continue
                if cand_top3 - orig_top3 >= D3_TOP3_DIFF:
                    bike_3rd_candidates.append(bk)

        for bk3 in bike_3rd_candidates:
            if len(bets) >= MAX_BETS_D3A: break
            bet = (bike_1st, bike_2nd, bk3)
            if bet not in seen:
                seen.add(bet)
                bets.append(bet)
                is_d3_ext = (bk3 != bike_3rd_orig)
                scenarios_fired.append({
                    "rank": sce["rank"],
                    "pct": sce["pct"],
                    "pattern": role_1st + "・" + kind_1st + " → " +
                               rel_2nd + "・" + kind_2nd + " → " + rel_3rd,
                    "bike_1st": bike_1st,
                    "bike_2nd": bike_2nd,
                    "bike_3rd": bk3,
                    "detail": detail,
                    "filter_info": "D3拡張" if is_d3_ext else "",
                })
    return bets, scenarios_fired, filtered_out


def predict_v14(race, player_profiles, wind_dict, std_dict):
    line_str = race.get("line", "")
    if not line_str:
        return {"bets": [], "scenarios": [], "filtered_out": [],
                "dict_key": None,
                "source": None, "wind_cat": None, "reason": "ライン情報なし"}
    lines = parse_lines(line_str)
    if not lines:
        return {"bets": [], "scenarios": [], "filtered_out": [],
                "dict_key": None,
                "source": None, "wind_cat": None, "reason": "ライン解析失敗"}
    if is_all_singles(lines):
        return {"bets": [], "scenarios": [], "filtered_out": [],
                "dict_key": None,
                "source": None, "wind_cat": None, "reason": "単騎戦"}

    place = race.get("place", "")
    n_cars = len(race.get("players", {}))
    if n_cars != 7:
        return {"bets": [], "scenarios": [], "filtered_out": [],
                "dict_key": None,
                "source": None, "wind_cat": None,
                "reason": str(n_cars) + "車戦"}

    line_config = get_line_config(lines)
    wind_cat = extract_wind_category(race.get("weather", ""), place=place)
    wind_key = place + "|7|" + line_config + "|" + wind_cat

    bike_info = build_bike_info(race, player_profiles)

    if wind_cat == "取得失敗":
        return {"bets": [], "scenarios": [], "filtered_out": [],
                "dict_key": wind_key, "source": None,
                "wind_cat": wind_cat,
                "reason": "天候取得失敗→スキップ"}

    wind_entry = wind_dict.get(wind_key)
    if wind_entry is not None:
        # 両方のロジックで予想
        bets_a, scenarios_a, filtered_a = _run_entry(wind_entry, lines, bike_info, place=place)
        bets_b, scenarios_b, filtered_b = _run_entry_d3a(wind_entry, lines, bike_info, place=place)
        
        # set 化して分類
        set_a = set(bets_a)
        set_b = set(bets_b)
        
        common = set_a & set_b           # 共通 (堅実/攻め)
        only_a = set_a - set_b           # 堅実のみ
        only_b = set_b - set_a           # 攻めのみ
        
        # 全買い目
        all_bets = list(common) + list(only_a) + list(only_b)
        
        # シナリオ情報マージ (重複排除)
        all_scenarios = []
        seen_keys = set()
        for s in scenarios_a + scenarios_b:
            key = (s["bike_1st"], s["bike_2nd"], s["bike_3rd"])
            if key in seen_keys: continue
            seen_keys.add(key)
            # カテゴリ判定
            if key in common:
                s["category"] = "★★★"  # 共通
            elif key in only_a:
                s["category"] = "★★"   # 堅実
            else:
                s["category"] = "★"    # 攻め
            all_scenarios.append(s)
        
        if all_bets:
            return {
                "bets": all_bets, "scenarios": all_scenarios,
                "filtered_out": filtered_a + filtered_b,
                "dict_key": wind_key, "source": "wind",
                "wind_cat": wind_cat,
                "sample_size": wind_entry.get("sample_size"),
                "reason": "OK",
                # 統合結果
                "common_bets": list(common),
                "only_a_bets": list(only_a),
                "only_b_bets": list(only_b),
            }
        # フィルタで全部弾かれた場合
        if filtered_a or filtered_b:
            return {"bets": [], "scenarios": [], "filtered_out": filtered_a + filtered_b,
                    "dict_key": wind_key, "source": None,
                    "wind_cat": wind_cat,
                    "sample_size": wind_entry.get("sample_size"),
                    "reason": "全フィルタで除外",
                    "common_bets": [], "only_a_bets": [], "only_b_bets": []}
        return {"bets": [], "scenarios": [], "filtered_out": [],
                "dict_key": wind_key, "source": None,
                "wind_cat": wind_cat,
                "sample_size": wind_entry.get("sample_size"),
                "reason": "辞書あり・発動シナリオなし",
                "common_bets": [], "only_a_bets": [], "only_b_bets": []}

    return {"bets": [], "scenarios": [], "filtered_out": [],
            "dict_key": wind_key, "source": None,
            "wind_cat": wind_cat,
            "reason": "風辞書ヒットなし→スキップ"}


# =====================================================================
# 周回ライン情報取得
# =====================================================================

def fetch_chariloto_lines(pc, ds):
    result = {}
    df_ = ds[:4] + "-" + ds[4:6] + "-" + ds[6:8]
    url = "https://www.chariloto.com/keirin/predictions/" + df_ + "/" + pc + "/detail"
    try:
        r = get_session().get(url, timeout=15)
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, 'html.parser')
        for rn in range(1, 13):
            ls = ""
            anc = soup.find('span', id=str(rn) + 'r')
            if anc:
                cur = anc; td = None
                while cur:
                    if cur.name == 'span' and cur.get('id') == str(rn + 1) + 'r': break
                    if cur.name == 'th' and "周回予想" in cur.get_text():
                        td = cur.find_next_sibling('td'); break
                    cur = cur.find_next()
                if td:
                    pts = []; cg = ""
                    for sp in td.find_all('span', class_=re.compile(r'square|p10')):
                        cls = sp.get('class', [])
                        if 'square' in cls:
                            n = sp.get_text(strip=True)
                            if n: cg += n
                        elif 'p10' in cls:
                            if cg: pts.append(cg); cg = ""
                    if cg: pts.append(cg)
                    ls = "-".join(pts) if pts else ""
            result[rn] = ls
    except Exception:
        pass
    return result


# =====================================================================
# ウィンチケット 天候取得
# =====================================================================

WINTICKET_VENUE_ROMA = {
    "函館": "hakodate", "青森": "aomori", "いわき平": "iwakidaira",
    "弥彦": "yahiko", "前橋": "maebashi", "取手": "toride",
    "宇都宮": "utsunomiya", "大宮": "omiya", "西武園": "seibuen",
    "京王閣": "keiokaku", "立川": "tachikawa", "松戸": "matsudo",
    "川崎": "kawasaki", "平塚": "hiratsuka", "小田原": "odawara",
    "伊東": "ito", "静岡": "shizuoka", "名古屋": "nagoya",
    "岐阜": "gifu", "大垣": "ogaki", "豊橋": "toyohashi",
    "富山": "toyama", "松阪": "matsusaka", "四日市": "yokkaichi",
    "福井": "fukui", "奈良": "nara", "向日町": "mukomachi",
    "和歌山": "wakayama", "岸和田": "kishiwada", "玉野": "tamano",
    "広島": "hiroshima", "防府": "hofu", "高松": "takamatsu",
    "小松島": "komatsushima", "高知": "kochi", "松山": "matsuyama",
    "小倉": "kokura", "久留米": "kurume", "武雄": "takeo",
    "佐世保": "sasebo", "別府": "beppu", "熊本": "kumamoto",
}

WINTICKET_WIND_DIR_16 = {
    16: "北", 1: "北北東", 2: "北東", 3: "東北東",
    4: "東", 5: "東南東", 6: "南東", 7: "南南東",
    8: "南", 9: "南南西", 10: "南西", 11: "西南西",
    12: "西", 13: "西北西", 14: "北西", 15: "北北西",
}

WINTICKET_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.7,en;q=0.3',
}

_winticket_holding_cache = {}
_winticket_cache_lock = threading.Lock()


def winticket_find_holding(venue_roma, target_date):
    cache_key = venue_roma + "_" + target_date
    with _winticket_cache_lock:
        if cache_key in _winticket_holding_cache:
            return _winticket_holding_cache[cache_key]

    venue_url = "https://www.winticket.jp/keirin/" + venue_roma + "/"
    result = None
    try:
        r = get_session().get(venue_url, headers=WINTICKET_HEADERS, timeout=15)
        r.encoding = r.apparent_encoding
        if r.status_code == 200:
            html = r.text
            pattern = r'/keirin/' + venue_roma + r'/racecard/(\d{8})(\d+)'
            matches = list(set(re.findall(pattern, html)))
            # 降順ソート (新しい開催から先に試す)
            matches.sort(reverse=True)
            for (d, cid) in matches:
                if d == target_date:
                    result = (d, cid)
                    break
            if result is None:
                try:
                    td = datetime.strptime(target_date, "%Y%m%d")
                    # 最新開催から target_date を含む開催 (0 <= delta < 7) を探す
                    for (d, cid) in matches:
                        start = datetime.strptime(d, "%Y%m%d")
                        delta = (td - start).days
                        if 0 <= delta < 7:
                            result = (d, cid)
                            break
                except Exception:
                    pass
    except Exception:
        pass

    with _winticket_cache_lock:
        _winticket_holding_cache[cache_key] = result
    return result


def winticket_extract_state(html):
    m = re.search(r'window\.__PRELOADED_STATE__\s*=\s*({.+?});\s*\n',
                  html, re.DOTALL)
    if not m:
        m = re.search(r'window\.__PRELOADED_STATE__\s*=\s*({.+?});</script>',
                      html, re.DOTALL)
    if not m: return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def winticket_find_weather_data(state):
    def search(obj):
        if isinstance(obj, dict):
            qk = obj.get("queryKey")
            if isinstance(qk, list) and len(qk) >= 1:
                if qk[0] == "keirin/race/weather":
                    return obj.get("state", {}).get("data")
            for v in obj.values():
                r = search(v)
                if r is not None: return r
        elif isinstance(obj, list):
            for item in obj:
                r = search(item)
                if r is not None: return r
        return None
    return search(state)


def winticket_find_races(state):
    def search(obj):
        if isinstance(obj, dict):
            qk = obj.get("queryKey")
            if isinstance(qk, list) and len(qk) >= 2:
                if qk[0] == "keirin" and qk[1] == "FETCH_KEIRIN_CUP_RACES":
                    return obj.get("state", {}).get("data", {}).get("races", [])
            for v in obj.values():
                r = search(v)
                if r is not None: return r
        elif isinstance(obj, list):
            for item in obj:
                r = search(item)
                if r is not None: return r
        return None
    return search(state)


def winticket_to_db_format(weather_api, race_in_list=None):
    sky = "不明"
    wind_speed_str = "--"
    wind_dir_str = "--"

    if weather_api:
        w_code = weather_api.get("weather")
        if isinstance(w_code, int):
            if 100 <= w_code < 200: sky = "晴"
            elif 200 <= w_code < 300: sky = "曇"
            elif 300 <= w_code < 400: sky = "雨"
            elif 400 <= w_code < 500: sky = "雪"
    if sky == "不明" and race_in_list and race_in_list.get("weather"):
        wstr = race_in_list.get("weather", "")
        if "晴" in wstr: sky = "晴"
        elif "曇" in wstr: sky = "曇"
        elif "雨" in wstr: sky = "雨"
        elif "雪" in wstr: sky = "雪"

    ws_raw = None
    if weather_api:
        try: ws_raw = float(weather_api.get("windSpeed", "0"))
        except Exception: pass
    if ws_raw is None and race_in_list:
        try: ws_raw = float(race_in_list.get("windSpeed", "0"))
        except Exception: pass
    if ws_raw is not None:
        if ws_raw < 0.5: wind_speed_str = "--"
        else: wind_speed_str = str(int(round(ws_raw))) + "m"
    else:
        wind_speed_str = "--"

    # 風向は風速とは独立して取得 (風速--でも風向は取れることがある)
    # winticketは windDirection を 1〜16 で返す (16=北)
    if weather_api:
        wd_code = weather_api.get("windDirection")
        if isinstance(wd_code, int) and 1 <= wd_code <= 16:
            wind_dir_str = WINTICKET_WIND_DIR_16.get(wd_code, "--")

    return "天気:" + sky + " 風速:" + wind_speed_str + " 風向:" + wind_dir_str


def fetch_winticket_weather(place_jp, target_date_str, race_no):
    venue_roma = WINTICKET_VENUE_ROMA.get(place_jp)
    if not venue_roma: return None

    holding = winticket_find_holding(venue_roma, target_date_str)
    if holding is None: return None
    start_date, cup_id = holding
    try:
        td = datetime.strptime(target_date_str, "%Y%m%d")
        sd = datetime.strptime(start_date, "%Y%m%d")
        day_idx = (td - sd).days + 1
    except Exception:
        return None

    url = "https://www.winticket.jp/keirin/" + venue_roma + \
          "/racecard/" + start_date + cup_id + "/" + str(day_idx) + \
          "/" + str(race_no)
    try:
        r = get_session().get(url, headers=WINTICKET_HEADERS, timeout=15)
        r.encoding = r.apparent_encoding
        # 404でも HTMLが返ってきてstateが取れることがあるので、 まず state抽出を試みる
        # 完全な接続失敗 (status 0, 500等) のみ諦める
        if r.status_code >= 500: return None
        state = winticket_extract_state(r.text)
        if state is None: return None
        weather_api = winticket_find_weather_data(state)
        races = winticket_find_races(state)
        target_race = None
        if races:
            for race in races:
                rid = race.get("id", "")
                if (len(rid) >= 8 and rid[-8:] == target_date_str and
                        race.get("number") == race_no):
                    target_race = race
                    break
        if weather_api is None and target_race is None: return None
        return winticket_to_db_format(weather_api, target_race)
    except Exception:
        return None


# =====================================================================
# レース取得
# =====================================================================

def ch(t):
    if pd.isna(t) or t == "": return "なし"
    t = t.replace("[映像]", "").replace("\n", " ").strip()
    mp = re.search(r'^([^\d]+ \w\d)', t)
    md = re.search(r'(\d{1,2}/\d{1,2})', t)
    rs = re.findall(r'(\d)着', t)
    p = mp.group(1) if mp else ""
    d = md.group(1) if md else ""
    r = "・".join(rs) if rs else ""
    return (p + " " + d + " " + r).strip() if r else "なし"


def fetch_race(pc, pn, bd, dy, adt, rn, line_cache):
    ads = adt.strftime("%Y%m%d")
    bds = bd.strftime("%Y%m%d")
    gu = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/result/"
          + pc + bds + "/" + pc + bds + str(dy).zfill(2) + "00/"
          + str(rn).zfill(2) + "/")
    ln = line_cache.get(rn, "")
    pls = {i: {"full_info": "未取得", "h1": "なし", "h2": "なし",
               "h3": "なし", "style": "--"} for i in range(1, 10)}
    try:
        r = get_session().get(gu, timeout=15)
        r.encoding = r.apparent_encoding
        html = r.text
        soup = BeautifulSoup(html, 'html.parser')
        pt = "--:--"
        dt = soup.find('dt', string=re.compile("発走予定"))
        if dt:
            dd = dt.find_next_sibling('dd')
            if dd: pt = dd.get_text(strip=True)

        if pn in INDOOR_VENUES:
            weather = "天気:不明 風速:-- 風向:--"
        else:
            weather = "天気:取得失敗 風速:-- 風向:--"
            try:
                wt = fetch_winticket_weather(pn, ads, int(rn))
                if wt: weather = wt
            except Exception:
                pass

        ts = pd.read_html(StringIO(html))
        if len(ts) < 3: return None
        for _, row in ts[0].iterrows():
            vs = [str(v).strip() for v in row.values]
            try:
                ci = int(vs[4])
                sm = re.search(r'[SAL]\d\s+([逃捲差追両])', " ".join(vs))
                if sm: pls[ci]["style"] = sm.group(1)
            except Exception:
                pass
        for _, row in ts[2].iterrows():
            vs = [str(v).strip() for v in row.values]
            cm = re.search(r'(\d)', vs[1])
            if not cm: continue
            ci = int(cm.group(1))
            ri = " ".join(vs[2].split())
            parts = ri.split('/')
            npr = parts[0].strip()
            fp = "不明"
            fn = npr
            st = npr.replace(" ", "").replace("\u3000", "")
            for pf in sorted(PREFS, key=len, reverse=True):
                if st.endswith(pf):
                    fp = pf
                    tn = npr
                    for c in reversed(list(pf)):
                        tn = re.sub(str(c) + r"\s*$", "", tn).strip()
                    fn = tn
                    break
            age = parts[1].strip() if len(parts) > 1 else "--"
            ki = parts[2].strip() if len(parts) > 2 else "--"
            sc = vs[3]
            pls[ci]["full_info"] = (fn + "/" + fp + "/" + age + "歳/"
                                    + ki + "期/" + sc + "点")
            pls[ci]["h1"] = ch(vs[4])
            pls[ci]["h2"] = ch(vs[5])
            pls[ci]["h3"] = ch(vs[6])
        pl = {str(i): pls[i] for i in range(1, 10)
              if pls[i]["full_info"] != "未取得"}
        return {"race_id": pc + ads + str(rn).zfill(2), "date": ads,
                "race_no": int(rn), "place": pn, "post_time": pt,
                "line": ln, "weather": weather, "players": pl}
    except Exception:
        return None


def check_venue_open(pc, pn, tdt):
    for d in range(1, 8):
        bd = tdt - timedelta(days=d - 1)
        bds = bd.strftime("%Y%m%d")
        tu = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/result/"
              + pc + bds + "/" + pc + bds + str(d).zfill(2) + "00/01/")
        for retry in range(2):
            try:
                r = get_session().get(tu, timeout=15)
                if (r.status_code == 200 and '<table' in r.text
                        and "レース結果がありません" not in r.text):
                    return (pc, pn, bd, d)
                break
            except Exception:
                if retry == 0: time.sleep(1)
    return None


def fetch_venue_races(pc, pn, bd, dy, tdt, ds):
    line_cache = fetch_chariloto_lines(pc, ds)
    races = []
    if pn not in INDOOR_VENUES:
        venue_roma = WINTICKET_VENUE_ROMA.get(pn)
        if venue_roma:
            winticket_find_holding(venue_roma, tdt.strftime("%Y%m%d"))

    with ThreadPoolExecutor(max_workers=RACE_WORKERS) as ex:
        futures = {ex.submit(fetch_race, pc, pn, bd, dy, tdt, rn, line_cache): rn
                   for rn in range(1, 13)}
        for fut in as_completed(futures):
            rc = fut.result()
            if rc: races.append(rc)
    races.sort(key=lambda x: x["race_no"])
    return races


# =====================================================================
# 出力
# =====================================================================

def format_bet(bet):
    return str(bet[0]) + "-" + str(bet[1]) + "-" + str(bet[2])


def print_race_prediction(race, result):
    bets = result["bets"]
    scenarios = result["scenarios"]
    src = result.get("source")
    source_label = "★風辞書 [統合]" if src == "wind" else "?"
    wind_cat = result.get("wind_cat", "-")
    print("┌─ " + race["place"] + " " + str(race["race_no"]) + "R  " +
          race.get("post_time", "") + "  [ライン: " + race.get("line", "") + "]")
    print("│ 天候: " + race.get("weather", "不明") +
          "  -> 風カテゴリ: " + wind_cat)
    print("│ 予想源: " + source_label +
          "  辞書キー: " + (result["dict_key"] or "-") +
          "  (サンプル: " + str(result.get("sample_size", "-")) + "R)")
    print("│")
    mark_map = {}
    for i, b in enumerate(bets):
        mark_map[b[0]] = mark_map.get(b[0], "") + "★1着(S" + str(i + 1) + ") "
        mark_map[b[1]] = mark_map.get(b[1], "") + "☆2着(S" + str(i + 1) + ") "
        mark_map[b[2]] = mark_map.get(b[2], "") + "△3着(S" + str(i + 1) + ") "
    for bn in sorted(race.get("players", {}).keys(), key=int):
        pl = race["players"][bn]
        fi = pl.get("full_info", "")
        mark = mark_map.get(int(bn), "")
        print("│  " + bn + "番: " + fi + ("  " + mark if mark else ""))
    print("│")
    print("│ ◆ 発動シナリオ (" + str(len(scenarios)) + "件):")
    for i, sce in enumerate(scenarios, 1):
        cat = sce.get("category", "")
        cat_disp = cat + " " if cat else ""
        bet = (sce["bike_1st"], sce["bike_2nd"], sce["bike_3rd"])
        print("│   [S" + str(i) + "] " + cat_disp + "Rank" + str(sce["rank"]) +
              " (" + str(sce["pct"]) + "%) -> " + format_bet(bet))
        print("│        " + sce["pattern"])
        print("│        発動: " + sce["detail"])
        if sce.get("filter_info"):
            print("│        ✓P13: " + sce["filter_info"])
    
    # 統合サマリ (このレース)
    common = result.get("common_bets", [])
    only_a = result.get("only_a_bets", [])
    only_b = result.get("only_b_bets", [])
    print("│")
    print("│ ◆ 統合分類:")
    if common:
        print("│   ★★★ 共通(堅実/攻め): " +
              " / ".join(format_bet(b) for b in common))
    if only_a:
        print("│   ★★ 堅実のみ:        " +
              " / ".join(format_bet(b) for b in only_a))
    if only_b:
        print("│   ★ 攻めのみ:         " +
              " / ".join(format_bet(b) for b in only_b))
    # フィルタで弾かれたシナリオ表示 (検証用)
    filtered = result.get("filtered_out", [])
    if filtered:
        print("│")
        print("│ ◇ P13フィルタで除外されたシナリオ (" +
              str(len(filtered)) + "件):")
        for i, fo in enumerate(filtered, 1):
            print("│   [F" + str(i) + "] Rank" + str(fo["rank"]) +
                  " (" + str(fo["pct"]) + "%) -> " + format_bet(fo["bet"]))
            print("│        " + fo["reason"])
    print("│")
    n_common = len(result.get("common_bets", []))
    n_only_a = len(result.get("only_a_bets", []))
    n_only_b = len(result.get("only_b_bets", []))
    n_total = n_common + n_only_a + n_only_b
    print("│ >>> 3連単 計" + str(n_total) + "点  " +
          "(★★★" + str(n_common) +
          " / ★★" + str(n_only_a) +
          " / ★" + str(n_only_b) + ")")
    print("│     全買い目: " + " / ".join(format_bet(b) for b in bets) + " <<<")
    print("└" + "─" * 60 + "\n")


# =====================================================================
# メイン
# =====================================================================

def main():
    t_start = time.time()
    print("\n" + "=" * 60)
    print("  v14 辞書ベース予想エンジン 【統合: P13_CD1_VML + D3a_VML】")
    print()
    print("  ★★★ 共通 (堅実/攻め) = 両システムが選んだ買い目 (最重要)")
    print("  ★★ 堅実のみ          = P13_CD1_VML だけ選んだ買い目")
    print("  ★ 攻めのみ           = D3a_VML だけ選んだ買い目")
    print()
    print("  3連単 1点" + str(BET_UNIT) + "円")
    print("  堅実 (P13_CD1_VML): MAX " + str(MAX_BETS) + "点")
    print("    A + B-2 + kind除外C + D1除外 + VML会場除外")
    print("  攻め (D3a_VML):     MAX " + str(MAX_BETS_D3A) + "点")
    print("    A + B-2 + 3着拡張 (top3差>=" + str(D3_TOP3_DIFF) + ") + VML会場除外")
    print()
    print("  P13フィルタ: top3>=" + str(TOP3_THRESHOLD) +
          " / top2(番手)>=" + str(TOP2_THRESHOLD))
    print("  kind除外C (堅実のみ適用):")
    for ex_role, ex_kind in EXCLUDED_ROLE_KIND:
        print("    - " + ex_role + " × " + ex_kind)
    print("  3着rel除外D1 (堅実のみ適用):")
    for ex_role, ex_rel in EXCLUDED_3RD_REL:
        print("    - " + ex_role + " → 3着=" + ex_rel)
    print("  VML会場除外 (両方適用、" + str(len(EXCLUDED_VENUES_KIND)) + "会場):")
    print("    対象: " + ", ".join(EXCLUDED_VENUES_KIND.keys()))
    print("    内容: ライン先頭×まくり / ライン先頭×逃切り を除外")
    print()
    print("  辞書: scenario_dict_wind_3lv_full.json (24/01-25/12 全期間)")
    print("  実績(OOS):")
    print("    P13_CD1_VML: ROI 213.0%, 損益+274,790円, 月別6/6 ★完全")
    print("    D3a_VML:     ROI 177.7%, 損益+346,690円, 月別5/6 ★損益最大")
    print("=" * 60)

    print("\n  風辞書読み込み中...")
    if not os.path.exists(WIND_DICT_PATH):
        print("[警告] " + WIND_DICT_PATH + " が見つかりません")
        wind_dict = {}
    else:
        with open(WIND_DICT_PATH, 'r', encoding='utf-8') as f:
            wind_dict = json.load(f)
    print("  風辞書キー数: " + str(len(wind_dict)))

    print("  標準辞書読み込み中...")
    if not os.path.exists(STD_DICT_PATH):
        print("[エラー] scenario_dict.json が見つかりません")
        return
    with open(STD_DICT_PATH, 'r', encoding='utf-8') as f:
        std_dict = json.load(f)
    print("  標準辞書キー数: " + str(len(std_dict)))

    print("  プロファイル読み込み中...")
    player_profiles = load_player_profiles()
    print("  選手プロファイル数: " + str(len(player_profiles)))

    ds = input("\n日付(YYYYMMDD、空Enterで今日): ").strip()
    if not ds:
        # 空Enter → 今日の日付
        ds = datetime.now().strftime("%Y%m%d")
        print("  → 今日の日付を使用: " + ds)
    try:
        tdt = datetime.strptime(ds, "%Y%m%d")
    except Exception:
        print("[エラー] 日付形式が不正"); return

    print("\n" + ds[:4] + "/" + ds[4:6] + "/" + ds[6:] +
          " のレース取得中...\n")

    t1 = time.time()
    print("  [1/2] 全会場の開催チェック (並列" + str(VENUE_WORKERS) + ")...")
    open_venues = []
    with ThreadPoolExecutor(max_workers=VENUE_WORKERS) as ex:
        futures = {ex.submit(check_venue_open, pc, pn, tdt): (pc, pn)
                   for pc, pn in CODES.items()}
        for fut in as_completed(futures):
            pc, pn = futures[fut]
            res = fut.result()
            if res:
                open_venues.append(res)
                print("    開催: " + pn + " (" + str(res[3]) + "日目)")
    open_venues.sort(key=lambda x: x[0])
    print("  → " + str(len(open_venues)) + "会場開催  (" +
          "{:.1f}".format(time.time() - t1) + "秒)")

    if not open_venues:
        print("\n開催なし"); return

    t2 = time.time()
    print("\n  [2/2] レース取得 (会場内" + str(RACE_WORKERS) + "並列)...")
    found = []
    for pc, pn, bd, dy in open_venues:
        print("    " + pn + " ...", end="", flush=True)
        races = fetch_venue_races(pc, pn, bd, dy, tdt, ds)
        found.extend(races)
        print(" " + str(len(races)) + "R")
    print("  → 合計 " + str(len(found)) + "R 取得  (" +
          "{:.1f}".format(time.time() - t2) + "秒)")

    results = []
    for rc in found:
        res = predict_v14(rc, player_profiles, wind_dict, std_dict)
        results.append({"race": rc, "result": res})

    bets_races = [x for x in results if x["result"]["bets"]]
    skip_races = [x for x in results if not x["result"]["bets"]]
    bets_races.sort(key=lambda x: x["race"].get("post_time", "99:99")
                    if x["race"].get("post_time") != "--:--" else "99:99")

    print("\n" + "=" * 60)
    if not bets_races:
        print("  本日の買い目: なし")
    else:
        total_bets = sum(len(x["result"]["bets"]) for x in bets_races)
        print("  本日の買い目: " + str(len(bets_races)) + "R / " +
              str(len(found)) + "R中  (" + str(total_bets) + "点 = " +
              str(total_bets * BET_UNIT) + "円)")
    print("=" * 60 + "\n")

    for x in bets_races:
        print_race_prediction(x["race"], x["result"])

    if bets_races:
        print("─" * 60)
        print("  買い目サマリ [統合: P13_CD1_VML + D3a_VML]")
        print("─" * 60)
        for x in bets_races:
            rc = x["race"]; res = x["result"]
            wc = res.get("wind_cat", "-")
            
            common = res.get("common_bets", [])
            only_a = res.get("only_a_bets", [])
            only_b = res.get("only_b_bets", [])
            
            print()
            print("  " + rc["place"].ljust(6) + " " +
                  str(rc["race_no"]).rjust(2) + "R " +
                  rc.get("post_time", "").rjust(6) +
                  "  [" + wc + "]")
            
            if common:
                bet_str = " / ".join(format_bet(b) for b in common)
                print("    ★★★ 堅実/攻め: " + bet_str)
            if only_a:
                bet_str = " / ".join(format_bet(b) for b in only_a)
                print("    ★★ 堅実: " + bet_str)
            if only_b:
                bet_str = " / ".join(format_bet(b) for b in only_b)
                print("    ★ 攻め: " + bet_str)
        
        # サマリ
        total_common = sum(len(x["result"].get("common_bets", []))
                              for x in bets_races)
        total_only_a = sum(len(x["result"].get("only_a_bets", []))
                              for x in bets_races)
        total_only_b = sum(len(x["result"].get("only_b_bets", []))
                              for x in bets_races)
        total_bets = total_common + total_only_a + total_only_b
        
        print()
        print("─" * 60)
        print("  運用サマリ:")
        print("    ★★★ 堅実/攻め: " + str(total_common) + "点 (最重要)")
        print("    ★★ 堅実のみ:   " + str(total_only_a) + "点")
        print("    ★ 攻めのみ:    " + str(total_only_b) + "点")
        print("    合計:           " + str(total_bets) + "点  投資 " +
              str(total_bets * BET_UNIT) + "円")
        print()
        print("    [ 堅実P13_CD1_VML ] " + str(total_common + total_only_a) +
              "点 (★★★+★★)  投資 " +
              str((total_common + total_only_a) * BET_UNIT) + "円")
        print("    [ 攻めD3a_VML    ] " + str(total_common + total_only_b) +
              "点 (★★★+★)   投資 " +
              str((total_common + total_only_b) * BET_UNIT) + "円")
        print("    [ 共通のみ        ] " + str(total_common) +
              "点 (★★★)     投資 " +
              str(total_common * BET_UNIT) + "円")
        print("─" * 60)

    reason_cnt = defaultdict(int)
    for x in skip_races: reason_cnt[x["result"]["reason"]] += 1
    if reason_cnt:
        print("\n  スキップ理由:")
        for r, c in sorted(reason_cnt.items(), key=lambda x: -x[1]):
            print("    " + r + ": " + str(c) + "R")

    print("\n  総実行時間: " + "{:.1f}".format(time.time() - t_start) + "秒")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
predict_cathedral.py  (大聖堂タブ 新予測エンジン・統合版)

build_end_to_end_predictor_v4.py と predict_shukai_v2.py を1ファイルに統合。
import時の副作用(辞書ロード/sys.exit/print)を完全排除し、
init_cathedral() で明示ロードする方式に変更。app本体に安全に組み込める。

採用ロジック: 新ロジック v1 (b) th10_rawscr
  [1] 周回中並び予測 (form_lookup_v1 + front_rate_table_v1)
  [2] 各outcome(>=5%) -> 周回中タグ (lap_pattern_dict_v3 最頻 + 戦術ラベル)
  [3] 最終並び辞書 final_order_dict_v2 (条件なし) -> バック並び分布
  [4] 買い目辞書 bet_dict_v3 (4階層バックオフ 閾値10)
  [5] 役割タグ -> 車番 逆変換, 確率重み合算
  [6] raw_score補正 (3車raw_score合計を重みに乗算)
  [7] 全候補生成 -> モードフィルタ -> 上位N点

3モード:
  maria  : おまかせ。全候補の上位N点。
  priest : 軸+対抗。固定(axis=1着,rival=2着) / 連対(どちらかが1-2着)。
           軸のみ/対抗のみも許容。
  bishop : 1-2車番指定。その車番を含む3連単の上位N点。

公開関数:
  init_cathedral(data_dir=None) -> dict  (辞書ロード, app起動時1回)
  predict_cathedral(line_str, players_info, venue, weather_str,
                    mode, top_n, axis, rival, rival_mode, include_bikes) -> dict

players_info の形:
  { 車番(int): {"s": int, "full_info": str, "raw_score": float} }
"""
import json
import os
import re
from collections import Counter, defaultdict

# ============================================================
# モジュール状態 (init_cathedral でロード)
# ============================================================
_STATE = {
    "loaded": False,
    "lap_dict": None,
    "final_dict": None,
    "bet_dict_v3": None,
    "venue_home_dir": None,
    "kimari": None,
    "lookup_table": None,
    "form_config": None,
    "front_table": None,
}

THRESHOLD = 10

# 探索ディレクトリ候補 (Pydroid3実機 + フォールバック)
_DEFAULT_DIRS = [
    "/storage/emulated/0/Download/takusen/data/dicts",
    "/storage/emulated/0/Download/takusen/data",
    "/storage/emulated/0/Download/takusen/data/static",
    "/storage/emulated/0/Download/takusen/static",
    "/storage/emulated/0/Download/takusen/code",
    "/storage/emulated/0/Download/takusen",
    "/storage/emulated/0/Download",
    os.getcwd(),
]

_REQUIRED = [
    "form_lookup_v1.json",
    "front_rate_table_v1.json",
    "lap_pattern_dict_v3.json",
    "final_order_dict_v2.json",
    "bet_dict_v3.json",
    "kimari_player_role_FINAL.jsonl",
    "venue_home_direction.json",
]


def _find_file(name, data_dir=None):
    """name を探す。同名 .gz もフォールバック候補にする。
    見つかったパス(.json or .json.gz)を返す。"""
    dirs = []
    if data_dir:
        dirs.append(data_dir)
        dirs.append(os.path.join(data_dir, "static"))
    dirs.extend(_DEFAULT_DIRS)
    cand_names = [name, name + ".gz"]
    for d in dirs:
        for cn in cand_names:
            p = os.path.join(d, cn)
            if os.path.exists(p):
                return p
    return None


def _open_text(path):
    """通常 or .gz のテキストファイルを開く (utf-8)。"""
    if path.endswith(".gz"):
        import gzip
        return gzip.open(path, "rt", encoding="utf-8")
    return open(path, encoding="utf-8")


def _load_json(path):
    f = _open_text(path)
    try:
        return json.load(f)
    finally:
        f.close()


def init_cathedral(data_dir=None):
    """辞書をロードして _STATE に格納。
    返り値: {"ok": bool, "loaded": [...], "missing": [...], "kimari_n": int}
    例外は投げない。失敗時は _STATE["loaded"]=False のまま。"""
    paths = {}
    missing = []
    for nm in _REQUIRED:
        p = _find_file(nm, data_dir)
        if p:
            paths[nm] = p
        else:
            missing.append(nm)
    if missing:
        _STATE["loaded"] = False
        return {"ok": False, "loaded": list(paths.keys()),
                "missing": missing, "kimari_n": 0}

    try:
        lookup_data = _load_json(paths["form_lookup_v1.json"])
        form_config = {}
        for st, vs in lookup_data["meta"]["vars"].items():
            form_config[st] = tuple(vs)
        lookup_table = lookup_data["table"]

        front_table = _load_json(paths["front_rate_table_v1.json"])["table"]

        lap_dict = _load_json(paths["lap_pattern_dict_v3.json"])
        final_dict = _load_json(paths["final_order_dict_v2.json"])
        bet_dict_v3 = _load_json(paths["bet_dict_v3.json"])

        venue_home_dir = _load_json(paths["venue_home_direction.json"])
        if "伊東温泉" in venue_home_dir and "伊東" not in venue_home_dir:
            venue_home_dir["伊東"] = venue_home_dir["伊東温泉"]

        kimari = {}
        kf = _open_text(paths["kimari_player_role_FINAL.jsonl"])
        try:
            for line in kf:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("k"):
                    kimari[r["k"]] = r
        finally:
            kf.close()
    except Exception as e:
        _STATE["loaded"] = False
        return {"ok": False, "loaded": [], "missing": [],
                "kimari_n": 0, "error": str(e)}

    _STATE["lap_dict"] = lap_dict
    _STATE["final_dict"] = final_dict
    _STATE["bet_dict_v3"] = bet_dict_v3
    _STATE["venue_home_dir"] = venue_home_dir
    _STATE["kimari"] = kimari
    _STATE["lookup_table"] = lookup_table
    _STATE["form_config"] = form_config
    _STATE["front_table"] = front_table
    _STATE["loaded"] = True
    return {"ok": True, "loaded": list(paths.keys()),
            "missing": [], "kimari_n": len(kimari)}


def is_ready():
    return _STATE["loaded"]


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
DOME_VENUES = set(["前橋", "小倉", "千葉"])


def _parse_wind_speed(weather_str):
    if not weather_str:
        return None
    m = re.search(r'風速\s*[:：]\s*(\d+(?:\.\d+)?)\s*m', weather_str)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def _parse_wind_dir_jp(weather_str):
    if not weather_str:
        return None
    m = re.search(r'風向[き]?\s*[:：]\s*([東西南北]+)', weather_str)
    if m:
        return m.group(1)
    return None


def _classify_speed_adjusted(ws):
    adj = ws - 1.0
    if adj <= 0.5:
        return "無風"
    if adj <= 2.0:
        return "弱風"
    if adj <= 3.5:
        return "中風"
    return "強風"


def _classify_wind_at_direction(wfd, rd):
    diff = abs(wfd - rd) % 360
    diff = min(diff, 360 - diff)
    if diff <= 45:
        return "向かい風"
    if diff >= 135:
        return "追い風"
    return "横風"


def _get_wind_cross_jp(wfd, hr):
    rel = (wfd - hr) % 360
    if 45 < rel < 135:
        return "HB横"
    if 225 < rel < 315:
        return "BH横"
    return None


def normalize_venue(v):
    if v == "伊東温泉":
        return "伊東"
    return v


def get_wind_pattern(venue, weather_str):
    venue_home_dir = _STATE["venue_home_dir"]
    if venue in DOME_VENUES:
        return "無風", "無風"
    if not weather_str:
        return None, None
    if "風速:--" in weather_str or "風向:--" in weather_str:
        return "無風", "無風"
    ws = _parse_wind_speed(weather_str)
    if ws is None:
        return None, None
    speed_cls = _classify_speed_adjusted(ws)
    if speed_cls == "無風":
        return "無風", "無風"
    wd_jp = _parse_wind_dir_jp(weather_str)
    if not wd_jp or wd_jp not in _WIND_JP_TO_DEG:
        return None, None
    wfd = _WIND_JP_TO_DEG[wd_jp]
    hd_jp = venue_home_dir.get(venue)
    if not hd_jp:
        return None, None
    hd = _HOME_DIR_TO_DEG.get(hd_jp)
    if hd is None:
        return None, None
    hr = (hd - 90) % 360
    br = (hr + 180) % 360
    hc = _classify_wind_at_direction(wfd, hr)
    bc = _classify_wind_at_direction(wfd, br)
    if hc == "追い風" and bc == "向かい風":
        return "H追B向", speed_cls
    if hc == "向かい風" and bc == "追い風":
        return "H向B追", speed_cls
    if hc == "横風" and bc == "横風":
        cr = _get_wind_cross_jp(wfd, hr)
        if cr:
            return cr, speed_cls
    return None, None


def classify_weather(venue, weather_str):
    if venue in DOME_VENUES:
        return "雨以外"
    if not weather_str:
        return "雨以外"
    if "雨" in weather_str:
        return "雨"
    return "雨以外"


# ============================================================
# 戦術
# ============================================================
def _classify_tactic(pn, pk):
    if pn + pk < 50:
        return "差"
    if pn >= pk * 2:
        return "逃"
    if pk >= pn * 2:
        return "捲"
    return "両"


def _tactic_of(key, is_solo):
    kimari = _STATE["kimari"]
    r = kimari.get(key)
    if r is None:
        return "両"
    if is_solo:
        br = r.get("by_role", {}).get("単騎", {})
        n = br.get("_n", 0)
        if n >= 10:
            pn = 100.0 * br.get("逃", 0) / n
            pk = 100.0 * br.get("捲", 0) / n
            return _classify_tactic(pn, pk)
    else:
        br = r.get("by_role", {}).get("先頭", {})
        n = br.get("_n", 0)
        if n >= 15:
            pn = 100.0 * br.get("逃", 0) / n
            pk = 100.0 * br.get("捲", 0) / n
            return _classify_tactic(pn, pk)
    tot = r.get("total", {})
    tn = tot.get("_n", 0)
    if tn == 0:
        return "両"
    pn = 100.0 * tot.get("逃", 0) / tn
    pk = 100.0 * tot.get("捲", 0) / tn
    return _classify_tactic(pn, pk)


def _make_player_key(full_info):
    if not isinstance(full_info, str):
        return None
    m = re.search(r"(\d+)\s*期", full_info)
    if not m:
        return None
    parts = full_info.split("/")
    return "%s|%s" % (parts[0].strip(), m.group(0).replace(" ", ""))


# ============================================================
# 周回中並び予測 (旧 predict_shukai_v2 内蔵)
# ============================================================
def _front_rate(bike, s):
    front_table = _STATE["front_table"]
    sbin = s if s <= 19 else 20
    v = front_table.get(str(bike), {}).get(str(sbin))
    return 50.0 if v is None else v


def _s_bin(s):
    if s == 0:
        return "0"
    if s <= 4:
        return "1-4"
    if s <= 9:
        return "5-9"
    if s <= 14:
        return "10-14"
    return "15+"


def _diff_bin(d):
    if d < -20:
        return "<-20"
    if d < 0:
        return "-20-0"
    if d < 20:
        return "0-20"
    if d < 40:
        return "20-40"
    return "40+"


def _get_var_value(var_name, rep_info):
    if var_name == "L1bike":
        return str(rep_info[0]["bike"])
    if var_name == "L1S":
        return _s_bin(rep_info[0]["s"])
    m = re.match(r"L(\d)L(\d)diff", var_name)
    if m:
        i = int(m.group(1)) - 1
        j = int(m.group(2)) - 1
        if i >= len(rep_info) or j >= len(rep_info):
            return None
        return _diff_bin(rep_info[i]["rate"] - rep_info[j]["rate"])
    return None


def parse_line_str(line_str):
    chunks = []
    for part in line_str.split("-"):
        bs = [int(c) for c in part if c.isdigit()]
        if bs:
            chunks.append(bs)
    return chunks


def predict_shukai(line_str, players_info):
    form_config = _STATE["form_config"]
    lookup_table = _STATE["lookup_table"]

    lines_ = parse_line_str(line_str)
    if not lines_:
        return {"form_supported": False, "error": "line_parse_fail"}
    if sum(len(c) for c in lines_) != 7:
        return {"form_supported": False, "error": "not_7"}
    if all(len(c) == 1 for c in lines_):
        return {"form_supported": False, "error": "all_solo"}

    st = "-".join(str(len(c)) for c in lines_)
    if st not in form_config:
        return {"form_supported": False, "size_tag": st,
                "error": "form_not_supported"}

    rep_info = []
    for li, lb in enumerate(lines_):
        cand = []
        for b in lb:
            if b not in players_info:
                return {"form_supported": False, "error": "no_player_info"}
            s = players_info[b].get("s")
            if not isinstance(s, int):
                return {"form_supported": False, "error": "no_s"}
            cand.append((s, b))
        cand.sort(reverse=True)
        s_max, b_max = cand[0]
        rep_info.append({"bike": b_max, "s": s_max,
                         "rate": _front_rate(b_max, s_max)})

    vars_ = form_config[st]
    cell = tuple(_get_var_value(v, rep_info) for v in vars_)
    if None in cell:
        return {"form_supported": False, "error": "var_value_none"}
    cell_str = "|".join(cell)

    table = lookup_table.get(st, {})
    if cell_str not in table:
        return {
            "form_supported": True, "size_tag": st, "cell_str": cell_str,
            "cell_hit": False, "outcome_dist": {},
            "top_outcome": "維持", "top_prob": 0.0,
            "prediction_type": "default_only", "fallback": True,
        }

    info = table[cell_str]
    dist = info["pct"]
    top_outcome, top_prob = max(dist.items(), key=lambda kv: kv[1])
    pred_type = "default_only" if top_outcome == "維持" else "with_alt"

    return {
        "form_supported": True, "size_tag": st, "cell_str": cell_str,
        "cell_hit": True, "cell_total": info["total"],
        "outcome_dist": dist, "top_outcome": top_outcome,
        "top_prob": top_prob, "prediction_type": pred_type,
    }


# ============================================================
# 表記
# ============================================================
def _label_map_by_order(lines, order):
    lc, tc = 0, 0
    lab = {}
    for li in order:
        sz = len(lines[li])
        if sz == 1:
            tc += 1
            lab[li] = "T%d" % tc
        else:
            lc += 1
            lab[li] = "%dL%d" % (sz, lc)
    return lab


def _role_to_bike_map(lines, order):
    lab = _label_map_by_order(lines, order)
    r2b = {}
    for li in order:
        base = lab[li]
        lb = lines[li]
        if base.startswith("T"):
            r2b[base] = lb[0]
        else:
            for idx, bk in enumerate(lb):
                r2b["%s#%d" % (base, idx + 1)] = bk
    return r2b


def _shukai_norm_tag(lines, order, tactics_by_line):
    lab = _label_map_by_order(lines, order)
    return "-".join("%s%s" % (lab[li], tactics_by_line.get(li, "両"))
                    for li in order)


def _get_most_freq_order_for_outcome(size_tag, outcome, n_lines):
    lap_dict = _STATE["lap_dict"]
    if size_tag not in lap_dict:
        return None
    patterns = lap_dict[size_tag]["patterns"]
    default_order = list(range(n_lines))
    cand = []
    for raw_tag, info in patterns.items():
        parts = raw_tag.split(">")
        order = [int(p[1:]) - 1 for p in parts]
        if not order:
            continue
        head_li = order[0]
        if order == default_order:
            oc = "維持"
        elif head_li == 0:
            oc = "その他"
        else:
            oc = "L%d先頭" % (head_li + 1)
        if oc == outcome:
            cand.append((order, info["pct"]))
    if not cand:
        return None
    cand.sort(key=lambda x: -x[1])
    return tuple(cand[0][0])


def _recover_order_from_bag_tag(bag_tag_with, lines):
    segs = bag_tag_with.split("-")
    seg_clean = [re.sub(r"(逃|捲|両|差)", "", s) for s in segs]
    normal_indices = [li for li, lb in enumerate(lines) if len(lb) >= 2]
    solo_indices = [li for li, lb in enumerate(lines) if len(lb) == 1]
    order = []
    pool_normal = defaultdict(list)
    for li in normal_indices:
        pool_normal[len(lines[li])].append(li)
    pool_solo = list(solo_indices)
    for s in seg_clean:
        m = re.match(r"(\d+)L(\d+)$", s)
        if m:
            n = int(m.group(1))
            if not pool_normal[n]:
                return None
            li = pool_normal[n].pop(0)
            order.append(li)
        else:
            m2 = re.match(r"T(\d+)$", s)
            if not m2 or not pool_solo:
                return None
            li = pool_solo.pop(0)
            order.append(li)
    if len(order) != len(lines):
        return None
    return order


# ============================================================
# バックオフ買い目辞書
# ============================================================
def _get_bet_with_backoff(bag_with_tactic, bag_without_tactic, cond_keys):
    bet_dict_v3 = _STATE["bet_dict_v3"]
    k0, k1, k2, k3 = cond_keys
    lv0_table = bet_dict_v3.get("lv0_normal", {})
    if k0 in lv0_table:
        bag_data = lv0_table[k0].get(bag_with_tactic)
        if bag_data and bag_data.get("total", 0) >= THRESHOLD:
            return bag_data["patterns"], "lv0"
    lv1_table = bet_dict_v3.get("lv1_no_weather", {})
    if k1 in lv1_table:
        bag_data = lv1_table[k1].get(bag_with_tactic)
        if bag_data and bag_data.get("total", 0) >= THRESHOLD:
            return bag_data["patterns"], "lv1"
    lv2_table = bet_dict_v3.get("lv2_no_speed", {})
    if k2 in lv2_table:
        bag_data = lv2_table[k2].get(bag_with_tactic)
        if bag_data and bag_data.get("total", 0) >= THRESHOLD:
            return bag_data["patterns"], "lv2"
    lv3_table = bet_dict_v3.get("lv3_venue_only_no_tac", {})
    if k3 in lv3_table:
        bag_data = lv3_table[k3].get(bag_without_tactic)
        if bag_data and bag_data.get("total", 0) >= THRESHOLD:
            return bag_data["patterns"], "lv3"
    return None, "miss"


# ============================================================
# エンドツーエンド予測 (全候補生成・top_n切りなし)
# ============================================================
def _predict_all_candidates(line_str, players_info, venue, weather_str,
                            prob_threshold=5.0):
    """全3連単候補を生成して返す (top_n切りはしない)。
    返り値: {"ok":bool, "reason":str, "candidates":[{...}], "meta":{...}}
      candidates の各要素: {"bikes":(a,b,c), "weight":float}
    """
    lines = parse_line_str(line_str)
    if not lines:
        return {"ok": False, "reason": "line_parse_fail"}
    size_tag = "-".join(str(len(c)) for c in lines)
    n_lines = len(lines)

    final_dict = _STATE["final_dict"]

    # 戦術
    tactics_by_line = {}
    for li, lb in enumerate(lines):
        head_bk = lb[0]
        p = players_info.get(head_bk)
        if not isinstance(p, dict):
            tactics_by_line[li] = "両"
            continue
        key = _make_player_key(p.get("full_info", ""))
        if not key:
            tactics_by_line[li] = "両"
            continue
        tactics_by_line[li] = _tactic_of(key, is_solo=(len(lb) == 1))

    # 周回中予測
    res = predict_shukai(line_str, players_info)
    if not res.get("form_supported"):
        return {"ok": False, "reason": res.get("error", "form_not_supported"),
                "detail": res}
    if not res.get("cell_hit", True):
        return {"ok": False, "reason": "cell_miss", "size_tag": size_tag}
    outcome_dist = res["outcome_dist"]

    # 条件キー
    venue_n = normalize_venue(venue)
    wind_pat, speed_cls = get_wind_pattern(venue_n, weather_str)
    if wind_pat is None:
        wind_pat, speed_cls = "無風", "無風"
    weather_cat = classify_weather(venue_n, weather_str)
    if wind_pat == "無風":
        k0 = "%s|無風|%s" % (venue_n, weather_cat)
        k1 = "%s|無風" % venue_n
        k2 = "%s|無風" % venue_n
    else:
        k0 = "%s|%s|%s|%s" % (venue_n, wind_pat, speed_cls, weather_cat)
        k1 = "%s|%s|%s" % (venue_n, wind_pat, speed_cls)
        k2 = "%s|%s" % (venue_n, wind_pat)
    k3 = "%s|無風" % venue_n
    cond_keys = (k0, k1, k2, k3)

    bet_aggregated = Counter()
    backoff_stats = Counter()
    total_coverage = 0.0

    for outcome, prob in outcome_dist.items():
        if prob < prob_threshold:
            continue
        order = _get_most_freq_order_for_outcome(size_tag, outcome, n_lines)
        if order is None:
            continue
        s_tag = _shukai_norm_tag(lines, order, tactics_by_line)
        if s_tag not in final_dict:
            continue
        final_info = final_dict[s_tag]
        final_patterns = final_info["patterns"]

        sub_levels = Counter()
        for bag_tag_with, fpinfo in final_patterns.items():
            back_pct = fpinfo["pct"]
            bag_tag_without = re.sub(r"(逃|捲|両|差)", "", bag_tag_with)
            back_order = _recover_order_from_bag_tag(bag_tag_with, lines)
            if back_order is None:
                continue
            r2b = _role_to_bike_map(lines, back_order)
            patterns, level = _get_bet_with_backoff(
                bag_tag_with, bag_tag_without, cond_keys)
            sub_levels[level] += 1
            if patterns is None:
                continue
            bet_total = sum(p.get("count", 0) for p in patterns.values())
            if bet_total == 0:
                continue
            for triplet_tag, bpinfo in patterns.items():
                trip_clean = re.sub(r"(逃|捲|両|差)", "", triplet_tag)
                roles = trip_clean.split(">")
                try:
                    bikes = tuple(r2b[r] for r in roles)
                except KeyError:
                    continue
                w = prob * (back_pct / 100.0) * (bpinfo["count"] / bet_total)
                bet_aggregated[bikes] += w

        total_coverage += prob
        for lv, c in sub_levels.items():
            backoff_stats[lv] += c

    # raw_score補正
    bet_corrected = {}
    for bikes, w in bet_aggregated.items():
        rs_sum = 0.0
        rs_ok = True
        for bk in bikes:
            p = players_info.get(bk)
            if not isinstance(p, dict):
                rs_ok = False
                break
            rs = p.get("raw_score")
            if not isinstance(rs, (int, float)):
                rs_ok = False
                break
            rs_sum += rs
        bet_corrected[bikes] = w * rs_sum if rs_ok else w

    candidates = []
    for bikes, w in bet_corrected.items():
        candidates.append({"bikes": bikes, "weight": w})
    candidates.sort(key=lambda c: -c["weight"])

    meta = {
        "size_tag": size_tag, "venue": venue_n,
        "wind_pat": wind_pat, "speed_cls": speed_cls,
        "weather_cat": weather_cat, "cond_keys": list(cond_keys),
        "tactics": tactics_by_line, "outcome_dist": outcome_dist,
        "backoff_stats": dict(backoff_stats),
        "total_coverage": round(total_coverage, 1),
        "total_unique_3t": len(bet_corrected),
    }
    return {"ok": True, "reason": "ok", "candidates": candidates, "meta": meta}


# ============================================================
# モード別フィルタ
# ============================================================
def _filter_priest(candidates, axis, rival, rival_mode):
    # 軸は必須 (呼び出し前に検証済み)。万一axisなしで到達したら空。
    if axis is None:
        return []
    result = []
    for c in candidates:
        bikes = c["bikes"]
        if rival is None:
            # 軸のみ: 固定(軸が1着)のみ。連対スイッチは無効。
            if bikes[0] == axis:
                result.append(c)
        else:
            # 軸+対抗
            if rival_mode == "fixed":
                # 固定: 軸=1着, 対抗=2着
                if bikes[0] == axis and bikes[1] == rival:
                    result.append(c)
            else:
                # 連対: 軸・対抗が1着2着のどちらか (順不同)
                if set(bikes[:2]) == set([axis, rival]):
                    result.append(c)
    return result


def _filter_bishop(candidates, include_bikes):
    if not include_bikes:
        return []
    inc_set = set(include_bikes)
    result = []
    for c in candidates:
        if inc_set.issubset(set(c["bikes"])):
            result.append(c)
    return result


# ============================================================
# 公開エントリ
# ============================================================
def predict_all_raw(line_str, players_info, venue, weather_str, cap_n=40):
    """事前計算用。フィルタ前の全候補を生weightで返す。
    アプリ側で maria/priest/bishop の絞り込みと norm_pct 再計算を行うため、
    norm_pct は付けず weight (raw_score補正済みの生スコア) のみ返す。

    返り値:
      {"ok": bool, "reason": str,
       "all": [{"3t":"1-4-6", "weight": float}, ...],   # weight降順, 最大cap_n件
       "metadata": {...}}
    """
    if not _STATE["loaded"]:
        return {"ok": False, "reason": "dict_not_loaded", "all": [], "metadata": {}}
    if not isinstance(players_info, dict) or not players_info:
        return {"ok": False, "reason": "no_players_info", "all": [], "metadata": {}}
    for bk, p in players_info.items():
        if not isinstance(p, dict) or not isinstance(p.get("s"), int):
            return {"ok": False, "reason": "s_missing", "all": [], "metadata": {}}

    base = _predict_all_candidates(line_str, players_info, venue, weather_str,
                                   prob_threshold=5.0)
    if not base["ok"]:
        return {"ok": False, "reason": base["reason"], "all": [],
                "metadata": base.get("meta", {})}

    try:
        cap = int(cap_n)
    except Exception:
        cap = 40
    if cap < 1:
        cap = 1

    cands = base["candidates"][:cap]
    out = []
    for c in cands:
        out.append({"3t": "%d-%d-%d" % c["bikes"], "weight": round(c["weight"], 6)})
    return {"ok": True, "reason": "ok", "all": out, "metadata": base["meta"]}


def predict_cathedral(line_str, players_info, venue, weather_str,
                      mode="maria", top_n=20,
                      axis=None, rival=None, rival_mode="fixed",
                      include_bikes=None):
    """大聖堂タブ予測エンジン。

    返り値:
      {"ok": bool, "reason": str,
       "candidates": [{"3t":"1-4-6", "norm_pct":10.15, "weight":..}, ...],
       "metadata": {...}}
    """
    if not _STATE["loaded"]:
        return {"ok": False, "reason": "dict_not_loaded",
                "candidates": [], "metadata": {}}

    if top_n is None:
        top_n = 20
    try:
        top_n = int(top_n)
    except Exception:
        top_n = 20
    if top_n < 1:
        top_n = 1
    if top_n > 20:
        top_n = 20

    # mode別の入力検証
    if mode == "bishop":
        if not include_bikes:
            return {"ok": False, "reason": "bishop_no_bike",
                    "candidates": [], "metadata": {}}
        if len(include_bikes) > 2:
            return {"ok": False, "reason": "bishop_too_many",
                    "candidates": [], "metadata": {}}
    elif mode == "priest":
        if axis is None and rival is None:
            # 軸も対抗もなし -> 予想不可
            return {"ok": False, "reason": "priest_no_axis",
                    "candidates": [], "metadata": {}}
        elif axis is None and rival is not None:
            # 対抗のみ指定は不可 (軸が必須)
            return {"ok": False, "reason": "priest_rival_only",
                    "candidates": [], "metadata": {}}

    # s欠損チェック (新ロジックは s:int 必須)
    if not isinstance(players_info, dict) or not players_info:
        return {"ok": False, "reason": "no_players_info",
                "candidates": [], "metadata": {}}
    for bk, p in players_info.items():
        if not isinstance(p, dict) or not isinstance(p.get("s"), int):
            return {"ok": False, "reason": "s_missing",
                    "candidates": [], "metadata": {}}

    base = _predict_all_candidates(line_str, players_info, venue, weather_str,
                                   prob_threshold=5.0)
    if not base["ok"]:
        return {"ok": False, "reason": base["reason"],
                "candidates": [], "metadata": base.get("meta", {})}

    cands = base["candidates"]

    if mode == "priest":
        cands = _filter_priest(cands, axis, rival, rival_mode)
    elif mode == "bishop":
        cands = _filter_bishop(cands, include_bikes)
    # maria: フィルタなし

    if not cands:
        return {"ok": True, "reason": "no_candidate_after_filter",
                "candidates": [], "metadata": base["meta"]}

    # 正規化はフィルタ後の母集合で行う (表示用の相対確率)
    total_w = sum(c["weight"] for c in cands)
    top = cands[:top_n]
    out = []
    for c in top:
        bikes = c["bikes"]
        w = c["weight"]
        norm_pct = round(100.0 * w / total_w, 3) if total_w > 0 else 0
        out.append({"3t": "%d-%d-%d" % bikes,
                    "norm_pct": norm_pct, "weight": round(w, 6)})

    meta = dict(base["meta"])
    meta["mode"] = mode
    meta["top_n"] = top_n
    meta["n_after_filter"] = len(cands)
    return {"ok": True, "reason": "ok", "candidates": out, "metadata": meta}

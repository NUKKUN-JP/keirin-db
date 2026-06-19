"""
dicts_build_FINAL.py  (フェーズ4本体 / look-ahead解消)

DBから予想用の辞書群を「指定日(cutoff)まで」のレコードだけで生成する。
これにより「前日までのデータで予想」が成立し、walk-forwardバックテスト(フェーズ5)
の土台になる。GitHub Actionsの夜間ジョブで毎晩、前日までのDBから本番辞書を再生成する
用途にも使う。

使い方:
  python dicts_build_FINAL.py                      # 全期間(本番用)、out=既定
  python dicts_build_FINAL.py --cutoff 20251231    # 指定日まで
  python dicts_build_FINAL.py --cutoff 20251231 --out /path/to/dir
  python dicts_build_FINAL.py --only profiles       # 一部の辞書だけ

確定した標準フィルタ (ラモスレオ照合で骨格一致を確認済み):
  1. 7車立てのみ (len(players)==7)
  2. 正常完走のみ (len(result)==7)
  3. 日付 <= cutoff
  4. line解析可能 / lap存在

生成する辞書:
  profiles      → player_profiles_FINAL/ + player_profile_index_FINAL.json
  line_lead     → player_line_lead_rate_FINAL.json
  rsrank_finish → player_rsrank_finish_7car_FINAL.jsonl
  rawscore_pat  → rawscore_pattern_stats_FINAL.json   (後半パートで実装)
  kimari_stats  → kimari_stats_FINAL.json             (後半パートで実装)

Pydroid3制約: f-string禁止 / for-else禁止 / 完全ファイル提供
"""

import os
import sys
import json
import time

DL = "/storage/emulated/0/Download"
# 入力DB候補: Download配下 → cwd配下 → 環境変数 の順で解決 (Actions/PC対応)
DB_CANDIDATES = [
    os.path.join(DL, "takusen", "data", "keirin_data_scored_v2.jsonl"),
    os.path.join(DL, "keirin_db", "keirin_data_scored_v2.jsonl"),
    os.path.join(DL, "keirin_data_scored_v2.jsonl"),
    os.path.join(os.getcwd(), "takusen", "data", "keirin_data_scored_v2.jsonl"),
    os.path.join(os.getcwd(), "keirin_data_scored_v2.jsonl"),
]
_env_db = os.environ.get("KEIRIN_DB", "").strip()
if _env_db:
    DB_CANDIDATES.insert(0, _env_db)
# 出力先・static: Download配下が無ければ cwd/takusen/data 配下にフォールバック
if os.path.isdir(os.path.join(DL, "takusen", "data")):
    _BASE_DATA = os.path.join(DL, "takusen", "data")
elif os.path.isdir(os.path.join(os.getcwd(), "takusen", "data")):
    _BASE_DATA = os.path.join(os.getcwd(), "takusen", "data")
else:
    _BASE_DATA = os.getcwd()
DEFAULT_OUT = os.path.join(_BASE_DATA, "dicts")
STATIC_DIR = os.path.join(_BASE_DATA, "static")

SCENES = ["周回中", "赤板", "打鐘", "ホーム", "バック"]
POS_LABELS = ["L", "S", "T", "F", "F", "F"]

DOME_VENUES = set(["前橋", "小倉", "千葉"])

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

# 選手プロファイル保存の最低出走数 / 信頼ライン
MIN_N_TO_SAVE = 5
MIN_N_RELIABLE = 20


# ============================================================
# 共通ユーティリティ
# ============================================================
def find_db():
    for p in DB_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def parse_args(argv):
    opt = {"cutoff": "99999999", "out": DEFAULT_OUT, "only": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--cutoff" and i + 1 < len(argv):
            opt["cutoff"] = argv[i + 1]
            i = i + 2
            continue
        if a == "--out" and i + 1 < len(argv):
            opt["out"] = argv[i + 1]
            i = i + 2
            continue
        if a == "--only" and i + 1 < len(argv):
            opt["only"] = argv[i + 1]
            i = i + 2
            continue
        i = i + 1
    return opt


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
            if p[j].isdigit():
                digits.append(p[j])
            j = j + 1
        if digits:
            chunks.append(digits)
        i = i + 1
    if not chunks:
        return None
    return chunks


def assign_roles(chunks):
    """line文字列の順で役割割当。
    返り値: {bike_str: (canonical_role, simple_role)}"""
    role_map = {}
    line_idx = 0
    i = 0
    while i < len(chunks):
        chunk = chunks[i]
        if len(chunk) > 1:
            line_idx = line_idx + 1
            size = len(chunk)
            j = 0
            while j < len(chunk):
                bs = chunk[j]
                pos = POS_LABELS[j] if j < len(POS_LABELS) else "F"
                crole = str(size) + "L" + str(line_idx) + pos
                if j == 0:
                    srole = "先頭"
                elif j == 1:
                    srole = "番手"
                else:
                    srole = "三番手以降"
                role_map[bs] = (crole, srole)
                j = j + 1
        i = i + 1
    solo_idx = 0
    i = 0
    while i < len(chunks):
        chunk = chunks[i]
        if len(chunk) == 1:
            solo_idx = solo_idx + 1
            role_map[chunk[0]] = ("T" + str(solo_idx), "単騎")
        i = i + 1
    return role_map


def canon_T(crole):
    if crole and crole.startswith("T"):
        try:
            n = int(crole[1:])
            if n >= 2:
                return "T2plus"
        except Exception:
            return crole
    return crole


def make_player_id(full_info):
    if not full_info:
        return None
    parts = full_info.split("/")
    if len(parts) < 4:
        return None
    name = parts[0].replace(" ", "").replace("\u3000", "")
    kihan = ""
    for ch in parts[3]:
        if ch.isdigit():
            kihan = kihan + ch
        elif kihan:
            break
    if not kihan:
        return None
    return name + "_" + kihan


def player_name(full_info):
    if not full_info:
        return ""
    return full_info.split("/")[0]


def player_pref(full_info):
    parts = full_info.split("/") if full_info else []
    if len(parts) >= 2:
        return parts[1]
    return ""


def player_kihan(full_info):
    parts = full_info.split("/") if full_info else []
    if len(parts) >= 4:
        kihan = ""
        for ch in parts[3]:
            if ch.isdigit():
                kihan = kihan + ch
            elif kihan:
                break
        if kihan:
            return int(kihan)
    return None


def rank_of(result, bike):
    if not isinstance(result, list):
        return None
    for r in result:
        if str(r.get("bike")) == str(bike):
            return r.get("rank")
    return None


def scene_xy_of(lap, bike):
    """各シーンでの(x,y)を返す {scene:(x,y)}"""
    out = {}
    if not isinstance(lap, dict):
        return out
    for sc in SCENES:
        arr = lap.get(sc)
        if isinstance(arr, list):
            for e in arr:
                if str(e.get("bike")) == str(bike):
                    out[sc] = (e.get("x"), e.get("y"))
                    break
    return out


def valid_race(race, cutoff):
    """標準フィルタ。Trueなら集計対象"""
    players = race.get("players")
    if not isinstance(players, dict) or len(players) != 7:
        return False
    result = race.get("result")
    if not isinstance(result, list) or len(result) != 7:
        return False
    rdate = str(race.get("date", ""))
    if not rdate or rdate > cutoff:
        return False
    if not isinstance(race.get("lap"), dict):
        return False
    return True


# ============================================================
# プロファイル集計
# ============================================================
def empty_role_block():
    blk = {"n": 0, "rank_dist": {}, "by_scene": {}, "by_scene_x_rank": {}}
    for sc in SCENES:
        blk["by_scene"][sc] = {"x_dist": {}, "y_dist": {}}
        blk["by_scene_x_rank"][sc] = {}
    return blk


def add_to_block(blk, rank, scene_xy):
    blk["n"] = blk["n"] + 1
    rk = str(rank)
    blk["rank_dist"][rk] = blk["rank_dist"].get(rk, 0) + 1
    for sc in SCENES:
        xy = scene_xy.get(sc)
        if not xy or xy[0] is None:
            continue
        xk = str(xy[0])
        yk = str(xy[1])
        bs = blk["by_scene"][sc]
        bs["x_dist"][xk] = bs["x_dist"].get(xk, 0) + 1
        bs["y_dist"][yk] = bs["y_dist"].get(yk, 0) + 1
        sxr = blk["by_scene_x_rank"][sc]
        if xk not in sxr:
            sxr[xk] = {}
        sxr[xk][rk] = sxr[xk].get(rk, 0) + 1


def finalize_block(blk):
    """率(pct)とtop1/2/3、by_scene_x_rank_pctを付与"""
    n = blk["n"]
    # rank_pct
    rp = {}
    if n > 0:
        for k in blk["rank_dist"]:
            rp[k] = round(blk["rank_dist"][k] / n, 4)
    blk["rank_pct"] = rp
    blk["top1"] = round(blk["rank_dist"].get("1", 0) / n, 4) if n else 0
    blk["top2"] = round((blk["rank_dist"].get("1", 0)
                         + blk["rank_dist"].get("2", 0)) / n, 4) if n else 0
    blk["top3"] = round((blk["rank_dist"].get("1", 0)
                         + blk["rank_dist"].get("2", 0)
                         + blk["rank_dist"].get("3", 0)) / n, 4) if n else 0
    # by_scene の pct
    for sc in SCENES:
        bs = blk["by_scene"][sc]
        xt = 0
        for k in bs["x_dist"]:
            xt = xt + bs["x_dist"][k]
        yt = 0
        for k in bs["y_dist"]:
            yt = yt + bs["y_dist"][k]
        bs["x_pct"] = {}
        bs["y_pct"] = {}
        if xt > 0:
            for k in bs["x_dist"]:
                bs["x_pct"][k] = round(bs["x_dist"][k] / xt, 4)
        if yt > 0:
            for k in bs["y_dist"]:
                bs["y_pct"][k] = round(bs["y_dist"][k] / yt, 4)
    # by_scene_x_rank_pct
    bsxrp = {}
    for sc in SCENES:
        bsxrp[sc] = {}
        sxr = blk["by_scene_x_rank"][sc]
        for xk in sxr:
            cnts = sxr[xk]
            tot = 0
            for rk in cnts:
                tot = tot + cnts[rk]
            rkpct = {}
            t1 = t2 = t3 = 0
            if tot > 0:
                for rk in cnts:
                    rkpct[rk] = round(cnts[rk] / tot, 4)
                t1 = round(cnts.get("1", 0) / tot, 4)
                t2 = round((cnts.get("1", 0) + cnts.get("2", 0)) / tot, 4)
                t3 = round((cnts.get("1", 0) + cnts.get("2", 0)
                            + cnts.get("3", 0)) / tot, 4)
            bsxrp[sc][xk] = {"n": tot, "rank_pct": rkpct,
                             "top1": t1, "top2": t2, "top3": t3}
    blk["by_scene_x_rank_pct"] = bsxrp
    return blk


class ProfileAcc:
    """選手別プロファイルの累積器"""

    def __init__(self):
        self.players = {}   # pid -> {meta_fields, overall, by_simple, by_canon, counts}

    def add(self, race):
        players = race.get("players")
        result = race.get("result")
        lap = race.get("lap")
        place = race.get("place", "")
        rdate = str(race.get("date", ""))
        chunks = parse_line_chunks(race.get("line", ""))
        if not chunks:
            return
        role_map = assign_roles(chunks)
        for bs in players:
            pinfo = players[bs]
            fi = pinfo.get("full_info", "")
            pid = make_player_id(fi)
            if not pid or bs not in role_map:
                continue
            rank = rank_of(result, bs)
            if rank is None:
                continue
            crole_raw, srole = role_map[bs]
            crole = canon_T(crole_raw)
            sxy = scene_xy_of(lap, bs)

            if pid not in self.players:
                self.players[pid] = {
                    "name": player_name(fi),
                    "kihan": player_kihan(fi),
                    "prefs_seen": {}, "styles_seen": {}, "venues_raced": {},
                    "first_date": rdate, "last_date": rdate,
                    "total_races": 0,
                    "role_count_canonical": {}, "role_count_simple": {},
                    "overall": empty_role_block(),
                    "by_simple_role": {}, "by_canonical_role": {},
                }
            P = self.players[pid]
            P["total_races"] = P["total_races"] + 1
            pref = player_pref(fi)
            if pref:
                P["prefs_seen"][pref] = P["prefs_seen"].get(pref, 0) + 1
            stl = pinfo.get("style", "")
            if stl:
                P["styles_seen"][stl] = P["styles_seen"].get(stl, 0) + 1
            if place:
                P["venues_raced"][place] = P["venues_raced"].get(place, 0) + 1
            if rdate < P["first_date"]:
                P["first_date"] = rdate
            if rdate > P["last_date"]:
                P["last_date"] = rdate
            P["role_count_canonical"][crole] = \
                P["role_count_canonical"].get(crole, 0) + 1
            P["role_count_simple"][srole] = \
                P["role_count_simple"].get(srole, 0) + 1
            add_to_block(P["overall"], rank, sxy)
            if srole not in P["by_simple_role"]:
                P["by_simple_role"][srole] = empty_role_block()
            add_to_block(P["by_simple_role"][srole], rank, sxy)
            if crole not in P["by_canonical_role"]:
                P["by_canonical_role"][crole] = empty_role_block()
            add_to_block(P["by_canonical_role"][crole], rank, sxy)

    def finalize_and_write(self, out_dir):
        prof_dir = os.path.join(out_dir, "player_profiles_FINAL")
        if not os.path.isdir(prof_dir):
            os.makedirs(prof_dir)
        index = {"meta": {"generated_at": now_str(),
                          "min_n_to_save": MIN_N_TO_SAVE,
                          "min_n_reliable": MIN_N_RELIABLE},
                 "players": {}}
        saved = 0
        for pid in self.players:
            P = self.players[pid]
            if P["total_races"] < MIN_N_TO_SAVE:
                continue
            finalize_block(P["overall"])
            for r in P["by_simple_role"]:
                finalize_block(P["by_simple_role"][r])
            for r in P["by_canonical_role"]:
                finalize_block(P["by_canonical_role"][r])
            primary_style = top_key(P["styles_seen"])
            primary_pref = top_key(P["prefs_seen"])
            primary_rc = top_key(P["role_count_canonical"])
            primary_rs = top_key(P["role_count_simple"])
            reliable = P["total_races"] >= MIN_N_RELIABLE
            meta = {
                "name": P["name"], "kihan": P["kihan"],
                "prefs_seen": P["prefs_seen"], "styles_seen": P["styles_seen"],
                "total_races": P["total_races"],
                "venues_raced": P["venues_raced"],
                "first_date": P["first_date"], "last_date": P["last_date"],
                "primary_style": primary_style,
                "primary_role_canonical": primary_rc,
                "primary_role_simple": primary_rs,
                "primary_pref": primary_pref,
                "venues_count": len(P["venues_raced"]),
                "reliable": reliable,
            }
            doc = {
                "meta": meta,
                "role_count_canonical": P["role_count_canonical"],
                "role_count_simple": P["role_count_simple"],
                "overall": P["overall"],
                "by_simple_role": P["by_simple_role"],
                "by_canonical_role": P["by_canonical_role"],
            }
            wf = open(os.path.join(prof_dir, pid + ".json"), "w",
                      encoding="utf-8")
            json.dump(doc, wf, ensure_ascii=False)
            wf.close()
            index["players"][pid] = {
                "name": P["name"], "n": P["total_races"],
                "reliable": reliable,
                "primary_role_canonical": primary_rc,
                "primary_style": primary_style,
            }
            saved = saved + 1
        wf = open(os.path.join(out_dir, "player_profile_index_FINAL.json"),
                  "w", encoding="utf-8")
        json.dump(index, wf, ensure_ascii=False)
        wf.close()
        return saved


# ============================================================
# ライン先頭率
# ============================================================
class LineLeadAcc:
    def __init__(self):
        self.players = {}

    def add(self, race):
        players = race.get("players")
        lap = race.get("lap")
        chunks = parse_line_chunks(race.get("line", ""))
        if not chunks:
            return
        role_map = assign_roles(chunks)
        # 周回中で各bikeのx位置
        xpos = {}
        if isinstance(lap, dict):
            arr = lap.get("周回中")
            if isinstance(arr, list):
                for e in arr:
                    xpos[str(e.get("bike"))] = e.get("x")
        # 各ラインの先頭(line内j=0)が周回中x==1なら、そのライン所属者は先頭ライン
        lead_line_bikes = set()
        for chunk in chunks:
            head = chunk[0]
            if xpos.get(str(head)) == 1:
                for b in chunk:
                    lead_line_bikes.add(str(b))
        for bs in players:
            fi = players[bs].get("full_info", "")
            pid = make_player_id(fi)
            if not pid or bs not in role_map:
                continue
            crole_raw, srole = role_map[bs]
            crole = canon_T(crole_raw)
            if pid not in self.players:
                self.players[pid] = {
                    "name": player_name(fi), "kihan": player_kihan(fi),
                    "prefs_seen": {}, "styles_seen": {},
                    "total_races": 0, "lead_races": 0, "by_role": {},
                }
            P = self.players[pid]
            P["total_races"] = P["total_races"] + 1
            is_lead = str(bs) in lead_line_bikes
            if is_lead:
                P["lead_races"] = P["lead_races"] + 1
            if crole not in P["by_role"]:
                P["by_role"][crole] = {"n": 0, "lead_n": 0}
            P["by_role"][crole]["n"] = P["by_role"][crole]["n"] + 1
            if is_lead:
                P["by_role"][crole]["lead_n"] = \
                    P["by_role"][crole]["lead_n"] + 1
            stl = players[bs].get("style", "")
            if stl:
                P["styles_seen"][stl] = P["styles_seen"].get(stl, 0) + 1

    def finalize_and_write(self, out_dir):
        doc = {"meta": {"generated_at": now_str(),
                        "min_n_to_save": MIN_N_TO_SAVE,
                        "min_n_reliable": MIN_N_RELIABLE,
                        "definition": "lead_rate = 所属ライン先頭だった割合"},
               "players": {}}
        saved = 0
        for pid in self.players:
            P = self.players[pid]
            if P["total_races"] < MIN_N_TO_SAVE:
                continue
            for r in P["by_role"]:
                br = P["by_role"][r]
                br["lead_rate"] = round(br["lead_n"] / br["n"], 4) if br["n"] else 0
            doc["players"][pid] = {
                "name": P["name"], "kihan": P["kihan"],
                "styles_seen": P["styles_seen"],
                "total_races": P["total_races"], "lead_races": P["lead_races"],
                "lead_rate": round(P["lead_races"] / P["total_races"], 4),
                "reliable": P["total_races"] >= MIN_N_RELIABLE,
                "by_role": P["by_role"],
            }
            saved = saved + 1
        wf = open(os.path.join(out_dir, "player_line_lead_rate_FINAL.json"),
                  "w", encoding="utf-8")
        json.dump(doc, wf, ensure_ascii=False)
        wf.close()
        return saved


# ============================================================
# rsrank_finish (raw_score順位×着順分布)
# ============================================================
class RsRankAcc:
    def __init__(self):
        self.players = {}   # player_key -> {rs_rank: {着順: count}}
        self.baseline = {}  # rs_rank -> {着順: count}

    def add(self, race):
        players = race.get("players")
        result = race.get("result")
        # raw_scoreでrsrank確定
        scored = []
        for bs in players:
            rs = players[bs].get("raw_score")
            if rs is None:
                return  # raw_score欠損レースはスキップ
            scored.append((bs, rs))
        scored.sort(key=lambda x: -x[1])
        rsrank = {}
        i = 0
        while i < len(scored):
            rsrank[scored[i][0]] = i + 1
            i = i + 1
        for bs in players:
            fi = players[bs].get("full_info", "")
            pid = make_player_id(fi)
            if not pid:
                continue
            rank = rank_of(result, bs)
            if rank is None:
                continue
            rr = rsrank.get(bs)
            if rr is None:
                continue
            rrk = str(rr)
            rkk = str(rank)
            if pid not in self.players:
                self.players[pid] = {}
            if rrk not in self.players[pid]:
                self.players[pid][rrk] = {}
            self.players[pid][rrk][rkk] = \
                self.players[pid][rrk].get(rkk, 0) + 1
            if rrk not in self.baseline:
                self.baseline[rrk] = {}
            self.baseline[rrk][rkk] = self.baseline[rrk].get(rkk, 0) + 1

    def finalize_and_write(self, out_dir):
        # baseline率
        base = {}
        for rrk in self.baseline:
            tot = 0
            for rk in self.baseline[rrk]:
                tot = tot + self.baseline[rrk][rk]
            pct = []
            i = 1
            while i <= 7:
                pct.append(round(self.baseline[rrk].get(str(i), 0) / tot, 4)
                           if tot else 0)
                i = i + 1
            base[rrk] = {"n": tot, "pct": pct}
        path = os.path.join(out_dir, "player_rsrank_finish_7car_FINAL.jsonl")
        wf = open(path, "w", encoding="utf-8")
        # 1行目: baseline
        wf.write(json.dumps({"player_key": "__baseline__",
                             "by_rs_rank": base}, ensure_ascii=False) + "\n")
        saved = 0
        for pid in self.players:
            byrr = {}
            total_n = 0
            for rrk in self.players[pid]:
                cnts = self.players[pid][rrk]
                tot = 0
                for rk in cnts:
                    tot = tot + cnts[rk]
                total_n = total_n + tot
                pct = []
                i = 1
                while i <= 7:
                    pct.append(round(cnts.get(str(i), 0) / tot, 4) if tot else 0)
                    i = i + 1
                byrr[rrk] = {"n": tot, "pct": pct}
            if total_n < MIN_N_TO_SAVE:
                continue
            wf.write(json.dumps({"player_key": pid, "by_rs_rank": byrr},
                                ensure_ascii=False) + "\n")
            saved = saved + 1
        wf.close()
        return saved


# ============================================================
# 風向・バンク (predict_today_v31と同一ロジック / 集計は予想と同じ-1m補正)
# ============================================================
import re as _re


def parse_wind_speed(weather_str):
    if not weather_str:
        return None
    m = _re.search(r'風速\s*[:：]\s*(\d+(?:\.\d+)?)\s*m', weather_str)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            return None
    return None


def parse_wind_dir_jp(weather_str):
    if not weather_str:
        return None
    m = _re.search(r'風向[き]?\s*[:：]\s*([東西南北]+)', weather_str)
    if m:
        return m.group(1)
    return None


def classify_speed_adjusted(wind_speed_raw):
    """予想と同一: 公式風速から -1m 補正してクラス分け"""
    adj = wind_speed_raw - 1.0
    if adj <= 0.5:
        return "無風", adj
    if adj <= 2.0:
        return "弱風", adj
    if adj <= 3.5:
        return "中風", adj
    return "強風", adj


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


def get_wind_pattern(venue, weather_str, venue_home_dir):
    """(wind_pat, speed_cls) or (None, None)。集計用(予想と同一の-1m補正)"""
    if venue in DOME_VENUES:
        return "無風", "無風"
    if not weather_str:
        return None, None
    if "風速:--" in weather_str or "風向:--" in weather_str:
        return "無風", "無風"
    wind_speed = parse_wind_speed(weather_str)
    if wind_speed is None:
        return None, None
    speed_cls, _adj = classify_speed_adjusted(wind_speed)
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


def get_bank_attrs(venue, bank_data):
    bd = bank_data.get(venue, {})
    if not bd:
        return None
    circ = bd.get("circ_class", bd.get("circumference_class"))
    cant = bd.get("cant_class", bd.get("cant_classification"))
    straight = bd.get("straight_class", bd.get("str_class"))
    if not circ or not cant or not straight:
        return None
    return {"circ": str(circ), "cant": str(cant), "straight": str(straight)}


def make_bank_wind_key(bank_attrs, wind_pat, speed_cls):
    base = bank_attrs["circ"] + "|" + bank_attrs["cant"] + "|" + bank_attrs["straight"]
    if wind_pat == "無風" or speed_cls == "無風":
        return base + "|無風"
    return base + "|" + wind_pat + "|" + speed_cls


def make_kimari_cell_key(venue, wind_pat, speed_cls):
    if wind_pat == "無風" or speed_cls == "無風":
        return venue + "|無風"
    return venue + "|" + wind_pat + "|" + speed_cls


def normalize_kimari(finish_raw):
    """DBのfinish生値(まくり/逃切り/逃残/差し/マーク等)を、辞書の決まり手
    3分類(逃/捲/差)へ正規化する。元のkimari_statsと同じ表記に揃えるため必須。
    分類不能なものはNone(集計から除外)。"""
    if not finish_raw:
        return None
    s = str(finish_raw)
    # 逃げ系
    if "逃" in s:
        return "逃"
    # まくり系 (捲)
    if "捲" in s or "まく" in s or "マク" in s:
        return "捲"
    # 差し系・マーク系 (差)
    if "差" in s or "サシ" in s or "さし" in s:
        return "差"
    if "マーク" in s or "マ" == s:
        return "差"
    return None


def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        f = open(path, "r", encoding="utf-8")
        d = json.load(f)
        f.close()
        return d
    except Exception:
        return {}


def rsrank_map(players):
    """raw_scoreでrsrank(1..7)を返す {bike: rank}。raw_score欠損ならNone"""
    scored = []
    for bs in players:
        rs = players[bs].get("raw_score")
        if rs is None:
            return None
        scored.append((bs, rs))
    scored.sort(key=lambda x: -x[1])
    out = {}
    i = 0
    while i < len(scored):
        out[scored[i][0]] = i + 1
        i = i + 1
    return out


# ============================================================
# rawscore_pattern_stats (バンク×風セル × rsrank別 着順・シーンx分布)
# ============================================================
class RawscorePatternAcc:
    def __init__(self, bank_data, venue_home_dir):
        self.bank_data = bank_data
        self.vhd = venue_home_dir
        # by_winner_rsrank[rsrank] = {n, win_n, by_bank_wind{key: cell}}
        self.bw = {}
        for i in range(1, 8):
            self.bw[str(i)] = {"n": 0, "win_n": 0, "by_bank_wind": {}}
        self.scanned = 0
        self.used = 0

    def _empty_cell(self):
        cell = {"n": 0, "by_rsrank": {}, "kimari_total": 0,
                "kimari_1st_dist": {}, "top_trifectas_cnt": {}}
        return cell

    def _empty_rsrank_entry(self):
        e = {"rank_dist": {}, "scene_x_dist": {}}
        for sc in SCENES:
            e["scene_x_dist"][sc] = {}
        return e

    def add(self, race):
        self.scanned = self.scanned + 1
        players = race.get("players")
        result = race.get("result")
        lap = race.get("lap")
        venue = race.get("place", "")
        rsr = rsrank_map(players)
        if rsr is None:
            return
        bank_attrs = get_bank_attrs(venue, self.bank_data)
        if not bank_attrs:
            return
        wind_pat, speed_cls = get_wind_pattern(venue, race.get("weather", ""),
                                               self.vhd)
        if wind_pat is None:
            return
        key = make_bank_wind_key(bank_attrs, wind_pat, speed_cls)
        self.used = self.used + 1
        # 各選手のrsrankごとに着順・シーンx
        # 1着のrsrank
        winner_bike = None
        for r in result:
            if r.get("rank") == 1:
                winner_bike = str(r.get("bike"))
                break
        for bs in players:
            rr = rsr.get(bs)
            rank = rank_of(result, bs)
            if rr is None or rank is None:
                continue
            rrk = str(rr)
            self.bw[rrk]["n"] = self.bw[rrk]["n"] + 1
            if winner_bike is not None and str(bs) == winner_bike:
                self.bw[rrk]["win_n"] = self.bw[rrk]["win_n"] + 1
            cells = self.bw[rrk]["by_bank_wind"]
            if key not in cells:
                cells[key] = self._empty_cell()
            cell = cells[key]
            cell["n"] = cell["n"] + 1
            if rrk not in cell["by_rsrank"]:
                cell["by_rsrank"][rrk] = self._empty_rsrank_entry()
            ent = cell["by_rsrank"][rrk]
            rkk = str(rank)
            ent["rank_dist"][rkk] = ent["rank_dist"].get(rkk, 0) + 1
            sxy = scene_xy_of(lap, bs)
            for sc in SCENES:
                xy = sxy.get(sc)
                if not xy or xy[0] is None:
                    continue
                xk = str(xy[0])
                ent["scene_x_dist"][sc][xk] = \
                    ent["scene_x_dist"][sc].get(xk, 0) + 1
        # セル単位の決まり手・3連単(1着のrsrankセルに紐づけ)
        if winner_bike is not None:
            wrr = rsr.get(winner_bike)
            if wrr is not None:
                cells = self.bw[str(wrr)]["by_bank_wind"]
                if key in cells:
                    cell = cells[key]
                    fin = ""
                    for r in result:
                        if r.get("rank") == 1:
                            fin = normalize_kimari(r.get("finish", ""))
                            break
                    if fin:
                        cell["kimari_total"] = cell["kimari_total"] + 1
                        cell["kimari_1st_dist"][fin] = \
                            cell["kimari_1st_dist"].get(fin, 0) + 1
                    # 3連単
                    top3 = sorted([r for r in result if r.get("rank") in (1, 2, 3)],
                                  key=lambda x: x.get("rank"))
                    if len(top3) == 3:
                        tk = (str(top3[0].get("bike")) + "-"
                              + str(top3[1].get("bike")) + "-"
                              + str(top3[2].get("bike")))
                        cell["top_trifectas_cnt"][tk] = \
                            cell["top_trifectas_cnt"].get(tk, 0) + 1

    def finalize_and_write(self, out_dir):
        out = {"meta": {"generated_at": now_str(),
                        "definition": "周長×カント×直線×風向×風速別 rsrank着順/シーンx分布",
                        "scenes": SCENES,
                        "total_races_scanned": self.scanned,
                        "total_races_used": self.used,
                        "wind_adjust": "-1m (予想エンジンと同一)"},
               "by_winner_rsrank": {}}
        for rrk in self.bw:
            blk = self.bw[rrk]
            occ = round(blk["win_n"] / blk["n"], 4) if blk["n"] else 0
            cells_out = {}
            for key in blk["by_bank_wind"]:
                cell = blk["by_bank_wind"][key]
                byr = {}
                for k2 in cell["by_rsrank"]:
                    ent = cell["by_rsrank"][k2]
                    # rank_dist率化
                    rt = 0
                    for rk in ent["rank_dist"]:
                        rt = rt + ent["rank_dist"][rk]
                    rd = {}
                    if rt > 0:
                        for rk in ent["rank_dist"]:
                            rd[rk] = round(ent["rank_dist"][rk] / rt, 4)
                    # scene_x_dist率化
                    sxd = {}
                    for sc in SCENES:
                        cnts = ent["scene_x_dist"][sc]
                        tot = 0
                        for xk in cnts:
                            tot = tot + cnts[xk]
                        sd = {}
                        if tot > 0:
                            for xk in cnts:
                                sd[xk] = round(cnts[xk] / tot, 4)
                        sxd[sc] = sd
                    byr[k2] = {"rank_dist": rd, "scene_x_dist": sxd}
                # kimari率化
                kt = cell["kimari_total"]
                kd = {}
                if kt > 0:
                    for fk in cell["kimari_1st_dist"]:
                        kd[fk] = round(cell["kimari_1st_dist"][fk] / kt, 4)
                # top_trifectas
                tris = []
                for tk in cell["top_trifectas_cnt"]:
                    tris.append({"key": tk, "n": cell["top_trifectas_cnt"][tk],
                                 "rate": round(cell["top_trifectas_cnt"][tk]
                                               / cell["n"], 4) if cell["n"] else 0})
                tris.sort(key=lambda x: -x["n"])
                cells_out[key] = {"n": cell["n"], "by_rsrank": byr,
                                  "kimari_total": kt, "kimari_1st_dist": kd,
                                  "top_trifectas": tris[:5]}
            out["by_winner_rsrank"][rrk] = {
                "n": blk["n"], "occurrence_rate": occ,
                "by_bank_wind": cells_out}
        wf = open(os.path.join(out_dir, "rawscore_pattern_stats_FINAL.json"),
                  "w", encoding="utf-8")
        json.dump(out, wf, ensure_ascii=False)
        wf.close()
        return self.used


# ============================================================
# kimari_stats (会場×風セル別 決まり手 + 3着位置連動)
# ============================================================
class KimariStatsAcc:
    def __init__(self, venue_home_dir):
        self.vhd = venue_home_dir
        self.cells = {}   # cellkey -> {n, k1st{決まり手:cnt}, klink{決まり手:{cat:cnt}}}
        self.used = 0

    def add(self, race):
        result = race.get("result")
        venue = race.get("place", "")
        wind_pat, speed_cls = get_wind_pattern(venue, race.get("weather", ""),
                                               self.vhd)
        if wind_pat is None:
            return
        key = make_kimari_cell_key(venue, wind_pat, speed_cls)
        # 1着の決まり手
        fin1 = ""
        bike3 = None
        for r in result:
            if r.get("rank") == 1:
                fin1 = normalize_kimari(r.get("finish", ""))
            if r.get("rank") == 3:
                bike3 = str(r.get("bike"))
        if not fin1:
            return
        self.used = self.used + 1
        if key not in self.cells:
            self.cells[key] = {"n": 0, "k1st": {}, "klink": {}}
        c = self.cells[key]
        c["n"] = c["n"] + 1
        c["k1st"][fin1] = c["k1st"].get(fin1, 0) + 1
        # 3着の決まり手(連動カテゴリ: 正規化した3着決まり手)
        fin3 = ""
        for r in result:
            if r.get("rank") == 3:
                fin3 = normalize_kimari(r.get("finish", ""))
        if fin1 not in c["klink"]:
            c["klink"][fin1] = {}
        cat = fin3 if fin3 else "不明"
        c["klink"][fin1][cat] = c["klink"][fin1].get(cat, 0) + 1

    def finalize_and_write(self, out_dir):
        out = {"meta": {"generated_at": now_str(),
                        "definition": "会場×風向×風速別 決まり手 + 3着連動",
                        "total_races_used": self.used,
                        "wind_adjust": "-1m (予想エンジンと同一)"},
               "cells": {}}
        for key in self.cells:
            c = self.cells[key]
            k1 = {}
            if c["n"] > 0:
                for fk in c["k1st"]:
                    k1[fk] = round(c["k1st"][fk] / c["n"], 4)
            klink = {}
            for fk in c["klink"]:
                sub = c["klink"][fk]
                tot = 0
                for ck in sub:
                    tot = tot + sub[ck]
                dist = {}
                if tot > 0:
                    for ck in sub:
                        dist[ck] = round(sub[ck] / tot, 4)
                klink[fk] = {"n": tot, "dist": dist}
            out["cells"][key] = {"n": c["n"], "kimari_1st_dist": k1,
                                 "kimari_link_dist": klink}
        wf = open(os.path.join(out_dir, "kimari_stats_FINAL.json"),
                  "w", encoding="utf-8")
        json.dump(out, wf, ensure_ascii=False)
        wf.close()
        return self.used


# ============================================================
# 共通小道具
# ============================================================
def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def top_key(d):
    best = None
    bestv = -1
    for k in d:
        if d[k] > bestv:
            bestv = d[k]
            best = k
    return best


# ============================================================
# メイン
# ============================================================
def main():
    opt = parse_args(sys.argv[1:])
    db = find_db()
    if not db:
        print("[error] DBが見つかりません")
        return
    out_dir = opt["out"]
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    cutoff = opt["cutoff"]
    only = opt["only"]

    print("============================================")
    print(" dicts_build_FINAL")
    print(" DB    : " + db)
    print(" out   : " + out_dir)
    print(" cutoff: " + cutoff + (" (全期間)" if cutoff == "99999999" else ""))
    print("============================================")

    accs = {}
    if only is None or only == "profiles":
        accs["profiles"] = ProfileAcc()
    if only is None or only == "line_lead":
        accs["line_lead"] = LineLeadAcc()
    if only is None or only == "rsrank_finish":
        accs["rsrank_finish"] = RsRankAcc()
    # rawscore_pat / kimari_stats はバンク・風向データが必要
    need_bank = (only is None or only in ("rawscore_pat", "kimari_stats"))
    if need_bank:
        bank_data = load_json(os.path.join(STATIC_DIR, "bank_data.json"))
        vhd = load_json(os.path.join(STATIC_DIR, "venue_home_direction.json"))
        if not bank_data or not vhd:
            print("[warn] bank_data/venue_home_direction が見つからないため")
            print("       rawscore_pat/kimari_stats はスキップします")
            print("       (static/ に配置されているか確認)")
        else:
            if only is None or only == "rawscore_pat":
                accs["rawscore_pat"] = RawscorePatternAcc(bank_data, vhd)
            if only is None or only == "kimari_stats":
                accs["kimari_stats"] = KimariStatsAcc(vhd)

    n_total = 0
    n_used = 0
    t0 = time.time()
    f = open(db, "r", encoding="utf-8")
    try:
        for line in f:
            s = line.strip()
            if not s:
                continue
            n_total = n_total + 1
            try:
                race = json.loads(s)
            except Exception:
                continue
            if not valid_race(race, cutoff):
                continue
            n_used = n_used + 1
            for key in accs:
                accs[key].add(race)
            if n_used % 5000 == 0:
                print("  ...処理 " + str(n_used) + "レース (経過 "
                      + str(int(time.time() - t0)) + "秒)")
    finally:
        f.close()

    print("")
    print("スキャン: 全" + str(n_total) + " / 有効" + str(n_used))
    print("書き出し中...")
    for key in accs:
        saved = accs[key].finalize_and_write(out_dir)
        print("  " + key + ": " + str(saved) + " 件保存")
    print("完了 (経過 " + str(int(time.time() - t0)) + "秒)")


if __name__ == "__main__":
    main()

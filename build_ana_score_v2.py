# -*- coding: utf-8 -*-
"""
build_ana_score_v2.py

穴特性スコア生成 (venue=会場別 と global=全期間 を1回のDB走査で両方出力)

  venue_ana_score  = (会場×選手の穴帯3着内率) - (会場×選手の全期間3着内率)
  ana_score(global)= (選手の穴帯3着内率) - (選手の全期間3着内率)   ※会場区別なし

入力: 本番DB keirin_data_scored_v2.jsonl をDB直接走査 (中間ファイル依存を廃止)
  入力DB解決順: 環境変数 KEIRIN_DB → takusen/data → Download直下 → cwd

出力: takusen/data/dicts/ に アプリ読込名(_FINAL)で直接出力
  ana_score_venue_FINAL.jsonl  (キー: place/player_name/venue_ana_score/venue_in_band_starts)
  ana_score_global_FINAL.jsonl (キー: player_name/ana_score/in_band_starts)
  KEIRIN_OUT_VENUE / KEIRIN_OUT_GLOBAL で上書き可
  採用サンプル下限: venue=KEIRIN_ANA_VENUE_MIN(既定10) / global=KEIRIN_ANA_GLOBAL_MIN(既定20)

Pydroid3制約: f-string不使用 / for-else不使用
"""
import os
import sys
import json
import re
from collections import defaultdict

DOWNLOAD_DIR = "/storage/emulated/0/Download"
# 入力DB解決
_DATA_DIR = os.path.join(DOWNLOAD_DIR, "takusen", "data")
if not os.path.isdir(_DATA_DIR):
    if os.path.isdir(os.path.join(os.getcwd(), "takusen", "data")):
        _DATA_DIR = os.path.join(os.getcwd(), "takusen", "data")
    elif os.path.isdir(DOWNLOAD_DIR):
        _DATA_DIR = DOWNLOAD_DIR
    else:
        _DATA_DIR = os.getcwd()

_env_db = os.environ.get("KEIRIN_DB", "").strip()
if _env_db:
    DB_PATH = _env_db
else:
    DB_PATH = os.path.join(_DATA_DIR, "keirin_data_scored_v2.jsonl")
    if not os.path.exists(DB_PATH):
        _alt1 = os.path.join(DOWNLOAD_DIR, "keirin_data_scored_v2.jsonl")
        _alt2 = os.path.join(os.getcwd(), "keirin_data_scored_v2.jsonl")
        if os.path.exists(_alt1):
            DB_PATH = _alt1
        elif os.path.exists(_alt2):
            DB_PATH = _alt2

# 出力先 dicts/
_DICTS_DIR = os.path.join(_DATA_DIR, "dicts")
if not os.path.isdir(_DICTS_DIR):
    try:
        os.makedirs(_DICTS_DIR)
    except Exception:
        _DICTS_DIR = _DATA_DIR

_env_v = os.environ.get("KEIRIN_OUT_VENUE", "").strip()
if _env_v:
    OUT_VENUE = _env_v
else:
    OUT_VENUE = os.path.join(_DICTS_DIR, "ana_score_venue_FINAL.jsonl")
_env_g = os.environ.get("KEIRIN_OUT_GLOBAL", "").strip()
if _env_g:
    OUT_GLOBAL = _env_g
else:
    OUT_GLOBAL = os.path.join(_DICTS_DIR, "ana_score_global_FINAL.jsonl")
SUMMARY = os.path.join(_DICTS_DIR, "ana_score_summary.txt")

# 採用サンプル下限
try:
    VENUE_MIN = int(os.environ.get("KEIRIN_ANA_VENUE_MIN", "10"))
except Exception:
    VENUE_MIN = 10
try:
    GLOBAL_MIN = int(os.environ.get("KEIRIN_ANA_GLOBAL_MIN", "20"))
except Exception:
    GLOBAL_MIN = 20


BANDS = [
    ( 5000,  10000),
    (10000,  20000),
    (20000,  30000),
    (30000,  40000),
    (40000,  50000),
    (50000,  60000),
    (60000,  70000),
    (70000,  80000),
]
BAND_MIN = BANDS[0][0]
BAND_MAX = BANDS[-1][1]

N_CARS_REQUIRED = 7
RE_REFUND_AMOUNT = re.compile(r'\(([\d,]+)\s*円\)')

ROLE_NAMES = ["lead", "second", "third", "fourth", "solo"]
N_ROLES = 5
N_RANKS = 7
ROLE_LEAD, ROLE_SECOND, ROLE_THIRD, ROLE_FOURTH, ROLE_SOLO = 0, 1, 2, 3, 4


# =============================================================
# ライン解析
# =============================================================
def parse_line_chunks(line_str):
    if not line_str or not isinstance(line_str, str): return None
    parts = line_str.split("-")
    chunks = []
    i = 0
    while i < len(parts):
        p = parts[i].strip()
        i += 1
        if not p: continue
        digits = []
        j = 0
        while j < len(p):
            ch = p[j]
            j += 1
            if ch.isdigit(): digits.append(ch)
        if digits: chunks.append(digits)
    if not chunks: return None
    return chunks


def chunks_to_sizes(chunks):
    sizes = []
    i = 0
    while i < len(chunks):
        sizes.append(len(chunks[i])); i += 1
    return sizes


def is_all_solo(sizes):
    if not sizes: return False
    i = 0
    while i < len(sizes):
        if sizes[i] != 1: return False
        i += 1
    return True


def total_in_chunks(chunks):
    n = 0; i = 0
    while i < len(chunks):
        n += len(chunks[i]); i += 1
    return n


def build_bike_role_map(chunks):
    m = {}
    i = 0
    while i < len(chunks):
        c = chunks[i]; i += 1
        if len(c) == 1:
            m[c[0]] = ROLE_SOLO; continue
        j = 0
        while j < len(c):
            bs = c[j]
            if j == 0:    m[bs] = ROLE_LEAD
            elif j == 1:  m[bs] = ROLE_SECOND
            elif j == 2:  m[bs] = ROLE_THIRD
            else:         m[bs] = ROLE_FOURTH
            j += 1
    return m


# =============================================================
# 帯
# =============================================================
def in_band_range(refund):
    if refund is None: return False
    return BAND_MIN <= refund < BAND_MAX


def parse_refund_3t(rec):
    raw = rec.get('refund_3t')
    if raw is None: return None
    if isinstance(raw, (int, float)): return int(raw)
    if not isinstance(raw, str): return None
    matches = RE_REFUND_AMOUNT.findall(raw)
    if not matches: return None
    amounts = []
    i = 0
    while i < len(matches):
        s = matches[i].replace(",", ""); i += 1
        try: amounts.append(int(s))
        except ValueError: continue
    if not amounts: return None
    return max(amounts)


# =============================================================
# 選手情報
# =============================================================
def extract_player_name(full_info):
    if not full_info or not isinstance(full_info, str): return ""
    return full_info.split("/")[0].strip()


def extract_period(full_info):
    if not full_info or not isinstance(full_info, str): return ""
    parts = full_info.split("/")
    if len(parts) >= 4: return parts[3].strip()
    return ""


def players_bike_map(rec):
    players = rec.get('players')
    out = {}
    if not isinstance(players, dict): return out
    items = list(players.items())
    i = 0
    while i < len(items):
        bs, pdata = items[i]; i += 1
        if not isinstance(pdata, dict): continue
        full = pdata.get('full_info', "")
        name = extract_player_name(full)
        if not name: continue
        period = extract_period(full)
        pkey = name + "|" + period if period else name
        out[str(bs)] = (pkey, name)
    return out


def get_full_result(rec):
    result = rec.get('result')
    if not isinstance(result, list): return []
    out = []
    i = 0
    while i < len(result):
        r = result[i]; i += 1
        if not isinstance(r, dict): continue
        rank = r.get('rank')
        try: rank = int(rank)
        except (ValueError, TypeError): continue
        if rank < 1 or rank > 7: continue
        bike = r.get('bike')
        if bike is None: continue
        out.append((rank, str(bike)))
    return out


def new_role_rank_matrix():
    m = []; i = 0
    while i < N_ROLES:
        m.append([0]*N_RANKS); i += 1
    return m


def new_role_starts():
    return [0]*N_ROLES


# =============================================================
# メイン
# =============================================================
def main():
    if not os.path.exists(DB_PATH):
        print("[ERROR] DB not found: " + DB_PATH); return

    print("=" * 70)
    print("  build_ana_score_by_venue.py")
    print("  会場別 穴特性スコア生成")
    print("=" * 70)
    print("  DB: " + DB_PATH)
    print("")

    # (place, pkey) -> 各種カウンタ
    venue_starts        = defaultdict(int)         # 全期間出走数
    venue_in_band_starts= defaultdict(int)         # 穴帯出走数
    venue_rank_counts   = defaultdict(lambda: [0]*N_RANKS)   # 全期間着順
    venue_in_band_rank  = defaultdict(lambda: [0]*N_RANKS)   # 穴帯着順
    venue_role_starts   = defaultdict(new_role_starts)        # 全期間役割別出走
    venue_role_rank     = defaultdict(new_role_rank_matrix)   # 全期間役割×着順
    venue_in_band_role_starts = defaultdict(new_role_starts)
    venue_in_band_role_rank   = defaultdict(new_role_rank_matrix)

    # global (pkey単独・会場区別なし) -> カウンタ
    g_starts         = defaultdict(int)               # 全期間出走数
    g_in_band_starts = defaultdict(int)               # 穴帯出走数
    g_rank_counts    = defaultdict(lambda: [0]*N_RANKS)   # 全期間着順
    g_in_band_rank   = defaultdict(lambda: [0]*N_RANKS)   # 穴帯着順

    pkey_to_name = {}
    venue_set = set()

    total = 0; eligible = 0
    in_band_n = 0; out_band_n = 0

    f = open(DB_PATH, "r", encoding="utf-8")
    while True:
        line = f.readline()
        if not line: break
        line = line.strip()
        if not line: continue
        try: rec = json.loads(line)
        except: continue
        total += 1
        if total % 10000 == 0:
            print("  ... " + str(total) + " records")

        place = rec.get('place')
        line_str = rec.get('line')
        if not place or not line_str: continue

        chunks = parse_line_chunks(line_str)
        if chunks is None: continue
        sizes = chunks_to_sizes(chunks)
        n_total = total_in_chunks(chunks)
        if n_total != N_CARS_REQUIRED: continue
        if is_all_solo(sizes): continue

        bike_role = build_bike_role_map(chunks)
        bike_pmap = players_bike_map(rec)
        if not bike_pmap or len(bike_pmap) != N_CARS_REQUIRED: continue

        eligible += 1
        venue_set.add(place)

        # 全期間: 出走 + 役割
        for bs, info in bike_pmap.items():
            pkey, pname = info
            venue_starts[(place, pkey)] += 1
            g_starts[pkey] += 1
            role_idx = bike_role.get(bs)
            if role_idx is not None:
                venue_role_starts[(place, pkey)][role_idx] += 1
            if pkey not in pkey_to_name:
                pkey_to_name[pkey] = pname

        # 全期間: 着順
        results = get_full_result(rec)
        for rank, bike_str in results:
            info = bike_pmap.get(bike_str)
            if info is None: continue
            pkey, _ = info
            venue_rank_counts[(place, pkey)][rank-1] += 1
            g_rank_counts[pkey][rank-1] += 1
            role_idx = bike_role.get(bike_str)
            if role_idx is not None:
                venue_role_rank[(place, pkey)][role_idx][rank-1] += 1

        # 帯判定
        refund = parse_refund_3t(rec)
        if refund is None: continue
        if not in_band_range(refund):
            out_band_n += 1; continue
        in_band_n += 1

        # 穴帯: 出走 + 役割
        for bs, info in bike_pmap.items():
            pkey, _ = info
            venue_in_band_starts[(place, pkey)] += 1
            g_in_band_starts[pkey] += 1
            role_idx = bike_role.get(bs)
            if role_idx is not None:
                venue_in_band_role_starts[(place, pkey)][role_idx] += 1

        # 穴帯: 着順
        for rank, bike_str in results:
            info = bike_pmap.get(bike_str)
            if info is None: continue
            pkey, _ = info
            venue_in_band_rank[(place, pkey)][rank-1] += 1
            g_in_band_rank[pkey][rank-1] += 1
            role_idx = bike_role.get(bike_str)
            if role_idx is not None:
                venue_in_band_role_rank[(place, pkey)][role_idx][rank-1] += 1
    f.close()

    print("")
    print("  読み込み完了:")
    print("    総レコード      : " + str(total))
    print("    集計対象レース  : " + str(eligible))
    print("    穴帯内          : " + str(in_band_n))
    print("    穴帯外          : " + str(out_band_n))
    print("    ユニーク会場    : " + str(len(venue_set)))
    print("    ユニーク(会場,選手): " + str(len(venue_starts)))
    print("")

    # =============================================================
    # 各 (会場,選手) ペアでスコア計算
    # =============================================================
    rows = []
    for (place, pkey), v_starts in venue_starts.items():
        if v_starts <= 0: continue
        rcs = venue_rank_counts.get((place, pkey), [0]*N_RANKS)
        v_total_hit3 = rcs[0] + rcs[1] + rcs[2]
        v_total_rate = float(v_total_hit3) / v_starts

        ib_starts = venue_in_band_starts.get((place, pkey), 0)
        if ib_starts <= 0: continue
        ib_rcs = venue_in_band_rank.get((place, pkey), [0]*N_RANKS)
        ib_hit3 = ib_rcs[0] + ib_rcs[1] + ib_rcs[2]
        ib_rate = float(ib_hit3) / ib_starts
        score = ib_rate - v_total_rate

        rs_total = venue_role_starts.get((place, pkey), new_role_starts())
        rr_total = venue_role_rank.get((place, pkey), new_role_rank_matrix())
        rs_in_band = venue_in_band_role_starts.get((place, pkey), new_role_starts())
        rr_in_band = venue_in_band_role_rank.get((place, pkey), new_role_rank_matrix())

        # 主要役割
        roles_pairs = []
        i = 0
        while i < N_ROLES:
            roles_pairs.append((ROLE_NAMES[i], rs_total[i])); i += 1
        roles_pairs.sort(key=lambda x: -x[1])
        primary_role = roles_pairs[0][0] if roles_pairs[0][1] > 0 else "unknown"

        row = {
            "place": place,
            "player_key": pkey,
            "player_name": pkey_to_name.get(pkey, ""),
            "venue_ana_score": round(score, 4),
            "venue_in_band_hit3_rate": round(ib_rate, 4),
            "venue_hit3_rate_total": round(v_total_rate, 4),
            "venue_in_band_starts": ib_starts,
            "venue_in_band_hit3_count": ib_hit3,
            "venue_in_band_rank1_count": ib_rcs[0],
            "venue_in_band_rank2_count": ib_rcs[1],
            "venue_in_band_rank3_count": ib_rcs[2],
            "venue_total_starts": v_starts,
            "venue_total_hit3_count": v_total_hit3,
            "primary_role": primary_role,
        }
        # 全期間 役割×着順 + 役割出走数
        ri = 0
        while ri < N_ROLES:
            rname = ROLE_NAMES[ri]
            row[rname + "_starts"] = rs_total[ri]
            rk = 0
            while rk < N_RANKS:
                row[rname + "_r" + str(rk+1)] = rr_total[ri][rk]
                rk += 1
            # 穴帯のみ役割×着順 (in_band_lead_r1 など)
            row["in_band_" + rname + "_starts"] = rs_in_band[ri]
            rk = 0
            while rk < N_RANKS:
                row["in_band_" + rname + "_r" + str(rk+1)] = rr_in_band[ri][rk]
                rk += 1
            ri += 1
        rows.append(row)

    print("  集計済みペア数: " + str(len(rows)))
    print("")

    # =============================================================
    # 3ファイル出力
    # =============================================================
    summary_lines = []
    summary_lines.append("会場別 穴特性スコア サマリ")
    summary_lines.append("=" * 60)
    summary_lines.append("総レコード         : " + str(total))
    summary_lines.append("集計対象レース     : " + str(eligible))
    summary_lines.append("穴帯内             : " + str(in_band_n))
    summary_lines.append("ユニーク会場       : " + str(len(venue_set)))
    summary_lines.append("ユニーク(会場,選手): " + str(len(rows)))
    summary_lines.append("")

    # =============================================================
    # venue出力 (VENUE_MIN以上を単一FINALに)
    # =============================================================
    v_filtered = [dict(r) for r in rows if r["venue_in_band_starts"] >= VENUE_MIN]
    v_filtered.sort(key=lambda x: -x["venue_ana_score"])
    rank = 0
    for r in v_filtered:
        rank += 1
        r["score_rank"] = rank
    out = open(OUT_VENUE, "w", encoding="utf-8")
    for r in v_filtered:
        out.write(json.dumps(r, ensure_ascii=False) + "\n")
    out.close()
    line = ("venue 穴出走 >= " + str(VENUE_MIN) + " : " + str(len(v_filtered))
            + " 行 -> " + os.path.basename(OUT_VENUE))
    print("  " + line)
    summary_lines.append(line)

    # =============================================================
    # global出力 (会場区別なし・GLOBAL_MIN以上を単一FINALに)
    # =============================================================
    g_rows = []
    for pkey, gs in g_starts.items():
        if gs <= 0: continue
        grcs = g_rank_counts.get(pkey, [0]*N_RANKS)
        g_total_hit3 = grcs[0] + grcs[1] + grcs[2]
        g_total_rate = float(g_total_hit3) / gs
        gib = g_in_band_starts.get(pkey, 0)
        if gib <= 0: continue
        gib_rcs = g_in_band_rank.get(pkey, [0]*N_RANKS)
        gib_hit3 = gib_rcs[0] + gib_rcs[1] + gib_rcs[2]
        gib_rate = float(gib_hit3) / gib
        gscore = gib_rate - g_total_rate
        g_rows.append({
            "player_key": pkey,
            "player_name": pkey_to_name.get(pkey, ""),
            "ana_score": round(gscore, 4),
            "in_band_hit3_rate": round(gib_rate, 4),
            "hit3_rate_total": round(g_total_rate, 4),
            "in_band_starts": gib,
            "in_band_hit3_count": gib_hit3,
            "total_starts": gs,
            "total_hit3_count": g_total_hit3,
        })
    g_filtered = [dict(r) for r in g_rows if r["in_band_starts"] >= GLOBAL_MIN]
    g_filtered.sort(key=lambda x: -x["ana_score"])
    rank = 0
    for r in g_filtered:
        rank += 1
        r["score_rank"] = rank
    out = open(OUT_GLOBAL, "w", encoding="utf-8")
    for r in g_filtered:
        out.write(json.dumps(r, ensure_ascii=False) + "\n")
    out.close()
    line = ("global 穴出走 >= " + str(GLOBAL_MIN) + " : " + str(len(g_filtered))
            + " 行 -> " + os.path.basename(OUT_GLOBAL))
    print("  " + line)
    summary_lines.append(line)

    _unused_output_paths = []
    for thr, path in _unused_output_paths:
        filtered = [dict(r) for r in rows if r["venue_in_band_starts"] >= thr]
        # スコア降順
        filtered.sort(key=lambda x: -x["venue_ana_score"])
        rank = 0
        for r in filtered:
            rank += 1
            r["score_rank"] = rank
        # 書き出し
        out = open(path, "w", encoding="utf-8")
        for r in filtered:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
        out.close()

        # 統計
        if filtered:
            scores = [r["venue_ana_score"] for r in filtered]
            scores_sorted = sorted(scores)
            n = len(scores)
            mean_s = sum(scores) / n
            median_s = scores_sorted[n // 2]
            min_s = scores_sorted[0]
            max_s = scores_sorted[-1]
            plus_c = sum(1 for s in scores if s > 0)
            minus_c = sum(1 for s in scores if s < 0)
        else:
            mean_s = median_s = min_s = max_s = 0
            plus_c = minus_c = 0

        line = ("会場×選手 穴出走 >= " + str(thr) + " : "
                + str(len(filtered)) + " 行 -> " + os.path.basename(path))
        print("  " + line)
        summary_lines.append(line)
        summary_lines.append("    プラス: " + str(plus_c) + " / マイナス: " + str(minus_c))
        summary_lines.append("    スコア: 平均={:.4f} 中央={:.4f} 最小={:.4f} 最大={:.4f}".format(
            mean_s, median_s, min_s, max_s))

        if filtered:
            summary_lines.append("    [会場別 穴特化型 TOP10]")
            i = 0
            top_n = 10 if len(filtered) >= 10 else len(filtered)
            while i < top_n:
                r = filtered[i]; i += 1
                summary_lines.append("      {0:3d}. {1:6s} {2:14s} score={3:+.4f}  穴={4}/{5}={6:.2%}  全={7}/{8}={9:.2%}  役={10}".format(
                    r["score_rank"], r["place"], r["player_name"], r["venue_ana_score"],
                    r["venue_in_band_hit3_count"], r["venue_in_band_starts"], r["venue_in_band_hit3_rate"],
                    r["venue_total_hit3_count"], r["venue_total_starts"], r["venue_hit3_rate_total"],
                    r["primary_role"]))

            summary_lines.append("    [会場別 人気サイド型 BOTTOM10]")
            n_total_f = len(filtered)
            bot_n = 10 if n_total_f >= 10 else n_total_f
            i = n_total_f - bot_n
            while i < n_total_f:
                r = filtered[i]; i += 1
                summary_lines.append("      {0:4d}. {1:6s} {2:14s} score={3:+.4f}  穴={4}/{5}={6:.2%}  全={7}/{8}={9:.2%}  役={10}".format(
                    r["score_rank"], r["place"], r["player_name"], r["venue_ana_score"],
                    r["venue_in_band_hit3_count"], r["venue_in_band_starts"], r["venue_in_band_hit3_rate"],
                    r["venue_total_hit3_count"], r["venue_total_starts"], r["venue_hit3_rate_total"],
                    r["primary_role"]))
        summary_lines.append("")

    sf = open(SUMMARY, "w", encoding="utf-8")
    sf.write("\n".join(summary_lines))
    sf.close()

    print("")
    i = 0
    while i < len(summary_lines):
        print("  " + summary_lines[i]); i += 1

    print("")
    print("=" * 70)
    print("  完了")
    print("=" * 70)
    print("  venue : " + OUT_VENUE)
    print("  global: " + OUT_GLOBAL)


if __name__ == "__main__":
    main()

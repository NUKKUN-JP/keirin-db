# -*- coding: utf-8 -*-
"""
build_picks.py -- その日の買い目を確定させる (第1段/Python)

GitHub Actions で毎日走らせる。2段構成の1段目。
  build_picks.py  payload生成 + ★計算 + 条件判定 -> _picks_stage1.json
  run_picks.js    oracle_core.js で買い目生成    -> picks/YYYYMMDD.json

アプリはできあがった picks/YYYYMMDD.json を読むだけ。
★の計算も条件判定も買い目生成も行わないので、
端末の辞書に依存せず、誰が起動しても同じ結果になる。

【v2の変更: 全レースを対象にする】
これまでは3条件に該当したレースだけを出していた。
見送りをなくし、すべてのレースで買い目を見せる。

  狙いレース  … 3条件(終幕/極星/拮抗)に該当したもの。従来どおり
  それ以外    … 検証Aの絞り込み6段階

【絞り込みの段階】
検証A/B/Cは和集合なので「1点」と指定しても実際の買い目は2目になる。
点数指定をやめ、段階に名前を付ける。括弧内は実測の中央値。
  断罪(約2点) 峻別(約3点) 選定(約5点)
  目星(約6点) 網掛(約8点) 総覧(約9点)
12,125レースで測ったところ、設定1点→実際1〜2点、
設定6点→実際6〜13点だった。

【なぜ2段か】
検証A/B/Cの和集合ロジックは oracle_core.js にあり、Python側には無い。
実測で確認した対応は
  検証A = __btUnionRace(payload, tri, refund, [0,1,2])
  検証B = __btUnionRace(payload, tri, refund, [0,1,2,3])
  検証C = __btTavernRace(payload, tri, refund, [0,1,2,3])   ←関数が違う
買い目は byN[N-1].combos。

【payloadの作り方】
app_singlev329.py を import して build_race_payload を呼ぶ。
この関数は464行あり、グローバル3つと自作関数13個に依存するため、
切り出すより import する方が安全。
app.run() は __name__ ガードの中なので、import してもサーバは起動しない。

制約: f-string 禁止 / for-else 禁止
"""
import os
import re
import sys
import json
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STAR_MODEL = "arare_star_12m.json"
CONDITIONS = "conditions_v2.json"
OUT_DIR = os.path.join(HERE, "picks")

# 置き場所を選ばないように、いくつかの候補を順に探す。
# Actions ではリポジトリ直下に全部並ぶので HERE だけで足りるが、
# 手元での試しではフォルダが分かれていることがある。
SEARCH_DIRS = [
    HERE,
    os.getcwd(),
    "/storage/emulated/0/Download",
    "/storage/emulated/0/Download/takusen",
    "/storage/emulated/0/Download/takusen/code",
    "/storage/emulated/0/Download/takusen/data",
]


def find_near(name):
    """name を候補フォルダから探す。見つかったパスを返す。"""
    i = 0
    while i < len(SEARCH_DIRS):
        d = SEARCH_DIRS[i]
        i = i + 1
        if not d:
            continue
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return ""

FL_BOUNDS = [9.3637, 9.1749, 8.9724, 8.6605]
SERIES_KEY = {"vfa": "検証A", "vfb": "検証B", "vfc": "検証C"}

# 絞り込みの段階。設定点数と、実測から出した目安の点数。
#   12,125レースで測った中央値を approx にしている。
STEPS = [
    {"id": "danzai", "name": "断罪", "pts": 1, "approx": 2},
    {"id": "shunbetsu", "name": "峻別", "pts": 2, "approx": 3},
    {"id": "sentei", "name": "選定", "pts": 3, "approx": 5},
    {"id": "meboshi", "name": "目星", "pts": 4, "approx": 6},
    {"id": "amikake", "name": "網掛", "pts": 5, "approx": 8},
    {"id": "souran", "name": "総覧", "pts": 6, "approx": 9},
]
STEP_SERIES = "vfa"   # 段階は検証Aで作る


# ------------------------------------------------------------
# 素性
# ------------------------------------------------------------
def extract_points(full_info):
    if not full_info or full_info == "未取得":
        return None
    m = re.search(r'([\d.]+)点$', full_info)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def extract_finishes(h):
    if not h or h == "なし" or not isinstance(h, str):
        return []
    t = h.strip().split()
    if not t:
        return []
    out = []
    for x in re.split(r'[・.]', t[-1]):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(int(x))
        except Exception:
            pass
    return out


def extract_grade_h(h):
    if not h or h == "なし" or not isinstance(h, str):
        return ""
    m = re.search(r'(GP|G1|G2|G3|F1|F2)', h)
    if m:
        return m.group(1)
    s2 = h
    for a, b in (("Ｇ", "G"), ("Ｆ", "F"), ("１", "1"), ("２", "2"), ("３", "3")):
        s2 = s2.replace(a, b)
    m = re.search(r'(GP|G1|G2|G3|F1|F2)', s2)
    if m:
        return m.group(1)
    return ""


def q_of(rec):
    """拮抗度Q。集計・検証で使ったものと同じ式。"""
    players = rec.get("players") or {}
    vals = []
    for bk in players:
        p = players[bk]
        if not isinstance(p, dict):
            return ""
        pt = extract_points(p.get("full_info", ""))
        if pt is None or pt == 0.0:
            return ""
        ranks = []
        for key in ("h1", "h2", "h3"):
            for r in extract_finishes(p.get(key, "")):
                rr = 7 if r >= 8 else r
                if rr >= 1:
                    ranks.append(rr)
        if not ranks:
            return ""
        avg = float(sum(ranks)) / float(len(ranks))
        g = extract_grade_h(p.get("h2", ""))
        gb = 0
        if g in ("GP", "G1", "G2", "G3"):
            gb = 5
        elif g == "F1":
            gb = 3
        elif g == "F2":
            gb = 1
        vals.append(pt - avg * 5.0 + gb)
    if len(vals) < 3:
        return ""
    mx = max(vals)
    if mx <= 0:
        return ""
    t = 0.0
    for v in vals:
        t = t + v / mx * 10.0
    a = t / len(vals)
    q = 1
    for b in FL_BOUNDS:
        if a >= b:
            return "Q" + str(q)
        q = q + 1
    return "Q5"


def parse_chunks(line_str):
    if not line_str or not isinstance(line_str, str):
        return None
    s = line_str.replace("ー", "-").replace("−", "-").replace("―", "-")
    if "-" not in s and (" " in s or "\u3000" in s):
        s = "-".join(s.replace("\u3000", " ").split())
    out = []
    for p in s.split("-"):
        d = ""
        for ch in p:
            if ch.isdigit():
                d = d + ch
        if d:
            out.append(d)
    if not out:
        return None
    return out


def line_config(chunks):
    ns = []
    i = 0
    while i < len(chunks):
        ns.append(len(chunks[i]))
        i = i + 1
    ns.sort(reverse=True)
    out = []
    j = 0
    while j < len(ns):
        out.append(str(ns[j]))
        j = j + 1
    return "-".join(out)


def class_rank(rk, grade):
    if not rk or not isinstance(rk, str):
        rk = ""
    if "チャレンジ" in rk:
        return "A級"
    if "Ｓ級" in rk or "S級" in rk:
        return "S級"
    if "Ａ級" in rk or "A級" in rk:
        return "A級"
    g = (grade or "").strip()
    if g in ("GP", "G1", "G2", "G3"):
        return "S級"
    if g == "F2":
        return "A級"
    return "不明"


def race_type(rk):
    if not rk or not isinstance(rk, str):
        return "不明"
    if "チャレンジ" in rk:
        return "チャレンジ"
    order = [("初日特別選抜", "初特選"), ("初日特選", "初特選"),
             ("初特選", "初特選"),
             ("特別選抜予選", "特予選"), ("特予選", "特予選"),
             ("一次予選", "一次予選"), ("一予選", "一次予選"),
             ("二次予選", "二次予選"), ("二予選", "二次予選"),
             ("特別優秀", "特秀"), ("特秀", "特秀"),
             ("特別選抜", "特選"), ("特選", "特選"),
             ("特別一般", "特一般"), ("特一般", "特一般"),
             ("準決勝", "準決勝"), ("決勝", "決勝"), ("選抜", "選抜"),
             ("優秀", "優秀"), ("予選", "予選"), ("一般", "一般")]
    i = 0
    while i < len(order):
        kw, nm = order[i]
        i = i + 1
        if kw in rk:
            return nm
    return "不明"


def build_feats(rec):
    """★と条件判定に必要な素性。取れなければ None。"""
    ch = parse_chunks(rec.get("line", ""))
    if ch is None:
        return None
    q = q_of(rec)
    if not q:
        q = "不明"
    gr = (rec.get("grade") or "").strip() or "欠損"
    nsolo = 0
    mx = 0
    ci = 0
    while ci < len(ch):
        if len(ch[ci]) == 1:
            nsolo = nsolo + 1
        if len(ch[ci]) > mx:
            mx = len(ch[ci])
        ci = ci + 1
    nl = len(ch)
    return {
        "拮抗度Q": q,
        "ライン構成": line_config(ch),
        "分戦": (str(nl) + "分戦") if nl <= 4 else "細切戦",
        "単騎数": (str(nsolo) + "人") if nsolo <= 2 else "3人以上",
        "最長ライン": (str(mx) + "人") if mx <= 4 else "5人以上",
        "級班": class_rank(rec.get("race_kind"), gr),
        "グレード": gr,
        "会場": str(rec.get("place", "")),
        "種別": race_type(rec.get("race_kind")),
        "n_line": nl,
    }


# ------------------------------------------------------------
# ★
# ------------------------------------------------------------
def load_star_model():
    p = find_near(STAR_MODEL)
    if not p:
        return None
    f = open(p, "r", encoding="utf-8")
    try:
        return json.load(f)
    finally:
        f.close()


def star_of(model, ft):
    """予測配当を出し、cutsと比べて★1〜5を返す。取れなければ0。"""
    if not model:
        return 0, 0.0
    ws = model.get("weights") or {}
    tb = model.get("tables") or {}
    num = 0.0
    den = 0.0
    for nm in ws:
        wv = ws[nm]
        if not wv or wv <= 0:
            continue
        t = tb.get(nm)
        if not t:
            continue
        v = t.get(str(ft.get(nm, "")))
        if v is None:
            continue
        num = num + float(v) * float(wv)
        den = den + float(wv)
    if den <= 0:
        return 0, 0.0
    pv = num / den
    cuts = model.get("cuts") or []
    s = 1
    ci = 0
    while ci < len(cuts):
        if pv >= cuts[ci]:
            s = s + 1
        ci = ci + 1
    return s, pv


# ------------------------------------------------------------
# 条件
# ------------------------------------------------------------
def load_conditions():
    p = find_near(CONDITIONS)
    if not p:
        return None
    f = open(p, "r", encoding="utf-8")
    try:
        return json.load(f)
    finally:
        f.close()


def cond_match(cond, ft, star):
    """条件に合うか。指定された項目がすべて合えば True。"""
    if not cond:
        return True
    if "race_type" in cond:
        if ft.get("種別") not in cond["race_type"]:
            return False
    if "q" in cond:
        if ft.get("拮抗度Q") not in cond["q"]:
            return False
    if "star" in cond:
        if star not in cond["star"]:
            return False
    if "line_config" in cond:
        if ft.get("ライン構成") not in cond["line_config"]:
            return False
    if "class_rank" in cond:
        if ft.get("級班") not in cond["class_rank"]:
            return False
    return True


def main():
    date_str = ""
    if len(sys.argv) > 1:
        date_str = sys.argv[1].strip().replace("-", "")
    if not date_str:
        date_str = datetime.datetime.now().strftime("%Y%m%d")
    print("=== 買い目の下ごしらえ " + date_str + " ===")

    model = load_star_model()
    if not model:
        print("[error] " + STAR_MODEL + " が見つかりません")
        i = 0
        while i < len(SEARCH_DIRS):
            print("  探した場所: " + str(SEARCH_DIRS[i]))
            i = i + 1
        return 1
    meta = model.get("meta") or {}
    print("★モデル: 学習 " + str(meta.get("learn_from", "?"))
          + "-" + str(meta.get("learn_to", "?"))
          + "  " + str(meta.get("n_learn", "?")) + "R")

    cond = load_conditions()
    if not cond:
        print("[error] " + CONDITIONS + " が見つかりません")
        i = 0
        while i < len(SEARCH_DIRS):
            print("  探した場所: " + str(SEARCH_DIRS[i]))
            i = i + 1
        return 1
    strategies = cond.get("strategies") or []
    print("条件 " + str(len(strategies)) + "件 (" + str(cond.get("updated", "")) + ")")

    # アプリをライブラリとして使う。app.run() は __name__ ガードの中なので
    # import してもサーバは起動しない。
    #   置き場所を選ばないよう、候補フォルダを import パスに足す。
    #   アプリ本体の名前が違う場合も順に試す。
    si2 = 0
    while si2 < len(SEARCH_DIRS):
        d2 = SEARCH_DIRS[si2]
        si2 = si2 + 1
        if d2 and os.path.isdir(d2) and d2 not in sys.path:
            sys.path.insert(0, d2)

    APP = None
    tried = []
    names = ["app_singlev329", "app_single", "app"]
    # 候補フォルダにある app*.py も名前として拾う
    si2 = 0
    while si2 < len(SEARCH_DIRS):
        d2 = SEARCH_DIRS[si2]
        si2 = si2 + 1
        if not d2 or not os.path.isdir(d2):
            continue
        try:
            for fn2 in sorted(os.listdir(d2)):
                if fn2.startswith("app_single") and fn2.endswith(".py"):
                    nm2 = fn2[:-3]
                    if nm2 not in names:
                        names.append(nm2)
        except Exception:
            pass
    ni = 0
    while ni < len(names):
        nm2 = names[ni]
        ni = ni + 1
        try:
            APP = __import__(nm2)
            print("アプリ: " + nm2)
            break
        except Exception as e:
            tried.append(nm2 + " -> " + str(e)[:70])
    if APP is None:
        print("[error] アプリ本体を読めません")
        ti = 0
        while ti < len(tried):
            print("  " + tried[ti])
            ti = ti + 1
        print("  探したフォルダ:")
        si2 = 0
        while si2 < len(SEARCH_DIRS):
            print("    " + str(SEARCH_DIRS[si2]))
            si2 = si2 + 1
        return 1

    try:
        _lr = APP.load_races(date_str)
        # load_races は環境により (races, rmap) を返す場合がある
        if isinstance(_lr, tuple):
            races = _lr[0]
        else:
            races = _lr
    except Exception as e:
        print("[error] load_races 失敗: " + str(e)[:200])
        return 1
    # load_races が見つけられないときは、自分で出走表を探して読む。
    #   アプリ側は決まった場所しか見ないので、置き場所が違うと空になる。
    if not races:
        cand = []
        si3 = 0
        while si3 < len(SEARCH_DIRS):
            d3 = SEARCH_DIRS[si3]
            si3 = si3 + 1
            if not d3:
                continue
            cand.append(os.path.join(d3, "today_cache",
                                     "races_" + date_str + ".json"))
            cand.append(os.path.join(d3, "races_" + date_str + ".json"))
        cand.append(os.path.join("/storage/emulated/0/Download/takusen/data",
                                 "today_cache", "races_" + date_str + ".json"))
        found = ""
        ci3 = 0
        while ci3 < len(cand):
            if os.path.exists(cand[ci3]):
                found = cand[ci3]
                break
            ci3 = ci3 + 1
        if found:
            try:
                f3 = open(found, "r", encoding="utf-8")
                try:
                    races = json.load(f3)
                finally:
                    f3.close()
                print("出走表を直接読み込み: " + found)
            except Exception as e:
                print("[error] 出走表を読めません: " + str(e)[:120])
                return 1
        else:
            print("[error] " + date_str + " のレースがありません")
            print("  races_" + date_str + ".json を探した場所:")
            ci3 = 0
            while ci3 < len(cand):
                print("    " + cand[ci3])
                ci3 = ci3 + 1
            return 1
    print("レース " + str(len(races)) + "R")

    try:
        d = APP.get_dicts()
    except Exception as e:
        print("[error] get_dicts 失敗: " + str(e)[:200])
        return 1

    out = []
    skips = []      # 買い目を作れなかったレースと、その理由
    n_skip = 0
    n_nocond = 0
    n_nopay = 0
    i = 0
    while i < len(races):
        rec = races[i]
        i = i + 1
        rkey = APP.race_key(rec)
        pl = rec.get("players") or {}
        if len(pl) != 7:
            skips.append({"key": rkey, "reason": str(len(pl)) + "車立て"})
            n_skip = n_skip + 1
            continue
        rk2 = rec.get("race_kind")
        if isinstance(rk2, str) and ("ガールズ" in rk2 or "Ｌ級" in rk2):
            skips.append({"key": rkey, "reason": "ガールズ"})
            n_skip = n_skip + 1
            continue
        ft = build_feats(rec)
        if ft is None:
            skips.append({"key": rkey, "reason": "ライン情報なし"})
            n_skip = n_skip + 1
            continue
        if ft["n_line"] <= 1:
            skips.append({"key": rkey, "reason": "個人戦"})
            n_skip = n_skip + 1
            continue
        star, pv = star_of(model, ft)

        # 3条件に該当するか (該当すれば「狙いレース」)
        hits = []
        si = 0
        while si < len(strategies):
            st = strategies[si]
            si = si + 1
            if cond_match(st.get("cond"), ft, star):
                hits.append(st)

        try:
            payload = APP.build_race_payload(rec, d["venue_home_dir"],
                                             d["bank_data"])
        except Exception as e:
            skips.append({"key": rkey, "reason": "計算できず"})
            n_nopay = n_nopay + 1
            continue
        if not payload or payload.get("status") != "ok":
            why = "計算できず"
            det2 = str((payload or {}).get("detail", ""))
            rsn = str((payload or {}).get("reason", ""))
            if "raw_score" in det2:
                why = "出走表が未確定"
            elif rsn == "no_line":
                why = "ライン情報なし"
            elif rsn == "kojinsen":
                why = "個人戦"
            skips.append({"key": rkey, "reason": why})
            n_nopay = n_nopay + 1
            continue

        # 狙いレースの買い目 (3条件)
        plan = []
        hj = 0
        while hj < len(hits):
            st = hits[hj]
            hj = hj + 1
            plan.append({"id": st.get("id"), "label": st.get("label"),
                         "mark": st.get("mark", ""),
                         "series": st.get("series"),
                         "points": int(st.get("points", 1))})

        # 絞り込み6段階 (全レース共通・検証A)
        steps = []
        sj = 0
        while sj < len(STEPS):
            stp = STEPS[sj]
            sj = sj + 1
            steps.append({"id": stp["id"], "name": stp["name"],
                          "approx": stp["approx"],
                          "series": STEP_SERIES, "points": stp["pts"]})

        out.append({
            "key": APP.race_key(rec),
            "venue": rec.get("place", ""),
            "race_no": rec.get("race_no", ""),
            "post_time": rec.get("post_time", ""),
            "star": star,
            "pred_pay": int(pv),
            "q": ft["拮抗度Q"],
            "line_config": ft["ライン構成"],
            "race_type": ft["種別"],
            "line": str(rec.get("line", "") or ""),
            "target": (len(plan) > 0),
            "plan": plan,
            "steps": steps,
            "payload": payload,
        })

    body = {"date": date_str,
            "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "star_model": meta,
            "conditions_updated": cond.get("updated", ""),
            "races": out,
            "skips": skips}
    # 出力先。Pydroid3の内部領域(/data/user/0/...)に出すと
    # Termux の node から読めないので、共有フォルダを優先する。
    # Actions ではリポジトリ直下(HERE)に出す。
    opath = ""
    for _d in ["/storage/emulated/0/Download", HERE, os.getcwd()]:
        if not _d or not os.path.isdir(_d):
            continue
        _p = os.path.join(_d, "_picks_stage1.json")
        try:
            _t = open(_p, "w", encoding="utf-8")
            _t.close()
            opath = _p
            break
        except Exception:
            continue
    if not opath:
        opath = os.path.join(HERE, "_picks_stage1.json")
    g = open(opath, "w", encoding="utf-8")
    try:
        json.dump(body, g, ensure_ascii=False)
    finally:
        g.close()

    n_target = 0
    j = 0
    while j < len(out):
        if out[j]["target"]:
            n_target = n_target + 1
        j = j + 1
    print("")
    print("対象レース " + str(len(out)) + "R  (うち狙いレース "
          + str(n_target) + "R)")
    print("  7車立てでない等 " + str(n_skip) + "R")
    print("  予想が作れない   " + str(n_nopay) + "R")
    j = 0
    while j < len(out) and j < 8:
        r = out[j]
        j = j + 1
        lb = []
        pj = 0
        while pj < len(r["plan"]):
            lb.append(str(r["plan"][pj]["label"]))
            pj = pj + 1
        print("  " + str(r["venue"]) + str(r["race_no"]) + "R  "
              + ("★" * r["star"]).ljust(5) + " "
              + (" · ".join(lb) if lb else "(段階のみ)"))
    if len(out) > 8:
        print("  ... 他 " + str(len(out) - 8) + "R")
    print("")
    print("出力: " + opath)
    print("次: node run_picks.js " + date_str)
    return 0


if __name__ == "__main__":
    sys.exit(main())

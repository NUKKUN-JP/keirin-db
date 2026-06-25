# -*- coding: utf-8 -*-
"""
build_cathedral_cache_v1.py  (GitHub Actions / ローカル両用)

新ロジック(大聖堂)の事前計算。月別DB keirin_months/keirin_YYYYMM.jsonl の
各レースについて、predict_cathedral.predict_all_raw() でフィルタ前の全候補
(生weight)を計算し、結果(着順/払戻)と的中可否を添えて日別JSONに出力する。

アプリは cathedral_cache/cathedral_YYYYMMDD.json を読み、モード(maria/
priest/bishop)に応じてJS側で絞り込み・norm_pct再計算する。これにより
スマホ側の重い同期予測が不要になる(起動後の託宣ボタンのダウン対策)。

出力: cathedral_cache/cathedral_YYYYMMDD.json
  {
    "date": "20260601",
    "races": {
      "<race_key>": {
        "place": "宇都宮", "race_no": 1,
        "line": "156-23-47", "weather": "...",
        "all": [{"3t":"1-4-6","weight":0.123}, ...],   # weight降順 cap_n件
        "result_3t": "1-4-6",      # 確定3連単(なければ "")
        "refund_3t": 12340,        # 払戻(円, なければ 0)
        "ok": true, "reason": "ok"
      }, ...
    },
    "meta": {"n_races": 123, "n_ok": 120, "generated": "..."}
  }

環境変数:
  CATHEDRAL_START  YYYYMMDD  対象開始日(空=月の頭)
  CATHEDRAL_END    YYYYMMDD  対象終了日(空=月の末)
  CATHEDRAL_MONTH  YYYYMM    対象月(指定時はその月だけ。空=START/ENDから判定)
  CATHEDRAL_CAP_N  整数      1レースあたり保存する候補上限(既定40)
  CATHEDRAL_RAWSCORE  "db"|"none"  raw_score補正の出所(既定 db)

raw_scoreについて:
  Actions環境では predict_for_race(当日計算rs_map) は predict_today_v31 と
  多数の辞書に依存し重い。バッチでは DBの players[bs].raw_score を使う(db)。
  ※ DB raw_score と アプリのrs_map が同一ロジックかは check_rawscore_
    consistency.py で要確認。別物なら将来 rs_map 基準で再生成を検討。
"""
import json
import os
import re
import sys
from datetime import datetime

import predict_cathedral as pc


# ------------------------------------------------------------
# パス解決 (リポジトリルート / takusen配下 両対応)
# ------------------------------------------------------------
def _first_existing(cands):
    for d in cands:
        if d and os.path.exists(d):
            return d
    return None


MONTHS_DIR = _first_existing([
    "keirin_months",
    os.path.join("takusen", "data", "keirin_months"),
    os.path.join("takusen", "keirin_months"),
])
DICTS_DIR = _first_existing([
    "dicts",
    os.path.join("takusen", "data", "dicts"),
    os.path.join("takusen", "data"),
    os.path.join("takusen", "dicts"),
])
OUT_DIR = "cathedral_cache"


def _env(name, default=""):
    v = os.environ.get(name, "")
    if v is None:
        return default
    v = v.strip()
    return v if v else default


# ------------------------------------------------------------
# 結果パース (app の _parse_refund / _normalize_result を移植)
# ------------------------------------------------------------
def _parse_refund(refund_raw):
    if not refund_raw or not isinstance(refund_raw, str):
        return ("", 0)
    m = re.match(r'^([\d\-]+)\(([\d,]+)円\)', refund_raw)
    if not m:
        return ("", 0)
    combo = m.group(1)
    try:
        yen = int(m.group(2).replace(",", ""))
    except Exception:
        yen = 0
    return (combo, yen)


def _result_3t_and_refund(rec):
    """確定3連単と払戻を返す。(combo3 or '', yen)"""
    refund_3t_raw = rec.get("refund_3t", "")
    combo3, yen3 = _parse_refund(refund_3t_raw)
    if combo3:
        return combo3, yen3
    # 払戻が無ければ着順上位3から組む
    result = rec.get("result")
    if isinstance(result, list) and len(result) >= 3:
        items = []
        for r in result:
            if isinstance(r, dict) and r.get("rank") is not None:
                items.append((r.get("rank"), r.get("bike")))
        items.sort(key=lambda x: (x[0] is None, x[0]))
        if len(items) >= 3:
            return "-".join(str(b) for _, b in items[:3]), 0
    return "", 0


# ------------------------------------------------------------
# players_info 構築 (DBの players dict から)
# ------------------------------------------------------------
def _safe_int(v):
    try:
        return int(v)
    except Exception:
        return None


def _build_players_info(rec, rawscore_mode):
    players = rec.get("players", {})
    if not isinstance(players, dict):
        return None
    out = {}
    for bs in players:
        pdata = players[bs]
        if not isinstance(pdata, dict):
            continue
        bike = _safe_int(bs)
        if bike is None:
            continue
        s_val = pdata.get("s")
        rs = pdata.get("raw_score")
        if rawscore_mode == "none":
            rs_use = 0.0
        else:
            try:
                rs_use = float(rs) if rs is not None else 0.0
            except Exception:
                rs_use = 0.0
        out[bike] = {
            "s": s_val if isinstance(s_val, int) else None,
            "full_info": pdata.get("full_info", ""),
            "raw_score": rs_use,
        }
    return out


def _race_key(rec):
    return str(rec.get("place", "")) + "_" + str(rec.get("race_no", ""))


def _race_date(rec):
    """race_id の2..10文字目が YYYYMMDD。なければ date フィールド。"""
    rid = rec.get("race_id", "")
    if isinstance(rid, str) and len(rid) >= 10 and rid[2:10].isdigit():
        return rid[2:10]
    d = rec.get("date", "")
    if isinstance(d, str):
        ds = d.replace("/", "").replace("-", "")
        if len(ds) >= 8 and ds[:8].isdigit():
            return ds[:8]
    return None


# ------------------------------------------------------------
# メイン
# ------------------------------------------------------------
def _build_from_today_cache(today_json, cap_n, rawscore_mode):
    """today_cache の出走表JSON(レースのリスト)から当日分キャッシュを作る。
    当日なので結果(result_3t/refund)は空。出力は1日分。"""
    if not os.path.exists(today_json):
        print("[エラー] today_cache JSONなし:", today_json)
        sys.exit(1)
    try:
        races_list = json.load(open(today_json, encoding="utf-8"))
    except Exception as e:
        print("[エラー] today_cache 読込失敗:", str(e))
        sys.exit(1)
    if not isinstance(races_list, list):
        print("[エラー] today_cache 形式が不正(listでない)")
        sys.exit(1)

    # ファイル名 races_YYYYMMDD.json から日付を拾う
    base = os.path.basename(today_json)
    m = re.search(r'(\d{8})', base)
    ds = m.group(1) if m else datetime.now().strftime("%Y%m%d")

    races = {}
    for rec in races_list:
        if not isinstance(rec, dict):
            continue
        line_str = rec.get("line", "") or ""
        venue = rec.get("place", "") or ""
        weather = rec.get("weather", "") or ""
        players_info = _build_players_info(rec, rawscore_mode)
        entry = {
            "place": venue, "race_no": rec.get("race_no", ""),
            "line": line_str, "weather": weather,
        }
        if not players_info:
            entry["ok"] = False
            entry["reason"] = "no_players_info"
            entry["all"] = []
        else:
            res = pc.predict_all_raw(line_str, players_info, venue, weather,
                                     cap_n=cap_n)
            entry["ok"] = res.get("ok", False)
            entry["reason"] = res.get("reason", "")
            entry["all"] = res.get("all", [])
        entry["result_3t"] = ""
        entry["refund_3t"] = 0
        races[_race_key(rec)] = entry

    n = len(races)
    nok = sum(1 for v in races.values() if v.get("ok"))
    payload = {
        "date": ds, "races": races,
        "meta": {"n_races": n, "n_ok": nok, "cap_n": cap_n,
                 "rawscore": rawscore_mode, "today": True,
                 "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    }
    op = os.path.join(OUT_DIR, "cathedral_" + ds + ".json")
    with open(op, "w", encoding="utf-8") as wf:
        json.dump(payload, wf, ensure_ascii=False, separators=(",", ":"))
    print("[書込]", op, "races=%d ok=%d (当日)" % (n, nok))


def main():
    if not DICTS_DIR:
        print("[エラー] dicts フォルダが見つかりません")
        sys.exit(1)

    cap_n = _safe_int(_env("CATHEDRAL_CAP_N", "40")) or 40
    rawscore_mode = _env("CATHEDRAL_RAWSCORE", "db")
    if rawscore_mode not in ("db", "none"):
        rawscore_mode = "db"

    info = pc.init_cathedral(DICTS_DIR)
    if not info.get("ok"):
        print("[エラー] 辞書ロード失敗:", info.get("missing"), info.get("error", ""))
        sys.exit(1)
    print("[大聖堂] 辞書ロードOK kimari=", info.get("kimari_n", 0))

    if not os.path.exists(OUT_DIR):
        os.makedirs(OUT_DIR)

    # === 当日モード: today_cache の出走表JSONを入力にする ===
    today_json = _env("CATHEDRAL_TODAY_JSON")
    if today_json:
        _build_from_today_cache(today_json, cap_n, rawscore_mode)
        return

    # === 過去モード: 月別DBを入力にする ===
    if not MONTHS_DIR:
        print("[エラー] keirin_months が見つかりません")
        sys.exit(1)

    start = _env("CATHEDRAL_START")
    end = _env("CATHEDRAL_END")
    month = _env("CATHEDRAL_MONTH")

    # 対象月の決定
    months = []
    if month:
        months = [month]
    elif start:
        ms = start[:6]
        me = (end or start)[:6]
        # start..end が同月想定。跨ぐ場合は両端の月を含める簡易処理
        months = sorted(set([ms, me]))
    else:
        print("[エラー] CATHEDRAL_MONTH か CATHEDRAL_START を指定してください")
        sys.exit(1)

    print("=" * 60)
    print("MONTHS_DIR:", MONTHS_DIR)
    print("DICTS_DIR :", DICTS_DIR)
    print("対象月    :", months)
    print("期間      :", start or "(月頭)", "..", end or "(月末)")
    print("cap_n     :", cap_n, " rawscore:", rawscore_mode)
    print("=" * 60)

    # 日別に集計
    by_date = {}

    for ym in months:
        mp = os.path.join(MONTHS_DIR, "keirin_" + ym + ".jsonl")
        if not os.path.exists(mp):
            print("[警告] 月別DBなし:", mp)
            continue
        print("[読込]", mp)
        with open(mp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ds = _race_date(rec)
                if ds is None:
                    continue
                if start and ds < start:
                    continue
                if end and ds > end:
                    continue

                line_str = rec.get("line", "") or ""
                venue = rec.get("place", "") or ""
                weather = rec.get("weather", "") or ""
                players_info = _build_players_info(rec, rawscore_mode)

                entry = {
                    "place": venue,
                    "race_no": rec.get("race_no", ""),
                    "line": line_str,
                    "weather": weather,
                }
                if not players_info:
                    entry["ok"] = False
                    entry["reason"] = "no_players_info"
                    entry["all"] = []
                else:
                    res = pc.predict_all_raw(line_str, players_info, venue,
                                             weather, cap_n=cap_n)
                    entry["ok"] = res.get("ok", False)
                    entry["reason"] = res.get("reason", "")
                    entry["all"] = res.get("all", [])

                r3, yen = _result_3t_and_refund(rec)
                entry["result_3t"] = r3
                entry["refund_3t"] = yen

                by_date.setdefault(ds, {})[_race_key(rec)] = entry

    # 日別に書き出し
    total_races = 0
    total_ok = 0
    for ds in sorted(by_date.keys()):
        races = by_date[ds]
        n = len(races)
        nok = sum(1 for v in races.values() if v.get("ok"))
        total_races += n
        total_ok += nok
        payload = {
            "date": ds,
            "races": races,
            "meta": {
                "n_races": n, "n_ok": nok,
                "cap_n": cap_n, "rawscore": rawscore_mode,
                "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
        op = os.path.join(OUT_DIR, "cathedral_" + ds + ".json")
        with open(op, "w", encoding="utf-8") as wf:
            json.dump(payload, wf, ensure_ascii=False, separators=(",", ":"))
        print("[書込]", op, "races=%d ok=%d" % (n, nok))

    print("=" * 60)
    print("完了 総レース=%d ok=%d 日数=%d" % (total_races, total_ok, len(by_date)))


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
# ============================================================
# 3連単オッズ 収集 v1 (月別分割ストック)
# ============================================================
# appの実績あるパーサー(parse_gamboo_trifecta_odds)をそのまま移植。
#   HTML構造: <table class="odds_table bt5 N"> の N = 1着車番
#             行頭th = 3着車番 / 列 = 2着車番(1着を除く昇順) / td = オッズ
#             ※行頭を2着と誤ると2着3着が転置する(app実装のコメントに記録あり)
#
# ストック方式: keirin_months と同じ月別分割
#   odds_months/YYYYMM.jsonl   1行 = 1レース
#   {"race_id","date","place","race_no","ts","n","odds":{"1-2-3":12.3,...}}
#   概算 1レース約3KB → 1日200R=600KB → 月18MB (100MB制限に余裕)
#
# 同一レースを複数回取得した場合は「最後に取得したもの」で上書き。
# (締切直前のオッズが最も実用的なため)
#
# 環境変数:
#   OD_DATE        取得日 YYYYMMDD (既定: 今日 JST)
#   OD_START/OD_END 範囲取得(過去日はgamboo側に数日しか残らない)
#   OD_ROSTER_DIR  出走表ディレクトリ (既定 today_cache)
#   OD_DB_DIR      DB月別分割ディレクトリ (既定 keirin_months)
#                  出走表が無い過去日はここからレース一覧を取る
#   OD_OUT_DIR     出力先 (既定 odds_months)
#   OD_SLEEP       リクエスト間隔秒 (既定 0.5)
#   OD_FORCE       "1" で既存も再取得
#   OD_MAXDAY      開催日数の上限 (既定7)
# ============================================================
import os
import io
import re
import json
import time

try:
    import requests as _rq
except Exception:
    _rq = None


def _env(k, d):
    v = os.environ.get(k, "")
    return v.strip() if v and v.strip() else d


def _jst_today():
    import datetime as _dt
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=9)).strftime("%Y%m%d")


OD_DATE = _env("OD_DATE", "")
OD_START = _env("OD_START", "")
OD_END = _env("OD_END", "")
ROSTER_DIR = _env("OD_ROSTER_DIR", "today_cache")
DB_DIR = _env("OD_DB_DIR", "keirin_months")
DB_FILE = _env("OD_DB_FILE", "keirin_data_scored_v2.jsonl")
OUT_DIR = _env("OD_OUT_DIR", "odds_months")
SLEEP = float(_env("OD_SLEEP", "0.5"))
FORCE = _env("OD_FORCE", "0") in ("1", "true", "yes")
MAXDAY = int(_env("OD_MAXDAY", "7"))   # 開催日数の上限(G1は7日開催もある)

# fetch_keirin_data_v21.py の BROWSER_HEADERS と完全一致させる。
# Referer が無いと gamboo は200を返しつつオッズ表を含まないページを返す。
# (端末の check_odds4.py 「ブラウザのリクエストヘッダーを完全再現」で
#  過去に判明していた問題。移植時に落としていた。)
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "*/*;q=0.8"),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.yen-joy.net/",
}
_RETRY_WAITS = [5, 15, 40]


def fetch_with_retry(url):
    if _rq is None:
        return -1, ""
    attempt = 0
    while True:
        try:
            r = _rq.get(url, headers=_HEADERS, timeout=20)
            try:
                r.encoding = r.apparent_encoding
            except Exception:
                pass
            st = r.status_code
            if st == 200:
                return 200, r.text
            if st == 404:
                return 404, ""
            if not (st == 403 or 500 <= st < 600):
                return st, ""
        except Exception:
            pass
        if attempt >= len(_RETRY_WAITS):
            return -1, ""
        time.sleep(_RETRY_WAITS[attempt])
        attempt = attempt + 1


def parse_gamboo_trifecta_odds(html):
    """app実装からの移植。3連単オッズを {"a-b-c": float} で返す。
    行頭th = 3着車番 / 列 = 2着車番。転置しないよう注意。"""
    out = {}
    if not html:
        return out
    tbls = re.findall(
        r'<table\s+class="odds_table\s+bt5\s+(\d+)[^"]*"[^>]*>(.*?)</table>',
        html, re.S)
    max_car = 0
    fi = 0
    while fi < len(tbls):
        try:
            fnum = int(tbls[fi][0])
            if fnum > max_car:
                max_car = fnum
        except Exception:
            pass
        fi = fi + 1
    if max_car < 3:
        max_car = 9
    ti = 0
    while ti < len(tbls):
        first_s, tbl = tbls[ti]
        ti = ti + 1
        try:
            first = int(first_s)
        except Exception:
            continue
        third_cols = []
        cc = 1
        while cc <= max_car:
            if cc != first:
                third_cols.append(cc)
            cc = cc + 1
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tbl, re.S)
        ri = 0
        while ri < len(rows):
            row = rows[ri]
            ri = ri + 1
            mth = re.search(r'<th[^>]*>(.*?)</th>', row, re.S)
            if not mth:
                continue
            second_txt = re.sub(r"<[^>]+>", "", mth.group(1)).strip()
            if not second_txt.isdigit():
                continue
            second = int(second_txt)
            tds = re.findall(r'<td([^>]*)>(.*?)</td>', row, re.S)
            col = 0
            for attr, val in tds:
                if col < len(third_cols):
                    third = third_cols[col]
                else:
                    third = None
                col = col + 1
                if third is None:
                    continue
                if "empty" in attr:
                    continue
                txt = re.sub(r"<[^>]+>", "", val).strip()
                if not txt:
                    continue
                try:
                    fv = float(txt)
                except Exception:
                    continue
                if fv <= 0:
                    continue
                row_bike = second   # 行頭th = 実3着
                col_bike = third    # 列    = 実2着
                out[str(first) + "-" + str(col_bike) + "-" + str(row_bike)] = fv
            # end for
    return out


DIAG = {"http_ng": 0, "html_short": 0, "no_table": 0, "ok": 0,
        "first_fail_url": "", "first_len": 0, "cache_hit": 0}

# (会場コード, 日付) -> (base_date, day) を記憶する。
# 同じ開催日の他レースは探索不要で即ヒットする(リクエスト1/3に削減)。
_CUP_CACHE = {}


def fetch_trifecta(code, date_str, race_no):
    """gambooから3連単オッズ取得。app実装と同じ探索。
       開催の base_date/day は (code,date) 単位でキャッシュする。"""
    from datetime import datetime as _dt, timedelta
    try:
        actual = _dt.strptime(str(date_str), "%Y%m%d")
    except Exception:
        return {}, ""

    # キャッシュ済みの開催なら、その組み合わせだけ試す
    ck = (code, date_str)
    if ck in _CUP_CACHE:
        base, day, rn_fmt = _CUP_CACHE[ck]
        cup_id = code + base
        sched_id = code + base + str(day).zfill(2) + "00"
        rn = str(int(race_no)).zfill(2) if rn_fmt == 2 else str(int(race_no))
        url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/"
               "odds/" + cup_id + "/" + sched_id + "/" + rn + "/3rentan/")
        st, html = fetch_with_retry(url)
        time.sleep(SLEEP)
        if st == 200 and html and len(html) >= 20000:
            odds = parse_gamboo_trifecta_odds(html)
            if odds:
                DIAG["ok"] = DIAG["ok"] + 1
                DIAG["cache_hit"] = DIAG["cache_hit"] + 1
                return odds, url
        # キャッシュが外れたら通常探索に落ちる
        del _CUP_CACHE[ck]

    diff = 0
    while diff < MAXDAY:
        base = (actual - timedelta(days=diff)).strftime("%Y%m%d")
        day = diff + 1
        cup_id = code + base
        sched_id = code + base + str(day).zfill(2) + "00"
        diff = diff + 1
        for rn in [str(int(race_no)).zfill(2), str(int(race_no))]:
            url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/"
                   "odds/" + cup_id + "/" + sched_id + "/" + rn + "/3rentan/")
            st, html = fetch_with_retry(url)
            time.sleep(SLEEP)
            if st != 200 or not html:
                DIAG["http_ng"] = DIAG["http_ng"] + 1
                if not DIAG["first_fail_url"]:
                    DIAG["first_fail_url"] = url + " status=" + str(st)
                continue
            if len(html) < 20000:
                DIAG["html_short"] = DIAG["html_short"] + 1
                if not DIAG["first_len"]:
                    DIAG["first_len"] = len(html)
                    DIAG["first_fail_url"] = url + " len=" + str(len(html))
                continue
            odds = parse_gamboo_trifecta_odds(html)
            if odds:
                DIAG["ok"] = DIAG["ok"] + 1
                _CUP_CACHE[ck] = (base, day, 2 if len(rn) == 2 else 1)
                return odds, url
            DIAG["no_table"] = DIAG["no_table"] + 1
            if not DIAG["first_len"]:
                DIAG["first_len"] = len(html)
                DIAG["first_fail_url"] = (url + " len=" + str(len(html))
                                          + " (表なし)")
    return {}, ""


_DB_CACHE = {}


def load_from_db(date_str):
    """DBの月別分割からその日のレース一覧を得る。
       過去日は出走表が残っていないので、こちらが本命の経路。
       keirin_months/YYYYMM.jsonl を探し、無ければ統合DBも見る。"""
    ym = date_str[0:6]
    if ym in _DB_CACHE:
        by_date = _DB_CACHE[ym]
        return by_date.get(date_str), "db:" + ym
    cands = [
        os.path.join(DB_DIR, ym + ".jsonl"),
        os.path.join(DB_DIR, "keirin_" + ym + ".jsonl"),
        DB_FILE,
    ]
    path = ""
    for c in cands:
        if c and os.path.exists(c):
            path = c
            break
    if not path:
        return None, ""
    by_date = {}
    f = io.open(path, encoding="utf-8")
    while True:
        ln = f.readline()
        if not ln:
            break
        ln = ln.strip()
        if not ln:
            continue
        # 統合DBを読む場合、対象月以外は素通り(高速化)
        if path == DB_FILE and ('"' + ym) not in ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        d = str(o.get("date", ""))
        if not d or d[0:6] != ym:
            continue
        rid = str(o.get("race_id", ""))
        if len(rid) < 11:
            continue
        if d not in by_date:
            by_date[d] = []
        by_date[d].append({"race_id": rid, "place": o.get("place", ""),
                           "race_no": o.get("race_no")})
    f.close()
    _DB_CACHE[ym] = by_date
    return by_date.get(date_str), path


def load_roster(date_str):
    """レース一覧を得る。出走表 → 無ければDB の順で探す。"""
    cands = [
        os.path.join(ROSTER_DIR, "races_" + date_str + ".json"),
        os.path.join(ROSTER_DIR, "cache_" + date_str + ".json"),
    ]
    for p in cands:
        if not os.path.exists(p):
            continue
        try:
            f = io.open(p, encoding="utf-8")
            data = json.load(f)
            f.close()
        except Exception:
            continue
        races = data if isinstance(data, list) else (
            data.get("races") if isinstance(data, dict) else None)
        if races:
            return races, p
    # 出走表が無ければDBから(過去日はこちら)
    return load_from_db(date_str)


def month_path(date_str):
    return os.path.join(OUT_DIR, date_str[0:6] + ".jsonl")


def load_existing(date_str):
    """その月の既存レコードを race_id -> line で読む。"""
    p = month_path(date_str)
    ex = {}
    if not os.path.exists(p):
        return ex, p
    f = io.open(p, encoding="utf-8")
    while True:
        ln = f.readline()
        if not ln:
            break
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        rid = o.get("race_id")
        if rid:
            ex[rid] = o
    f.close()
    return ex, p


def run_date(date_str):
    races, rpath = load_roster(date_str)
    if not races:
        print("  [" + date_str + "] 出走表なし → スキップ")
        return 0, 0
    print("  [" + date_str + "] 出走表: " + rpath + "  " + str(len(races))
          + " レース")
    ex, mp = load_existing(date_str)
    got = 0
    empty = 0
    for r in races:
        if not isinstance(r, dict):
            continue
        rid = str(r.get("race_id", ""))
        if len(rid) < 11:
            continue
        if (not FORCE) and rid in ex and ex[rid].get("n", 0) > 0:
            continue
        code = rid[0:2]
        d = rid[2:10]
        rno = rid[10:]
        odds, url = fetch_trifecta(code, d, rno)
        if odds:
            ex[rid] = {"race_id": rid, "date": d,
                       "place": r.get("place", ""),
                       "race_no": r.get("race_no"),
                       "ts": int(time.time()), "n": len(odds),
                       "odds": odds}
            got = got + 1
        else:
            empty = empty + 1
    # 月ファイルを書き出し(race_id順)。毎日書くので途中終了しても成果が残る。
    if got:
        if not os.path.isdir(OUT_DIR):
            os.makedirs(OUT_DIR)
        tmp = mp + ".tmp"
        f = io.open(tmp, "w", encoding="utf-8")
        for rid in sorted(ex):
            f.write(json.dumps(ex[rid], ensure_ascii=False) + "\n")
        f.close()
        os.replace(tmp, mp)
    print("    取得 " + str(got) + " / 空 " + str(empty)
          + "  → " + mp)
    return got, empty


def main():
    if _rq is None:
        print("[エラー] requests が必要")
        return
    dates = []
    if OD_START and OD_END:
        from datetime import datetime as _dt, timedelta
        a = _dt.strptime(OD_START, "%Y%m%d")
        b = _dt.strptime(OD_END, "%Y%m%d")
        while a <= b:
            dates.append(a.strftime("%Y%m%d"))
            a = a + timedelta(days=1)
    else:
        dates.append(OD_DATE if OD_DATE else _jst_today())
    print("=== 3連単オッズ収集 ===")
    print("対象日:", dates[0], "〜", dates[-1], " (" + str(len(dates)) + "日)")
    print("レース一覧: 出走表(" + ROSTER_DIR + ") → 無ければDB("
          + DB_DIR + ")")
    print("出力:", OUT_DIR)
    tg = 0
    te = 0
    for d in dates:
        g, e = run_date(d)
        tg = tg + g
        te = te + e
    print("")
    print("[完了] 取得 " + str(tg) + " レース / 空 " + str(te))
    print("")
    print("[診断] HTTP失敗 " + str(DIAG["http_ng"])
          + " / HTML短すぎ " + str(DIAG["html_short"])
          + " / 表が無い " + str(DIAG["no_table"])
          + " / 成功 " + str(DIAG["ok"])
          + " / うち開催キャッシュ命中 " + str(DIAG["cache_hit"]))
    print("  ※HTML短すぎ は開催日の総当たり探索によるもので異常ではない")
    if DIAG["first_fail_url"]:
        print("  最初の失敗: " + DIAG["first_fail_url"])
    if tg == 0 and te > 0:
        print("  ※全滅の場合、Referer等のヘッダー、URL形式、")
        print("    または開催の有無を疑ってください。")


if __name__ == "__main__":
    main()

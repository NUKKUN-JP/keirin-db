# -*- coding: utf-8 -*-
# ============================================================
# gamboo 2車単オッズ 収集・キャッシュ化 v3 (GitHub Actions対応)
# ============================================================
# 既存パイプライン(fetch_today_basic_v5.py)と同じ素の HTTP GET で取得する。
# 地域制限は無い(既存収集がGitHub Actionsで普通に動いている)。
#
# 目的: 指定日付範囲について gambooから2車単オッズを取得し
#   odds_cache/YYYYMMDD.jsonl に保存する。
#   ・過去日のオッズは gamboo 側で数日で失効する。取れるのは概ね直近数日。
#     (これはIPや経路の問題ではなく、gambooが古いオッズを保持しないため)
#
# 対象日の会場×R一覧:
#   today_cache/races_YYYYMMDD.json (既存収集の出力) を第一候補に読む。
#   各レコードの race_id(会場2桁+YYYYMMDD+R2桁)から会場コードとR番号を得る。
#   無ければ cache_YYYYMMDD.json / DB でフォールバック。
#
# 出力: odds_cache/YYYYMMDD.jsonl  1行=1レース
#   {"date","code","rno","n","odds":{"a-b":float,...},"ts"}
#
# 実行: python fetch_odds_cache_v2.py   (設定は下のブロック)
# ============================================================
import os
import re
import io
import json
import time
from urllib import request as _urlreq
from urllib import error as _urlerr

try:
    import requests as _rq
except Exception:
    _rq = None

# ===== 設定 (環境変数があれば優先。GitHub Actionsから渡せる) =====
import os as _os
def _envs(name, default):
    v = _os.environ.get(name, "")
    return v.strip() if v and v.strip() else default

# 収集対象日。ODDS_DATE / ODDS_START_DATE・ODDS_END_DATE で範囲指定可。
# 何も無ければ当日(JST想定・TZは呼び出し側)。
_today = __import__("datetime").datetime.now().strftime("%Y%m%d")
START_DATE = _envs("ODDS_START_DATE", _envs("ODDS_DATE", _today))
END_DATE   = _envs("ODDS_END_DATE",   _envs("ODDS_DATE", START_DATE))
TODAY_CACHE_DIR = _envs("ODDS_ROSTER_DIR", "today_cache")   # races_YYYYMMDD.json の場所
# ワークフローが対象JSONを直接指す場合(cathedral_today.yml と同方式)
ROSTER_JSON = _envs("ODDS_ROSTER_JSON", "")
DB_PATH = _envs("ODDS_DB_PATH", "keirin_data_scored_v2.jsonl")
OUT_DIR = _envs("ODDS_OUT_DIR", "odds_cache")
FORCE = _envs("ODDS_FORCE", "0") in ("1", "true", "yes")
SLEEP_SEC = float(_envs("ODDS_SLEEP", "0.35"))
MAX_RETRY = int(_envs("ODDS_RETRY", "3"))
TIMEOUT = int(_envs("ODDS_TIMEOUT", "15"))
PROBE_ONLY = _envs("ODDS_PROBE_ONLY", "0") in ("1", "true", "yes")
# ================

# 環境変数による上書き(GitHub Actions用。未設定なら上の既定値を使う)
START_DATE = os.environ.get("FL_START", START_DATE)
END_DATE = os.environ.get("FL_END", END_DATE)
TODAY_CACHE_DIR = os.environ.get("FL_ROSTER_DIR", TODAY_CACHE_DIR)
OUT_DIR = os.environ.get("FL_OUT_DIR", OUT_DIR)
if os.environ.get("FL_PROBE_ONLY", "") == "1":
    PROBE_ONLY = True
if os.environ.get("FL_FORCE", "") == "1":
    FORCE = True

# 会場名→コード(cache_ / DB が会場名しか持たない場合のフォールバック用)
NAME_TO_CODE = {
    "函館": "11", "青森": "12", "いわき平": "13",
    "弥彦": "21", "前橋": "22", "取手": "23", "宇都宮": "24", "大宮": "25",
    "西武園": "26", "京王閣": "27", "立川": "28",
    "松戸": "31", "川崎": "34", "平塚": "35", "小田原": "36", "伊東": "37",
    "伊東温泉": "37", "静岡": "38",
    "名古屋": "42", "岐阜": "43", "大垣": "44", "豊橋": "45", "富山": "46",
    "松阪": "47", "四日市": "48",
    "福井": "51", "奈良": "53", "向日町": "54", "和歌山": "55", "岸和田": "56",
    "玉野": "61", "広島": "62", "防府": "63",
    "高松": "71", "小松島": "73", "高知": "74", "松山": "75",
    "小倉": "81", "久留米": "83", "武雄": "84", "佐世保": "85", "別府": "86",
    "熊本": "87",
}

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"),
    "Accept-Language": "ja,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml",
}


def _http_get(url):
    """(status, html)。失敗時 (0,'')。requestsがあれば優先。"""
    tries = 0
    while tries < MAX_RETRY:
        tries = tries + 1
        if _rq is not None:
            try:
                r = _rq.get(url, headers=_HEADERS, timeout=TIMEOUT)
                if r.status_code in (404, 403):
                    return (r.status_code, "")
                if r.status_code == 200:
                    return (200, r.text)
            except Exception:
                time.sleep(1.0)
                continue
        else:
            try:
                req = _urlreq.Request(url, headers=_HEADERS)
                resp = _urlreq.urlopen(req, timeout=TIMEOUT)
                raw = resp.read()
                return (resp.getcode(), raw.decode("utf-8", "replace"))
            except _urlerr.HTTPError as e:
                if e.code in (404, 403):
                    return (e.code, "")
                time.sleep(1.0)
            except Exception:
                time.sleep(1.0)
    return (0, "")


def parse_gamboo_exacta_odds(html):
    """gambooオッズページHTMLから2車単オッズを {"a-b": float} で返す。"""
    out = {}
    if not html:
        return out
    tbls = re.findall(
        r'<table\s+class="odds_table\s+bt5\s+(\d+)[^"]*"[^>]*>(.*?)</table>',
        html, re.S)
    if tbls:
        ti = 0
        while ti < len(tbls):
            first_s, tbl = tbls[ti]
            ti = ti + 1
            try:
                first = int(first_s)
            except Exception:
                continue
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tbl, re.S)
            ri = 0
            while ri < len(rows):
                row = rows[ri]
                ri = ri + 1
                mth = re.search(r'<th[^>]*>(.*?)</th>', row, re.S)
                if not mth:
                    continue
                sec_txt = re.sub(r"<[^>]+>", "", mth.group(1)).strip()
                if not sec_txt.isdigit():
                    continue
                second = int(sec_txt)
                if second == first:
                    continue
                mtd = re.search(r'<td[^>]*>(.*?)</td>', row, re.S)
                if not mtd:
                    continue
                txt = re.sub(r"<[^>]+>", "", mtd.group(1)).strip()
                try:
                    fv = float(txt)
                except Exception:
                    continue
                if fv > 0:
                    out[str(first) + "-" + str(second)] = fv
        if out:
            return out
    single = re.search(r'<table[^>]*class="[^"]*odds[^"]*"[^>]*>(.*?)</table>',
                       html, re.S)
    if single:
        tbl = single.group(1)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', tbl, re.S)
        max_car = 9
        ri = 0
        while ri < len(rows):
            row = rows[ri]
            ri = ri + 1
            mth = re.search(r'<th[^>]*>(.*?)</th>', row, re.S)
            if not mth:
                continue
            first_txt = re.sub(r"<[^>]+>", "", mth.group(1)).strip()
            if not first_txt.isdigit():
                continue
            first = int(first_txt)
            second_cols = []
            cc = 1
            while cc <= max_car:
                if cc != first:
                    second_cols.append(cc)
                cc = cc + 1
            tds = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
            col = 0
            for val in tds:
                if col < len(second_cols):
                    second = second_cols[col]
                else:
                    second = None
                col = col + 1
                if second is None:
                    continue
                txt = re.sub(r"<[^>]+>", "", val).strip()
                if not txt:
                    continue
                try:
                    fv = float(txt)
                except Exception:
                    continue
                if fv > 0:
                    out[str(first) + "-" + str(second)] = fv
    return out


def fetch_exacta(code, date_str, rno):
    """gambooから2車単オッズを取得。code=会場2桁。返り値 (odds, tried)。"""
    tried = []
    if not code:
        return ({}, tried)
    from datetime import datetime as _dt, timedelta
    try:
        actual_dt = _dt.strptime(str(date_str), "%Y%m%d")
    except Exception:
        return ({}, tried)
    kinds = ["2shatan", "nishatan", "2rentan"]
    diff_days = 0
    while diff_days < 4:
        base_dt = actual_dt - timedelta(days=diff_days)
        base_date_str = base_dt.strftime("%Y%m%d")
        day = diff_days + 1
        cup_id = code + base_date_str
        sched_id = code + base_date_str + str(day).zfill(2) + "00"
        diff_days = diff_days + 1
        rn_forms = [str(int(rno)).zfill(2), str(int(rno))]
        seen = []
        for rn in rn_forms:
            if rn in seen:
                continue
            seen.append(rn)
            for kind in kinds:
                url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/"
                       "race-card/odds/" + cup_id + "/" + sched_id + "/"
                       + rn + "/" + kind + "/")
                tried.append(url)
                status, html = _http_get(url)
                if status != 200 or not html:
                    continue
                odds = parse_gamboo_exacta_odds(html)
                if odds:
                    return (odds, tried)
                time.sleep(SLEEP_SEC)
    return ({}, tried)


def _parse_race_id(rec):
    """race_id(会場2桁+YYYYMMDD+R2桁)→(code,rno)。無ければ補完。失敗時None。"""
    rid = str(rec.get("race_id", ""))
    if len(rid) >= 11 and rid.isdigit():
        vc = rid[0:2]
        rno = rid[10:]
        try:
            rno_i = int(rno)
        except Exception:
            rno_i = None
        if rno_i:
            return (vc, rno_i)
    vc = str(rec.get("venue_code", "") or "")
    rno = rec.get("race_no")
    if (not vc):
        nm = rec.get("place") or rec.get("venue") or ""
        vc = NAME_TO_CODE.get(nm, "")
    if vc and rno is not None:
        try:
            return (vc, int(rno))
        except Exception:
            return None
    return None


def _load_roster_json(path):
    try:
        f = io.open(path, encoding="utf-8")
        data = json.load(f)
        f.close()
    except Exception:
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        r = data.get("races") or data.get("list")
        if r:
            return r
        vals = list(data.values())
        if vals and isinstance(vals[0], dict):
            return vals
    return None


def _races_for_date(date_str):
    """(code,rno) の一覧。ROSTER_JSON → races_ → cache_ → DB の順で解決。"""
    cand = []
    if ROSTER_JSON:
        cand.append(ROSTER_JSON)
    cand.append(os.path.join(TODAY_CACHE_DIR, "races_" + date_str + ".json"))
    cand.append(os.path.join(TODAY_CACHE_DIR, "cache_" + date_str + ".json"))
    for path in cand:
        if not os.path.exists(path):
            continue
        recs = _load_roster_json(path)
        if not recs:
            continue
        out = []
        seen = {}
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            pr = _parse_race_id(rec)
            if pr is None:
                continue
            k = (pr[0], str(pr[1]))
            if k in seen:
                continue
            seen[k] = True
            out.append(pr)
        if out:
            return out
    # DB フォールバック
    out = []
    if os.path.exists(DB_PATH):
        seen = {}
        try:
            f = io.open(DB_PATH, encoding="utf-8")
        except Exception:
            f = None
        if f is not None:
            try:
                for line in f:
                    if ('"' + date_str + '"') not in line:
                        continue
                    try:
                        rec = json.loads(line.strip())
                    except Exception:
                        continue
                    if str(rec.get("date", "")) != str(date_str):
                        continue
                    pr = _parse_race_id(rec)
                    if pr is None:
                        continue
                    k = (pr[0], str(pr[1]))
                    if k in seen:
                        continue
                    seen[k] = True
                    out.append(pr)
            finally:
                f.close()
    return out


def _load_out(date_str):
    path = os.path.join(OUT_DIR, date_str + ".jsonl")
    out = {}
    if not os.path.exists(path):
        return out
    try:
        f = io.open(path, encoding="utf-8")
    except Exception:
        return out
    try:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            out[(str(rec.get("code", "")), str(rec.get("rno", "")))] = rec
    finally:
        f.close()
    return out


def _save_out(date_str, recmap):
    if not os.path.isdir(OUT_DIR):
        try:
            os.makedirs(OUT_DIR)
        except Exception:
            pass
    path = os.path.join(OUT_DIR, date_str + ".jsonl")
    tmp = path + ".tmp"
    g = io.open(tmp, "w", encoding="utf-8")
    try:
        for key in sorted(recmap, key=lambda k: (k[0], str(k[1]))):
            g.write(json.dumps(recmap[key], ensure_ascii=False) + "\n")
    finally:
        g.close()
    if os.path.exists(path):
        os.remove(path)
    os.rename(tmp, path)


def _date_range(a, b):
    from datetime import datetime, timedelta
    d0 = datetime.strptime(a, "%Y%m%d")
    d1 = datetime.strptime(b, "%Y%m%d")
    if d1 < d0:
        d0, d1 = d1, d0
    out = []
    cur = d0
    while cur <= d1:
        out.append(cur.strftime("%Y%m%d"))
        cur = cur + timedelta(days=1)
    return out


def _probe(dates):
    for ds in dates:
        races = _races_for_date(ds)
        if not races:
            continue
        code, rno = races[0]
        print("[probe] 到達テスト: " + ds + " code=" + code + " " + str(rno) + "R")
        odds, tried = fetch_exacta(code, ds, rno)
        if odds:
            print("[probe] OK 取得成功 (" + str(len(odds)) + "点) → 続行")
            return True
        print("[probe] 取得0件。gambooに該当オッズが無い可能性(過去日は失効)。")
        if tried:
            print("[probe]   例: " + tried[0])
        return False
    print("[probe] 対象日の roster(today_cache/races_*.json)が見つかりません。")
    print("[probe]   TODAY_CACHE_DIR のパスと、races_YYYYMMDD.json の有無を確認。")
    return False


def main():
    print("=== gamboo 2車単オッズ収集 v2  " + START_DATE + "〜" + END_DATE + " ===")
    if _rq is None:
        print("[info] requests 不在 → urllib で取得")
    dates = _date_range(START_DATE, END_DATE)

    ok = _probe(dates)
    if PROBE_ONLY:
        print("[done] PROBE_ONLY 終了")
        return
    if not ok:
        print("[中止] 到達/取得できませんでした。上のprobeメッセージを確認してください。")
        return

    g_fetch = 0
    g_empty = 0
    for ds in dates:
        races = _races_for_date(ds)
        if not races:
            print(ds + ": roster無し → スキップ")
            continue
        recmap = _load_out(ds)
        n_fetch = 0
        n_empty = 0
        n_skip = 0
        for code, rno in races:
            key = (code, str(rno))
            if (not FORCE) and key in recmap and recmap[key].get("odds"):
                n_skip = n_skip + 1
                continue
            odds, _t = fetch_exacta(code, ds, rno)
            recmap[key] = {"date": ds, "code": code, "rno": rno,
                           "n": len(odds), "odds": odds,
                           "ts": int(time.time())}
            if odds:
                n_fetch = n_fetch + 1
            else:
                n_empty = n_empty + 1
            time.sleep(SLEEP_SEC)
        _save_out(ds, recmap)
        g_fetch = g_fetch + n_fetch
        g_empty = g_empty + n_empty
        print(ds + ": 取得" + str(n_fetch) + " 空" + str(n_empty)
              + " skip" + str(n_skip) + " → odds_cache/" + ds + ".jsonl")

    print("")
    print("[完了] 取得 " + str(g_fetch) + "R / 空(失効等) " + str(g_empty) + "R")


if __name__ == "__main__":
    main()

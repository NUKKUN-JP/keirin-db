"""
backfill_shb_github_v1.py  (GitHub Actions用 S/H/B全期間補完)

winticket APIを cup_id 直接推定で叩き、DB全レコードの players[bike] に
s/h/b を補完する。過去レースも取得可能(検証済み)。H無し時代は h=null。

効率化:
  (会場コード, 開催日) 単位で cup_id+day を1回だけ総当たり推定し、
  その日の全レースは cup_id+day 固定で rno を変えるだけで取得。
  → 総当たり(最大7日遡り)は日付ごとに1回で済む。

  S = records[i].standing
  H = records[i].home (hasHome=False のとき null)
  B = records[i].back

GitHub Actions想定:
  - requests 使用 (Actionsには入っている / requirements.txtでも可)
  - 環境変数 KEIRIN_DB で DBパス指定可
  - 1000レコードごと + 開催ごとに途中保存 (再開対応: s済みはスキップ)
  - 既存DBは .bak にバックアップ

ローカル(Pydroid3)でも動く。引数不要で全期間補完。
  python backfill_shb_github_v1.py
  python backfill_shb_github_v1.py --from 20240101 --to 20241231  (期間限定)

Pydroid3制約: f-string禁止 / for-else禁止
"""

import os
import sys
import json
import time

try:
    import requests
    _HAS_REQUESTS = True
except Exception:
    _HAS_REQUESTS = False

DL = "/storage/emulated/0/Download"
DATA_DIR = os.path.join(DL, "takusen", "data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = os.path.join(os.getcwd(), "takusen", "data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = os.getcwd()
DB_PATH = os.path.join(DATA_DIR, "keirin_data_scored_v2.jsonl")
_ENV_DB = os.environ.get("KEIRIN_DB", "")
if _ENV_DB:
    DB_PATH = _ENV_DB

MAX_LOOKBACK = 7   # 開催開始日を遡る最大日数
SLEEP_SEC = 0.1    # API間隔
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android) keirin-oracle/1.0",
    "Accept": "application/json",
    "Referer": "https://www.winticket.jp/",
}


def http_json(url):
    if _HAS_REQUESTS:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    try:
        from urllib.request import Request, urlopen
    except Exception:
        from urllib2 import Request, urlopen
    req = Request(url, headers=HEADERS)
    f = urlopen(req, timeout=20)
    try:
        raw = f.read()
    finally:
        try:
            f.close()
        except Exception:
            pass
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


def date_minus(ymd, days):
    from datetime import datetime as _dt, timedelta
    d = _dt.strptime(ymd, "%Y%m%d") - timedelta(days=days)
    return d.strftime("%Y%m%d")


def api_url(cup_id, day, rno):
    return ("https://api.winticket.jp/v1/keirin/cups/" + cup_id
            + "/schedules/" + str(day) + "/races/" + str(int(rno)) + "?pf=web")


def extract_shb(data):
    records = data.get("records") if isinstance(data, dict) else None
    entries = data.get("entries") if isinstance(data, dict) else None
    if not records or not entries:
        return None
    out = {}
    n = min(len(records), len(entries))
    i = 0
    while i < n:
        rec = records[i]
        ent = entries[i]
        bike = ent.get("number")
        if bike is None:
            bike = ent.get("bracketNumber")
        if bike is None:
            i = i + 1
            continue
        has_home = rec.get("hasHome", False)
        out[str(bike)] = {
            "s": rec.get("standing"),
            "h": rec.get("home") if has_home else None,
            "b": rec.get("back"),
        }
        i = i + 1
    return out if out else None


# (会場コード, 開催日) -> (cup_id, day) の解決キャッシュ
_cupday_cache = {}


def resolve_cup_day(venue_code, race_date, sample_rno):
    """開催開始日を遡って総当たりし、(cup_id, day)を確定。失敗時None。
    sample_rno は確認用レース番号(その日存在するR)。"""
    ck = venue_code + "_" + race_date
    if ck in _cupday_cache:
        return _cupday_cache[ck]
    result = None
    back = 0
    while back <= MAX_LOOKBACK:
        start_date = date_minus(race_date, back)
        cup_id = start_date + venue_code
        day = back + 1
        url = api_url(cup_id, day, sample_rno)
        try:
            data = http_json(url)
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("records") and data.get("entries"):
            result = (cup_id, day)
            break
        time.sleep(SLEEP_SEC)
        back = back + 1
    _cupday_cache[ck] = result
    return result


def fetch_shb_fast(cup_id, day, rno):
    """確定済み cup_id+day で1レースのS/H/Bを取得"""
    url = api_url(cup_id, day, rno)
    try:
        data = http_json(url)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return extract_shb(data)


def has_shb(rec):
    players = rec.get("players")
    if not isinstance(players, dict) or not players:
        return False
    for bs in players:
        if "s" not in players[bs]:
            return False
    return True


def parse_args(argv):
    opt = {"from": "00000000", "to": "99999999"}
    i = 0
    while i < len(argv):
        if argv[i] == "--from" and i + 1 < len(argv):
            opt["from"] = argv[i + 1]
            i = i + 2
            continue
        if argv[i] == "--to" and i + 1 < len(argv):
            opt["to"] = argv[i + 1]
            i = i + 2
            continue
        i = i + 1
    return opt


def save_all(records_text):
    tmp = DB_PATH + ".tmp"
    f = open(tmp, "w", encoding="utf-8")
    try:
        for ln in records_text:
            f.write(ln + "\n")
    finally:
        f.close()
    os.replace(tmp, DB_PATH)


def main():
    opt = parse_args(sys.argv[1:])
    print("================================================")
    print(" backfill_shb_github_v1  (S/H/B 全期間補完)")
    print(" DB: " + DB_PATH)
    print(" requests: " + ("あり" if _HAS_REQUESTS else "なし(urllib)"))
    print(" 期間: " + opt["from"] + " 〜 " + opt["to"])
    print("================================================")
    if not os.path.exists(DB_PATH):
        print("[error] DB not found: " + DB_PATH)
        return

    # バックアップ
    bak = DB_PATH + ".bak"
    if not os.path.exists(bak):
        print("バックアップ作成: " + bak)
        src = open(DB_PATH, "r", encoding="utf-8")
        dst = open(bak, "w", encoding="utf-8")
        try:
            for line in src:
                dst.write(line)
        finally:
            src.close()
            dst.close()

    # 全レコード読み込み(順序保持)
    recs = []
    f = open(DB_PATH, "r", encoding="utf-8")
    try:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                recs.append(json.loads(s))
            except Exception:
                recs.append({"__raw__": s})
    finally:
        f.close()
    total = len(recs)
    print("総レコード: " + str(total))

    filled = 0
    skipped = 0
    failed = 0
    done = 0
    t0 = time.time()

    i = 0
    while i < total:
        rec = recs[i]
        i = i + 1
        if "__raw__" in rec:
            continue
        date_str = str(rec.get("date", ""))
        if date_str < opt["from"] or date_str > opt["to"]:
            continue
        if has_shb(rec):
            skipped = skipped + 1
            continue
        rid = str(rec.get("race_id", ""))
        vcode = rid[:2] if len(rid) >= 2 else ""
        rno = rec.get("race_no", 1)
        if not vcode or not date_str:
            failed = failed + 1
            continue
        # 日付単位で cup_id+day を解決(キャッシュ)
        cupday = resolve_cup_day(vcode, date_str, rno)
        done = done + 1
        if not cupday:
            failed = failed + 1
        else:
            cup_id, day = cupday
            shb = fetch_shb_fast(cup_id, day, rno)
            time.sleep(SLEEP_SEC)
            if shb:
                players = rec.get("players", {})
                for bs in players:
                    v = shb.get(str(bs))
                    if v:
                        players[bs]["s"] = v.get("s")
                        players[bs]["h"] = v.get("h")
                        players[bs]["b"] = v.get("b")
                    else:
                        players[bs]["s"] = None
                        players[bs]["h"] = None
                        players[bs]["b"] = None
                filled = filled + 1
            else:
                failed = failed + 1
        # 進捗・途中保存
        if done % 200 == 0:
            el = int(time.time() - t0)
            rate = round(done / el, 1) if el > 0 else 0
            print("  処理" + str(done) + " (補完" + str(filled)
                  + "/失敗" + str(failed) + "/スキップ" + str(skipped)
                  + ") 経過" + str(el) + "秒 " + str(rate) + "件/秒")
        if done % 1000 == 0:
            _flush(recs)
            print("  --- 途中保存 ---")

    _flush(recs)
    print("")
    print("=== 完了 ===")
    print("補完:" + str(filled) + " 失敗:" + str(failed)
          + " スキップ:" + str(skipped))
    print("所要:" + str(int(time.time() - t0)) + "秒")


def _flush(recs):
    out = []
    for r in recs:
        if "__raw__" in r:
            out.append(r["__raw__"])
        else:
            out.append(json.dumps(r, ensure_ascii=False))
    save_all(out)


if __name__ == "__main__":
    main()

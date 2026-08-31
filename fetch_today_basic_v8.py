"""当日基本データ事前取得 v8

v7 からの変更点 (取得パイプラインの作り直し):

  1. 日単位の「取得済みならスキップ」を廃止した。
     v7 はキャッシュがあると『キャッシュに載っている会場』だけで
     完全性を判定していたため、最初の取得時にまだ掲載されていない会場
     (ミッドナイト等) には以後どれだけ実行しても気づけなかった。
     8/26 が 61R のまま固まり、青森・宇都宮が丸ごと欠けたのはこれ。
     v8 は毎回すべての会場を確認し、キャッシュに無い会場を取りに行く。

  2. 判定をレース単位にした。
       ready = 発走時刻あり かつ ライン情報あり
     readyなレースには触らない。未readyのレースだけを、
     発走時刻を過ぎるまで追い続ける。過ぎたら諦める。
     これにより無限リトライにならず、打ち切りも自然に決まる。

  3. S/B は ready の条件に入れない。
     入れると、最後まで S/B が付かないレースが永久に未readyとなり、
     毎回の再取得を呼び続けてしまう。
     S/B・race_kind・grade は毎回まとめて付与を試みる。

  4. KEIRIN_LINE_HEAL を廃止した。
     補完は「モード」ではなく常時の動作にした。
     v7 では HEAL=0 の実行が冒頭のスキップ判定で早期returnし、
     補完処理まで到達していなかった。

  5. 会場の集合は減らさない (merge_races)。
     race_id をキーに和集合を取り、既にreadyなレースは上書きしない。
     取得元が一時的に空を返しても、埋まっていたラインを失わない。

環境変数:
  KEIRIN_DATE          単日指定 (YYYYMMDD)
  KEIRIN_START/END     期間指定 (YYYYMMDD)
  KEIRIN_NOTIFY_FAIL   1 で取得0件のときLINE通知
"""

import os
import sys
import json
import glob
from datetime import datetime, timedelta

OUT_DIR = "today_cache"
KEEP_DAYS = 3
MIN_RACES = 5   # 1会場あたりの最低レース数。未満なら取りこぼしとみなす
MAX_HEAL_TRIES = 8   # 1レースあたりの補完試行の上限。超えたら追わない
RETRY_WAIT = 5  # 再取得前の待機秒

# ============================================================
# S/H/B 付与 (backfill_shb_github_v3.py から移植)
#   winticket APIを叩き、各レースの players[車番] に s/h/b を付与する。
# ============================================================
try:
    import requests as _shb_requests
    _SHB_HAS_REQUESTS = True
except Exception:
    _SHB_HAS_REQUESTS = False

_SHB_MAX_LOOKBACK = 7    # 開催開始日を遡る最大日数
_SHB_SLEEP = 0.1         # cup_id解決の総当たり間隔
_SHB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android) keirin-oracle/1.0",
    "Accept": "application/json",
    "Referer": "https://www.winticket.jp/",
}
# (会場コード, 開催日) -> (cup_id, day) 解決キャッシュ
_shb_cupday_cache = {}


# 半角の級班を全角へ。DBの race_kind が全角なので揃える。
_CLASS_FW = {
    "S級": "Ｓ級", "A級": "Ａ級", "L級": "Ｌ級",
    "Ｓ級": "Ｓ級", "Ａ級": "Ａ級", "Ｌ級": "Ｌ級",
}
# raceType にこれらが入っていれば級班を足さない (二重になるため)
_KIND_HAS_CLASS = ("チャレンジ", "ガールズ", "Ｓ級", "S級", "Ａ級", "A級",
                   "Ｌ級", "L級")


# grade の数値 -> 文字。DBの grade と152件突き合わせて確定した。
#   1:F2 97件 / 2:F1 34件 / 3:G3 21件  (混在なし)
# 4以上は今回のデータに無かったので入れていない。
# 未知の数値が来たら空欄にして warn を出す (記念開催で判明したら足す)。
_GRADE_NUM = {1: "F2", 2: "F1", 3: "G3"}
_grade_unknown = {}


def grade_of(num):
    """cups[].grade の数値をグレード文字にする。分からなければ空。"""
    if num is None:
        return ""
    try:
        n = int(num)
    except Exception:
        return ""
    if n in _GRADE_NUM:
        return _GRADE_NUM[n]
    if n not in _grade_unknown:
        _grade_unknown[n] = 0
        print("[grade] 未知の数値 " + str(n) + " (空欄にします)")
    _grade_unknown[n] = _grade_unknown[n] + 1
    return ""


def day_label_of(day, duration):
    """何日目か。初日 / N日目 / 最終日。"""
    try:
        d = int(day)
    except Exception:
        return ""
    if d <= 0:
        return ""
    try:
        du = int(duration)
    except Exception:
        du = 0
    if d == 1:
        return "初日"
    if du and d >= du:
        return "最終日"
    return str(d) + "日目"


# 選手の級班。class 1=S級 2=A級、group が班。
#   A級3班 = A3、S級1班 = S1 のように書く。
#   ガールズ(L級)は class が別値になるはずなので、その時は空にする。
_CLASS_NUM = {1: "S", 2: "A", 3: "L"}


def player_class_of(cls_num, grp_num):
    """playerCurrentTermClass / Group から S1 A3 のような表記を作る。"""
    if cls_num is None:
        return ""
    try:
        c = int(cls_num)
    except Exception:
        return ""
    ch = _CLASS_NUM.get(c, "")
    if not ch:
        return ""
    try:
        g = int(grp_num)
    except Exception:
        return ch
    if g <= 0:
        return ch
    return ch + str(g)


def build_race_kind(cls, race_type):
    """class と raceType から DBと同じ形の race_kind を組み立てる。"""
    rt = (race_type or "").strip()
    cl = (cls or "").strip()
    if not rt:
        return ""
    i = 0
    while i < len(_KIND_HAS_CLASS):
        if _KIND_HAS_CLASS[i] in rt:
            return rt
        i = i + 1
    if not cl:
        return rt
    return _CLASS_FW.get(cl, cl) + rt


def _shb_http_json(url):
    if _SHB_HAS_REQUESTS:
        r = _shb_requests.get(url, headers=_SHB_HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        return r.json()
    try:
        from urllib.request import Request, urlopen
    except Exception:
        from urllib2 import Request, urlopen
    req = Request(url, headers=_SHB_HEADERS)
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


def _shb_date_minus(ymd, days):
    d = datetime.strptime(ymd, "%Y%m%d") - timedelta(days=days)
    return d.strftime("%Y%m%d")


def _shb_api_url(cup_id, day, rno):
    return ("https://api.winticket.jp/v1/keirin/cups/" + cup_id
            + "/schedules/" + str(day) + "/races/" + str(int(rno)) + "?pf=web")


def _shb_extract(data):
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


def _shb_resolve_cup_day(venue_code, race_date, sample_rno):
    ck = venue_code + "_" + race_date
    if ck in _shb_cupday_cache:
        return _shb_cupday_cache[ck]
    import time
    result = None
    back = 0
    while back <= _SHB_MAX_LOOKBACK:
        start_date = _shb_date_minus(race_date, back)
        cup_id = start_date + venue_code
        day = back + 1
        url = _shb_api_url(cup_id, day, sample_rno)
        try:
            data = _shb_http_json(url)
        except Exception:
            data = None
        if isinstance(data, dict) and data.get("records") and data.get("entries"):
            result = (cup_id, day)
            break
        time.sleep(_SHB_SLEEP)
        back = back + 1
    _shb_cupday_cache[ck] = result
    return result


def _shb_fetch(cup_id, day, rno):
    """1回のAPI呼び出しで、必要なものをまとめて取る。
    S/B取得で既に叩いているので、追加の通信は発生しない。
    返り値:
      {"shb": {車番->s/h/b}, "race_kind": str, "grade": str,
       "day_label": str, "pclass": {車番->"A3"}}"""
    url = _shb_api_url(cup_id, day, rno)
    try:
        data = _shb_http_json(url)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    out = {"shb": _shb_extract(data), "race_kind": "",
           "grade": "", "day_label": "", "pclass": {},
           "line": _line_from_api(data)}

    race = data.get("race")
    if isinstance(race, dict):
        out["race_kind"] = build_race_kind(race.get("class"),
                                           race.get("raceType"))

    # グレードと日目。schedule.cupId と一致する cups を引く。
    sch = data.get("schedule")
    cid = ""
    dnum = None
    if isinstance(sch, dict):
        cid = str(sch.get("cupId", "") or "")
        dnum = sch.get("day")
    cups = data.get("cups")
    if isinstance(cups, list) and cid:
        i = 0
        while i < len(cups):
            c = cups[i]
            i = i + 1
            if not isinstance(c, dict):
                continue
            if str(c.get("id", "")) != cid:
                continue
            out["grade"] = grade_of(c.get("grade"))
            out["day_label"] = day_label_of(dnum, c.get("duration"))
            break
    if not out["day_label"] and dnum is not None:
        out["day_label"] = day_label_of(dnum, 0)

    # 選手の級班
    ent = data.get("entries")
    if isinstance(ent, list):
        i = 0
        while i < len(ent):
            e = ent[i]
            i = i + 1
            if not isinstance(e, dict):
                continue
            bike = e.get("number")
            if bike is None:
                bike = e.get("bracketNumber")
            if bike is None:
                continue
            pc = player_class_of(e.get("playerCurrentTermClass"),
                                 e.get("playerCurrentTermGroup"))
            if pc:
                out["pclass"][str(bike)] = pc
    return out


def _line_from_api(data):
    """ウィンチケットの linePrediction から並びを組み立てる。

    v8: ミッドナイト開催で、こちらの取得経路だとラインが空のまま
      残ることがあった (8/28 の玉野・小松島が全レース「ライン情報なし」)。
      ところが同じAPIの linePrediction には並びが入っており、
      別途調べた1,260レースでは100%取れていた。
      S/B取得で既に叩いている応答なので、追加の通信は発生しない。

    返り値: "534-1-2-6" の形。取れなければ ""。
    """
    lp = data.get("linePrediction")
    if not isinstance(lp, dict):
        return ""
    chunks = []
    for ln in (lp.get("lines") or []):
        if not isinstance(ln, dict):
            continue
        grp = ""
        for ent in (ln.get("entries") or []):
            if not isinstance(ent, dict):
                continue
            for n in (ent.get("numbers") or []):
                try:
                    grp = grp + str(int(n))
                except Exception:
                    pass
        if grp:
            chunks.append(grp)
    if not chunks:
        return ""
    return "-".join(chunks)


def _race_parts(rec):
    """race_id(会場2桁+YYYYMMDD+R2桁) から (venue_code, date, race_no) を返す。
    取れない場合は rec の date/venue_code/race_no キーで補完。失敗時None。"""
    rid = str(rec.get("race_id", ""))
    if len(rid) >= 11 and rid.isdigit():
        # 形式: VV(2) YYYYMMDD(8) RR(残り)
        vc = rid[0:2]
        dt = rid[2:10]
        rno = rid[10:]
        try:
            rno_i = int(rno)
        except Exception:
            rno_i = None
        if rno_i:
            return (vc, dt, rno_i)
    # フォールバック
    dt = str(rec.get("date", ""))
    vc = str(rec.get("venue_code", "") or "")
    rno = rec.get("race_no")
    if vc and dt and rno:
        try:
            return (vc, dt, int(rno))
        except Exception:
            return None
    return None


def apply_shb_to_races(all_races):
    """all_races(レコード配列)の各 players[車番] に s/h/b を付与する。
    best-effort: 失敗しても例外は投げず、付与できたぶんだけ反映する。
    返り値: (付与レース数, 対象レース数)。"""
    if os.environ.get("KEIRIN_SHB", "1").strip() == "0":
        print("[shb] KEIRIN_SHB=0 のためS/B付与をスキップ")
        return (0, 0)
    if not isinstance(all_races, list) or not all_races:
        return (0, 0)

    # 1) 会場×日付ごとに cup_id/day を解決 (各グループの代表レース番号で)
    #    レースを (vc,dt) でまとめる
    groups = {}
    parts_map = {}
    idx = 0
    while idx < len(all_races):
        rec = all_races[idx]
        p = _race_parts(rec)
        parts_map[idx] = p
        if p is not None:
            key = p[0] + "_" + p[1]
            groups.setdefault(key, []).append((idx, p[2]))
        idx = idx + 1

    if not groups:
        print("[shb] race_id解析不可。S/B付与なし")
        return (0, 0)

    # 2) 各レースの (cup_id, day, rno) を確定 → 並列でSHB取得
    workers = 8
    try:
        workers = int(os.environ.get("KEIRIN_SHB_WORKERS", "8"))
    except Exception:
        workers = 8

    tasks = []   # (idx, cup_id, day, rno)
    for key in groups:
        members = groups[key]
        vc, dt = key.split("_", 1)
        sample_rno = members[0][1]
        cd = _shb_resolve_cup_day(vc, dt, sample_rno)
        if cd is None:
            continue
        cup_id, day = cd
        mi = 0
        while mi < len(members):
            ridx, rno = members[mi]
            tasks.append((ridx, cup_id, day, rno))
            mi = mi + 1

    if not tasks:
        print("[shb] cup_id解決できる会場がありませんでした")
        return (0, len(all_races))

    results = {}
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ex = ThreadPoolExecutor(max_workers=workers)
        futmap = {}
        ti = 0
        while ti < len(tasks):
            ridx, cup_id, day, rno = tasks[ti]
            fut = ex.submit(_shb_fetch, cup_id, day, rno)
            futmap[fut] = ridx
            ti = ti + 1
        for fut in as_completed(futmap):
            ridx = futmap[fut]
            try:
                results[ridx] = fut.result()
            except Exception:
                results[ridx] = None
        ex.shutdown(wait=True)
    except Exception:
        # 並列不可なら逐次
        ti = 0
        while ti < len(tasks):
            ridx, cup_id, day, rno = tasks[ti]
            results[ridx] = _shb_fetch(cup_id, day, rno)
            ti = ti + 1

    # 3) players[車番] に S/H/B を、レコードに race_kind を書き込む
    applied = 0
    applied_kind = 0
    applied_grade = 0
    applied_day = 0
    applied_line = 0
    for ridx in results:
        got = results[ridx]
        if not got:
            continue
        # v6: _shb_fetch の返り値が dict({"shb":..,"race_kind":..}) に変わった
        if isinstance(got, dict) and ("shb" in got or "race_kind" in got):
            shb = got.get("shb")
            rk = str(got.get("race_kind", "") or "")
            gr = str(got.get("grade", "") or "")
            dl = str(got.get("day_label", "") or "")
            pcl = got.get("pclass") or {}
            api_line = str(got.get("line", "") or "")
        else:
            shb = got
            rk = ""
            gr = ""
            dl = ""
            pcl = {}
            api_line = ""
        rec = all_races[ridx]
        # ラインが空のままなら、APIの並び予想で埋める。
        #   既に入っているものは上書きしない。
        if api_line and not str(rec.get("line", "") or "").strip():
            rec["line"] = api_line
            applied_line = applied_line + 1
        if rk and not str(rec.get("race_kind", "") or "").strip():
            rec["race_kind"] = rk
            applied_kind = applied_kind + 1
        if gr and not str(rec.get("grade", "") or "").strip():
            rec["grade"] = gr
            applied_grade = applied_grade + 1
        if dl and not str(rec.get("day_label", "") or "").strip():
            rec["day_label"] = dl
            applied_day = applied_day + 1
        players = rec.get("players")
        if isinstance(players, dict) and pcl:
            for bs2 in players:
                pd2 = players[bs2]
                if not isinstance(pd2, dict):
                    continue
                v2 = pcl.get(str(bs2))
                if v2 and not str(pd2.get("pclass", "") or "").strip():
                    pd2["pclass"] = v2
        if not shb:
            continue
        if not isinstance(players, dict):
            continue
        wrote = False
        for bs in players:
            v = shb.get(str(bs))
            pd = players[bs]
            if not isinstance(pd, dict):
                continue
            if v:
                pd["s"] = v.get("s")
                pd["h"] = v.get("h")
                pd["b"] = v.get("b")
                wrote = True
            else:
                # そのレースのSHBは取れたが該当車番が無い場合はNoneで明示
                if "s" not in pd:
                    pd["s"] = None
                if "h" not in pd:
                    pd["h"] = None
                if "b" not in pd:
                    pd["b"] = None
        if wrote:
            applied = applied + 1
    print("[shb] S/B付与: " + str(applied) + " / " + str(len(all_races)) + "R")
    print("[kind] race_kind付与: " + str(applied_kind) + " / "
          + str(len(all_races)) + "R")
    print("[line-api] 並び予想から補完: " + str(applied_line) + "R")
    print("[meta] grade付与: " + str(applied_grade)
          + " / day_label付与: " + str(applied_day)
          + " / " + str(len(all_races)) + "R")
    return (applied, len(all_races))


def races_already_has_shb(races):
    """races の中に1つでも s/h/b キーを持つ選手がいれば True。
    (付与済みキャッシュかどうかの判定。値がNoneでもキーがあれば付与済み扱い)"""
    if not isinstance(races, list):
        return False
    for rec in races:
        players = rec.get("players")
        if not isinstance(players, dict):
            continue
        for bs in players:
            pd = players[bs]
            if isinstance(pd, dict) and ("s" in pd or "b" in pd or "h" in pd):
                return True
    return False


def races_already_has_meta(races):
    """v7: grade / day_label / 選手の級班 が付いているか。
    1つでも入っていれば付与済みとみなす。"""
    if not isinstance(races, list):
        return False
    for rec in races:
        if str(rec.get("grade", "") or "").strip():
            return True
        if str(rec.get("day_label", "") or "").strip():
            return True
        players = rec.get("players")
        if isinstance(players, dict):
            for bs in players:
                pd = players[bs]
                if isinstance(pd, dict) and str(pd.get("pclass", "") or ""):
                    return True
    return False


def races_already_has_kind(races):
    """v6: race_kind が付いているキャッシュかどうか。
    1つでも入っていれば付与済みとみなす。
    S/Bだけ付いた古いキャッシュを後付け対象にするために使う。"""
    if not isinstance(races, list):
        return False
    for rec in races:
        if str(rec.get("race_kind", "") or "").strip():
            return True
    return False


def send_line(text):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()
    if not token or not user_id:
        print("[line] 未設定のため通知スキップ")
        return
    try:
        import requests
        url = "https://api.line.me/v2/bot/message/push"
        headers = {"Content-Type": "application/json",
                   "Authorization": "Bearer " + token}
        body = {"to": user_id,
                "messages": [{"type": "text", "text": text}]}
        r = requests.post(url, headers=headers,
                          data=json.dumps(body), timeout=30)
        print("[line] 送信 HTTP" + str(r.status_code))
    except Exception as e:
        print("[line] 送信失敗: " + str(e)[:80])


def cleanup_old(today_dt):
    """KEEP_DAYS日より古い事前取得ファイルを削除 (リポジトリ肥大化防止)"""
    files = glob.glob(os.path.join(OUT_DIR, "races_*.json"))
    for fp in files:
        base = os.path.basename(fp)
        ds = base[6:14]
        if not ds.isdigit():
            continue
        try:
            fdt = datetime.strptime(ds, "%Y%m%d")
        except Exception:
            continue
        if (today_dt - fdt).days > KEEP_DAYS:
            try:
                os.remove(fp)
                print("[cleanup] 削除: " + base)
            except Exception:
                pass


def venue_is_weak(race_list):
    """会場の取得が不完全か判定: レース数不足 or 歯抜け(欠番)あり"""
    if not race_list:
        return True
    if len(race_list) < MIN_RACES:
        return True
    nos = set()
    for r in race_list:
        rid = str(r.get("race_id", ""))
        if len(rid) >= 2 and rid[-2:].isdigit():
            nos.add(int(rid[-2:]))
    if not nos:
        return True
    mx = max(nos)
    i = 1
    while i < mx:
        if i not in nos:
            return True
        i += 1
    return False


def count_lineless(race_list):
    """ライン情報が空のレース数"""
    n = 0
    for r in race_list:
        if not str(r.get("line", "")).strip():
            n += 1
    return n


def find_holes(races):
    """v7: 穴のあるレースを拾う。今のところ穴はラインだけ。
    ライン未掲載の会場があると、その会場のレースは丸ごと
    予想が作れなくなる (8/25は熊本7件、8/22は松山・大垣で各9件)。
    返り値: {会場コード: [欠けているレース番号, ...]}"""
    out = {}
    if not isinstance(races, list):
        return out
    i = 0
    while i < len(races):
        r = races[i]
        i = i + 1
        if str(r.get("line", "") or "").strip():
            continue
        rid = str(r.get("race_id", ""))
        if len(rid) < 11 or not rid.isdigit():
            continue
        pc = rid[0:2]
        try:
            rno = int(rid[10:])
        except Exception:
            continue
        if pc not in out:
            out[pc] = []
        out[pc].append(rno)
    return out


def heal_lines(engine, races, tdt, date_str):
    """ラインが欠けている会場だけ取り直して、欠けていたレースに入れる。
    正常に取れているレースには触らない。
    返り値: 埋まったレース数"""
    holes = find_holes(races)
    if not holes:
        print("[line] 欠けているレースはありません")
        return 0
    names = []
    for pc in holes:
        nm = engine.CODES.get(pc, pc)
        names.append(nm + "(" + str(len(holes[pc])) + "R)")
    print("[line] ライン未掲載: " + ", ".join(names))

    # race_id -> レコード の引き表
    by_id = {}
    i = 0
    while i < len(races):
        r = races[i]
        i = i + 1
        by_id[str(r.get("race_id", ""))] = r

    filled = 0
    for pc in holes:
        pn = engine.CODES.get(pc, pc)
        vr = fetch_one_venue(engine, pc, pn, tdt, date_str)
        if not vr:
            print("[line] " + pn + ": 取り直せません")
            continue
        got = 0
        j = 0
        while j < len(vr):
            nr = vr[j]
            j = j + 1
            rid = str(nr.get("race_id", ""))
            old = by_id.get(rid)
            if old is None:
                continue
            ln = str(nr.get("line", "") or "").strip()
            if not ln:
                continue
            if str(old.get("line", "") or "").strip():
                continue      # 既にあるものは触らない
            old["line"] = ln
            got = got + 1
        filled = filled + got
        print("[line] " + pn + ": " + str(got) + "R 埋めた"
              + (" (残り" + str(len(holes[pc]) - got) + "R)"
                 if got < len(holes[pc]) else ""))
    return filled


# v8: 会場ごとの取得結果を数えておく。
#   check_venue_open が偽を返すと無言で終わるため、
#   43会場すべてが空でも「開催会場なし」としか出ず、
#   何が起きたのか分からなかった (8/29-8/31 がこれ)。
_FETCH_STAT = {"closed": 0, "err_open": 0, "empty": 0,
               "ok": 0, "err_race": 0, "api": 0}
_CODE_TO_NAME = {}


def fetch_one_venue(engine, pc, pn, tdt, date_str):
    """1会場分の取得。少レース時は1回だけ再取得して多い方を採用"""
    import time
    try:
        res = engine.check_venue_open(pc, pn, tdt)
    except Exception as e:
        _FETCH_STAT["err_open"] = _FETCH_STAT["err_open"] + 1
        print("[warn] " + pn + " 開催確認失敗: " + str(e)[:60])
        return None
    if not res:
        _FETCH_STAT["closed"] = _FETCH_STAT["closed"] + 1
        return None
    pc2, pn2, bd, dy = res
    try:
        vr = engine.fetch_venue_races(pc2, pn2, bd, dy, tdt, date_str)
    except Exception as e:
        print("[warn] " + pn + " 取得失敗: " + str(e)[:60])
        vr = []
    if vr and not venue_is_weak(vr):
        _FETCH_STAT["ok"] = _FETCH_STAT["ok"] + 1
        return vr
    # 取りこぼし疑い (少レース or 歯抜け) → 待機して再取得
    print("[retry] " + pn + ": " + str(len(vr) if vr else 0)
          + "R (少レース/歯抜け) のため再取得します")
    time.sleep(RETRY_WAIT)
    try:
        vr2 = engine.fetch_venue_races(pc2, pn2, bd, dy, tdt, date_str)
    except Exception as e:
        print("[warn] " + pn + " 再取得失敗: " + str(e)[:60])
        vr2 = []
    if vr2 and (not vr or len(vr2) > len(vr)):
        _FETCH_STAT["ok"] = _FETCH_STAT["ok"] + 1
        return vr2
    if vr:
        _FETCH_STAT["ok"] = _FETCH_STAT["ok"] + 1
        return vr
    _FETCH_STAT["empty"] = _FETCH_STAT["empty"] + 1
    return None


# ============================================================
# v9: ウィンチケットAPIだけで1会場ぶんの出走表を作る。
#
#   従来の取得は engine.check_venue_open() に頼っており、
#   それが偽を返すと 43会場すべてが無言で落ちる。
#   8/29〜8/31 が丸ごと取れなかったのはこれで、原因も追えなかった。
#
#   一方、同じAPIから4月の1,260レースを取ったときは
#   ライン・発走・選手のいずれも欠けが0件だった。過去日でも引ける。
#   そこで、従来経路が0件のときはAPIから組み立てる。
#
#   出力は既存キャッシュと同じ形にする。実物と突き合わせて確認した:
#     full_info '日高 裕太/静岡/24歳/121期/100.12点'
#     h1        '8/27 9'  複数日は '8/26 5・6'  無ければ 'なし'
#     h2/h3     '四日市 Ｆ１ 8/17 3・1・1'
#     weather   '天気:曇 風速:2m 風向:西'
# ============================================================
_GRADE_WIDE = {"F1": "Ｆ１", "F2": "Ｆ２", "G1": "Ｇ１",
               "G2": "Ｇ２", "G3": "Ｇ３"}


def _md(date8):
    """20260817 -> 8/17"""
    d = str(date8 or "")
    if len(d) != 8 or not d.isdigit():
        return ""
    return str(int(d[4:6])) + "/" + str(int(d[6:8]))


def _api_full_info(pl, rec):
    """'姓 名/府県/24歳/121期/100.12点'"""
    ln = str((pl or {}).get("lastName", "") or "")
    fn = str((pl or {}).get("firstName", "") or "")
    if ln and fn:
        name = ln + " " + fn
    else:
        name = str((pl or {}).get("name", "") or "")
    pref = str((pl or {}).get("prefecture", "") or "")
    age = (pl or {}).get("age")
    term = (pl or {}).get("term")
    pt = (rec or {}).get("racePoint")
    parts = [name, pref]
    parts.append((str(age) + "歳") if age is not None else "")
    parts.append((str(term) + "期") if term is not None else "")
    parts.append((str(pt) + "点") if pt is not None else "")
    return "/".join(parts)


def _orders_str(results):
    """着順を ・ でつなぐ。着外や失格は数字のまま。"""
    out = []
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        o = r.get("order")
        if o is None:
            continue
        out.append(str(o))
    return "・".join(out)


def _cup_meta(cups_by_id, cup_id, code_to_name):
    """cupId から (会場名, 全角グレード, 開始日M/D) を作る"""
    cid = str(cup_id or "")
    c = cups_by_id.get(cid) or {}
    vname = ""
    vid = str(c.get("venueId", "") or "")
    if vid:
        vname = code_to_name.get(vid, "")
    if not vname and len(cid) >= 10:
        vname = code_to_name.get(cid[8:10], "")
    g = _GRADE_WIDE.get(grade_of(c.get("grade")), "")
    start = str(c.get("startDate", "") or "")
    if not start and len(cid) >= 10:
        # cupId は YYYYMMDD + 会場2桁
        start = cid[:8]
    return (vname, g, _md(start))


def _api_history(rec, cur_cup_id, cups_by_id, code_to_name):
    """h1 (今場所) / h2 / h3 (前2場所) を作る"""
    h1 = "なし"
    hist = []
    for cup in (rec.get("latestCupResults") or []):
        if not isinstance(cup, dict):
            continue
        cid = str(cup.get("cupId", "") or "")
        rr = cup.get("raceResults") or []
        od = _orders_str(rr)
        if not od:
            continue
        if cid == str(cur_cup_id):
            # 今場所は「開始日 着順」だけ
            vname, g, md = _cup_meta(cups_by_id, cid, code_to_name)
            first = ""
            # raceId は RR(2) + 会場(2) + YYYYMMDD(8)。日付は末尾8桁。
            for r in rr:
                if isinstance(r, dict) and r.get("raceId"):
                    rid = str(r["raceId"])
                    if len(rid) >= 12:
                        first = _md(rid[-8:])
                        break
            if not first:
                first = md
            h1 = (first + " " + od).strip()
            continue
        vname, g, md = _cup_meta(cups_by_id, cid, code_to_name)
        label = " ".join([x for x in (vname, g, md) if x])
        hist.append((cid, (label + " " + od).strip()))
    hist.sort(reverse=True)          # 新しい開催が先
    h2 = hist[0][1] if len(hist) > 0 else ""
    h3 = hist[1][1] if len(hist) > 1 else ""
    return (h1, h2, h3)


def _api_weather(race):
    w = str((race or {}).get("weather", "") or "").strip()
    ws = str((race or {}).get("windSpeed", "") or "").strip()
    wd = str((race or {}).get("windDirection", "") or "").strip()
    if not w:
        w = "不明"
    else:
        # 「曇り」→「曇」。既存キャッシュは1文字表記。
        w = w.replace("り", "")
    if ws:
        try:
            f = float(ws)
            ws = str(int(f)) if f == int(f) else str(f)
        except Exception:
            pass
        ws2 = ws + "m"
    else:
        ws2 = "--"
    wd2 = wd if wd else "--"
    return "天気:" + w + " 風速:" + ws2 + " 風向:" + wd2


def build_races_from_api(pc, pn, date_str, code_to_name):
    """1会場ぶんの出走表をAPIから作る。取れなければ []。"""
    cd = _shb_resolve_cup_day(pc, date_str, 1)
    if cd is None:
        return []
    cup_id, day = cd

    out = []
    miss = 0
    rno = 1
    while rno <= 12 and miss < 3:
        data = None
        try:
            data = _shb_http_json(_shb_api_url(cup_id, day, rno))
        except Exception:
            data = None
        if not isinstance(data, dict) or not data.get("entries"):
            miss = miss + 1
            rno = rno + 1
            continue
        miss = 0

        race = data.get("race") or {}
        sch = data.get("schedule") or {}
        cid = str(sch.get("cupId", "") or "")
        cups_by_id = {}
        for c in (data.get("cups") or []):
            if isinstance(c, dict):
                cups_by_id[str(c.get("id", ""))] = c
        cup = cups_by_id.get(cid) or {}

        pl_by_id = {}
        for pl in (data.get("players") or []):
            pl_by_id[str(pl.get("id"))] = pl
        rec_by_id = {}
        for rc in (data.get("records") or []):
            rec_by_id[str(rc.get("playerId"))] = rc

        players = {}
        for e in (data.get("entries") or []):
            if not isinstance(e, dict):
                continue
            bike = e.get("number")
            if bike is None:
                bike = e.get("bracketNumber")
            if bike is None:
                continue
            pid = str(e.get("playerId"))
            pl = pl_by_id.get(pid) or {}
            rc = rec_by_id.get(pid) or {}
            h1, h2, h3 = _api_history(rc, cid, cups_by_id, code_to_name)
            players[str(int(bike))] = {
                "full_info": _api_full_info(pl, rc),
                "h1": h1, "h2": h2, "h3": h3,
                "style": str(rc.get("style", "") or ""),
                "pclass": player_class_of(e.get("playerCurrentTermClass"),
                                          e.get("playerCurrentTermGroup")),
                "s": rc.get("standing"),
                "h": rc.get("home") if rc.get("hasHome") is True else None,
                "b": rc.get("back"),
            }

        out.append({
            "race_id": pc + date_str + ("%02d" % rno),
            "date": date_str,
            "place": pn,
            "race_no": rno,
            "post_time": _api_post_time(race.get("startAt")),
            "line": _line_from_api(data),
            "grade": grade_of(cup.get("grade")),
            "race_kind": build_race_kind(race.get("class"),
                                         race.get("raceType")),
            "day_label": day_label_of(sch.get("day"), cup.get("duration")),
            "weather": _api_weather(race),
            "players": players,
        })
        rno = rno + 1
    return out


def _api_post_time(ts):
    """発走時刻を日本時間の HH:MM で。環境のTZに左右されないよう固定。"""
    try:
        from datetime import timezone as _tz, timedelta as _td
        jst = _tz(_td(hours=9))
        return datetime.fromtimestamp(int(ts), jst).strftime("%H:%M")
    except Exception:
        return "--:--"


def _probe_open(engine, tdt, date_str):
    """0件だったときに、1会場だけ詳しく試して手掛かりを出す。

    どこで止まっているのかが分からないと直しようがないので、
    check_venue_open の戻り値をそのまま見せる。
    """
    try:
        codes = list(engine.CODES.items())
    except Exception:
        return
    print("[probe] 代表3会場で開催確認を試します (" + date_str + ")")
    n = 0
    for pc, pn in codes:
        if n >= 3:
            break
        n = n + 1
        try:
            res = engine.check_venue_open(pc, pn, tdt)
            print("  %s %-6s -> %s" % (pc, pn, repr(res)[:90]))
        except Exception as e:
            print("  %s %-6s -> 例外 %s: %s"
                  % (pc, pn, type(e).__name__, str(e)[:70]))


def race_no_of(rec):
    """race_id の末尾からレース番号を取り出す"""
    rid = str(rec.get("race_id", ""))
    if len(rid) >= 11 and rid.isdigit():
        try:
            return int(rid[10:])
        except Exception:
            return 0
    return 0


def venue_code_of(rec):
    rid = str(rec.get("race_id", ""))
    if len(rid) >= 2:
        return rid[:2]
    return ""


def race_is_ready(rec):
    """v8: そのレースの買い目を作れる状態か。
    発走時刻とラインの2つだけを見る。
    S/B は条件に入れない。最後まで付かないレースがあると、
    そのレースが永久に未readyのまま毎回の再取得を呼び続けるため。
    S/B は別途、発走時刻を過ぎるまで毎回取りに行く。"""
    pt = str(rec.get("post_time", "") or "").strip()
    if (not pt) or ("-" in pt):
        return False
    if not str(rec.get("line", "") or "").strip():
        return False
    return True


def heal_tries_of(rec):
    try:
        return int(rec.get("_heal_tries", 0) or 0)
    except Exception:
        return 0


def race_should_heal(rec):
    """v8: 補完を試みる価値があるか。

    発走時刻で切ると、過去日のラインが永久に埋まらない。
    ラインは元サイトに載っているので、取りに行けば埋まる見込みがある。
    そこで「発走したか」ではなく「何回試したか」で打ち切る。
    MAX_HEAL_TRIES 回試して駄目なら、そのレースはもう追わない。
    これなら過去日も補完でき、かつ無限には繰り返さない。"""
    if race_is_ready(rec):
        return False
    return heal_tries_of(rec) < MAX_HEAL_TRIES


def summarize_ready(races):
    """readyの数、まだ追う未readyの数、諦めた数"""
    n_ready = 0
    n_wait = 0
    n_gone = 0
    for r in races:
        if race_is_ready(r):
            n_ready = n_ready + 1
        elif race_should_heal(r):
            n_wait = n_wait + 1
        else:
            n_gone = n_gone + 1
    return n_ready, n_wait, n_gone


def merge_races(old_list, new_list):
    """v8: 会場の集合は減らさない。race_id をキーに和集合を取る。
    既にreadyなレースは新しい取得で上書きしない
    (取得元が一時的に空を返しても、埋まっていたラインを失わないため)。
    返り値: (統合後のリスト, 新規追加数, 更新数)"""
    by_id = {}
    order = []
    for r in (old_list or []):
        rid = str(r.get("race_id", ""))
        if not rid:
            continue
        if rid not in by_id:
            order.append(rid)
        by_id[rid] = r
    added = 0
    updated = 0
    for r in (new_list or []):
        rid = str(r.get("race_id", ""))
        if not rid:
            continue
        if rid not in by_id:
            by_id[rid] = r
            order.append(rid)
            added = added + 1
            continue
        cur = by_id[rid]
        if race_is_ready(cur):
            continue          # 既に揃っているものには触らない
        if race_is_ready(r) or (not str(cur.get("line", "") or "").strip()
                                and str(r.get("line", "") or "").strip()):
            by_id[rid] = r
            updated = updated + 1
    out = []
    for rid in order:
        out.append(by_id[rid])
    return out, added, updated


def main():
    """v8: 日単位の「取得済みならスキップ」を廃止した。

    v7 は、キャッシュがあると『キャッシュに載っている会場』だけを見て
    完全性を判定していた。そのため、最初の取得時にまだ掲載されていない
    会場 (ミッドナイト等) は、以後どれだけ実行しても永久に現れなかった。
    8/26 が 61R のまま固まり、青森・宇都宮が丸ごと欠けたのはこれが原因。

    v8 は毎回すべての会場を確認し、キャッシュに無い会場は取りに行く。
    レース単位で ready (発走時刻あり かつ ライン情報あり) を見て、
    未readyのレースだけを、発走時刻を過ぎるまで追い続ける。
    """
    date_str = os.environ.get("KEIRIN_DATE", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    try:
        tdt = datetime.strptime(date_str, "%Y%m%d")
    except Exception:
        print("[error] 日付不正: " + date_str)
        sys.exit(1)

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    out_path = os.path.join(OUT_DIR, "races_" + date_str + ".json")

    try:
        import predict_v14_wind_unified as engine
    except Exception as e:
        print("[error] engine読み込み失敗: " + str(e))
        print("  → predict_v14_wind_unified.py (と依存ファイル) を"
              "リポジトリ直下に置いてください")
        sys.exit(1)

    # ---- 既存キャッシュを読む ----
    races = []
    if os.path.exists(out_path):
        try:
            f = open(out_path, "r", encoding="utf-8")
            races = json.load(f)
            f.close()
        except Exception:
            races = []
        if not isinstance(races, list):
            races = []

    have_pc = {}
    for r in races:
        pc = venue_code_of(r)
        if pc:
            have_pc.setdefault(pc, []).append(r)

    if races:
        n_ready, n_wait, n_gone = summarize_ready(races)
        print("[cache] " + str(len(races)) + "R / " + str(len(have_pc))
              + "会場  ready " + str(n_ready)
              + " / 待ち " + str(n_wait)
              + " / 発走済み未ready " + str(n_gone))

    # ---- 会場の確認は毎回すべて行う ----
    #   キャッシュに無い会場、レース数が不足している会場だけ取りに行く。
    #   ここを省くと、後から掲載された会場に永久に気づけない。
    # API側で会場名を引くための対応表 (engine の CODES をそのまま使う)
    global _CODE_TO_NAME
    _CODE_TO_NAME = {}
    try:
        for _pc in engine.CODES:
            _CODE_TO_NAME[_pc] = engine.CODES[_pc]
    except Exception:
        pass

    changed = False
    n_added_total = 0
    n_new_venue = 0
    for k in _FETCH_STAT:
        _FETCH_STAT[k] = 0
    n_scan = 0
    for pc in engine.CODES:
        pn = engine.CODES[pc]
        cur = have_pc.get(pc)
        if cur and not venue_is_weak(cur):
            continue          # その会場はレースが揃っている
        n_scan = n_scan + 1
        # v9: ウィンチケットAPIを主にする。
        #   従来経路は check_venue_open が偽を返すと無言で落ち、
        #   8/29〜8/31 は43会場すべてが空になった。原因も追えていない。
        #   一方APIは、4月の1,260レースでライン・発走・選手のいずれも
        #   欠けが0件だった。過去日も引ける。
        #   そこでAPIを先に試し、取れなかった会場だけ従来経路に回す。
        vr = []
        try:
            vr = build_races_from_api(pc, pn, date_str, _CODE_TO_NAME)
        except Exception as e:
            print("[warn] " + pn + " API取得で例外: " + str(e)[:60])
            vr = []
        if vr and not venue_is_weak(vr):
            _FETCH_STAT["api"] = _FETCH_STAT.get("api", 0) + 1
        else:
            # APIで取れない / レース数が足りないときだけ従来経路
            old_vr = fetch_one_venue(engine, pc, pn, tdt, date_str)
            if old_vr and (not vr or len(old_vr) > len(vr)):
                print("[legacy] " + pn + ": " + str(len(old_vr))
                      + "R (従来経路で補いました)")
                vr = old_vr
            elif vr:
                _FETCH_STAT["api"] = _FETCH_STAT.get("api", 0) + 1
        if not vr:
            continue
        if not cur:
            n_new_venue = n_new_venue + 1
            print("[new] " + pn + ": " + str(len(vr)) + "R (キャッシュに無かった会場)")
        races, added, updated = merge_races(races, vr)
        if added or updated:
            changed = True
            n_added_total = n_added_total + added
        have_pc = {}
        for r in races:
            pc2 = venue_code_of(r)
            if pc2:
                have_pc.setdefault(pc2, []).append(r)

    print("[scan] %d会場を確認: API %d / 従来経路 %d "
          "/ 開催なし %d / 空 %d / 開催確認エラー %d"
          % (n_scan, _FETCH_STAT.get("api", 0), _FETCH_STAT["ok"],
             _FETCH_STAT["closed"], _FETCH_STAT["empty"],
             _FETCH_STAT["err_open"]))

    if not races:
        print("[fail] 開催会場なし or 取得0件 (メンテナンス中の可能性)")
        print("  → APIでも従来経路でも取れませんでした。")
        print("  → その日に開催が無いか、取得元に届いていない可能性が"
              "あります。")
        _probe_open(engine, tdt, date_str)
        if os.environ.get("KEIRIN_NOTIFY_FAIL", "") == "1":
            send_line("【競輪】" + date_str
                      + " 当日基本データの事前取得に失敗しました (0件)。"
                      + "アプリ側は従来のスクレイピングにフォールバックします。")
        return

    # ---- 未readyのレースを埋める ----
    #   発走時刻ではなく試行回数で打ち切る。過去日のラインも埋めたいため。
    pending = []
    for r in races:
        if race_should_heal(r):
            pending.append(r)

    filled = 0
    if pending:
        print("[line] 未ready(試行 " + str(MAX_HEAL_TRIES) + "回以内): "
              + str(len(pending)) + "R → 埋めにいきます")
        try:
            filled = heal_lines(engine, races, tdt, date_str)
        except Exception as e:
            print("[line] 穴埋めで例外: " + str(e)[:80])
        # 埋まらなかったレースの試行回数を進める。
        #   これをしないと、取れないレースを毎回いつまでも取りに行く。
        for r in pending:
            if not race_is_ready(r):
                r["_heal_tries"] = heal_tries_of(r) + 1
        changed = True
    else:
        n_giveup = 0
        for r in races:
            if not race_is_ready(r):
                n_giveup = n_giveup + 1
        if n_giveup:
            print("[line] 未readyが " + str(n_giveup) + "R 残っていますが、"
                  + "試行上限に達したため追いません")
        else:
            print("[line] すべて揃っています")

    # ---- S/B・race_kind・grade は発走前が残っている限り毎回試す ----
    _had_meta = races_already_has_meta(races)
    _had_kind = races_already_has_kind(races)
    applied = 0
    try:
        applied, total = apply_shb_to_races(races)
    except Exception as e:
        print("[shb] 付与で例外: " + str(e)[:80])
    if applied > 0:
        changed = True
    if races_already_has_meta(races) != _had_meta:
        changed = True
    if races_already_has_kind(races) != _had_kind:
        changed = True

    # ---- 保存 ----
    if changed:
        f = open(out_path, "w", encoding="utf-8")
        json.dump(races, f, ensure_ascii=False)
        f.close()
        print("[done] 保存: " + str(len(races)) + "R / "
              + str(len(have_pc)) + "会場"
              + " (新会場 " + str(n_new_venue)
              + " / 新規レース " + str(n_added_total)
              + " / ライン " + str(filled)
              + " / S・B " + str(applied) + ")"
              + " → " + out_path)
    else:
        print("[done] 変化なし (" + str(len(races)) + "R)")

    n_ready, n_wait, n_gone = summarize_ready(races)
    print("[ready] " + str(n_ready) + "R 完成"
          + " / 次回も追う " + str(n_wait) + "R"
          + " / 試行上限で打ち切り " + str(n_gone) + "R")
    if n_gone:
        print("::warning::" + str(MAX_HEAL_TRIES)
              + "回試しても揃わなかったレースが " + str(n_gone) + "R あります")

    cleanup_old(tdt)


def _build_date_list():
    """環境変数から処理対象の日付リスト(YYYYMMDD)を構築する。
      KEIRIN_START  取得開始日 (空欄=当日)
      KEIRIN_END    取得終了日 (空欄=開始日と同じ=1日のみ)
    ルール:
      - 開始日 空欄                  → 当日のみ
      - 開始日 入力 / 終了日 空欄     → 開始日のみ
      - 開始日 入力 / 終了日 入力     → 開始日〜終了日 (両端含む)
    逆順は昇順に補正。不正な日付は当日にフォールバック。
    互換: KEIRIN_DATE が単独で入っていれば従来どおりその1日。"""
    def _norm(s):
        s = (s or "").strip()
        if not s:
            return None
        try:
            datetime.strptime(s, "%Y%m%d")
            return s
        except Exception:
            print("[warn] 日付不正のため無視: " + s)
            return None

    start = _norm(os.environ.get("KEIRIN_START", ""))
    end = _norm(os.environ.get("KEIRIN_END", ""))

    # 後方互換: 旧 KEIRIN_DATE 単独指定
    if start is None and end is None:
        legacy = _norm(os.environ.get("KEIRIN_DATE", ""))
        if legacy:
            return [legacy]

    # 開始日が空欄 → 当日のみ (終了日は無視)
    if start is None:
        return [datetime.now().strftime("%Y%m%d")]

    # 終了日が空欄 → 開始日のみ
    if end is None:
        return [start]

    # 両方あり → 範囲 (両端含む・逆順は補正)
    a = datetime.strptime(start, "%Y%m%d")
    b = datetime.strptime(end, "%Y%m%d")
    if a > b:
        a, b = b, a
    out = []
    cur = a
    guard = 0
    while cur <= b and guard < 400:
        out.append(cur.strftime("%Y%m%d"))
        cur = cur + timedelta(days=1)
        guard = guard + 1
    return out


def run_all():
    """対象日を構築し、各日について main() を実行する。
    main() は KEIRIN_DATE を読むので、ループ内で環境変数を差し替える。"""
    dates = _build_date_list()
    if len(dates) == 1:
        # 単一日は従来どおり (環境変数はそのまま main() が読む)
        os.environ["KEIRIN_DATE"] = dates[0]
        main()
        return
    print("[multi] 複数日取得: " + ", ".join(dates) + " (" + str(len(dates)) + "日)")
    di = 0
    while di < len(dates):
        ds = dates[di]
        di = di + 1
        print("")
        print("======== " + ds + " (" + str(di) + "/" + str(len(dates)) + ") ========")
        os.environ["KEIRIN_DATE"] = ds
        try:
            main()
        except SystemExit:
            # main() 内 sys.exit を1日分のスキップに留め、他の日を続行
            print("[warn] " + ds + " は処理中断 (次の日へ)")
        except Exception as e:
            print("[warn] " + ds + " で例外: " + str(e)[:80] + " (次の日へ)")
    print("")
    print("[multi] 全日完了: " + str(len(dates)) + "日")


if __name__ == "__main__":
    run_all()

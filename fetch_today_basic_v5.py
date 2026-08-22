"""
fetch_today_basic_v6.py
GitHub Actions 用: 当日のレース基本データ (出走表) を事前取得して
today_cache/races_YYYYMMDD.json として保存する。

アプリ (託宣) は託宣ボタン押下時にこのファイルをダウンロードするだけで
全会場スクレイピングを省略でき、即計算を開始できる。

v6からの変更点 (race_kind付与):
  - winticket のレース単位APIに級班と種別が入っていることを実測で確認した。
      race.class    = "A級" / "S級" / "L級"
      race.raceType = "準決勝" / "チャレンジ予選" / "ガールズ決勝" / "一予選" ...
    S/B取得で既に同じAPIを叩いているので、追加の通信は要らない。
  - これを組み立てて各レコードに race_kind を持たせる。
    当日データには従来 race_kind が無く、「決勝なら◯◯」という
    条件判定が当日はできなかった。
  - 組み立て規則 (実データ90レースで確認):
      raceType に「チャレンジ」「ガールズ」等が含まれる場合、
      そこに級班が既に入っているので class を足すと二重になる。
        A級 + チャレンジ予選 -> チャレンジ予選   (足さない)
        L級 + ガールズ決勝   -> ガールズ決勝     (足さない)
        A級 + 準決勝        -> Ａ級準決勝       (足す)
      DB側は全角(Ａ級/Ｓ級)なので、半角のclassは全角に直す。
  - 既存キャッシュに race_kind が無ければ後付けして保存し直す
    (S/Bと同じ仕組み)。

v5からの変更点 (S/B付与):
  - 保存直前に各レースの players[車番] へ winticket APIから取得した
    S(先頭通過回数)/H(ホーム)/B(バック取り回数) を付与する。
    出走表のSR-逃間に表示するS/B列が当日キャッシュでも埋まる。
  - SHB取得は backfill_shb_github_v3.py と同じロジック (extract_shb/
    resolve_cup_day/fetch_shb_fast) を移植。会場×日付ごとに cup_id/day を
    1度だけ解決してキャッシュし、各レースを並列取得する。
  - 取得失敗/未掲載のレースは s/h/b=None のまま (アプリ側は「-」表示)。
    SHB付与で例外が出ても本体の保存は必ず行う (付与は best-effort)。
  - 「取得済み(全会場正常)」で早期skipする場合でも、既存キャッシュに
    S/Bが未付与なら付与して保存し直す (races_already_has_shb で判定)。
    朝の実行でS/B無しキャッシュが先に出来ても、後続実行で後付けされる。

旧版からの変更点 (S/B付与・初版メモ):

v3からの変更点 (ライン補完モード):
  - KEIRIN_LINE_HEAL=1 のとき、ライン情報が空の会場を再取得して補完
    (ミッドナイト開催はラインの掲載が午後のため、朝の取得では空になる。
     午後14時/15時の実行でこのモードを使い、ラインを取り直す)
  - ライン充足率が改善した場合のみ差し替え

v2からの変更点:
  - 会場の健全性判定を「レース数」から「歯抜け(欠番)検知」に強化
    (7Rのうち5Rだけ取れている、のようなケースも検知して再取得)
  - engine側(r2)のリトライ+二次取得と合わせた二重の保険

v1からの変更点 (自己修復):
  - 取得レース数が MIN_RACES (5) 未満の会場は取りこぼしとみなし、その場で再取得
  - 保存済みファイルがある場合も、少レース会場だけ再取得して差し替え
    (6:00の取得で青森1Rのような取りこぼしが起きても、6:30の保険実行が自動修復)

必要ファイル: predict_v14_wind_unified.py をリポジトリ直下に配置すること。
(import失敗時にログへ出るモジュール名のファイルも順次追加)

環境変数:
  KEIRIN_START        取得開始日 YYYYMMDD (空欄=当日)
  KEIRIN_END          取得終了日 YYYYMMDD (空欄=開始日と同じ=1日のみ)
                      開始日入力かつ終了日入力で 開始日〜終了日 を一括取得
  KEIRIN_DATE         (旧) 単一日指定。START/END未指定時のみ有効 (後方互換)
  KEIRIN_NOTIFY_FAIL  '1' なら取得0件時にLINE通知
  KEIRIN_SHB          '0' でS/B付与をスキップ (既定は付与する)
  KEIRIN_SHB_WORKERS  S/B並列取得数 (既定8)
  LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID
"""

import os
import sys
import json
import glob
from datetime import datetime, timedelta

OUT_DIR = "today_cache"
KEEP_DAYS = 3
MIN_RACES = 5   # 1会場あたりの最低レース数。未満なら取りこぼしとみなす
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
    """S/H/B と race_kind をまとめて返す。
    返り値: {"shb": {...} or None, "race_kind": str}"""
    url = _shb_api_url(cup_id, day, rno)
    try:
        data = _shb_http_json(url)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    out = {"shb": _shb_extract(data), "race_kind": ""}
    race = data.get("race")
    if isinstance(race, dict):
        out["race_kind"] = build_race_kind(race.get("class"),
                                           race.get("raceType"))
    return out


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
    for ridx in results:
        got = results[ridx]
        if not got:
            continue
        # v6: _shb_fetch の返り値が dict({"shb":..,"race_kind":..}) に変わった
        if isinstance(got, dict) and ("shb" in got or "race_kind" in got):
            shb = got.get("shb")
            rk = str(got.get("race_kind", "") or "")
        else:
            shb = got
            rk = ""
        rec = all_races[ridx]
        if rk and not str(rec.get("race_kind", "") or "").strip():
            rec["race_kind"] = rk
            applied_kind = applied_kind + 1
        if not shb:
            continue
        players = rec.get("players")
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


def fetch_one_venue(engine, pc, pn, tdt, date_str):
    """1会場分の取得。少レース時は1回だけ再取得して多い方を採用"""
    import time
    try:
        res = engine.check_venue_open(pc, pn, tdt)
    except Exception as e:
        print("[warn] " + pn + " 開催確認失敗: " + str(e)[:60])
        return None
    if not res:
        return None
    pc2, pn2, bd, dy = res
    try:
        vr = engine.fetch_venue_races(pc2, pn2, bd, dy, tdt, date_str)
    except Exception as e:
        print("[warn] " + pn + " 取得失敗: " + str(e)[:60])
        vr = []
    if vr and not venue_is_weak(vr):
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
        return vr2
    return vr if vr else None


def main():
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

    # 既存ファイルがある場合: 少レース会場だけ再取得して自己修復
    if os.path.exists(out_path):
        races = []
        try:
            f = open(out_path, "r", encoding="utf-8")
            races = json.load(f)
            f.close()
        except Exception:
            races = []
        if isinstance(races, list) and races:
            by_pc = {}
            for r in races:
                rid = str(r.get("race_id", ""))
                if len(rid) >= 2:
                    by_pc.setdefault(rid[:2], []).append(r)
            line_heal = os.environ.get("KEIRIN_LINE_HEAL", "").strip() == "1"
            if line_heal:
                # ライン補完モード: ライン空きのある会場を再取得し、
                # ライン充足が改善した場合のみ差し替える
                targets = [pc for pc in by_pc if count_lineless(by_pc[pc]) > 0]
                if not targets:
                    print("[line] 全会場ライン取得済み。補完不要")
                    cleanup_old(tdt)
                    return
                print("[line] ライン未掲載の会場を再取得: "
                      + ", ".join([engine.CODES.get(pc, pc) + "(空"
                                   + str(count_lineless(by_pc[pc])) + "R)"
                                   for pc in targets]))
                improved = False
                for pc in targets:
                    pn = engine.CODES.get(pc, pc)
                    vr = fetch_one_venue(engine, pc, pn, tdt, date_str)
                    if vr and count_lineless(vr) < count_lineless(by_pc[pc]):
                        by_pc[pc] = vr
                        improved = True
                        print("[ok] " + pn + ": ライン補完 (残り空"
                              + str(count_lineless(vr)) + "R)")
                if not improved:
                    print("[line] まだ掲載されていないようです (現状維持)")
                    cleanup_old(tdt)
                    return
                all_races = []
                for pc in by_pc:
                    all_races.extend(by_pc[pc])
                apply_shb_to_races(all_races)
                f = open(out_path, "w", encoding="utf-8")
                json.dump(all_races, f, ensure_ascii=False)
                f.close()
                print("[done] ライン補完保存: " + str(len(all_races))
                      + "R → " + out_path)
                cleanup_old(tdt)
                return
            weak = [pc for pc in by_pc if venue_is_weak(by_pc[pc])]
            if not weak:
                # 全会場正常。ただし既存キャッシュにS/B未付与なら付与して保存し直す。
                if races_already_has_shb(races) and races_already_has_kind(races):
                    print("[skip] 取得済み: " + str(len(races))
                          + "R (全会場正常・S/B・race_kind付与済み)")
                    cleanup_old(tdt)
                    return
                print("[shb] 既存キャッシュに未付与の項目あり → 後付けします ("
                      + str(len(races)) + "R)")
                _had_kind = races_already_has_kind(races)
                applied, total = apply_shb_to_races(races)
                _now_kind = races_already_has_kind(races)
                # S/B が付いたか、race_kind が新たに付いたなら保存し直す
                if applied > 0 or (_now_kind and not _had_kind):
                    f = open(out_path, "w", encoding="utf-8")
                    json.dump(races, f, ensure_ascii=False)
                    f.close()
                    print("[done] 後付け保存: S/B " + str(applied) + "/"
                          + str(total) + "R, race_kind "
                          + ("有" if _now_kind else "無") + " → " + out_path)
                else:
                    print("[shb] 付与0件 (winticket未掲載等)。現状維持")
                cleanup_old(tdt)
                return
            print("[heal] 少レース会場を再取得: "
                  + ", ".join([engine.CODES.get(pc, pc) + "("
                               + str(len(by_pc[pc])) + "R)" for pc in weak]))
            healed = False
            for pc in weak:
                pn = engine.CODES.get(pc, pc)
                vr = fetch_one_venue(engine, pc, pn, tdt, date_str)
                better = False
                if vr:
                    if len(vr) > len(by_pc[pc]):
                        better = True
                    elif not venue_is_weak(vr) and venue_is_weak(by_pc[pc]):
                        better = True
                if better:
                    by_pc[pc] = vr
                    healed = True
                    print("[ok] " + pn + ": " + str(len(vr)) + "R に修復")
            if not healed:
                print("[heal] 修復できる会場はありませんでした (現状維持)")
                cleanup_old(tdt)
                return
            all_races = []
            for pc in by_pc:
                all_races.extend(by_pc[pc])
            apply_shb_to_races(all_races)
            f = open(out_path, "w", encoding="utf-8")
            json.dump(all_races, f, ensure_ascii=False)
            f.close()
            print("[done] 修復保存: " + str(len(all_races)) + "R → " + out_path)
            cleanup_old(tdt)
            return

    all_races = []
    venues = []
    for pc in engine.CODES:
        pn = engine.CODES[pc]
        vr = fetch_one_venue(engine, pc, pn, tdt, date_str)
        if vr:
            all_races.extend(vr)
            venues.append(pn)
            print("[ok] " + pn + ": " + str(len(vr)) + "R")

    if not all_races:
        print("[fail] 開催会場なし or 取得0件 (メンテナンス中の可能性)")
        if os.environ.get("KEIRIN_NOTIFY_FAIL", "") == "1":
            send_line("【競輪】" + date_str
                      + " 当日基本データの事前取得に失敗しました (0件)。"
                      + "アプリ側は従来のスクレイピングにフォールバックします。")
        return

    apply_shb_to_races(all_races)
    f = open(out_path, "w", encoding="utf-8")
    json.dump(all_races, f, ensure_ascii=False)
    f.close()
    print("[done] " + date_str + ": " + str(len(venues)) + "会場 "
          + str(len(all_races)) + "R → " + out_path)
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

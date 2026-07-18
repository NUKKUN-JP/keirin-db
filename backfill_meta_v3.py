# -*- coding: utf-8 -*-
# ============================================================
# メタ一括補填 v3 (grade/race_kind/day_label/series_name)
# ============================================================
# 方針(性能重視):
#   1. DBを1回走査し、grade欠損レコードの race_id を収集(対象期間)。
#   2. 各レースの gamboo 結果ページHTMLを取得 → extract_race_meta で4項目抽出。
#      base_date(開催初日)/day を day=1..MAXDAY で試行して解決。
#      開催(place_code+base_date)単位で解決結果をキャッシュ。
#   3. 抽出結果を全件メモリに蓄積。
#   4. 最後に DB を1回だけ読み直し、該当行を差し替えて1回で書き出す。
#      → 435MBの書き直しは「1回」だけ(既存の逐次upsertは1件ごとに書き直し=非現実的)。
#
# 途中経過は fill_cache(JSONL)に逐次保存し、再実行で続きから。
# 環境変数:
#   GB_START / GB_END : 対象期間 YYYYMMDD (既定 20220101 / 20241231)
#   GB_DB             : DBパス(既定 自動探索)
#   GB_FILLS          : 抽出結果キャッシュ(既定 grade_fills.jsonl)
#   GB_APPLY          : "1" のとき、収集済みキャッシュをDBに一括適用して終了
#   GB_MAXDAY         : 開催日数の上限(既定 8)
#   GB_LIMIT          : 1回の実行で処理するレース数上限(既定 0=無制限)
#   GB_SLEEP          : リクエスト間隔秒(既定 0.3)
#
# 実機/GitHub Actions で実行。requests + BeautifulSoup 必要(既存repoにあり)。
# ============================================================
import os
import re
import io
import json
import time

try:
    import requests as _rq
except Exception:
    _rq = None
try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None


def _env(k, d):
    v = os.environ.get(k, "")
    return v.strip() if v and v.strip() else d


START = _env("GB_START", "20220101")
END = _env("GB_END", "20241231")
FILLS_PATH = _env("GB_FILLS", "grade_fills.jsonl")
# 【v3】キャッシュに race_kind が無いものを再取得するか(既定: する)
REDO_NOKIND = _env("GB_REDO", "1") not in ("0", "false", "False")
APPLY_MODE = _env("GB_APPLY", "0") in ("1", "true", "yes")
MAXDAY = int(_env("GB_MAXDAY", "8"))
LIMIT = int(_env("GB_LIMIT", "0"))
SLEEP = float(_env("GB_SLEEP", "0.3"))

_DBC = [
    _env("GB_DB", ""),
    "/storage/emulated/0/Download/takusen/data/keirin_data_scored_v2.jsonl",
    "keirin_data_scored_v2.jsonl",
]
DB_PATH = ""
for _c in _DBC:
    if _c and os.path.exists(_c):
        DB_PATH = _c
        break

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120 Mobile Safari/537.36"),
    "Accept-Language": "ja,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml",
}
_RETRY_WAITS = [5, 15, 40]


def http_get(url):
    if _rq is None:
        return -1, ""
    attempt = 0
    while True:
        try:
            r = _rq.get(url, headers=_HEADERS, timeout=15)
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
        attempt += 1


# ---- extract_race_meta (fetch_keirin_data_v21.py より流用) ----
def extract_race_meta(html):
    out = {"grade": "", "series_name": "", "day_label": "", "race_kind": ""}
    if not html or BeautifulSoup is None:
        return out
    grade_map = {
        "Ｆ１": "F1", "Ｆ２": "F2",
        "Ｇ１": "G1", "Ｇ２": "G2", "Ｇ３": "G3",
        "ＧⅠ": "G1", "ＧⅡ": "G2", "ＧⅢ": "G3",
        "GＰ": "GP", "ＧＰ": "GP",
    }
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("title")
    if title:
        ttext = title.get_text(strip=True)
        for fw, hw in grade_map.items():
            if fw in ttext:
                out["grade"] = hw
                break
        m = re.search(r'競輪\s*[ＦＧ][\d\u2160-\u2163ⅠⅡⅢ]\w?\s*(.+?)\s*(?:初日|[０-９0-9０２-９2-9]日目|最終日)', ttext)
        if m:
            out["series_name"] = m.group(1).strip()
        m = re.search(r'(初日|[０-９0-9]+日目|最終日)', ttext)
        if m:
            day = m.group(1)
            day = day.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            out["day_label"] = day
    text = soup.get_text(separator=" ")
    text_norm = re.sub(r"\s+", " ", text)
    m = re.search(r'情報提供\s+\S+\s+\S+\s+(\S{2,12}?)\s+発走予定', text_norm)
    if m:
        kind = m.group(1).strip()
        if kind not in ("投票締切", "予想", "勝ち上がり"):
            out["race_kind"] = kind
    if not out["race_kind"]:
        m2 = re.search(r'(チャレンジ[\u4e00-\u9fff]+|Ａ級\S{1,8}|Ｓ級\S{1,8}|Ｌ級\S{1,8}|ガールズ\S{1,8})\s+発走予定', text_norm)
        if m2:
            out["race_kind"] = m2.group(1).strip()
    if not out["day_label"]:
        for h3 in soup.find_all("h3"):
            ht = h3.get_text(strip=True)
            m = re.search(r'(初日|[０-９0-9]+日目|最終日)', ht)
            if m:
                day = m.group(1)
                day = day.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
                out["day_label"] = day
                break
    return out


def _url(place_code, base_date_str, day, rno):
    return ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/result/"
            + place_code + base_date_str + "/"
            + place_code + base_date_str + str(day).zfill(2) + "00/"
            + str(rno).zfill(2) + "/")


def has_meta(rec):
    for k in ("grade", "race_kind", "day_label", "series_name"):
        v = rec.get(k)
        if v is None or str(v).strip() == "":
            return False
    return True


def load_done_ids():
    done = {}
    if not os.path.exists(FILLS_PATH):
        return done
    f = io.open(FILLS_PATH, encoding="utf-8")
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        rid = rec.get("race_id")
        if rid:
            # 後勝ち: 同じrace_idが複数あれば新しい行(後の行)を採用。
            # v3で再取得した race_kind 入りの行が、古い空の行を上書きする。
            done[rid] = rec
    f.close()
    return done


def collect():
    """DB走査→grade欠損抽出→gambooから4項目取得→FILLS_PATHへ逐次追記。"""
    if not DB_PATH:
        print("[エラー] DBなし")
        return
    if _rq is None or BeautifulSoup is None:
        print("[エラー] requests / BeautifulSoup が必要です")
        return
    print("=== grade一括補填 収集フェーズ " + START + "〜" + END + " ===")
    print("DB:", DB_PATH, " FILLS:", FILLS_PATH)

    done = load_done_ids()
    print("既取得(キャッシュ):", len(done), "件")

    # 対象race_id収集
    targets = []
    f = io.open(DB_PATH, encoding="utf-8")
    for line in f:
        line = line.strip()
        if not line:
            continue
        if ('"date"' not in line):
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        d = str(rec.get("date", ""))
        if not (START <= d <= END):
            continue
        if has_meta(rec):
            continue
        rid = str(rec.get("race_id", ""))
        if len(rid) != 12 or not rid.isdigit():
            continue
        # 【v3修正】キャッシュにあっても race_kind が空なら再取得する。
        # 旧版は race_kind を抽出していなかったため、grade だけ入った
        # キャッシュが53,000件残っており、無条件スキップで永久に
        # 埋まらなくなっていた。GB_REDO=0 で従来の挙動に戻せる。
        d0 = done.get(rid)
        if d0 is not None:
            if not REDO_NOKIND:
                continue
            if str(d0.get("race_kind", "")).strip():
                continue
        targets.append((rid, d))
    f.close()
    print("補填対象(未取得):", len(targets), "件")

    # 開催(place_code+base_date)ごとの解決キャッシュ
    cup_cache = {}
    g = io.open(FILLS_PATH, "a", encoding="utf-8")
    processed = 0
    ok = 0
    ok_kind = 0
    try:
        for rid, d in targets:
            if LIMIT and processed >= LIMIT:
                print("[LIMIT到達] " + str(LIMIT) + "件で打ち切り")
                break
            place_code = rid[0:2]
            actual_date = rid[2:10]
            rno = int(rid[10:12])
            processed += 1

            # base_date/day を解決(この開催で既に解決済みなら再利用)
            resolved = None
            # 同一開催の候補キー: place_code + actual_date の近傍
            # まずキャッシュ(place_code, base_date, day)を探す
            for (pc, bd, dy) in list(cup_cache.keys()):
                if pc != place_code:
                    continue
                # base_date <= actual_date, day = 日差+1
                try:
                    from datetime import datetime
                    diff = (datetime.strptime(actual_date, "%Y%m%d")
                            - datetime.strptime(bd, "%Y%m%d")).days
                except Exception:
                    continue
                if 0 <= diff < MAXDAY and (dy == diff + 1):
                    resolved = (bd, dy)
                    break

            meta = None
            if resolved:
                bd, dy = resolved
                st, html = http_get(_url(place_code, bd, dy, rno))
                if st == 200 and html:
                    meta = extract_race_meta(html)
                time.sleep(SLEEP)

            if meta is None or not (meta.get("grade") or meta.get("race_kind")):
                # 総当たり: day=1..MAXDAY, base_date=actual_date-(day-1)
                from datetime import datetime, timedelta
                ad = datetime.strptime(actual_date, "%Y%m%d")
                found = False
                day = 1
                while day <= MAXDAY:
                    bd_dt = ad - timedelta(days=day - 1)
                    bd = bd_dt.strftime("%Y%m%d")
                    st, html = http_get(_url(place_code, bd, day, rno))
                    time.sleep(SLEEP)
                    if st == 200 and html:
                        mm = extract_race_meta(html)
                        if mm.get("grade") or mm.get("race_kind"):
                            meta = mm
                            cup_cache[(place_code, bd, day)] = True
                            found = True
                            break
                    day += 1
                if not found and meta is None:
                    meta = {"grade": "", "series_name": "",
                            "day_label": "", "race_kind": ""}

            if str(meta.get("race_kind", "")).strip():
                ok_kind = ok_kind + 1
            rec_out = {"race_id": rid, "grade": meta.get("grade", ""),
                       "race_kind": meta.get("race_kind", ""),
                       "day_label": meta.get("day_label", ""),
                       "series_name": meta.get("series_name", "")}
            g.write(json.dumps(rec_out, ensure_ascii=False) + "\n")
            g.flush()
            if rec_out["grade"] or rec_out["race_kind"]:
                ok += 1
            if processed % 50 == 0:
                print("  進捗 " + str(processed) + "/" + str(len(targets))
                      + "  grade有 " + str(ok)
                      + " / race_kind有 " + str(ok_kind))
    finally:
        g.close()
    print("[収集完了] 処理 " + str(processed) + " 件")
    print("           grade取得成功     " + str(ok) + " 件")
    print("           race_kind取得成功 " + str(ok_kind) + " 件"
          + ("   ← ここが0なら抽出が壊れている" if processed and not ok_kind
             else ""))
    print("           → " + FILLS_PATH)
    print("  DBへ反映するには GB_APPLY=1 で再実行してください。")


def apply_to_db():
    """FILLS_PATH の内容で DB を1回だけ読み直して一括置換。"""
    if not DB_PATH:
        print("[エラー] DBなし")
        return
    fills = load_done_ids()
    # grade も race_kind も空のものは適用しない(取得失敗)
    usable = {}
    for rid in fills:
        r = fills[rid]
        if str(r.get("grade", "")).strip() or str(r.get("race_kind", "")).strip():
            usable[rid] = r
    print("=== DB一括適用 ===")
    print("DB:", DB_PATH, " 適用可能な補填:", len(usable), "件")
    if not usable:
        print("適用対象なし")
        return

    tmp = DB_PATH + ".tmp"
    n_all = 0
    n_upd = 0
    fin = io.open(DB_PATH, encoding="utf-8")
    fout = io.open(tmp, "w", encoding="utf-8")
    try:
        for line in fin:
            s = line.strip()
            if not s:
                fout.write(line)
                continue
            n_all += 1
            try:
                rec = json.loads(s)
            except Exception:
                fout.write(line if line.endswith("\n") else line + "\n")
                continue
            rid = str(rec.get("race_id", ""))
            fill = usable.get(rid)
            if fill:
                changed = False
                for k in ("grade", "race_kind", "day_label", "series_name"):
                    v = str(fill.get(k, "")).strip()
                    cur = rec.get(k)
                    if v and (cur is None or str(cur).strip() == ""):
                        rec[k] = v
                        changed = True
                if changed:
                    n_upd += 1
                fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            else:
                fout.write(line if line.endswith("\n") else line + "\n")
    finally:
        fin.close()
        fout.close()
    os.replace(tmp, DB_PATH)
    print("[適用完了] 全 " + str(n_all) + " 行中 " + str(n_upd) + " 行を更新。")


def main():
    if APPLY_MODE:
        apply_to_db()
    else:
        collect()


if __name__ == "__main__":
    main()

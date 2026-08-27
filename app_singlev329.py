# -*- coding: utf-8 -*-
"""
app.py - 競輪予測 Web UI (Flask)

predict_today_v28.py のロジックを「計算層」としてそのまま再利用し、
print() 出力の代わりに JSON を返す薄い API 層を被せたもの。

設計:
  - 計算は「レースをタップした瞬間にその1Rだけ」遅延評価 (calc-on-tap)
  - 初期表示は会場一覧・レース一覧のメタ情報のみ → 最速
  - 既存のキャッシュ (ana_cache_today/cache_<date>.json) をそのまま使う

起動 (Pydroid3 / PC共通):
  pip install flask      # Pydroid3 は PIP メニューから flask を入れる
  python app.py
  → ブラウザで http://127.0.0.1:5000 を開く (同一端末の Chrome でOK)

配置:
  app.py / templates/index.html を Download に置く。
  predict_today_v28.py と各種データ (jsonl, json) も同じ Download に置く。

Pydroid3制約: f-string不使用 (このファイルは Web 側なので緩いが踏襲)
"""

import os
import sys
import json
import re
import random
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, jsonify, request, render_template, Response

# ------------------------------------------------------------
# 計算層 (既存ロジック) を import
# ------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else os.getcwd()
DOWNLOAD_DIR = "/storage/emulated/0/Download"
# Pydroid3は実行時に __file__ を定義しないことがあるため、
# takusen/code を明示的にimportパスへ追加する
TAKUSEN_CODE_DIR = "/storage/emulated/0/Download/takusen/code"
for _p in [SCRIPT_DIR, TAKUSEN_CODE_DIR, DOWNLOAD_DIR, os.getcwd()]:
    if _p and os.path.exists(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# ------------------------------------------------------------
# 背景画像(聖母マリア)のbase64を外部ファイルから読み込む
#   maria_bg.b64 をアプリ本体と同じフォルダに置く。
#   見つからない場合は背景なしで動作する(空文字)。
# ------------------------------------------------------------
def _load_maria_b64():
    names = ["maria_bg.b64"]
    dirs = []
    for _d in [SCRIPT_DIR, TAKUSEN_CODE_DIR, DOWNLOAD_DIR, os.getcwd()]:
        if _d and _d not in dirs:
            dirs.append(_d)
    for _d in dirs:
        for _n in names:
            _p = os.path.join(_d, _n)
            try:
                if os.path.exists(_p):
                    with open(_p, "r", encoding="utf-8") as _f:
                        return _f.read().strip()
            except Exception as _e:
                print("[警告] maria_bg.b64 の読み込みに失敗: " + str(_e))
    print("[情報] maria_bg.b64 が見つかりません(背景画像なしで起動)")
    return ""

MARIA_B64 = _load_maria_b64()

try:
    import predict_today_v31 as pt
except ImportError:
    print("[エラー] predict_today_v31.py が見つかりません")
    print("  → takusen/code/ にアプリ本体と一緒に置いてください")
    sys.exit(1)

# 大聖堂タブ 新予測エンジン (任意・無くてもアプリは起動する)
try:
    import predict_cathedral as pc
except Exception as _e_pc:
    pc = None
    print("[警告] predict_cathedral.py を読み込めません: " + str(_e_pc))
    print("  → 大聖堂タブは『エンジン未配置』表示になります")

# ===== ana_marker =====
# -*- coding: utf-8 -*-
"""
ana_marker.py - 穴サイド/勝負弱 判定ヘルパー

v42 (predict_today_v4f_v42.py) の穴判定ロジックを移植。
ana_score_min30.jsonl / ana_score_venue_min10.jsonl が無くても
安全に動作する (その場合は全選手ラベルなし)。

使い方:
  import ana_marker
  am = ana_marker.AnaMarker(SAVE_DIR)
  label = am.judge(player_name, venue)
  # label: {"kind": "ana"|"weak"|None, "text": "...", "hit": n, "den": n}
"""

import os
import re
import json

ANA_THRESHOLD = 0.10
POPULAR_THRESHOLD = -0.10


def normalize_name(s):
    if not s:
        return ""
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _reliability_global(score, starts):
    if score is None or starts is None:
        return 0
    abs_s = abs(score)
    if starts >= 50 and abs_s >= 0.15:
        return 3
    if starts >= 30 and abs_s >= 0.10:
        return 2
    if starts >= 15 and abs_s >= 0.05:
        return 1
    return 0


def _reliability_venue(score, starts):
    if score is None or starts is None:
        return 0
    abs_s = abs(score)
    if starts >= 10 and abs_s >= 0.15:
        return 3
    if starts >= 7 and abs_s >= 0.10:
        return 2
    if starts >= 5 and abs_s >= 0.05:
        return 1
    return 0


class AnaMarker(object):
    def __init__(self, save_dir):
        self.path_global = os.path.join(save_dir, "ana_score_global_FINAL.jsonl")
        self.path_venue = os.path.join(save_dir, "ana_score_venue_FINAL.jsonl")
        self.ana_global = self._load_global()
        self.ana_venue = self._load_venue()
        self.available = bool(self.ana_global) or bool(self.ana_venue)

    def _load_global(self):
        if not os.path.exists(self.path_global):
            return {}
        by_name = {}
        f = open(self.path_global, "r", encoding="utf-8")
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            nm = normalize_name(r.get("player_name", ""))
            if not nm:
                continue
            by_name.setdefault(nm, []).append(r)
        f.close()
        out = {}
        for nm in by_name:
            recs = by_name[nm]
            recs.sort(key=lambda x: -x.get("in_band_starts", 0))
            out[nm] = recs[0]
        return out

    def _load_venue(self):
        if not os.path.exists(self.path_venue):
            return {}
        by_key = {}
        f = open(self.path_venue, "r", encoding="utf-8")
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            place = r.get("place", "")
            nm = normalize_name(r.get("player_name", ""))
            if not place or not nm:
                continue
            key = (place, nm)
            by_key.setdefault(key, []).append(r)
        f.close()
        out = {}
        for k in by_key:
            recs = by_key[k]
            recs.sort(key=lambda x: -x.get("venue_in_band_starts", 0))
            out[k] = recs[0]
        return out

    def judge(self, name, venue):
        """選手の穴/勝負弱ラベルを返す
        返り値: {"kind": "ana"|"weak"|None, "text": str, "hit": int, "den": int}
        """
        result = {"kind": None, "text": "", "hit": 0, "den": 0}
        if not self.available:
            return result
        nm = normalize_name(name)
        global_rec = self.ana_global.get(nm)
        venue_rec = self.ana_venue.get((venue, nm))

        gs = global_rec.get("ana_score") if global_rec else None
        gst = global_rec.get("in_band_starts", 0) if global_rec else 0
        vs = venue_rec.get("venue_ana_score") if venue_rec else None
        vst = venue_rec.get("venue_in_band_starts", 0) if venue_rec else 0
        gst_n = _reliability_global(gs, gst)
        vst_n = _reliability_venue(vs, vst)

        is_pop = False
        is_ana = False
        use_global = False
        if vs is not None and vst_n >= 1:
            if vs <= POPULAR_THRESHOLD:
                is_pop = True
            elif vs >= ANA_THRESHOLD:
                is_ana = True
        if not is_pop and not is_ana:
            if gs is not None and gst_n >= 1:
                if gs <= POPULAR_THRESHOLD:
                    is_pop = True
                elif gs >= ANA_THRESHOLD:
                    is_ana = True
                use_global = (is_pop or is_ana)

        if not is_ana and not is_pop:
            return result

        # 該当/分母
        if vs is not None and vst_n >= 1 and not use_global:
            hit = venue_rec.get("venue_in_band_hit3_count", 0)
            den = vst
        else:
            hit = global_rec.get("in_band_hit3_count", 0) if global_rec else 0
            den = gst

        if is_ana:
            result["kind"] = "ana"
            result["text"] = "穴サイド"
        else:
            result["kind"] = "weak"
            result["text"] = "勝負弱"
        result["hit"] = hit
        result["den"] = den
        return result


# ===== result_provider =====
# -*- coding: utf-8 -*-
"""
result_provider.py - レース結果 (着順・決まり手・払戻) の取得

優先順位:
  1. キャッシュ ana_cache_today/result_<date>.json
  2. DB keirin_data_scored_v2.jsonl の該当 race_id レコード
  3. fetch_keirin_data_v14 を使ったスクレイピング (任意・無くても動く)

結果は確定後変わらないのでキャッシュに保存して再利用する。

返り値フォーマット (1レース):
  {
    "has_result": True/False,
    "result": [{"rank":1,"bike":4,"finish":"逃","diff":"..."}, ...],
    "trifecta": "4-1-2",          # 3連単の車番
    "refund_3t": 12340,            # 円 (int)
    "refund_3t_raw": "4-1-2(12340円)",
    "refund_2t_raw": "...",
    "source": "db" | "cache" | "scrape" | "none",
  }
"""

import os
import re
import json


# 会場名 → コード (race_id 生成用)
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


def _parse_refund_html(html):
    """結果ページHTMLから払戻 (3連単/2車単) を抽出。表記揺れ対策:
    NFKC正規化(全角数字→半角等)後、近接範囲限定パターン→広域パターンの順で試す。
    返り値: (refund_3t_raw, refund_2t_raw) 例 ("6-2-1(8920円)", "6-2(1230円)")"""
    import re as _re
    import unicodedata as _ud
    try:
        norm = _ud.normalize("NFKC", html)
    except Exception:
        norm = html
    r3 = ""
    r2 = ""
    pats3 = [
        r'3連(?:勝)?単[^0-9]{0,80}?(\d+\s*-\s*\d+\s*-\s*\d+)[^0-9]{0,80}?([\d,]+)\s*円',
        r'3連(?:勝)?単.*?(\d+\s*-\s*\d+\s*-\s*\d+).*?([\d,]+)\s*円',
    ]
    for pat in pats3:
        m3 = _re.search(pat, norm, _re.S)
        if m3:
            combo = m3.group(1).replace(" ", "")
            r3 = combo + "(" + m3.group(2) + "円)"
            break
    pats2 = [
        r'2車(?:連)?単[^0-9]{0,80}?(\d+\s*-\s*\d+)[^0-9]{0,80}?([\d,]+)\s*円',
        r'2車(?:連)?単.*?(\d+\s*-\s*\d+).*?([\d,]+)\s*円',
    ]
    for pat in pats2:
        m2 = _re.search(pat, norm, _re.S)
        if m2:
            combo2 = m2.group(1).replace(" ", "")
            r2 = combo2 + "(" + m2.group(2) + "円)"
            break
    return (r3, r2)


def _parse_refund(refund_raw):
    """ "4-1-2(12340円)" -> ("4-1-2", 12340) """
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


def _normalize_result(rec):
    """DBレコード or スクレイプ結果から結果dictを組む"""
    result = rec.get("result")
    refund_3t_raw = rec.get("refund_3t", "")
    refund_2t_raw = rec.get("refund_2t", "")

    has = bool(result) and isinstance(result, list) and len(result) > 0
    combo3, yen3 = _parse_refund(refund_3t_raw)
    if not has and not combo3:
        return {"has_result": False}

    res_items = []
    if isinstance(result, list):
        for r in result:
            if not isinstance(r, dict):
                continue
            res_items.append({
                "rank": r.get("rank"),
                "bike": r.get("bike"),
                "finish": r.get("finish", "--"),
                "diff": r.get("diff", "--"),
            })
        res_items.sort(key=lambda x: (x["rank"] is None, x["rank"]))

    # trifecta は払戻からが確実 (resultの上位3着でも組めるが払戻優先)
    trifecta = combo3
    if not trifecta and len(res_items) >= 3:
        top3 = res_items[:3]
        trifecta = "-".join(str(t["bike"]) for t in top3)

    # === 整合性チェック ===
    # 着順(result)の上位3車と払戻combo3が食い違う場合、着順は別レースを
    # 取得した可能性が高い。払戻を正としてresultを破棄する。
    if combo3 and len(res_items) >= 3:
        top3_from_result = "-".join(str(t["bike"]) for t in res_items[:3])
        if top3_from_result != combo3:
            # 着順データは信用できない → 払戻のcombo3だけ使う
            res_items = []
            for idx, part in enumerate(combo3.split("-")):
                try:
                    bk = int(part)
                except Exception:
                    continue
                res_items.append({"rank": idx + 1, "bike": bk,
                                   "finish": "--", "diff": "--"})
            trifecta = combo3

    return {
        "has_result": True,
        "result": res_items,
        "trifecta": trifecta,
        "refund_3t": yen3,
        "refund_3t_raw": refund_3t_raw,
        "refund_2t_raw": refund_2t_raw,
    }


class ResultProvider(object):
    def __init__(self, save_dir, db_path, cache_dir):
        self.save_dir = save_dir
        self.db_path = db_path
        self.cache_dir = cache_dir
        self._db_index = None  # race_id -> record (lazy)
        self._db_index_mtime = None  # 索引構築時のDBファイル更新時刻(DB同期後の再構築判定用)
        self._player_hist_index = None  # "氏名|期" -> [(date, raw, ten)...] (lazy)
        self._player_hist_index_name = None  # 氏名のみ -> [...] (フォールバック)
        import threading
        self._lock = threading.Lock()
        # スクレイピング用 engine (任意)
        self._scraper = None
        try:
            import fetch_keirin_data_v14 as fk
            self._scraper = fk
        except Exception:
            self._scraper = None

    # --- キャッシュ ---
    def _cache_path(self, date_str):
        if not os.path.exists(self.cache_dir):
            try:
                os.makedirs(self.cache_dir)
            except Exception:
                pass
        return os.path.join(self.cache_dir, "result_" + date_str + ".json")

    def _load_cache(self, date_str):
        p = self._cache_path(date_str)
        if not os.path.exists(p):
            return {}
        try:
            f = open(p, "r", encoding="utf-8")
            d = json.load(f)
            f.close()
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_cache(self, date_str, data):
        p = self._cache_path(date_str)
        try:
            f = open(p, "w", encoding="utf-8")
            json.dump(data, f, ensure_ascii=False)
            f.close()
        except Exception:
            pass

    # --- DB インデックス (メモリ効率: race_id -> ファイル内バイトオフセット) ---
    # 435MBのDB全件を dict に展開するとメモリ数GBに膨れスマホでOOM死するため、
    # race_id とオフセットだけ保持し、参照時にその行だけ seek して読む。
    def _current_db_mtime(self):
        try:
            return os.path.getmtime(self.db_path)
        except Exception:
            return None

    def _ensure_db_index(self):
        """DBファイルが更新(mtime変化)されていたら索引を作り直す。
        DB同期ボタンでDBを差し替えても、プロセス再起動なしで最新を引けるようにする。"""
        cur = self._current_db_mtime()
        if self._db_index is None or cur != self._db_index_mtime:
            self._build_db_index()

    def _build_db_index(self):
        idx = {}
        if not os.path.exists(self.db_path):
            self._db_index = idx
            self._db_index_mtime = None
            return
        f = open(self.db_path, "rb")
        try:
            offset = 0
            for raw in f:
                stripped = raw.strip()
                if stripped:
                    # race_id だけ軽量に取り出す (全体パースは避ける)
                    try:
                        rec = json.loads(stripped.decode("utf-8"))
                        rid = rec.get("race_id")
                    except Exception:
                        rid = None
                    if rid:
                        idx[rid] = offset
                offset += len(raw)
        finally:
            f.close()
        self._db_index = idx
        self._db_index_mtime = self._current_db_mtime()

    def _db_record_at(self, offset):
        """指定オフセットの1行を読んで dict を返す。"""
        try:
            f = open(self.db_path, "rb")
            try:
                f.seek(offset)
                raw = f.readline()
            finally:
                f.close()
            return json.loads(raw.decode("utf-8").strip())
        except Exception:
            return None

    def _db_lookup(self, race_id):
        with self._lock:
            self._ensure_db_index()
            offset = self._db_index.get(race_id) if self._db_index else None
        if offset is None:
            return None
        return self._db_record_at(offset)

    def get_races_for_date(self, date_str):
        """DB本体からその日付の全レースを出走表形式で返す。
        過去日の実績集計でスクレイピング不要にするため。
        race_id は 会場2桁+YYYYMMDD+R2桁 なので date 部分で抽出。"""
        with self._lock:
            self._ensure_db_index()
            if not self._db_index:
                return []
            # race_id は 会場2桁+YYYYMMDD+R2桁。文字列で日付を含むものだけ抽出。
            offsets = []
            for rid in self._db_index:
                if str(date_str) in str(rid):
                    offsets.append(self._db_index[rid])
        out = []
        for off in offsets:
            rec = self._db_record_at(off)
            if rec is not None and str(rec.get("date", "")) == str(date_str):
                out.append(rec)
        return out

    # --- 選手別 出走履歴インデックス (score推移グラフ用) ---
    #   player_key("氏名|期") -> [(date_str, raw_score(float or None), kyousou(float or None)), ...]
    #   _db_index 全件を1回だけ走査して構築 (lazy / キャッシュ)
    def _player_key_from_full_info(self, pdata):
        pk = pdata.get("player_key") or ""
        if pk:
            return pk
        fi = pdata.get("full_info", "")
        parts = fi.split("/") if isinstance(fi, str) else []
        if len(parts) >= 4:
            nm = parts[0].strip()
            pe = parts[3].strip()
            if nm and pe:
                return nm + "|" + pe
        return ""

    def _kyousou_from_full_info(self, pdata):
        # full_info "氏名/府県/歳/期/競走得点点" の末尾 "○○点" を float で返す
        fi = pdata.get("full_info", "")
        if not isinstance(fi, str):
            return None
        parts = fi.split("/")
        if not parts:
            return None
        tail = parts[-1].strip()
        num = ""
        for ch in tail:
            if ch.isdigit() or ch == ".":
                num = num + ch
            else:
                if num:
                    break
        if not num:
            return None
        try:
            return float(num)
        except Exception:
            return None

    def _build_player_history_index(self):
        idx = {}
        idx_name = {}  # 氏名のみ -> 履歴 (フォールバック用)
        # DBをストリームで1行ずつ読む (全件をメモリに展開しない)。
        if not os.path.exists(self.db_path):
            self._player_hist_index = idx
            self._player_hist_index_name = idx_name
            return
        f = open(self.db_path, "rb")
        try:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped.decode("utf-8"))
                except Exception:
                    continue
                date_str = rec.get("date", "")
                if not date_str:
                    continue
                players = rec.get("players", {})
                if not isinstance(players, dict):
                    continue
                for bs in players:
                    pdata = players[bs]
                    if not isinstance(pdata, dict):
                        continue
                    pkey = self._player_key_from_full_info(pdata)
                    raw = pdata.get("raw_score")
                    try:
                        raw = float(raw) if raw is not None else None
                    except Exception:
                        raw = None
                    ten = self._kyousou_from_full_info(pdata)
                    if raw is None and ten is None:
                        continue
                    rec_tuple = (date_str, raw, ten)
                    if pkey:
                        if pkey not in idx:
                            idx[pkey] = []
                        idx[pkey].append(rec_tuple)
                    # 氏名のみキー (player_key 先頭 or full_info 先頭)
                    nm = ""
                    if pkey and "|" in pkey:
                        nm = pkey.split("|")[0].strip()
                    if not nm:
                        fi = pdata.get("full_info", "")
                        if isinstance(fi, str):
                            ps = fi.split("/")
                            if ps:
                                nm = ps[0].strip()
                    if nm:
                        if nm not in idx_name:
                            idx_name[nm] = []
                        idx_name[nm].append(rec_tuple)
        finally:
            f.close()
        for pkey in idx:
            idx[pkey].sort(key=lambda r: r[0])
        for nm in idx_name:
            idx_name[nm].sort(key=lambda r: r[0])
        self._player_hist_index = idx
        self._player_hist_index_name = idx_name

    def build_score_trend(self, players_labels, today_str, months=5, race_result=None):
        # 出走表の各選手について、today から過去 months ヶ月の raw_score / 競走得点 推移を返す。
        #   返り値: {"months":5, "series":[{bike,key,raw:[{t,v}],ten:[{t,v}]}...],
        #            "has_result":bool, "top3_bikes":[...]}
        #   t は 0(months ヶ月前) ... 1(現在=予想レース) の正規化値。
        #   DB の最終点と現在(予想レース値)は無理に繋ぐ(t=1 に予想値を置く)。
        try:
            from datetime import datetime as _dt
            from datetime import timedelta as _td
        except Exception:
            return {"months": months, "series": [], "has_result": False, "top3_bikes": []}
        with self._lock:
            if getattr(self, "_player_hist_index", None) is None:
                self._build_player_history_index()
        hist = getattr(self, "_player_hist_index", {}) or {}
        hist_name = getattr(self, "_player_hist_index_name", {}) or {}
        try:
            today_dt = _dt.strptime(today_str, "%Y%m%d")
        except Exception:
            today_dt = _dt.now()
        # 期間 (months ヶ月 ≒ 30*months 日) の開始日
        span_days = 30 * months
        start_dt = today_dt - _td(days=span_days)
        total = float(span_days) if span_days > 0 else 1.0

        def to_t(d_dt):
            delta = (d_dt - start_dt).days
            t = delta / total
            if t < 0:
                t = 0.0
            if t > 1:
                t = 1.0
            return t

        series = []
        for pl in players_labels:
            bike = pl.get("bike")
            pkey = pl.get("pid") or ""
            # 履歴照合: player_key 優先、無ければ氏名フォールバック
            recs = hist.get(pkey, None)
            if recs is None:
                nm = ""
                if pkey and "|" in pkey:
                    nm = pkey.split("|")[0].strip()
                if not nm:
                    nm = pl.get("name") or ""
                recs = hist_name.get(nm, [])
            raw_pts = []
            ten_pts = []
            # 競走得点が極端に低い(10点以下)日は欠場・失格・データ欠損の可能性が高く、
            # 軸レンジを引っ張ってグラフ全体を潰すため raw/ten 両方とも除外する。
            min_ten = 10.0
            for (ds, raw, ten) in recs:
                try:
                    d_dt = _dt.strptime(ds, "%Y%m%d")
                except Exception:
                    continue
                if d_dt < start_dt or d_dt >= today_dt:
                    continue
                # ten が取得でき、かつ 10 点以下なら この日は丸ごとスキップ
                if ten is not None and ten <= min_ten:
                    continue
                tt = to_t(d_dt)
                if raw is not None:
                    raw_pts.append({"t": round(tt, 5), "v": round(raw, 3)})
                if ten is not None:
                    ten_pts.append({"t": round(tt, 5), "v": round(ten, 2)})
            # 現在(予想レース)の値を t=1 に追加 (無理に繋ぐ)
            cur_raw = pl.get("raw_score")
            cur_ten = pl.get("kyousou_ten")
            # 現在点も同じ閾値で判定 (10点以下なら両方除外)
            cur_ten_val = None
            if cur_ten is not None:
                try:
                    cur_ten_val = float(cur_ten)
                except Exception:
                    cur_ten_val = None
            cur_skip = (cur_ten_val is not None and cur_ten_val <= min_ten)
            if (not cur_skip) and cur_raw is not None:
                try:
                    raw_pts.append({"t": 1.0, "v": round(float(cur_raw), 3)})
                except Exception:
                    pass
            if (not cur_skip) and cur_ten_val is not None:
                try:
                    ten_pts.append({"t": 1.0, "v": round(cur_ten_val, 2)})
                except Exception:
                    pass
            if len(raw_pts) >= 1 or len(ten_pts) >= 1:
                series.append({
                    "bike": bike,
                    "key": pkey,
                    "raw": raw_pts,
                    "ten": ten_pts,
                })
        # 結果情報 (デフォルト選択用)
        has_result = False
        top3 = []
        if race_result and isinstance(race_result, dict):
            tb = race_result.get("top3_bikes") or []
            for x in tb:
                try:
                    top3.append(int(x))
                except Exception:
                    pass
            if top3:
                has_result = True
        return {"months": months, "series": series,
                "has_result": has_result, "top3_bikes": top3}

    def race_id_for(self, venue, date_str, race_no):
        code = NAME_TO_CODE.get(venue)
        if not code:
            return None
        try:
            rno = int(race_no)
        except Exception:
            return None
        return code + str(date_str) + str(rno).zfill(2)

    def get_result(self, venue, date_str, race_no, allow_scrape=False):
        """1レースの結果を返す。"""
        rid = self.race_id_for(venue, date_str, race_no)
        if rid is None:
            return {"has_result": False, "source": "none"}

        # 1. キャッシュ
        with self._lock:
            cache = self._load_cache(date_str)
            cached = dict(cache[rid]) if rid in cache else None
        if cached is not None:
            need_refund = (not cached.get("refund_3t")) and allow_scrape and self._scraper is not None
            if not need_refund:
                cached["source"] = "cache"
                return cached
            # 着順はキャッシュにある → 払戻だけ gamboo で補完
            code = NAME_TO_CODE.get(venue)
            if code:
                r3, r2 = self._refund_only(code, date_str, race_no)
                if r3:
                    from_combo, yen = _parse_refund(r3)
                    cached["refund_3t"] = yen
                    cached["refund_3t_raw"] = r3
                    if r2:
                        cached["refund_2t_raw"] = r2
                    if not cached.get("trifecta") and from_combo:
                        cached["trifecta"] = from_combo
                    cached["source"] = "cache+refund"
                    with self._lock:
                        cache = self._load_cache(date_str)
                        cache[rid] = cached
                        self._save_cache(date_str, cache)
                    return cached
            # 払戻が取れなくても、着順のあるキャッシュはそのまま返す
            cached["source"] = "cache"
            return cached

        # 2. DB
        rec = self._db_lookup(rid)
        if rec is not None:
            norm = _normalize_result(rec)
            if norm.get("has_result"):
                norm["source"] = "db"
                # DBに払戻が無く、スクレイプ可能なら払戻だけ補完
                if (not norm.get("refund_3t")) and allow_scrape and self._scraper is not None:
                    code = NAME_TO_CODE.get(venue)
                    if code:
                        r3, r2 = self._refund_only(code, date_str, race_no)
                        if r3:
                            from_combo, yen = _parse_refund(r3)
                            norm["refund_3t"] = yen
                            norm["refund_3t_raw"] = r3
                            if r2:
                                norm["refund_2t_raw"] = r2
                            if not norm.get("trifecta") and from_combo:
                                norm["trifecta"] = from_combo
                with self._lock:
                    cache = self._load_cache(date_str)
                    cache[rid] = norm
                    self._save_cache(date_str, cache)
                return norm

        # 3. スクレイピング (任意)
        if allow_scrape and self._scraper is not None:
            scraped = self._scrape(venue, date_str, race_no)
            if scraped is not None and scraped.get("has_result"):
                scraped["source"] = "scrape"
                with self._lock:
                    cache = self._load_cache(date_str)
                    cache[rid] = scraped
                    self._save_cache(date_str, cache)
                return scraped

        return {"has_result": False, "source": "none"}

    def get_results_for_day(self, race_list, date_str, allow_scrape=False):
        """1日分の結果をまとめて取得する (集計高速化用)。
        result_<date>.json を 1回だけ読み、DBインデックス(メモリ)を参照し、
        新規取得分があれば最後に 1回だけ保存する。
        race_list: [{"venue":..,"race_no":..,"rid":..}, ...]
        返り値: rid -> result dict (source 付き)。"""
        # DBインデックスを事前構築 (初回のみI/O)
        with self._lock:
            self._ensure_db_index()
            cache = self._load_cache(date_str)  # 1回だけ読む
            cache = dict(cache) if isinstance(cache, dict) else {}

        out = {}
        dirty = False
        i = 0
        while i < len(race_list):
            item = race_list[i]
            i = i + 1
            rid = item.get("rid")
            venue = item.get("venue", "")
            race_no = item.get("race_no", "")
            if rid is None:
                continue

            # 1. キャッシュ (メモリ上の辞書を参照・ファイルI/Oなし)
            if rid in cache:
                cached = dict(cache[rid])
                need_refund = ((not cached.get("refund_3t")) and allow_scrape
                               and self._scraper is not None)
                if not need_refund:
                    cached["source"] = "cache"
                    out[rid] = cached
                    continue
                code = NAME_TO_CODE.get(venue)
                if code:
                    r3, r2 = self._refund_only(code, date_str, race_no)
                    if r3:
                        from_combo, yen = _parse_refund(r3)
                        cached["refund_3t"] = yen
                        cached["refund_3t_raw"] = r3
                        if r2:
                            cached["refund_2t_raw"] = r2
                        if not cached.get("trifecta") and from_combo:
                            cached["trifecta"] = from_combo
                        cached["source"] = "cache+refund"
                        cache[rid] = cached
                        dirty = True
                        out[rid] = cached
                        continue
                cached["source"] = "cache"
                out[rid] = cached
                continue

            # 2. DB (メモリインデックス参照)
            rec = self._db_index.get(rid)
            if rec is not None:
                norm = _normalize_result(rec)
                if norm.get("has_result"):
                    norm["source"] = "db"
                    if ((not norm.get("refund_3t")) and allow_scrape
                            and self._scraper is not None):
                        code = NAME_TO_CODE.get(venue)
                        if code:
                            r3, r2 = self._refund_only(code, date_str, race_no)
                            if r3:
                                from_combo, yen = _parse_refund(r3)
                                norm["refund_3t"] = yen
                                norm["refund_3t_raw"] = r3
                                if r2:
                                    norm["refund_2t_raw"] = r2
                                if not norm.get("trifecta") and from_combo:
                                    norm["trifecta"] = from_combo
                    cache[rid] = norm
                    dirty = True
                    out[rid] = norm
                    continue

            # 3. スクレイピング (任意・DB/キャッシュに無い分のみ)
            if allow_scrape and self._scraper is not None:
                scraped = self._scrape(venue, date_str, race_no)
                if scraped is not None and scraped.get("has_result"):
                    scraped["source"] = "scrape"
                    cache[rid] = scraped
                    dirty = True
                    out[rid] = scraped
                    continue

            out[rid] = {"has_result": False, "source": "none"}

        # 新規取得分があれば 1回だけ保存
        if dirty:
            with self._lock:
                self._save_cache(date_str, cache)
        return out

    def _scrape(self, venue, date_str, race_no):
        """fetch_keirin_data_v14 の関数を使って結果ページを取得。
        yen-joy で着順・決まり手、gamboo で払戻金を取得。
        DB書き込みは行わず、結果dictだけ返す。
        """
        fk = self._scraper
        code = NAME_TO_CODE.get(venue)
        if not code:
            return None
        from datetime import datetime, timedelta
        try:
            actual_dt = datetime.strptime(date_str, "%Y%m%d")
        except Exception:
            return None

        for diff_days in range(0, 4):
            base_dt = actual_dt - timedelta(days=diff_days)
            base_date_str = base_dt.strftime("%Y%m%d")
            day = diff_days + 1
            import calendar
            _, last_day = calendar.monthrange(base_dt.year, base_dt.month)
            if base_dt.day == last_day:
                if base_dt.month == 12:
                    url_ym = str(base_dt.year + 1) + "01"
                else:
                    url_ym = str(base_dt.year) + str(base_dt.month + 1).zfill(2)
            else:
                url_ym = base_dt.strftime("%Y%m")
            enjoy_url = ("https://www.yen-joy.net/kaisai/race/result/detail/"
                + url_ym + "/" + code + "/" + base_date_str + "/"
                + date_str + "/" + str(race_no))
            try:
                lap_data, lap_pos, weather, result_data = fk.extract_lap_positions_enjoy(enjoy_url)
            except Exception:
                continue
            if result_data:
                # gamboo から払戻を取得
                refund_3t, refund_2t = self._scrape_refund_gamboo(
                    fk, code, base_date_str, day, race_no)
                rec = {"result": result_data,
                       "refund_3t": refund_3t, "refund_2t": refund_2t}
                norm = _normalize_result(rec)
                if norm.get("has_result"):
                    return norm
        return None

    def _refund_only(self, code, date_str, race_no):
        """着順はDBにあるが払戻だけ欠けている場合に、gambooで払戻のみ取得。
        base_date を 0〜3日前まで試す。返り値: (refund_3t_raw, refund_2t_raw)
        """
        fk = self._scraper
        if fk is None:
            return ("", "")
        from datetime import datetime, timedelta
        try:
            actual_dt = datetime.strptime(date_str, "%Y%m%d")
        except Exception:
            return ("", "")
        for diff_days in range(0, 4):
            base_dt = actual_dt - timedelta(days=diff_days)
            base_date_str = base_dt.strftime("%Y%m%d")
            day = diff_days + 1
            r3, r2 = self._scrape_refund_gamboo(fk, code, base_date_str, day, race_no)
            if r3:
                return (r3, r2)
        return ("", "")

    def _scrape_refund_gamboo(self, fk, code, base_date_str, day, race_no):
        """gamboo 結果ページから払戻金 (3連単・2車単) を取得。
        返り値: (refund_3t_raw, refund_2t_raw) 例 ("6-2-1(8920円)", "6-2(1230円)")
        取得できなければ ("", "")
        """
        import re as _re
        gamboo_url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/result/"
            + code + base_date_str + "/"
            + code + base_date_str + str(day).zfill(2) + "00/"
            + str(race_no).zfill(2) + "/")
        try:
            status, html = fk.fetch_with_retry(gamboo_url)
        except Exception:
            return ("", "")
        if status != 200 or not html:
            return ("", "")
        r3, r2 = _parse_refund_html(html)
        if not r3:
            print("[refund] 解析失敗: " + gamboo_url
                  + " len=" + str(len(html))
                  + " has3連=" + str("3連" in html))
        return (r3, r2)


# ===== rsrank_provider =====

import os
import json
import re


# 16方位 → 角度
_DIR_TO_DEG = {
    "北": 0, "北北東": 22.5, "北東": 45, "東北東": 67.5, "東": 90, "東南東": 112.5,
    "南東": 135, "南南東": 157.5, "南": 180, "南南西": 202.5, "南西": 225,
    "西南西": 247.5, "西": 270, "西北西": 292.5, "北西": 315, "北北西": 337.5,
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5,
    "SE": 135, "SSE": 157.5, "S": 180, "SSW": 202.5, "SW": 225,
    "WSW": 247.5, "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}


def parse_weather(weather_str):
    if not weather_str:
        return None, None, None
    sky_m = re.search(r"天気[::]\s*([^\s]+)", weather_str)
    ws_m = re.search(r"風速[::]\s*(\d+(?:\.\d+)?)m", weather_str)
    wd_m = re.search(r"風向[::]\s*([^\s\(（]+)", weather_str)
    sky = sky_m.group(1).strip() if sky_m else None
    ws = float(ws_m.group(1)) if ws_m else None
    wd = wd_m.group(1).strip() if wd_m else None
    if sky in ("--", "取得失敗", "不明"):
        sky = None
    if wd in ("--", ""):
        wd = None
    return sky, ws, wd


def categorize_sky(sky):
    if not sky:
        return None
    if "晴" in sky:
        return "晴"
    if "曇" in sky:
        return "曇"
    if "雨" in sky:
        return "雨"
    if "雪" in sky:
        return "雪"
    return None


def categorize_wind_speed(ws):
    if ws is None:
        return None
    if ws <= 0.5:
        return "無風"
    if ws <= 2.0:
        return "弱風"
    if ws <= 3.5:
        return "中風"
    return "強風"


def categorize_wind_dir(wd, venue, venue_home_dir):
    if not wd:
        return None
    deg = _DIR_TO_DEG.get(wd)
    if deg is None:
        return None
    hd_str = (venue_home_dir or {}).get(venue)
    if not hd_str:
        return None
    hd_deg = _DIR_TO_DEG.get(hd_str)
    if hd_deg is None:
        return None
    delta = (deg - hd_deg) % 360
    if delta < 45 or delta >= 315:
        return "H向B追"
    if 135 <= delta < 225:
        return "H追B向"
    if 45 <= delta < 135:
        return "HB横"
    return "BH横"


def cond_from_weather(weather_str, venue, venue_home_dir):
    """weather文字列+会場から (venue, sky_cat, ws_cat, wd_cat) を返す"""
    sky, ws, wd = parse_weather(weather_str)
    return (venue,
            categorize_sky(sky),
            categorize_wind_speed(ws),
            categorize_wind_dir(wd, venue, venue_home_dir))


class RsRankProvider(object):

    def __init__(self, save_dir):
        self.save_dir = save_dir
        self.path7 = os.path.join(save_dir, "player_rsrank_finish_7car_FINAL.jsonl")
        self.available = False
        # 個人: key=(pid, rs_rank) -> {"all":[r1..r7,total], "cond":{cond_key:[r1..r7,total]}}
        self._by_player = {}
        # 基準: rs_rank -> {"all":[r1..r7,total], "cond":{cond_key:[...]}}
        self._baseline = {}
        self._load()

    def _cond_key(self, venue, sky, ws, wd):
        return (venue or "") + "|" + (sky or "") + "|" + (ws or "") + "|" + (wd or "")

    def _load(self):
        if not os.path.exists(self.path7):
            print("[rsrank] file not found: " + self.path7)
            return
        # ★v303: 0件になる原因を特定するための計測。
        #   ファイルはあるのに rows=0 になる場合、書式が想定と違う。
        _sz = -1
        try:
            _sz = os.path.getsize(self.path7)
        except Exception:
            _sz = -1
        print("[rsrank] path=" + self.path7)
        print("[rsrank] size=" + str(_sz) + " bytes")
        _skip_blank = 0
        _skip_json = 0
        _skip_pid = 0
        _skip_rs = 0
        _first_keys = ""
        _first_line = ""
        n = 0
        with open(self.path7, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    _skip_blank = _skip_blank + 1
                    continue
                if not _first_line:
                    _first_line = line[:200]
                try:
                    row = json.loads(line)
                except Exception:
                    _skip_json = _skip_json + 1
                    continue
                if not _first_keys:
                    try:
                        _first_keys = ",".join(sorted([str(_k) for _k in row]))
                    except Exception:
                        _first_keys = "(取得不可)"
                pid = row.get("player_key") or row.get("name") or ""
                if not pid:
                    _skip_pid = _skip_pid + 1
                    continue
                # ★v304: 圧縮書式に対応。
                #   {"player_key":..., "by_rs_rank":{"2":{"n":N,"pct":[7]}, ...}}
                #   pct は割合(0-1)なので件数へ戻して従来の内部表現に合わせる。
                #   条件別(会場/天気/風)の内訳は持たないため scope="cond" は未対応。
                _brs = row.get("by_rs_rank")
                if isinstance(_brs, dict):
                    _is_base = (pid == "__baseline__")
                    for _rk in _brs:
                        _cell = _brs[_rk]
                        if not isinstance(_cell, dict):
                            continue
                        _pct = _cell.get("pct")
                        if not isinstance(_pct, list):
                            continue
                        try:
                            _rsv = int(_rk)
                        except Exception:
                            continue
                        try:
                            _tot = int(_cell.get("n", 0) or 0)
                        except Exception:
                            _tot = 0
                        if _tot <= 0:
                            continue
                        _vals = []
                        _q = 0
                        while _q < 7:
                            _pv = 0.0
                            if _q < len(_pct):
                                try:
                                    _pv = float(_pct[_q])
                                except Exception:
                                    _pv = 0.0
                            _vals.append(int(round(_pv * _tot)))
                            _q = _q + 1
                        _vals.append(_tot)
                        if _is_base:
                            _b = self._baseline.get(_rsv)
                            if _b is None:
                                _b = {"all": [0] * 8, "cond": {}}
                                self._baseline[_rsv] = _b
                            _q = 0
                            while _q < 8:
                                _b["all"][_q] += _vals[_q]
                                _q = _q + 1
                        else:
                            _sl = self._by_player.get((pid, _rsv))
                            if _sl is None:
                                _sl = {"all": [0] * 8, "cond": {}}
                                self._by_player[(pid, _rsv)] = _sl
                            _q = 0
                            while _q < 8:
                                _sl["all"][_q] += _vals[_q]
                                _q = _q + 1
                        n = n + 1
                    continue
                rs = row.get("rs_rank")
                if rs is None:
                    _skip_rs = _skip_rs + 1
                    continue
                # 1..7着 + total (7車立てなので rank8,9 は無視)
                vals = [
                    int(row.get("rank1", 0) or 0),
                    int(row.get("rank2", 0) or 0),
                    int(row.get("rank3", 0) or 0),
                    int(row.get("rank4", 0) or 0),
                    int(row.get("rank5", 0) or 0),
                    int(row.get("rank6", 0) or 0),
                    int(row.get("rank7", 0) or 0),
                    int(row.get("total", 0) or 0),
                ]
                ck = self._cond_key(row.get("venue"), row.get("sky"),
                                    row.get("wind_speed_cat"),
                                    row.get("wind_dir_cat"))
                # --- 個人 ---
                pkey = (pid, rs)
                slot = self._by_player.get(pkey)
                if slot is None:
                    slot = {"all": [0]*8, "cond": {}}
                    self._by_player[pkey] = slot
                for i in range(8):
                    slot["all"][i] += vals[i]
                c = slot["cond"].get(ck)
                if c is None:
                    c = [0]*8
                    slot["cond"][ck] = c
                for i in range(8):
                    c[i] += vals[i]
                # --- 基準 (rs_rank別 全選手平均=合算) ---
                b = self._baseline.get(rs)
                if b is None:
                    b = {"all": [0]*8, "cond": {}}
                    self._baseline[rs] = b
                for i in range(8):
                    b["all"][i] += vals[i]
                bc = b["cond"].get(ck)
                if bc is None:
                    bc = [0]*8
                    b["cond"][ck] = bc
                for i in range(8):
                    bc[i] += vals[i]
                n += 1
        self.available = n > 0
        print("[rsrank] loaded rows=" + str(n)
              + " players=" + str(len(self._by_player))
              + " available=" + str(self.available))
        if n == 0:
            print("[rsrank] 読めなかった内訳: 空行=" + str(_skip_blank)
                  + " JSON不正=" + str(_skip_json)
                  + " player_key/name無=" + str(_skip_pid)
                  + " rs_rank無=" + str(_skip_rs))
            print("[rsrank] 1行目のキー: " + (_first_keys or "(行が無い)"))
            print("[rsrank] 1行目の中身: " + (_first_line or "(空ファイル)"))
            print("[rsrank] 期待するキー: player_key(またはname), rs_rank,"
                  + " rank1..rank7, total, venue, sky, wind_speed_cat, wind_dir_cat")

    def _dist(self, arr):
        """[r1..r7,total] -> {"pct":[7], "n":total}. total0なら None"""
        if not arr:
            return None
        total = arr[7]
        if total <= 0:
            return None
        pct = []
        for i in range(7):
            pct.append(round(100.0 * arr[i] / total, 1))
        return {"pct": pct, "n": total}

    def get_player_dist(self, pid, rs_rank, scope, cond):
        """個人の着順分布。scope='all' or 'cond'.
        cond=(venue,sky,ws,wd) tuple (scope=='cond'時に使用)"""
        slot = self._by_player.get((pid, rs_rank))
        if slot is None:
            return None
        if scope == "cond":
            ck = self._cond_key(*cond)
            return self._dist(slot["cond"].get(ck))
        return self._dist(slot["all"])

    def get_baseline_dist(self, rs_rank, scope, cond):
        """rs_rank別の全選手平均(=合算分布)。"""
        b = self._baseline.get(rs_rank)
        if b is None:
            return None
        if scope == "cond":
            ck = self._cond_key(*cond)
            return self._dist(b["cond"].get(ck))
        return self._dist(b["all"])


app = Flask(__name__)

# 穴/勝負弱マーカー (ファイルが無くても安全に動く)
try:

    _SAVE_DIR = getattr(pt, "DICTS_DIR", getattr(pt, "SAVE_DIR", DOWNLOAD_DIR))
    ANA = AnaMarker(_SAVE_DIR)
    print("[ana] SAVE_DIR     = " + str(_SAVE_DIR))
    print("[ana] global file  = " + ANA.path_global + " exists=" + str(os.path.exists(ANA.path_global)))
    print("[ana] venue file   = " + ANA.path_venue + " exists=" + str(os.path.exists(ANA.path_venue)))
    print("[ana] global loaded= " + str(len(ANA.ana_global)) + " 名")
    print("[ana] venue loaded = " + str(len(ANA.ana_venue)) + " 件")
    print("[ana] available    = " + str(ANA.available))
except Exception as _e:
    print("[ana] 初期化失敗: " + str(_e))
    ANA = None

# 選手別 決まり手 (出走表用: 役割別 逃捲差マ 全期間)
KIMARI_PLAYER_ROLE = {}
try:
    _SD_K = getattr(pt, "DICTS_DIR", getattr(pt, "SAVE_DIR", DOWNLOAD_DIR))
    _kp = os.path.join(_SD_K, "kimari_player_role_FINAL.jsonl")
    if os.path.exists(_kp):
        _kf = open(_kp, "r", encoding="utf-8")
        for _ln in _kf:
            _ln = _ln.strip()
            if not _ln:
                continue
            try:
                _o = json.loads(_ln)
            except Exception:
                continue
            _k = _o.get("k")
            if _k:
                KIMARI_PLAYER_ROLE[_k] = _o
        _kf.close()
    print("[kimari] role file = " + _kp + " exists=" + str(os.path.exists(_kp)))
    print("[kimari] players   = " + str(len(KIMARI_PLAYER_ROLE)))
except Exception as _e:
    print("[kimari] 初期化失敗: " + str(_e))
    KIMARI_PLAYER_ROLE = {}

# 必須データファイルの存在チェック (v29のFINAL体系)
try:
    _missing = pt.check_dict_files()
    if _missing:
        print("[check] 不足ファイルあり (" + str(len(_missing)) + "件):")
        for _m in _missing:
            print("[check]   " + _m)
        print("[check] → migrate_layout_v2.py で移行してください")
    else:
        print("[check] 必須データファイル: 全て揃っています")
except Exception as _e:
    print("[check] チェック失敗: " + str(_e))

# 結果プロバイダ (DB優先 → スクレイピング)
# 【v242】takusen体系: データフォルダはptのDATA_DIRに連動 (単一情報源)。
# DBが無ければ従来の場所 (pt.PATH_DB / 旧keirin_db / Download直下) に
# 自動フォールバックするので、移行前の環境でもそのまま動く
KEIRIN_DB_DIR = getattr(pt, "DATA_DIR", "/storage/emulated/0/Download/takusen/data")
try:

    _SD = getattr(pt, "SAVE_DIR", DOWNLOAD_DIR)
    _DBP_NEW = os.path.join(KEIRIN_DB_DIR, "keirin_data_scored_v2.jsonl")
    if os.path.exists(_DBP_NEW):
        _DBP = _DBP_NEW
        # pt内部がPATH_DBを参照する場合に備えて追随させる
        try:
            pt.PATH_DB = _DBP_NEW
        except Exception:
            pass
        print("[result] DB専用フォルダを使用: " + KEIRIN_DB_DIR)
    else:
        _DBP = getattr(pt, "PATH_DB", os.path.join(_SD, "keirin_data_scored_v2.jsonl"))
        print("[result] 従来のDB位置を使用 (専用フォルダにDBなし)")
    _CD = getattr(pt, "CACHE_DIR", os.path.join(_SD, "ana_cache_today"))
    RESULTS = ResultProvider(_SD, _DBP, _CD)
    print("[result] db       = " + _DBP + " exists=" + str(os.path.exists(_DBP)))
    print("[result] scraper  = " + ("有効" if RESULTS._scraper is not None else "なし"))
except Exception as _e:
    print("[result] 初期化失敗: " + str(_e))
    RESULTS = None

# ============================================================
# GitHub 同期 (託宣ボタン統合)
#   - sync_db_months: 月別DB (keirin_months/) の差分だけをDLしてマスターへマージ
#   - _try_github_today_cache: Actionsが朝6時に事前取得した当日出走表を利用
#   どちらも失敗時は従来動作 (ローカルDB / その場スクレイピング) にフォールバック
# ============================================================
GITHUB_USER_REPO = "NUKKUN-JP/keirin-db"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/" + GITHUB_USER_REPO + "/main/"
GITHUB_API_MONTHS = ("https://api.github.com/repos/" + GITHUB_USER_REPO
                     + "/contents/keirin_months")
GITHUB_API_DICTS = ("https://api.github.com/repos/" + GITHUB_USER_REPO
                    + "/contents/dicts")
# 同期状態ファイルは「使用中のDB」と同じフォルダに置く (DB移動に自動追随)
if RESULTS is not None:
    _SYNC_DIR = os.path.dirname(RESULTS.db_path)
else:
    _SYNC_DIR = getattr(pt, "SAVE_DIR", DOWNLOAD_DIR)
SYNC_STATE_PATH = os.path.join(_SYNC_DIR, "keirin_sync_state.json")
DICTS_SYNC_STATE_PATH = os.path.join(_SYNC_DIR, "keirin_dicts_sync_state.json")
# 統計ファイル同期: dicts/ 配下でこれらを同期対象にする (毎日更新の小さい統計)。
# v330: rsrank_7car を同期対象に戻した。
#   「98MBだからReleases別管理」という前提はもう成り立たない。
#   書式が圧縮版 (by_rs_rank / 会場・天気の内訳なし) に変わり、1.5MB になっている。
#   除外したままだと Actions 側に辞書が無く、御告の第二柱が全レースで無効になる。
#   そのとき oracle_core.js の __oraRsrFluct は 1.0 を返すのでエラーにならず、
#   買い目の優劣だけが静かに消える。二度と同じことを起こさないための記録。
DICTS_SYNC_TARGETS = (
    "kimari_player_role_FINAL.jsonl",
    "kimari_stats_FINAL.json",
    "ana_score_global_FINAL.jsonl",
    "ana_score_venue_FINAL.jsonl",
    "player_line_lead_rate_FINAL.json",
    "player_profile_index_FINAL.json",
    "rawscore_pattern_stats_FINAL.json",
    "player_rsrank_finish_9car_FINAL.jsonl",
    "player_rsrank_finish_7car_FINAL.jsonl",
)
# 同期から除外する大容量ファイル (現在は該当なし)
DICTS_SYNC_EXCLUDE = ()
_RID_RE = re.compile(r'"race_id"\s*:\s*"([^"]+)"')


def _load_sync_state():
    if not os.path.exists(SYNC_STATE_PATH):
        return {}
    try:
        f = open(SYNC_STATE_PATH, "r", encoding="utf-8")
        d = json.load(f)
        f.close()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_sync_state(state):
    try:
        f = open(SYNC_STATE_PATH, "w", encoding="utf-8")
        json.dump(state, f, ensure_ascii=False)
        f.close()
    except Exception as e:
        print("[sync] 状態保存失敗: " + str(e)[:60])


def sync_db_months():
    """GitHubの月別DBと手元DBを差分同期する。戻り値: (updated, msg)。
    GitHub APIで keirin_months/ の一覧とサイズを取得し、前回同期時と
    サイズが変わったファイルだけをDL → race_id単位でマスターへマージ
    (GitHub側優先)。サイズ比較なので変化がない日は一覧取得1回だけで終わる。"""
    try:
        import requests
    except Exception:
        return (False, "requestsなし")
    if RESULTS is None:
        return (False, "DB未初期化")
    db_path = RESULTS.db_path
    state = _load_sync_state()
    try:
        r = requests.get(GITHUB_API_MONTHS, timeout=20)
    except Exception as e:
        return (False, "一覧取得失敗:" + str(e)[:50])
    if r.status_code != 200:
        return (False, "一覧取得失敗 HTTP" + str(r.status_code))
    try:
        items = r.json()
    except Exception:
        return (False, "一覧解析失敗")
    if not isinstance(items, list):
        return (False, "一覧形式不正")
    targets = []
    for it in items:
        name = str(it.get("name", ""))
        size = it.get("size", 0)
        if not (name.startswith("keirin_") and name.endswith(".jsonl")):
            continue
        if state.get(name) != size:
            targets.append((name, size))
    if not targets:
        return (False, "DBは最新")
    print("[sync] 差分あり: " + str(len(targets)) + "ファイルをDL")
    new_lines = []
    new_ids = set()
    for name, size in targets:
        try:
            rr = requests.get(GITHUB_RAW_BASE + "keirin_months/" + name,
                              timeout=180)
        except Exception as e:
            return (False, name + " DL失敗:" + str(e)[:40])
        if rr.status_code != 200:
            return (False, name + " DL失敗 HTTP" + str(rr.status_code))
        rr.encoding = "utf-8"
        for line in rr.text.splitlines():
            s = line.strip()
            if not s:
                continue
            m = _RID_RE.search(s)
            if not m:
                continue
            new_lines.append(s)
            new_ids.add(m.group(1))
    if not new_lines:
        return (False, "取得0件")
    # マスターをストリーム処理: 同一race_idは除外 (GitHub側で上書き) して末尾に追記
    tmp = db_path + ".synctmp"
    replaced = 0
    out = open(tmp, "w", encoding="utf-8")
    try:
        if os.path.exists(db_path):
            f = open(db_path, "r", encoding="utf-8")
            for line in f:
                s = line.strip()
                if not s:
                    continue
                m = _RID_RE.search(s)
                if m and m.group(1) in new_ids:
                    replaced += 1
                    continue
                out.write(s + "\n")
            f.close()
        i = 0
        while i < len(new_lines):
            out.write(new_lines[i] + "\n")
            i += 1
    finally:
        out.close()
    os.replace(tmp, db_path)
    for name, size in targets:
        state[name] = size
    _save_sync_state(state)
    # lazyインデックスを無効化 (次回アクセス時に新DBで再構築される)
    try:
        RESULTS._db_index = None
        RESULTS._player_hist_index = None
        RESULTS._player_hist_index_name = None
    except Exception:
        pass
    added = len(new_lines) - replaced
    msg = (str(len(targets)) + "ファイル同期 (新規" + str(added)
           + "件/更新" + str(replaced) + "件)")
    print("[sync] " + msg)
    return (True, msg)


def _load_dicts_sync_state():
    if not os.path.exists(DICTS_SYNC_STATE_PATH):
        return {}
    try:
        f = open(DICTS_SYNC_STATE_PATH, "r", encoding="utf-8")
        d = json.load(f)
        f.close()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_dicts_sync_state(state):
    try:
        f = open(DICTS_SYNC_STATE_PATH, "w", encoding="utf-8")
        json.dump(state, f, ensure_ascii=False)
        f.close()
    except Exception as e:
        print("[dicts_sync] 状態保存失敗: " + str(e)[:60])


def sync_dicts_files():
    """GitHubの dicts/ 統計ファイルと手元 dicts/ を差分同期する。戻り値: (updated, msg)。
    sync_db_months と同方式: API一覧+サイズ取得 → サイズ変化したファイルだけDL →
    takusen/data/dicts/ にまるごと保存 (統計はマージ不要・全置換)。
    v330: rsrank_7car も同期対象 (圧縮書式で1.5MB)。"""
    try:
        import requests
    except Exception:
        return (False, "requestsなし")
    # 出力先 dicts/ (アプリが読む先と同一)
    dicts_dir = getattr(pt, "DICTS_DIR", None)
    if not dicts_dir:
        base = getattr(pt, "DATA_DIR", os.path.join(DOWNLOAD_DIR, "takusen", "data"))
        dicts_dir = os.path.join(base, "dicts")
    if not os.path.isdir(dicts_dir):
        try:
            os.makedirs(dicts_dir)
        except Exception as e:
            return (False, "dicts作成失敗:" + str(e)[:40])
    state = _load_dicts_sync_state()
    try:
        r = requests.get(GITHUB_API_DICTS, timeout=20)
    except Exception as e:
        return (False, "一覧取得失敗:" + str(e)[:50])
    if r.status_code != 200:
        return (False, "一覧取得失敗 HTTP" + str(r.status_code))
    try:
        items = r.json()
    except Exception:
        return (False, "一覧解析失敗")
    if not isinstance(items, list):
        return (False, "一覧形式不正")
    # 同期対象 (DICTS_SYNC_TARGETS) かつ サイズ変化 のものを抽出
    targets = []
    for it in items:
        name = str(it.get("name", ""))
        size = it.get("size", 0)
        if name in DICTS_SYNC_EXCLUDE:
            continue
        if name not in DICTS_SYNC_TARGETS:
            continue
        if state.get(name) != size:
            targets.append((name, size))
    if not targets:
        return (False, "統計は最新")
    print("[dicts_sync] 差分あり: " + str(len(targets)) + "ファイルをDL")
    ok_count = 0
    ti = 0
    while ti < len(targets):
        name, size = targets[ti]
        ti = ti + 1
        try:
            rr = requests.get(GITHUB_RAW_BASE + "dicts/" + name, timeout=180)
        except Exception as e:
            return (False, name + " DL失敗:" + str(e)[:40])
        if rr.status_code != 200:
            return (False, name + " DL失敗 HTTP" + str(rr.status_code))
        rr.encoding = "utf-8"
        tmp = os.path.join(dicts_dir, name + ".synctmp")
        try:
            out = open(tmp, "w", encoding="utf-8")
            out.write(rr.text)
            out.close()
            os.replace(tmp, os.path.join(dicts_dir, name))
        except Exception as e:
            return (False, name + " 保存失敗:" + str(e)[:40])
        state[name] = size
        ok_count = ok_count + 1
    _save_dicts_sync_state(state)
    # 統計はプロセス内キャッシュされている可能性があるが、
    # 次回起動/再読込で反映される (KIMARI_PLAYER_ROLE等はモジュール起動時ロード)。
    msg = str(ok_count) + "統計ファイル同期"
    print("[dicts_sync] " + msg)
    return (True, msg)


def _try_github_today_cache(date_str):
    """GitHub Actionsが事前取得した当日出走表をDLしてキャッシュ保存。
    成功ならレース数を返す。0なら従来スクレイピングにフォールバック。"""
    try:
        import requests
    except Exception:
        return 0
    url = GITHUB_RAW_BASE + "today_cache/races_" + date_str + ".json"
    try:
        r = requests.get(url, timeout=60)
    except Exception:
        return 0
    if r.status_code != 200:
        return 0
    try:
        races = r.json()
    except Exception:
        return 0
    if not isinstance(races, list) or not races:
        return 0
    try:
        pt.save_cache(date_str, races)
    except Exception:
        return 0
    _RACES_BY_DATE.pop(date_str, None)
    print("[github] 当日事前取得データ利用: " + date_str + " "
          + str(len(races)) + "R")
    return len(races)


# ============================================================
# 買い目 (picks/YYYYMMDD.json)
#   GitHub Actions が毎日作ったものをそのまま読む。
#   ★の計算も条件判定も買い目生成も向こうで済んでいるので、
#   アプリは受け取って表示するだけ。端末の辞書に依存しない。
# ============================================================
_PICKS_BY_DATE = {}   # date_str -> {race_key: entry} / {} = 無し


def get_picks(date_str):
    """その日の買い目を返す。{race_key: entry}。無ければ空。"""
    if date_str in _PICKS_BY_DATE:
        return _PICKS_BY_DATE[date_str]
    out = {}
    try:
        import requests
    except Exception:
        _PICKS_BY_DATE[date_str] = out
        return out
    url = GITHUB_RAW_BASE + "picks/" + date_str + ".json"
    try:
        r = requests.get(url, timeout=30)
    except Exception:
        _PICKS_BY_DATE[date_str] = out
        return out
    if r.status_code != 200:
        print("[picks] " + date_str + " は未生成 (HTTP "
              + str(r.status_code) + ")")
        _PICKS_BY_DATE[date_str] = out
        return out
    try:
        body = r.json()
    except Exception:
        _PICKS_BY_DATE[date_str] = out
        return out
    rows = body.get("races") or []
    i = 0
    while i < len(rows):
        e = rows[i]
        i = i + 1
        k = str(e.get("key", ""))
        if k:
            out[k] = e
    _PICKS_BY_DATE[date_str] = out
    print("[picks] " + date_str + " " + str(len(out)) + "R  ("
          + str(body.get("generated", "")) + ")")
    return out


# ============================================================
# 買い目 (picks/YYYYMMDD.json) の取得
#   GitHub Actions が毎日作ったものを読むだけ。
#   ★の計算も条件判定も端末では行わない。
#   誰が起動しても同じ買い目が出るようにするため。
# ============================================================
_PICKS_BY_DATE = {}   # date_str -> {race_key: entry} / {} / None(取得失敗)


def get_picks(date_str, force=False):
    """picks/YYYYMMDD.json を取ってきて race_key 引きの辞書にする。
    取れなければ {} を返す(予想なしとして扱う)。"""
    if not force and date_str in _PICKS_BY_DATE:
        c = _PICKS_BY_DATE[date_str]
        return c if c is not None else {}
    out = {}
    meta = {}
    try:
        import requests
    except Exception:
        _PICKS_BY_DATE[date_str] = None
        return {}
    url = GITHUB_RAW_BASE + "picks/" + date_str + ".json"
    try:
        r = requests.get(url, timeout=60)
    except Exception:
        _PICKS_BY_DATE[date_str] = None
        return {}
    if r.status_code != 200:
        # まだ作られていない日もある。失敗ではなく「予想なし」。
        _PICKS_BY_DATE[date_str] = {}
        print("[picks] " + date_str + " は未作成 (status "
              + str(r.status_code) + ")")
        return {}
    try:
        body = r.json()
    except Exception:
        _PICKS_BY_DATE[date_str] = {}
        return {}
    races = body.get("races") or []
    i = 0
    while i < len(races):
        e = races[i]
        i = i + 1
        k = str(e.get("key", ""))
        if k:
            out[k] = e
    meta = {"generated": body.get("generated", ""),
            "conditions_updated": body.get("conditions_updated", ""),
            "star_model": body.get("star_model", {})}
    out["__meta__"] = meta
    _PICKS_BY_DATE[date_str] = out
    print("[picks] " + date_str + " 買い目 " + str(len(races)) + "R"
          + " (作成 " + str(meta.get("generated", "")) + ")")
    return out


# ============================================================
# 買い目 (picks/YYYYMMDD.json)
#   GitHub Actions が毎朝作ったものを読むだけ。
#   ★の計算も条件判定も買い目生成も向こうで済んでいるので、
#   端末は辞書を持たなくてよく、誰が起動しても同じ結果になる。
# ============================================================
_PICKS_BY_DATE = {}   # date_str -> {race_key: entry} or None(取得失敗)


def get_picks(date_str, force=False):
    """picks/YYYYMMDD.json を取ってきて {race_key: entry} にする。
    取れなければ None。1日1回だけ取りに行く。"""
    if not force and date_str in _PICKS_BY_DATE:
        return _PICKS_BY_DATE[date_str]
    out = None
    try:
        import requests
    except Exception:
        _PICKS_BY_DATE[date_str] = None
        return None
    url = GITHUB_RAW_BASE + "picks/" + date_str + ".json"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            body = r.json()
            races = body.get("races") if isinstance(body, dict) else None
            if isinstance(races, list):
                out = {}
                i = 0
                while i < len(races):
                    e = races[i]
                    i = i + 1
                    k = str(e.get("key", ""))
                    if k:
                        out[k] = e
                print("[picks] " + date_str + " " + str(len(out)) + "R "
                      + "(条件 " + str(body.get("conditions_updated", "")) + ")")
    except Exception as e:
        print("[picks] 取得失敗: " + str(e)[:80])
        out = None
    _PICKS_BY_DATE[date_str] = out
    return out


# ============================================================
# 買い目 (picks/YYYYMMDD.json)
#   GitHub Actions が前もって作ったものを読むだけ。
#   ★の計算も条件判定も買い目作りも向こうで済んでいるので、
#   端末は辞書を持たなくてよく、誰が開いても同じ結果になる。
# ============================================================
_PICKS_BY_DATE = {}   # date_str -> {race_key: entry} / None(取得失敗)


def get_picks(date_str, force=False):
    """picks/YYYYMMDD.json を取ってきて race_key で引ける形にする。"""
    if not force and date_str in _PICKS_BY_DATE:
        return _PICKS_BY_DATE[date_str]
    out = None
    try:
        import requests
    except Exception:
        _PICKS_BY_DATE[date_str] = None
        return None
    url = GITHUB_RAW_BASE + "picks/" + date_str + ".json"
    try:
        r = requests.get(url, timeout=60)
    except Exception:
        _PICKS_BY_DATE[date_str] = None
        return None
    if r.status_code != 200:
        # まだ作られていない日は 404。エラーではないので静かに戻す。
        _PICKS_BY_DATE[date_str] = None
        return None
    try:
        body = r.json()
    except Exception:
        _PICKS_BY_DATE[date_str] = None
        return None
    races = (body or {}).get("races")
    if not isinstance(races, list):
        _PICKS_BY_DATE[date_str] = None
        return None
    out = {}
    i = 0
    while i < len(races):
        e = races[i]
        i = i + 1
        k = str(e.get("key", ""))
        if k:
            out[k] = e
    _PICKS_BY_DATE[date_str] = out
    print("[github] 買い目: " + date_str + " " + str(len(out)) + "R "
          + "(条件 " + str((body or {}).get("conditions_updated", "")) + ")")
    return out


# ============================================================
# 買い目 (picks/YYYYMMDD.json)
#   GitHub Actions が毎日作ったものをそのまま読む。
#   ★の計算も条件判定も買い目生成もあちら側で済んでいるので、
#   アプリは表示するだけ。端末の辞書に依存しない。
# ============================================================
_PICKS_BY_DATE = {}   # date_str -> {race_key: entry} / None(取得失敗)


def get_picks(date_str, force=False):
    """その日の買い目を取ってくる。1度取ったら覚えておく。
    返り値: {race_key: entry} 。取れなければ {}。"""
    if not force and date_str in _PICKS_BY_DATE:
        return _PICKS_BY_DATE[date_str] or {}
    out = {}
    body = None
    # まず手元。GitHub Actions と同じ場所に置いてあれば使う。
    for d in (os.path.join(DATA_DIR, "picks"),
              os.path.join(BASE_DIR, "picks"),
              os.path.join(DOWNLOAD_DIR, "picks")):
        try:
            fp = os.path.join(d, date_str + ".json")
            if os.path.exists(fp):
                f = open(fp, "r", encoding="utf-8")
                try:
                    body = json.load(f)
                finally:
                    f.close()
                print("[picks] ローカル利用: " + fp)
                break
        except Exception:
            body = None
    # 無ければ GitHub から
    if body is None:
        try:
            import requests
            url = GITHUB_RAW_BASE + "picks/" + date_str + ".json"
            r = requests.get(url, timeout=60)
            if r.status_code == 200:
                body = r.json()
                print("[picks] GitHub利用: " + date_str)
        except Exception:
            body = None
    if not isinstance(body, dict):
        _PICKS_BY_DATE[date_str] = None
        return {}
    rows = body.get("races") or []
    i = 0
    while i < len(rows):
        e = rows[i]
        i = i + 1
        k = str(e.get("key", ""))
        if k:
            out[k] = e
    _PICKS_BY_DATE[date_str] = out
    print("[picks] " + date_str + " 買い目のあるレース " + str(len(out)) + "R")
    return out


# ============================================================
# 買い目キャッシュ (picks/YYYYMMDD.json)
#   GitHub Actions が毎日作る。★の計算も条件判定も買い目生成も
#   向こうで終わっているので、アプリは読んで出すだけでよい。
#   端末の辞書に依存しないので、誰が起動しても同じ結果になる。
# ============================================================
_PICKS_BY_DATE = {}   # date_str -> {race_key: entry} / None(取得失敗)


def get_picks(date_str, force=False):
    """picks/YYYYMMDD.json を取ってきて {race_key: entry} で返す。
    取れなければ None。一度取ったらメモリに残す。"""
    if not force and date_str in _PICKS_BY_DATE:
        return _PICKS_BY_DATE[date_str]
    try:
        import requests
    except Exception:
        _PICKS_BY_DATE[date_str] = None
        return None
    url = GITHUB_RAW_BASE + "picks/" + date_str + ".json"
    try:
        r = requests.get(url, timeout=30)
    except Exception:
        _PICKS_BY_DATE[date_str] = None
        return None
    if r.status_code != 200:
        _PICKS_BY_DATE[date_str] = None
        return None
    try:
        body = r.json()
    except Exception:
        _PICKS_BY_DATE[date_str] = None
        return None
    races = body.get("races") if isinstance(body, dict) else None
    if not isinstance(races, list):
        _PICKS_BY_DATE[date_str] = None
        return None
    out = {}
    i = 0
    while i < len(races):
        e = races[i]
        i = i + 1
        k = str(e.get("key", ""))
        if k:
            out[k] = e
    _PICKS_BY_DATE[date_str] = out
    print("[github] 買い目キャッシュ利用: " + date_str + " "
          + str(len(out)) + "R")
    return out


@app.route("/api/picks")
def api_picks():
    """その日の買い目をまとめて返す。アプリはこれを読むだけ。"""
    date_str = request.args.get("date", "").strip()
    if not date_str:
        return jsonify({"ok": False, "error": "date が必要", "races": {}})
    force = request.args.get("force", "") == "1"
    pk = get_picks(date_str, force=force)
    if pk is None:
        return jsonify({"ok": False, "error": "picks がありません",
                        "races": {}})
    return jsonify({"ok": True, "races": pk})


# ============================================================
# 買い目 (picks/YYYYMMDD.json)
#   GitHub Actions が毎日作ったものを読むだけ。
#   ★の計算も条件判定も買い目生成もアプリでは行わない。
#   端末の辞書に依存しないので、誰が起動しても同じ結果になる。
# ============================================================
# 出走表 (today_cache/races_YYYYMMDD.json)
#   v330: GitHub から読む。端末のファイルには依存しない。
#   誰が起動しても同じものが見えるようにするため。
#   端末に保存はせず、アプリが動いている間だけ覚えておく。
#   GitHub に置いてあるのは4日分なので、それより前は開けない。
# ============================================================
_TODAY_CACHE_MEM = {}   # date_str -> races


def fetch_today_cache_from_github(date_str, force=False):
    """その日の出走表を GitHub から取る。一度取ったら覚えておく。"""
    if not force and date_str in _TODAY_CACHE_MEM:
        return _TODAY_CACHE_MEM[date_str]
    out = []
    try:
        import requests
    except Exception:
        _TODAY_CACHE_MEM[date_str] = out
        return out
    url = GITHUB_RAW_BASE + "today_cache/races_" + date_str + ".json"
    try:
        r = requests.get(url, timeout=45)
    except Exception as e:
        print("[today] " + date_str + " 取得できません: " + str(e)[:60])
        return out
    if r.status_code != 200:
        print("[today] " + date_str + " は置いてありません (HTTP "
              + str(r.status_code) + ")")
        _TODAY_CACHE_MEM[date_str] = out
        return out
    try:
        races = r.json()
    except Exception:
        _TODAY_CACHE_MEM[date_str] = out
        return out
    if isinstance(races, list) and races:
        out = races
        print("[today] " + date_str + " " + str(len(races)) + "R 取得")
    _TODAY_CACHE_MEM[date_str] = out
    return out


# ============================================================
_PICKS_BY_DATE = {}   # date_str -> {race_key: entry} / {} = 取得できず


def get_picks(date_str, force=False):
    """その日の買い目をGitHubから取る。一度取ったら覚えておく。"""
    if not force and date_str in _PICKS_BY_DATE:
        return _PICKS_BY_DATE[date_str]
    out = {}
    meta = {}
    try:
        import requests
    except Exception:
        _PICKS_BY_DATE[date_str] = out
        return out
    url = GITHUB_RAW_BASE + "picks/" + date_str + ".json"
    try:
        r = requests.get(url, timeout=30)
    except Exception:
        _PICKS_BY_DATE[date_str] = out
        return out
    if r.status_code != 200:
        print("[picks] " + date_str + " は未生成 (HTTP "
              + str(r.status_code) + ")")
        _PICKS_BY_DATE[date_str] = out
        return out
    try:
        body = r.json()
    except Exception:
        _PICKS_BY_DATE[date_str] = out
        return out
    races = body.get("races") or []
    i = 0
    while i < len(races):
        e = races[i]
        i = i + 1
        k = str(e.get("key", ""))
        if k:
            out[k] = e
    # 買い目が作れなかったレースと、その理由
    sk = {}
    sl = body.get("skips") or []
    j = 0
    while j < len(sl):
        e2 = sl[j]
        j = j + 1
        k2 = str(e2.get("key", ""))
        if k2:
            sk[k2] = str(e2.get("reason", ""))
    out["__meta__"] = {"generated": body.get("generated", ""),
                       "conditions_updated": body.get("conditions_updated", ""),
                       "star_model": body.get("star_model", {}),
                       "skips": sk}
    print("[picks] " + date_str + " " + str(len(races)) + "R 取得")
    _PICKS_BY_DATE[date_str] = out
    return out


# 大聖堂事前計算キャッシュ (cathedral_cache/cathedral_YYYYMMDD.json)
_CATHEDRAL_CACHE_BY_DATE = {}  # date_str -> {race_key: entry} or None(取得失敗)


def _get_cathedral_cache(date_str):
    """その日の大聖堂事前計算キャッシュを返す (race_key -> entry の dict)。
    プロセス内キャッシュ優先。無ければ GitHub raw から取得。
    取得失敗/不存在なら None を返す (app側はその場計算にフォールバック)。"""
    if date_str in _CATHEDRAL_CACHE_BY_DATE:
        return _CATHEDRAL_CACHE_BY_DATE[date_str]
    races = None
    try:
        import requests
        url = GITHUB_RAW_BASE + "cathedral_cache/cathedral_" + date_str + ".json"
        r = requests.get(url, timeout=60)
        if r.status_code == 200:
            j = r.json()
            if isinstance(j, dict) and isinstance(j.get("races"), dict):
                races = j["races"]
                print("[github] 大聖堂キャッシュ利用: " + date_str + " "
                      + str(len(races)) + "R")
    except Exception:
        races = None
    _CATHEDRAL_CACHE_BY_DATE[date_str] = races
    return races


# ============================================================
# 3連単オッズ 月別ファイル (odds_months/YYYYMM.jsonl)
#   1行 = 1レース:
#     {"race_id":..., "date":"20260409", "place":"函館", "race_no":1,
#      "n":210, "odds":{"1-3-2":1171.5, ...}}
#   Σ(1/オッズ)=1.339 → 期待払戻率74.7% = 確定オッズ(控除率25%)。
#   期待値タブで EV = モデル確率 x オッズ を出すのに使う。
# ============================================================
_ODDS_MONTH_CACHE = {}   # "YYYYMM" -> {date: {place_raceno: {combo: odds}}}


def _load_odds_month(ym):
    """月別オッズを読み込む。実機ローカル優先、無ければGitHub raw。
    返り値: {date_str: {"会場_R番号": {combo: odds}}} / 取得失敗は None。"""
    if ym in _ODDS_MONTH_CACHE:
        return _ODDS_MONTH_CACHE[ym]
    lines = None
    # 1) ローカル
    cands = []
    base = getattr(pt, "DATA_DIR", "")
    if base:
        cands.append(os.path.join(base, "odds_months", ym + ".jsonl"))
        cands.append(os.path.join(base, ym + ".jsonl"))
    cands.append(os.path.join(DOWNLOAD_DIR, "takusen", "data", "odds_months",
                              ym + ".jsonl"))
    for c in cands:
        if c and os.path.exists(c):
            try:
                f = open(c, "r", encoding="utf-8")
                try:
                    lines = f.read().split("\n")
                finally:
                    f.close()
                print("[odds] ローカル利用: " + c)
                break
            except Exception:
                lines = None
    # 2) GitHub
    if lines is None:
        try:
            import requests
            url = GITHUB_RAW_BASE + "odds_months/" + ym + ".jsonl"
            r = requests.get(url, timeout=180)
            if r.status_code == 200:
                lines = r.text.split("\n")
                print("[odds] GitHub取得: " + ym + ".jsonl")
        except Exception:
            lines = None
    if lines is None:
        print("[odds] " + ym + " が取得できません (期待値タブは空になります)")
        _ODDS_MONTH_CACHE[ym] = None
        return None

    by_date = {}
    n_race = 0
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except Exception:
            continue
        d = str(rec.get("date", ""))
        od = rec.get("odds")
        if not d or not isinstance(od, dict):
            continue
        k = str(rec.get("place", "")) + "_" + str(rec.get("race_no", ""))
        if d not in by_date:
            by_date[d] = {}
        by_date[d][k] = od
        n_race = n_race + 1
    print("[odds] " + ym + " : " + str(n_race) + "R / " + str(len(by_date)) + "日")
    _ODDS_MONTH_CACHE[ym] = by_date
    return by_date


def _odds_for_date(date_str):
    """その日の {race_key: {combo: odds}} を返す。無ければ空dict。"""
    if len(str(date_str)) < 6:
        return {}
    m = _load_odds_month(str(date_str)[:6])
    if not m:
        return {}
    return m.get(str(date_str), {})


def _count_lineless(races):
    """ライン情報が空のレース数 (ミッドナイトは午後までライン未掲載)"""
    n = 0
    for r in races:
        if not str(r.get("line", "")).strip():
            n += 1
    return n


def _try_github_line_refresh(date_str, local_races):
    """手元キャッシュにライン空きがある場合、GitHubの午後更新版と比較し、
    ライン充足が改善していれば差し替える。差し替えたらレース数を返す"""
    local_missing = _count_lineless(local_races)
    if local_missing == 0:
        return 0
    try:
        import requests
    except Exception:
        return 0
    url = GITHUB_RAW_BASE + "today_cache/races_" + date_str + ".json"
    try:
        r = requests.get(url, timeout=60)
    except Exception:
        return 0
    if r.status_code != 200:
        return 0
    try:
        races = r.json()
    except Exception:
        return 0
    if not isinstance(races, list) or not races:
        return 0
    if _count_lineless(races) >= local_missing:
        return 0
    try:
        pt.save_cache(date_str, races)
    except Exception:
        return 0
    _RACES_BY_DATE.pop(date_str, None)
    print("[github] ライン補完版に更新: " + date_str + " (空き "
          + str(local_missing) + " → " + str(_count_lineless(races)) + "R)")
    return len(races)


# score順位×着順分布プロバイダ
try:

    _SD2 = getattr(pt, "DICTS_DIR", getattr(pt, "SAVE_DIR", DOWNLOAD_DIR))
    RSRANK = RsRankProvider(_SD2)
except Exception as _e:
    print("[rsrank] 初期化失敗: " + str(_e))
    RSRANK = None

# 共有辞書は一度だけロードしてプロセス内で保持 (表示スピード対策)
_DICTS = {}


def _wf_prev_month_end(date_str):
    """date_str(YYYYMMDD)の月の予想に使うcutoff=前月末日を返す"""
    try:
        y = int(date_str[:4])
        m = int(date_str[4:6])
    except Exception:
        return None
    m = m - 1
    if m == 0:
        m = 12
        y = y - 1
    if m in (1, 3, 5, 7, 8, 10, 12):
        last = 31
    elif m in (4, 6, 9, 11):
        last = 30
    else:
        if (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0):
            last = 29
        else:
            last = 28
    return str(y) + str(m).zfill(2) + str(last).zfill(2)


# walk-forward用: 通常辞書のパスを退避する場所
_WF_SAVED_PATHS = {}
_WF_MONTHLY_ROOT = os.path.join(
    getattr(pt, "DATA_DIR", os.path.join(DOWNLOAD_DIR, "takusen", "data")),
    "dicts_monthly")


def _wf_switch_dicts(date_str):
    """date_str の月に対応する前月末cutoffの辞書フォルダへ pt を切り替える。
    dicts_monthly/<cutoff>/ が存在すれば切り替えてTrueを返す。
    無ければ何もせずFalse(通常辞書のまま=従来動作)。"""
    cutoff = _wf_prev_month_end(date_str)
    if not cutoff:
        return False
    cdir = os.path.join(_WF_MONTHLY_ROOT, cutoff)
    if not os.path.isdir(cdir):
        return False
    # 現在のパスを退避 (初回のみ)
    if not _WF_SAVED_PATHS:
        _WF_SAVED_PATHS["PROFILES_DIR"] = pt.PROFILES_DIR
        _WF_SAVED_PATHS["PATH_INDEX"] = pt.PATH_INDEX
        _WF_SAVED_PATHS["PATH_LINE_LEAD"] = pt.PATH_LINE_LEAD
        _WF_SAVED_PATHS["PATH_RAWSCORE_PATTERN_STATS"] = \
            pt.PATH_RAWSCORE_PATTERN_STATS
        _WF_SAVED_PATHS["PATH_KIMARI_STATS"] = pt.PATH_KIMARI_STATS
    # cutoff辞書のパスへ向け直す
    pt.PROFILES_DIR = os.path.join(cdir, "player_profiles_FINAL")
    pt.PATH_INDEX = os.path.join(cdir, "player_profile_index_FINAL.json")
    pt.PATH_LINE_LEAD = os.path.join(cdir, "player_line_lead_rate_FINAL.json")
    pt.PATH_RAWSCORE_PATTERN_STATS = os.path.join(
        cdir, "rawscore_pattern_stats_FINAL.json")
    pt.PATH_KIMARI_STATS = os.path.join(cdir, "kimari_stats_FINAL.json")
    # ★v311: rsrank(揺らぎ=3柱合議の柱3)も切り替える。
    #   従来は対象外で、窓検証中も「全期間で学習した揺らぎ」を読んでいた。
    #   柱3だけ look-ahead が入り、回収率が甘く出ていた。
    _wf_switch_rsrank(cdir)
    # ★v325: 6種すべてが切り替わったかをログに出す。
    #   月別辞書の欠落は7月・8月と2回見落とした(エラーが出ないため)。
    #   毎回ログで目視できるようにする。
    #   1つでも「★無い」が出たら、その辞書だけ通常辞書のまま使われている。
    try:
        _items = [
            ("kimari", pt.PATH_KIMARI_STATS, "file"),
            ("rawscore", pt.PATH_RAWSCORE_PATTERN_STATS, "file"),
            ("rsrank", getattr(RSRANK, "path7", ""), "file"),
            ("profiles", pt.PROFILES_DIR, "dir"),
            ("index", pt.PATH_INDEX, "file"),
            ("line_lead", pt.PATH_LINE_LEAD, "file"),
        ]
        print("[wf] 辞書切替 cutoff=" + str(cutoff))
        _ng = 0
        _q = 0
        while _q < len(_items):
            _nm, _pp, _kind = _items[_q]
            _q = _q + 1
            if not _pp:
                print("      " + _nm + " : ★パス未設定")
                _ng = _ng + 1
                continue
            _inmonthly = (str(cutoff) in str(_pp))
            if _kind == "dir":
                if not os.path.isdir(_pp):
                    print("      " + _nm + " : ★無い " + _pp)
                    _ng = _ng + 1
                    continue
                _n = 0
                for _nm2 in os.listdir(_pp):
                    if _nm2.endswith(".json"):
                        _n = _n + 1
                print("      " + _nm + " : 選手" + str(_n) + "人"
                      + ("" if _inmonthly else "  ★通常辞書のまま"))
            else:
                if not os.path.exists(_pp):
                    print("      " + _nm + " : ★無い " + _pp)
                    _ng = _ng + 1
                    continue
                try:
                    _sz = os.path.getsize(_pp)
                except Exception:
                    _sz = -1
                _ex = ""
                if _nm == "rsrank":
                    _ex = " available=" + str(getattr(RSRANK, "available", "?"))
                print("      " + _nm + " : " + str(_sz) + "B" + _ex
                      + ("" if _inmonthly else "  ★通常辞書のまま"))
        if _ng:
            print("      >>> " + str(_ng) + "種が欠けている。"
                  + "その辞書だけ通常辞書が使われる(混在)。")
    except Exception as e:
        print("[wf] 辞書切替の確認に失敗: " + str(e)[:60])
    # キャッシュをクリアして次回load時にcutoff辞書を読ませる
    _wf_clear_dict_cache()
    return True


_WF_RSRANK_CUR = {"path": ""}


def _wf_switch_rsrank(cdir):
    """RSRANK(揺らぎ)を cutoff辞書へ向け直して読み直す。
    cdir が空なら通常辞書へ戻す。
    月別ファイルが無ければ通常辞書のまま(従来動作)。
    同じパスなら読み直さない(1日ごとの再ロードを避ける)。"""
    if RSRANK is None:
        return False
    if not _WF_SAVED_PATHS.get("RSRANK_PATH7"):
        _WF_SAVED_PATHS["RSRANK_PATH7"] = getattr(RSRANK, "path7", "")
    target = _WF_SAVED_PATHS["RSRANK_PATH7"]
    if cdir:
        cand = os.path.join(cdir, "player_rsrank_finish_7car_FINAL.jsonl")
        if os.path.exists(cand):
            target = cand
        else:
            print("[wf] 月別rsrankが無いため通常辞書を使用: " + cdir)
    if not target:
        return False
    if _WF_RSRANK_CUR["path"] == target:
        return True
    try:
        RSRANK.path7 = target
        RSRANK._by_player = {}
        RSRANK._baseline = {}
        RSRANK.available = False
        RSRANK._load()
        _WF_RSRANK_CUR["path"] = target
        print("[wf] rsrank切替: " + os.path.basename(os.path.dirname(target))
              + " available=" + str(RSRANK.available))
        return RSRANK.available
    except Exception as e:
        print("[wf] rsrank切替に失敗: " + str(e)[:60])
        return False


def _wf_evw_path(date_str):
    """その日に対応する EV重み付き辞書のパスを返す。無ければ空文字。
    dicts_monthly/<cutoff>/rawscore_pattern_stats_EVW.json を見る。
    cutoff辞書が無い日(=通常辞書で動く日)は dicts/ 直下も見る。"""
    cutoff = _wf_prev_month_end(date_str)
    if cutoff:
        p = os.path.join(_WF_MONTHLY_ROOT, cutoff,
                         "rawscore_pattern_stats_EVW.json")
        if os.path.exists(p):
            return p
    base = getattr(pt, "DICTS_DIR", "")
    if base:
        p2 = os.path.join(base, "rawscore_pattern_stats_EVW.json")
        if os.path.exists(p2):
            return p2
    return ""


def _wf_restore_dicts():
    """通常辞書のパスへ戻す"""
    if not _WF_SAVED_PATHS:
        return
    pt.PROFILES_DIR = _WF_SAVED_PATHS["PROFILES_DIR"]
    pt.PATH_INDEX = _WF_SAVED_PATHS["PATH_INDEX"]
    pt.PATH_LINE_LEAD = _WF_SAVED_PATHS["PATH_LINE_LEAD"]
    pt.PATH_RAWSCORE_PATTERN_STATS = \
        _WF_SAVED_PATHS["PATH_RAWSCORE_PATTERN_STATS"]
    pt.PATH_KIMARI_STATS = _WF_SAVED_PATHS["PATH_KIMARI_STATS"]
    _wf_switch_rsrank("")
    _wf_clear_dict_cache()


def _wf_clear_dict_cache():
    """pt._cache とアプリ側 _DICTS のうち辞書系をクリアして再ロードを促す"""
    for k in ["lld", "index", "rps", "ks"]:
        if k in pt._cache:
            del pt._cache[k]
    # アプリ側 get_dicts のキャッシュもクリア (bank/vhdは静的なので残してよいが
    # 安全のため辞書系再ロードのため全クリア→get_dictsで再ウォームアップ)
    # venue_home_dir/bank_data は静的データなので保持
    pass


def get_dicts():
    if _DICTS:
        return _DICTS
    _DICTS["venue_home_dir"] = pt.load_venue_home_direction()
    _DICTS["bank_data"] = pt.load_bank_data()
    # 以下は pt 内部の _cache に載るので呼んでおく (ウォームアップ)
    pt.load_line_lead_data()
    pt.load_index()
    pt.load_rawscore_pattern_stats()
    pt.load_kimari_stats()
    # 大聖堂エンジンの辞書ロードは重い(7辞書)。会場表示/レース閲覧では不要
    # (venue_flagsの的中判定は事前計算キャッシュを使う)。初回の御託宣その場計算
    # (api_cathedral の live フォールバック)まで遅延させ、起動/一覧表示を軽くする。
    # _init_cathedral_once() は api_cathedral 側で必要時に呼ばれる。
    return _DICTS


_CATHEDRAL_INIT = {"done": False, "ok": False, "info": None}


def _cathedral_dicts_dir():
    return getattr(pt, "DICTS_DIR", getattr(pt, "SAVE_DIR", DOWNLOAD_DIR))


def _init_cathedral_once():
    if _CATHEDRAL_INIT["done"]:
        return _CATHEDRAL_INIT
    _CATHEDRAL_INIT["done"] = True
    if pc is None:
        _CATHEDRAL_INIT["ok"] = False
        _CATHEDRAL_INIT["info"] = {"reason": "module_not_imported"}
        print("[大聖堂] エンジン未import")
        return _CATHEDRAL_INIT
    try:
        info = pc.init_cathedral(_cathedral_dicts_dir())
    except Exception as e:
        _CATHEDRAL_INIT["ok"] = False
        _CATHEDRAL_INIT["info"] = {"reason": "init_exception", "error": str(e)}
        print("[大聖堂] init例外: " + str(e))
        return _CATHEDRAL_INIT
    _CATHEDRAL_INIT["ok"] = bool(info.get("ok"))
    _CATHEDRAL_INIT["info"] = info
    if info.get("ok"):
        print("[大聖堂] 辞書ロードOK kimari=" + str(info.get("kimari_n", 0)))
    else:
        print("[大聖堂] 辞書不足: " + ",".join(info.get("missing", [])))
    return _CATHEDRAL_INIT


# ------------------------------------------------------------
# ユーティリティ: レースに一意キー (place + race_no) を振る
# ------------------------------------------------------------
def race_key(r):
    return str(r.get("place", "")) + "_" + str(r.get("race_no", ""))


# ------------------------------------------------------------
# WINTICKET 個別レースURLを組み立てる
#   engine 側の WINTICKET_VENUE_ROMA / winticket_find_holding を流用。
#   build_cache 時に天候取得で holding はキャッシュ済みのため通常は追加通信なし。
#   失敗時は会場トップ or keirin トップに安全フォールバック。
# ------------------------------------------------------------
def winticket_url(venue, date_str, race_no):
    info = winticket_url_diag(venue, date_str, race_no)
    return info["url"]


def winticket_url_diag(venue, date_str, race_no):
    """winticket レースURLを組む。失敗箇所を diag に残してフォールバック。"""
    diag = {"venue": venue, "date": str(date_str), "race_no": race_no,
            "step": "", "roma": None, "holding": None}
    try:
        import predict_v14_wind_unified as engine
    except Exception as e:
        diag["step"] = "import_failed:" + str(e)[:60]
        return {"url": "https://www.winticket.jp/keirin/", "diag": diag}
    roma = None
    try:
        roma = engine.WINTICKET_VENUE_ROMA.get(venue)
    except Exception as e:
        diag["step"] = "roma_attr_error:" + str(e)[:60]
        return {"url": "https://www.winticket.jp/keirin/", "diag": diag}
    diag["roma"] = roma
    if not roma:
        diag["step"] = "roma_not_found"
        return {"url": "https://www.winticket.jp/keirin/", "diag": diag}
    venue_top = "https://www.winticket.jp/keirin/" + roma + "/"
    try:
        ds = str(date_str).replace("/", "").replace("-", "")
        holding = engine.winticket_find_holding(roma, ds)
        diag["holding"] = holding
        if not holding:
            diag["step"] = "holding_none"
            return {"url": venue_top, "diag": diag}
        start_date, cup_id = holding
        from datetime import datetime as _dt
        td = _dt.strptime(ds, "%Y%m%d")
        sd = _dt.strptime(start_date, "%Y%m%d")
        day_idx = (td - sd).days + 1
        rno = int(race_no)
        url = ("https://www.winticket.jp/keirin/" + roma
               + "/racecard/" + start_date + cup_id + "/"
               + str(day_idx) + "/" + str(rno))
        diag["step"] = "ok"
        return {"url": url, "diag": diag}
    except Exception as e:
        diag["step"] = "build_error:" + str(e)[:60]
        return {"url": venue_top, "diag": diag}


# ------------------------------------------------------------
# WINTICKET 3連単オッズ取得
#   winticket の web SPA が叩く公開 JSON API を利用。
#   cupId = start_date + cup_id (winticket_url_diag と同じ holding 解決を流用)。
#   API: https://api.winticket.jp/v1/keirin/cups/{cupId}/schedules/{day}/races/{rno}/odds
#   失敗箇所は diag に残す。返すオッズは {"3-1-2": 12.3, ...} の dict。
# ------------------------------------------------------------
def winticket_ids(venue, date_str, race_no):
    """会場/日付/R番号から (cupId, day_idx, rno) を解決。失敗時 None。"""
    out = {"cup_id": None, "day_idx": None, "rno": None,
           "roma": None, "step": ""}
    try:
        import predict_v14_wind_unified as engine
    except Exception as e:
        out["step"] = "import_failed:" + str(e)[:50]
        return out
    try:
        roma = engine.WINTICKET_VENUE_ROMA.get(venue)
    except Exception as e:
        out["step"] = "roma_err:" + str(e)[:50]
        return out
    out["roma"] = roma
    if not roma:
        out["step"] = "roma_not_found"
        return out
    try:
        ds = str(date_str).replace("/", "").replace("-", "")
        holding = engine.winticket_find_holding(roma, ds)
        if not holding:
            out["step"] = "holding_none"
            return out
        start_date, cup_suffix = holding
        from datetime import datetime as _dt
        td = _dt.strptime(ds, "%Y%m%d")
        sd = _dt.strptime(start_date, "%Y%m%d")
        diff = (td - sd).days
        out["cup_id"] = start_date + cup_suffix
        out["day_idx"] = diff + 1        # 従来値(1始まり)
        out["day_candidates"] = [diff, diff + 1]  # 0始まり/1始まり両対応
        out["rno"] = int(race_no)
        out["step"] = "ok"
        return out
    except Exception as e:
        out["step"] = "build_err:" + str(e)[:50]
        return out


def _wt_http_json(url):
    """urllib で JSON GET。Pydroid3 で requests 非依存にするため urllib 使用。"""
    try:
        from urllib.request import Request, urlopen
    except Exception:
        from urllib2 import Request, urlopen  # fallback (py2想定はしないが安全側)
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Linux; Android) keirin-oracle/1.0",
        "Accept": "application/json",
        "Referer": "https://www.winticket.jp/",
    })
    f = urlopen(req, timeout=12)
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


def _parse_trifecta_odds(data):
    """winticket odds JSON から 3連単オッズを {"a-b-c": float} で抽出。
    キー形式の揺れ(1=2=3 / 1-2-3 / type名)に耐えるよう複数経路を探索。"""
    out = {}

    def norm_key(k):
        s = str(k)
        for ch in ("=", "_", ",", " "):
            s = s.replace(ch, "-")
        # 連続ハイフン除去
        while "--" in s:
            s = s.replace("--", "-")
        return s.strip("-")

    def take_list(lst):
        n = 0
        for it in lst:
            if not isinstance(it, dict):
                continue
            key = (it.get("key") or it.get("combination") or it.get("number")
                   or it.get("numbers") or it.get("name"))
            val = (it.get("odds") if it.get("odds") is not None
                   else it.get("odd") if it.get("odd") is not None
                   else it.get("value") if it.get("value") is not None
                   else it.get("min"))
            if key is None or val is None:
                continue
            nk = norm_key(key)
            if nk.count("-") != 2:
                continue
            try:
                out[nk] = float(val)
                n += 1
            except Exception:
                pass
        return n

    # data 内を再帰探索し、3連単(trifecta/tierce/sanrentan)らしき配列を拾う
    TRI_HINT = ("trifecta", "tierce", "sanrentan", "3t", "san_ren_tan")

    def walk(node, hint_tri):
        if isinstance(node, dict):
            # この dict 自身が type/name 値で3連単を示すなら子配列を対象化
            self_hint = hint_tri
            for vk in ("type", "name", "betType", "bet_type", "key"):
                vv = node.get(vk)
                if isinstance(vv, str) and any(h in vv.lower() for h in TRI_HINT):
                    self_hint = True
            for k, v in node.items():
                lk = str(k).lower()
                child_hint = self_hint or any(h in lk for h in TRI_HINT)
                if isinstance(v, list) and child_hint:
                    take_list(v)
                walk(v, child_hint)
        elif isinstance(node, list):
            for it in node:
                walk(it, hint_tri)

    walk(data, False)
    return out


_WT_ODDS_CACHE = {}  # key=(date,venue,rno) -> {"odds":{...}, "ts":epoch}


def fetch_winticket_trifecta_odds(venue, date_str, race_no, force=False):
    """3連単オッズ取得。返り値 {"ok":bool, "odds":{"a-b-c":float}, "diag":{...}}。"""
    import time as _t
    ck = (str(date_str), str(venue), str(race_no))
    if (not force) and ck in _WT_ODDS_CACHE:
        c = _WT_ODDS_CACHE[ck]
        if (_t.time() - c["ts"]) < 90:  # 90秒キャッシュ
            return {"ok": True, "odds": c["odds"],
                    "diag": {"step": "cache", "n": len(c["odds"])}}
    ids = winticket_ids(venue, date_str, race_no)
    diag = {"ids_step": ids.get("step"), "cup_id": ids.get("cup_id"),
            "day_idx": ids.get("day_idx"), "rno": ids.get("rno"),
            "step": "", "url": ""}
    if ids.get("step") != "ok":
        diag["step"] = "ids_failed"
        return {"ok": False, "odds": {}, "diag": diag}
    day_cands = ids.get("day_candidates") or [ids["day_idx"]]
    data = None
    used_day = None
    last_err = ""
    tried = []
    for dcand in day_cands:
        url = ("https://api.winticket.jp/v1/keirin/cups/" + str(ids["cup_id"])
               + "/schedules/" + str(dcand)
               + "/races/" + str(ids["rno"])
               + "/odds?fields=odds&pf=web")
        tried.append({"day": dcand, "url": url})
        try:
            d = _wt_http_json(url)
        except Exception as e:
            last_err = str(e)[:80]
            continue
        # trifecta に要素があればこの day を採用
        tri = d.get("trifecta") if isinstance(d, dict) else None
        if isinstance(tri, list) and len(tri) > 0:
            data = d
            used_day = dcand
            break
        # 中身が無くても最後の応答は保持(診断用)
        if data is None:
            data = d
            used_day = dcand
    diag["tried"] = tried
    diag["used_day"] = used_day
    diag["url"] = tried[-1]["url"] if tried else ""
    if data is None:
        diag["step"] = "http_err:" + last_err
        return {"ok": False, "odds": {}, "diag": diag}
    # --- デバッグ: 生JSONの構造を診断に載せる ---
    try:
        if isinstance(data, dict):
            diag["raw_keys"] = list(data.keys())
            od = data.get("odds")
            if isinstance(od, dict):
                diag["odds_keys"] = list(od.keys())
            elif isinstance(od, list):
                diag["odds_list_len"] = len(od)
                if od and isinstance(od[0], dict):
                    diag["odds_item0_keys"] = list(od[0].keys())
        diag["raw_sample"] = json.dumps(data, ensure_ascii=False)[:1500]
    except Exception as _e:
        diag["raw_dbg_err"] = str(_e)[:80]
    try:
        odds = _parse_trifecta_odds(data)
    except Exception as e:
        diag["step"] = "parse_err:" + str(e)[:80]
        return {"ok": False, "odds": {}, "diag": diag}
    diag["step"] = "ok"
    diag["n"] = len(odds)
    if odds:
        _WT_ODDS_CACHE[ck] = {"odds": odds, "ts": _t.time()}
    return {"ok": bool(odds), "odds": odds, "diag": diag}


# ================================================================
# GAMBOO(kdreams) 3連単オッズ取得
#   オッズページHTMLの <div class="odds_table N"> 内 <table class="odds_b5">
#   に数値が直接埋込。th[class=num_X]=2着, th値=3着, 直後td=オッズ。
#   1着はテーブル番号 N。→ {"a-b-c": float} を作る。
# ================================================================
def parse_gamboo_trifecta_odds(html):
    """gambooオッズページHTMLから3連単オッズを {"a-b-c": float} で返す。

    実際のHTML構造 (2026年時点):
      <div class="odds_table_wrapper">
        <table class="odds_table bt5 1 ">    # 末尾の数字 N = 1着車番
          <tbody>
            <tr> ... 見出し行 (th.n1..n7 colspan=9) ... </tr>
            <tr>
              <th class="n4">4</th>      # 行頭の th = 2着車番
              <td> 2321.0 </td>          # 列(1着を除いた昇順の1番目)
              <td> 589.6 </td>
              <td class="empty"> </td>   # 2着と同じ番号の列=空(列は数える)
              ...
              <th class="n4">4</th>      # 右端の th (2着車番の再掲, 無視)
            </tr>
        <table class="odds_table bt5 2 none"> ... (1着=2番)
        ... bt5 N まで

    3着車番の決め方: 各テーブル(1着=first)で、列は
    1..max_car から first を除いた車番の昇順に対応する。
    empty(=2着と同じ番号の位置)は値なしなのでスキップするが列としては数える。
    """
    out = {}
    if not html:
        return out
    tbls = re.findall(
        r'<table\s+class="odds_table\s+bt5\s+(\d+)[^"]*"[^>]*>(.*?)</table>',
        html, re.S)
    # 出走車数(最大車番)= テーブルの最大番号
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
        # 1着=first のとき3着候補列 = 1..max_car から first を除いた昇順
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
                # gamboo 3連単オッズ表の軸: 行頭th = 3着車番, 列ラベル(third_cols) = 2着車番。
                # 旧実装は行頭を2着・列を3着と取り違えており、2着と3着が転置した
                # 組み合わせにオッズが入っていた(例: 着順1-7-5の値が 1-5-7 に化ける)。
                # ここで second=行頭th(=実3着), third=列(=実2着) を正しい順に並べ直す。
                row_bike = second   # 行頭th = 実際の3着
                col_bike = third    # 列ラベル = 実際の2着
                key = str(first) + "-" + str(col_bike) + "-" + str(row_bike)
                out[key] = fv
    return out


_GAMBOO_ODDS_CACHE = {}  # key=(date,venue,rno) -> {"odds":{...}, "ts":epoch}


def fetch_gamboo_trifecta_odds(venue, date_str, race_no, force=False):
    """gambooから3連単オッズ取得。返り値 {"ok","odds","diag"}。
    既存の _scrape_refund_gamboo と同じID構造(code+base_date / +day2桁+'00')
    を 0〜3日前まで遡って構築し、fetch_with_retry でHTML取得→パース。
    """
    import time as _t
    ck = (str(date_str), str(venue), str(race_no))
    if (not force) and ck in _GAMBOO_ODDS_CACHE:
        c = _GAMBOO_ODDS_CACHE[ck]
        if (_t.time() - c["ts"]) < 90:
            return {"ok": True, "odds": c["odds"],
                    "diag": {"step": "cache", "n": len(c["odds"])}}
    diag = {"step": "", "url": "", "tried": []}
    code = NAME_TO_CODE.get(venue)
    if not code:
        diag["step"] = "code_not_found:" + str(venue)
        return {"ok": False, "odds": {}, "diag": diag}
    fk = None
    try:
        fk = RESULTS._scraper
    except Exception:
        fk = None
    if fk is None:
        diag["step"] = "no_scraper"
        return {"ok": False, "odds": {}, "diag": diag}
    from datetime import datetime as _dt, timedelta
    try:
        actual_dt = _dt.strptime(str(date_str), "%Y%m%d")
    except Exception:
        diag["step"] = "bad_date"
        return {"ok": False, "odds": {}, "diag": diag}
    diff_days = 0
    while diff_days < 4:
        base_dt = actual_dt - timedelta(days=diff_days)
        base_date_str = base_dt.strftime("%Y%m%d")
        day = diff_days + 1
        cup_id = code + base_date_str
        sched_id = code + base_date_str + str(day).zfill(2) + "00"
        diff_days = diff_days + 1
        # レース番号は gamboo の本来形式である2桁ゼロ埋めを優先し、
        # 取れない場合は従来の1桁(ゼロ埋めなし)もフォールバックで試す。
        rn_forms = [str(int(race_no)).zfill(2), str(int(race_no))]
        seen_rn = []
        for rn in rn_forms:
            if rn in seen_rn:
                continue
            seen_rn.append(rn)
            url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/odds/"
                   + cup_id + "/" + sched_id + "/"
                   + rn + "/3rentan/")
            diag["tried"].append(url)
            try:
                status, html = fk.fetch_with_retry(url)
            except Exception as e:
                diag["last_err"] = str(e)[:60]
                continue
            if status != 200 or not html:
                continue
            odds = parse_gamboo_trifecta_odds(html)
            if odds:
                diag["step"] = "ok"
                diag["url"] = url
                diag["n"] = len(odds)
                _GAMBOO_ODDS_CACHE[ck] = {"odds": odds, "ts": _t.time()}
                _GAMBOO_CUP_CACHE[(str(date_str), str(venue))] = (
                    cup_id, sched_id)
                return {"ok": True, "odds": odds, "diag": diag}
    if not diag["step"]:
        diag["step"] = "no_odds_in_html"
    diag["url"] = diag["tried"][0] if diag["tried"] else ""
    return {"ok": False, "odds": {}, "diag": diag}


# 日付ごとのレースリストをメモリに保持 (キャッシュから読む)
_RACES_BY_DATE = {}


#   URLの /3rentan/ を /2shatan/ に変えて2車単オッズページを取得。
#   2車単表: 行頭th=1着車番, 列=2着車番(1着を除く昇順)。{"a-b": float}。
#   HTML構造がgamboo仕様と異なる場合に備え、複数パターンで拾う。
# ================================================================
def parse_gamboo_exacta_odds(html):
    """gambooオッズページHTMLから2車単オッズを {"a-b": float} で返す (a=1着,b=2着)。

    ★v288で全面修正。gambooの2車単表は
        列 = 1着 (ヘッダ行に車番)
        行 = 2着 (行頭thに車番)
        対角(1着==2着)は空セル
    という構造。旧版は「行頭th=1着 / 列=2着」と取り違えたうえ、
    空セルが列を1つ消費して以降が1つずつずれ、存在しない車番8まで出ていた。
    """
    out = {}
    if not html:
        return out
    m = re.search(r'<table[^>]*class="[^"]*odds[^"]*"[^>]*>(.*?)</table>',
                  html, re.S)
    if not m:
        return out
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', m.group(1), re.S)
    if not rows:
        return out

    def cells(row, tag):
        got = []
        for c in re.findall(r'<' + tag + r'[^>]*>(.*?)</' + tag + r'>',
                            row, re.S):
            got.append(re.sub(r"<[^>]+>", "", c).replace("\u00a0", " ").strip())
        return got

    # ヘッダ行から車立て(N)を決める。数字のセルの個数がそのまま出走数。
    ncar = 0
    ri = 0
    while ri < len(rows) and ri < 3:
        nums = []
        for t in ("th", "td"):
            for v in cells(rows[ri], t):
                if v.isdigit():
                    iv = int(v)
                    if 1 <= iv <= 9:
                        nums.append(iv)
        if len(nums) >= 5 and sorted(nums) == list(range(1, len(nums) + 1)):
            ncar = len(nums)
            break
        ri = ri + 1
    if ncar < 5:
        ncar = 7

    ri = 0
    while ri < len(rows):
        row = rows[ri]
        ri = ri + 1
        ths = cells(row, "th")
        second = None
        for v in ths:
            if v.isdigit() and 1 <= int(v) <= ncar:
                second = int(v)
                break
        if second is None:
            continue
        tds = cells(row, "td")
        # 行頭のラベルがtdで来る作りに備え、末尾N個だけを使う
        if len(tds) > ncar:
            tds = tds[len(tds) - ncar:]
        col = 0
        while col < len(tds):
            first = col + 1
            col = col + 1
            if first == second or first > ncar:
                continue
            txt = tds[col - 1]
            if not txt:
                continue
            try:
                fv = float(txt.replace(",", ""))
            except Exception:
                continue
            if fv > 0:
                out[str(first) + "-" + str(second)] = fv

    # 妥当性チェック: 車番がNを超えていたり、点数が極端に少なければ捨てる
    ok = True
    for k in out:
        ps = k.split("-")
        if int(ps[0]) > ncar or int(ps[1]) > ncar:
            ok = False
            break
    if not ok or len(out) < ncar * (ncar - 1) // 2:
        return {}
    return out


_GAMBOO_EXACTA_CACHE = {}  # key=(date,venue,rno) -> {"odds":{...}, "ts":epoch}


# 開催初日の解決結果 (会場,日付) -> (cup_id, sched_id)
#   同じ会場の12レースは開催初日が同じなので、1回解けば以降は1発で当たる。
#   ★オッズの値そのものはキャッシュしない。ここに入れるのはURLの部品だけ。
#   未確定レースは force=True でいつでも取り直せる。
_GAMBOO_CUP_CACHE = {}
# 解決に失敗した会場を覚える (date,venue) -> 最終失敗時刻。
#   開催していない会場を毎レース24回も叩き直さないため。
_GAMBOO_CUP_FAIL = {}
_GAMBOO_FAIL_TTL = 600


def _gamboo_cup_key(date_str, venue):
    return (str(date_str), str(venue))


def fetch_gamboo_exacta_odds(venue, date_str, race_no, force=False):
    """gambooから2車単オッズ取得。返り値 {"ok","odds","diag"}。
    fetch_gamboo_trifecta_odds と同じID構造で、URLの種別を 2shatan にする。"""
    import time as _t
    ck = (str(date_str), str(venue), str(race_no))
    if (not force) and ck in _GAMBOO_EXACTA_CACHE:
        c = _GAMBOO_EXACTA_CACHE[ck]
        if (_t.time() - c["ts"]) < 90:
            return {"ok": True, "odds": c["odds"],
                    "diag": {"step": "cache", "n": len(c["odds"])}}
    diag = {"step": "", "url": "", "tried": []}
    code = NAME_TO_CODE.get(venue)
    if not code:
        diag["step"] = "code_not_found:" + str(venue)
        return {"ok": False, "odds": {}, "diag": diag}
    fk = None
    try:
        fk = RESULTS._scraper
    except Exception:
        fk = None
    if fk is None:
        diag["step"] = "no_scraper"
        return {"ok": False, "odds": {}, "diag": diag}
    from datetime import datetime as _dt, timedelta
    try:
        actual_dt = _dt.strptime(str(date_str), "%Y%m%d")
    except Exception:
        diag["step"] = "bad_date"
        return {"ok": False, "odds": {}, "diag": diag}
    # 2車単ページのURL種別候補(gamboo仕様差に備え複数試す)
    kinds = ["2shatan", "nishatan", "2rentan"]
    # 開催初日が既知なら、その組み合わせだけを先に試す(1〜3リクエストで済む)
    ckey = _gamboo_cup_key(date_str, venue)
    hit = _GAMBOO_CUP_CACHE.get(ckey)
    if hit:
        cup_id = hit[0]
        sched_id = hit[1]
        good_kind = hit[2] if len(hit) > 2 else kinds[0]
        good_rn = hit[3] if len(hit) > 3 else "z2"
        if good_rn == "z2":
            rn = str(int(race_no)).zfill(2)
        else:
            rn = str(int(race_no))
        url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/odds/"
               + cup_id + "/" + sched_id + "/" + rn + "/" + good_kind + "/")
        diag["tried"].append(url)
        status = 0
        html = ""
        try:
            status, html = fk.fetch_with_retry(url)
        except Exception as e:
            diag["last_err"] = str(e)[:60]
        if status == 200 and html:
            odds = parse_gamboo_exacta_odds(html)
            if odds:
                diag["step"] = "ok(cup_cache)"
                diag["url"] = url
                diag["n"] = len(odds)
                _GAMBOO_EXACTA_CACHE[ck] = {"odds": odds, "ts": _t.time()}
                return {"ok": True, "odds": odds, "diag": diag}
            # ★ページは開けたのにオッズが空 = 発売前か終了後。
            #   ここで総当たりに落ちると1レースあたり24回も叩くことになる。
            diag["step"] = "empty(not_on_sale)"
            diag["url"] = url
            return {"ok": False, "odds": {}, "diag": diag}
    else:
        # 直近に解決を失敗した会場は、しばらく再挑戦しない
        ft = _GAMBOO_CUP_FAIL.get(ckey)
        if ft is not None and (_t.time() - ft) < _GAMBOO_FAIL_TTL:
            diag["step"] = "skip(recent_fail)"
            return {"ok": False, "odds": {}, "diag": diag}
    diff_days = 0
    while diff_days < 4:
        base_dt = actual_dt - timedelta(days=diff_days)
        base_date_str = base_dt.strftime("%Y%m%d")
        day = diff_days + 1
        cup_id = code + base_date_str
        sched_id = code + base_date_str + str(day).zfill(2) + "00"
        diff_days = diff_days + 1
        rn_forms = [str(int(race_no)).zfill(2), str(int(race_no))]
        seen_rn = []
        for rn in rn_forms:
            if rn in seen_rn:
                continue
            seen_rn.append(rn)
            for kind in kinds:
                url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/odds/"
                       + cup_id + "/" + sched_id + "/" + rn + "/" + kind + "/")
                diag["tried"].append(url)
                try:
                    status, html = fk.fetch_with_retry(url)
                except Exception as e:
                    diag["last_err"] = str(e)[:60]
                    continue
                if status != 200 or not html:
                    continue
                odds = parse_gamboo_exacta_odds(html)
                if odds:
                    diag["step"] = "ok"
                    diag["url"] = url
                    diag["n"] = len(odds)
                    _GAMBOO_EXACTA_CACHE[ck] = {"odds": odds, "ts": _t.time()}
                    rnf = "z2" if len(str(rn)) == 2 else "p1"
                    _GAMBOO_CUP_CACHE[ckey] = (cup_id, sched_id, kind, rnf)
                    if ckey in _GAMBOO_CUP_FAIL:
                        del _GAMBOO_CUP_FAIL[ckey]
                    return {"ok": True, "odds": odds, "diag": diag}
    if not diag["step"]:
        diag["step"] = "no_odds_in_html"
    diag["url"] = diag["tried"][0] if diag["tried"] else ""
    if not hit:
        # 総当たりでも解決できなかった。しばらく再挑戦しない。
        _GAMBOO_CUP_FAIL[ckey] = _t.time()
    return {"ok": False, "odds": {}, "diag": diag}

def load_races(date_str):
    if date_str in _RACES_BY_DATE:
        _cached = _RACES_BY_DATE[date_str]
        # 空(0件)がキャッシュされている場合は信用しない。
        #   DB更新前にその日を集計すると空がキャッシュされ、DB更新後もプロセスが
        #   生きているとその空を返し続ける(過去集計0R)不具合を防ぐ。
        #   非空キャッシュはそのまま使う。
        if _cached and len(_cached[0]) > 0:
            return _cached
    # v330: 出走表は GitHub から読む。端末のファイルに依存しない。
    #   誰が起動しても同じものが見えるようにするため。
    #   GitHub に置いてあるのは4日分なので、それより前は開けない。
    races = fetch_today_cache_from_github(date_str)
    # 通信できないときの保険。端末に残っていれば使う (無ければ空)。
    if not races:
        try:
            races = pt.load_cache(date_str)
        except Exception:
            races = None
    # DBがあれば最後の保険として使う。DBは必須ではない。
    if not races and RESULTS is not None:
        try:
            races = RESULTS.get_races_for_date(date_str)
        except Exception:
            races = None
    if races is None:
        races = []

    # v329: today_cache(GitHub事前取得分)には グレード / 開催情報 /
    #   S・B回数 が入っていない。DB本体にはあるので、欠けている分だけ補う。
    #   これが無いと、集計の明細で「どんなレースだったか」が空欄になる。
    #   当日のレースはDBにまだ無いので、その場合は何も起きない(欠けたまま)。
    if races and RESULTS is not None:
        need = False
        i0 = 0
        while i0 < len(races):
            r0 = races[i0]
            i0 = i0 + 1
            if not (r0.get("grade") or "").strip():
                need = True
                break
            if not (r0.get("race_kind") or "").strip():
                need = True
                break
            # lap(周回中の並び)も today_cache には入っていない
            _lp0 = r0.get("lap")
            if _lp0 is None or _lp0 == {} or _lp0 == []:
                need = True
                break
        if need:
            try:
                db_rows = RESULTS.get_races_for_date(date_str)
            except Exception:
                db_rows = []
            if db_rows:
                dbmap = {}
                j0 = 0
                while j0 < len(db_rows):
                    dbmap[race_key(db_rows[j0])] = db_rows[j0]
                    j0 = j0 + 1
                k0 = 0
                while k0 < len(races):
                    r1 = races[k0]
                    k0 = k0 + 1
                    d1 = dbmap.get(race_key(r1))
                    if not d1:
                        continue
                    for fld in ("grade", "race_kind"):
                        if not (r1.get(fld) or "").strip():
                            v1 = d1.get(fld)
                            if v1:
                                r1[fld] = v1
                    # 周回中の並び
                    _lp1 = r1.get("lap")
                    if _lp1 is None or _lp1 == {} or _lp1 == []:
                        _lp2 = d1.get("lap")
                        if _lp2:
                            r1["lap"] = _lp2
                    # S / B は選手ごと
                    pl1 = r1.get("players") or {}
                    pl2 = d1.get("players") or {}
                    for bk in pl1:
                        a1 = pl1[bk]
                        a2 = pl2.get(bk)
                        if not isinstance(a1, dict) or not isinstance(a2, dict):
                            continue
                        if a1.get("s") is None and a2.get("s") is not None:
                            a1["s"] = a2.get("s")
                        if a1.get("b") is None and a2.get("b") is not None:
                            a1["b"] = a2.get("b")

    rmap = {}
    i = 0
    while i < len(races):
        rmap[race_key(races[i])] = races[i]
        i = i + 1
    result = (races, rmap)
    # 実データが取れたときだけ永続キャッシュ。空は焼き付けない(次回再復元)。
    if len(races) > 0:
        _RACES_BY_DATE[date_str] = result
    return result


# ============================================================
# API: 当日キャッシュ生成 (キャッシュが無い時に全会場スクレイピング)
# ============================================================
def _build_cache_for_date(date_str):
    """指定日の出走表をスクレイプしてキャッシュ保存する。
    返り値: (built:bool, n:int, reason:str)。既存キャッシュがあれば built=False。"""
    existing = pt.load_cache(date_str)
    if existing:
        return (False, len(existing), "既存キャッシュあり")
    # GitHub事前取得分があればスクレイピング不要
    gh_n = _try_github_today_cache(date_str)
    if gh_n:
        return (True, gh_n, "GitHub事前取得分を利用")
    try:
        import predict_v14_wind_unified as engine
    except Exception as e:
        return (False, 0, "engine読み込み失敗:" + str(e)[:50])
    try:
        tdt = datetime.strptime(date_str, "%Y%m%d")
    except Exception:
        return (False, 0, "日付不正")
    all_races = []
    for pc in engine.CODES:
        pn = engine.CODES[pc]
        try:
            res = engine.check_venue_open(pc, pn, tdt)
        except Exception:
            continue
        if not res:
            continue
        try:
            pc2, pn2, bd, dy = res
            vr = engine.fetch_venue_races(pc2, pn2, bd, dy, tdt, date_str)
        except Exception:
            continue
        if vr:
            all_races.extend(vr)
    if not all_races:
        return (False, 0, "開催会場なし")
    try:
        pt.save_cache(date_str, all_races)
    except Exception as e:
        return (False, 0, "保存失敗:" + str(e)[:50])
    _RACES_BY_DATE.pop(date_str, None)
    return (True, len(all_races), "ok")


@app.route("/api/db_status")
def api_db_status():
    """メニュー表示用: DBの最新日付・サイズ・場所を返す。
    最新日は末尾2MBだけを走査して取得 (DBは時系列追記なので末尾=最新)"""
    info = {"exists": False, "db_date": "", "size_mb": 0, "location": ""}
    if RESULTS is None:
        return jsonify(info)
    p = RESULTS.db_path
    if not os.path.exists(p):
        return jsonify(info)
    size = os.path.getsize(p)
    info["exists"] = True
    info["size_mb"] = round(size / 1048576.0, 1)
    if p.startswith(KEIRIN_DB_DIR):
        info["location"] = "専用フォルダ"
    else:
        info["location"] = "従来 (Download直下)"
    try:
        f = open(p, "rb")
        try:
            window = 2 * 1048576
            if size > window:
                f.seek(size - window)
                f.readline()  # 行の途中を読み捨て
            data = f.read().decode("utf-8", "ignore")
        finally:
            f.close()
        dates = re.findall(r'"date"\s*:\s*"?(\d{8})"?', data)
        if not dates:
            # dateフィールドがない場合はrace_id (会場2桁+日付8桁) から復元
            dates = [x for x in re.findall(r'"race_id"\s*:\s*"\d{2}(\d{8})', data)]
        if dates:
            info["db_date"] = max(dates)
    except Exception as e:
        print("[db_status] " + str(e)[:60])
    return jsonify(info)


@app.route("/api/build_cache")
def api_build_cache():
    date_str = request.args.get("date", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")

    # 同期は明示的に sync=1 が指定されたときだけ実行する。
    # 託宣ボタン(通常呼び出し)では同期しない → 435MB DB再書込みによる
    # 重さ・ダウンを回避。最新DB/統計を取り込みたいときだけ sync=1 で呼ぶ。
    do_sync = request.args.get("sync", "").strip() in ("1", "true", "yes")
    db_sync_msg = ""
    if do_sync:
        try:
            _upd, db_sync_msg = sync_db_months()
        except Exception as _e:
            db_sync_msg = "同期エラー:" + str(_e)[:60]
        try:
            _dupd, _dmsg = sync_dicts_files()
            db_sync_msg = db_sync_msg + " / 統計:" + _dmsg
        except Exception as _e:
            db_sync_msg = db_sync_msg + " / 統計同期エラー:" + str(_e)[:50]
    else:
        db_sync_msg = "同期スキップ(sync=1で実行)"

    # 既にキャッシュがある場合: ライン空き(ミッドナイト等)があれば
    # GitHubの午後更新版での差し替えを試みる
    existing = pt.load_cache(date_str)
    if existing:
        refreshed = 0
        try:
            refreshed = _try_github_line_refresh(date_str, existing)
        except Exception:
            refreshed = 0
        if refreshed:
            return jsonify({"date": date_str, "built": True,
                            "races": refreshed, "venues": [],
                            "source": "github-line-refresh",
                            "db_sync": db_sync_msg})
        return jsonify({"date": date_str, "built": False,
                        "races": len(existing), "reason": "既存キャッシュあり",
                        "line_missing": _count_lineless(existing),
                        "db_sync": db_sync_msg})

    # GitHub事前取得分があればスクレイピング不要 → 即計算へ
    gh_n = _try_github_today_cache(date_str)
    if gh_n:
        return jsonify({"date": date_str, "built": True,
                        "races": gh_n, "venues": [],
                        "source": "github", "db_sync": db_sync_msg})

    try:
        import predict_v14_wind_unified as engine
    except Exception as e:
        return jsonify({"date": date_str, "built": False,
                        "error": "engine 読み込み失敗: " + str(e)})

    try:
        tdt = datetime.strptime(date_str, "%Y%m%d")
    except Exception:
        return jsonify({"date": date_str, "built": False, "error": "日付不正"})

    all_races = []
    venues_found = []
    errors = []
    # engine.CODES: コード -> 会場名
    for pc in engine.CODES:
        pn = engine.CODES[pc]
        try:
            res = engine.check_venue_open(pc, pn, tdt)
        except Exception as e:
            continue
        if not res:
            continue
        try:
            pc2, pn2, bd, dy = res
            vr = engine.fetch_venue_races(pc2, pn2, bd, dy, tdt, date_str)
        except Exception as e:
            errors.append(pn + ":" + str(e)[:50])
            continue
        if vr:
            all_races.extend(vr)
            venues_found.append(pn)

    if not all_races:
        return jsonify({"date": date_str, "built": False,
                        "venues": [], "reason": "開催会場なし or 取得0件",
                        "errors": errors[:5]})

    # キャッシュ保存
    try:
        pt.save_cache(date_str, all_races)
    except Exception as e:
        return jsonify({"date": date_str, "built": False,
                        "error": "保存失敗: " + str(e)[:80]})

    # メモリキャッシュをクリアして次の読み込みで反映
    _RACES_BY_DATE.pop(date_str, None)
    return jsonify({"date": date_str, "built": True,
                    "races": len(all_races), "venues": venues_found,
                    "source": "scrape", "db_sync": db_sync_msg})


# ============================================================
# API: 欠けたレースだけ取り直す  (v329)
#
# GitHubの当日事前取得データには、たまに戦績(h1/h2/h3)と発走時刻が
# 落ちているレースが混ざる。競走得点とラインは入っているので
# 一見正常に見えるが、平均着順が出せず raw_score が全車で失敗する。
#
# そのレースだけ元サイトから取り直してキャッシュに差し替える。
# 正常に取れているレースには触らない。
# ============================================================
@app.route("/api/refetch_race")
def api_refetch_race():
    date_str = request.args.get("date", "").strip()
    venue = request.args.get("venue", "").strip()
    if not date_str:
        return jsonify({"ok": False, "error": "date が必要"}), 400

    def _lacks_hist(r):
        pl = r.get("players") or {}
        if not pl:
            return True
        for bk in pl:
            p = pl[bk]
            if not isinstance(p, dict):
                continue
            for k in ("h1", "h2", "h3"):
                v = p.get(k, "")
                if isinstance(v, str) and v.strip() and v.strip() != "なし":
                    return False
        return True

    existing = pt.load_cache(date_str)
    if not existing:
        return jsonify({"ok": False, "error": "キャッシュがありません"})

    # 取り直しが要るレースを洗い出す
    need = []
    i = 0
    while i < len(existing):
        r = existing[i]
        i = i + 1
        if venue and r.get("place", "") != venue:
            continue
        if _lacks_hist(r):
            need.append(r)
    if not need:
        return jsonify({"ok": True, "fixed": 0, "checked": len(existing),
                        "message": "取り直しが必要なレースはありません"})

    # 対象会場だけ取り直す
    venues = {}
    for r in need:
        venues[r.get("place", "")] = 1

    try:
        import predict_v14_wind_unified as engine
    except Exception as e:
        return jsonify({"ok": False, "error": "engine 読み込み失敗: " + str(e)[:80]})
    try:
        tdt = datetime.strptime(date_str, "%Y%m%d")
    except Exception:
        return jsonify({"ok": False, "error": "日付不正"})

    fresh = {}
    errs = []
    for pc in engine.CODES:
        pn = engine.CODES[pc]
        if pn not in venues:
            continue
        try:
            res = engine.check_venue_open(pc, pn, tdt)
        except Exception as e:
            errs.append(pn + ":" + str(e)[:40])
            continue
        if not res:
            continue
        try:
            pc2, pn2, bd, dy = res
            vr = engine.fetch_venue_races(pc2, pn2, bd, dy, tdt, date_str)
        except Exception as e:
            errs.append(pn + ":" + str(e)[:40])
            continue
        j = 0
        while j < len(vr or []):
            nr = vr[j]
            j = j + 1
            fresh[race_key(nr)] = nr

    # 差し替え。取り直しても戦績が無いものは元のまま残す。
    fixed = 0
    still = []
    k = 0
    while k < len(existing):
        r = existing[k]
        kk = race_key(r)
        if _lacks_hist(r) and kk in fresh:
            nr = fresh[kk]
            if not _lacks_hist(nr):
                existing[k] = nr
                fixed = fixed + 1
            else:
                still.append(kk)
        elif _lacks_hist(r):
            still.append(kk)
        k = k + 1

    if fixed:
        try:
            pt.save_cache(date_str, existing)
        except Exception as e:
            return jsonify({"ok": False, "error": "保存失敗: " + str(e)[:80]})
        _RACES_BY_DATE.pop(date_str, None)

    return jsonify({"ok": True, "fixed": fixed, "target": len(need),
                    "still_missing": still[:20], "errors": errs[:5]})


# ============================================================
# API: 会場一覧 (初期表示用・計算なし)
# ============================================================
# ============================================================
# v330: 会場ボタンにバンク要目を出すための下ごしらえ。
#   bank_data.json は鍵の名前が版によって違うので、候補を順に見る。
#   数にできないものは None にして、平均の計算から外す。
# ============================================================
_BANK_KEYS = {
    "circ":     ["circumference", "bank_length", "length", "shuucho", "周長"],
    "cant":     ["cant", "cant_degree", "cant_deg", "カント"],
    "straight": ["straight", "straight_length", "home_straight",
                 "homestretch", "straight_distance", "直線", "直線長"],
}


def _bank_num(v):
    try:
        return float(str(v).replace("m", "").replace("°", "").strip())
    except Exception:
        return None


def _bank_of(bank_data, venue):
    out = {"circ": None, "cant": None, "straight": None}
    if not bank_data:
        return out
    bd = bank_data.get(venue) or {}
    if not bd:
        return out
    for fld in _BANK_KEYS:
        for k in _BANK_KEYS[fld]:
            if k in bd:
                n = _bank_num(bd.get(k))
                if n is not None:
                    out[fld] = n
                    break
    return out


def _bank_avg(bank_data):
    """全国平均。会場ボタンで Ave. として併記する。"""
    acc = {"circ": [], "cant": [], "straight": []}
    if bank_data:
        for v in bank_data:
            b = _bank_of(bank_data, v)
            for fld in acc:
                if b[fld] is not None:
                    acc[fld].append(b[fld])
    out = {}
    for fld in acc:
        out[fld] = round(sum(acc[fld]) / len(acc[fld]), 1) if acc[fld] else None
    return out


@app.route("/api/venues")
def api_venues():
    date_str = request.args.get("date", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")

    races, _rmap = load_races(date_str)
    if not races:
        return jsonify({
            "date": date_str,
            "venues": [],
            "message": "キャッシュなし (cache_" + date_str + ".json が必要)",
        })

    # 会場別にグルーピング
    venue_to_races = {}
    i = 0
    while i < len(races):
        r = races[i]
        v = r.get("place", "不明")
        if v not in venue_to_races:
            venue_to_races[v] = []
        venue_to_races[v].append(r)
        i = i + 1

    # v330: バンク要目 (会場ボタンに出す)
    # v333: 例外を握り潰すと「なぜか空」で終わってしまうので、理由を持ち帰る。
    _bank_err = ""
    try:
        _bd_all = get_dicts().get("bank_data") or {}
    except Exception as _be:
        _bd_all = {}
        _bank_err = str(_be)[:120]

    out_venues = []
    for v in venue_to_races:
        rl = sorted(venue_to_races[v], key=lambda r: _safe_int(r.get("race_no", 99)))
        first_post = rl[0].get("post_time", "") if rl else ""
        race_items = []
        j = 0
        while j < len(rl):
            r = rl[j]
            race_items.append({
                "key": race_key(r),
                "race_no": r.get("race_no", "?"),
                "post_time": r.get("post_time", "--:--"),
                "has_line": bool(r.get("line", "").strip()),
                # v330: Rボタンにグレードと種別を出す
                "grade": (r.get("grade") or "").strip(),
                "race_kind": (r.get("race_kind") or "").strip(),
            })
            j = j + 1
        out_venues.append({
            "name": v,
            "first_post": first_post,
            "race_count": len(rl),
            "races": race_items,
            "bank": _bank_of(_bd_all, v),
        })

    # 第1R発走時刻順
    # v330: "--:--" を空と同じ「不明」として末尾に送る。
    #   これをしないと "-" が数字より小さいため、発走時刻が取れていない会場が
    #   先頭に来る (8/26 に熊本が左端に出たのはこれが原因)。
    def _post_key(v):
        fp = str(v.get("first_post") or "").strip()
        if not fp or fp.replace("-", "").replace(":", "") == "":
            return "99:99"
        return fp

    out_venues.sort(key=_post_key)

    # v333: バンクが出ないときに何が起きているかを画面から確かめられるようにする。
    #   /api/venues?date=... をブラウザで直接開けば中身が読める。
    _bd_keys = []
    _bd_sample = {}
    try:
        _bd_keys = list(_bd_all.keys())[:5]
        if _bd_keys:
            _bd_sample = _bd_all.get(_bd_keys[0]) or {}
    except Exception:
        pass
    return jsonify({"date": date_str, "venues": out_venues,
                    "bank_avg": _bank_avg(_bd_all),
                    "bank_diag": {
                        "n_venues_in_dict": len(_bd_all),
                        "err": _bank_err,
                        "sample_keys": _bd_keys,
                        "sample_fields": sorted([str(_k) for _k in _bd_sample]),
                        "asked": sorted([str(_v) for _v in venue_to_races]),
                    }})


def _safe_int(v):
    try:
        return int(v)
    except Exception:
        return 99


# ============================================================
# 選手別 決まり手 (出走表用)
# ============================================================
def _bike_role_from_chunks(chunks):
    """車番(int) -> 役割文字列。集計スクリプトと同一定義。
       単騎ライン=単騎 / 多人数=先頭/番手/3番手/4番手..."""
    role = {}
    i = 0
    while i < len(chunks):
        c = chunks[i]
        size = len(c)
        j = 0
        while j < len(c):
            bk = _safe_int(c[j])
            if bk == 99:
                j = j + 1
                continue
            if size == 1:
                role[bk] = "単騎"
            else:
                pos = j + 1
                if pos == 1:
                    role[bk] = "先頭"
                elif pos == 2:
                    role[bk] = "番手"
                else:
                    role[bk] = str(pos) + "番手"
            j = j + 1
        i = i + 1
    return role


def _kimari_diag_reason(player_key, role):
    """_kimari_for_player が None を返す場合の理由を文字列で返す (診断用)。
    成功時は None (理由なし)。"""
    if not player_key:
        return "キー無し(氏名|期が作れない)"
    rec = KIMARI_PLAYER_ROLE.get(player_key)
    if rec is None:
        return "決まり手データに該当キー無し: " + str(player_key)
    by_role = rec.get("by_role", {})
    cell = by_role.get(role)
    if not cell:
        avail = ",".join(list(by_role.keys())) if by_role else "(役割データ無し)"
        return ("役割『" + str(role) + "』のデータ無し / 保有役割=[" + avail + "]")
    den = cell.get("_n", 0)
    if not den:
        return "役割『" + str(role) + "』の母数0"
    return None


def _kimari_for_player(player_key, role):
    """player_key(氏名|期) と 今回の役割 から、その役割の全期間 逃捲差マ を返す。
       返り値: {"role":役割, "items":[{"k":"逃","rate":0.69,"hit":69,"den":100}, ...], "den":N}
       データが無ければ None。"""
    if not player_key:
        return None
    rec = KIMARI_PLAYER_ROLE.get(player_key)
    if rec is None:
        return None
    by_role = rec.get("by_role", {})
    cell = by_role.get(role)
    if not cell:
        return None
    den = cell.get("_n", 0)
    if not den:
        return None
    items = []
    order = ["逃", "捲", "差", "マ"]
    oi = 0
    while oi < len(order):
        kk = order[oi]
        hit = cell.get(kk, 0)
        rate = round(hit / float(den), 4) if den else 0.0
        items.append({"k": kk, "rate": rate, "hit": hit, "den": den})
        oi = oi + 1
    return {"role": role, "items": items, "den": den}


def _kimari_total_for_player(player_key):
    """player_key(氏名|期) から、役割を問わない通算 逃捲差マ を返す。
       返り値の形は _kimari_for_player と同じ (role="通算")。データ無しは None。"""
    if not player_key:
        return None
    rec = KIMARI_PLAYER_ROLE.get(player_key)
    if rec is None:
        return None
    tot = rec.get("total", {})
    den = tot.get("_n", 0)
    if not den:
        return None
    items = []
    order = ["逃", "捲", "差", "マ"]
    oi = 0
    while oi < len(order):
        kk = order[oi]
        hit = tot.get(kk, 0)
        rate = round(hit / float(den), 4) if den else 0.0
        items.append({"k": kk, "rate": rate, "hit": hit, "den": den})
        oi = oi + 1
    return {"role": "通算", "items": items, "den": den}



# ============================================================
# API: ライン情報の再取得 (会場にライン空Rがある時に呼ぶ)
#   engine.fetch_venue_races で会場単位再取得し、キャッシュ更新。
# ============================================================
@app.route("/api/refetch_lines")
def api_refetch_lines():
    date_str = request.args.get("date", "").strip()
    venue = request.args.get("venue", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    if not venue:
        return jsonify({"error": "venue が必要"}), 400

    races, rmap = load_races(date_str)
    # この会場にライン空のRがあるか
    has_empty = False
    i = 0
    while i < len(races):
        r = races[i]
        if r.get("place", "") == venue and not r.get("line", "").strip():
            has_empty = True
            break
        i = i + 1
    if not has_empty:
        return jsonify({"venue": venue, "updated": 0, "reason": "ライン空のRなし"})

    # engine で再取得
    try:
        import predict_v14_wind_unified as engine
    except Exception as e:
        return jsonify({"venue": venue, "updated": 0,
                        "error": "engine 読み込み失敗: " + str(e)})

    try:
        tdt = datetime.strptime(date_str, "%Y%m%d")
    except Exception:
        return jsonify({"venue": venue, "updated": 0, "error": "日付不正"})

    # 会場コードを引く
    place_code = None
    for pc in engine.CODES:
        if engine.CODES[pc] == venue:
            place_code = pc
            break
    if place_code is None:
        return jsonify({"venue": venue, "updated": 0, "error": "会場コード不明"})

    try:
        res = engine.check_venue_open(place_code, venue, tdt)
        if not res:
            return jsonify({"venue": venue, "updated": 0, "reason": "開催情報なし"})
        pc, pn, bd, dy = res
        new_races = engine.fetch_venue_races(pc, pn, bd, dy, tdt, date_str)
    except Exception as e:
        return jsonify({"venue": venue, "updated": 0,
                        "error": "再取得失敗: " + str(e)[:100]})

    if not new_races:
        return jsonify({"venue": venue, "updated": 0, "reason": "取得0件"})

    # キャッシュ更新: 同会場のレースを新データで置き換え
    new_by_rno = {}
    j = 0
    while j < len(new_races):
        new_by_rno[new_races[j].get("race_no")] = new_races[j]
        j = j + 1

    updated = 0
    k = 0
    while k < len(races):
        r = races[k]
        if r.get("place", "") == venue:
            rno = r.get("race_no")
            if rno in new_by_rno:
                nr = new_by_rno[rno]
                # ラインや天候など主要フィールドを更新
                for fld in ("line", "weather", "players", "post_time"):
                    if nr.get(fld):
                        r[fld] = nr[fld]
                if nr.get("line", "").strip():
                    updated = updated + 1
        k = k + 1

    # キャッシュ保存 + メモリ再構築
    try:
        pt.save_cache(date_str, races)
    except Exception:
        pass
    _RACES_BY_DATE.pop(date_str, None)

    return jsonify({"venue": venue, "updated": updated})


# ============================================================
# API: 会場内レースの太枠フラグ (会場タップ時に呼ぶ)
#   各レースを predict_for_race で評価し displayable を返す。
#   結果本体は捨てる (タップ時に再計算)。
# ============================================================
@app.route("/api/venue_flags")
def api_venue_flags():
    date_str = request.args.get("date", "").strip()
    venue = request.args.get("venue", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    if not venue:
        return jsonify({"error": "venue が必要"}), 400

    races, _rmap = load_races(date_str)
    d = get_dicts()
    now = datetime.now()

    # 対象会場のレースを抽出
    target = []
    i = 0
    while i < len(races):
        r = races[i]
        if r.get("place", "") == venue:
            target.append(r)
        i = i + 1

    def eval_one(r):
        k = race_key(r)
        finished_time = _is_post_passed(r, date_str, now)
        # 発走前のレースには結果が存在しない。結果取得を呼ぶと未確定時に
        # 435MBのDB全件インデックス構築が走りメモリ不足(OOM)で落ちるため、
        # 終了済みレースのみ結果照合する。
        if finished_time:
            finished = _result_and_hit(r, d["venue_home_dir"], d["bank_data"],
                                       allow_scrape=True)
        else:
            finished = {"has_result": False, "hit": False,
                        "trifecta": "", "refund_3t": 0}
        return (k, {
            "displayable": _quick_displayable(r, d["venue_home_dir"], d["bank_data"]),
            "labels": _quick_label_kinds(r),
            "has_result": finished["has_result"],
            "hit": finished["hit"],
            "trifecta": finished.get("trifecta", ""),
            "refund_3t": finished.get("refund_3t", 0),
            "finished": finished_time,
        })

    flags = {}
    # 並列実行 (スクレイピングのI/O待ちを並列化して高速化)
    ex = ThreadPoolExecutor(max_workers=6)
    futs = [ex.submit(eval_one, r) for r in target]
    for f in futs:
        try:
            k, v = f.result()
            flags[k] = v
        except Exception:
            pass
    ex.shutdown(wait=True)
    return jsonify({"venue": venue, "flags": flags})


def _latest_race_date(pdata, today_dt):
    """選手の h1/h2/h3 戦績欄から最新の出走日を返す。
    形式例: "函館 Ｆ１ 5/23 4・3・4" / "なし"。
    月/日のみなので today を基準に最も近い過去の日付として年を推定。
    見つからなければ None。
    """
    best = None
    for key in ("h1", "h2", "h3"):
        s = pdata.get(key, "")
        if not s or not isinstance(s, str) or s.strip() == "なし":
            continue
        m = re.search(r'(\d{1,2})/(\d{1,2})', s)
        if not m:
            continue
        try:
            mo = int(m.group(1)); da = int(m.group(2))
        except Exception:
            continue
        # 年推定: まず当年。未来日になるなら前年。
        yr = today_dt.year
        try:
            cand = datetime(yr, mo, da)
        except Exception:
            continue
        if cand > today_dt:
            try:
                cand = datetime(yr - 1, mo, da)
            except Exception:
                continue
        if best is None or cand > best:
            best = cand
    return best


def _layoff_label(pdata, today_dt):
    """1ヶ月(31日)以上の長期離脱明けなら緑ラベル情報を返す。
    返り値: dict {"kind","text","hit","den"} or None
    """
    last_dt = _latest_race_date(pdata, today_dt)
    if last_dt is None:
        return None
    gap_days = (today_dt - last_dt).days
    if gap_days >= 31:
        return {"kind": "layoff", "text": "離脱明", "hit": 0, "den": 0,
                "gap": gap_days}
    return None


def _is_post_passed(race, date_str, now):
    """発走時刻を過ぎているか (終了済み判定)"""
    post = race.get("post_time", "")
    if not post:
        return False
    m = re.match(r'(\d{1,2}):(\d{2})', post.strip())
    if not m:
        return False
    try:
        y = int(date_str[:4]); mo = int(date_str[4:6]); da = int(date_str[6:8])
        h = int(m.group(1)); mi = int(m.group(2))
        post_dt = datetime(y, mo, da, h, mi)
    except Exception:
        return False
    return now > post_dt


def _maria6_combos(date_str, rkey):
    """事前計算キャッシュから maria(おまかせ) の上位6点の combo集合を返す。
    キャッシュが無い/該当なしなら空集合。Rボタン的中判定に使う。"""
    try:
        cache = _get_cathedral_cache(date_str)
    except Exception:
        cache = None
    if not cache or rkey not in cache:
        return set()
    entry = cache[rkey]
    if not entry.get("ok"):
        return set()
    all_list = entry.get("all", [])
    try:
        filtered = _cat_filter_candidates(all_list, "maria", None, None,
                                          "fixed", None)
        top6 = _cat_finalize(filtered, 6)
    except Exception:
        return set()
    out = set()
    for c in top6:
        t = c.get("3t", "")
        if t:
            out.add(t)
    return out


def _result_and_hit(race, venue_home_dir, bank_data, allow_scrape=False):
    """そのレースの結果有無と、maria6点が的中したかを返す。
    返り値: {"has_result": bool, "hit": bool, "trifecta": str}
    的中判定は maria(おまかせ)の上位6点に結果3連単が含まれるかで行う。"""
    out = {"has_result": False, "hit": False, "trifecta": "", "refund_3t": 0}
    if RESULTS is None:
        return out
    venue = race.get("place", "")
    date_str = race.get("date", "")
    race_no = race.get("race_no", "")
    res = RESULTS.get_result(venue, date_str, race_no, allow_scrape=allow_scrape)
    if not res.get("has_result"):
        return out
    out["has_result"] = True
    out["trifecta"] = res.get("trifecta", "")
    out["refund_3t"] = res.get("refund_3t", 0)

    # v329: ここで大聖堂キャッシュ(maria6点)との照合をしていたが、
    #   的中判定は「稼働条件の買い目が当たったか」に変えたので不要になった。
    #   照合のたびに GitHub へ取りに行っており(タイムアウト60秒)、
    #   会場を押してからの待ち時間の一因になっていた。
    #   結果と配当だけ返す。hit は使う側が判定する。
    return out


def _result_and_hit_tavern(race, venue_home_dir, bank_data, allow_scrape=False):
    """(旧) 酒場/御告フォーメーションでの的中判定。参考用に温存。"""
    out = {"has_result": False, "hit": False, "trifecta": "", "refund_3t": 0}
    if RESULTS is None:
        return out
    venue = race.get("place", "")
    date_str = race.get("date", "")
    race_no = race.get("race_no", "")
    res = RESULTS.get_result(venue, date_str, race_no, allow_scrape=allow_scrape)
    if not res.get("has_result"):
        return out
    out["has_result"] = True
    out["trifecta"] = res.get("trifecta", "")
    out["refund_3t"] = res.get("refund_3t", 0)

    # 予測買い目を組んで的中照合
    try:
        payload = build_race_payload(race, venue_home_dir, bank_data)
    except Exception:
        return out
    if payload.get("status") != "ok":
        return out
    trifecta = res.get("trifecta", "")
    if not trifecta:
        return out
    if _check_hit(payload.get("patterns", []), trifecta):
        out["hit"] = True
    return out


def _expand_formation(form):
    """ "1-4-7235" → set(["1-4-7","1-4-2","1-4-3","1-4-5"]) """
    parts = form.split("-")
    if len(parts) != 3:
        return set()
    firsts = list(parts[0])
    seconds = list(parts[1])
    thirds = list(parts[2])
    out = set()
    for a in firsts:
        for b in seconds:
            for c in thirds:
                if a != b and b != c and a != c:
                    out.add(a + "-" + b + "-" + c)
    return out


def _check_hit(patterns, trifecta):
    """買い目パターン群に trifecta (例 "4-1-2") が含まれるか"""
    tri = trifecta.replace(" ", "")
    for p in patterns:
        for form in p.get("formations", []):
            if tri in _expand_formation(form):
                return True
    return False


def _quick_label_kinds(race):
    """このレースに含まれる穴/勝負弱/離脱明けラベルの種類を返す
    返り値: {"ana": bool, "weak": bool, "layoff": bool}
    """
    out = {"ana": False, "weak": False, "layoff": False}
    venue = race.get("place", "")
    players = race.get("players", {})
    if not isinstance(players, dict):
        return out
    # 離脱明け判定用の当日日付
    try:
        _today_dt = datetime.strptime(race.get("date", ""), "%Y%m%d")
    except Exception:
        _today_dt = datetime.now()
    ana_ok = ANA is not None and getattr(ANA, "available", False)
    for bs in players:
        pdata = players[bs]
        if not isinstance(pdata, dict):
            continue
        # 離脱明け (ANA不要)
        if not out["layoff"]:
            if _layoff_label(pdata, _today_dt):
                out["layoff"] = True
        if ana_ok:
            info = pt.parse_full_info(pdata.get("full_info", ""))
            try:
                lab = ANA.judge(info["name"], venue)
            except Exception:
                lab = {"kind": None}
            if lab.get("kind") == "ana":
                out["ana"] = True
            elif lab.get("kind") == "weak":
                out["weak"] = True
        if out["ana"] and out["weak"] and out["layoff"]:
            break
    return out


def _quick_displayable(race, venue_home_dir, bank_data):
    """そのレースが詳細表示に値するか (風判定・該当セル存在まで)。
    predict_for_race を呼んで valid かどうかだけ見る。
    """
    line_str = race.get("line", "")
    chunks = pt.parse_line_chunks(line_str)
    if chunks is None:
        return False
    # 個人戦判定
    is_kojinsen = True
    i = 0
    while i < len(chunks):
        if len(chunks[i]) > 1:
            is_kojinsen = False
            break
        i = i + 1
    if is_kojinsen:
        return False
    try:
        result = pt.predict_for_race(race, venue_home_dir, bank_data)
    except Exception:
        return False
    if result is None:
        return False
    if not bool(result.get("valid", False)):
        return False
    # 御告(託宣)成立条件: 決まり手遷移セルが存在すること。
    # __oraOmakasePredict は kimari.exists が無いと早期returnで託宣不成立になるため、
    # 表示可否(金枠)も御告成立に揃える。
    try:
        kp = build_kimari_payload(
            result.get("venue", ""),
            result.get("wind_pat", ""),
            result.get("speed_cls", ""),
        )
    except Exception:
        kp = None
    if not kp or not kp.get("exists", False):
        return False
    return True


# ============================================================
# API: 実績バックテスト用
#   /api/period_races : 期間内のキャッシュ済みレースkey一覧
#   /api/result       : レース結果(着順trifecta)と3連単払戻
# ============================================================
def _date_range_list(from_s, to_s):
    """from_s..to_s (YYYYMMDD) の日付文字列リストを返す(昇順)"""
    out = []
    try:
        sd = datetime.strptime(from_s, "%Y%m%d")
        ed = datetime.strptime(to_s, "%Y%m%d")
    except Exception:
        return out
    if ed < sd:
        sd, ed = ed, sd
    cur = sd
    one = timedelta(days=1)
    guard = 0
    while cur <= ed and guard < 400:
        out.append(cur.strftime("%Y%m%d"))
        cur = cur + one
        guard = guard + 1
    return out


@app.route("/api/period_races")
def api_period_races():
    """期間内のレースkey一覧を日付ごとに返す。
    scrape=1 のとき、未キャッシュの日は出走表をスクレイプして取得する。"""
    from_s = request.args.get("from", "").strip()
    to_s = request.args.get("to", "").strip()
    allow = request.args.get("scrape", "").strip() in ("1", "true", "yes")
    if not from_s or not to_s:
        return jsonify({"error": "from, to が必要(YYYYMMDD)"}), 400
    dates = _date_range_list(from_s, to_s)
    days = []
    total = 0
    built_days = 0
    for ds in dates:
        races, rmap = load_races(ds)
        # 未キャッシュかつ scrape 許可なら取得を試みる
        if (not races) and allow:
            try:
                built, n, reason = _build_cache_for_date(ds)
            except Exception:
                built = False
            if built:
                built_days = built_days + 1
                races, rmap = load_races(ds)
        keys = []
        i = 0
        while i < len(races):
            keys.append(race_key(races[i]))
            i = i + 1
        if keys:
            days.append({"date": ds, "keys": keys, "n": len(keys)})
            total = total + len(keys)
    return jsonify({"ok": True, "from": from_s, "to": to_s,
                    "days": days, "total": total, "built_days": built_days})


@app.route("/api/result")
def api_result():
    """レース結果と3連単払戻を返す。
    allow_scrape=1 のとき、払戻が無ければ gamboo 等から取得しキャッシュ保存。"""
    date_str = request.args.get("date", "").strip()
    rkey = request.args.get("key", "").strip()
    allow = request.args.get("scrape", "").strip() in ("1", "true", "yes")
    if not date_str or not rkey:
        return jsonify({"error": "date, key が必要"}), 400
    _races, rmap = load_races(date_str)
    race = rmap.get(rkey)
    if race is None:
        return jsonify({"ok": False, "error": "レース未キャッシュ: " + rkey})
    venue = race.get("place", "")
    race_no = race.get("race_no", "")
    if RESULTS is None:
        return jsonify({"ok": False, "error": "結果プロバイダ無効"})
    res = RESULTS.get_result(venue, date_str, race_no, allow_scrape=allow)
    return jsonify({
        "ok": bool(res.get("has_result")),
        "trifecta": res.get("trifecta", ""),
        "refund_3t": res.get("refund_3t", 0),
        "source": res.get("source", ""),
        "venue": venue, "race_no": race_no,
    })


# ============================================================
# API: 実績集計結果の保存・一覧・取得
#   保存先: DOWNLOAD_DIR/bt_results.jsonl (1行=1集計)
# ============================================================
def _bt_results_path():
    base = DOWNLOAD_DIR
    try:
        base = getattr(pt, "DATA_DIR", getattr(pt, "SAVE_DIR", DOWNLOAD_DIR))
    except Exception:
        pass
    return os.path.join(base, "bt_results.jsonl")


def _bt_days_dir():
    """★v327: 集計結果を1日1ファイルで置くフォルダ。

    従来は bt_results.jsonl 1本に全日分を入れ、**保存のたびに
    236MB全部を書き直していた**。書き直し中に止まると全滅する。
    実際に2026年212日分+2025年分を失った。

    1日1ファイルなら
      ・保存は当日ぶん(数百KB)だけ。全書き直しが無くなる
      ・途中で落ちてもその日のファイルだけで済む
      ・スクリプトで日別に作って置くこともできる
      ・GitHubへ日別で上げられる
    """
    base = DOWNLOAD_DIR
    try:
        base = getattr(pt, "DATA_DIR", getattr(pt, "SAVE_DIR", DOWNLOAD_DIR))
    except Exception:
        pass
    d = os.path.join(base, "bt_days")
    if not os.path.isdir(d):
        try:
            os.makedirs(d)
        except Exception:
            pass
    return d


def _bt_day_path(date_str):
    return os.path.join(_bt_days_dir(), str(date_str) + ".json")


def _bt_load_all():
    """保存済み日別レコードを dict {date: rec} で返す。

    v327: bt_days/ の日別ファイルを読む。
    旧 bt_results.jsonl があれば、そこにしか無い日を補って読む
    (移行期の互換。日別ファイルが優先)。
    """
    out = {}

    # --- 1) 旧形式を先に読む(あれば) ---
    path = _bt_results_path()
    if os.path.exists(path):
        try:
            f = open(path, "r", encoding="utf-8")
            try:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    d = str(r.get("date", ""))
                    if d:
                        out[d] = r
            finally:
                f.close()
        except Exception:
            pass

    # --- 2) 日別ファイルで上書き(こちらが正) ---
    ddir = _bt_days_dir()
    if os.path.isdir(ddir):
        try:
            names = sorted(os.listdir(ddir))
        except Exception:
            names = []
        i = 0
        while i < len(names):
            nm = names[i]
            i = i + 1
            if not nm.endswith(".json") or nm.endswith(".tmp"):
                continue
            fp = os.path.join(ddir, nm)
            try:
                f = open(fp, "r", encoding="utf-8")
                try:
                    r = json.load(f)
                finally:
                    f.close()
            except Exception as e:
                print("[bt_load] ★読めない日別ファイル: " + nm
                      + " (" + str(e)[:40] + ")")
                continue
            d = str(r.get("date", ""))
            if d:
                out[d] = r
    return out


def _bt_write_day(rec):
    """★v327: 1日ぶんだけを書く。これが通常の保存経路。

    全書き直しが無いので、途中で落ちても他の日は無傷。
    書き込み自体もアトミック(tmp→rename)にしてある。
    """
    d = str(rec.get("date", ""))
    if not d:
        return False
    path = _bt_day_path(d)
    tmp = path + ".tmp"
    try:
        body = json.dumps(rec, ensure_ascii=False)
    except Exception as e:
        print("[bt_save] ★JSON化に失敗 " + d + ": " + str(e)[:50])
        return False
    try:
        f = open(tmp, "w", encoding="utf-8")
        try:
            f.write(body)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        finally:
            f.close()
    except Exception as e:
        print("[bt_save] ★一時ファイル書込失敗 " + d + ": " + str(e)[:50])
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False
    # 検証: 書いたサイズが本文長と合うか
    try:
        tsz = os.path.getsize(tmp)
    except Exception:
        tsz = -1
    if tsz <= 0:
        print("[bt_save] ★書込結果が空 " + d + "。既存を残します。")
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False
    try:
        os.replace(tmp, path)
    except Exception as e:
        print("[bt_save] ★差し替え失敗 " + d + ": " + str(e)[:50])
        return False
    return True


def _bt_write_all(recs_dict):
    """全日を日別ファイルへ書き出す。

    v327: 旧形式(1本のjsonl)への全書き直しは廃止した。
    通常の保存は _bt_write_day が1日ぶんだけ書く。
    この関数は移行や一括修復のときだけ使う。
    """
    keys = sorted(recs_dict.keys())
    ok = 0
    ng = 0
    i = 0
    while i < len(keys):
        if _bt_write_day(recs_dict[keys[i]]):
            ok = ok + 1
        else:
            ng = ng + 1
        i = i + 1
    print("[bt_save] 日別保存 成功" + str(ok) + " 失敗" + str(ng))

def _bt_agg_betN_sum(agg):
    """集計器dict(agg)の betN 合計を返す。betN構造が無ければ None。"""
    if not isinstance(agg, dict):
        return None
    betN = agg.get("betN", None)
    if not isinstance(betN, list):
        return None
    s = 0.0
    i = 0
    while i < len(betN):
        try:
            s = s + float(betN[i])
        except Exception:
            pass
        i = i + 1
    return s


def _bt_raw_betN_scan(raw):
    """保存raw(2階層)を走査し (has_data, any_betN) を返す。
    raw = {"omk":{"0":agg,...}, "ora":{"honmei":agg,...}} が正規形。
    念のため1階層({"omk":agg})の旧形式も拾えるようにしている。
      has_data : betN合計が1つでも >0 (=中身のある集計)
      any_betN : betN構造を1つでも見つけた (=新形式で保存されている)
    """
    has_data = False
    any_betN = False
    if not isinstance(raw, dict):
        return (has_data, any_betN)
    for k1 in raw:
        v1 = raw[k1]
        s1 = _bt_agg_betN_sum(v1)
        if s1 is not None:
            any_betN = True
            if s1 > 0:
                has_data = True
            continue
        if not isinstance(v1, dict):
            continue
        for k2 in v1:
            s2 = _bt_agg_betN_sum(v1[k2])
            if s2 is None:
                continue
            any_betN = True
            if s2 > 0:
                has_data = True
    return (has_data, any_betN)


def _bt_raw_content_scan(raw):
    """保存raw を走査し (races合計, betN合計, 見つけた集計器の数) を返す。
    raw = {"omk":{"0":agg,...}, "ora":{"honmei":agg,...}} が正規形。
    1階層({"omk":agg})の旧形式も拾う。"""
    n_races = 0
    n_bet = 0.0
    n_agg = 0
    if not isinstance(raw, dict):
        return (n_races, n_bet, n_agg)
    stack = []
    for k1 in raw:
        stack.append(raw[k1])
    depth = 0
    while stack and depth < 3:
        nxt = []
        i = 0
        while i < len(stack):
            v = stack[i]
            i = i + 1
            if not isinstance(v, dict):
                continue
            s = _bt_agg_betN_sum(v)
            has_races = "races" in v
            if s is not None or has_races:
                n_agg = n_agg + 1
                if s is not None:
                    n_bet = n_bet + s
                try:
                    n_races = n_races + int(v.get("races", 0) or 0)
                except Exception:
                    pass
                continue
            for k2 in v:
                nxt.append(v[k2])
        stack = nxt
        depth = depth + 1
    return (n_races, n_bet, n_agg)


@app.route("/api/bt_exists")
def api_bt_exists():
    """指定日(date)が既に集計保存済みかを返す。
    ただし『0レースで保存された日』(raw集計の母数が全て0)は未集計とみなす。
    DB更新前に空で焼き付いた0R集計を、次回自動で再計算させるため。"""
    d = request.args.get("date", "").strip()
    allr = _bt_load_all()
    rec = allr.get(d)
    if rec is None:
        print("[bt_exists] " + str(d) + " 保存レコード無し -> exists=False")
        return jsonify({"ok": True, "exists": False})
    raw = rec.get("raw", {})
    if not raw:
        print("[bt_exists] " + str(d) + " raw空 -> exists=False")
        return jsonify({"ok": True, "exists": False})
    # ★v298: 判定を単純化した。
    #   「中身がある」= どこかの集計器で races>0 または betN合計>0。
    #   v296/v297 は betN構造の有無(any_betN)で旧形式を保存済み扱いしていたが、
    #   中身が空のレコードを温存してしまう余地があった。
    #   0件のレコードは残しておく価値が無いので、常に未集計扱いにして
    #   再集計させる。これでチェックボックス操作なしに自動復旧する。
    n_races, n_bet, n_agg = _bt_raw_content_scan(raw)
    exists = (n_races > 0) or (n_bet > 0)
    # ★v313: 期待値(evw)だけ欠けている日を自動で拾う。
    #   託宣/御告は入っているがEV集計が無い日は「未集計」扱いにして
    #   再集計させる。上書きチェックを押さなくても空欄が埋まる。
    #   ただしEV辞書がそもそも無い日は永久に埋まらないので対象外にする
    #   (毎回再集計してしまうため)。
    if exists and _odds_for_date(d):
        _vv = raw.get("vfa")
        if not _vv:
            _vv = raw.get("evw", {})
        ev_races, ev_bet, ev_agg = _bt_raw_content_scan(_vv)
        if ev_races <= 0 and ev_bet <= 0:
            print("[bt_exists] " + str(d)
                  + " 期待値が未集計 -> 再集計対象にする")
            exists = False
    print("[bt_exists] " + str(d) + " 集計器=" + str(n_agg)
          + " races計=" + str(n_races) + " betN計=" + str(int(n_bet))
          + " -> exists=" + str(exists))
    if not exists:
        print("[bt_exists] " + str(d) + " 中身が空。再集計させる。")
    return jsonify({"ok": True, "exists": exists})


@app.route("/api/bt_save", methods=["POST"])
def api_bt_save():
    """1日分の生集計を保存。bodyは {date, raw} のJSON。
    raw = {omk:{"0":agg,...}, ora:{"honmei":agg,...}}。同じ date は上書き。"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"ok": False, "error": "JSON不正"})
    if not data:
        return jsonify({"ok": False, "error": "body無し"})
    d = str(data.get("date", ""))
    if not d:
        return jsonify({"ok": False, "error": "date必須"})
    allr = _bt_load_all()
    _raw_in = data.get("raw", {})
    # ★v320: 部分再集計。scope で指定された系列だけ差し替え、
    #   他は既存の保存値を残す。託宣だけ/検証だけ測り直したいときに
    #   全系列を計算し直さずに済む。scope 未指定なら従来どおり全置換。
    _scope = data.get("scope") or ""
    if _scope and isinstance(_raw_in, dict):
        _prev = (allr.get(d, {}) or {}).get("raw", {}) or {}
        _merged = {}
        for _k in _prev:
            _merged[_k] = _prev[_k]
        for _k in _raw_in:
            _merged[_k] = _raw_in[_k]
        _raw_in = _merged
        print("[bt_save] " + d + " 部分保存 scope=" + str(_scope))
    # ★v300: ブラウザが実際に何を送ってきたかを記録する。
    #   races>0 かつ betN=0 なら「集計器は回ったが買い目(combos)が空」。
    #   races=0 なら「JS側で全レースが捨てられている」。
    #   どちらでもなければ集計は成功しており、表示側が犯人。
    _pr = []
    if isinstance(_raw_in, dict):
        for _k1 in _raw_in:
            _v1 = _raw_in[_k1]
            if not isinstance(_v1, dict):
                continue
            for _k2 in _v1:
                _a = _v1[_k2]
                if not isinstance(_a, dict):
                    continue
                _bn = _a.get("betN")
                _bs = 0.0
                if isinstance(_bn, list):
                    _q = 0
                    while _q < len(_bn):
                        try:
                            _bs = _bs + float(_bn[_q])
                        except Exception:
                            pass
                        _q = _q + 1
                _dt = _a.get("detail")
                _pr.append(str(_k1) + "." + str(_k2)
                           + " races=" + str(_a.get("races", "?"))
                           + " betN計=" + str(int(_bs))
                           + " detail=" + str(len(_dt) if isinstance(_dt, list) else "?"))
    _diag = data.get("diag") or {}
    if isinstance(_diag, dict) and _diag:
        print("[bt_save] " + d + " 託宣(omk)の失敗理由:")
        for _dk in _diag:
            print("          " + str(_dk) + " = " + str(_diag[_dk]) + "件")
    print("[bt_save] " + d + " 受信:")
    _q = 0
    while _q < len(_pr):
        print("          " + _pr[_q])
        _q = _q + 1
    if not _pr:
        print("          (集計器が1つも入っていない)")
    _rec = {
        "date": d,
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "raw": _raw_in,
    }
    allr[d] = _rec
    # ★v327: この日ぶんだけ書く。全書き直しはしない。
    try:
        if not _bt_write_day(_rec):
            return jsonify({"ok": False, "error": "保存失敗(日別)"})
    except Exception as e:
        return jsonify({"ok": False, "error": "保存失敗:" + str(e)[:60]})
    return jsonify({"ok": True})


@app.route("/api/bt_list")
def api_bt_list():
    """保存済みの日付一覧を、年ごとにまとめて返す。"""
    allr = _bt_load_all()
    years = {}
    for d in allr:
        rec = allr[d]
        raw = rec.get("raw", {}) or {}
        omk0 = ((raw.get("omk", {}) or {}).get("0", {}) or {})
        races = omk0.get("races", 0)
        y = d[:4]
        if y not in years:
            years[y] = []
        years[y].append({"date": d, "races": races})
    out = []
    for y in sorted(years.keys(), reverse=True):
        days = years[y]
        days.sort(key=lambda x: x["date"])
        out.append({"year": y, "days": days})
    return jsonify({"ok": True, "years": out})


def _bt_merge_aggs(agg_list):
    """複数の生aggを合算して1つの生aggにする。"""
    MAXN = 20
    out = {"races": 0, "hitN": [0] * MAXN, "retN": [0] * MAXN,
           "betN": [0] * MAXN, "detail": []}
    for a in agg_list:
        if not a:
            continue
        out["races"] = out["races"] + a.get("races", 0)
        hn = a.get("hitN", []) or []
        rn = a.get("retN", []) or []
        bn = a.get("betN", []) or []
        i = 0
        while i < MAXN:
            if i < len(hn):
                out["hitN"][i] = out["hitN"][i] + hn[i]
            if i < len(rn):
                out["retN"][i] = out["retN"][i] + rn[i]
            if i < len(bn):
                out["betN"][i] = out["betN"][i] + bn[i]
            i = i + 1
        det = a.get("detail", []) or []
        out["detail"].extend(det)
    return out


@app.route("/api/bt_get")
def api_bt_get():
    """単日(date)または期間(from,to)の生集計を返す。
    期間指定時は各日の同一パターン同士を合算する。
    返り値: {ok, raw:{omk:{...}, ora:{...}}}"""
    d = request.args.get("date", "").strip()
    from_s = request.args.get("from", "").strip()
    to_s = request.args.get("to", "").strip()
    allr = _bt_load_all()
    target_dates = []
    if d:
        target_dates = [d]
    elif from_s and to_s:
        for ds in _date_range_list(from_s, to_s):
            if ds in allr:
                target_dates.append(ds)
    if not target_dates:
        return jsonify({"ok": False, "error": "該当なし"})
    omk_keys = ["0", "1", "2", "3"]
    ora_keys = ["honmei", "taikou", "tanana", "renka"]
    raw_out = {"omk": {}, "ora": {}, "vfa": {}, "vfb": {}, "vfc": {}}
    for k in omk_keys:
        aggs = []
        for ds in target_dates:
            raw = (allr[ds].get("raw", {}) or {})
            aggs.append((raw.get("omk", {}) or {}).get(k))
        raw_out["omk"][k] = _bt_merge_aggs(aggs)
    for k in ora_keys:
        aggs = []
        for ds in target_dates:
            raw = (allr[ds].get("raw", {}) or {})
            aggs.append((raw.get("ora", {}) or {}).get(k))
        raw_out["ora"][k] = _bt_merge_aggs(aggs)
    # v320: 検証A / 検証B。omkと同じ軸キー。
    #   raw.evw は旧名。既存の保存データを読めるよう vfa へ寄せる。
    for k in omk_keys:
        aggs = []
        for ds in target_dates:
            raw = (allr[ds].get("raw", {}) or {})
            _va = raw.get("vfa")
            if not _va:
                _va = raw.get("evw", {}) or {}
            aggs.append(_va.get(k))
        raw_out["vfa"][k] = _bt_merge_aggs(aggs)
    for k in omk_keys:
        aggs = []
        for ds in target_dates:
            raw = (allr[ds].get("raw", {}) or {})
            aggs.append((raw.get("vfb", {}) or {}).get(k))
        raw_out["vfb"][k] = _bt_merge_aggs(aggs)
    for k in omk_keys:
        aggs = []
        for ds in target_dates:
            raw = (allr[ds].get("raw", {}) or {})
            aggs.append((raw.get("vfc", {}) or {}).get(k))
        raw_out["vfc"][k] = _bt_merge_aggs(aggs)
    return jsonify({"ok": True, "raw": raw_out, "dates": target_dates})


@app.route("/api/bt_day")
def api_bt_day():
    """集計高速化用: 指定日(date)の全レースの payload + result を 1レスポンスで返す。
    レース単位の payload 構築と結果取得を並列実行する。
    scrape=1 のとき、未キャッシュ日は出走表をスクレイプ、結果も払戻補完を許可。
    返り値: {ok, date, races:[{key, payload, result}], n}
    payload は /api/race と同一、result は /api/result と同一構造。"""
    date_str = request.args.get("date", "").strip()
    allow = request.args.get("scrape", "").strip() in ("1", "true", "yes")
    if not date_str:
        return jsonify({"ok": False, "error": "date が必要"}), 400

    races, rmap = load_races(date_str)
    # ★v297: 0R切り分け用ログ。取得元の内訳を必ず出す。
    _n_cache = 0
    _n_db = 0
    try:
        _c = pt.load_cache(date_str)
        _n_cache = len(_c) if _c else 0
    except Exception:
        _n_cache = -1
    if RESULTS is not None:
        try:
            _n_db = len(RESULTS.get_races_for_date(date_str))
        except Exception:
            _n_db = -1
    print("[bt_day] " + str(date_str) + " load_races=" + str(len(races))
          + " (today_cache=" + str(_n_cache) + " DB復元=" + str(_n_db) + ")")
    if (not races) and allow:
        try:
            built, _n, _r = _build_cache_for_date(date_str)
        except Exception:
            built = False
        if built:
            races, rmap = load_races(date_str)
            print("[bt_day] " + str(date_str) + " スクレイプ後 load_races="
                  + str(len(races)))
    if not races:
        print("[bt_day] " + str(date_str)
              + " >>> レース0件。load_races/_db_index経路の問題。")
        return jsonify({"ok": True, "date": date_str, "races": [], "n": 0})

    # walk-forward: この日に対応する「前月末cutoffの辞書」へ切り替える。
    # dicts_monthly/<cutoff>/ があれば look-ahead なしの正しい予想になる。
    # 無ければ通常辞書のまま(従来動作)。集計後は通常辞書へ戻す。
    _wf_switched = _wf_switch_dicts(date_str)

    d = get_dicts()
    vhd = d["venue_home_dir"]
    bank = d["bank_data"]

    def _build_one(race):
        rkey = race_key(race)
        try:
            payload = build_race_payload(race, vhd, bank, fast=True)
        except Exception as e:
            payload = {"status": "skip", "reason": "build_error",
                       "detail": str(e)[:80], "header": {}}
        return {"key": rkey, "race": race, "payload": payload}

    # payload 構築 (計算主体) を並列化
    built_list = []
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            built_list = list(ex.map(_build_one, races))
    except Exception:
        built_list = [_build_one(r) for r in races]

    # 結果は 1日分まとめて取得 (result_<date>.json を 1回読み + 1回書き)
    results_by_rid = {}
    rid_by_index = []
    if RESULTS is not None:
        req_list = []
        j = 0
        while j < len(built_list):
            race = built_list[j]["race"]
            j = j + 1
            venue = race.get("place", "")
            race_no = race.get("race_no", "")
            rid = RESULTS.race_id_for(venue, date_str, race_no)
            rid_by_index.append(rid)
            if rid is not None:
                req_list.append({"venue": venue, "race_no": race_no, "rid": rid})
        try:
            results_by_rid = RESULTS.get_results_for_day(req_list, date_str,
                                                         allow_scrape=allow)
        except Exception:
            results_by_rid = {}
    else:
        k = 0
        while k < len(built_list):
            rid_by_index.append(None)
            k = k + 1

    out = []
    m = 0
    while m < len(built_list):
        b = built_list[m]
        rid = rid_by_index[m] if m < len(rid_by_index) else None
        m = m + 1
        rr = results_by_rid.get(rid) if rid is not None else None
        if rr is None:
            result = {"ok": False, "trifecta": "", "refund_3t": 0, "source": ""}
        else:
            result = {
                "ok": bool(rr.get("has_result")),
                "trifecta": rr.get("trifecta", ""),
                "refund_3t": rr.get("refund_3t", 0),
                "source": rr.get("source", ""),
            }
        # 結果が取れていない場合、DB本体レコードに result があれば
        # それを使う(過去日の月別walk-forward集計でスクレイピング不要)。
        if not result.get("ok"):
            db_res = _result_from_db_record(b.get("race"))
            if db_res is None:
                # ★v305: load_races が today_cache を優先した日は、
                #   race が出走表キャッシュのオブジェクトで result を持たない。
                #   DB本体に同じレースがあれば、そこから結果を引き直す。
                #   (7/29がDB有りなのに結果0件、7/30がDB無しでスクレイプ成功、
                #    という日ごとの割れの原因はこれ。)
                db_rec = _db_record_for(date_str, b.get("key"), b.get("race"))
                if db_rec is not None:
                    db_res = _result_from_db_record(db_rec)
            if db_res is not None:
                result = db_res
        out.append({"key": b["key"], "payload": b["payload"], "result": result})

    # ★v315: 期待値タブ用。確定オッズ(odds_months)をレースごとに添付する。
    #   EV = モデル確率 x オッズ をブラウザ側で算出する。
    #   辞書を作り替える方式(v312-v314)は、託宣の買い目が
    #   rawscore_pattern_stats でなく kimari_link から作られるため
    #   効かなかった。オッズを直接使う方式に変更した。
    _odds_day = _odds_for_date(date_str)
    _od_hit = 0
    if _odds_day:
        _z = 0
        while _z < len(out):
            _item = out[_z]
            _z = _z + 1
            _om = _odds_day.get(_item["key"])
            if _om:
                _item["odds"] = _om
                _od_hit = _od_hit + 1
    print("[bt_day] " + str(date_str) + " オッズ添付: " + str(_od_hit)
          + "/" + str(len(out)) + "R")

    # ★v316修正: v315でブロック差し替えの際、この集計ループごと消してしまい
    #   NameError: _reasons で bt_day が500になっていた。復元する。
    _ok_p = 0
    _ok_r = 0
    _both = 0
    _reasons = {}
    _z = 0
    while _z < len(out):
        _pl = out[_z].get("payload") or {}
        _rs0 = out[_z].get("result") or {}
        _z = _z + 1
        _st = _pl.get("status", "?")
        if _st == "ok":
            _ok_p = _ok_p + 1
        else:
            _rk = str(_pl.get("reason", "?"))
            _dt = str(_pl.get("detail", ""))
            if _dt:
                _rk = _rk + "(" + _dt[:24] + ")"
            _reasons[_rk] = _reasons.get(_rk, 0) + 1
        _hasr = bool(_rs0.get("ok")) and bool(_rs0.get("trifecta"))
        if _hasr:
            _ok_r = _ok_r + 1
        if _st == "ok" and _hasr:
            _both = _both + 1

    _rtxt = ""
    for _k in _reasons:
        _rtxt = _rtxt + " " + _k + "=" + str(_reasons[_k])
    _src = {}
    _z = 0
    while _z < len(out):
        _rs = out[_z].get("result") or {}
        _z = _z + 1
        if _rs.get("ok"):
            _sk = str(_rs.get("source", "?")) or "?"
            _src[_sk] = _src.get(_sk, 0) + 1
    _stxt = ""
    for _k in _src:
        _stxt = _stxt + " " + _k + "=" + str(_src[_k])
    if _stxt:
        print("[bt_day] " + str(date_str) + " 結果の取得元:" + _stxt)
    print("[bt_day] " + str(date_str) + " 総数=" + str(len(out))
          + " payload_ok=" + str(_ok_p) + " 結果有=" + str(_ok_r)
          + " 両方通過(R)=" + str(_both))
    if _rtxt:
        print("[bt_day] " + str(date_str) + " skip理由:" + _rtxt)
    # ★v299: JS側 __oraOmakasePredict は payload.kimari.exists が真でないと
    #   即エラーになり、集計器が一度も回らない(=画面0R)。
    #   そこで kimari ブロックの状態をサーバ側で数えておく。
    _km_none = 0
    _km_false = 0
    _km_true = 0
    _km_samples = []
    _z = 0
    while _z < len(out):
        _pl = out[_z].get("payload") or {}
        _z = _z + 1
        if _pl.get("status") != "ok":
            continue
        _km = _pl.get("kimari")
        if _km is None:
            _km_none = _km_none + 1
            continue
        if not _km.get("exists"):
            _km_false = _km_false + 1
            if len(_km_samples) < 3:
                _km_samples.append(str(_km.get("cell_key", "?")))
            continue
        _km_true = _km_true + 1
    # ★v302: 選手別の決まり手(players[].kimari)が付いているかを数える。
    #   __oraAxisKimRank は選手の決まり手率が0.08未満のシナリオを全部捨てるため、
    #   ここが欠けると託宣は「買い目を構成できません」で全滅する。
    _pk_have = 0
    _pk_none = 0
    _pk_races_all_none = 0
    _pk_sample = []
    _z = 0
    while _z < len(out):
        _pl = out[_z].get("payload") or {}
        _z = _z + 1
        if _pl.get("status") != "ok":
            continue
        _hdr = _pl.get("header") or {}
        _pls = _hdr.get("players") or []
        _have_here = 0
        _y = 0
        while _y < len(_pls):
            _one = _pls[_y] or {}
            _y = _y + 1
            if _one.get("kimari"):
                _pk_have = _pk_have + 1
                _have_here = _have_here + 1
            else:
                _pk_none = _pk_none + 1
                if len(_pk_sample) < 3:
                    _pk_sample.append(str(_one.get("bike", "?")) + ":" +
                                      str(_one.get("full_info", ""))[:28])
        if _pls and _have_here == 0:
            _pk_races_all_none = _pk_races_all_none + 1
    print("[bt_day] " + str(date_str) + " 選手別決まり手: 有=" + str(_pk_have)
          + " 無=" + str(_pk_none)
          + " / 全員無しのレース=" + str(_pk_races_all_none))
    if _pk_sample:
        print("[bt_day] " + str(date_str) + " 決まり手が無い選手例: "
              + " | ".join(_pk_sample))
    # ★v309: サーバがpayloadに入れた2着ラベルを実際に出す。
    #   辞書(kimari_stats_FINAL.json)は健全(425セル中424が正常, 不明ゼロ)と
    #   probe_kimari_dict_v1 で確認済み。にもかかわらずJS側は "不明" を受け取る。
    #   どちらが嘘をついているかをここで確定させる。
    _z = 0
    _dumped = 0
    while _z < len(out) and _dumped < 2:
        _pl = out[_z].get("payload") or {}
        _z = _z + 1
        if _pl.get("status") != "ok":
            continue
        _km = _pl.get("kimari") or {}
        if not _km.get("exists"):
            continue
        _dumped = _dumped + 1
        print("[bt_day] " + str(date_str) + " payload.kimari cell_key="
              + str(_km.get("cell_key", "?"))
              + " cell_n=" + str(_km.get("cell_n", "?")))
        # DB由来の選手別決まり手も一緒に出す。
        #   ユーザー仮説「DB側が不明なのでは」の検証。
        #   ここが不明だらけなら、辞書でなく元データ側の問題になる。
        _hdr = _pl.get("header") or {}
        _pls = _hdr.get("players") or []
        _pk = []
        _w = 0
        while _w < len(_pls) and _w < 7:
            _one = _pls[_w] or {}
            _w = _w + 1
            _kk = _one.get("kimari")
            if isinstance(_kk, dict):
                _top = _kk.get("top") or _kk.get("label") or ""
                _desc = "dict(" + ",".join(sorted([str(x) for x in _kk])[:4]) + ")"
                if _top:
                    _desc = str(_top)
            elif _kk is None:
                _desc = "None"
            else:
                _desc = str(_kk)
            _pk.append(str(_one.get("bike", "?")) + ":" + _desc[:24])
        print("          選手別決まり手(DB由来): " + " | ".join(_pk))
        _kl = _km.get("kimari_link") or []
        if not _kl:
            print("          kimari_link が空")
        _y = 0
        while _y < len(_kl):
            _e = _kl[_y] or {}
            _y = _y + 1
            _its = _e.get("items") or []
            _labs = []
            _w = 0
            while _w < len(_its) and _w < 5:
                _labs.append(str((_its[_w] or {}).get("label", "(labelキー無)")))
                _w = _w + 1
            print("          kimari=" + str(_e.get("kimari", "?"))
                  + " n=" + str(_e.get("n", "?"))
                  + " items=" + str(len(_its))
                  + " ラベル: " + " / ".join(_labs))
    print("[bt_day] " + str(date_str) + " kimari: None=" + str(_km_none)
          + " exists=False:" + str(_km_false)
          + " exists=True:" + str(_km_true))
    if _km_samples:
        print("[bt_day] " + str(date_str) + " 引けなかったcell_key例: "
              + " / ".join(_km_samples))
    if _km_none > 0:
        print("[bt_day] " + str(date_str)
              + " >>> kimari統計そのものが読めていない(load_kimari_stats=None)。")
    if _km_true == 0 and _ok_p > 0:
        print("[bt_day] " + str(date_str)
              + " >>> 全レースで kimari.exists が偽。JS側で全件捨てられる(=0R)。")
    if _both == 0:
        print("[bt_day] " + str(date_str)
              + " >>> Rが0。上のskip理由/結果有を見ること。")
    if _wf_switched:
        _wf_restore_dicts()
    return jsonify({"ok": True, "date": date_str, "races": out, "n": len(out)})


_DBREC_CACHE = {}


def _db_record_for(date_str, rkey, race):
    """DB本体から date_str の同一レース(会場_R番号)のレコードを引く。
    today_cache のレースには result が無いため、結果だけDBから補う用途。
    見つからなければ None。日付単位でキャッシュする。"""
    if RESULTS is None:
        return None
    idx = _DBREC_CACHE.get(date_str)
    if idx is None:
        idx = {}
        try:
            recs = RESULTS.get_races_for_date(date_str)
        except Exception:
            recs = []
        for rec in (recs or []):
            if not isinstance(rec, dict):
                continue
            k = str(rec.get("place", "")) + "_" + str(rec.get("race_no", ""))
            idx[k] = rec
            rid = str(rec.get("race_id", ""))
            if len(rid) >= 2:
                idx["#" + rid[-2:] + "_" + str(rec.get("place", ""))] = rec
        _DBREC_CACHE[date_str] = idx
    if not idx:
        return None
    if rkey and rkey in idx:
        return idx[rkey]
    if isinstance(race, dict):
        k2 = str(race.get("place", "")) + "_" + str(race.get("race_no", ""))
        if k2 in idx:
            return idx[k2]
        rno = race.get("race_no")
        if rno is not None:
            try:
                k3 = "#" + ("0" + str(int(rno)))[-2:] + "_" + str(race.get("place", ""))
            except Exception:
                k3 = ""
            if k3 and k3 in idx:
                return idx[k3]
    return None


def _result_from_db_record(race):
    """DB本体レコードの result から bt_day 用の結果dictを作る。
    過去日の集計でスクレイピング不要にする。結果が無ければNone。"""
    if not isinstance(race, dict):
        return None
    result_list = race.get("result")
    if not isinstance(result_list, list) or len(result_list) < 3:
        return None
    top = {}
    for r in result_list:
        rk = r.get("rank")
        if rk in (1, 2, 3):
            top[rk] = str(r.get("bike"))
    if 1 not in top or 2 not in top or 3 not in top:
        return None
    trifecta = top[1] + "-" + top[2] + "-" + top[3]
    # 払戻 (refund_3t は "4-3-5(990円)" 形式 or 数値)
    refund = 0
    rf = race.get("refund_3t", "")
    if isinstance(rf, (int, float)):
        refund = int(rf)
    elif isinstance(rf, str) and rf:
        mm = re.search(r'\(([\d,]+)円\)', rf)
        if mm:
            try:
                refund = int(mm.group(1).replace(",", ""))
            except Exception:
                refund = 0
    return {"ok": True, "trifecta": trifecta,
            "refund_3t": refund, "source": "db"}


# ============================================================
# API: レース1件の計算 (タップ時に呼ぶ・遅延評価)
# ============================================================
@app.route("/api/race")
def api_race():
    date_str = request.args.get("date", "").strip()
    rkey = request.args.get("key", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    if not rkey:
        return jsonify({"error": "key が必要"}), 400

    _races, rmap = load_races(date_str)
    race = rmap.get(rkey)
    if race is None:
        return jsonify({"error": "レースが見つかりません: " + rkey}), 404

    d = get_dicts()
    payload = build_race_payload(race, d["venue_home_dir"], d["bank_data"])
    return jsonify(payload)


def _cat_parse_3t(s):
    parts = str(s).split("-")
    if len(parts) != 3:
        return None
    out = []
    for p in parts:
        v = _safe_int(p)
        if v is None:
            return None
        out.append(v)
    return tuple(out)


def _cat_filter_candidates(all_list, mode, axis, rival, rival_mode, include_bikes):
    """事前計算の全候補(all: [{"3t","weight"}])をモード別に絞り込む。
    返り値: 絞り込み後の [{"bikes":(a,b,c), "weight":float}] (weight降順)。"""
    cands = []
    for item in all_list:
        bikes = _cat_parse_3t(item.get("3t", ""))
        if bikes is None:
            continue
        w = item.get("weight", 0.0)
        try:
            w = float(w)
        except Exception:
            w = 0.0
        cands.append({"bikes": bikes, "weight": w})

    if mode == "priest":
        if axis is None:
            return []
        out = []
        for c in cands:
            b = c["bikes"]
            if rival is None:
                if b[0] == axis:
                    out.append(c)
            else:
                if rival_mode == "fixed":
                    if b[0] == axis and b[1] == rival:
                        out.append(c)
                else:
                    if set(b[:2]) == set([axis, rival]):
                        out.append(c)
        return out
    if mode == "bishop":
        if not include_bikes:
            return []
        inc = set(include_bikes)
        out = []
        for c in cands:
            if inc.issubset(set(c["bikes"])):
                out.append(c)
        return out
    # maria
    return cands


def _cat_finalize(cands, top_n):
    """絞り込み後候補を weight降順で top_n に切り、norm_pct を再計算して返す。"""
    cands = sorted(cands, key=lambda c: -c["weight"])
    total_w = sum(c["weight"] for c in cands)
    top = cands[:top_n]
    out = []
    for c in top:
        w = c["weight"]
        norm_pct = round(100.0 * w / total_w, 3) if total_w > 0 else 0
        out.append({"3t": "%d-%d-%d" % c["bikes"],
                    "norm_pct": norm_pct, "weight": round(w, 6)})
    return out


def _parse_refund3(r3):
    """ '5-7-1(15,560円)' -> ('5-7-1', 15560)。取れなければ ('', 0)。"""
    if not isinstance(r3, str) or not r3:
        return ("", 0)
    m = re.match(r'^([\d\-]+)\(([\d,]+)円\)', r3)
    if not m:
        return ("", 0)
    try:
        return (m.group(1), int(m.group(2).replace(",", "")))
    except Exception:
        return (m.group(1), 0)


@app.route("/api/cathedral")
def api_cathedral():
    """大聖堂タブ 新予測エンジン。
    クエリ:
      date, key       : レース特定 (/api/race と同じ)
      mode            : maria | priest | bishop  (既定 maria)
      top_n           : 1..20  (既定 20)
      axis            : 牧師モードの軸 車番 (int)
      rival           : 牧師モードの対抗 車番 (int)
      rival_mode      : fixed | renta  (既定 fixed)
      bikes           : 大司教モードの指定車番 カンマ区切り "1" or "1,4"

    事前計算キャッシュ(cathedral_cache)があればそれを使い、
    モード絞り込みと norm_pct 再計算はサーバ側で行う(計算ゼロで軽い)。
    無ければ predict_cathedral でその場計算にフォールバック。
    """
    date_str = request.args.get("date", "").strip()
    rkey = request.args.get("key", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    if not rkey:
        return jsonify({"ok": False, "reason": "key が必要"}), 400

    mode = request.args.get("mode", "maria").strip() or "maria"
    if mode not in ("maria", "priest", "bishop"):
        mode = "maria"

    top_n = _safe_int(request.args.get("top_n", "20"))
    if top_n is None:
        top_n = 20

    axis = _safe_int(request.args.get("axis", ""))
    rival = _safe_int(request.args.get("rival", ""))
    rival_mode = request.args.get("rival_mode", "fixed").strip() or "fixed"
    if rival_mode not in ("fixed", "renta"):
        rival_mode = "fixed"

    include_bikes = None
    bikes_raw = request.args.get("bikes", "").strip()
    if bikes_raw:
        tmp = []
        for tok in bikes_raw.split(","):
            bi = _safe_int(tok.strip())
            if bi is not None:
                tmp.append(bi)
        if tmp:
            include_bikes = tmp

    # mode別の入力検証 (キャッシュ経路でも適用)
    if mode == "priest":
        if axis is None and rival is None:
            return jsonify({"ok": False, "reason": "priest_no_axis",
                            "candidates": []})
        if axis is None and rival is not None:
            return jsonify({"ok": False, "reason": "priest_rival_only",
                            "candidates": []})
    elif mode == "bishop":
        if not include_bikes:
            return jsonify({"ok": False, "reason": "bishop_no_bike",
                            "candidates": []})
        if len(include_bikes) > 2:
            return jsonify({"ok": False, "reason": "bishop_too_many",
                            "candidates": []})

    if top_n < 1:
        top_n = 1
    if top_n > 20:
        top_n = 20

    # === 事前計算キャッシュ優先 ===
    cache = _get_cathedral_cache(date_str)
    if cache is not None and rkey in cache:
        entry = cache[rkey]
        if not entry.get("ok"):
            return jsonify({"ok": False, "reason": entry.get("reason", "cache_ng"),
                            "candidates": [],
                            "metadata": {"source": "cache"},
                            "race": {"place": entry.get("place", ""),
                                     "race_no": entry.get("race_no", ""),
                                     "line": entry.get("line", ""),
                                     "weather": entry.get("weather", ""),
                                     "result_3t": entry.get("result_3t", ""),
                                     "refund_3t": entry.get("refund_3t", 0)}})
        all_list = entry.get("all", [])
        filtered = _cat_filter_candidates(all_list, mode, axis, rival,
                                          rival_mode, include_bikes)
        if not filtered:
            return jsonify({"ok": True, "reason": "no_candidate_after_filter",
                            "candidates": [],
                            "metadata": {"source": "cache"},
                            "race": {"place": entry.get("place", ""),
                                     "race_no": entry.get("race_no", ""),
                                     "line": entry.get("line", ""),
                                     "weather": entry.get("weather", ""),
                                     "result_3t": entry.get("result_3t", ""),
                                     "refund_3t": entry.get("refund_3t", 0)}})
        out = _cat_finalize(filtered, top_n)
        # キャッシュはレース前生成のため result_3t が空。race の払戻から復元する。
        _cr3 = entry.get("result_3t", "")
        _crf = entry.get("refund_3t", 0)
        try:
            _r2, _rm2 = load_races(date_str)
            _rc = _rm2.get(rkey)
            if _rc is not None:
                _pr3, _prf = _parse_refund3(_rc.get("refund_3t", ""))
                if _pr3:
                    _cr3 = _pr3
                    _crf = _prf
        except Exception:
            pass
        return jsonify({"ok": True, "reason": "ok", "candidates": out,
                        "metadata": {"source": "cache", "mode": mode,
                                     "top_n": top_n,
                                     "n_after_filter": len(filtered)},
                        "race": {"place": entry.get("place", ""),
                                 "race_no": entry.get("race_no", ""),
                                 "line": entry.get("line", ""),
                                 "weather": entry.get("weather", ""),
                                 "result_3t": _cr3,
                                 "refund_3t": _crf}})

    # エンジン未配置チェック (その場計算フォールバック)
    ci = _init_cathedral_once()
    if pc is None or not ci.get("ok"):
        reason = "engine_unavailable"
        detail = ci.get("info") if ci else None
        return jsonify({"ok": False, "reason": reason,
                        "detail": detail, "candidates": []})

    _races, rmap = load_races(date_str)
    race = rmap.get(rkey)
    if race is None:
        return jsonify({"ok": False,
                        "reason": "レースが見つかりません: " + rkey,
                        "candidates": []}), 404

    line_str = race.get("line", "") or ""
    venue = race.get("place", "") or ""
    weather_str = race.get("weather", "") or ""

    d = get_dicts()
    # 当日計算の raw_score を rs_map から取得 (既存ロジックと一致させる)
    result = pt.predict_for_race(race, d["venue_home_dir"], d["bank_data"])
    rs_map = {}
    if result and result.get("valid", False):
        rs_map = result.get("rs_map", {}) or {}

    players = race.get("players", {})
    players_info = {}
    if isinstance(players, dict):
        for bs in players:
            pdata = players[bs]
            if not isinstance(pdata, dict):
                continue
            bike = _safe_int(bs)
            if bike is None:
                continue
            s_val = pdata.get("s")
            players_info[bike] = {
                "s": s_val if isinstance(s_val, int) else None,
                "full_info": pdata.get("full_info", ""),
                "raw_score": rs_map.get(bike, 0.0),
            }

    res = pc.predict_cathedral(
        line_str, players_info, venue, weather_str,
        mode=mode, top_n=top_n,
        axis=axis, rival=rival, rival_mode=rival_mode,
        include_bikes=include_bikes)

    # 結果情報 (的中表示用)
    r3 = race.get("refund_3t", "")
    res_3t = ""
    refund_3t = 0
    if isinstance(r3, str) and r3:
        m = re.match(r'^([\d\-]+)\(([\d,]+)円\)', r3)
        if m:
            res_3t = m.group(1)
            try:
                refund_3t = int(m.group(2).replace(",", ""))
            except Exception:
                refund_3t = 0

    # 表示用の付帯情報を足す
    if "metadata" not in res or not isinstance(res.get("metadata"), dict):
        res["metadata"] = {}
    res["metadata"]["source"] = "live"
    res["race"] = {
        "place": venue, "race_no": race.get("race_no", ""),
        "line": line_str, "weather": weather_str,
        "result_3t": res_3t, "refund_3t": refund_3t,
    }
    return jsonify(res)


@app.route("/api/odds")
def api_odds():
    """3連単オッズを人気順(オッズ昇順)で返す。winticket から取得。"""
    date_str = request.args.get("date", "").strip()
    rkey = request.args.get("key", "").strip()
    force = request.args.get("force", "").strip() in ("1", "true", "yes")
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    if not rkey:
        return jsonify({"error": "key が必要"}), 400

    _races, rmap = load_races(date_str)
    race = rmap.get(rkey)
    if race is None:
        return jsonify({"error": "レースが見つかりません: " + rkey}), 404

    venue = race.get("place", "")
    race_no = race.get("race_no", "?")
    # gamboo(kdreams) を主取得先に。失敗時のみ winticket をフォールバック。
    r = fetch_gamboo_trifecta_odds(venue, date_str, race_no, force=force)
    if not r.get("ok"):
        rw = fetch_winticket_trifecta_odds(venue, date_str, race_no, force=force)
        if rw.get("ok"):
            r = rw
        else:
            r["diag"] = {"gamboo": r.get("diag", {}),
                         "winticket": rw.get("diag", {})}
    odds = r.get("odds", {})
    items = []
    for k, v in odds.items():
        try:
            items.append({"combo": k, "odds": float(v)})
        except Exception:
            pass
    # 人気順 = オッズ昇順(同値は車番順)
    items.sort(key=lambda x: (x["odds"], x["combo"]))
    for i in range(len(items)):
        items[i]["rank"] = i + 1
    return jsonify({
        "ok": r.get("ok", False),
        "venue": venue,
        "race_no": race_no,
        "count": len(items),
        "items": items,
        "diag": r.get("diag", {}),
    })


@app.route("/api/rsrank")
def api_rsrank():
    """score順位グラフ用: 各選手の rs_rank 着順分布 + 基準値(同rs_rank全選手平均)。
    scope = all(選手合計) / cond(今回レース同区分)。
    """
    date_str = request.args.get("date", "").strip()
    rkey = request.args.get("key", "").strip()
    scope = request.args.get("scope", "all").strip()
    if scope not in ("all", "cond"):
        scope = "all"
    if not date_str:
        date_str = datetime.now().strftime("%Y%m%d")
    if not rkey:
        return jsonify({"error": "key が必要"}), 400
    if RSRANK is None or not RSRANK.available:
        return jsonify({"available": False, "rows": []})

    _races, rmap = load_races(date_str)
    race = rmap.get(rkey)
    if race is None:
        return jsonify({"error": "レースが見つかりません: " + rkey}), 404

    d = get_dicts()
    payload = build_race_payload(race, d["venue_home_dir"], d["bank_data"])
    header = payload.get("header", {})
    players = header.get("players", [])
    venue = header.get("venue", "")
    weather = header.get("weather_raw", "")

    # 同区分の条件キー
    cond = cond_from_weather(weather, venue, d["venue_home_dir"])
    cond_label = "|".join([str(c) for c in cond])

    rows = []
    for pl in players:
        rs = pl.get("rs_rank")
        pid = pl.get("pid")
        if rs is None or not pid:
            continue
        self_dist = RSRANK.get_player_dist(pid, rs, scope, cond)
        base_dist = RSRANK.get_baseline_dist(rs, scope, cond)
        rows.append({
            "bike": pl.get("bike"),
            "name": pl.get("name"),
            "rs_rank": rs,
            "label_kind": pl.get("label_kind"),
            "label_text": pl.get("label_text"),
            "layoff_kind": pl.get("layoff_kind"),
            "layoff_text": pl.get("layoff_text"),
            "self": self_dist,     # {"pct":[7], "n":N} or None
            "baseline": base_dist,  # 同rs_rank全選手平均
        })
    # rs_rank 昇順
    rows.sort(key=lambda r: (r["rs_rank"] if r["rs_rank"] is not None else 99))
    return jsonify({
        "available": True,
        "scope": scope,
        "cond_label": cond_label,
        "venue": venue,
        "rows": rows,
    })


# ============================================================
# 計算結果を JSON 構造に変換 (display_race の print を置換)
# ============================================================
def make_rsrank_key(full_info, name):
    """rsrank辞書のキー『姓名_期』を作る。

    辞書側 : 齊藤英伊須_125   (空白なし + アンダーバー + 期)
    出走表 : 江端 隆司        (名前だけ。player_key は入っていない)
    このずれで score順位が全員「該当データなし」になっていた。

    full_info の形: 江端 隆司/福井/33歳/103期/84.04点
    """
    fi = full_info if isinstance(full_info, str) else ""
    nm = ""
    ki = ""
    if fi:
        parts = fi.split("/")
        if parts:
            nm = parts[0].strip()
        i = 0
        while i < len(parts):
            p = parts[i].strip()
            i = i + 1
            m = re.match(r'^(\d+)期$', p)
            if m:
                ki = m.group(1)
                break
    if not nm:
        nm = name if isinstance(name, str) else ""
    nm = nm.replace(" ", "").replace("\u3000", "")
    if not nm:
        return ""
    if not ki:
        return nm
    return nm + "_" + ki


def build_race_payload(race, venue_home_dir, bank_data, fast=False):
    """display_race 相当のロジックを JSON 化。
    既存 predict_for_race() をそのまま呼び、表示用の数値を組み立てる。
    """
    venue = race.get("place", "")
    race_no = race.get("race_no", "?")
    post = race.get("post_time", "--:--")
    line_str = race.get("line", "")
    weather = race.get("weather", "")
    today = race.get("date", "")

    # ★v306: fast=True のときは winticket URL を組まない。
    #   winticket_url_diag は開催情報を引くため1レースごとに通信し、
    #   過去日はキャッシュが無いので day=1..7 の総当たりが走る。
    #   集計(bt_day)は race_url を使わないので、ここが丸ごと無駄だった。
    #   72レースで約3分 → 通信ゼロで大幅に短縮される。
    if fast:
        _wt_info = {"url": "", "diag": {"step": "skipped_fast"}}
    else:
        _wt_info = winticket_url_diag(venue, today, race_no)

    header = {
        "venue": venue,
        "race_no": race_no,
        "post_time": post,
        "weather_raw": weather,
        "line_display": "",
        "rank_display": "",
        "wind_arrow": "",
        "wind_judge": "",
        "bank": "",
        "race_url": _wt_info["url"],
        "race_url_diag": _wt_info["diag"],
        # v329: 分析用。グレードと開催情報を payload に載せる。
        #   これが無いと、集計の明細から「どんなレースだったか」が追えない。
        "grade": (race.get("grade") or "").strip(),
        "race_kind": (race.get("race_kind") or "").strip(),
        # 周回中の並び。DBに入っている形のまま渡す(文字列/配列どちらもあり得る)。
        "lap": race.get("lap"),
    }

    # ライン無し / 個人戦 はスキップ扱い
    chunks = pt.parse_line_chunks(line_str)
    if chunks is None:
        return {"status": "skip", "reason": "no_line", "header": header}

    is_kojinsen = True
    i = 0
    while i < len(chunks):
        if len(chunks[i]) > 1:
            is_kojinsen = False
            break
        i = i + 1
    if is_kojinsen:
        return {"status": "skip", "reason": "kojinsen", "header": header}

    # 風向矢印・判定
    if weather and venue_home_dir:
        arrow = pt.get_wind_arrow(venue, weather, venue_home_dir)
        if arrow:
            header["wind_arrow"] = arrow
        wj = pt.judge_wind_advantage(venue, weather, venue_home_dir)
        if wj:
            header["wind_judge"] = wj

    # バンク
    if bank_data:
        bd = bank_data.get(venue, {})
        if bd:
            bl = bd.get("circumference", bd.get("bank_length", "?"))
            cant = bd.get("cant", bd.get("cant_degree", "?"))
            header["bank"] = "周長" + str(bl) + "m カント" + str(cant) + "°"

    # === 予測本体 (既存ロジックをそのまま呼ぶ) ===
    result = pt.predict_for_race(race, venue_home_dir, bank_data)
    if result is None:
        return {"status": "skip", "reason": "predict_none", "header": header}
    if not result.get("valid", False):
        # v329: 計算できなかったとき、素材がどうなっているかを一緒に返す。
        #   「出走表が無いから」と決めつけずに、実際の欠け方を見て判断するため。
        _pl = race.get("players") or {}
        _np = 0
        _nh = 0
        _miss = []
        for _bk in sorted(_pl.keys(), key=lambda x: str(x)):
            _p = _pl[_bk]
            if not isinstance(_p, dict):
                continue
            _fi = _p.get("full_info", "")
            _has_pt = bool(re.search(r'[\d.]+点$', str(_fi))) if _fi else False
            if _has_pt:
                _np = _np + 1
            else:
                _miss.append(str(_bk))
            _hh = False
            for _k in ("h1", "h2", "h3"):
                _v = _p.get(_k, "")
                if isinstance(_v, str) and _v.strip() and _v.strip() != "なし":
                    _hh = True
            if _hh:
                _nh = _nh + 1
        return {
            "status": "skip",
            "reason": "predict_invalid",
            "detail": result.get("reason", ""),
            "header": header,
            "diag": {"n_players": len(_pl), "n_points": _np, "n_hist": _nh,
                     "missing": _miss, "line": race.get("line", ""),
                     "post": race.get("post_time", "")},
        }

    rsrank_to_bike = result["rsrank_to_bike"]
    rs_map = result["rs_map"]

    # ライン表示 + score順位表示 (display_race と同じ作り)
    line_display = "-".join("".join(c) for c in chunks)
    rank_chunks = []
    i = 0
    while i < len(chunks):
        c = chunks[i]
        rstr = ""
        j = 0
        while j < len(c):
            bs = c[j]
            bi = _safe_int(bs)
            rk = result["bike_to_rsrank"].get(bi)
            rstr = rstr + (str(rk) if rk is not None else "?")
            j = j + 1
        rank_chunks.append(rstr)
        i = i + 1
    rank_display = "-".join(rank_chunks)
    header["line_display"] = line_display
    header["rank_display"] = rank_display

    # 全車の穴/勝負弱ラベル
    players_labels = []
    bikes_sorted = sorted(rsrank_to_bike.values())
    _role_map = _bike_role_from_chunks(chunks)
    bi2 = 0
    while bi2 < len(bikes_sorted):
        bike = bikes_sorted[bi2]
        bs = str(bike)
        pdata = race.get("players", {}).get(bs, {})
        info = pt.parse_full_info(pdata.get("full_info", ""))
        lab = {"kind": None, "text": "", "hit": 0, "den": 0}
        if ANA is not None:
            try:
                lab = ANA.judge(info["name"], venue)
            except Exception:
                pass
        # score順位グラフ用: rs_rank と player_key
        _rs = result["bike_to_rsrank"].get(bike)
        _pid = (pdata.get("player_key")
                or make_rsrank_key(pdata.get("full_info", ""), info.get("name"))
                or "")
        # 長期離脱明け判定 (当日基準)
        try:
            _today_dt = datetime.strptime(today, "%Y%m%d")
        except Exception:
            _today_dt = datetime.now()
        _layoff = _layoff_label(pdata, _today_dt)
        # 決まり手 (今回の役割の全期間 逃捲差マ)
        _role = _role_map.get(bike, "単騎")
        _pkey = pdata.get("player_key") or ""
        if not _pkey:
            # full_info "氏名/府県/歳/期/点" から "氏名|期" を生成
            _fi = pdata.get("full_info", "")
            _parts = _fi.split("/") if isinstance(_fi, str) else []
            if len(_parts) >= 4:
                _nm = _parts[0].strip()
                _pe = _parts[3].strip()
                if _nm and _pe:
                    _pkey = _nm + "|" + _pe
        _kimari = _kimari_for_player(_pkey, _role)
        _kimari_total = _kimari_total_for_player(_pkey)
        # 決まり手が引けなかった場合、理由を診断用に保持 (フロントで診断ログ出力)
        _kimari_diag = None
        if _kimari is None:
            _kimari_diag = _kimari_diag_reason(_pkey, _role)
        # 現在(予想レース)の競走得点を full_info 末尾 "○○点" から抽出 (推移グラフ t=1 用)
        _cur_ten = None
        _fi2 = pdata.get("full_info", "")
        if isinstance(_fi2, str):
            _p2 = _fi2.split("/")
            if _p2:
                _tail = _p2[-1].strip()
                _num = ""
                for _ch in _tail:
                    if _ch.isdigit() or _ch == ".":
                        _num = _num + _ch
                    else:
                        if _num:
                            break
                if _num:
                    try:
                        _cur_ten = float(_num)
                    except Exception:
                        _cur_ten = None
        # S(先頭通過回数) / B(バック取り回数): SHBバックフィルで players[bike] に
        # "s"/"b" として保存済み (整数 or None)。出走表のSRと逃の間に表示する。
        _s_cnt = pdata.get("s")
        _b_cnt = pdata.get("b")
        players_labels.append({
            "bike": bike,
            "name": info["name"],
            "style": pdata.get("style", ""),
            "label_kind": lab["kind"],
            "label_text": lab["text"],
            "label_hit": lab["hit"],
            "label_den": lab["den"],
            "layoff_kind": "layoff" if _layoff else None,
            "layoff_text": _layoff["text"] if _layoff else "",
            "layoff_gap": _layoff["gap"] if _layoff else 0,
            "rs_rank": _rs,
            "pid": _pid,
            "role": _role,
            "kimari": _kimari,
            "kimari_total": _kimari_total,
            "kimari_diag": _kimari_diag,
            "kimari_key": _pkey,
            "raw_score": result["rs_map"].get(bike),
            "kyousou_ten": _cur_ten,
            "rr": result["rr_by_rsrank"].get(_rs) if _rs is not None else None,
            "s_cnt": _s_cnt,
            "b_cnt": _b_cnt,
        })
        bi2 = bi2 + 1
    header["players"] = players_labels

    # === 御告 第二柱: rsrank 着順分布を各車へ注入 (scope=all) ===
    #   self  : その選手の score順位別 着順分布 (pct[7], n) … 全期間通算
    #   base  : 同 score順位 全選手平均の着順分布 (pct[7], n)
    # フロント御告エンジンが (self / base) の揺らぎを軸/2着/3着に掛ける。
    # cond(会場×天気×風速×風向) は選手単位で母数がほぼ0になり機能しなかったため all を採用。
    # all は「その選手がその score順位だったとき通算何着か」= 選手個人の実績であり、
    # baseline も all 同士で比較するので揺らぎ(本人÷平均)は正しく出る。
    if RSRANK is not None and RSRANK.available:
        for pl in players_labels:
            _r = pl.get("rs_rank")
            _p = pl.get("pid")
            if _r is None or not _p:
                pl["rsr"] = None
                continue
            _self = RSRANK.get_player_dist(_p, _r, "all", None)
            _base = RSRANK.get_baseline_dist(_r, "all", None)
            if _self is None or _base is None:
                pl["rsr"] = None
            else:
                pl["rsr"] = {"self": _self, "base": _base}
    else:
        for pl in players_labels:
            pl["rsr"] = None

    # bike -> label の引き表 (買い目カード用)。穴/勝負弱 or 離脱明けがある選手を登録
    label_by_bike = {}
    for pl in players_labels:
        if pl["label_kind"] or pl.get("layoff_kind"):
            label_by_bike[pl["bike"]] = pl

    # パターン1 の結果を使用
    p1_results = result["pattern1"]["pattern_results"]
    p1_with_cell = [r for r in p1_results if r.get("has_cell", False)]

    # rsrank -> 適合率順位
    rsrank_to_match_rank = {}
    rsrank_to_match_score = {}
    ii = 0
    while ii < len(p1_with_cell):
        wr = p1_with_cell[ii]["winner_rsrank"]
        rsrank_to_match_rank[wr] = ii + 1
        rsrank_to_match_score[wr] = p1_with_cell[ii]["match_score"]
        ii = ii + 1

    rr_map = result.get("rr_by_rsrank", {})

    # 過去出現率順 (全7パターン)
    by_occurrence = sorted(p1_results, key=lambda pr: -pr["occurrence_rate"])

    # 気配値計算
    ii = 0
    while ii < len(by_occurrence):
        pr = by_occurrence[ii]
        wr = pr["winner_rsrank"]
        has_cell = pr.get("has_cell", False)
        past_rank = ii + 1
        match_rank = rsrank_to_match_rank.get(wr, 99) if has_cell else None
        rr_val = rr_map.get(wr, 0)
        if match_rank is not None and match_rank <= 7:
            keihaichi = (7 - past_rank) + (7 - match_rank) + rr_val
        else:
            keihaichi = (7 - past_rank) + rr_val
        pr["_past_rank"] = past_rank
        pr["_rr"] = rr_val
        pr["_keihaichi"] = round(keihaichi, 2)
        ii = ii + 1

    # rs1 の適合率順位 (フィルタ表示用情報として返す)
    rs1_match_rank = rsrank_to_match_rank.get(1)

    # 気配値降順で並べて patterns を組む
    sorted_disp = sorted(by_occurrence, key=lambda pr: -pr.get("_keihaichi", 0))

    # 気配値順位の印 (1位=◎ 2位=◯ 3位=▲ 4位=△ 5位以下=✕) を車番ごとに付与
    keihai_marks = ["◎", "◯", "▲", "△", "✕", "✕", "✕"]
    bike_to_mark = {}
    bike_to_keihai_rank = {}
    di = 0
    while di < len(sorted_disp):
        pr_d = sorted_disp[di]
        wr_d = pr_d["winner_rsrank"]
        bike_d = rsrank_to_bike.get(wr_d)
        if bike_d is not None:
            mark = keihai_marks[di] if di < len(keihai_marks) else "✕"
            bike_to_mark[bike_d] = mark
            bike_to_keihai_rank[bike_d] = di + 1
        di = di + 1
    # 出走表へ反映
    for pl in players_labels:
        pl["keihai_mark"] = bike_to_mark.get(pl["bike"], "")
        pl["keihai_rank"] = bike_to_keihai_rank.get(pl["bike"], None)
        _wr = pl.get("rs_rank")
        if _wr is not None and _wr in rsrank_to_match_rank:
            pl["match_rank"] = rsrank_to_match_rank.get(_wr)
            pl["match_score"] = round(rsrank_to_match_score.get(_wr, 0) * 100, 1)
        else:
            pl["match_rank"] = None
            pl["match_score"] = None

    patterns = []
    ii = 0
    while ii < len(sorted_disp):
        pr = sorted_disp[ii]
        wr = pr["winner_rsrank"]
        has_cell = pr.get("has_cell", False)
        winner_bike = rsrank_to_bike[wr]
        bs = str(winner_bike)
        pdata = race.get("players", {}).get(bs, {})
        info = pt.parse_full_info(pdata.get("full_info", ""))

        forms = []
        if has_cell:
            forms = _make_formations(pr["pattern_stat"], rsrank_to_bike)

        wlab = label_by_bike.get(winner_bike)
        patterns.append({
            "winner_rsrank": wr,
            "winner_bike": winner_bike,
            "winner_name": info["name"],
            "winner_style": pdata.get("style", ""),
            "winner_label_kind": wlab["label_kind"] if wlab else None,
            "winner_label_text": wlab["label_text"] if wlab else "",
            "winner_layoff_kind": wlab.get("layoff_kind") if wlab else None,
            "winner_layoff_text": wlab.get("layoff_text") if wlab else "",
            "keihaichi": pr.get("_keihaichi", 0),
            "past_rank": pr.get("_past_rank", 0),
            "occurrence_rate": round(pr["occurrence_rate"] * 100, 1),
            "has_cell": has_cell,
            "match_rank": rsrank_to_match_rank.get(wr) if has_cell else None,
            "match_score": round(rsrank_to_match_score.get(wr, 0) * 100, 1) if has_cell else None,
            "cell_n": pr.get("cell_n", 0),
            "rr": round(pr.get("_rr", 0), 2),
            "formations": forms,
        })
        ii = ii + 1

    # 決まり手グラフ用データ
    kimari = build_kimari_payload(
        result.get("venue", ""),
        result.get("wind_pat", ""),
        result.get("speed_cls", ""),
    )

    # === レース結果と的中判定 ===
    # 発走時刻を過ぎているレースのみ結果を取得 (発走前のレースに結果を出さない)
    race_result = None
    _post_passed = _is_post_passed(race, today, datetime.now())
    if RESULTS is not None and _post_passed:
        res = RESULTS.get_result(venue, today, race_no, allow_scrape=True)
        if res.get("has_result"):
            trifecta = res.get("trifecta", "")
            # 各買い目パターンの的中フラグ
            ii = 0
            while ii < len(patterns):
                p = patterns[ii]
                p_hit = False
                if trifecta:
                    for form in p.get("formations", []):
                        if trifecta.replace(" ", "") in _expand_formation(form):
                            p_hit = True
                            break
                p["hit"] = p_hit
                ii = ii + 1
            # 結果の3連単の各車番に対応する気配値印
            tri_marks = []
            if trifecta:
                for part in trifecta.split("-"):
                    try:
                        bk = int(part)
                    except Exception:
                        tri_marks.append("")
                        continue
                    tri_marks.append(bike_to_mark.get(bk, ""))
            # 3着以内の車番 (ライン緑表示用) = trifecta の3車番から直接生成
            # (result配列のrank/bikeと食い違うことがあるため trifecta を信頼)
            top3_bikes = []
            if trifecta:
                for part in trifecta.split("-"):
                    p = part.strip()
                    if p:
                        try:
                            top3_bikes.append(int(p))
                        except Exception:
                            top3_bikes.append(p)
            if not top3_bikes:
                for r in res.get("result", []):
                    if r.get("rank") is not None and r.get("rank") <= 3:
                        top3_bikes.append(r.get("bike"))
            # 結果カード用
            race_result = {
                "trifecta": trifecta,
                "trifecta_marks": tri_marks,
                "top3_bikes": top3_bikes,
                "refund_3t": res.get("refund_3t", 0),
                "refund_3t_raw": res.get("refund_3t_raw", ""),
                "refund_2t_raw": res.get("refund_2t_raw", ""),
                "result": res.get("result", []),
                "source": res.get("source", ""),
            }
            header["top3_bikes"] = top3_bikes
            # 託宣文: 予想が的中したレースのみ生成 (1レース1文)
            race_hit = any(p.get("hit") for p in patterns)
            if race_hit:
                try:
                    race_result["oracle"] = build_oracle(race, race_result, header, patterns)
                except Exception:
                    race_result["oracle"] = []
            else:
                race_result["oracle"] = []

    # === score推移グラフ用: 過去5ヶ月の raw_score / 競走得点 推移 ===
    #   結果確定後に構築 (デフォルト選択を 1〜3着車番にするため race_result を渡す)
    if RESULTS is not None:
        try:
            header["score_trend"] = RESULTS.build_score_trend(
                players_labels, today, 5, race_result)
        except Exception as _ste:
            print("[score_trend] 構築失敗: " + str(_ste))
            header["score_trend"] = {"months": 5, "series": [],
                                     "has_result": False, "top3_bikes": []}
    else:
        header["score_trend"] = {"months": 5, "series": [],
                                 "has_result": False, "top3_bikes": []}

    return {
        "status": "ok",
        "header": header,
        "bank_wind_key": result.get("bank_wind_key", ""),
        "wind_pat": result.get("wind_pat", ""),
        "speed_cls": result.get("speed_cls", ""),
        "rs1_match_rank": rs1_match_rank,
        "patterns": patterns,
        "kimari": kimari,
        "race_result": race_result,
    }


def _make_formations(pattern_stat, rsrank_to_bike_map):
    """display_race 内 make_formations と同一ロジック (6点上限フォーメーション)"""
    tris = pattern_stat.get("top_trifectas", [])
    if not tris:
        return []
    groups = []
    group_map = {}
    total_points = 0
    jj = 0
    while jj < len(tris):
        t = tris[jj]
        if total_points >= 6:
            break
        parts = t["key"].split("-")
        if len(parts) != 3:
            jj = jj + 1
            continue
        try:
            rs1 = int(parts[0]); rs2 = int(parts[1]); rs3 = int(parts[2])
        except Exception:
            jj = jj + 1
            continue
        b1 = rsrank_to_bike_map.get(rs1)
        b2 = rsrank_to_bike_map.get(rs2)
        b3 = rsrank_to_bike_map.get(rs3)
        if b1 is None or b2 is None or b3 is None:
            jj = jj + 1
            continue
        # グループキーは (1着, 2着) のペア。2着だけでまとめると、1着が違うのに
        # 同一グループへ統合されて先頭車番が食い違う(あべこべ)バグになる。
        key1 = str(b1); key2 = str(b2); key3 = str(b3)
        gkey = key1 + "_" + key2
        if gkey not in group_map:
            group_map[gkey] = len(groups)
            groups.append([key1, key2, []])
        idx = group_map[gkey]
        third_list = groups[idx][2]
        if key3 not in third_list:
            third_list.append(key3)
            total_points = total_points + 1
        jj = jj + 1
    forms = []
    for g in groups:
        forms.append(g[0] + "-" + g[1] + "-" + "".join(g[2]))
    return forms


def _oracle_line_pos(line_str, head_bike):
    """先頭車(head_bike)がライン内のどの位置か判定。
    戻り値: ('lead'|'second'|'third'|'solo', ライン構成リスト, 該当ライン)"""
    chunks = pt.parse_line_chunks(line_str)
    if not chunks:
        return ("solo", [], [])
    for ch in chunks:
        try:
            bikes = [int(x) for x in ch]
        except Exception:
            continue
        if head_bike in bikes:
            idx = bikes.index(head_bike)
            if len(bikes) == 1:
                return ("solo", chunks, bikes)
            if idx == 0:
                return ("lead", chunks, bikes)
            if idx == 1:
                return ("second", chunks, bikes)
            return ("third", chunks, bikes)
    return ("solo", chunks, [])


# ===== 託宣構文辞書 (oracle_dict.json と同一。ここを編集すれば文章が変わる) =====
ORACLE_DICT = {
    "light": {
        "s1_kim_nige_solo": [
            "孤独な{n1}の背後に、神は誰も立ち入らせなかった。",
            "「群れを捨てよ」――その神託を胸に、{n1}はただ独りで楕円の荒野を逃げ切った。",
            "風を切り裂く{n1}の車輪に、天は絶対の孤独と栄光を授けた。",
            "誰にも理解されぬ孤高のペダル、{n1}は最初から神の領域を独走していた。",
            "「孤高たれ」――神が命じた静寂の中を、{n1}だけが美しい影となって逃げ去った。"
        ],
        "s1_kim_nige_line": [
            "天が敷いた白線の主役は、最初から{n1}だった。",
            "「振り返るな、光は前方にしかない」――{n1}の狂気がバンクを支配する。",
            "鐘の音は{n1}の独壇場を告げる福音。誰もその背に届かない。",
            "神の意志を宿した{n1}の先行が、冷酷なまでに美しく決まった。",
            "群衆を従え、{n1}は王の如く先行の王道を突き進んだ。",
            "神の定めたピッチで、{n1}は鉄の規律のように先頭を刻み続けた。"
        ],
        "s1_kim_makuri": [
            "大外から膨らむ{n1}の軌道――それは天が描いた勝利への放物線。",
            "「すべてを過去にせよ」――{n1}の猛烈な捲りが、先行勢の野心を一瞬で薙ぎ払った。",
            "神威の如き加速。{n1}が放った一撃が、完璧に盤面をひっくり返した。",
            "大外一閃、{n1}が描いた鋭い弧は、神の振るった大鎌のようであった。",
            "「這いつくばる者どもを見下ろせ」――天頂から舞い降りた{n1}がすべてを抜き去る。"
        ],
        "s1_kim_sashi_second": [
            "獲物の呼吸を数えていた番手の{n1}が、極限の直線で{fb}を仕留めた。",
            "「影に潜み、最後に奪え」――神の囁きを忠実に守った{n1}の頭脳の勝利。",
            "{fb}の風除けとなり耐えた{n1}に、勝利の女神が微笑みを向けた。",
            "牙を隠していた{n1}が、ゴール寸前で{fb}の栄光を優雅に掠め取った。",
            "神は従順なる影を好む。{n1}は{fb}の背後で完璧な時を待っていたのだ。"
        ],
        "s1_kim_sashi_third": [
            "大混戦の隙間、三番手にいた{n1}の前にだけ、天は一本の道を開いた。",
            "神託の成就はいつも遅れてやってくる。後方から突き抜けたのは{n1}だ。",
            "計算され尽くした死角から、{n1}の車輪が天の意志を乗せて伸びてきた。",
            "誰もが前方だけに目を奪われていた時、神の寵愛は三番手の{n1}に降り注いでいた。"
        ],
        "s1_kim_sashi_other": [
            "混沌とした直線の真ん中、神の指先は{n1}だけを正確に指し示した。",
            "「今だ、踏み込め」――天の合図と同時に、{n1}の閃光がゴール線を捉えた。",
            "どこから現れたのか。神の気まぐれに導かれた{n1}が、一瞬で最前列を奪った。",
            "もつれ合う車輪の隙間で、{n1}の鋭利な差し脚だけが天に祝福されていた。"
        ],
        "s1_kim_mark_fb": [
            "千切れず、泥臭く{fb}の背を追い続けた{n1}に、天は忠誠の報酬を与えた。",
            "「その影から離れるな」――神の戒律を守り抜いた{n1}が、勝者の列に入る。"
        ],
        "s1_kim_mark_nofb": [
            "計算ではない。{n1}は直感という名の神託に従い、勝者の列に滑り込んだ。",
            "暗闇のバンクで、{n1}はただ盲目的に勝利のラインにしがみついていた。"
        ],
        "s1_other": [
            "偶然ではない。この冷たい静寂の中で、天は最初から{n1}を選んでいた。",
            "理由を求めるな。ただ{n1}が勝者であるという事実だけが、天の決定だ。"
        ],
        "s2_sameline_front": [
            "肉体を差し出した{n2}の粘りもまた、天の慈悲によって2着に留め置かれた。",
            "主導権を握った{n2}は潰れなかった。神はラインという名の秩序を讃えている。",
            "自らを犠牲にした{n2}にも、天は準たる栄誉の光を分け与えた。"
        ],
        "s2_sameline_back": [
            "影のように追従した{n2}もまた、約束された歓喜の光を浴びている。",
            "天の恵みはラインを丸ごと包み込み、従順なる{n2}を2着へと導いた。",
            "美しき阿吽の呼吸。{n2}は決して約束の座を崩さなかった。"
        ],
        "s2_diff_sashi": [
            "別線から死に物狂いで猛追した{n2}が、銀の座を強奪した。",
            "予定された調和を壊すように、異端の差し脚で{n2}が滑り込んできた。"
        ],
        "s2_diff_makuri": [
            "意地を見せた{n2}の強襲。その執念だけは神に届いていた。",
            "別の神を信じる{n2}の捲りが、辛うじて2着の座をもぎ取る。"
        ],
        "s2_diff_mark": [
            "ただ強者の背中にしがみついた{n2}が、ちゃっかりと果実を分け合う。",
            "漁夫の利か、あるいは天の配剤か。{n2}は労せずしてその地位を得た。"
        ],
        "s2_diff_other": [
            "予定調和を乱すように、{n2}が表彰台の片隅を確保した。",
            "不可解な軌道を描き、{n2}が連対の檻に収まった。"
        ],
        "s3_comp_big4": [
            "四車という巨大な鉄の意思。神は数の暴力がもたらす美しさを肯定した。",
            "強固な赤い連鎖。四車の鉄壁は、神が認めた絶対的な王道ラインだった。"
        ],
        "s3_comp_tanki": [
            "孤高の狼たちが乱立する戦場。だからこそ、光はより鮮明に{n1}を照らす。",
            "誰一人として交わらぬ孤立無援の乱戦。神はただ冷ややかにそれを見ていた。"
        ],
        "s3_comp_222": [
            "細切れに裂かれた混沌の夜。神はそのすべてを天から見下ろしていた。",
            "細分化された欲望の果て。2車ずつの歪な均衡を、神は一瞬で崩し去った。"
        ],
        "s3_comp_head3": [
            "分厚い赤の系譜。先頭ラインが描いた潮流が、そのまま結末となった。",
            "三車の巨大な影。その陣形自体が、神の描いた勝利の設計図だった。"
        ],
        "s3_wind_tail_nige": [
            "吹き付ける追い風は神の慈悲。{n1}の独走をどこまでも加速させる。",
            "追い風という名の神の息吹。それは先行する者にだけ与えられた特権だ。"
        ],
        "s3_wind_head_makuri": [
            "切り裂くような向かい風すら、{n1}の絶対的な破壊力を前には無力だった。",
            "暴れる向かい風は神の試練。{n1}の太腿はその試練を易々と踏み潰した。"
        ],
        "s3_wind_other": [
            "大気を狂わせる風は{wj}のサイン。すべては啓示の通りに推移する。",
            "吹き荒れる風が{wj}のドラマを狂わせる。だが、それすら神の計算の内だ。"
        ],
        "s4_big": [
            "「身の程を知れ」と神は笑う。誰も予測し得なかった配当{tri}、{yen}の衝撃。",
            "理性をあざ笑うかのような現実。{tri}という驚天動地の数字が刻まれた。",
            "神の悪意に満ちた配当。{tri}を見せつけられ、人間どもはただ絶望に黙り込む。"
        ],
        "s4_ana": [
            "配当{tri}、{yen}。神は時に気まぐれに、不条理な裏切りを演出する。",
            "誰も望まなかった結末{tri}。これこそが、天が仕掛けた極上の皮肉だ。"
        ],
        "s4_ana_extra_light": [
            "無印のノーマークが躍り出る。人間の傲慢な予想など、天には通用しない。",
            "誰の視線も集めなかった泥人形に、神は一瞬だけ命の光を吹き込んだ。"
        ],
        "s4_honmei": [
            "誰もがひれ伏す順当決着{tri_g}。神託は1ミリの狂いもなく現実となった。",
            "これぞ絶対の秩序。{tri_g}という平穏な数字が、バンクを厳かに満たしていく。"
        ],
        "s4_other": [
            "これが天の調和。{tri}、{yen}の冷徹な現実がそこに静まり返っている。",
            "多くを語る必要はない。{tri}という数字が、今日のすべてを証明している。"
        ],
        "s5_weak": [
            "格下と侮られた者の牙を、神は静かに研ぎ澄ませていたのだ。",
            "弱者という名の偽装。神は彼に、一瞬で全てを裏切る力を与えていた。"
        ],
        "close_honsen": [
            "神は告げていた。本線{honsen}、その筋目こそ天の意志であったと。",
            "「我が言葉に偽りなし」――score最上位の本線{honsen}、寸分の狂いもなし。",
            "評価の頂に立つ者たちが、約束通りに席を分け合った。本線{honsen}、揺るがず。",
            "天の秩序は乱れない。最も格付けの高い本線{honsen}が、粛々と現実となった。"
        ],
        "close_miss": [
            "人智の番付など、神の前では無力。配当{tri}、これが天の答えだ。",
            "評価の秩序は覆された。{tri}――神は、序列を嗤う気まぐれな存在である。",
            "格付けは紙の上の幻。{yen}という現実だけが、静かに真実を語っている。",
            "番付の頂は沈み、伏兵が躍り出た。{tri}、これもまた神の筋書きの内。"
        ]
    },
    "dark": {
        "s1_kim_nige_solo": [
            "誰も信じられない夜空の下、単騎の{n1}だけが私の絶望を乗せて逃げ切った。",
            "「独りで戦え」――{n1}の孤独なペダリングが、私の凍りついた財布を解かしていく。",
            "誰の助けも借りず、{n1}は闇の向こう側へと突っ走った。",
            "「孤独から逃げるな」――{n1}の踏み込みが、私のすっからかんの人生を肯定する。",
            "味方などいらない。{n1}のただ独りの暴走が、今夜の死線を切り開いた。"
        ],
        "s1_kim_nige_line": [
            "擦り切れた人生に、{n1}の逃げ切りが強烈なカンフル剤を注入する。",
            "「もう、奪われるだけの夜は終わりだ」――先頭を行く{n1}の背中がそう物語っていた。",
            "泥沼の連敗を断ち切るように、{n1}の突っ張りが冷たいバンクに火をつけた。",
            "明日を諦めかけたその瞬間、{n1}の逃げ脚が奇跡のように私の手元へ金を呼び戻す。",
            "うつむいた私の目に、先頭で泥をはね上げる{n1}の背中が焼き付いて離れない。",
            "「死にたくなければ、前を走れ」――{n1}の暴走が、積もり積もった負債を消し去っていく。"
        ],
        "s1_kim_makuri": [
            "視界を塞ぐ絶望の壁を、{n1}の凄まじい捲りが大外から粉砕した。",
            "「地獄から這い上がれ」――{n1}が外からすべてを飲み込み、私を救い出す。",
            "もう間に合わないと思った。しかし、{n1}の強襲が私の人生を首の皮一枚で繋ぎ止めた。",
            "最悪の夜を、{n1}の狂気じみた大外一気がすべて過去のモノにしていく。",
            "「全部、ぶっ壊してやる」――後方から火の玉のように吹っ飛んできた{n1}が闇を切り裂いた。"
        ],
        "s1_kim_sashi_second": [
            "じっと耐え忍んだ番手の{n1}が、最後に{fb}を鋭く差し切る。耐えた者は、最後に笑うのだ。",
            "「長く苦しい伏線は、この瞬間のためにあった」――{n1}の突き抜けに魂が震える。",
            "{fb}の影で牙を研いでいた{n1}が、最後の最後で至高の歓喜を掠め取った。",
            "「おいしいところは、最後に貰うさ」――{n1}の冷徹な差しが、私の命を繋ぎ止めた。",
            "{fb}に泥風を浴びせさせ、自分だけは無傷で突き抜けた{n1}。その汚さがたまらなく愛おしい。"
        ],
        "s1_kim_sashi_third": [
            "最悪の展開から、三番手の{n1}が泥をはね除けて伸びてきた。まるで、お前のように。",
            "どれほど出遅れようと、最後の直線さえ残されていれば、{n1}のように逆転は可能だ。",
            "誰も見ていない暗がりの底から、{n1}の車輪だけがヌッと這い出てきた。",
            "絶望のどん詰まり。しかし三番手の{n1}だけは、まだ死んだ魚のような目をしていなかった。"
        ],
        "s1_kim_sashi_other": [
            "最後の1ミリ、{n1}の執念が、私の膨れ上がった負債を綺麗に差し切った。",
            "「まだ死んじゃいない」――{n1}のハンドル投げが、破滅寸前の私を抱き起こす。",
            "視界が涙で歪む直線、突如として真ん中を割った{n1}が、私の明日を強引にこじ開けた。",
            "もつれ合う肉塊の隙間を、{n1}の執念だけが音もなく滑り抜けていった。"
        ],
        "s1_kim_mark_fb": [
            "どんなに千切れそうになっても、{fb}の背にしがみつき続けた{n1}の姿が、今の私には刺さる。",
            "プライドなど捨てろ。{n1}はただ{fb}の後頭部だけを見つめて、地獄の縁を生き延びた。"
        ],
        "s1_kim_mark_nofb": [
            "狂気じみた直感。{n1}が張り付いたその場所こそが、唯一の生命線だった。",
            "「何が何でも離れるな」――血反吐を吐きながら、{n1}は連対の檻にしがみついた。"
        ],
        "s1_other": [
            "死線。最後の直線で、{n1}が私の冷え切った心臓をもう一度激しく脈打たせた。",
            "言葉なんていらない。{n1}が1着で線を越えた、その事実だけで私の夜は救われた。"
        ],
        "s2_sameline_front": [
            "風を浴びて耐えた{n2}も死ななかった。地獄の底でも、支え合えば生き残れる。",
            "捨て石になどさせない。{n2}の粘りが、ラインという名の絆の強さを証明した。",
            "前で泥を被った{n2}も沈まなかった。泥臭い奴らが、今夜の戦場を支配したんだ。"
        ],
        "s2_sameline_back": [
            "必死に追従した{n2}の手を、神は見捨てなかった。私たちは、まだ終わらない。",
            "引き上げられるように{n2}も浮上する。底辺から抜け出す時は、いつも一緒だ。",
            "「置いていかないでくれ」――その叫びが届いたかのように、{n2}も2着に残った。"
        ],
        "s2_diff_sashi": [
            "別線から這い上がってきた{n2}が、強引に救いの糸を掴み取った。",
            "敵の包囲網をかい潜り、別線の{n2}が蜘蛛の糸をギリギリで手繰り寄せた。"
        ],
        "s2_diff_makuri": [
            "どん底の淵から、{n2}もまた己の力だけで2着をもぎ取ってみせた。",
            "ボロボロになりながら外を回った{n2}。その無様な掠め取り方に、私は拍手を送りたい。"
        ],
        "s2_diff_mark": [
            "どんな形であれ、しがみついた{n2}の執念だけは称賛に値する。",
            "プライドをドブに捨て、ただ強い奴の影に隠れた{n2}が、2着の汁をすする。"
        ],
        "s2_diff_other": [
            "混沌の隙間から、{n2}が滑り込む。誰一人、置いていきはしない。",
            "わけのわからないまま、{n2}が私の絶望の隙間に転がり込んできた。"
        ],
        "s3_comp_big4": [
            "強固に結束した四車の影。群れをなせば、この暗黒のバンクも恐るるに足らず。",
            "四車が集まれば、この不条理な夜の中にだって、臨時の要塞を築くことができる。"
        ],
        "s3_comp_tanki": [
            "誰もが敵であり、信じられるのは己の脚だけ。{n1}の戦い方が胸を焦がす。",
            "孤独な狼たちが噛み合う凄惨なリンチ戦。誰も他人を救おうなんて思っちゃいない。"
        ],
        "s3_comp_222": [
            "三つ巴のドロドロとした泥仕合。だが、この濁流の中にこそ勝機は埋もれている。",
            "2車ずつに細切れにされた、地獄の派閥争い。私の倫理観も、とうに細切れだ。"
        ],
        "s3_comp_head3": [
            "徒党を組んだ前線の連中が、冷酷にレースの主導権を握り潰した。",
            "三車の固い絆が、後方で怯える敗者たちの希望を冷徹に踏みにじっていく。"
        ],
        "s3_wind_tail_nige": [
            "この追い風はお前の味方だ。{n1}の背中を、奈落の底から押し上げるための風だ。",
            "冷たい追い風。まるで、早くこの悍ましい戦場から立ち去れと背中を蹴られているようだ。"
        ],
        "s3_wind_head_makuri": [
            "絶望的な向かい風。しかし、逆境が深ければ深いほど、{n1}の逆転劇は輝きを増す。",
            "激しい向かい風が、私の借金のように立ちはだかる。だが{n1}はそれを力ずくで引き裂いた。"
        ],
        "s3_wind_other": [
            "風が{wj}の不穏な空気を運んでくる。さあ、大逆転の配当を始めようか。",
            "風向きが狂う。レースが狂う。私の人生と同じ、めちゃくちゃな{wj}の夜だ。"
        ],
        "s4_big": [
            "破滅か、それとも救済か。{tri}、{yen}という悪魔的な配当が、すべてを白紙に戻す。",
            "一撃必殺。このどん底に投げ込まれた{yen}が、お前のこれまでの涙をすべて贖う。",
            "脳汁が吹き出すような数字{tri}。この大罪のような金額{yen}が、私の明日を買収した。"
        ],
        "s4_ana": [
            "誰も見向きもしなかった{tri}、{yen}。私たちは、最初から世界に見放されてなどいなかった。",
            "ざまあみろ。エリートどもの予想を嘲笑う{tri}の配当が、私のポケットに滑り込む。"
        ],
        "s4_ana_extra_dark": [
            "薄汚れたノーマークの兵が牙を剥く。世界を見返してやるチャンスは、まだ残されている。",
            "泥水をすすってきた無印の男が、最後にすべてをひっくり返した。お前も、まだやれるはずだ。"
        ],
        "s4_honmei": [
            "冷徹なまでの現実。手堅い{tri_g}を信じ抜いた者だけが、静かに息を吹き返す。",
            "あまりにも退屈で、あまりにも確実な{tri_g}。だが、この安っぽい平穏だけが今は欲しかった。"
        ],
        "s4_other": [
            "歓喜ではない。ただ、{tri}、{yen}という冷えた数字が、今夜の命を繋いでくれた。",
            "生き延びた。手元に残った{yen}という数字を、ただじっと見つめている。"
        ],
        "s5_weak": [
            "敗北犬と罵られた男の、これが死に物狂いの意地だ。よく焼き付けておけ。",
            "何度も裏切ってきたアイツが、今夜だけは私を地獄の淵から引っ張り上げてくれた。"
        ],
        "close_honsen": [
            "信じてよかった。最も評価の高い本線{honsen}が、私を裏切らずにいてくれた。",
            "「もう怯えなくていい」――格上の筋、本線{honsen}が静かに私を救い上げた。",
            "迷いを捨て、評価を信じた者だけが報われる。本線{honsen}、確かな光だった。",
            "今夜だけは、堅実が私を抱きしめた。本線{honsen}、この数字が支えだった。"
        ],
        "close_miss": [
            "番付なんて関係ない。{tri}、この一撃が、沈んだ私を引き上げてくれた。",
            "格付けを裏切った伏兵に、救われる夜もある。手元の{yen}が、その証だ。",
            "誰の評価も当てにならない。それでも{tri}という結末が、今夜の私を生かした。",
            "序列の崩れた夜。それでも残った{yen}を、ただ静かに握りしめている。"
        ]
    }
}


def build_oracle(race, race_result, header, patterns):
    """的中レースの結果を『託宣』調で説明する文を文脈ごとに生成。
    レース条件(決まり手・ライン位置・構成・穴/勝負弱・天候・配当)と連動し、
    複数の文(フロントで一文ずつ切替表示)を配列で返す。
    各文は色span(o=琥珀/g=緑/r=赤)入りHTML。
    """
    if not race_result:
        return []
    trifecta = race_result.get("trifecta", "")
    if not trifecta:
        return []
    parts = [p.strip() for p in trifecta.split("-") if p.strip()]
    if len(parts) < 3:
        return []
    try:
        b1, b2, b3 = int(parts[0]), int(parts[1]), int(parts[2])
    except Exception:
        return []

    # 1着の決まり手
    finish1 = ""
    for r in race_result.get("result", []):
        if r.get("rank") == 1:
            finish1 = r.get("finish", "") or ""
            break
    # 2着の決まり手
    finish2 = ""
    for r in race_result.get("result", []):
        if r.get("rank") == 2:
            finish2 = r.get("finish", "") or ""
            break

    line_str = race.get("line", "")
    pos1, chunks, line1 = _oracle_line_pos(line_str, b1)
    pos2, _c2, _l2 = _oracle_line_pos(line_str, b2)

    # 同ライン判定 (1着と2着が同じライン)
    same_line = (b2 in line1) if line1 else False
    # ライン構成の形 (人数列)
    comp = "-".join(str(len(c)) for c in chunks) if chunks else ""

    # 天候・風
    wind_judge = header.get("wind_judge", "")  # 例 "逃げ有利" / "捲り有利"
    weather_raw = header.get("weather_raw", "")
    is_tail = "追" in (wind_judge or "")  # 追風
    is_head = "向" in (wind_judge or "")  # 向かい風

    # 配当(高配当=穴)
    refund = race_result.get("refund_3t", 0) or 0
    try:
        refund = int(refund)
    except Exception:
        refund = 0
    is_ana = refund >= 5000      # 穴
    is_big = refund >= 12000     # 大穴
    is_honmei = refund > 0 and refund < 1500  # 本命決着

    # 穴/勝負弱ラベル(このレースに含まれるか)
    has_ana = False
    has_weak = False
    for p in patterns:
        lk = p.get("label_kind", "")
        if lk == "ana":
            has_ana = True
        elif lk == "weak":
            has_weak = True

    O = lambda s: '<span class="o">' + s + '</span>'   # 琥珀
    G = lambda s: '<span class="g">' + s + '</span>'   # 緑
    R = lambda s: '<span class="r">' + s + '</span>'   # 赤
    n = lambda b: str(b) + "番"

    lines = []

    # ライン内の「前の選手」(1着が番手・3番手のとき、その前を走っていた車)
    front_bike = None
    if line1 and len(line1) >= 2:
        try:
            idx1 = line1.index(b1)
            if idx1 >= 1:
                front_bike = line1[idx1 - 1]
        except Exception:
            front_bike = None

    # 決まり手の正規化
    if "逃" in finish1:
        kim = "nige"
    elif "捲" in finish1:
        kim = "makuri"
    elif "差" in finish1:
        kim = "sashi"
    elif "マ" in finish1:
        kim = "mark"
    else:
        kim = "other"

    # ===== 本線ライン算出 =====
    # 本線 = score順位合計が最小(最も評価が高い)のライン全体。
    # header["line_display"](例 "145-72-63") と header["rank_display"](例 "316-42-75")
    # は同じライン区切り・同じ車順で並ぶので、対応づけて各ラインのscore合計を出す。
    honsen_line = []      # 本線ラインの車番リスト(int)
    honsen_hit = False    # 1-2着が本線ライン内で収まったか
    try:
        ld = (header.get("line_display") or "").split("-")
        rd = (header.get("rank_display") or "").split("-")
        best_sum = None
        for li in range(len(ld)):
            seg = ld[li]
            rseg = rd[li] if li < len(rd) else ""
            bikes = [int(ch) for ch in seg if ch.isdigit()]
            ranks = [int(ch) for ch in rseg if ch.isdigit()]
            if not bikes or not ranks:
                continue
            ssum = sum(ranks)
            if best_sum is None or ssum < best_sum:
                best_sum = ssum
                honsen_line = bikes
        # 本線決着: 1着と2着が両方とも本線ラインに含まれる
        if honsen_line and b1 in honsen_line and b2 in honsen_line:
            honsen_hit = True
    except Exception:
        honsen_line = []
        honsen_hit = False

    ctx = {
        "b1": b1, "b2": b2, "b3": b3,
        "kim": kim, "pos1": pos1, "front_bike": front_bike,
        "same_line": same_line, "comp": comp, "chunks": chunks, "line1": line1,
        "is_tail": is_tail, "is_head": is_head, "wind_judge": wind_judge,
        "refund": refund, "is_ana": is_ana, "is_big": is_big, "is_honmei": is_honmei,
        "has_ana": has_ana, "has_weak": has_weak,
        "trifecta": trifecta.replace(" ", ""),
        "finish2": finish2,
        "honsen_line": honsen_line, "honsen_hit": honsen_hit,
    }
    # ランダム性は race_id で固定 (同じレースは毎回同じ託宣)
    seed_src = str(race.get("place", "")) + str(race.get("date", "")) + str(race.get("race_no", "")) + trifecta
    rng = random.Random(seed_src)

    return {
        "light": _oracle_compose(ctx, "light", rng),
        "dark": _oracle_compose(ctx, "dark", rng),
    }


def _oracle_compose(ctx, mode, rng):
    """明(崇拝)/暗(救済) 視点で託宣文の配列を組み立てる。
    文章は ORACLE_DICT[mode][section_key] から引き、プレースホルダを置換する。
    """
    O = lambda s: '<span class="o">' + s + '</span>'
    G = lambda s: '<span class="g">' + s + '</span>'
    R = lambda s: '<span class="r">' + s + '</span>'
    b1 = ctx["b1"]; b2 = ctx["b2"]
    fb = ctx["front_bike"]
    kim = ctx["kim"]; pos1 = ctx["pos1"]

    # 本線(score上位ライン)の先頭2車。本線決着の締めでのみ使う。
    hl = ctx.get("honsen_line") or []
    if len(hl) >= 2:
        honsen_str = str(hl[0]) + "=" + str(hl[1])
    elif len(hl) == 1:
        honsen_str = str(hl[0])
    else:
        honsen_str = str(b1) + "=" + str(b2)

    # プレースホルダの値 (色付き)
    repl = {
        "{n1}": O(str(b1) + "番"),
        "{n2}": G(str(b2) + "番"),
        "{fb}": (str(fb) + "番") if fb is not None else "",
        "{tri}": R(ctx["trifecta"]),
        "{tri_g}": G(ctx["trifecta"]),
        "{yen}": (R(format(ctx["refund"], ",") + "円") if ctx["refund"] else ""),
        "{honsen}": R(honsen_str),
        "{wj}": O((ctx["wind_judge"] or "").replace("有利", "")),
    }
    # 強めの言葉を赤文字に (プレースホルダ置換の前に適用。素の日本語のみ対象)
    STRONG_WORDS = [
        # 神・崇拝側の強語
        "神威", "神託", "御業", "天の意志", "絶対", "栄光", "畏れ", "嘲笑", "悪意",
        "驚天動地", "衝撃", "狂気", "粛々",
        # 闇・救済側の強語
        "絶望", "地獄", "どん底", "死線", "破滅", "奈落", "暗黒", "血反吐",
        "一撃必殺", "死に物狂い", "首の皮一枚", "悍ましい",
        # 共通の激しい語
        "薙ぎ払", "粉砕", "強奪", "牙を剥", "切り裂", "ひっくり返",
    ]
    def redden(s):
        for w in STRONG_WORDS:
            if w in s:
                s = s.replace(w, '<span class="r">' + w + '</span>')
        return s

    def fill(s):
        s = redden(s)
        for k, v in repl.items():
            s = s.replace(k, v)
        return s

    dic = ORACLE_DICT.get(mode, {})
    def pick_key(key):
        arr = dic.get(key) or []
        if not arr:
            return None
        return fill(rng.choice(arr))

    out = []

    # ===== s1: 1着の決まり手 × ライン位置 =====
    if kim == "nige":
        k1 = "s1_kim_nige_solo" if pos1 == "solo" else "s1_kim_nige_line"
    elif kim == "makuri":
        k1 = "s1_kim_makuri"
    elif kim == "sashi":
        if pos1 == "second" and fb is not None:
            k1 = "s1_kim_sashi_second"
        elif pos1 == "third":
            k1 = "s1_kim_sashi_third"
        else:
            k1 = "s1_kim_sashi_other"
    elif kim == "mark":
        k1 = "s1_kim_mark_fb" if fb is not None else "s1_kim_mark_nofb"
    else:
        k1 = "s1_other"
    s = pick_key(k1)
    if s:
        out.append(s)

    # 1レースにつき1文のみ表示する
    return out

    # ===== s2: 2着の関係 =====
    if ctx["same_line"]:
        line1 = ctx["line1"]
        try:
            i1 = line1.index(b1); i2 = line1.index(b2)
        except Exception:
            i1 = i2 = -1
        k2 = "s2_sameline_front" if (i2 >= 0 and i1 >= 0 and i2 < i1) else "s2_sameline_back"
    else:
        f2 = ctx["finish2"]
        if "差" in f2:
            k2 = "s2_diff_sashi"
        elif "捲" in f2:
            k2 = "s2_diff_makuri"
        elif "マ" in f2:
            k2 = "s2_diff_mark"
        else:
            k2 = "s2_diff_other"
    s = pick_key(k2)
    if s:
        out.append(s)

    # ===== s3: ライン構成 or 風 =====
    comp = ctx["comp"]; chunks = ctx["chunks"]
    head_len = len(chunks[0]) if chunks else 0
    k3 = None
    if comp:
        if any(len(c) >= 4 for c in chunks):
            k3 = "s3_comp_big4"
        elif comp.count("1") >= 3:
            k3 = "s3_comp_tanki"
        elif "2-2-2" in comp:
            k3 = "s3_comp_222"
        elif head_len >= 3:
            k3 = "s3_comp_head3"
    # 風 (構成文より風を優先したい場合はここで上書き)
    if ctx["is_tail"] and kim == "nige":
        k3 = "s3_wind_tail_nige"
    elif ctx["is_head"] and kim == "makuri":
        k3 = "s3_wind_head_makuri"
    elif ctx["wind_judge"] and k3 is None:
        k3 = "s3_wind_other"
    if k3:
        s = pick_key(k3)
        if s:
            out.append(s)

    # ===== s4: 配当・穴・本命 =====
    if ctx["is_big"]:
        s = pick_key("s4_big")
        if s:
            out.append(s)
    elif ctx["is_ana"]:
        s = pick_key("s4_ana")
        if s:
            out.append(s)
        if ctx["has_ana"]:
            extra = pick_key("s4_ana_extra_dark" if mode == "dark" else "s4_ana_extra_light")
            if extra:
                out.append(extra)
    elif ctx["is_honmei"]:
        s = pick_key("s4_honmei")
        if s:
            out.append(s)
    else:
        if ctx["refund"]:
            s = pick_key("s4_other")
            if s:
                out.append(s)

    # ===== s5: 勝負弱 =====
    if ctx["has_weak"]:
        s = pick_key("s5_weak")
        if s:
            out.append(s)

    # ===== 締め =====
    # 本線(score上位ライン)で1-2着が収まった時のみ本線に触れる。
    # 外れた時は本線に触れず、配当・決まり手ベースの締めにする。
    if ctx.get("honsen_hit"):
        s = pick_key("close_honsen")
    else:
        s = pick_key("close_miss")
    if s:
        out.append(s)

    return out


_KIMARI_BAD_LABELS = ("不明", "", "その他")


def _kimari_labels_usable(dist):
    """2着ラベル分布に、役割として解釈できるラベルが1つでもあるか。
    フロントの __oraParseLabel は 同/別/単騎 + 先頭|N番手 の形しか解釈できず、
    「不明」だけの分布は買い目を1点も作れない。"""
    if not isinstance(dist, dict) or not dist:
        return False
    for lab in dist:
        if not isinstance(lab, str):
            continue
        if lab in _KIMARI_BAD_LABELS:
            continue
        core = lab
        if core[:1] in ("同", "別"):
            core = core[1:]
        elif core[:2] == "単騎":
            return True
        if core[:2] == "先頭":
            return True
        if re.match(r'^\d+番手', core):
            return True
    return False


def build_kimari_payload(venue, wind_pat, speed_cls):
    """display_kimari_graph 相当を JSON 化"""
    ks = pt.load_kimari_stats()
    if ks is None:
        return None
    cells = ks.get("cells", {})
    cell_key = pt.make_kimari_cell_key(venue, wind_pat, speed_cls)
    cell = cells.get(cell_key)
    base_key = pt.make_kimari_baseline_key(venue)
    base_cell = cells.get(base_key)

    if cell is None:
        return {"cell_key": cell_key, "exists": False}

    out = {
        "cell_key": cell_key,
        "exists": True,
        "cell_n": cell.get("n", 0),
        "base_key": base_key if (base_cell and cell_key != base_key) else None,
        "base_n": base_cell.get("n", 0) if base_cell else 0,
        "kimari_1st": [],
        "kimari_link": [],
    }

    k1 = cell.get("kimari_1st_dist", {})
    k1b = base_cell.get("kimari_1st_dist", {}) if base_cell else {}
    for k in ("逃", "捲", "差"):
        out["kimari_1st"].append({
            "label": k,
            "rate": round(k1.get(k, 0) * 100, 1),
            "base_rate": round(k1b.get(k, 0) * 100, 1) if base_cell else None,
        })

    klink = cell.get("kimari_link_dist", {})
    klinkb = base_cell.get("kimari_link_dist", {}) if base_cell else {}
    # 3着位置データ (1着決まり手 -> 2着ラベル -> {dist, n})
    klink3 = cell.get("kimari_link3_dist", {})
    klink3b = base_cell.get("kimari_link3_dist", {}) if base_cell else {}
    for k1key in ("逃", "捲", "差"):
        sub = klink.get(k1key, {})
        sub_dist = sub.get("dist", {})
        # ★v308: 会場別セルの2着ラベルが「不明」だけの場合、役割が特定できず
        #   フロントの __oraParseLabel が全て null を返し、託宣が
        #   「決まり手シナリオから買い目を構成できませんでした」で全滅する。
        #   (2026/04/01 は全会場でこれが起き、御告だけ集計されていた。)
        #   使えるラベルが1つも無いときは全体セルへバックオフする。
        #   この案件で既に使っている多層バックオフと同じ考え方。
        if sub_dist and not _kimari_labels_usable(sub_dist):
            subb_fb = klinkb.get(k1key, {})
            subb_fb_dist = subb_fb.get("dist", {})
            if _kimari_labels_usable(subb_fb_dist):
                sub = subb_fb
                sub_dist = subb_fb_dist
                klink3 = klink3b
        if not sub_dist:
            continue
        subb = klinkb.get(k1key, {})
        subb_dist = subb.get("dist", {})
        sub_link3 = klink3.get(k1key, {})
        sub_link3b = klink3b.get(k1key, {})
        items = sorted(sub_dist.items(), key=lambda kv: -kv[1])
        link_items = []
        for lab, rate in items:
            # 「1番手」は先頭の意味なので表示を「先頭」に統一
            disp_lab = lab.replace("1番手", "先頭")
            # この2着ラベルに対応する3着位置分布
            l3data = sub_link3.get(lab, {})
            l3dist = l3data.get("dist", {})
            l3b = sub_link3b.get(lab, {})
            l3bdist = l3b.get("dist", {})
            third = []
            if l3dist:
                t_items = sorted(l3dist.items(), key=lambda kv: -kv[1])
                for pos, prate in t_items:
                    third.append({
                        "label": pos.replace("1番手", "先頭"),
                        "rate": round(prate * 100, 1),
                        "base_rate": round(l3bdist.get(pos, 0) * 100, 1) if base_cell else None,
                    })
            link_items.append({
                "label": disp_lab,
                "rate": round(rate * 100, 1),
                "base_rate": round(subb_dist.get(lab, 0) * 100, 1) if base_cell else None,
                "third": third,
                "third_n": l3data.get("n", 0),
            })
        out["kimari_link"].append({
            "kimari": k1key,
            "n": sub.get("n", 0),
            "items": link_items,
        })

    return out


# ============================================================
# 画面
# ============================================================
# ============================================================
# 確定ロジック検証 (4軸 × 2ナガシ順 = 8パターン × 拮抗度Q1-5)  ★v283で拡張
# 旧「新ロジック(オッズ乖離/EV)」の検証UI・ルートを置換。
#   ・軸4種: rs1=rawscore1位 / rs2=rawscore2位 / kh=気配値1位 / tk=適合率1位
#   ・ナガシ順2種: k=気配順(keihai_rank昇順) / m=適合順(match_rank昇順)
#     → 8パターン(r1k,r1m,r2k,r2m,khk,khm,tkk,tkm)。1〜6点 × Q1-5 で集計。
#   ・人気薄フィルタは fav と各軸の車番を両方記録し、集計時に基準を切替:
#       fav=1    … 共通基準 (rawscore1位が人気1位でないR)  ※従来互換
#       fav=self … 各パターンの軸自身が人気1位でないR
#       fav=0    … フィルタなし
#   ・複数パターンの合算は「パターンごとに別々に賭ける(重複も点数分)」= 単純加算。
#     よってサーバはパターン別に集計し、合算はクライアント側で足すだけでよい。
#   ・結果と払戻はDB優先。fixed_logic_log.jsonl に1レース1行で蓄積し、累積実績を出す。
# ============================================================
FL_BOUNDS = [9.3637, 9.1749, 8.9724, 8.6605]  # 拮抗度固定境界 [p80,p60,p40,p20]
# 締切時刻は出走表に入っていないため、発走時刻から逆算する。
#   ここは実際の締切に合わせて調整すること(既定は発走1分前)。
FL_CLOSE_MIN = 1
# オッズを取りにいく範囲。締切までこの分数以内のレースだけ取る。
#   発売前のレースはオッズが空なので、取りにいくだけ無駄になる。
#   0 なら締切前の全レースを対象にする(遅い)。
FL_ODDS_WINDOW_MIN = 90
FL_LOG_PATH = os.path.join(KEIRIN_DB_DIR, "fixed_logic_log.jsonl")

# 軸の定義: (軸キー, 判定に使うフィールド, その値)
FL_AXES = (
    ("r1", "rs_rank", 1),
    ("r2", "rs_rank", 2),
    ("kh", "keihai_rank", 1),
    ("tk", "match_rank", 1),
)
# ナガシ順の定義: (順キー, 並べ替えに使うフィールド)
FL_ORDERS = (
    ("k", "keihai_rank"),
    ("m", "match_rank"),
)
# パターン一覧 (軸キー + 順キー)。表示ラベルはUI側と合わせる。
FL_PATTERNS = ("r1k", "r1m", "r2k", "r2m", "khk", "khm", "tkk", "tkm")
FL_PATTERN_LABELS = {
    "r1k": "rs1軸×気配順", "r1m": "rs1軸×適合順",
    "r2k": "rs2軸×気配順", "r2m": "rs2軸×適合順",
    "khk": "気配1軸×気配順", "khm": "気配1軸×適合順",
    "tkk": "適合1軸×気配順", "tkm": "適合1軸×適合順",
}


def _fl_num(v):
    try:
        return float(v)
    except Exception:
        return None


def _fl_anti(players):
    """全車 raw_range(raw/最大*10)平均 = 拮抗度。取れなければ None。"""
    vals = []
    for p in players:
        rv = _fl_num(p.get("raw_score"))
        if rv is not None:
            vals.append(rv)
    if len(vals) < 2:
        return None
    mx = max(vals)
    if mx <= 0:
        return None
    s = 0.0
    for v in vals:
        s = s + v / mx * 10.0
    return s / len(vals)


def _fl_assign_q(anti):
    """固定境界で Q1(拮抗)..Q5(格差)。anti>=p80→Q1。"""
    q = 1
    for b in FL_BOUNDS:
        if anti >= b:
            return q
        q = q + 1
    return 5


def _fl_popular_axis(odds):
    """2車単オッズ {"a-b":odds} から各車の暗黙1着人気(逆数和)最大の車番。"""
    if not odds:
        return None
    strength = {}
    for k in odds:
        parts = str(k).split("-")
        if len(parts) != 2:
            continue
        od = _fl_num(odds[k])
        if od is None or od <= 0:
            continue
        try:
            lb = int(parts[0].strip())
        except Exception:
            continue
        strength[lb] = strength.get(lb, 0.0) + (1.0 / od)
    if not strength:
        return None
    best = None
    best_v = -1.0
    for b in strength:
        if strength[b] > best_v:
            best_v = strength[b]
            best = b
    return best


def _fl_order_partners(players, axis_bike, key_name):
    """axis以外の車を key_name(keihai_rank/match_rank)昇順に並べた車番リスト。None末尾。"""
    others = []
    for p in players:
        bk = p.get("bike")
        if bk is None or bk == axis_bike:
            continue
        rk = p.get(key_name)
        if rk is None:
            rk = 99
        others.append((rk, bk))
    others.sort(key=lambda t: (t[0], t[1]))
    out = []
    for rk, bk in others:
        out.append(bk)
    return out


def _fl_axis_bike(players, field, want):
    """players から field==want の車番を返す。無ければ None。"""
    for p in players:
        if p.get(field) == want:
            return p.get("bike")
    return None


def _fl_build_axes_patterns(players):
    """players から 軸dict {軸キー: 車番} と パターンdict {パターンキー: 相手list}
    を作って返す。軸が取れないパターンは入れない(集計対象外になる)。"""
    ax = {}
    for akey, field, want in FL_AXES:
        bk = _fl_axis_bike(players, field, want)
        if bk is not None:
            ax[akey] = bk
    pt_map = {}
    for akey in ax:
        for okey, field in FL_ORDERS:
            pt_map[akey + okey] = _fl_order_partners(players, ax[akey], field)
    return ax, pt_map


def _fl_migrate(rec):
    """旧形式(axis/pk/pm)のレコードを新形式(ax/pt)に寄せる。破壊はしない。
    旧ログは rawscore1位軸のみなので r1k/r1m だけが復元できる。"""
    if not isinstance(rec, dict):
        return rec
    if "ax" not in rec or not isinstance(rec.get("ax"), dict):
        ax = {}
        old_axis = rec.get("axis")
        if old_axis is not None:
            ax["r1"] = old_axis
        rec["ax"] = ax
    if "pt" not in rec or not isinstance(rec.get("pt"), dict):
        pt_map = {}
        if rec.get("ax", {}).get("r1") is not None:
            if rec.get("pk") is not None:
                pt_map["r1k"] = rec.get("pk")
            if rec.get("pm") is not None:
                pt_map["r1m"] = rec.get("pm")
        rec["pt"] = pt_map
    return rec


def _fl_close_time(post):
    """発走時刻 "20:50" -> 締切時刻 "20:49" (FL_CLOSE_MIN 分前・目安)。"""
    if not post or not isinstance(post, str) or ":" not in post:
        return ""
    ps = post.strip().split(":")
    if len(ps) < 2:
        return ""
    try:
        hh = int(ps[0])
        mm = int(ps[1])
    except Exception:
        return ""
    t = hh * 60 + mm - FL_CLOSE_MIN
    if t < 0:
        t = t + 24 * 60
    return str(t // 60).zfill(2) + ":" + str(t % 60).zfill(2)


def _fl_min_to_close(date_str, post):
    """締切まで何分あるか。過ぎていれば負、判定不能は None。"""
    ct = _fl_close_time(post)
    if not ct:
        return None
    now = datetime.now()
    if date_str != now.strftime("%Y%m%d"):
        if date_str < now.strftime("%Y%m%d"):
            return -99999
        return 99999
    ps = ct.split(":")
    try:
        tgt = int(ps[0]) * 60 + int(ps[1])
    except Exception:
        return None
    cur = now.hour * 60 + now.minute
    return tgt - cur


def _fl_is_closed(date_str, post):
    """そのレースの締切を過ぎているか。判定できないときは False。"""
    ct = _fl_close_time(post)
    if not ct:
        return False
    now = datetime.now()
    if date_str != now.strftime("%Y%m%d"):
        return date_str < now.strftime("%Y%m%d")
    return now.strftime("%H:%M") >= ct


def _fl_load_log():
    """fixed_logic_log.jsonl を {(date,venue,rno): rec} で返す。"""
    out = {}
    if not os.path.exists(FL_LOG_PATH):
        return out
    try:
        f = open(FL_LOG_PATH, encoding="utf-8")
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
            rec = _fl_migrate(rec)
            key = (rec.get("date", ""), rec.get("venue", ""),
                   str(rec.get("rno", "")))
            out[key] = rec
    finally:
        f.close()
    return out


def _fl_save_log(recmap):
    """{(date,venue,rno): rec} を1レース1行で全書き出し(上書き)。"""
    dirp = os.path.dirname(FL_LOG_PATH)
    if dirp and not os.path.isdir(dirp):
        try:
            os.makedirs(dirp)
        except Exception:
            pass
    tmp = FL_LOG_PATH + ".tmp"
    g = open(tmp, "w", encoding="utf-8")
    try:
        skeys = sorted(recmap, key=lambda k: (k[0], k[1], str(k[2])))
        for key in skeys:
            g.write(json.dumps(recmap[key], ensure_ascii=False) + "\n")
    finally:
        g.close()
    if os.path.exists(FL_LOG_PATH):
        os.remove(FL_LOG_PATH)
    os.rename(tmp, FL_LOG_PATH)


def _fl_analyze_day(date_str, fetch_odds=True, force_odds=False,
                    only_upcoming=False, window_min=None):
    """指定日の全レースで確定ロジックの中間データを計算しログにupsert。
    rec = {date,venue,rno,axis,anti,q,pk,pm,fav,is_notfav,actual2,payout,ts}
    返り値: そのレコード一覧。"""
    d = get_dicts()
    vhd = d.get("venue_home_dir")
    bd = d.get("bank_data")
    try:
        races = pt.load_cache(date_str)
    except Exception:
        races = []
    recmap = _fl_load_log()
    out = []
    if not races:
        return out
    for race in races:
        venue = race.get("place", "")
        rno = race.get("race_no", "")
        try:
            payload = build_race_payload(race, vhd, bd)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        hdr = payload.get("header") or {}
        players = hdr.get("players", []) or []
        if len(players) != 7:
            continue
        ax, pt_map = _fl_build_axes_patterns(players)
        axis = ax.get("r1")
        if axis is None:
            continue
        anti = _fl_anti(players)
        if anti is None:
            continue
        key = (date_str, venue, str(rno))
        rec = recmap.get(key, {})
        rec["date"] = date_str
        rec["venue"] = venue
        rec["rno"] = rno
        rec["ax"] = ax
        rec["pt"] = pt_map
        rec["axis"] = axis                 # 旧形式互換 (rawscore1位)
        rec["pk"] = pt_map.get("r1k", [])  # 旧形式互換
        rec["pm"] = pt_map.get("r1m", [])  # 旧形式互換
        rec["anti"] = round(anti, 4)
        rec["q"] = _fl_assign_q(anti)
        rec["post"] = str(race.get("post_time", "") or "")
        rec["ts"] = int(datetime.now().timestamp())
        rec["close"] = _fl_close_time(rec.get("post", ""))
        closed = _fl_is_closed(date_str, rec.get("post", ""))
        rec["closed"] = bool(closed)
        # 人気1位(オッズ)。force_odds のときは結果未確定のレースを取り直す。
        #   発売中しかオッズは取れないので、当日は繰り返し押して更新する運用。
        #   ★キャッシュしているのはURLの部品(開催初日)だけで、
        #     オッズの値は未確定レースなら毎回取り直せる。
        need_odds = rec.get("fav") is None
        if force_odds and not rec.get("actual2"):
            need_odds = True
        if only_upcoming and closed:
            need_odds = False
        # 締切がまだ先すぎるレースは発売前でオッズが空。取りにいかない。
        wm = FL_ODDS_WINDOW_MIN if window_min is None else window_min
        if need_odds and wm and only_upcoming:
            mtc = _fl_min_to_close(date_str, rec.get("post", ""))
            if mtc is not None and mtc > wm:
                need_odds = False
        if fetch_odds and need_odds:
            odds = {}
            try:
                ores = fetch_gamboo_exacta_odds(venue, date_str, rno,
                                                force=force_odds)
                if ores and ores.get("ok"):
                    odds = ores.get("odds", {}) or {}
            except Exception:
                odds = {}
            if odds:
                fav = _fl_popular_axis(odds)
                rec["fav"] = fav
                rec["is_notfav"] = bool(fav is not None and fav != axis)
                # 買い目ごとのオッズを保存 (8パターン分の 軸→相手 のみ)
                od = {}
                for _pk in FL_PATTERNS:
                    _ab = (rec.get("ax") or {}).get(_pk[:-1])
                    if _ab is None:
                        continue
                    for _pb in (rec.get("pt") or {}).get(_pk, []):
                        _k = str(_ab) + "-" + str(_pb)
                        if _k in odds and _k not in od:
                            try:
                                od[_k] = float(odds[_k])
                            except Exception:
                                pass
                rec["odds"] = od
                rec["odds_ts"] = int(datetime.now().timestamp())
        # 結果照合(DB優先)。未確定なら後日の再実行で埋まる。
        if not rec.get("actual2"):
            try:
                rres = RESULTS.get_result(venue, date_str, rno,
                                          allow_scrape=False)
                if rres.get("has_result"):
                    tri = rres.get("trifecta", "") or ""
                    if tri and tri.count("-") == 2:
                        ps = tri.split("-")
                        rec["actual2"] = ps[0] + "-" + ps[1]
                    r2raw = rres.get("refund_2t_raw", "")
                    if r2raw:
                        _c, _y = _parse_refund(r2raw)
                        rec["payout"] = _y
            except Exception:
                pass
        recmap[key] = rec
        out.append(rec)
    _fl_save_log(recmap)
    return out


def _fl_same_bike(a, b):
    """車番の一致判定 (int/str混在に耐える)。"""
    if a is None or b is None:
        return False
    return str(a).strip() == str(b).strip()


def _fl_eligible(rec, pkey, fav_mode):
    """人気薄フィルタの判定。
    fav_mode: "0"=フィルタなし / "self"=各パターンの軸自身 / それ以外=共通(rawscore1位)。"""
    if fav_mode == "0":
        return True
    fav = rec.get("fav")
    if fav is None:
        return False        # 人気1位が取れていないRは対象外
    ax = rec.get("ax") or {}
    if fav_mode == "self":
        bk = ax.get(pkey[:-1])
    else:
        bk = ax.get("r1")
    if bk is None:
        return False
    return not _fl_same_bike(bk, fav)


def _fl_matrix(recs, fav_mode):
    """recs から パターン別 × Q1-5 × 1-6点 の集計。
    結果未確定(actual2空)は集計外。
    返り値: {"pat": {pkey:{q:{k:{hit,tgt,pay,hr,roi}}}}, "qn": {pkey:{q:count}}}
    ※複数パターンの合算は「別々に賭ける」前提なので、呼び出し側でセルを
      単純加算すればよい(stakeも加算になる)。"""
    mat = {}
    qn = {}
    for pkey in FL_PATTERNS:
        mat[pkey] = {}
        qn[pkey] = {}
    for rec in recs:
        actual2 = rec.get("actual2", "")
        if not actual2 or "-" not in actual2:
            continue
        q = rec.get("q")
        if q is None:
            continue
        parts = actual2.split("-")
        lead = parts[0]
        second = parts[1]
        payout = rec.get("payout", 0) or 0
        ax = rec.get("ax") or {}
        pt_map = rec.get("pt") or {}
        for pkey in FL_PATTERNS:
            partners = pt_map.get(pkey)
            if not partners:
                continue
            abike = ax.get(pkey[:-1])
            if abike is None:
                continue
            if not _fl_eligible(rec, pkey, fav_mode):
                continue
            qn[pkey][q] = qn[pkey].get(q, 0) + 1
            lead_ok = _fl_same_bike(abike, lead)
            if q not in mat[pkey]:
                mat[pkey][q] = {}
            k = 1
            while k <= 6:
                topk = partners[:k]
                hit = 0
                if lead_ok:
                    for tb in topk:
                        if _fl_same_bike(tb, second):
                            hit = 1
                cell = mat[pkey][q].get(k)
                if cell is None:
                    cell = {"hit": 0, "tgt": 0, "pay": 0}
                    mat[pkey][q][k] = cell
                cell["tgt"] = cell["tgt"] + 1
                if hit:
                    cell["hit"] = cell["hit"] + 1
                    cell["pay"] = cell["pay"] + payout
                k = k + 1
    # 各セルに的中率(hr)・回収率(roi)を付与。k点=1レースk*100円。
    for pkey in mat:
        for q in mat[pkey]:
            for k in mat[pkey][q]:
                cell = mat[pkey][q][k]
                tgt = cell["tgt"]
                stake = k * 100 * tgt
                cell["hr"] = round(100.0 * cell["hit"] / tgt, 1) if tgt else 0.0
                cell["roi"] = round(100.0 * cell["pay"] / stake, 1) if stake else 0.0
    return {"pat": mat, "qn": qn}


def _fl_race_row(rec, fav_mode):
    """レース1件の表示用dict。パターンごとの対象可否も返す。"""
    elig = {}
    for pkey in FL_PATTERNS:
        elig[pkey] = bool(_fl_eligible(rec, pkey, fav_mode))
    return {
        "venue": rec.get("venue"), "rno": rec.get("rno"),
        "q": rec.get("q"), "anti": rec.get("anti"),
        "ax": rec.get("ax") or {}, "pt": rec.get("pt") or {},
        "fav": rec.get("fav"), "elig": elig,
        "odds": rec.get("odds") or {}, "odds_ts": rec.get("odds_ts", 0),
        "post": rec.get("post", ""), "close": rec.get("close", ""),
        "closed": bool(rec.get("closed")),
        "actual2": rec.get("actual2", ""), "payout": rec.get("payout", 0),
    }


def _fl_any_elig(rec, fav_mode):
    """どれか1パターンでも対象になるか。"""
    for pkey in FL_PATTERNS:
        if _fl_eligible(rec, pkey, fav_mode):
            return True
    return False


def _fl_venue_list(recs):
    """recs に含まれる会場を件数つきで返す(会場名の昇順)。"""
    cnt = {}
    for rec in recs:
        v = rec.get("venue", "")
        if v:
            cnt[v] = cnt.get(v, 0) + 1
    out = []
    for v in sorted(cnt.keys()):
        out.append({"v": v, "n": cnt[v]})
    return out


def _fl_day_list(recs):
    """recs に含まれる日付を件数つきで返す(新しい順)。"""
    cnt = {}
    for rec in recs:
        d = rec.get("date", "")
        if d:
            cnt[d] = cnt.get(d, 0) + 1
    out = []
    for d in sorted(cnt.keys(), reverse=True):
        out.append({"d": d, "n": cnt[d]})
    return out


def _fl_filter(recs, venue, day):
    """会場・日付での絞り込み。空文字は絞り込まない。"""
    out = []
    for rec in recs:
        if venue and rec.get("venue", "") != venue:
            continue
        if day and rec.get("date", "") != day:
            continue
        out.append(rec)
    return out


@app.route("/api/fl_run")
def api_fl_run():
    """確定ロジック: 指定日(既定=今日)を集計しログに記録。
    fav: 1=共通 / self=軸自身 / 0=なし。venue: 空=全会場。"""
    date_str = request.args.get("date", "") or datetime.now().strftime("%Y%m%d")
    fav_mode = request.args.get("fav", "1")
    venue = request.args.get("venue", "")
    refresh = request.args.get("refresh", "") == "1"
    upcoming = request.args.get("upcoming", "1") == "1"
    try:
        window_min = int(request.args.get("window", str(FL_ODDS_WINDOW_MIN)))
    except Exception:
        window_min = FL_ODDS_WINDOW_MIN
    recs_all = _fl_analyze_day(date_str, fetch_odds=True, force_odds=refresh,
                               only_upcoming=upcoming, window_min=window_min)
    venues = _fl_venue_list(recs_all)
    recs = _fl_filter(recs_all, venue, "")
    mat = _fl_matrix(recs, fav_mode)
    races_out = []
    for rec in recs:
        if not _fl_any_elig(rec, fav_mode):
            continue
        races_out.append(_fl_race_row(rec, fav_mode))
    today = datetime.now().strftime("%Y%m%d")
    return jsonify({"ok": True, "date": date_str, "fav_mode": fav_mode,
                    "venue": venue, "venues": venues, "refresh": refresh,
                    "upcoming": upcoming, "close_min": FL_CLOSE_MIN,
                    "window_min": window_min,
                    "is_today": bool(date_str == today),
                    "now": datetime.now().strftime("%H:%M:%S"),
                    "patterns": list(FL_PATTERNS), "labels": FL_PATTERN_LABELS,
                    "n_race": len(recs), "n_target": len(races_out),
                    "matrix": mat, "races": races_out})


@app.route("/api/fl_stats")
def api_fl_stats():
    """確定ロジック: 蓄積ログの累積実績。
    day を指定するとその日だけを集計し、買い目一覧も返す。"""
    fav_mode = request.args.get("fav", "1")
    venue = request.args.get("venue", "")
    day = request.args.get("day", "")
    start = request.args.get("start", "")
    end = request.args.get("end", "")
    recmap = _fl_load_log()
    pool = []
    for key in recmap:
        rec = recmap[key]
        ds = rec.get("date", "")
        if start and ds < start:
            continue
        if end and ds > end:
            continue
        pool.append(rec)
    days = _fl_day_list(pool)
    venues = _fl_venue_list(pool)
    recs = _fl_filter(pool, venue, day)
    mat = _fl_matrix(recs, fav_mode)
    dates = {}
    for rec in recs:
        if rec.get("actual2"):
            dates[rec.get("date", "")] = True
    races_out = []
    if day:
        rows = sorted(recs, key=lambda r: (r.get("venue", ""),
                                           _fl_num(r.get("rno")) or 0))
        for rec in rows:
            if not _fl_any_elig(rec, fav_mode):
                continue
            races_out.append(_fl_race_row(rec, fav_mode))
    return jsonify({"ok": True, "fav_mode": fav_mode,
                    "venue": venue, "venues": venues, "day": day, "days": days,
                    "patterns": list(FL_PATTERNS), "labels": FL_PATTERN_LABELS,
                    "n_days": len(dates), "n_race": len(recs),
                    "matrix": mat, "races": races_out})


@app.route("/")
def index():
    html = INDEX_HTML.replace("__MARIA_B64__", MARIA_B64)
    return Response(html, mimetype="text/html")


# ============================================================
# 診断: 穴ラベルがなぜ出ないかを確認する
#   /api/diag           … ファイル状況とJSON先頭サンプル
#   /api/diag?date=YYYYMMDD&venue=弥彦 … その会場の全選手の判定結果
# ============================================================
@app.route("/api/diag")
def api_diag():
    out = {"ana_available": ANA is not None and getattr(ANA, "available", False)}
    if ANA is None:
        out["error"] = "ANA 初期化に失敗 (ana_marker import 失敗)"
        return jsonify(out)

    out["save_dir"] = getattr(pt, "SAVE_DIR", "?")
    out["global_path"] = ANA.path_global
    out["global_exists"] = os.path.exists(ANA.path_global)
    out["global_loaded"] = len(ANA.ana_global)
    out["venue_path"] = ANA.path_venue
    out["venue_exists"] = os.path.exists(ANA.path_venue)
    out["venue_loaded"] = len(ANA.ana_venue)

    # 実ファイルの先頭1行を生で見る (キー名確認用)
    def head_line(path):
        if not os.path.exists(path):
            return None
        f = open(path, "r", encoding="utf-8")
        ln = f.readline().strip()
        f.close()
        try:
            obj = json.loads(ln)
            return {"keys": sorted(list(obj.keys())), "sample": obj}
        except Exception:
            return {"raw": ln[:300]}
    out["global_head"] = head_line(ANA.path_global)
    out["venue_head"] = head_line(ANA.path_venue)

    # 読み込んだ辞書から先頭3名のスコア分布
    sample_g = []
    cnt = 0
    for nm in ANA.ana_global:
        rec = ANA.ana_global[nm]
        sample_g.append({
            "name": nm,
            "ana_score": rec.get("ana_score"),
            "in_band_starts": rec.get("in_band_starts"),
        })
        cnt = cnt + 1
        if cnt >= 5:
            break
    out["global_sample"] = sample_g

    # 特定会場の全選手を判定にかける
    date_str = request.args.get("date", "").strip()
    venue = request.args.get("venue", "").strip()
    if date_str and venue:
        _races, rmap = load_races(date_str)
        judged = []
        for k in rmap:
            r = rmap[k]
            if r.get("place", "") != venue:
                continue
            players = r.get("players", {})
            if not isinstance(players, dict):
                continue
            for bs in players:
                pdata = players[bs]
                if not isinstance(pdata, dict):
                    continue
                info = pt.parse_full_info(pdata.get("full_info", ""))
                lab = ANA.judge(info["name"], venue)
                if lab["kind"]:
                    judged.append({
                        "race": r.get("race_no"),
                        "bike": bs,
                        "name": info["name"],
                        "kind": lab["kind"],
                        "hit": lab["hit"],
                        "den": lab["den"],
                    })
        out["venue_query"] = {"venue": venue, "labeled_count": len(judged), "labeled": judged[:30]}

    # 結果取得の診断
    if date_str and venue and RESULTS is not None:
        _races, rmap = load_races(date_str)
        res_diag = []
        for k in rmap:
            r = rmap[k]
            if r.get("place", "") != venue:
                continue
            rno = r.get("race_no")
            rid = RESULTS.race_id_for(venue, date_str, rno)
            rec = RESULTS._db_lookup(rid) if rid else None
            res = RESULTS.get_result(venue, date_str, rno, allow_scrape=False)
            res_diag.append({
                "race_no": rno,
                "race_id": rid,
                "db_hit": rec is not None,
                "db_has_result_field": (rec is not None and bool(rec.get("result"))),
                "db_refund_3t": (rec.get("refund_3t") if rec else None),
                "has_result": res.get("has_result"),
                "trifecta": res.get("trifecta", ""),
                "source": res.get("source", ""),
            })
        res_diag.sort(key=lambda x: (x["race_no"] is None, x["race_no"]))
        out["result_query"] = {
            "venue": venue,
            "with_result": len([x for x in res_diag if x["has_result"]]),
            "races": res_diag,
        }

    # 払戻スクレイピングの直接テスト (1レース指定: &test_refund_r=10)
    test_r = request.args.get("test_refund_r", "").strip()
    if date_str and venue and test_r and RESULTS is not None and RESULTS._scraper is not None:
        from datetime import datetime as _dt, timedelta as _td
        code = result_provider.NAME_TO_CODE.get(venue) if hasattr(result_provider, "NAME_TO_CODE") else RESULTS.race_id_for(venue, date_str, test_r)
        # race_id_for から code を逆算
        rid = RESULTS.race_id_for(venue, date_str, test_r)
        code2 = rid[:2] if rid else None
        try:
            actual_dt = _dt.strptime(date_str, "%Y%m%d")
        except Exception:
            actual_dt = None
        ref_tests = []
        if code2 and actual_dt:
            for diff_days in range(0, 4):
                base_dt = actual_dt - _td(days=diff_days)
                base_date_str = base_dt.strftime("%Y%m%d")
                day = diff_days + 1
                gamboo_url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/result/"
                    + code2 + base_date_str + "/"
                    + code2 + base_date_str + str(day).zfill(2) + "00/"
                    + str(test_r).zfill(2) + "/")
                try:
                    st, html = RESULTS._scraper.fetch_with_retry(gamboo_url)
                except Exception as e:
                    ref_tests.append({"day": day, "url": gamboo_url, "error": str(e)[:80]})
                    continue
                has3 = "3連" in (html or "")
                _r3x, _r2x = _parse_refund_html(html or "")
                ref_tests.append({
                    "day": day,
                    "status": st,
                    "html_len": len(html or ""),
                    "has_3ren_text": has3,
                    "matched": _r3x if _r3x else None,
                })
                if _r3x:
                    break
        out["refund_test"] = {"race_no": test_r, "tests": ref_tests}
    return jsonify(out)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>KEIRIN ORACLE</title>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+JP:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* ===== ダーク(託宣)モード = 既定の暗い世界観 ===== */
  :root{
    --bg:#12150f;
    --bg2:#171b12;
    --card:#1b1f15;
    --card2:#232818;
    --line:#3a3a28;
    --gold:#d9a25e;
    --gold-dim:#9a7340;
    --gold-strong:#e6b46b;
    --grad-a:#d9a25e;
    --grad-b:#caa45a;
    --txt:#e4dcc7;
    --txt-dim:#a39c86;
    --red:#df4338;
    --blue:#6f9fd8;
    --green:#7bb265;
    --wt:#7bb265;
    --r:14px;
    --bgimg:url('data:image/jpeg;base64,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');
    --bgimg-light:url('data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAwICQoJBwwKCQoNDAwOER0TERAQESMZGxUdKiUsKyklKCguNEI4LjE/MigoOk46P0RHSktKLTdRV1FIVkJJSkf/2wBDAQwNDREPESITEyJHMCgwR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0f/wAARCAP1AvgDASIAAhEBAxEB/8QAGgAAAwEBAQEAAAAAAAAAAAAAAQIDAAQFBv/EADwQAAICAQMDAwIEBgICAgIABwECABEDEiExBEFREyJhcYEFMpGhFCNCscHRUuHw8TNiFSQGckNTgpKi/8QAGAEBAQEBAQAAAAAAAAAAAAAAAAECAwT/xAAjEQEBAQADAQEAAgMBAQEAAAAAARECITESQQNRImFxEzJC/9oADAMBAAIRAxEAPwD74RhBCJyaMu0shkRHUyC1bQiKDDAcTQTCdJUYwERoJLAJpoZkaaaYSgzTTTYFTVDNJgBEBEaapLxCVNGqCYswCCNBMqwhEWESwNBVQzGaqMDEYckRpjvJfFRhAuHSZuO8jQVNUcBau7iHnaDQmhmqBotxotQNNU1QwBU1QzQBU0M0ihU1QzQhCIKlIjSqXTAdtpiYp3hCmAxjFMqBNDBKNBDBCNNNAYAuOp33iTXAsHokdpRMjHvOW4QxHBjF13jIF/MZQMCJ5hyMeTHTMw2uTKdO5ztOfJvGV/b7yAYmZ9I23mViZEUrM2Qab7yRY+ZrE1TTcIxySsQdp0pkpgGG5irAXEY4wkToxkGORMmooKjPVQMN4pBkE2W4hxy3EGoCVXO2KI2KdJyCTdwY7HM2OTKzqoNFIA5mkc+iYYrM68eIPuJYYABxJpjgbCewiHEeJ6LJQqRdIlXHnslSZWdjpIss1KzjlZYpEuyyZEqJERSJQiKRKiZEUyhikShIIxEEqEM0JmgfWVDDNU5NgI6wQiEVXiMIixxAaYTQzcRppppQJoYJMGmmmkBmgmmtBmguG5dGmmmjRpppoAIgjQTFgE0M0zgEM001gBMFzGaYVjsOYqCzH2IqZRUuGlcb3FAuVIuAACLOzU+Joz1cWRWgMMEDTTQGRWmuaCUGaCaRRguaCAYCJo0CZWIRLGIZRMiKZQxSJUIZoSIJUCCNBAEEJgMIUzQwSgTXMYDCNc1wGCUNrJ7w69uZOaA1zAWdotwg1AquI6q8d50pjT06Y79zOLWfMZMhGxOxmbK1K9HG+NVoGVsEbTx9ZDWDLYeqK0DvJlR6BA5i2InrKy8zlyn3Wh/eTFdTmhYFznOQ17kKyS9S6nfeO+XJlx2uIkealzDU3y0aElZLd4vezzK4nUAk7zXiHXG4YVvOo4EYWRvOXHnFnv33lseUhSX4+Ji61FMGMoaNVLggzz36xSa3HzGwdRvsb+sZTp2OJDIJuo6pMeO7nFj64tkpxaySU3D5BIMJ1ZNJFicrkXNQqTDeTYShMBmkRIilZYiDRcajnIikTobGRJ5Fpbl0xExTMQSYDNMgZpjNA+umjVBU5NsBGAmAhAgERhBUIhDQzTCaiNDNNNQaaaaUCaGaTAJpppBppppBppppRoYIZYNARDNLZoAmMMBmbMAmmmmBhNNNKNUImMEeBoDMDMZr0TN3NVcx4rA6vrOdi6WaOFOkxIVoCIYJFCDvGqCAJoYJVaaCaQNcFzTQNUUiNAYCEQGMYDKhCItR4JQtQVGMWVAMBhgMIUwQwSgQGNAYCmCNFMqBNDNAEENTQBBcMEAXDc1QVAYMRwYyanYIO5iVL9E4x9QC3B2ijrboUYjcj/Muqrjx6RwI6kMoI4MllBnOrHPmXAbtKJ7iednUY2AD6vtO/IpM5XxMuxX821nma4rXLqMb1GI3JisKNQTowzG4VyMnBimCEHLkLmz+kXHs2rsICIN+IHU/UK1KgPE5yzayLqLuOOYVNMGO8mYu66NDaeJMhh+YVOrBlTL+bYy7dOjrtMbnrea8r1KPE6MBDmpsvSlWNGhJgaW0qRfmXqncdb4l8zj6gaeBM+VlfkmTyuz8VJJS2OZibsxCZXKOBzUiROjATQTSo+1qAiU0wVODZQIRDU1QrCGYRpUaYTTTUQZpppsaaaaBpppoGghmkoE000g0wmmgGaaaWDTTTSjTQXNcmjVNDc0mBZoZpnAIZppRpoZprAh5hBvmEiJ3nO9VVIpq/rADDpvky7qJzRmXfaArQuZxosxhgkAmhghQmhmhQmhmgCCEwQARFIjGAyhSII0EIUxTHMUyhJjCYJUAxY8BEIQwRoDKFgjQVCFmjVBUoEENTQBUdcORvyoT9p1dFgDEs6WBxc7WNDxM2q8UqQaIIlMOE5n0ggfWd/opkctk93iVx48eP8gA+Y0xzr0CAHUxJ7GTXoyMp5Za+k7zALEm0DECEAIr4mcXMWqD1BJaFKip5vWltd7iuPmd7ZALk86Ll6c8XzfiJ008g/MUiVZCBdd6iETs5kMEYzFdt4QFNcS+DEciFUWyeTIATu6AleFvVz8TPLxrj65jjOFzfbwZMKrXQA+pnf1mDVuABe9ziGBlY6vtJLq2HGIolnc/BnRizqq72PrOUuygi4Meln952izSXF8+dTtVzlZ1vYGVfGpUkNvOVgREkLaVzcmeblNJO8UqZtkhBJilT3Er+XiYmxRgc5E0o3iaVH2m0FRATcoNxOW601QVGmmvkAQzTSyDTTTS4NNNNA0000DTTTQNNNNA0EM0gE000g0MEMQaaaaaAmMxmmBqmE00AzQXDNdDQQzRg0000AGKY1xTMciFjC6gqETMUDzNRCxoGs/SXAk000itBDNIBNDU1QBU1QzQFIgqPARCkqCPUBEKSoCI1QVKFIimPUBEImYI5EBEoSYiNUFSoQiCpUITwCYChHIqBOoKl0ws96RxHXpHPJAlRy1NpJNAGdeHBqf3C1HnvOqlUAADaTR5BBHIqYCzPRzKuQFSovsZBemYi/mNMdWHIrDSDZURnFxcWIYhtuZTkSCarvHCiECY7Qa3EBYRTkrkSTPvGrIOTIJzPk3jOZFhEVXHkUgq3fvOfKzC1B2JhAI3ga2NmVElJDCDOih6U3tuZ0JixsDrNSWYAOa4l3tM6c9bwEV2uUIiGaZbGAWAYUPidgzrjH8tfvOTvc1yWasuOrOS+MPc5C7cEyiudOk8QPjFWDJOlvaLHbeLcZliETTKg34iEarhQ6bmXcwrKjBeNjFdbHFGdQY6O0hku9pmK5iIB7d6B+ssR8RCoM2yiZo5QzQPqxHU1AFmqcWz3DEhE39Mmmg5hmpdGmmmlGmmmgaaaaBpppoGmmmgaaaaBoIZpAJoYJBpppoGmmmkGmmmgaa4Jo0NcFwTRoIhgmuWA1FqNNFgWpoZpnBoKqGC4o3e6gbftDc3MKATzCVHaEQmXJiaVRR3ENDxF3B2jxACAYhFRjNRkvalAubQY4AEMs4mokQES1C4ujf4mfmrqJEFShWjUFTK6SoKj1BUqk0yiYARbfpCqnkS3aWTWbUHwWfYKjJgVR7hZlDBZjqJtEUNhQiuL2AmC2d442l9AVQq0BASY0EVCrfeEi4bilplShPdZlYoYVCCDxLCtc0RyQYA9ckGNMVgJ2gLgczbMNotEnEkZ0FZNlmWog0mRLMsmwlCkbRCI8R2oyhCaiNuZQ79riFTe4qajNTgIjkRSJUJUFR6glAG0dWFUYh4iyCrKtWeJFgNXxG1GDmIUpEwFHeE3DjXU1E1KG1EjbiIaMOQaGIBuLYq73kXSsu0TgxywHeKxB4lQDvNBqAHFzQj6yotQqSeYTUzkvahU0YTVcfIAhmmmpOhoIYIoM0EMg00000NNNNA0000DTTTQNNNNA0000gE000g0000g0000ATQwSDTTTQNCIJpQ0EE0aDNMJrlGgIhuaShamjTSYADDNDUs0CoZppqQCGaaAJpppmjTRTNM6M3MWox3gBkqhU2mG4QYXSAESimxNUIFTUiWgRBGmixAFwzQR4CJqmmlCkRSY5iMtzFWAdxtFsji4yDeMQK3kXUHysJEsblcoF7SJE1FURgdmMti2Ox2nIDUomQrxGDsMk9jeR9ZtUsMgbHuJEiDPFu4W5iWRNYaapLKpuUBuWXFabj6SeL6bpcSrjDckzZ8YcEEb+ZXEunGBVQFbMiOfH0i76zcJ6fCm+m/rOgrQkMpjascf8ADs+UqlAfPac7qVYqeRO05CN6JrxOTIdeRm4szc1mpGCORFImmSGaEiCoBglESzMV3jVRMUid/wDB3iDKSSRxIP02RVvTJOUPmuaoKl05qv2ljgRgBwZdM1wETTqy9NpPtuvmaNiZX0K7QxLjKZyjVGETVMBNyWVBguGAzVBmghjQIZppBppppoaaaaBpppoGmmmgaaaaBppppKNBDBINNNNA0000g0000ATQzQBNNNA0M00DTGCaBoeIIYBggh2lBE0E1yygzTTQNc0E0gMBmmgaCoZpmwAQECGYyKUioI0xEDKYbiwxoaGAGGbiFmhgmaDEYmNARJVhQxj9opEXUQZNU2wO0VowIMJEiIEeZNlnQVuD09pWtcpSEKZZkqACXQiqL3jNXaGqg2gTKyZnRpsSRXeWJWxLbjedwAnGFre51YmtaJsyVDwGYxC8hIzMRtOd5Y7xCpMjcQFXuIB06Ekse/aUK1zCHA7S6mJr02PXZNjsIOp6YMQcYA+JZSCdxKkAiNp048fSACshsfAks/ShRqxm/gzqy2BzOZmbgmWWlkQS1PFjxLjGHo6CPiVB04/N9oEzY/pFpI6EFJUhmB7GVGVau9oNmvxMtOL3FuBfmXTGAoJG8dsQmohaltSRLIVBmgdCeZpYV6dRhNUIEknbGiIYCYLnTcQ00HaYS6NNDBIDNNNLBppppRpppoGmmmgaaaaBpppoGmmmgaaaaQCaaaQaaaaBppppBpppoGmmmgaaaaAJoZoAmhmgCaGaBpppoGmhmmsAmmmmRoYBDNQCaYzTNAmhgkGmmmkGqaGQ6jOEWkPuP7Si8081OrzBwS1jwZ24c6ZR7Tv3EumKTTXATMq1w3BDIFuarmIgBhRAowwVcNQgzQGbkQEdhFUiHIpk+JcU7AVJVtH1QMYgUGoC0xMEqMTcfFYYHsYuna50CtN9oqmLDvIlhq+PMyjXk+JYoKoCZPCLvxCdoVUKKEzC6kEtNxWxyxscRS1QuohSp3lgdoGdSJD1qbcUJc0PkWxOdkA3nSrq/BiugI4idK5HJI2NVIUQfE70xLvqFxeo0aAtWBNSs2OVGJ9tzoxO4Oll2HeFVxMF9tETpAWtpm1ZCjeKxqDKxTgSXqkniTF0cj7bCaSCsxNmppoespuOJIAiZnPETljGHNXNFG4hB7GTdBEMEM3EGCaGa9GmuaCL0DNBDGjTTTSjTTTQNNNNA0000DTTTQNNNNIBNDBINNNNA0000DTTTSDTTTQNNNNA0000DTTTQNNNNA0000AzQQzXo000xgaaCaNBgM0MnoE00MmAVNUMBIAJPAlwTztox2DRM8/lgLoAWSJ0Zsut0sV3nOFOxO6k2fpMNRJ9m8QKSDYNH4jZKPuU3f7RJR6mDOuZdvzDmWqeZ0bFeoWjs2xnpxiNU00MiFImqNNGLpDYMwaMRFIkDcxbo1ALmMBpNlEYGba4VIrUAlGHiTM0FIm4jRTKjFoSfbsYswkF8RpLO0orXOcvttxHxMOJFUJg7wsLkiSDMkO11ckWBNNKEkiQdTzLFBl3sdpDIxJoy6tW0lmFgESwpceQoZ0ZculAQOZxwsxOxmrNZldIyWmoipJkORrjYz6hCniXVFXgTHjXrnbFoFjmDG+U8D9ZfIwBqompQlrtGrgk2N4qKoa5E596MKZAxoGMNUegJomRCR+aaIPRdxVCSveAGHtMmYohjkSScXKg2IjNEGxGkySvEIe+Z0nKJhqmmuaVGuaaaBpppoBmghmpRppppRpppoGmmmgaaaaBppppBpppoAmhmkwCGaaWDQQzQBNNDIBNDBA0000DTTTQNNNNINNNNA0000o0ME0DGaaGAJoZoGmmmlGuc/U5tI0gfWXnFmOoMfNzNqxPNWgHsBEUkqdOxqhCx/k/U7QhAMQDHmyfrMtOY7Mam7zOKYiwa7iAG5pk6MUKsOQbnrY8i5EDKQQZ4177zt6HMij02JsttA7oYIZEaCaaNGua4CaikyauCRcBFQg3zCRAWKTCdophQJgMxglGikRoJUKYI0EDRlI22mTTw0fTjrmvmSkUDAjaIVLGIDRpWuOpbuJlTVS1JPtKni5FzIsTIsbbQf0kGAmLq3m8NIwiy1AwadjQs/2l1nC4mKn4nSxZk9nMTHhFWx57SjKijb9pm2NRFMjk6WQbd5PI4Cmxue0qxqt7EiQXuhZ8QOc7tOrAuqtQ/LxG6fAACzjfsJYMqkgCotJE3rUBXM0pqE0ypQZRTtIA1HVqMiq6qFQh6MmTc1wY6L1CLwYMTb0THYeIY8FTvHklMoDvN8alGaaaaRpppoGmmmlBmmmlGmmmlGmmmgaaaaBppppBpppoGmmmlGmmmgaaaaMGmmguTwGaCa40aaGCQaaGaMAmhggaaaaBpppoGmmmgaaaaAZpoLgLlYhdoEyWKO5iv7rMnxM6uKZcg0kAzlcXgvvGyGxX6zE1061ydpKqIGy8UDdmFwX6ZmPt3/WPSM6qDYHJ8yT5vZoApIiuZhtUHfnmM59xPF9onBmmRLXzMrGyb3vaK/N9oyQPawZPVwq/kb/WPc4vw/J7GTxvOvVvM0NFO0btFJ3ikKWi3GZZMxGl0NiI7GKrHtMwIFmEwwOofMYKKkQ9G5YEMNoCaSeIrKRyJTIdKznOUja7EDE7wzDS1UalQgUeTGiDMBAGBMGT80ndSjora4MrCgBIjKRFd9RjDRLRkzMNr2kCYLlxHeuUOKPMm5AajIJmZRQqpixyMLNSYuqNFIlRj9otriooa9+I0xMbGUVTqBHeOEUcxmyAbCTTCOGqQbIwly4MjkW9xEWkbJqWj9ocWUpsO8VRvMcZ1TXTPbpxvq2I3mbHZDbj4mxgY1u7MVs3mY/42BB7TR0dSu00CYM1xL2muRV1NwgySNRlDyDApdVLI2oTmMfG1GrkSxc1diZTFO8AMM4vNJhjY32lJ1l1mxpoZpQJoYIGhmmgaaaaUaaaaBppppRpppoGmmmkGmmmgaaaaUaaaaBjBDBM0CaaaYBuGLDNygzTTTQ0000g0EM0YBNNNINNNNA00M0AMaBMhqKuTHckuAO0mRbGRY2oGonfmo3YGAC5FJl2sfQRNJKkmgBsPiXcEqzVuDMEVU3HAuRXOoIwsK5GxiZEC4wDz/aUS0BJINDcfPMllZmxkk7DeBB9zvE5v5jtusS6E0yw5owoa2qAi1scwjdb8yimN2xOGU1vuJ6y0QCDsZ4xP+56fRZA+AL3TaZo6Cdot3CTAKmVFuJNoX2ijcyhk9ouK79o7rS/Eg3MoBMZMmkxQpJiHYyiztqFyDGHVUVjcYMGnQMoOPnechm1VFmoq7WdzcmZg18zQpYKjTSoQiCPUFQEhBqGoKhFFzELRFzDIQ9yYEdQO8irByV+JgQxqIGA2ks/UY8JUu1WaEmLrpKATj6nrMGByjsbA7C/t9ZEfi6gFWxNqHFGwZ5mXIcuUmwGssYkLXp9P+IYMzlTeM9tXBnWXBFqQQe4nzI5npfh3Ufy2xMdkGoEnapu8WZyei+U9pM5DcRHTIupG1DyJmqtuZMXTeqQdppEzRia7AZriiac3Q4Nyym0+Zzg1LY27RVPqsQg73JXRMYGQdKtCfMijbR1aZTFFN7SgfsZHgxie8suJYsreY0gDtLKbE6cbvTNgzTTTaNNNNA0000DTTTSjTTTSDTTTSjTTTQNNNNINNNNA0BhgkoBmuaAzFBmmhlnYExM0EmqNwxLhBjTDXMDBNLqGmi3Dc1ugzTQSgzTTQNNNMRcBCvJMjVHedFbVEZdpnxY562jVWk9uZTRv5EzJdGSqzkhLA3kS2rC9nk0D3qbJkYZaO1SOslwaJA7SYKFf5IHF/Hmc7EMpVAZ1ZmpCLHmchIGXc99yIiosKJBHEn2nTQZn2vmpznjaaZb47TXQoTHeA8G+8oYcC52/h7gO6k8i/0nAv5a7xtRq5LB67uqprJ9vmMtHvPHOZmRULe1eBOvpcw9IJfu32kxddrFfF/MmLU2IhfaZchG0YOkDUPdN6a+IEcETM8iFcKBtOZhvKuxuTJuaipkRZQxQtmpUKYpE606a1Jex8CTydO6i6sSaOeEGMyEc94pEoNzRYy7wBBLaPEmRAWaYiYQjTRqiuQqkngcwJtkVW0swB8EzzfxB9WUsDenb6SWZmz5Gat2bYyecVpobAUD5EsAxty7sNVe0QY7JYqNttzJjnYSnTrqJUsVB4lsQj7MaN0Ysz8mjY4+sTftKj0fw3IAzYid23E754aZGx5FdD7l3E6vwzM3rnExsPuPrGD0CJpQrNIpwZr8RAYbnJ1G946tRElGBgXY8GEH27yd2kwPEirI28oTtOcGWU2JKKK17Rwe0isoDZmQ4lENGS+YymWVmuiaIGtYdW06/UYw00XUY0sujTTTTQ0000DTTTQNNNNA0000DTTTQNAYYJmgTTTTA000EiiIRzBNLKg1FMMEUCpoYJFYGMIsIlBmmguA00UGMJqIMBhglo1wxSamB2uSUMYIuuZjUWmCdhJ5HoXdQZmPptRoiSynTRO+37yKnkBZzXuPmTRqyC96mL0bk3P8zbvKqpIORdJ4H6SeUDUK2sQ2FYeIcraiL5HeTO1/CoDodvAr6znYLZomu1ztxgjCWXdufpObKmmj57CJ6lR3Xabhtu8LCrreL25mkHg1MfEW6PzDewMIUGUxPoyAk8GSvcwXKPUDBlDDgwXvIdG9oynhd5bJpKgaqDdx4hSN1npsQouuZ1Yc65cQc8nn4nmdRi9PKwA9t0K8xcWZsTHSbB5HmTNNx67OvYD7xdaMdJq5wZM7lqx2K5kC7A2CQfMYa9c4r4s/MbHgYOCanJ0vWNoGreuRO5M6sLEl01U7STkg3cJyDzJM9yKnmFnVfMlLUG2JjL097sQB8TXiOYp3jY6BGobXLrg91FtvMb+GBf8AN7Y2CmlSA1XI5ChFFRU6SKWpyZRTTMhqTqLteIlSykVTCxCMJeyuw7XNeIhU4vxHNpX0a3YXd9o3W9XkwdQMaBW7G/M87N1DdQ5L7e3jsAOwj0ZW04XI4Y6fnj/1OazO3DpPQutHWWoHz3P9pxn6czUSlG5McKChIam7D/MVRsb77S2f00OMpk0llGoAbrFHITY33m+YDzXa+0A8SsnG4j9O4xdRjynhW3iLzXMPmUez0fWJ1TMmnSy7gXdiaeb+HZlw9WpatLe0k9ppMV7EFzXAZydRhUxe01wLIbFTcGTU7yp3kUQdpRG2kRHUyC17xwZIHaMpmWl1NiESanePIiqHeo0kDUpdiViwbhBMXmMTVSyoaxDJw3NzmYeaAGGb1GmmmMujTTXNGjTTTQNNNBJoME00lAmhgmaNNNNIBNDBCtDBDA1TVCJjLiBUEM0gBO0XmM28AEqsBHEW5riBpydTlbXpUkD4lsuUIm/J2E4crU5+NjvLpI6l6hWRdXPBlSdqnnqdp2K4dQRxFU30lK4MiGGrTe5ldQ4B3mSoZyOL5gzEBAK3M2VSch0i96mzEaWPgiByOK5iOCGF9xGPMm133m0Mx2ELGyIhI+8dRuTA6OnyD0ihFd7ks4VlIWgE2B/5RhXoMh2YHcd4uXG6YwCLFWTM/q3xzRGGxAjNtx/6gFN9TNMp9we0I4ImqtoByfmUYzVMYRvUDBtJ2NCu03qMDsxFTEcycqLjKH0jJuBZHyT5kyPcTwO1xRzvHBsEGMNYNQIrmArVG7uA87TX4gPgNZB87T0MJTHkvIwI7VPNGwlg1i4sHZn6kWdAsTmPUPqsVXiLuZlxszgBSb4jw3XTiy6xY2PidK5GAkekwachJFMD38S2XqMa2pFMJm9tRQZR3jLlTzPNzdSK9nMQdSbFjaPlNelkzDgSRy3+YWJBG1i1NynptV0ajJDRpSdiJHrHbD07Mhs8D7x+DOLrsuvJ6StQUWT2uKryclh6u9uYg3O+8bKffXYbCIvM3Ga68agZVxK9qVsnsCRvIuCGogbE8docL6chatgIrH3gjxJgzY6whyduAO5M5mDAAt33ncB/MTDlX2Yz7vJHM5Mx1Of8iJeyo1c0YbdpRgoAYABuedpUSvejxMrb13hUfH1mcVtxcIANfrNAeJpR9Bc0E1zi6iZoJoDLzLLuJERw1GpFOZlM3MwkVZYw2k0PaOZFUU3KA7SIMopkD3GRqNRDNciL3UFxVNiGoQwMwMW5gZUUEYGIp3jTUqDc0EM3qBDNBIDNcE0uhoJpo0aaaaBpppoAmhmkwCaoamqMAmhNTVGDCYwwGaoUwXGIi1MKIhqAbCYm5QGIAJ7Cc46j+Z7hS/2hyudejse852FvZ+plU+Zy5ajsBOY95T8rknntEYWbvmBr2EfHk9N/jxBjX3G78D6yeS6sbb+YFnyajYJ+srjznkj7ziV+3cx0Yg78czNix6Ki3vmhOfI38s6jyxP1jYM49MhuRx8wEWh+N5IOaL/mMYp3nRC6d6llBDDvUnjDeou9Fp0dO143Vt6NyWkRVdNtvYjZc1oxY7vwB2gfIdBA2q7qSZQFcMeK0/JkEzvFXY0IQfMDCvtNsg5s7d4K/wCoTuPHeC9vvAzc/MINWJjuD5ik738So39UWNFN7wMN/pD2qYcTHnaBh8TEcTbwjcQg32lukQZHKlgNr3kKN1HxsUyAwPRTF090ST+0pkyrjFKAOwkcB0jWd9trks+QFvzXJnbW9EOTQ+oXZNxcuY5G1GpJjZJiXNYzpibMx4gEtgI9UEgVAp0GQpnFi1bYzv6jqQntWr+O04cuQKx08fE4+ozkClYg/EmauvQ1qRbED6zzM7AesNQOsgX8bmP67ZcCdiCFs9yf+pDMgOs9sZCgebmf1fxxuDYPkAwLsbjPWs1ddrj9QpViicKav/c1qFxAliACZTpcQyZhqoAbmzW0hjd8anfY9oUysuogkkijJdIw3ygMdK2bkWyatgNKk3UtlxPiKpsxKhmHgntIla5H3lhQFbb/ABM5pRtcXnaMdxKywJP5YSSxHxzFsTLsf8iUYj95obviaB7c17RQZiZydRJ8TAxbMb+m5BrjAxAd5YYyFDEj6XCxVNxG07wdIBkzaG2viekvToszV1x4lZTq02PmWOIFdQG39p0uoC7CpDG+5VuDMkqPEpjFjfaI6hXonaUwjUd+IUYCI7AaiIpBkBUyg3Eisop7QlMRBDXcTEC5UFTGuJxMDCH1bxgQZKEGXTFJrgBuYy6gzQQyjQxYY1GmmmlBEMWESygzVNNNdDTTTXA00ExNSAwGC4ZN0CaoZowAwNspPxGqJlNAAd4xXI9kA9/MWq1EfS5Zxaj6SZU0RIqDEGvPeL2oxmFbxQLNSgsRjUGt6kFNHSdwZXIaU2N2nPuPiICTv9I6t37RW2o9+8DbfC/EqKq5BBEsrF0NnntORW2EomStgeZmxqVQneFwFABButzDgUO9MwA+DBl2c03B78xvYAUqlhqLbD4nR0wAxBSRd71OXNekE83vcXDmZDQPtPMl7g6Mv5gQo0WCa7+JLqTpYJYoCuN4fWUq7E/lFLX95HJuLIokd+8sSpsBQI2+PmIGJMcbn6RDyTNsqKFMTJs31hDbXNlFrfMIykEH6QHYn5gQwnc/WUCA2K+YVokgwHeARs1QkWTJ3TXKKbFyoUiMBtv3hqDVewgEHeobA5izFvjeBbG5K6fEIxO52BMTDk0tfxGbM+rY7QGzdO2Me8ruNqPM5yJ0k+ogPYwJi3smvtcDn3EZCQdp1dQwOMJRNDYxugwBn1uNhxJq45yp07/vPPyvbar1KbCjjbzPU/EMbLid2NWaE8vOdRXj8gr4j08Ljc+olDhhQ7SmcsmdwdiCLB8iRVijKy7EbiUyvr9xJLSfqhiwliSaAUXZ4+JX0AyrjZSXyPrY9gtdpZMRT8Mcld3cXfYDcT0CikKQPdWmh/xmbyakfO9UjLkBYEBhY2kkd8ZJRipIo13E7OtBf1CP6chJHgcTlCanCjcnYTc8YvrqxZGy+pkUe8IFvySNzObnccQ5VOHK+PUdjREmTVkcGJEoZEqiB9Yg4lvzId5E/m4r4lQNNkBd7ijxKq17nYwOoG4NwEokzQ0R9ZoHrjaETVCJzdBGx3mJvYCY87wsR2gBRvvK6qG0jqqbVIqytRBGxE9ZerT0FyMascfM8QEgR9Z87CSxXqjq1yg6bBHYybHe5xYXpwTOszOLBLGMHN2Iqx0FtxIpgxPMf6zBByCBGKGu0iaU/qIwA5BibiFTRgVBHab4k7BMOuEwxMXvCGDbRTzKprmi3vDcqKKY0kDKA2ISjNcEEqGmEEMAzQCGVGmmmgGaCGag0EM0AE0IpJMJ3MwEyoCENAfEEKpc1yYO8eXUwbksjXXi47HaRP5B9Y0kEiz8ARH2sfrHQ8/O0kSdRMKmVsRShAvyalVFnfjvMAAtt3Ji1XNm4C1xOcgab8TqzA699jzIMgIO1Sxki+4NfeJZB527RwDfxFybb/MoVedjMTX3mQH1BXfzMzDt95UZWqWtn0qB34E5Ts3j6xldlPfiZsXV8jX9pMHsO8XVccKaLdl+Y8X1m9rUNqjFy9Cj9Bvclvvf7zEyox8xiBR7xGsKO4qFW+0qAKDUY54oxSN9phybhCXpP0j37du29xGFwA0CDKG/qHzML7RPi4QaqENyNoBYaob3uY7PvKGvaLdEGMagqBgwmMBEwsbwMOJg0w7wQL4Gp68zoDATiVirAjkRhlN7wO0ZFOxlkyUNjPPBIMdchEmGunqymfGMbNRNkTw3WrE9DPkPqY2BO3P0nN1BVlQIDpVa37nvJOq17HMRuJRFDMqkd96i6fdLKmnpzlawCdIofrLUj0mwF+jOPYWdVxcXUK3Sn0yScYpjXJ7SvUtp/DfZszDQo8Tn6o4+n6PHhVSA5BPn5nF0eXn1JkyKT9fmQQkNqBoruJ1dc6sQFXSASxHi+05sKl3sVSkE39Z2l6c76HVv6mZjo0f/AFiAahzV9pbrMYx9QwDFt+akVPaWeJfQu1354isKN3c18j5jMDVgbQiYsMI5Ar8tbxSN6uYMQD88wAdjNDqurmlHsd5rgJ2msVObbEwEwFotwprhPES4RwZA17Rwbk+1+IyHaRVFM6MWUhwGNg+e05bjqd5FelpmFicuDMxygMSQdt526TMqKNUor2ZMKYQJBYqOZN1qUW6gfjiRIkATsJo6g3cVx7pVLdQk72IvxCPEqmsEQxV2MNwhoyGIIQZUVMEwO00IwhgmuAbjRZhKhoIZoGhghlRoLEx4gMWg7TXFgJk1cYneaCG4UeJrqaDzvIE1E2bir+RhCTWOvMQWRLFUUbCSaXNgV5kQuo0PNRArD2AdzvFBLZEBG4G9SmQW9DY1xJ5B7mdeBxAnkHvax8ybcRiTf1in+00idbGIVPBP6yp2r95NrIviVEwdJuu1RT5O+91GIugJmWqPMqEeibHBEA2NQtVkdvEBVu3HaEY7N/aMMrBNOrbwJOqBBu4L3uMVUkHi/m4IoIF94wI/6kDMv8v3Gj2FcyW8djqJJ3i8G+8odW23+0Dcn+0U7DeEXzKgHYfExA5mJoWRfmE1VCAnxDttCIsIPHELbm/iAdob2raUYHeOCDJzXAciD95gZoAE000DTDcTQgwiisAKPIh2PEl2lOnGvMinuYUjMGLqe3HybitQ6UjT7tVgyuTGUxKexN/eDNQ9gA2qz8zMarkFlgByZdyT0GLGDuXNDtYPMbBirHlfTq20qfBiDHkYnHXtx8fUxaR6nUMmDphoqsYNX/Ue88vqHbqs6Fe5AUHtOvqFfq8WArS6zWnwBF6hArZFXb0EGk+JzjVeZ1YIzMbsFjRv5iYNQbQCAGFNfFRuoFZGBHJuHprGvTepl0gD55/adfxj9Sz86tZeybJkRKOBqoGxE+viVCnZo+xTbniIbskRk43+0qCFJViO3eTYbzoxKX9o4vfeqi5gquwx0RfIkVzHY8TRyBxNCPVB8TciIDcKkneYbY94sbeCALjCLCOIUw3O8K8/SKDv4hN2agOTsTGUyd+2Mh2Eiqq287x1bHSwUVwR3nm3R+0dWmbFe2ul1BBsGELPN6bqTixlKs3Yudq9SjpYNHijMYKmN9ZH1IPXUtp1C4wxY+IhFzapg0BSsWqMpcUyq39NiJZuOICkAg9xDcQbQgyiqmNJId5UbwyxghMEoMMWEQHE0AjSshMZjtFLSVRgikzXIuGgM1xSZAbg1AbniCSyWWriVT+sCdhcLOASCeeSIiUik9/MkWOok8ywUYgixt4mTcH6RE32l8a0p+RF6DcAC/EmFs6Ow3v5hLUV7R0H9X3mRz5SfVYD5qZwNCou/kR8gHqfJ/aRzfnrtVCanYk4q7EVubHeOtkbQOB9CO00ibG94CIf6CTN2gTAq68THcX3lKJEGjfeVEGXx+sVrI8gTpcY2BNbyDIV+ksqYUgEcmJwYxJU33gVQW323lQpFccTLyJRgtkElvBEnweNoBLEbdoeeIWUaQb3ImUhRvW8AD5lBWnbuCJJmsgiYbjb7yA/EA/N4jAEmj3gaw1Ebyg13BqIRUdSGHO/iYiEThhqjvB+UwCJpppQe0wMENQjQTTQDcE00AyvTlRmUtdCRjKW9JmXamEl8WO4p6/ShQ9AFmr6Tht8rmhZO8thzenhGRT7xan4s3/icy5HJJW6vciZmxquz1seLpFVaLkEE9t+ZyHPt+UC+a7CITrz0dgO4kiaBFkk8ySGvW6fqgU9QKFVaWhvpWcfVfzlbqL21Cx/aRwZjjxZEHDimv8AxOlcZz9G1LSgF7+R2jMXdcPUkN1Dun5Sdr8SuFAejzMxoCqPzvE6rSHBC6dSg1d9odaDpkHL3qII24Imvxn9cjVpGkEGt9+YpG0s+NgisaGoWo7kSZ5mmU5r32hOzQAC9pR0Y2AwMvBY3c2fCVArf/7fMRFarHEp1GdnTGKC6RXP7yDlPzNH/MfG80o7BGDCIJphtSxMRe43iD4jKYAmjN9IsAiEncQA94SOJFERu8QWDv3hkFIQaMTttMd6MK6MeQo4YciVDKWoHnxOUGOrVv4mbF1062QUGsAxfzG+Lk9VsCeD8ymoFqRiaHjaZF06gBAGs138yiZQ/HmcNijR3/WMMrDe94xXdqpqvebUZxeqzEEmz5lcOQVpOwHBlHSGjapIGMDAJ24hBmBsQVKhh5lUNiRjKd4FjvBCDcxhAjCLNKKDia4lzSamG55ikQ3NIpKmEJoRSwEKaaIrWeCI+3J2k0AjY+JI8x3YnaTLBdjcAHxQPeKq2dqNTBzwqiEONO4lBVAG923xHPUYya3G8gx1H673JspH63Lm+o6CC9k3UuCESz4HE40bJoJX8sbG7qwORhp5o9pFUY+8s1bXsZMDWSx8EmbK4b8m9xsVLjJq7lE2AQL5q5HJuCRKZDZ3i1YmoiYP8sjuJkX2iBh3MbG2wB+0qKontG1kymTFoUWo379xAjFEBT6Ex2V2whrv/ElVysm52oDtEIJu9z5lS5Y6ibMU0ZUc70xqqPmTdCBU6jjsCh9YrYG16CtEDeVHKpHBjMKHFiM+P+obbbiSF1zCBv8AaBhzKMpFNyDzF03xKFEZWAinZvdNpqvniBRiSbE16rs2REBmBriASK3EdCCvyIo3BgIIqoQ7CL8EXGVtQ+YxA8QEatiO8WPUGmULCJqmqECvENTRghIsAmAkYr7Q3Yw52TpghzKx1XsPicnU9V6oU439MAUECzn9741n9rkitiD9DOlOnZunqtJO+/e5wYfxE4lo4ATValjZOv8AXAxuxxg8kC7/ANScry/Isx1HEmEgZsm138H4nD1fUhSyYCoBNkg8RuqXF0ef3sc7NjDK39O/f5nC7F3JJsnknvJx4291bXf0WRHTUze9dyP8xHPvPE882BzXxK9NkUZFGQnSTvOnnbLpHIreehbj8NR0bSKo78m55bdQnqMFUhb2+Z34lD9EeQ2/6bbyWkQzlTjQAEsLs/faTUKuFy25NBa7SjMCoOj8u1yDgFr7CWUP1PUeuw20qopV8CRFVfEU/WCzXkTSDp1NV/SNjxG9xAje4WOPE6AVG97Sot02L1AUFKALszkzKuo0bF8y+PqWxMQp/MK2nNlY3v8ApJ+r+Bp0nzNEU6tuJpUdkNwCYzDZlMJ3+JO946mAQahsHeDaYbQMdtoQdoDcEBzvNMp7Q3V+DINGG8BH9oQaIhRHeFWg2vaADbbtIKjvHD6KKc/MiGhOwkxVC5bc8+YQSREXiEEAbGA3zGVqIMQcQ/NwLrmIFeJ0JkDjbtOG5XC+l7PHBkV2CNUlhfXqB2IlhABEwj1FqVFEMeSTmWFSBajAVDUBgDvATCTELACQNcBbxIsx1xgw03xJVbId77yZY8wkm9uTE7fAiKquVu9fEDuSKk2O1gzIxPJ2+YxFBei728yRJrmUyAHawKk6Oqq5lgKUW+TsI3IKKDsID7SASa7CVKhVBUcbn5gQAF77/SEUcZauNoofSCOb2+kfEA1r96lEizUBfEDsSu+0qUATVz/qTLMzURdnYSoRWINiULllJAoVQA7TVpewfkEDmbI66aBo1XN3HoVDqIvf/MD0rEA2JINpJ73Dqu63lB/pImC15gHAnTgxepd7VKigx6cKhzXfaL1LkIFXYXtUd8gVSTu3AknIy474YcVMz+1qL1qsbA7x8GMZHomhF0GwO86un6em1E0BxLUF+m0gU2wnMzsr29ETtynTjIuedm3BHaSdqjkIeyNvMjp8HePRG0ABUna+02wykg71EcFG2EcALXaB0G5s0ZRNhsDB2oGoynsdxFFbgwjDmFl8TEVUwNjeAuq/iNe30mK1vCDsaoXAOnv38x1NiSBI7w663qBQjxNArBviUxoznitrs8VG4EIi1LjExBNUB3iaSTVSfUMIFsj6yrIRmVMdvoINIf7ntE2nXhZcWE5cv/xrjWwBufcdpnnNa4uH8VxMcS5GILo+j2jYbXQ815nkUebP6T3fxYKvQ49DBkJDKR3sGeJRNAAk+B3jh4vL0pv/AJH9JNrBBs/pHLUTYr/Em+9HkTbKuR3yAMzMSBVk39IlMN7P6RuEEfQ2Y6UW9oHOdR4aamFbidGXE+Kjkx0vFg2JB6vbceYAyatVHcT0cIbD0zJkybkChd9+JwjdwLrep3O6equtgUX+nzJyIdFPolm2Utt5M5XamIHB8zrbMPQfSoAFBe85c4oKD+YAX995OPa1I7zb8XMOJjNsgCQbHeHUaomAEjiA3CDuN4Wt147V9YA1CqhRwLB4MCVle80dwDuCJoV2TH4gmO8yrRgR4ij5h+kB7uDeLHB2gOp7dopmqhNCsIx4ETvtHP15gEHYgmY8QDfaAWO1wG+fHMcG7iQg1UgJ5jA+YCDdwAniFNdVGO8W9prIMgYEiNqG0Xma64gU7RhJg7RwRxIq2NiGsDfv8y4yVlWidLeZxg/MtjIqrIkHcpB4MahOXG2g2D9RLpksbioVSUWROQLVC44yqVvUIRSAwagIjNcmmM5obSRFsPHjzHMU0DzvM6pXG93+kUWWrtGJ+n6zbdzfxKokbbSZFbncSmx21RSfd7B9zJBMWTZj96Aoea5lNwNzck7EEnn4ll1Adlb2942IbFBV3uTOf3Dkc8SuHNpFMK35AmsNUdRr9xPO23MfJkUp8jaIXDLanft5knc0OPtADAE7bXKY1oknexUTGNRAA5O0q6AXsfaLsy1Cu17XAwCIH7wKQ7e79orm9h+kYFdi5s8eB2gxoSRuBvtcfEln9oMhpvYKoUKgSzBQ9EnUOdtjJK23H0lNBaLo1WBdiaQ6bgnxOnp7FEntZ+nacSnlbvzU7GyAIGTYGh9f/BJVhcrbEci95NSSaqNlIs78naQOQrsJYlel05V/zAEjgyzOFXaeSudl3BlP4kuu/MzeK662yBjRnO+OztIeqwbedGPIAQSZrMN0uTDpqxIOgDb7mdOTJq3u5zub3iJUHxgCxFV62McsexknA5mmRcWbEXn5mB0m9/iYnx3gKTtCp7doGIrYbxQYRUQEURcW/EcCxV2IAPO8wEJU1AB+sC3SAHNut0Lrt94c/VOFAVkA1Faq6H+pNHZL01vM6BwXFGuduT9Jx58du1qXonq5MmpkJTQu45uKOucEKbI7nzFtgaNkMKETKNApPoTLOEXXVjyrk2ACn+8f8QbT0mNAdzQI8VZ/zPNTIyEnUQx5lHGVsQZ1A3oefMvfhFOqyV+HYMRBLWWvtW+0n0ZWnYD3cX8S2ZF//FoaF+uRf/8AjI4yUwHT2bvNTpKj+IpuuQLsdmPzORN1N8XO/rULdPqvZWG3m5xKtK2+220sRnYABaInodIbwqMSmzufmcORQdAqtp0ZHbFprYUAfpLR0Z9C4yMr7MLI/tPJ4O1kXOzI65BbCgovbecxoDat5nj6J5WIyA8HxGL2fdZgyIC4MBoqfM1UN6h7Gh4M6hkGZNTbP327TjVCR7dyBZ+J3YcZON1WiSorar+knU7VmUaECKQx5J7/APUiQQand1TAgIm4VQq0AePmcaIcmVVUaiTwO8nG7NLFGxn0xWy1dna5BwAdhU6866MR95azYo/acmo8HeXj2lKYIxq+IDU0gTTbTQO7jkQTkx5Mn8QbU6WFi+w8zrU6hd/TapmVoYYCQu7Gh3PiYG1B7GBoZqPMIXj52gMD2hrax+kDr6blCQSNtoVIOxiXVCGGttxBW8DA024hP63Ad+01bbQGDCt4Tuu/2iCi0YcbwHB2ozDmKDRjcHzIGIHaL2hEYDmAFNGo3eALvHkUAI4G8wG1xwIAUX9Y42mEJEimUiWWgLJv4kkWx3/SPpJZFHcV9JA7MNBIu5MudwRHZQuOrtpMglie/iIq2PLuBe0rq8zlFg7ipdTYksIJMWzW0JsRN2NASDfXfsIdekbczFa/Ma+kKDf2ih8wFCluRUdQF2G0Zr3sgCISEWgSTJ6pix2raIuktbEV3i3qBYn7RWAJpR27yyIm35je024uhsYSO8Yc7mj2nRGXdBRo3FP5qIh2UkA7QA++gb8GEPjFkAH7+JXK1sVq+5kshCqiIfk/JjkfzNJNsfzV5kUMa0wO9XBkAZ3Ybd5RrCu1kU23zIu+reUOorHfeK67mo9t6YU794tSBETZiQdhJ5U0ltB+86WGlNI77mTINcbyjkCEe4DjmUxsADqJIPYGdPpgYyb3A38SDYyy7LY8iX1Ei57xKJ3llxNlbSTVDdj2mTEVJDtsByBGmIVXIMbGyggnffg94XYNxe/YRWWiLYc77RqCTbnTuI2s6aMSym3Kn943qoSDxXmAQ7VY4g12IxyWpC0o8eZK/HaUKQSdopEs7EACtqkoCGAXcZu8A+tSoUiDvCdjNW0IwqFTvtBUIqBVSDzzL48WP0/VyFiAaoCc17Tp6Zi/52GlBdGY57nSz1mbpySxAA5IA5hVlddGLSo5JHMk2MZlLhiu/a5DK+SveKvfaxx2nKNk6lvRykqNtQNiZBYD5CtDavJinMfRVXUshYERDjV3pPuRxOk1mujR05YM5AbwBL9cqL0WDQBWo7j6TzAG9QhSCR2noZtQ/DMIftkOnbtUd6sTzmvwzHW/89uPoJFQrdMyst2efG8rm2/CcXznb+wksHuTbyZqJR6gEdE4NVqWvjmcPGNvqJ3dUQvR1dhmBH6GcS7ow+REujZgUyKrCiO32nbnXHkxKAN9O4M4+qfXm1Hmx/adOMsdqNy1CdLtkyeqmo6CNNbH/qc3UJpcDgFVP6z0RmTDZKlmZaYE7Tl60DWpAoHEhr7ST1b45W/Mg+YmTZu1D4lCLyoOLYAR+pw6W0krpB2YcCW3tC9LnGFGY4wx7E9h9O8ZOtJxsHrX2at5ysToAqwDWqbELyDkVuTVyXjL3TXSCpUEuVZRbf8AU6ehZG6sEb6VJob2Zw5MThQ7Kabe4MXq429upT54kzrpXf1Qr2qNP9J+e9zmX8wsX2lshd+nxl7BArfvvExlVdS247ianiX0hFGt4srk1ctJneWIX6zTETSomCcuRFOSm4sm6HYfE6+myK5CUNQ2+s5HU5MoOPGF1UpCna/v5j4FYOxHt7TnG3b1LkMyIgXSPdtZG28hh6lxjCEBlB5PabqMWUIDVFh47fP1kg7KyshIdODXiZg72vTasONoh6jJ04OSxrr42vuBETIV6f1HcEvftX/j5J7SGsPkJF8+4g2PiN2qvhyZXYF6YNxZ3/edI2O+050yClXINLVe/eX3GzKQf0moimoV4h/vJCOv6TQxE0O57EiYg/8ARgAfpCIJoDDwYYtxrkDrRPNRu8RduJYBTUAAWIeY4QgWIKNyKyiUXeIL7C4htX9x3+DA6CAosnb4j409QiuPic+OiSG7jnxLYUOoFX+nYzNVYYyX00SF4sy+MBWHk7eZJV/mFiSSe1bRhkUHwfgTG6p3TfUpIJ2uQLFXCkkzoTdQwp9/NbxCigksa+QZfPQgyKo5JF1VR1dGbZYr+47WKHJEne42v6CB1Mq6PiSQqOdowbSmkkX9ZNiYwWNdquKTvVyQLngwPrBDGhUmCjC61bAQBQR+wiq4cGuR2gJPmWQUVQF3G4/vItYazzKp31E7SbD7zUQlWLEDWOftKKDovYfMk49xriaQWo/G0yJ/M9wuhxAgJauxj6vTaxuO0AZGKiq0jmIHJN3vAWJPkHkeZVSD7in5e9QL5ySpXYgitvMhpprI+3iEMSFJ8klR4iu1ORYO9SRVlI0GhJu9ZNoVb2mAAFgTz3lQbsAymFeWPaTUEua/SV1aMWkck77wqb7m+xMgCyZCUP8A3Og8SZWzKgM/8rU4tmsb+JI5TWkksvyZXqFACgdlkseLUWvgCPwbG+MYyVBUkmye0TNn1uPpxXEGQj1e2kcbSZxszmvcfiTDQYXuDcQ+Tf3juuj238xa2+ne5qMljKa5ikfrAJRS9vibtY+kHxDdSBSOYpEeYCzKJkTVHKzVASoY0FQjR8RQN/MBI8CLMOYvaulsodPSxg0dhtQE5syCyj+6j5lcLrjttyTsBEb+Y9k0K8TnOM41d1xOCjLjN0TYI7y3p1pFCwe20R9TlGXGx0tfHaJmzZMuM0ukDcm+01pjqGmyaBI2i9ZlZ+nwqra1DEADtsJH8PLs+QlCwA3auJT+Mfo+qZWxYywob8r+kzttwkHr8fp9NgVlKm22J4/7idDo9HKWb8o2F0Tc4+ozDJmd1NaiTV3EDLp3/N3901nRvbp6ogdNj33Y3V8VE6H02cnJuinUw812nNp927UPrDjNEq2ZVB7kE/2lzDRz6Gykjj6z0ui/m/zA1VSizvdbzyiBqNZA3yNr/WYh1RSwpWNXF7iR6nU43/hUZBVMbNb/ABOL8QbIOpfHkv8AlgKAOwHEu2fHiHphQiqNyN9U58nUpkRhuus21dz8/E58eV3xquR8lOCO06nVlyEMVZiLNG+ZzZvT1ViLMBySOYmrYir8dp19YUxKGBXakMZteFCLBBG9dpy2w8ymJwz6chYg/PBiwPjysDpV6VtjfAllGfG4Z7ZWFDvY+JELjGYWVbHdmu8z53shSVWqoSZquvL6igkqQH/vK5MDXjUil0gX/ec3T58pZdw1H8pndly6FZH507DwTVydxXP1P/zvX0kgCTQ3lMhLFSTZC1AnxufE15GakdpoSbYzShWT0sjI/tOnkGwY+B9SHUV1E0ADz/1FPTvmxtms1fuA5I7xcSNitjuVNBexBnJp6GF0cPrL6CL0g9xxc5M2IeuPTIbVvTcX3hxPY0gMW39o7Ed5sgYrRGhSArbbkHvGf0aig9E2hDEiwe30lsan0gF9tdzDixhgDuQvB8mWWwGUgaTR+8uUczdO241WCdge07F4EFTagWIFGvBlQ11xGGQg+YkajpLVsBZMUVR1JstXbbaY5dztamTBregD9IwZbsgb87XJiiN125gqMQlHkHxMr7Uwv67/AKS6BDcoy4t9LnbuRFCFhYFjyI0HhQSR+u8Pq0NhcmRW3EEC46hgPaAPrvFGRi+osbkoQDcDpy5iy6QAo8LFDCiSST2kt46WaULZPG0mKYGzHVmB9t2PEwU49RFt2UgfrBTcqCRe8mjux5jlFGr+YzC9ypAE4kXITaAmjK5spL41f2029cTOf0rq6ZkIYKTzdShAsEf2nLgdMeYqWGlu8vkYDEzK+9bTNnatlJOwv5kiBRokfHmTwZCbUsRQsVGZhexubkzpDAE8AytEpuD9YAXI778bQK5GzD9ZN1RFhaUe4mTyHVW1GXC6lBFCLptjYIIoXAhWjcEg/SFXrnuZmRix238RLIrwZUWIrcMK7X3ik2IjVXJ+IcbVseIDUSIlUTKF2A8AxdiJQo/vJuKO0oeYAt34lQiD3D5ljRojbfYXNprYU3aAmuB+kAAFSG1b3sImU1soHFHaBwCWNVXYdoNV7EAwCrHSATtKIQF9x38RMa6uZVgCLA3BlFVWiHNjuPmTyONe23mUZwy3wfHic7j3jnc7yCo434MFfEzH2WNocBs6mG1yjZ1ogXwBMCuPHe1k8RsjAm+T4MgQWb5Miud1JbuTAUYCgQLFkePrLuCRpU7se0jkHi/k+ZWUCDZsV3m1HzKKxG1mviK/OoUa52lRtyNxzxUQpUNnnzCD2MoWEGHSDxARUDXcYGuO8Vb4jsOO0gFRSJRVsTFRAmASZqj7zV+kont2mqMRUEATs6fGuTGBoYj+qvI+TOTmep+Gkp0eRqv3cTHObFjmzdNny4SSTQeqJux4+kGXodsqhgincKB8XPRy+7GVDUzvQ+K5M5WxDpMpy5MxyKX7m+Qf8zGY287o8WHHqfqTSsQovfec34qmJOpU421B8Ybfn7z1M3Tr1XQfxGk6idRUHj4nl/imNVzBApGnGoPk7TXH1L480rubF2YSgB3F7doRt9t4MqMoR2u3F0R27TowAqrCi/ESizhVUlidgJ2dO2LpunOR8Pq5GJABWwsf8NyD+LLsApKk0F4+niYvPJelxyZcGbCA2XEyA8ahOj1czdKAEbGKo7UN/A7SufqA5L+uCxBBAW7Hj6TnzdYaVSV2GwBsTG3l7F8czOcrMAK2uudhIGwaNyoIfMC6k77he/2jZsLvmOhWL/1KfzAzpOumU0YVTCx4uZkAUCwTySDENryIA/u3mkZhR5mX80OUd/PMmNpRQ8ze77QrvUaxyd4Bxuyk6TVzqGU5Ftjbf3nINtzCGpgR2gd7JaoaAtf7QMVA0pxXJ8xf4jUoalJ8RWLAgkGiOO/1mf8AqgLB1gaq5E0oHXHhD3Z8VxNJeX9GOrrm96DEDjOmiV5J7EjttPLyp6ftJHHu24nbj6odXYIALNZAHfzI5sGRcbO9DS1Ecm5idere0V6jIMZXFxzquudj+s6sY/iAMYxsqoAHBPJ8TgVvTBSr+CNjLYc7YlOMMV3vbm5bP2D0cQOXMuNKOo0K4EpkxrZXE4ci7vapwdLkXC7tmZ0cKdIX+on57S+HMWVshRbZaHtqvkSby3peh6gumTEBrIA7LV99vMhgyhsrFRSt7qJjZ3LFtQFE/lBscTnAbGwZdJBFixdf5l4z+0r0LHmv8xc/WBen9JUJNWGqxf8AqIM94XcJpU7bCyB9T3PmcqINOt2ISjtXFxf8jx14shyYw17EbSqsRwZHGgVANmUbrtvKibQ9nvuD3jEk1Rs/SpPeuI+ItdXtf5b5kU4GoXz2G/8AaFQUyakfSRwTJkAN7Wo97jpY/wDkvTIOgtmZKyHUD3IuLkx41XbVfiHEd7C7AbUDVxco1uT6gJ8kzMUMeH1HpWH6TpXowPzZP0Eh059NtdgrwwUzqx9Rhdq1FT8ycrfxZhsePEv/AMeMZGHN7/8AqUyOBgIYjEfjiouJ0XIfS9I6lP1gykhaK6vtM31U8DaEGgu4O47CUVsagFlRb8SOBSWOjUBfB4hyOiY2DMXo1fEWdhsmT3H2A0OR58TjOZj+bcjg+I5zXkOoUALoRHON7YEqfBG03JjNYZ387+a3lF6h60sbHzOW4wM3kTXUuS2BGw4+06U03p1gk8TgXJ7a8R9VkNfPMli69IkqpGqRDK7Heq4+YUya8SKg1EbH5iMVVieD3CniYjToxsSNxfzUpqApbB87zlxuqJubN7ASqkEWoBreBQl1JNWKsyZ97bA/O1x8bE7WfrFcsrc6ge5H5YED7CQG3HAmBW/cdB8VBjUOdyAR38yhwENqUBgO1y6g8AUwuuIBfiiO0n7i2/JlUbSoBCm/3gAx1FLe+8nYZ6AreWZXC7kb9hGibc2IANrO3ibnaoSa5/SVE/NdzxJlaNWdpVQCe/kxG/5c2d5QFNN4lVOpwfAsyPBMtjFJfcyodjtQ88SbX6lHzGN9vuYcYFEnm4UchpLBFcQYQQt/eLkOpwt7GOfam32+YCO9mxwZRq9JQPqZyqTYHmdTMDuRsBt8yCVDckgeJNhYlsm4HjiJXsAoXKOcr4iiWcbxCvcSomykHeDTe0pd0GEGkqxqEJVTVcffaEC+0BNNVNRIHxKAWKm00bqAoscQ13sVD2siJdf4gPsYDxAsMBGEUypG+8GgsaAjRNZ6nRKP4Tfk2R/aeZjZAwJPB2BHPz9J1p1D4sRxHIuTIWoaSNr+Zz58mpFcap0/T5MpTU2N20gE96nFkOVvwkuGK2xJFc+6XCZcboMjgrqthvTGT67Nld/4XDjVKWzZ2HzM/TeO3DiXGBhUMAQXPu7/AE+88L8SJy/iORMSknZQO5oT0eo/EsSJsCG06Q3P1nDjTCrErlbI2Raux+w5j6ztL24v4Q0DlyohJqvzV+kP4g+JdKIC9KBrcVYG20pl6fWcja0ABvSGs/SQUL1LNpT0lWlCgaj8manfes4526rKQqIzKFFAav1lU618aHUqE8BtIsTmYAN5qLlyeo5YgKT2UUBN3jGdZ2PqEfl/ednToMnRtp9JGXcs27MfA8TzuDCHIFDnzF47DXauYrj/AIg5AMu+NVA48tc4y5u7N3d3HGMNgL2oK/qZAxJCqBr77/EHBB5HaKt3tHQWD3m0Y6i1nY3X1gYLe13UxfYgrV95iCdzsYABN1H1Wunj5iEUdtx5m1Hb4gVHO55PMp6QJoOtzn1m9p6nTHo8iI2VQSBTKEPP6wObHiOvZ1Uj6xPXYsbY77Gu86MyYcTq+PK+QkHVaaanNl08qKvcyWaAczlSp3+vM0nU0ZB1fh3UJib3MVAU1XY+frN6zdRkRFCgGltTQ42sfrODSwoUQeZRsL42AyezUA1eLnO8ZutavkCLnVrGRVALc19JLOy5X1Y7FDjxC1PiyMcjF771xW31klHfSRfEsHb02ZGUqUDMaFVzXz2nb0+N2xmxWk6SSeD4nJ0a40xjNlJC1ppRyD/mdXUfiN5SqKcSqNIpt6+TM8tl6hCdRhKF2LAgcaTzI5FHpjRYah9/pM/UocRomgKO/wCbxUrjUNpZ81sFrcjb6VLN/Q+NDkA1KRwqJXP+zIdVSt6ZB53mXMCNyQAe8fIgPTtkVdjQJJHt8kDmP/k9Tx9UdC6yoN9xVidfYHseJ574G9QBW1BhQY9v9S3THNZLixek2dxNI7Aw42+03ehcUbeJr7SiiaSwDNX2lsbqjjS1KN6r+85LjLkccGSzV1fPnd9uAew4iIyqbbc9hBfsG+/b4inmJPwEuSTXfehHV97kqhBlxFCxO/edGPqMq47VzsN5yXGB3ksXXoKcmXCuS1GoEtWxNSLI7lVK6e91sJunyF8IwKdLBiwaPiwUWfJkF9iTt/3MXqr65crV3snvJhpbqdDG0yqwUUL5M5uGIM3x8Sq13mLbbRVa5jsAfM0hw2/xHVtmHAkb38Rgd7kHRjdl4JFyrHStMPcf2nIGoXKA3Ji6qCQfvzL439hBInKD3jhqA32MmK7l1pwDXjzCXNUaAJiY2U4wQ4U+G5mKjIw1v95j/qpH+XibIDvdV5l8bFlDLRBG/wATZcWvGFshV7lZDGug6Ttq2uXqniq0V9ykgdxvEZCGABDfEXHqUlVGrVyLoxxaiyovuGPaTwBXLNvW0sMugGjQPmcxsHUUoXsQNoXUrW+/gHtLkFy+pb2sDmJkUqbaTRyPNShoj23fjzLJiGUWpINKOfkyLbbcy7ZFXHpA5HEh8ywpa3qWTdKHaTAjqRVGVGdtI4qMDUVt9hzF4FHaAderIp+alCd7O3YTnNF74qW9QFSK3q4ElA1323qZ393xCw0KCDdjmSLXArrLAWbhvxJKe0Y2DCi0yi9yLAhQajvx5lTQTSNu/ECSYTkfYgAdzJ5R7hprbmUo3tF27mj5hCgWDcHBo9oXXz+sB49xMqMTCHBi+3kGKTcooa0xKs7bQiGgZAvEII+sxEqmD2qzGgew5ktkWFVSUJq64+Y5FYGshSCFNxyf5a9PjJtjZ7Rc2HTjz4x2AYnySe/2E57rUjz82Uux2CjwO/1kulbT1ONidgwMznc7VJ46DGbkR9F1JtlBFAW18gVwf3ni9Tq6rLlYWNbqinv87fSeuuQDohnLFm9MLVX9Z5PSYwmHqOtNgo5YD/lQ2+wuc/1p5HUMfUYX7boV4io5RgwJBHiHIzZGLPWo7mJp08b3953kcxZnc1+gG0t0/qDIBjf3NyD/AJnODV+RMzkkaeT4ksNdGceour+HAHnGCAfmcjLRNih9Knp4FyYMKv7kyOKJyH8o+BJZeqysS7EZBp/r4Pjac5zv4tiGbojjVV1q2ZjugNgCr5nIyFCQf17GdC58esNlxBtPYChNn6k53YnYEUF5AmpeX6nSfSvhGYfxGrR/9ZM4mCK/ZuDGVEs6ieeRJttxNfox2MKtp2EW+53gsXtc0ipUNuIm90ZgTW8FwGsgUCagmB8kxqFc3AWjKY2K8RRUND5kHSOoVkAINgbxlxo42df1nIOb5+soEI3G4l1MVfpmxqXJWvrNBmyK6KKKkfPM0mqhgZQ2vJbadwPMmzs7MXayTZJ7mdfRYVyHLpBFghRWo/P3nI6/y2evg/UzEvbQ4XCMuStWkgkTqz5UzOpVSMS7aQdh3q5xKAe9kd5cKAAdztvUtiL4lJwOERXLHTwbHyJy6ra2Jo+Iwyek2xBrx3lkRnwtkY4kDtueT8D4Enikxs65TkfGGYigT2nb0+TI1EuF07bCz+8jgxnMSgf3AWtDYwjp8iMAHI25H1jqiuXCpx6lUlwbskkmUxWUBJBvxHXibYTUmIwWrrubMM0PMqEJgjEQdoVuZgZu00DXxHXeKIRAaCYzVA1xr/eJW8ajcIuupcPtcKW+O0k+VmBWyd7s8xsuUPQGwAqqi7+mWJG5r5MzIpAajXaCyduIswNTQIBB5hJ9ogPmaBhzH3PAqJCIDDggyuJ/6SOZG4ykH4MC5vc8zA+2vEysGmYfrIpixOMV2Mv0gLkLZq9wJHH+Y/SUTIcTa15422kF8u2oKbF7/b/E52rsbHmE5AoIUltXNiqmDoy3ww4rvIp/XfY6h9JHXvbKCL4uWZFx47yFbItR3qTYFjZoXEgy5SqkAkb3cC5LsOurwYCjVdGosuRHVjf1F07fOqOygULA+CZxjmVRiGBO4HYyYuugj2BbBI3MTtxcYKKDEkr32/tK60KgaSZBDYbj7RCCTzOgKpG3J7SZQi+00hfymwfpENxqikVKFhDe2aj9YCNzWwgFmsV+kQbGGqomFQIDAgbdiY6i7PMRjZpaAhBYLVyClhQe0KMdQsXfaQLWSLuuIwPtFwGyg6zZs/EQAWAe8J9xvvBte5gZV079vmTcEkneOw32i7yhD8xkAY1qoc7zAWaEn1DIcda6RWF6V3Mzb+Qkb+IxnJpFgcAnvKCcGRjmfUKs9gKqdHS5QV0MSWG4vxLB0LRYDyalX1ZepCmxX+JLGC2RQOSZfJpxtzf9O3czHO9rB6ZNTu7+7SdVnvX/AHOXq+o9RGGS1LEMFG4+pPmehmf+H6chgGdqRQe4vk/czxOoJ9dySSdZsn6y8YtpT722223MHTIC5sE12EUWclLuTOz8OQr1IawtLY3G98S3pIr+Ms3o6kyEYl2IXvFVFx/g7Y8rhdYqxvzR/tK5sHqdE+N//lZT7VH5a/8AU8vDmZvwtno6sbUBzQ23nONvOK+pkVMY/MaFzNQLDitpfpULlshDaLK2PJ/t5kOpxlG0oy5Aw/MO87y9uWIvuavaZd3ALhR5PaE0F2kj3lR6hynrAcePIVTGAC9cj6eZ5uQhXIQ7cbd4vqPo0hjp8TKAzgMwUHue0xx4/K26FzBiLAOx5EzAKxAYML2ImJ23mgQaPzC1EXe/e4g5hqzvtKgHvxvAFNXU21HeMx7/AKVAIJIoHnzHGIlS9gG91EjZjjIw7wAyEcQKJW7F/rEU8bd4BAG5JuY3dEQHbiYcSBvvLDF/IXIG3JqhH6Tpi73lxZGWrGkgfv4i9SoDflGNQeFnO8u8jWKAJidgoLtVBwR+005MWUK254G00llNN1C5OlzMqPwNyp2O/nuLkMoYm2YEtvsYXYm9TWe5iYyNSn5m4ipXS2lSGFXtuJXBkxY9bZw7kLSoDQJ+T4i9QMiZW0WFA37ab7fWI6gAaGLWLO0eh8nT+njXJqLaz3ERbsS2DqFIGLqlZ8YGxU7r8/MUjEF9QaiNRBBIB+JNv6rt6YKdHBfVsV2v/ZnqZAiorJiGTchibq/ifO4szq1KxW/mdRyrixqmvWCQzGuCL2HwZz5cbqyvTTHhAUZMvuO+lRZrzM7YBkpsLBb20vvPJTLjyOGyKfGhDW3idGPOE0lyxXtQ/wAy972ju9P1VLYcZCjbc7CSKkDcEb1GOVceL00YZSd2K/T55ip1SPjHqKbXjwZePK7/AKLIH1gqM1athpPiajW9D6zruoQzQzVA1Q1DwKMFwjAmGAQiUNVCKOYTcC+ZA5ploCiICbUDxMVIHY/QxbowrTdpvpNAFw3BW8IEII8TTTAeDCiNx9Ju83exNV7wKI1G+ZfY1vyJyiUViODxAtupsQ3a77ESYYkbmG/MgY/IgBIIqHk3yYIDl9TW3u+sbV8xABuSRwdr5MAYAjUNj4hVlLHYWftcBAO6ggR+qvGmNVGnazQ7yGIsSbJ24kgo2MqLJXbtcAj1qocn4hVKBN7CAVY6dNmo+LIVOx55kuO8ZSR25lFjZa13qMXQrZX3fWKhD7cH4EDHSNB3+ZArUODAtE7iG6IOnb+8I0/0rv8AEBSKH5RJkEnYS5G19oPT1MAbqNENB7kCZtu9yzYwORfi+YjJQNmNChu9XXEBY+AtwEG9pqF+6EAVfNfSNcBbxBudoDXG2qIAb2hHO4hW47xTH28QEQF3uhydog6ct1WTDuysBRvg8ylWYcGAnq2BDKGuyDVDtJVjztOlqrgy/RJqdiew5+JJx7wAb3oVGwE6yaB2qX/iOrJ1AxAaUI1XR8feN05zZ8xygVjVga7X2ET0Rl6nGmSytb19OI+ydNjwq4DhrLKbojcn7bCYag/ifVhnx+gfyknURyRtPMLFiSxs3vc2R9Te0UAKAgUEsFA3YgCbkxnVsQUKXNauBq443iLqVhoNG6Eo6qgIUFtJq24J+kTpyWzKpr4sSW/q49bKMZ6M5TbaQas1e2/2nhPkzlsvShFRsj7qgq/A+k9TJkw4Rh6R9a48yF2IO/0kup6cdR+KIindEW67m+P0nPj03UOv6fH0vTr02JQruoZmPcntf0E8go2qgNRq6G89X8Z6rHk6xiuMsQaDE7ADxPIXK65PUDsr3dg7zpwtxjkxHts8eJMjyI+TI2Q2xLE8knmKp3sHcCdGGOMe2tpJhRlXaxV2eZOq5gKDTC/ML/mo9oKJjN+excil34jdtyT23go3MSeOJQIyWdroCLKIABZ3r+8IJVfr4mOMgAmvpAG1Nf8AV5jk6iS92eN4CKCxoV+swG9wMK3B+kKgaquxAalIrvDjOhwxUGux4MKjyYTWoHt3EUd2HPnyAux0YqNKngczz+qcs9gADttxO85lyDRhWqX2gcCcvVYXGJmcg0dPHBnn49VuuB6DbGx5mmdSpIYEEeZp3ZNlRVxr77YdqjIotSrBDfPj5m6pQuXQKOkcg3fyJgobMmI5FUf89yBM/inCsbZC/BsneyeZKivI5E6X6exkODIzYO2pSus9xXxFysH5BUoAAARS/AklGxAohyrdjYgWK/67RMlDegDdgA3QhTqMmNdGPliL76vgiJnR8eVsbrpdfzC+Jf1CWfMoX1GyO1cyY06aIJa+e1R3xnGxRvzA0QDdSimJijB6ujyRtcri6jRkuyWuxXAM5tyONuJXEwQqyoNSm7J5ksHaQrYrdyHvYfEoAXRXSxorcD+81LlRXYhrXsePiUXUq+06RwQO4+ZjKrLk2ptx5jA0f9yYNMCO0s3UnIhVgoHbb+06eIDNbe/Y/wB5tJHImxhg9pfxtK8qxyDU/I3jcEyh02dvvMq71FyKQ9kV8SmIBlOpqP0l0CvEFShQ9mU/QxSK5EagVc3HzMJpQLgIhhHEgANLW0LgCiJq8QhHPAuAsw53j+m3/Heb0sl0Eax2qNUm9DeERlxMw2U/pCMDkWEb9I0IOdoZRE1DZDcJUEXoP2MaidHxCDGAU8bH5ll6YncOvElqprHUXLJhxirtj33qVGLAMgQaiSLomZvKLjm0kC+3mbTtPSRQuQadKiq01LOpGMkqvxsN5n7XHiP7dO19zGyABRQ7Xv22nR1otiAoBsXX0nJlYltySaF3Ny6h3zvkxqr7kCtV7keI3T5vSe9IYHY3Oe5TEBr9zFa8by4PRQ42YlNqs7Dt5ksT+sXUsALsWO0jlzMmV1F1xz4kQ2xsXvJIO3IQqqqHURzYinf+o/Nx8Do2MWaaquo5KBgCQGmdE1tdz+soGGQeK8mMcfqAG9vpEGEX7cguXQMlncj7xCxUEDv3lxqOQISD8jtCcIG1qWO/iXTHMPb2JHeNjfTdj2n54jPgfVzZ+sUqxB9yggVAGq2pSSPmDI9Hg38wDGTx9xHUAJuRX6wJrTccntG0b7jbvUAOM9yPpHBVTs17dxAkQpvT57wEbShPfTt4vvDqUtRXaBIX2uUAUbElvMGpL/LUZH+APvA2i/yg/QzemfI/WUs82BcBahtRMgQ49HuLbLuY2Byfw3cagA9+RvsIGPsfSCTpPMToSy9BnLAlSDVfSSrHnGzUv0B/mMpArTdntUi1Cj4k0zPidXQ0R+8tmwe2rr0y4y5BZhte1H/1PJzlsaegyrexJ8d6l/4/HkyHPmxF8o/IL9onFnynNlOQgAnkCTjC3pP+ozr6LAr68zH/AOKiF/5Hn/E5V5nWukdCVv3s9/YCaviRta5OnyMTv6mpvuNv3uDosaPkZsjhFVbLHjxJaHHT5m20+3+86ulw+p+GZmRQztai/PxM3xqF6/CenxHri7HMraVvcCXwp6GGydeXLjA1dyxFk/pB1GMDosCdaaONw1XeuhxPN6vNkyM4BJBBbwAPE5Xv/GNW4jkXH1OY5DlbSAWcH48ftOBiA3H6ymPIUyAkBh3BFiIwDOKoDzO8mdOVLwIDd1+8o6hMlWDR8cxHUlvG36TaEsQEEkfMYAVt35hVaNmAu11Nt25/vAbDTcgftABO9XF5uE7nYVHVSEobHuYBUHR+XaOukWb5G1zK+hAp2s7zM5oECj8wFOMBidW97g7QCv6gYN6veMPcNzRgBguxG8yLtx95tBBhWyewECqH20B9SYQBR+YqUDR+8qq6moQHw6lIC+b+sX8QwsukMwsAEjV3O/E7sGPHiw5MjkCxoW/J5P6Tm/G3wjrUPTMWKKNTE3R7D7Tnv+XTUnTzeob1G1UAaANd9uZpbrfSIx5MRN5FthVAHuBNNxK5CG1gEcxdXuJ+0vno9PsvJ3fzJphZggVSXf8AKB3mdD48jkekCa/pGoivPHmOWL4goVRp4oVHx9OceJlZwM3LYyu6gfP+JLOF1Ure35PEnVUq6NOot7rqv8xtS6SAQpA7H83xIuQXNLpF/l8frNR0/FyodCA4sWO4uodhsLEUfHbvH05MhL7tXJ5lFsCY8ikXRIoX58wek250mh5lML5OmJxakIeiQCCD4FzpTImRgpOo+CNpAejDH+Xr79+060xreQZSyBPiUx4kOJNeou7GirCz9QYv4gXxggXpY82SCR9ZzvPvIuIjGW3BGmrJ7CSA99wB8igkWurb6y6jV+bZzzewm5b+oysRte0stVqDGxxZqSKaGptiIOR4mvRdnsaSqkcjaSY3uKHwJQJkobG+wgbDk1D28+OImQSs83zzLB00Xo+53/aRKkcgiZWIN+Jb2ihckWhA8KIdaULu+4riTJBN0AfiPqBxgagSP1k8VdcJYAjgxx0zHx+s58XUPjFahX0lz1rFaQDVFtDjpD3YR16cL3/aHH1a6Rrx+6uxMqcuMgXX2MztMKgOOyg3O1+IpV2BVnavrGGRG/KbrmhFNEmsf33/ANyYpCiEVqNDtEYrekFqO3eO5yaqfHtW9CHDj1aiuoMoujKEsqoC6ivi+JjkN1sQO7cxnQ1dsPoJNlK47rY9zzcB1/8AvjVt+QJUKCbsVyRJYyWQlWCrwa5nVj0st6GY+CNv1kpEn2P8tdVdgDvFRHBRtB13V3xOhS74rx6TRO4F38VAo0OVyZkVmU8/MikGN8uambSOTXYTrZwcC9wxvbwJIquMgqw0hQC/MD5sSkjWreCJLt8VzdQxd9R87SfpFmpa82dp1tiP5ihA8mxJ5QgHvbV9DuJZUcegk0u/0j6SLYLYWrBlcWjXQU+72gkxcHGUH/8At/5E3qFykZCcgO7HiIBGfvUxoVVVUqOnGGbGpCk1zSw9Q7BCoXQOPcd4/RuFw0zH3GgJDqcakesoJs0bPf4mb60YdQRjVVI1WAa7yzZVZdtqGwPmecF3NCdRDWF0neXEOrHVeMkN4uEZXv3G/IMCY2X3Vv2Hj5hKlELst+BF6F2zKaFEAxMmQV7BdTnyZeWYiz/THw24DgkX4mYonLsNge/0hPupR+3eEpsBt+kISiCDR+NpcRM4Td3+ginGb2qvrOkDaopUE8RBJbVr0i/2l/RX0QxHuO/0mCi+I7H2gQOZUFkMu17G4Vx23YD4MrUwFSoxx0NgP1kmRj2EtcUmUc+RCvT5XJqhtv34lenVsf4Vksi9BOnxcl1Seo+JfqSPgRWz5zgGJMetm/Oa2+k58q3PHBmYaLJnObO/md+RdtPVembNimo/r/iFcHTknTn48i5ftMcKqSK7xxi9t3Z7TvxdFifcdSKJq9MsPw/Cu75rHY3zH3DK8tMDs21WfJqeh1HTPkXGuGnGPGL00bPJ4jZenx5AFGXG6jciyKMRcTgnJjzKtbDQP2mLz1ZD5+hznp8eHGoFgNkY7b+PtFw4m6FPSJD5S3t08C+33qVOfOqHQjZWPBY8Tj6l82bqMKuunJuSoNVtsZJd6XxPN1QyZyoBOgFmY9jW/wBp5OTO/qElgwB8bSujqETOGDVa6z9dx+s5SDzOnDhIzaz6dVgUPrBpXUPHeAg8QlihBu9v0nRgGNnfi4rHtczd72My7jiAVpTd/aZgas94u+qu8ZRQJP7yhNjFBqPYs7RCPdAfCo/MRfxGLUb8QC9GqyLgsV8wGBBIvgRSewuPpGpdO/c3FI3544gBeN/1m4jLxvzAQT9oBvUv0j4q07j4iLsLIuFb7QHIpaBE6sCAgHvORfzbzu6bbFqgXz6W6Vcekmn1P9BW0878Uy4up6x8uGwpA2YUZ3+qQvtH3nF1lsU9l0CQ3Jb6/Sc871qXpw2aAJJC8DxNOrpukbq8xXBsqi2Ldppbzk9Pm1w5H20qSMZOoLd0YyvmwhlBK6hvtvJLuwnSVLozruq7EXZA+Zag4Uw6NbufULUFqxXmKdIOhSGBPIHEi2pSwuvIuHUfTrt8yYDp1PQBbc8DcwrROosCRwDvcfCxYBRQ9pF1x/7inGVYgbkeICWVY7Eb95VMrB2J3Lc71J0PTJZiW4A7RwAAKvfzzKLKnrZlJZUZj3FDid/QdJnbSACP+Q5/9Tlx4si6HUkIffqqqr68z3MbHINaEBH5KnYzny5YsgeljTBv72AOoV27ThyDUchyNsAukA+ew+J6WoYgpUk2OTzXE5vTR+oyuKIoBTfFc/rOPG99tOdADiKuwZhwb7eKgQOdWMnUg28WZROlrJ6mRj7T7R5l/T2tSKneYynpU1+ah+0Ghf6TX1j+mSaYivrFcKDSkV2mkHG2kElmv/694a7qxAJ3DComnWwCix3qZjp5O/baUOzNoC6dx4HMkV2vj4lMbsKNbHvxOoMGNDIQa32BMluHrz6JF0a8w7ryv7TryqbGgAit7NftFxDEbOYA786o+ujHOSHN1XzKAgKPZv5oidOP0PVpFdTXbaA6FNt7STROsyfS45mVlGrcRlZtQJG/ckRjlwa69IMPJPMD5CtaFC14H+5dRRchUkh1vzXM6MeR3A042byQJzJnKg+1HHPuWW/iX6nB6WNdJXfQhoEfA8yYqjPVqVG3PuqFM2ChdA1X555pLsfr2EOMDXpYEtdafEYPVTKmTh9JT8t2f0kcujITRsj9pzvkCNoVSb+0Jze3QByO0nYYvixE1ZPatoceXHlJOWgB5MgEUkuzfQc3MaK3qCnx5lwdwfGTSobHYAVE1KN2xAWe841LIDzRlkymguSyPPMnzhqo6kG9djxOlcjhKK4yCbFKJx+njJ9hJHkyuKwQjuVXtp3kz+h1ZMnVPh7aQedUkuDPl92nEQOdJ/xKdC6s3o5MSsDvfcTow9PjBfK/5Qdh4E1lVyomNQG0hWDc2TX/AHBkGrEqYxrYtuwHI8R85x5fyqV8KGklbcA4/d/SLupOxz5LDEBNIB8f3k97HgzsyYyXf1LA5pTdmIjdObBQnSedzv8AaXUwgZg+Mbr/ADDVbbWIwb+Q1n2jLVfrJ+nkZ12/KbN9t5fPiXFgAUsSz6jY42jYoZmxejaKONr5Hu/1JjMC6++2vk9hFazhJ07DYmc7D3EiMHshCeATFcHWMbAdued5HpXc40YZCQrdzvLhtWbWVN7d9pLyhI85kNJsdzz5lXyOq6SK+gjsAenxvVaXO/kTZ8SshfG2ojkRmiiMWxgkUamLhWAJoxOn/wDhFsSSTtJOpdwN+aJviVHWNxY3jbxManGgW7qNZlQbmuCzMT5NQDc0mMinvUY/ls7SbM1RM5XzZTlKYsd0au43UO+MBqBCmx9tt/v/AGnAesfIGXI9Ua0gfvJ3e4rpbrHfqVx4XUWdJYj/AM2kOpy5GfdqSttJ5+ZDOyFw6Ch894rMXPmJxnppkxscbOPcLC2fJnpL0adLjC5W3Zhr081/xH1gTFp6TG5Kgj3KCKAPkzi6jORiOMH3FizMDZNzO3l4vi2ZsWTK7KxVAdK4+w25MmMuIoMGNiTZ978HbxE6XIuHpi5AZtVqL42q/wB5wnzNfOproVSjAFSQx5PedPS9KDryNYRbABOxP+pysxZFZLtRRFTvwKMHSp1DU75LUIwuj8SVYk9LlbIGGkbIC1gfMZdOZ8DKQfccdk82NpLqci5HVCSMY2B5P1nR+HdP/IfMK/k5A243O0k/sW/EenVy2FVLZDiBBHcrt/aeBlQ43ZXFEGqntv1Nfi+PHwijQXJrgbzzevxq2JOpxvqXIxUqWtgZeNsOXbzWIBJG0Q7jxW8qV1MBVGDJQ4FVyJ2c0WPbzGBpaBinczAntzIGHFncmZjttMNuwJmO62RAGrudj2MCqWJIW6gosQo3lk9vs4uUBiQiqx47eIqjxzHpQxNExbI2ABvvA31O8xG/tFkd4OT7to2Pna4CEm941kDfYygFPpbg7mpnUWeYC8iv7TAaTFA3nTjxgbubvioC48Rciht3l2YY1GPmTbIeONq28QAEi4D6mOwsi50419PC2UkoSpCsRsfO/mpzqhOMtvYOw8/SW6j0irtZUtjC6eAN6qvOx/Wcvrem5P0Pw3q8Kt1DpjCJpVtLC9+Npp5efN6aPixbK5BJ7kDtNJf45btPqxy1parG3idA9VVRD7EbfcHf5M5xzHORqIY3tXM6VkX0LQrULvVe5+IcA15F1ozqCBpU1fxcvjTp8fSsfVD5X9lVQX5+kTJiXENSZEyjUaPkea+ZnVM+UMAhwY8ekk7bX8QOmTEvqMugP+X/AKiMQFL4g4UEUSeD4m9RsmS3Jc1W+8Yiq1lyF8hCoa9oUC/oIdeMY6Aoh70lRwB/5tIts1b7bbxtPqZNgvPAPP0jB0qoyZVfP/Ixk9t6HwJ2ZepyqcByk4U/pJABPzXAnHhVsCu23sokEWDI5epZj7gGHAHgTPztXXot1gfHgTCNeVQSdRJ2Gws9/MgXZcW5DqBpBOwvxJYcgdHIyJhNDlqFAcASOWw1sRqP9NUR9RHHjJS16uL8QXEF0tqbbb6/WdCZ2bPSk6RsBp/Mfip4Kks6oWAUHk9p7qZ8eMsqtqI9tgXRrk9iI5cZ/RK6cmQYwF9IWw31bn9Zz5CqnTSmufr9ZPJ1CuNRB2Hm9hOTFlssrGm1bWe0vGZ6V1oTqNE2e4jIuo0WAHe+BAgBG+1Dt5jlSEttlPFzogMFDD0+24J4MQl/zNZPmCiY2kkb3GBRpIAJo+e0dvYdKtYHfzF9OhZNCUC7EFtgPMnQlq5sb+RKrmIUKTqXxGXpwy6l3BnRj6PbcftFwcuRVZ6VAB2I2j4sbAkawBVWRc726XSuoqa+kR0u6WBzthBTfIC186Dcv0+HCrozMXI7aKH7GCq7SijvRFSBcyYyXOKgx49nH3uR9LIMQ0sNQ5Nb/rOgAWCb/SMALrvBrgGFzYNRRiYPvtU9VEUsLFfMr/D42HBJPeo0eUbVaUgbdhJEMTZ3nrHpATQBqOvSCtxA8inYUbMYYclbIf0ntL0YHeVTpgBUaPDXBkqqM6MPTEn3X9p6o6cA9oRhrxGiPTJhxsaVrIqybh6jfFpxkKPBHMqcXcUPtJsprzCuH0yFssAfgSqdOjWzPwOblGxbcGKqV2MzYqOVgoCM1rx7drkQxxbdPq3O9naU61G1hwLInNgBSyfzcnyZmzICcfqZCdQB8LzBmvJkBJ0heTdzqQJoJICfInD1BVchIN6uaMkttFS6vkXCPygX7RHbp0ewHCr4IO0ljwhgMuOgpXf3cTp6d3YUllRyWqpLc8VBkXEUX+YWJ2obGWOTSpTJiZsgOyA0T+kl1mpMy5XptLewDx5MiepU5PVAZHrejYmp32ePQy4deNEB9NB7iDZontJFHAKrk2O268zlTrSrNsfd3veP69aTrD3uTW9SWck2HU5cYCgq9fNVJE5FJJ2s3LWRbKQ+o/lBu5Q5MegKyAeRW4M1KJYcr6aL/TvLDKQGJIevG1TkdQzHSn3FmFdd+5C3wZct7R0pnN06181JZsjHYMC3wYyf8sikVwNqlP4XGRrZgm2uqswrjb+WCztdcqDvGxs7ZBmyNpxndVvn4H+4cmMNkVCPzPYvazN1ZY+pgCraDWT8UIl2CXV9Qcyg6aUm9v0/3OFgVDN/ylchI0gtxtt4ksjavaKvvNRKDsdhzOnpx/DMMudQSRaqd6+SJznYizuOPrM5IQMDu1/WpbB0fiOZ83p5GLAMPaL7DzOZ8zPjRDwooRWyWqgm688RcaM+RUTlthcSYHOVlxBFAq7O3PiJt6ZJBu+Z05cSYmKMRY7A2T/4ZxvzXiJ2L9EuvqEW6BYXOvqevZ2bGrGvN1+nxOHo85xZtQAJI03V18iWzZMBx36ZDGyCK/eYs/yWXpHVsO1T0vwd3yLmwI+kmmvue3PaeQxsyvS5cmLH1OTESGGLkHjcTVnSSupulPV9Sox5QQy6iT2Pf95HL+HZsQyCmYp7tSiwwutpb8NYDQc6MRi32WyAeL+O9x/x3JpZQmRjdEU22mtv8zG3ca6zXkXpfc1fMn+cn5PJlXVQga7JG48GIKVTsNxOmsIsN4BY2EcjVZ4iNsJQQQJmYjcjYzYlLPttNk3aufpKjYiLJMqAdRoGvPiIQP6dqm1MFq9jALjSaP6TAg3q5gq7vcx1Avj5gALW5J8xlBVgaszA22/nmFydQ7mAH9u97zC2PO0PNE7mC6NkfaATio3sR5EJNEV2jBztX6TKhYQCiFt+BLKmwF/eNiTYDsJ0hHLY8QcC+317fWZ5XFk1zOow4MjFg2pqX+8XGhyOHygFQA1X3PE6+pwFsKqGC0x3I2Hn95zdRlCKAhSlHLdzOO/0348rq/dlJBuaVy5ULP7d7tbHeadZ4w4lu9plrWLur3qZGq/pDjrWAdgdrlBchXIQkr2lix/hKK0p/KfJH9/Egw93t38SmPH6imnAIoaTuTv2AkoGEKz1lYqlHgd+0tjx4vTVxkIYEhqBO18/AkD7Cauwef8AqMmU4yDQYDsRt94oozYUysVvIt+2/bf1E6MWZMlgYUZmI04v6b8zgJs328CENRBBNg7RZoq94siMWVu5Ub6fgwMysSaoMeAeIthrZiSx7kxaN8xBYOWxHGAK54iZAAwG+ofmOq7iqdPwZ0Y8S5kYnKqFRa2PzHx8R4D0jomX1MieooBsHb6QjqHNWeOADOZiQa7woQCC3HepUehhw9TmAyEEJZGuwP7zsXBj1sXOkA1uL+k83L1ZYenj1rhU2FJv6ffmd/TdQKC5iMasaUkgzHftaXXSm2rG192BFzZTagkV/wDygwKcOZ20MSFNWBz81CcmRbILV4O8suimJW9vt1r3I5qWKqrC0ZVPnmpz5uoQaBiTevezCrPwOwnT0z4MuKsusleAtbfrG2HSGcB9kDVVjab0z6Ppqace4qf9yubJixh2CFRey6uR8+JJ8nRsFdGyqzci70ySmD0DZFy1alBsbbb6z1tWgEgfeeOMPSszIvUtVWCVG58SjYspp/4gbb1vFsF26w/xKodwdp1hNVX28zxfSDZRWZSSLvtPRw5OpwoFBR9II37xsgt1AGJbFav1nAc+Rsllu3Al+pZ8rJeKiSAaf83+onT4kVWfNiZqYrpBmevarp6RznBJRdvE6Rj3r8s4+jy9PhZ2LED/AIsNxO3D1WHJRx5ce/6zeoqo4u/0lFNbARRk8sIwawCCI0UFVuK+IRXiJq37QqwIvUtfWNFLE1yK58TCxkUg/MgPxDGMrI4Kr/S3mPqGO25p5Gb8VyLm/l40OMc225lOn/F8TjTmGhq57H/UaPTMUjacz9bjA2sxF64NqFA0e3b6zP3Fx1UDJsAPic2bO2RdAJUE7lTR/WMcupfdv/mZv8kMLkyocS5CrMD/AMd5IjVjJxobI2DCpYZFrYLVeIpa6Aapi86uJ4lYYgr4wGA5FVOXN02Vyx0JRHJNV8ztPUBX0a11VxOXNnViVfIB/wDym7k426UmLI2NUUNhVbFgNZI7zoyisICvvzYAFnzPPythUEqLY9ieJbo+oOjTkxnYbV3m7xubCU56fG66jk93cWDZiv0IP5Tp+eRHyBcg0e1XqyPHxciWzYSFDBqP5blm/wBom3R5V32b6TJjyC62udmNldxuEYncntO0fh7GjrUj4HM6cdvqV5OMviIK7MJ1dOzG9RBLHckXU6sfR6ntsYIHkESrdPhxKXYFR8S9HbnzKuPCNXUgDwF3k+iU9QztYZBwdQBH1Eh12UFwcXC7b1dzl6fI+DIXXxUD1OpX01U4/cTuO85c2Rn1ZGf3Abm+D2g9cZD/ADGJLfPELsuHERS+5Cd/nYTHtVHojr6pGyvqONCx7yeZgqs7BhkyN7hf5V7CdPShMfRO39TC2NfoJ5+Y+rb1pvc73vNT0/AZht3i4kbLlGNKFiySaAA7xCdgT4hyIy4tVElxXHA5lRnbCHC63YclgJ0HHjyq2lrIUemAeAP7zgNbGtv1mLnSE4F3FlTT+mxzaLVdtyTsPrOnDpw4Mjgo7AgA1f6fM5MeQIwYi64+sZB7DqfSpPAigO5Zixq/AFSZNiHvLDAqYVfIxUn+mt67GXZBseBTiDblieCaAkclhqJBradC5UOIYlWjYo97kGoMSfc0k1U6M9L8G6YZupyjIgZRiOx432nnruTPZ/AGBXqrIBIX9N9o5eE9dnSgJ6nWMoXViAcdhpsTw+kxNn6ldtaKCzUuy1Zqeq7auh6nH0zOSSMrqB+Wzx/mT/DMGROnfIHKes9sP/qJz/G/15HWr6b6MjW/LBRW533kGyM2JMI3VWJ+5l/xAOMi69yRrJqrLb/2qcnf6TpxnTF9agB8wadeQAmhXMcsD7q28RQd7HaaZMyhKVbs7kiTxodXuFjt8yo59xI+YCQrGv2Momb1EH67RbNbRjyT5iDmQHtvKA7UNvMUbiPjUQMo3qo9b7/aahf0jCzzAC77RxhDHY7zaCpIYURtUvjUBVNcnmNCNioio+HCSeI7ZUUABtW+4Xt9Z1dHkwPmZQykqpOkyfUzVwEwEFQB+bj5nP1TZF6i8TEhTqUA73/ududgwNGhQPzU83qM6glx3vYCcvr6rWYtle8ykEKvfvU818hD2FLhW3c7hfpHUtkRlLBU5FCz/wCpFy3p22QWPaF+KlkxNTOZDkLuPUJJOngD6+Zohc8IAAdiFE03iIKSDdA7VvADvtzBNNB1ZlcMCQRxXaFWZWBDbg3YPeBW9mnaubqVy4gEGTHtjNABmGq65rxdyChw4mxBkys+StTqFND4B7mc9Eqa7czpvDkyroJxk7szbjjfYTnYgrqLbk8VJAt8Cprgv7zbiv8Ac0GBIh1UKG3+ZlLqCwHbckXUOHI+PIHStQ2FgH+8g25PzUIYgVyPiZmBoKAPbR27/wDnedHTqh0EYXyhf/k22/62jRzlmY7mzxGZdIBAP3nd1HS10f8AFJ6GLHqvGA1v9D9JxLkL5FZmtxxe/wBLvtJLoVOdjKrjZlLKpZUHurtN6jq+TbGGJptIH6CdvT9aU6lBkCrjYU2NECgeD/mLaLdBi6heqVMhatFjft/mehl6fKw0hiQf+QEfpxjwnIwckO+rfetvM6QQQDYpxYphxON5XWpHDk6VFUes7A1QAF3H6UdKH0g5DfnidD4sYBBxijzZuTDIm4VRQoURH1cMNmxrktMQRATsxFtXjxDj6fEqnWMTeLXic+Tq0BoG7+dpL+NPAsAd6jOVOnoUiD2DGf8A/Gor5EAtiFH2nmHq8pJPqGh88ybdQXH81mahsQaqX/zpr0hj6XKQ4dbPOwhZsatX8SBfmeQzll2yEgdjEB9wu6B3mv8Az/2fT1eo6gLpQsCbvVXiJ/EC3De7EzavYbr6iecTZveYWDYO/wAS/wDnE17WLH0mRbxorbXsLlUXHi3RAh86QDPGx5GV9d0R42uX/jnZefsef1mLwq7HqHMoos4H1NRG6zGp/OP1nHk9HSurJjN7mhTD9NpzB2VtAyWL2NVE/jlLXpfxyH2g1fBq5Pqs+XEb9U7/APmwE5em6o4lY6Q2RuGPK/SP0+V+oGf1lGasZI19j9e01P45Km6yErWXIGoj2/JhewnqOhDEUnfSPn5idQvppiZcmpK2KjdT4M59TURqJEvzd00+rxFveyJq4NVBW86MrrmZcVK7DatjK9J1ZDDGwG/9RG85KozV7qJ2mLxlXXoHrB6oAZSCeaqVTPkyGseki99+J54XGVxozaRyxG8fEoxMWOcVVjTufj6TPzF12r1F+0rR8E1Ed9eFmAKm9tjOVi+cltVkDezGxNkzPpBZSosm9gPpHzIunxWRepRX9XMwXGdwxdx4NRiArsuNFO3uIarlHxq4BG48gXIODLsxBXT8SnRsqdQjOTse0LIACSdztR5ETRQvxN+zEXfKPVZlsG97PP6RjlLaW2sHmc1XxKp+UiMHodJgRicuY6rXWBxf1nRg61QCANJ+Tc5jaAKOAAP2kcaFnIWaNevhz5M9hSFo1c4etz5chC37QTz3qd3SYThwMzijud+08nMwsWb2/TeZvqolgRTJQHa+/wAwMFC+22238faPmUl78iz8SIsHkyoKrqyKACbIE6silerzZWQWmKgnknYD9pBXGBxlbUFHFdz4E6ULP+Gg5Va2vVXNLczyqxwtkY9Mpy5D7iWCjv2ucxb2G4Xawf8AyohG1X2mkMmP1KHNLqI8/Ep1pKfyCtN+Zt+57fpQj9KVTKpVbZF/qPLmgPsLjfiHSMOpykOppTkI+Bt+8n6udPOfSppCSB3Mm2/eUveiLuJdbzbBVJ1Cub7xmsbGvsZMm2PzMeYDqSb3radPU5Q2kKwIIBsCt6nIDRjckAmhcmdq6elwsScmoYwBsxHJO1CbJkVQcWM2BYvzNlyuv8lLCbEWfEi7AkaQFA+JnNu1TFFYkilrxvc7PwvHkXJnQvoBx6gR5B2nFjYAE8FeCP8AM9H8OzHGuXI/upf1s7Rd8J67/wAL/ldN1WSgHLsQL5of7MDHAuZOk6jIxVMYBRTyzGtyPvJIyZfwjFiwn+ZkcIxB3Fkk2foIM2fHj6XqvSCJp9mM9+N6/czGdtvC6zKc/V5ctUGbYeBwP2nPyaG0q5FfMRa3PG3M7OZX/LVjb94+ALdNZPgcV8xH3I/eFjZHaEBiy1cI2Wu/MFamhRWyOQiliBe3iApg077ievn6fpRiRxi9M1/VYv6iL1OH1mU5XRMaJ+ZRudtqH6TlP5JWvl5gEdQfpKOitk/ko+kmlB3Mtl6V8AU5ANx+h8TpsTEUWWXE+/tIWuSI2PEWQsTSjaz58QsmhTqcEqbo2R8fWTlywxLLlOIe4WvbeOmcZQFT8wF+L+si2dc3sfZjsTwJBsWVcbFQ2kCz2mP+qXM5GQ+GOxG1iUPUF3AW0AFKAeJzACiSx1A7AjmNp9hN7g1U3kR2Y+rJQqzEtf5id68QFMhyWiEgdm8SfSg429UgEfXeMcz5H0kA7UtGgDMZ30rZlCItZRqN6gBwfE5GBfirA37RupYnIRQBHNSSjWSLqhc3IimbF6LLTajzY27TRcq5NQ9p34rf9JpRzD5hWr+IdB31e2he/eZBdyjMKMw3O8arNbxTV7QHDHIyKgCkgLzU9D/8WcWPXna1J9rIbU/QyHQpjZtOqshGxLAC5Reubp30+3KpJNXX9pi27kUMnS4Vx5CCF0iwSf7fWcWhrG1Dm6lhlLkMwJrgAbCHI4c6uLFaQT+81JUMMQy42YMWyXWlUsAebuc7IUYqeZ14upZWX8vtHjn9ImUFnOqyzGyON/pJOhAEWb/adKZG6cg4ch962Sp2MjkwutsQCoNbGTRbuX0eg6HqcJyt7nO5a/8AE4tFNR2F7zu/D86eoMWTYE+0mVzdKj5rIIZn3ZDe3mpncHNi6ag1BTY2Y8TtVMZUlsa2TZ+sth6dxiQNjBIG+jc15h9bFkKqw9PGCfyjcSfUXCM+1kD6TYrNFnAXxcdxhfIExBiw+bv6QaMaY21l9bflH5aHkxsCv1Dljqcn6cGKmemNAKK7QFRtqNj4gKDs32msiaJId9VBgORXaNrD0te0HjvJorBtgSO9bRyyLk9tpR5gSbZtuJqlsjYclsCwP638yRV1vUp+ssoRhv8AEHaNfzMRa3KjA7V2m42MAFHmMPBlBA+ICKNwgVuJhzAO4E7Pw7P6bNjYqVycK6ggH5/tOMAGYCj9JB6PUjDk9v8ADrhyA0dBI3+k4lteNjxOnHl9ZNOW2YGlbvJdQunJY4bf6eYsF+hcgZcOxXIvDCxYnIRpaj+kr07FcobxB1S3lD/8jAfL7sCOd2XY/wCJAcyq/wDxFTvuP0k1FGFY7EbQb6voIxm094QLoioQpLb3MRuBCho32gEF007/AJTt8S5zHTTopJ5Yjc/eSNgV5mQG65EzijrOo79u8uuUlbACg7HTsYjYV9EOHGo9rjYxQ0gg33riTqqpTHc+9e1i4rIG3UUPErisgm9zyJUY99wPtIrkKUaIlMWP+YF53l2wkkHvOjp+mJYNxNJhHW9W3iV6DCF15XGymh8mdP8ADWCQefMoMOnEFB72ZQvUdQiqV5JsGeJlB2G9T0OoxspJPe5yZVJOleQOTxM2q522U2wBOws8wocQIUg5HPYdoMyo6tqJfRyFFX9IuTVi6ZmC6L5A+eBMbvQnnz+rkYAgqGGkEbA8CV/EMronpqxFgAqPE4qLY0UCizE2TtQjZ8zZsmt9yaHE18mpObOwoeB2lOlCZM6jIQMYNuT/AMRzFZdS/be5fosJODPk03o088c3/ia3pB6fOidWce3prkL2R4/6E5+qzPmzPkY2WO8r02NsgyuRQyOFPk34+85cxX1W0AhRsLNxPSpctvMAK3q5u4isbmmSzQ14mo3uKgCUxoW3okfEUCyAO8u6tjxhXa17aZLVYFdBCmhXuJF7w48QK6iATViztX0mxYsj4wWYJj55/wAQAKCy4yW+DM/8AOM/8RR4oyqMen6ZwGKu9WKv2/7idMgbKAz2xYAUa/eL1W/WatQZSbB8jtKr1+kzYn6IE41RBbEg8AbC/mcf4pl0+mg4YM/0B2r+8hhzjN0enKo04zZRfbqFHc18zlyZGyPb0DQAobACSTtbeitvtE4JJ4Ea7NGT1an08AmbZMFoWRve8T+qVY+0xMY2vzKjcLvL9FmOHMKZgCR7QaBPzMOmyvjV1AfVdBTZFeREbE2LQcgrWLA71MXLMXx6GfIGrGMgd99V7/bbaTxKCjb0q7m4+M4fQJ6bEGIFEmg33kHbW5VVKkkmqnKT8aru6XLWRWyZAw3IAHfvJ9RkbqHJC6Vq9u+85bK2HGlhtUbE2RmCLvfAJiccv0b+Kv6YQKqAdiT3kOpxZCrMjMSKNd51VkfI2MnGRstngD48Q9QuLp0CMxbKuzEMRQ+kz9dmPF1Pr1m9QO5MfM+YZCCGVRyCdxU6mONQGzsQG/IAdgPJnH6ra2bKS2vc77mdZ2jYtD5BZoqNVn44G8t0oXK2QsUCiyVNi/vOIrqIruY6NlxMVVmHffb7y2f0kdeVhqCLrO2wI3r7zlGUhwwJ2PER8rM2okkn5i3ZFmWTB0qhZD6i0OSzGrjJhx+nXbuRyTKYsmJV0rqockn95z5Mljk7m9/+pO6HVMabj1L45qaRGQlvfv32PE0YOZNOr3LYrYX3lNNUBv2Fd4uN/TJpQ1+YlltpoMWINCAKfNS+HAThbIKIWr34my4vTbQ1hgNxGwTVa32P1l3xppRsZYahuG2o/XxJY1XVeTVpH/Hky75f+Chce+lCbqShcekllNgkbDmzEvf4mZiQpJO3HxOjEqMC2IqjY8dv6jA6j307RuCeMACyQb2+k6ejwYW6kjJmbF7SQw5B7Tl2VBRs3x4lMLENSvTHYn4Mt7g686YHYAgK1AISa38n67zm/hR6YONtTVbKB+UdrMbOiYcior60H5WAokSuO82ZTiGnKBtoFbAf65mZMgknSMWQhtNqNIPPM68T5QCGJY34/N9Z04BmGNC9EYwWDd1E5M2Qrm1qwUkb+Tf+Zjfq4vjoxZQUfFr0bbiufj4E4z1L+oS+5PMfM9spBFDcWauIz2y5F/OTvtZJ82ZZCvSTqSyoye57500o23FSTdSrUmKtmBJbe/mvE4MeQo4vi6Mtkz6M7NhpQbGx3rvxM/GU1VhqOoY9N9u0RlGsiip8CUw57X1NKHSRa3sftKeu6g5KxtbEqq0aM19WdGJAMlgMFPcGxAzuAC26XzzC4OVBkZxdfl8CR70Samp3EXRsLUGVbI+/7SdhmJvTvsL2ETXWwFD4mJRvzJXipcwOdLMS3ccrUUqVq/t8xVG/m4bC/wBN/WaGmhUa29i7+BCRRo7GEZefNzVR8TCMN4GAhrc1zMNqh4MB8X5+anRlQ5cdqLK7/aQTdrPM6MBKuB2uUcyAhtpdxqRx/wATBkx6MhWWRCddVuJByTduJX0WPEwTyJFT02LmAIFcywQGgI/pUDYgQC6viDRRnSMdbiZsd7wIcjeZSVYEciVGKEYSeBAk1liR3lEBB4ll6ckXUsnTk1Q4kVLGD25M6sX5SDzCnTG6qdK9KZFBaoFq+86sSi7E8f8AEMr48/p8AAbzr6XrPT6djnNsp8ybnotk61V69OnAu+T4MPUdfiwNpA1tXYzzOqzasnq4lXU/f4nPly4lNlC97We8n1b4L5eu6rK5CCwxsACwPvOV2yZ8hRm0/Te465WzhloInAJMoCFICJQrkGtpm2wFcaYk9wNL+UE7sfJnP1HVWpUMDjJ9ygd/rOhmIQZtI0Xpvv8AYTznwH+HDBgW3YjwO33+JePHe6GdlXo8dqNTWVOrgX4+ZzqdTVxZjZ8hcpf5VUADxUTGGZwFBLE7AToi+QBbQMGJ8dzPSCJ04HS5T7Qhc/8A2PcfrOHBgZM4ysQ5x+4gcX2F/WdHW9PkPW4MWsszoNRPYA2ZnqqicmjosSUCwdjXnbv+sjgwaQcmVKAIrVxO7rcKsymxpCgEcaTz/mcPU5t9O2248mS3eoJZDiYsAlb3sOf9TmYAv7dweBzDkcs93R7x+mwNmckNpr9Zuf4xn1TB06nGMuUkKxIUDbjk34kX06zRbbzvO9/TxY1AsnRy54HipwtkNaAaUm+JnjbboGNXbKoxj3XtUrl6aqbJlsnmtx+vedHSBceH1XCk3SsZLMwfJ+YaeAI3b0A9KB7juNg3iKMGU2QhFmvEYo38UAAtr5PEVs+RSQHscWIn+hfB0rrnxgupKnVpG9AbmEjFl6kKuIHSulRfzd/pOjH038P+GZMuRdWXMoUL4B4v6zYEQ58xw6X/AJVEg8MaBr95NxqPM9MF8+lvUVV1BlFDkf7kGG/0ns4cOPp8PXZK/wD19QSm3LDsB9/7TyGFMRd1OkupYmdiJEGm1SmSwR+0mOZplQnWaEviCrkxlkDre6k0DI4ltqnRqVHUuusA7rdXJfB6ObKi4wuNA2mwSu4Hiv8Ac5FyKrByWOU8l96+kVupbOzMxVKAraz9I7Y7TWTYPczjx4561af+KLEgJV+Nr+TINaGzyb4O4lUxkgrsBtd7SnoVRALadz4mpJBzrjB5J0+a4lMGbDiBJYFgNtQqh9e/0gygnCEYFfaCARQI83OLIrUd/ap2I8yXvqjow5Uy9QSh0pZLJW1DzI5CWyOw2VDdlTOZXZTpFgtz8wHKdLLrsar2/ql+ezVeoyMzFWOrGDsef0MnlUemCvtFcHv/ALiNmagDvQ2sQKVY+9qFXsJZMQMeTTewI8GZnZyS7k/Ji6aGrt5jE2gG23E0E7xlNLQr/MDK45BFjvFNiUdLZm9EIC2iv3kST23gR65AP1gJNijtzRkkwNsp1Ahq7GaAFA11YHk1c0CVXKKAAK57xL8QiUXTKVXTuUJsqDyZfNnyZQcxxKMQtQNNUT/czjBo8ymPKyAekSGskm9iPFTOQlHI2M41OMUb3s7mTD0eYGYMfdX2ELhTbY70+GIJE14Hdf5KOGTcHYHcfWbDkK7EBlOxU7CDHk2ZSAdS1uOPpAfaSoIPyJMDMBaiwe+0NkNYFDi5LVR8fIjhr2JvwZUVAbIBpW6G9CUTD1D6Tjwtpb8ukXdcmSRhob3ENewA5+8COVewaPkHeS6PXw5864GxPjdWce/Ib4hZGVXfHhYB13Z1sr5F/wCZ5uL1HyBceptTDUASL+s9A+pjz6sy6hqogDauxHn7zjZl6bjjDBbKqWZT7jVioTkVgGKkUPd2jZETXkKhVUvXt34/xvJlSraSCQdxtyJ0jIDW1AnY9uIxR9BNcHiN6b7nSduduPrKMyvlLVp8BNgJQvTcizpprs9o+RQhD4Sd9j2iEFXpRYJ5HBh1alBF/Sv7yZvYqCVWle1IHBsRCLlcKYyqrqAWtyeb8Sq9MpHqI5CDuRV/SPqQxyETVLNpI0qGU3y21ybIy8jaa1CgUIxozTfSUYMVMIyMRpZrEEPHIkD6H0BipA81MFv4gAb+k3Q4jKGHuZSB8y6plH943p7R1Heto6j5EqExpuAZ0DGQR5Ey6AbO8sMp29t/WAMmLVRqPjxEHjmUxZFOxEuMYPBkwQOEeOZJsB8b+Z2FHApd5J1yqd0P6QuuZcNHehKMn3mZCdyCIhYqt8/eQOMYjjED3Ak/WxqgctsRYs7ySdWCSGF/8QPMku0da4Md75APtLY+nxX+bVI4ypADnTexB7Hxc6VQ6q7+AZQfQVeCZx9R1jdNnGPHuwPunVkf0t3fT9Z4vVBnzOyWdxx3uZ1X0HT9UMmJXJFHzDk6/Cg2IJ/QTwcORwDYYhaHFD/uTy5QMmgA5LO+25+BM7fwdnVZz1WcZEVQFNaiNh/uQy5kAAZhka9l4mDOcTWoxogJ+/j6ziDlcgJAbe7I3/WJx011tmIosasmxf8Aac2b3HUO/O8bHodjrsKN6lsWFMjhQ5rmgN5ZJBsKqNKk+4cggVLMcmTLoxICeAT+USGRvSxsEQoTe53IHb6SOPJlUqRlqm17n8vzJ872rv67CuHpnx3b5HWj48zixrk9P0sZ9pJ1WOewnTlrqAFGQnEo1GzZs838wu+PAgVa1diRdSXlnUHn/wAO/qsrA0Bfi5VWxdKq6UD5qsse3wIjdQxyUSADztKFUJDKoZyQQ3Ji7fUdKZQqYznQqpbdQPH/AGZ0dUrPn9VTSYxpG/Ldx9pTqMYw/helFV3Tknsb7feRxlQuPAKIRTqBP5jyY88ac3VZwgpDqvm6/N3P+JwdQraVcnciyJRwT1JOjUCTSjg/9S46cJ06+pTEG6vaXrinrzceN8rUikmeifWTAFy6QQKrV+nEmmUorogoKxJ2nLky6rDWR23i7yTwMhbVWRmtdhEC6qCm2JoCKTcAO86Yy7GGjCqZNmGxBPHzOY6hRP2j+ov9QL1sLPaJpJPEkmKuOppR7Rffad/4bhw9SCwwVpIB35PYTixpgC+73tPd6LFi6bpkVsdM384pe+3E58saiP4lbr6eA/0l3YntxZ/QzzuhzYMHWgOxZCtEkVvz/wBSobLlxPkVGY5cmnGD/wAV4H6n9pw58OTpnCsqkg8jej4uak6wteh+IZsj/hWJMiqjNlawPA4/vPJ+3PE7cOT+KXMM/NlxW3uJAP8A6k8nTFAGNV8TU6S99uR0B38SKijOlgd6BBHaTTGWagJtlsWxuUcEtvKY8egWRZ8RiGcmhsBvAXpsYOUM1aV9xB7/ABPQx3kwqOCa0pXe4vSYaxOx9yldwPN8T0fwrph/ErnyADHjGlBfLGceV3k1PEB/CY1J6q3y3XpKf8956K9NjbpgF6IoGB/NvX1nP0PRY8Gd+pygFkYqgPA+b+k2T8XVm0nUBdaVFsfoJLzyZFxLr/w9F6EY8Z15goBJOxnhMuR8ZwuSgTbSGsXPd6h8js2OtQrTQ2Nnye08TrMQTJ6eIWwFkje5z48rb2tmPO6hWOQ6xv3iYgmsjIQFo7m/8TrOP2gllDOa3PecTfG5ved5dYbTalvHPxDiyelZX8/A8fMnfmYEE0dhNYGYjTRYmuIyJr/Paj6cw5cRRyugjvuKNfMSwWo9+/iBbK5Ip3LbDfzW0568bmUyDHVJqJHcirkgSLEQa4DVbQk3yYDY2MoH2mmO80BitcUJhQoTDn4mJ87wgHdjvD9xCNxuPoTEYU3tEBiLFiMg3oqG1bCzVfMVasXRHeUDkp6e5W7C+DFVMqcbU4IPgiMVAF2D2qAn32dz31bwlaNCjXg2DIF0nmFKJod4dXANbQ0ASNr7DxKhgCu+n/qDiiRZ+YCW2N9o+NfUHyBe0CgzfyQhRTRu6o/S4vq5KKs7Ue1mAqNOoEX4AlAmvGz6vcoBIG/3Mz0r1MWfpunxoj6HxngAWbHf/G8iNTZBkPtIHtVd9Czkw6Qx9az3smq/7l8PUnD6dkHR2I3Fzn854usuY4maiGBO47N9Ypa8oTYE7fE6svSvmUNhwMu1m8inaufrOdOmYJqdwq7WeSAeDXcTU5T0w+N/UUJkuhsvaoWVBprJv/Uf8SOJ6Olzv2lgrO2lRZ5msTRVlvVSgi6FXc6DkbP05GrjYKZzD2n9oQZPk0osGiSB3l8Y1OoxkkAWQ3EmFDcnf5mCsDVTSKDGr3QKkHgDaTCkNQ3Pb5lFytoCihXmbEw1e8WDJ2oMukjcN5oRvUAr2ChK9RkFUigfNf2kkxnIwAr5MTzsMuYLuMag9towzv6isW4/SIcRX5HkQAEHaMhq+TqMmZxbrpG24lxiwt//AFK+gsTlGIkFl3UcmFWYMPdXz4kz+j/rqAxB9PqV8spnZgwdO9X1CNfied6hs17vkCo4fGB+cj4Ar94+uUMj2sfT4UohA/yTLqUXYIB9p4S9Tp9nvA7bxi7kAsG9Mmg5NCPq/wBLj3fVUePtEz52XAWwqMjdhc8A5FJFaj941+3a5pFeu6nPlCq+M4wNmABIv6zmLAYqLEm/yHj7xwWJoMx+LlPSyMmnSSBvuOJMNcOQs5tmszBe44ndjxAJ6gZNIPPO8bMq2GV0J23ZbFfAk+sq459GZ1A3YEewE7n6RcwbEwdcraxWpm7Hx5l0AGc5vUbUNhp9oA8RQMLZKBJJPAO0zeS4VczZELZAze7lr3ksufUvsbQf3iZs5vSrEL4qbEcWQ7gE+LqpJP2oU5Sx0l2KeSN50YUAF41tmU0eK+TFOHGpFK2Tfsa+0prXCgVaUvse+/iLZ+DncMmLQ2my1juagcnTp5UbfWNj6nS9MAQJRkbKbRSQBuTNy/2I4sRY2OBO/AujoGdGON2ej/8AYD/EHT41xZETIrFnO4G1CH8Wz+kgKKoKDSvYCLVkcjh8qu4/mFjYUDn5+kXKQtJhUZMmQBaAugOf3nFmyv6pByHHoFCl5Mnj6h8WX1Fbfv8AMdpr1aK41V6U3uU2A8SOUkuAdzV1X+oi+t1WJXIJXz894MmPPkyaVBQdrsTniuR21Mf2nR0LunWY6PB3s9oqYlfEzalUqu4J3Y32hZsXT51yYXVwD+Ugnb6zpv4j1TkY4TlY0Mr6caDgV3jp6SZMoF0UKIfjuSfkzgx/iOIIF0Db8tnYRk60u5BGkcKw7n7zF38jWurB0n8p/Uzf0/nrYfAnI+TFiZg2VmJHJA2+kbqOsdMAxodxt8zhfKzoquOPHeTjLe6lq3XPi1j0d1qj4M4SASdNn6yzZSz+6wK2Ij4saZX0mk25udJ1GfXLVTBbNCdp6LV/8Zdvmto69EVFMVG25qPuGVyaBjdbIJHNjYSjZtLEq4ZjydPA+J0np8aAjdj58SLHBhyemU3vckd5N1cW/DVXJ1+LEU1uzWxPAHJnpdUCetfNkvGBhZWI7SX4ap1nIoBIFAjmz/1cGBD1HVFMqtobLZJPAB/L/aZ9anS3UV+H/heFCp1unI5B5qeA65MpbMb97Hee5+Il83UIfU3Bal/470BOPqHAAUkawK4sD6S/WXIlcfS//IgYcNzc9odNpY5c4On+lq2YyydD0y4Ez9SjaiLCXVCdPUDp+n6MZLaiPaL/ANy3fSPIyYsWgUOfcCZy+nj1c/pOodYW6hT6Y0DdhQ3E2Tr2VmGEVjYfkUChH1iWIJhDsApH+pVenx41LB9R8VUivvylcdF+2k7gePrOjDgytj966DfBO9TPLlbMJDdAranQ0BWyzv6/X0nQpixgXdsSe8b8J6bRrdzq322j/ieQZsbYkGqua5v6TFnVv9tPE/ishsXpFflPE9Dpumx9P0+F8rK+TJ7gVIPPG/NSOH8PGM6CVyFgC3x4Eswf+Cd1IxrYWjVaV+ewuSZ+LHF1GRzlyKj6K2LUdzewnM2JkyL1GXVoxq1uvLHgAQ9Viy5M7+pl/l6iQU/LQ3NQ5HD9MqlgQRq50hV+fkx4V5XVbZNXuXbbULI+slgV3ZkxsL2IrueP7R82ZseNsSFXxsQSa3+lxMLVizXVNQo7XvO34wnnxNiO9MD+VxwfpId47kltz8RVFn95uIJJv+8HDA3CaI2mWxuvIlCk0bhsVsLMUzCA9gb7QE6tzzFOwghBmmaaFWXHZPuUX3JikC6PmG/kgf5ho2K3J8QhXRtiAQp4viKWDci62jFr81MtDfv8wJi/tGRiu4JBHcGqlCABQFE+JJ10nfaAW3F8Hz5msqRf7HmKrED/AHKjQa03ffbiADQBNb/EFir5MAY3vUZQp/LuT+0DWCfbfEoBoHa74uILA7DzCSSQCR9fEo6FVDYZqoWAZYdLkHRZMgxn2/mbxxtORDo3J2qjvzKr1LKrL+ZSpGknYX3HzM8pfxYX1PUKIBT/AJeJ2DEgpNAH/IkXXzOTUjozMCuSwQRxKY3YgkEm+T5mbB3Ys2bpcg1j1MfBvcH7zdVkbKw0ldLmgvGkDgX4kMWZ9Jxt7kbkH+86OnxKrH1T+etNixQ4Nd5zzLta9RwYUzuCwK6TTVuROoouJSQtEVZb/EuBjxk/lJI94Ud//U5+pVGINHiyQOY+raZieU68pbSAvleINJJ2EkCVbQVtidt9p39Pp0+/ERlFgdtxN3l8xnNTxdK+SqKgFdVk7ASJFHm/mdiZWTEFVGCsaeiASfiSbHjUrrDAkE0CN/H0knP+1xAC+YxLHk32hC6jaqQK+s1Tp6jWSK7DgR8QK+5W0n67wARwNoDox3A3J4MOQHX7lCn4iqstW1AfepMEQISvxH0w1KEANbAzFD4lUW4zAA1KmpKpLDURt5nV1pOVlVSmkKAFxqaEiVoXGZqqjvBquHp8HokZHzKfzEjEaEges9D2YEAUHbWoJP1jfxOUAj1GIO1E2JyPjFki6+TIuun+NzMQVJDdggqvsInWdZkdhbHVVUd6kMepcwYHf54kmBveT5hqmFyob3FQeTzUo2YKlWMjHk2ZzbVU3Jj5NdIyNmARQFra+wEIxnDjYKhOQ8knerqDpkOOshvUxAxr2bfvN1AdcxLZNIJo0bmLO8iubJQ21Ete/iTEckBy2MHTdb8x10ZA1aQea+PrN7iOnomVE1KpyZK3F8f+eZz9Q5bMQ7AVv7R3nQtYMQXGNRq3IPfxIZ1L4Swqzudpie6qYDY2JZRfaxe86ei6z0cjHMdStvvub+JLBgZ8HqOjaDspurP+ZTL+F5XpC6Ios6jz+kvLlJ6SV2fxw6jKXDDi6WcXWdU5yJjyBVSw3FkS+BenwYNGA+pkbY5P9TyeqJGXSVKhRQB587zHDOXLWr4XNkbLkd2PJuvEmeLoTEkDcc/2gJqruiPE7sL4eqy4U04m073Y5lH67NkTSW55I5M4iaYiNiynGwZaseRczeM9Nem2TAuAOcSCxsALP1M4mRHs42aydgRFbOzcsSPmTLWP8SceOFojx3l/aq16lntXEgG9tEX4mE0jsTPl9Sgnqhvy6xvUfL1OE4mU4ac8bDaS6Rsa5QWxaiBsL5MojYcrZc+cjTelUxiixmP1o3R5WalA1AbBasy+d+oTIVXAqGtW9EgTlXrcvTB0waVH5QfzFR8GS1jKz0GOobXuY+e9NdePrSzhWULe1gxs5ddJDhrNaQNzOBVYZApBA7hoUzMmYZAbI8x8T8NdLZcjGjqC/SNiHrn+aulVN6h3PzIvlA1MT7mHYwrlbKVW6F8CPmmvaxMMOLUlJjUEmhW8fpj62g4FC+mNRYmxqPb52/vH/EBh6foGxKBq0+3Uea/vOHpcmXFp6KwoIJdu4NWf9TE442PXjKc6ZaUIznSeTXcmVXoR1PWYrb2q11W2kS+TGGUYF3CqK+AebnX038vAW7qK28yf/qYlKo/iutyat8WMgH5rtJfjJUjECPcb/SdeFBixpiXljqY/uZ5X43kJ/EMSAmh2+Z15eMx53tZkGJa2qq7TjxIeo6oYqZtTXqLUCBO7T6eAanGNm9oI7X2g6XGcfS5coQu+y79h3oTnLk1VE6vZU6ZQFPLLzLjVlwZAmQlhQrjaQwi0LZWqud60A9p3fhYxtn9NBWMi1NEXMZ30r0fwzGcXQJrJ1Eajc5/xDFnVhnwE6iNwq7/rPSYWtD6Rcv8A8TfSduc2JHifhoytiL9Q5WiQoqtVckyWZcxxZMaFKXEKGoHVbWTX+5dMv89qYHQpUCvuZ5X4hlDdQ7DZm/MV2H0nGZ+NFZyqHXlJYe0o29g7bVPPyuFyjXqJUUykduKqY5snqBiLKjg+IMzXjxsDTCt14X/udJGbQ6xArhFVBX9AJY/U/MVujZenUtpx7ElmbnfYV5k031MDeUm7J2A7m4M+Q5HOttRGwPabm+Ik60BRsdz2ubCgfMisaUnc3W0KNT2eO4E78f8AD2DkRNV2FHAHg+ZbcRy9QuNWKqpFfaQJ57Tp6tqytTBiRuZzLjZ3ChS1+IngRgKsfpFBriVzFgWXIBqBo/EQAEWTNAAwQsKMwHmBjxNMTNAZDtR3jcHY/wDUj3EoCQvMocNsdU2kMLuoFIN3MCRe8yG/ooV5vuICCcd1ZPEqrAqNu+3beYgjH6aruTve2/iEc2m97mBIIo19I7A2djY5gAvtKEA32lFBuv3ig1cZTt9YDlWIBHj9ZMUtXz3Epr0JpI35sciI7BmBoA9/mUHXV2d+LhAOx7fWTUkNYoH+8ZW2qBR9W5797lOlcLlFrqBO48xKd2tnLUOTCuSnDEDUD4kzoex02PE/WLkR1AFsynYfaV9fGjFFdmG9EbnfmJhwDPh/i8uVAH/Kqjg/MC+jhzHLhybg0Tq5+KAnm6tb8cylnb2uRQ5rn4+I2bP6oRBQC37vg9p0PTqC2ZQAL70Sex+ZzDCyqGvTT6SOSAe86SxDYsgtAy6mBta/t9JmzEuoIIUbaRtYi58fosDYI2PO4+sRSHuzRPFnavmayXtFRnZMxDe4KdgTYnVa5UOQ5WC8sNO9zz9JNE1V19Y2soTbWDtXeS8DXScyY/bhsqdzZi5MuNCDuQfB4vzI4cpw9STpJUggrfY9jH6jPkdlxWoRVHP+ZMsqugVQo79xXEcCSHUdOvToxRlNUSOLlQQQCDYM3xupTA1G1ScYTSHBhuIB81CNztAvi33i5Wt7gQ0Dtv5gBJayBZgPrsaRfzJ5GFgDfaM3tPP6doGUsBaWDwRAlcFxnQhq0kSfEDGBxYh/WYwiUoiIVthMUsWIyCgRJVX1sxRrBIHJE43QkqCbAHB7Sy7bQHcmZnS65ihj4umyOpdaAHNxyJ34epVdC4sRNcD/AM5+80REYzlYqjbsdTEcCcvU9SF/k4001yb3+pnp9VnA6dtZXHRv2gW32E8DNkDdS7YhpDXQJ4nPj21enY34nnw41DP6jnfngdhtHx5vVwlusLnwh9oP+55uFUbMvqkhbsyvVM3qh3soeK2mbwmmul/xBcT3jRQTvYFTkyZEy49kpzvf/c53JO5NwKaIvedJwk8Z1TIjknUgWlBr4iMS9ajuOIzEuxdiCTFF6quaCkwdoSu187xTzKhowk7oxgdt4DxgpH5rEVG3BAhsEbQHdQFBVrB/X9I2LOyLpIBXsPnzJEltzNW3MKLksbJs+Zfo+obpOoXKBe1c1tIEQNuv0j0ej1zKfejLkZmLepe5B7V8ThIoxkYNRPMxESYMu+36Tq6Hp36nqURBtqFnwJxg1PS/Cc//AO1jx1QojbkmpL4R3fiucP0bEoASAQ1cDVsPrtc34djTGgz5W97pr1MON/8A1Ob8X0GsYOlwxLINxfm/NVLZs64tGHKpcqBVbVtUxnTe9uvrC64XGGyH3Y+K7TtVSnR4lyNbbM3zOHo+pXNlGMWaJofE9PIurIoHAO/0jilHACzFz22E+d/Gsrfx2qrri59Ivtxbn5ny/wCJD1eppAST28zbJFPq5sOg3ixjWRVcdo+XqBhZ1x0Mem//AOZj8xk6cp0YQuurecWbBkLJjStLH23z95xmVp1dM2PH0qOylgxNiuD2H/c9b8FLZycrqo07Cp5mBcXT41XqGV1SxoXueZ7n4SUbpdSLps7j5muMm6O0mhc5PxXNkwdHrxkDejYnVuVFdzPK/wD4hyEYFT5szd7iPLGamyZkNg7Fe1zibIfU1KGLZLA2/wASx61CxwqCRVBj/mcfVHLrVXv8tL9L5nDjx77atc+ddf5TdAX20/HzLFj03Tqg0uW911uK/wASuLqMOPCEVfUyNu2obA+LnJkwu75DwwOy3ZJ8CbnfqIZShIZAb5JPmKrKzk5i1Ve3JMbSPUUG6PzFzY9GRgCCAasGwZ0ZWxLg0UzMCR4G/wBJR8mKxjwKXYD2mufmcqZdNBhadwOT942bO2RCWIstsO4H+pLLoTKGDWRV+IodhYvnY/MAMFXNANMDW0BBuMEJHEoW7mHipRsen+q4lb1ALBaFGz3mmRdTqvk1NJuCYO8cEfYxIR5lDitVeRGKkEnfxAukgWdxG3NULiowbSfcN5RclbimYdmFgyeksdgSTwO8RVN2OPJkF20uRX5j87EzFQuLfY3tXf6xFPO91AWuzA2ldyd/i6lg648dKikFaYsL/wDUStRBDUfJ7zOnsH/K+IqoWDtvC5uqHHiVxdMXJtgoG8D4dJY6hQ7HYy6IAg8xwGoGrEzLV0QPiMdJTbt5lQUeu9SgO5LD4sTnBlk961dNx9YHqfh+VGxDCXCb2pbf/qdeTpM7+/EgB3BW6LfaeKt1tQrejPT6Tr8gx+k76aNqTv8AacefCz/Li1L+V15FXH6P8bhAqtg211sK7zOMGVT/ADAMhWivZa4+gkeqyHI/pPjGQqpO588mS/g1QkY9V17l3P78Gcp/to+XplbGuTIyrjApXBsOR/ac2NGOLXoJS9ps5yYQRrBB9rBVIAEvg6wnTjeyK0hgJ1lsjKSAs6qBZYgVdfvDkxlNQKhT57fYwdUAuVwKLFq00QR/7l8YR1X1nZioGk3sPgAzX1+piXoPkOpjQHdth9omTH7QcYAvsDPQ6psKMpDktf5fNznZg2UDUoW9wvP6xOWmOdnIUqcS7UCCPE7MJxthGg+4DcdpkxeoCrqTpN2YMXTMuVgBYv2+ZUUEYTNjbGaYVNNbobtY3+LhBA3AH0/zGGMgKWBUXyQIyKQtLe/cA/4kUhNm6oTbeb+gjadzbU3AqBh5INfWAVs7KSb8HYQFCARp7QqKNBj5jkEoTRHbubgRLEkbXFIU3t9KlCAPP35ihNX0hCriZ70KTQs/EU4cisQykTrcphxFMYb3d2HMi2UmgKoDfaY+7b0uJgUIBzOpMaJhDsQC3F8j6f7kDRNi/vLOW0whEU8zoOOsRbQWrxADiw4/UyMLIFKNyJLykJCDEQt5CqD5MZ8jYsZHTYyRdFz3+kj6wyamdFcg0gJuhObJnytlJZjttsdpMvJrqH6gdS1LlXt3Ismc2bCA6pVNW/eUXqciOSDY+ZM9Rlb2hyNR8yyWIQYMhbSilieKj9Wjrjxs1KK0hf8AM68WJenwZPeuR2FEgGgPH/c53KsgXJjJVaJo/YfST6to4SRvMOI2RQGIU2IoB/SdUGNtYvf6RdtprgMdkJkzyajNfeKOIQsJsDeEADc7wHcwCGjgyfeMID3f1jcdogO8axAwPaYzbSi0TVm4CrzHuxMwMZELcA13NcQFVSzUosz0PwrGV6vFkyKAgvc/3nOuNcaElS1/uI4yMMgZbJWqriZt1VusL5OoYMotm5A/MY3Wa2ylqJUGrleooMXJIUmxXmoCpHT41YFa4B/vMzl/TTp/B0Vs6k9p7LWEdh32E4vw7Fp6gso2qd53C3xdmaRPqDoxgXsFN7z5bqyfXZl/p5M+i6gk4t9vYdvrPneoohk43uVFvWV8uJX3ZyDYX8o+JTMwTIzcALenvXmS6JEOQ5RqpFCAnua3/aJ1RbM689hQ2FeJxsm40XEr9VkZcSjux3n0/wCH4vQ6BARRqzPI6MYRjzKrgkNekcAfE9xjo6cXtQAnTjUw67Ks+f8A/wCI8oZtI7T3lf3VWwUG58v+NPrysfnaaiV5eHJ/+4jOLWxYA/xOtkfOmRv6V2Fj3H/Qkuk6jD05JfGWYjZr4iZOsd8vp4SqKxAHmvM5cpbeo1PHOuvps5YGmravmbDnWmGUBlPY9/mHOzEFsi3ZpSdth8RejxDI5Vk1E8b0B8ma/O0TzLrfUpscDahUOIl8Iw5SFAbWgK8ngi+wlsijG2hCrVtZWtubiF0ytjVF34obdv8AwxvQ43FOdqF/WLe1S+VDo1D8t1fzIGblQvEN3NUNSgSuNWalUEsewiVK4nZcikELvz4ihsmJ0A1KRte8iPzAES2TMXytuSG3MkTWw5HeSaHDY11WoIIrSRwfrNIk78TRgkI3aLNKGB32l8bsF9po+ZzCVSypP95Rdc/tKkCybJreBnGlgPahNhQeI56g6QmUBgu632MGXKuV9dAOT2oD9JlHOdvpBZ8x6NnUDXBMQVdbEyqoGoVxfMJOwIP6yVkbVG5G9SIqH2o8xWDEkkb96PaKDXf6ShK6ARZMCTE2dtoVBG/F/vGYWuwF8wEM49xN97llC7EniFeb7QEVMp9wlV0gGlYc8wu5Uqy7A/tGxWRRB+IhSmKq2/PxKy716jEyDKRWRfB7yq9VRQhjYB55N9zOHp2Vcw9bdboqNpV0OTJeJdmOwHM4XhJW9dWc9OcJ9he96BoiXxjFtloAbEIpoBqq5wYenzO5o0g2Y3/5cr1PqANWRn30sbmLPzVVy6EyLmQamJJZ9RsHtOd1cM+RBeNCGu658ToU5FKMCzPtrXix4E3VhV6ZtICWQzld7FxxudIgvpemDkt27mydo2PQrepiCsV2o7ijHxuuX3MVAvg7EDtVczYUUNo4Yk7VOk7RcdbmxAMQvp7KVqd+HMmQE41G42obzm6TCrk4ch2AsGp2Y0xdPjb0RVnbf+3xOfLPGoTQ6OGcgqTwwkmxH1GY2V5B8mL/ABLhyDfgfErhOVGK+mSpO9D/ADLNh6xViFbQGB3oCiRAwyFbBZNuATx4nQUwqdbM58CyZPLi9VtdslmhY2M19mFCgge7b5M2gX7qv6VcbDidXrI2Mj5aU9JVJrKNzwY+4Yiy0d155upgjPxYHfbmFQMTX6mMniqMnk/MCmZKv/lzNajOvN7n57RseFmYUp8/aVx4kcgepZAugP8AcqbVKx0wqiNV/wDUl5/kMcXVHK7FqpR2Hacwxu+QDja9zO5tb4y7ITXzX6TzcrE5Cd9/mTj/AEV2IF1DVkBI7QrlxKaxjXWw1TlwYi2RdQOknfev3ndrxpjBwgJjvdid9pnksc3VdRkXTjH8zI7VoPb7SeYjDjGJkJFnvQb5lum15cmTqTsiAqt70TZnF1OQvlJLFq4N/wBvAmuMzoqeV2cBSAAvAAoCSdwFAT77cxr3NybAk0J0ZJzuagRyjA7GuLjFTUUD4gWOVmxb6bayTq3kPUJbU/uPzMRZMQgySSA8kG4aGok94CD3P0h5HeaQG2iRyKEFQBY47QCx9I9WvaZhQ7QEPEwBriGYeIAI3mEbsLgqoBEYfMURjA3xGU+Io3jCBRaY7zqJ9PEqhWWxxfMhhJpqq9jZNVC+Syw51TN7UHytfJBjhgpBTxzckDXO/wDiZfzCXB7PUYwQFUkjQKvsak8ZDsAxZjVE/MTq3Znc8Jq2/SdHS8oGB8333mJMaep0RPplhV7CdOUX09Lte0l0yhE9w3nSotVvet5pHB1tgHSe1TxlwaheQFRdknuPE+i6tUGJi+5ra+J851b5MgCqoCkhvPPFyW/kMVYJjwZFVz/MN6R/aec9nLRBGnavE7M1g4kUhlxbk1entNj6Zl6lSVGkmzcxxud1a7PwHGC+QsLIXYT2OpttGO9yd5yfg+JQXyA3e1ztYXnDH8qizOiA5FZNJ3sA/E+T/Ez/ADn+pn0uT+Xgdya1ZAbny/XNrzMR5M1ErkGDIzG1IAq7+Y/WKqY8WLCKCPbOTfuNcHwKlsK5M+TEqLqIbk8D4nL+Iu56p/VXS11Q4H0mO7WvwgUZepJdqBYlieD/AO5d8wGAY0AVtZ2O9j4/ScSsy7gmMzXgVCDYYm77GLx1D5OoLsfaNR5M5W5udvR4VyjKrdk1KfBHH0BkOowvhfS60Rt5H6xMnRiOpvT0gkAncXsYtbyqoWNAXOofh+Y1ekXyLsiW8pPRy48OpGYMo0i6N7xGBueqFOLGdWTUx4ULxW28h6eJnLZ3Yk7kzM5mOAKSeJ04unRsWp3AI5A3oS5wAkspoHcHk/rFyY8mPEMasCDze0XlpiLhcahQ2xumHcfSRKBiSDQqzfMqnTsxOqjXgx8XTtj92QgAHYE7H7S7g4gGs1c09JcmPESbBJWmK7GaPq/0Y8ejwJhD23mAJNLufibQY2M7kHvABZNnibavmBUAMADcUAj5+INRNcfeWFkAlb+g5hDKScekg77nfmKWyEFCLUrsPEU2fyqb8RxiyVQK/rLggVYNvKp0+UrYX9SBGUlWIyAr9palC2Ml/UbRg5t9wwI+Kj5CvpoqqARux/5R16lsYpT9ybhysudNarWQc13+0mCAJqhzHRhdPxF5JIFb+ZmPcDaQA7E9xCiqrjVYPcVBYPMYjdfpKOkFSBp5PFRHAb3AbgbxBuNO/N87RlB0i7H1mkMh1IPMv0oztqPTq18MbHHiQxhmcKosnah5nRicdL1BZ1XIvDIDYI+sxymzpY6m6hw5DjToFDQAR9B/uRyuuUrlx0NJ0kMavvNn6nGFXIi47f3aFJJT4vgTkZwzAhAL8XOc4frWrjqsjZGayCbqvmVzOy9OgZd9W5u/tU4nBNldx8cSnSOFzIWUGthvVfMt4pq6rgcFgWxNsRW86MOTMmfT1SHUBSk9vn6TYmUZQvUdPqJ3XSu7Cdq5unKgeivttrI4nK8mpHb0yhl9RaAI9pPcRsnsQk7EC+JDJ1N40dHKDcVxOReoyUGDjJjQG7NGc+N5Vq4JZcrFrFiWwswQFSQPEnixq7agpV8grSwsTux4UxgKTe1jedb/ACT9Zyh62YDcAic2XIzE2T9Li5MjfxBTIraeVY7x9E3xkKRPf+diFHJlCDlYLhBYKOT/AJuEqiLwHJ7+IcChQzEgDsD3Paa/2jZcbJi1ZCSzdgePrOQ8VW06SGYnk+ZN0I2qWQLgyBH1MTsNqMuvWM/tYAnsK2nP6ZH9JM6sOFkxnYDJRI+BMcsWNlbKy4zjUWANq4+JzJiOTISVoAbt3uP/ADkOYs5ojjyZyjO5yrgVmC9gePmY/wCK6mwrjSrBN9994vUYjkyYMXuLsQGr+wmFlg2MiwwAFzpVbcZmyBciqe/BJ/1LKIdMoXpMoLUust5AXiz5+B5nndQEDfy754Juh2+87s7O+YYiQmJarSOf+4mPCmXM7aVUKaNAaa/3NfWdmOROnyua0HYAm9qEbNkXFSICKHajv9Z05mbIcn8OF5tzfPjmcOQON2Uix3ES/XqeJMWIsk7/ALxQB3l0Clae6INUYhwtdAA+3Vt4m2UWHu4qAL7oxHMy0AbEoDivseJO9zGc2YneVBMF+Ye8EA2Jm3g7QwAAKmr7Rh2HEJUVte3xASH6TEVNA0wmqMBW5HPEAqp77RghJAUEk8QEnzCjFWsbfSB0LhPo17dXJF7wVSLZNg7CFRa2QAOCSYoc7heAJlQKnc1UfBhOTKFHczLqYgD3D4npdOqriBUKraCaHO+0aSF9MP1DKRVnv4nZhxDT7bO+xkPSAVTW5Jnd02MsFANDvIruxYqxICaMfLk0Mqivn6SeUlc+MDgSXUPeQr30giUc/XZmZnS+RsPM8o2uAE1eon5oTs65j/E6iSBXInDkcuHZtgPagA2mbA2MHN1AUsRiC6vF/wDhnQudMmpLIckszEcCQxKURWxEHVkUGx28QlUbKGx3p1FTtRmZNqvb/CxXRk1RuXytWJttq5mxIE6RRe1Wbkurb2Ig71t5nREev/l9Di2P5rM+Zz75D9Z9L+LteBBdb8Tw8aKdbmiwGwIsReWTTBF4chTRo9m9NupHa+18zhy9J6gb0ryFW9zXx8S+NjqyZsn80cUfPMGD01RmAD6iCALu/E4bZ2164M2HJgdsWVSrckTJ0ud8YZMbEHvPWOFTk9XIAX5OrtGUZXzoQGQLvZI3lv8AL/Rjl6Pps+EA0ys5oqOSPE2XD1Nlmw2u9lmB771vOzIyL/LVmGUEUQdlB5YyebXnyJ0uABUFe5hdCZltva4TCuLENaoFcijQgyZ1ZNSNq+9AfEl1AzdPh9NyrsrcKtGRweplcuapRvY2+kfP7U1Vh7Q3fmr4nEzEbngcVL9TlA1cHfgHgzkyBtKtd6j2M6cYzTplrMOdJOw8w5MhBOlq8fMghprPadLpjdwbB1Gr+ftNXJRfAAMTM4YArsB+0m7qGLZGIsUPA+ZTIqKoUZG5rcc/M4WYq5BFg+R2mZNF8uNHyUWFAWe004xqdztdmaazP0S0bcTaSp2q4Vb6Qg7b7zbJKob1BVkbVK7Eb1BagglR9IUqjbcfEpYUbN+0XUNwnBigE8youMhP5jfahL2gIJ//AOhYnHXt7gia77xo7Ey4SN1LXyWNf2kMxRsv5jo4XyJPV27TBvEugutKNwb8Q47urAvzADZ3GxHExFLXeBuHriMTq57zMqnH6ham/wCNSV7iplVKFx7oV9zckG2JN3E1wjrxhyV9PZxZHb95qAxFy1NdBANz8yeLJpomP1JyNkJyNqYgEm75iRQxPeUbmWzLTGw2knbUdzOfHsbBqu89TJ1OPqugCHfJhF6ioA+glHm8HijcLH54iM4C7GIhtgB3lRfGTZonfaVODKqK4QlTwRuPvJBaIYDnYymPKy+5WpuQRtJZb4PRasVa8LJVe7S2r/8Al8VOTqMosgMwUWADuR/qHqfxDO9aW07e4qfzX5kl6slmOXHjZtqtOJxnGz1q119LlOcFMrNsPaQdyR2EOPFl9YN6Z9o1Fb48WfMgOowF9QHpHvW4+w7To6oasJskDSCKO5+o/wAyeVUl63Kr6SxUgggA8eZ3YszkF8GovywvgeBPLyOWJLrTbcChUCZGT3LzNXhqa9PF+JOOsJzam206b4Pme0tfmIs1wO88DpWbIqg4w+LgsQLH38TuPUaVHpAmgAAfHx5nPlP6aj0iFdghBBPau0jkXSGOEANVCzH6cL1WIviemGx929+JdbGzEMfpxM7i48Y5+rwINTWin3ETt6MjqOnDrWr+q+063NDhClbgznysuTEMWN1xs++1UZqfyf6TFWVVfHjQqaGpgO5ks2NdBciiV4/zOVukzAKVz6VQ7Fj+8GZtZXTlQvRAZdh9/MXlpiyFG/mOCGArxc87qcWxyh1vGT3/ADS9O+IFx7q2I2B8Gcju6rZPvBLEd/rHH1Kv0zZcZu9QbhjvOksNF2GIBUV2+ZDC46ttKjQEogn+keJDP1OjKVQMCrbBh2+JctUuV82N9LFy1e3vt/3HVy+LGrPpb54A8VD0urqWIRSdKUSxoc9zOXJY9wNrqo/E1iHdNJYau9qCN2gZ8rEO5YkbBif8xQxZKdSD8w2dIUk0OB4m5EJLYT7SBjDMxAsjgSVTWRwZQc51ZNTG2PN8iQIoc8yjksbMQiIEIJikSm8BF9hKicEciCoQpgjkQESjILbfiVItlXVX9hJg1xsI2MjUSx/a4GZT2F+aiVUZmGrvUc+8CiPv2gTXmdAS0DE/aRVSeBOgNwqHgWbG33kokVDOFT95VMS61XegPcbiFNJ9ws87RSfEejpYqFGNW4BkwpJ06dzFW2YeeBLIj6/ykFebk8VTFhcC0AK17r7z0MBSme7CqB955wvKdNkfTvPQ6fG38Oq1Rvf5kWKWzGwB9p6nRoUxoK+ST2nL0y48Y9TJvX5R5Mvh6ps2QAAKBzXeFHqGrMT42H1k8i2cmU/0jbfmEPqzE3ekk14i6S7ZmY6VoAnxKjzOvOnIdPAGmpyE3jVL/qNidnXMpJ5LG+OJyYq9ZTXeB0s4xgGxq0gafnsY/wCGgnqSjbl+57GR9NmYkjne56P4ZiByK3/E7zMmK9XKQEC/ac2fKuMtlLKD+Vbj5X1P9JxdYpfoyhxBgW3JF1JyvQXrmXJjXQbNTz8vTkdPVgM/JrgeJsfTZMWR1dm+oPPxcbMPUTQNzV1qozly/kvi45s/Ta0GPEO1t21S2HCmBPTQjUAWsmPixacnqZSL2FgbQ9SMQVkXQXYlchYi9+wI4+0z3fVc3TuOr/mMCoU0L7/pOjNlHTktT0R9yficjPjx30+KwvG3ebGUyuc2rdRpUePmM/V1XpnLZXYocZIBZuT8b94cGQI+Z2OrbYAVx5k+oGUj+TlAwkD3nlj3oeJg6qqYw5YKfc35jv5qMTXM2rqOobIB7j3HMtoyr/LtVB3oG6lk9TW66U2alYHbbn7zmzhiW1aab28nt4mt2o4My4FbTrckfmOmScIBauSt3pM7cidONIQg39rkMyq5Ixi2+u5+Z1lZrjvf4nX0TNRXGqhgPcxJsj4E5ziyA7oRvUriyZMeM4kxkM5omtz8TXLuJB6l8oNZaY8BvFSYGMY9VuxPO9XM5JBtRd81vKYenGRAAG7ktW19hJ5FNi6nBiShiIJokg7zRh0OMD3sT9O00x/ivbyr3jAiJcN3O7J9VneMOO0kCYwO28gbbmPj0ltzt3qIaNURCQaB+0BiBRo/QSQ+8qik7Aj9ZX+EyMSdeNfNtCOdaq+8Mvl6LLj06WXLq3pQb/QznHNQGPkTKSDXHaDvGUgA2ORUDHcVubhXScYtRqHBjILIqPkA39LVpPZ6v9Y9HM93sCYceJshXsOLjjGzE77+BHoIqk2B2MsEzSEgrZE9LoelXqsJVkUHGS+TKT7VWqA/WeW1lgaNDj5nodJ1C4cWRWQ5C60BZADdjXeoqxzelqpVG87R0IXCM2QDHi8ufzfQd5yHIynY/X5Mv6jZMRBX1MjkDUwsqB4PaVHI6g3Q0/FcSQ2nW6qMe4p73kAgYmuID4XLAr8yjChsaPNTmopuDKo+rnxtA6B1GUdOcB0nGG1KKFg/XxG1YmxHJQ9RVOwFAHtzzIEEIDUjksNY2meXHVldGTAdaBMZXWCR7rFRxkPo6GyHY/lMXHl9Pph6bN6rnSaO1cgfW4MBDdUgKtkF7gDczH/R0u2LMQmHF7uAOS31kgunK2NtiDW+1SxRf6lZcg51Agg/5na+BWw26rkyKpsne9vOxEx9fLWa4ELKp5o7HxLDO5a2Yk+SZP1VyYhjOPSyrsV/qPyIouqnTqo7+n6xulya0b2HZhx957rElLTGxYjb3z5ZGbHksBWK+RYn0HTdUC6h9LalBQgC/kTlzmVrjVgQo058YXGB52uRPTpkKMqJpXg9x9J0ZmGXZwwUf8RR/aSQ41LaC1n82qc7/ppDN6egAuxLnSNuR347TiydBmBUrpABPt1XY/xPXOhWZgADwTON+rC5tOnaruttu8S3ciVxPh/EGU5AVtF8gUPG+05unwZB7clhNrUmrs0N519d1gXCMWEgA7uLv3Th/iM2Tpv4cbre3+p1m4h82HMrtoyIFxcMpHPix3nOuctpBB25IO5iLkKppJsHkRUxtkJCAkgWa8Tcn9surC748DaGGnUbU+IcTHJsdOw3J7TkLFcgIN1xGx5Crgy4muvQ3q6OYSpHadWFMZ6Q5c6myCoB2G3eN0b9PlDZMiqu9AWSb/1M/eNY5MuP020WDpHIHMmRO/qNGRQ6bozc12HeJ03TjIt0XF0ADUTn1tMcTY2CayPaTV/MODEMmYBvygEnetp6mfDiwgY8dHljYBJ+848J9JtRGhb3vmx2Ez/6bOjMcTqRVgjvKdNjRy5yhiqi/aQDc6smLJ1JC40DHseKHi+8r0/QemmtgWyMCukEERy/kmGdvP6jCmIqFfUatrHET+HzemcnpsFBqyJ76/hmEIH6oNSgewMOZzdfkyAqcLCiaA8V28m5Zz8hjxSO8zIygFlIDCxfcT0A+Po8aFemDZgSGcm144HyL3kU6pHyerkQPkBGgEmqr995v6v4mOMiYS+X1cjl+oOltOxK7t+n95Gal1CkfpHxggg8Qd4wsmVBY73zOhMbHpwdlLHjz4nOFJO/E6GyAN7dh4G0lUCun22NjyO8HpliKBJPiHUQwu9uLnRg0FTr1XYrTzAdcelEIqlUWR5Pb6xFV8mQ7kr5+JfqRpQJQDDkdxFwgj2MSO9CZ/2olTR0ijztzPR6ZGbDjQKSdzQE48NnINgdJu/M7fUZMRyaiGJoAeJL0sbqnC4wp2oVzzD0TllZloUJykDJ7sjDzzvLdMxTC45HAuJCrdIQq5nbeiOZbOwPRmuXtjJhCvQkH+s39oeoOjDdbgBQL7GaR5OUURVgFYemwBjZ37/SPlQ5cgA5Cy3T4/TUhuRJasA+652/h40hj3qcij3Tv6UViaplTnYM058vUFR7TS1H6nIFw6Q9Mew5qecGp7cNpY2Pmv8AE5c7b1FiWbqjlzMmHYgdxtD06hXOR2Gom9t6hZseE6VW9/HJnIMz5nKKBvzt+XcTM46OvLqONKYAXwdz/qeb1TKdGPa7u18y+dlzO+I5DSr25Zr/AMSTY0Q/yyLB22mp0lWw4E0DJRBYA23I+n+5s76bCraDmttvEjjy5M7MmoBdwG/vLB0VLG5qqJsCpLu9iHWMMeNRkBW9qDXUTD1GPGxXGTjobmt2nPkY9Rn15b08Cz3hzEkEpwTu1Tc49ZU1fJlyklQQqDYVzOVsmRnVXGk1XPadGLpcuTElNQY8k1sJ0YumxYRRUtqO55jZxPXnMhq1UnavO0VsTYxbBk22sT08jY2WjTV8cx0wg4zmYGn21He/p8yzmY5Olwr6Pq5GXSRQFyzBFJsa9Q2+IVBd8ifwxChrxOWFV8wvqCPkIxjSQrdyL44mbtqmx4F9XfSyijY7fWYqupwoRR2AB288zmDsDje9Gk3ff6wZNfGyKN/dIuq5EKE0+NvvU0joZzRJN95pZiPEcXve0W9hKgL/AEmvqJNlpqBuelhhGAsbCLRqyIQe0BlBPEqMYOxaq7SW9RlJu7/SBagrUCB5lMeV0YMtEDsaIkxkoBSoYeDBqN3Vf2EqOrqOoy5gHysWfkkij8SdrmYnINx/UDv/ANyIy7hWFr333m9QK+pL0jseagLkTS5UNYHB8waTVeJV9LC+4/cSdn7TIKtpogj6Ri9t7gJNj/7iE+YHRqDWVpL5qNjPCLi1FthYv9JyhjfP1jjISb1G5dFCLIJNAeI60imrF/2kC5O/eDWSSTGi6st2SQf8Tq/jmXC2DApx43A1WbLEfPb7TzixNb/SMH47mUUckG75G8ynaxsDNdkWNhCeN+PMCeTewO0Cmqh/opqEyDUa58yKsrFgL2FQZQCljtvNjPtKnejKBRoa+JUcm5P0nV0h0dSj5BQ54sSOVKCkAC9p1orH8MbK2MEBtKuDuDOfJY9jH1YZirMGbkHtfwJLOeq6jUcbiwaq9M8Ueor2xYFDX0M7un6xA6gpQ/qPczjf4/nuNbrrHQOSMpyaXvUTXB+IuTPgt0fHpN1aiiQeSbnQ3VhcBY5BYH5V3uc6ri6py7qVXs17bTMt95L/AMDJhx+mGxG14vuYMDnG6hmZRqsES2FTizlAoGMqO9j63I9WtMptSrcFTtOnG7cR765OpyJqXGpBGxDKf8RGwZcmQeoSn0KzzegydYQ2PCwZfrQE9HEc6tZwhyu9Bx/gyXjjUo5lOFWQtz3PJM41xY2DMCuTaj3nRmwZc2QZcwKBd9Ng2fMVqcNp1K3/ACqt5yuRXJm6fAq63wtlDbKEBFV4/wBznzv0/wDDhQukA7IQd6/85nfkf3hHdaO4O4H0B7SWTosT9R62dipLWe+r4qa+p+pjzPRXKNeLSCR+Uiq+IcDfw5OShYrtxO7qf4PE64xgYKw5Rjf7zYMeLrumfUmhsa6eapu31mvrrvxMec2FcuI5EVtQamI4JNn+0foemvrFD2KGv8urjeev+FdCcXTEZWRlY2CeB9RKP0mMuhXHT3t4P/niS/y5cPl5PV9Sc2P1UYimOzceRz/acJtUJzFxe4UefJE9X8Sx/wD49cYRiXDs2qthf/n0nn9XhzaTmye8E+5gdVHwT5nWZEro/CswfOEyG0RSTsT+s9Zs2MPsQgaivAJHwJ81gynE2taudQ669hjUcb9xOfPhbeiV6mfL07NvqrVRY3sPIqc2bEcmIHC7ZKHtU8gQo5zYguw3vSe4PfaW/D8Gb1RiQfl4I8HvMTrxfUehOUsMKY9xv4r5Nz18WbD06ktkx5GDfn3IX6yWTEUstpxBQTmyGiQPHxPLydbp6RMOF1fHqJclKBJ/prwJuTbq+D+I9U6+oX6jMzZT7V2AVfkfPicWLqGLDhWIoODVfMnnYZchONWOptgTZ37S3/43MdHpnVZ91+0IfBvvOuT9Z03XJpCjF6hQDUpYVY8123nBZ5J5nQ/UZMROMuGQndVYkH4uUIx9YQcKJhVMdAFhsR+5iW8fUvbkRtOx48yrKuoaGLCuSK3k8mJkoMedwZRApUbm63m0bSf/AAwgV3AhGJjwIdDDlTKjbf8AK4buAIfEoo08gfcQALJ3Nzr6c6Ax+K4i4cqY7JwJkvsw2nQeoR0GnpsaH/62JKsYYmILOSS252szKFb3quneqHEJd8uPSAdXJ35nR03S9SyADGaPzA6ujwpjT13vjjyYOqKugKmgd67zqxdNkXHpyVQ4E5swROpFDZPcfoB/uYs7aceRGR6IontOnCg/hSa3uQfI+bJqM7saVh0HkgGppDPY9HHdkm/iQ6xgFdVILO9n4rYS+ZtHUja6WvpOX0izMx83IJYrVyRW4qVss1kn4jNj0cQBe8zWmQe7YWfE5n63JhzE4vkNfj/qHqXVFtDZPYGedlzO2QDSdxtv+ac9to626g5R6uoWRxKLkTDjBLks5AWxv+k8zKr4XLErvsyA7iTPXMucOntA2rmhHxvhr1c2RkxByGOTJSYxe9d2P9hOVfW9cnK2jGpYqq1Zv5i4cbrnGXMQiMtot2T9RA6u2c5QWbmkHYSb+AKBj3cgORZA3rxZhxYAQyZHO5skDvB06tlyI/5hZLA8TpyhFKsW0gEGh5kt/Bz9TrRPSSwqirsAmcOZ8hBLL80O068+ZPUotYBrjecbYTmJOPJvf1ub4/7SlbLqAGn6fEv0arkYeylRreztB03S5W1ltjuA54HmdfT4kTEfSvJ7gL0197jlynkJDtm93pIWAW+a3kM2YoA25W+PEouMl3bI6HTuQBdTjzH+YNX5W30na/EzxkFMTK9Mrmv6gJ2jOmRAyBWrsB+3xPFY1kYLsT3HeU6fIW6gsulFA3HaavDeyV6GZnK3RJrYAzld/SyABtIarP3u6nTg6XNnwnLqASz3AJrmh3k26LH6gyOWN7gTMzj6pgozuW9Qtqbcgf3nX/D9LhN5HOU1W3tH6neU6fGhwFieCAqAfnPJH6TyM+fI2dtQTY3SxJavjqzdXhxpWJKHBqaP0f4evXdKMwdbDEZAw4+nmaanGT1MtfNCwd7+sNALY/eEE3v+sr6X8ovdDsPPmehhHkVX3jAAccxiqf8AKu+xg0GvawI/SUavJ4mJIgYFGpgQfmDmRDhqN1Zj2avWNx2ke/eEttAoApNVZgI3+naIG2mB3hVVOkUQOYh2Y1xC5s83cy133gLps7C7ikUZb09X5Tf+or49JI5r4jEToeeJuxowlaFxTx4hWvxDe1xfvNcBqhWwfmLf2hsje4HQCKsRcj3xfiSVjKDfkQCgJFbb+YwJDg2NRu5hYWu5mrliN72lDY/a9Dv3EqxsUK5kF2st9vmPjyBn2uvPiQHMbTSAAV32jdLlcfyjRRjuDxGcUKB9vE5Qa2ks0eoemxdTifMpb1ANRAoCyaqcLJ6Zptyd9jYl+j6goDjVVJcFbIvn+02dcdakLOaG1V9Zzmy5V9Vx5xkRFfQFGwSyNR8mANkw5SmUdwa7fBE4ktcgIPBnqOpzdEG9RVAJOnTufmSzKLpnx5VbGCQKG5Nfb6RMeDJ1CKmBNTKdxYszgxOUygz3Ogx4cgGbIOnLLxZKm/pJnws7R6fB1nTZR/8Ar2f+LUR+lz0MfUZ2pX/D8eknkUJ2YyqqUJx2dy+igYRj6IuCnphhuNJA3k+vrtrMceRHZmP8P6Krw1Df9JNcqD8rgljQ8T0uqGJsTLkyFdX/AB2M8/pumTGzahbM183t2nHniqLgQqTQo3d7gXzIdZixalbJmdFF0qnZRU7yC6kqw09vAnnZegXJ1DPkzOTsKHE5y990qWAdP05UB8uV8n5SW87/AG2nZg6ZceIDHudVlioP3kzg6bocLAqu57tQIhfrOnPT62CurCtJNAD58S22+CpQ4MOVAdYCmiT3J7w4yuIl2z6iPyrf7meMeufplBIbJgP5Sfv+o+s2P8RTp8jn0nGpgdTG+23/AKm/jkmujrOnfqMmTI+W8Z06u/Hg9p5vUt1HUAqMYAU6VCA1/wDyj7S38eQwa2yY63U92PNT0MLt6HqDEQte212Wa3lx9T14fTdOGV8mVgioO47nYbf4hxYG9TIrYmLBCVVRe/b7T2c+VU6YY1xeoFokadj9pHD1ODEW9ILjQtuANzNffK7cMc34Z0GfPmLZA+PAovIa3rwPkz3cuXF0eJSCU9xKBGPjYH/lPHx5smPOXTqdaM3uVjR+KM9DBhfLiObFmU5P/wC4xB0jx8Ry5LIhnXqOp6PSQF9Rtw35iB8f7+JxN0PVqExjGuHbl2Av537z3MIzYsSHJ6Zbu+rYntz8d5w9X+J4OnfKilMzOLL4zbD41H/HE1w3Cj0XQDp8CPmxMcqsX1Bwo45B7zh6/p/xDPkAKUAnsxjKCQvfvZ+TPNydXmdSjZGKE3puxOrEwx/hr5lxB2Y6Tkb+mxVDztvN5ePdZ3XFmxZMD+nlUq9XR8TYcmTG1oTZBUzC0PpuW0WCwqjJtsx03XYkczp76y62zY8rO+S/UdrPcAfHzNrRaK7NVicgqud45dmfUxJJ5uJMHsdOwKhQpIAFMd7lgoJqpy9Fk1dOFqtO31nYljvJiCMansb+I3oqe0YfWVCWLgc/oKO0ZcQAPt3+k6fTF7/rCqgc8/HeVEtgBSBT8S2PO6H20D5E1A7EH7QbE8H6SmuzH1z/ANYuQylMxasZBPe7iYx3IN/MviJB2QH5MmLqKdI4cbbfM7ACHPaqqVXMxFEftCSG/pkrTmZC2UnneMMYrfzLen3ERxVXMqi62TtEbESrAcywG9mQ6vKo6e0YHUaFd5m1Xg53CZSGUrXYGPjxpjyHO2q15oUBt/eBi9nIyrqB2HAPjeIyhS4yPyKG1i+8zZ0OTq2YZva36EmDpcbHqQWxnY0Qymp19L07OfWyKCxNKvz8yuVnwMuolgRub7/SLy/ImBmyYsLEsLd+5a5EZ8WVGSmLuTQHMTNhGR1ZmOgb0dyfiV6XAnqtkdRbDkbV9BM9SaBk6henrHiQKnY/7gxdWM2S2F1sL4HzNnxeoSTYJ3PehI6OmwBtRZ37DipZmB+oOEalJW+DW8HR9OH9z2F7UK1CQwaeoy5HyK2y3QudZzjGFGNdIH9JH+Jbs6gOT0lPpkMFJ23nRkZwFXGyIF3o9h9J5mTJqOp6NjezdfQRsGVQXF6h/STyZLwNdS/ysDOGDPe5YgThYM7BifVs3QNmHJlc5jj0hD8jcTsw9PjxrTE665BqvtL/APPdPXn5OnysgyDCygUOOTOnoOkbGWfOg06d1YTsy5U27aBzq4kBmDtoxhrPBHeT65WYY6tT5MWjp8aqoUGyt0AP2nCnWFsmkHUy3vdCW671MSY0xAqy7ljyTxsPF7Ty9ZVhvuO8s4f2tr6LpeoX+HylUB6nGjFbN1Y7DzPK/EsXp9RjU5FLlBYB/Ka7/M5cebIrD0yQVNgrzPQZEfAHzAJVtqIJOo9q8zWfJuxz9N1PUfhztpAttieePE07eh6jpNOvKobIoJrQKvtNJeX9wn/XzJPa44bcAkAf2i1e5O/mCjc7sKEUCrKD3sRzjTT7XNnjbaRY1QHbv5mBJ45gM+RygRiaXgGLfjiWXAzsVLU/FVcRsTYyQwv5EBdRr/MN/rARait/8RQDxXPEBu0Knb57RTVCYQGB383Kqyqtd2FGQHMeqG+0gYGjVyitY9zA9qMQj2b9oqmjZ78SocgVQ58xGUx0/NsLE2gsbvmBGt4CN5dl02O4PMQgWdoVPtUINQldrqoABcAjmVB0iItCNq1c7wH527zE0QD2g4FxlGnmvrKGAJKjtuSIUUKa0j5g3I2EJJAvsf3hFC40MpIAnLzvL4yCPfwRvEQbXM1WxOUdWBIIIO2xE6sbepqJFE8V2/7nIRpJ3EphyFHFGpjlNGyKFysAbrvVTo6XOUI5+0zoMmMMq24JLEd5AL7tgY/+oPU/gG6ljkwMpB3IJog/edvQ/hWpj/EhwF7Dv95z/h15kCZOtda2XHrIJnZ1P4f1Ff8A6vWuuQ1qU5TRnLbL863n6vi/DsDZm1YcyY62Zst/oBKf/ilDBk6jOKOwKjYTz06P8WXYu77UNOUf7hw9L1q5g2bJmxjhtT2DLeor0X6XFjwlGzZcjHn3VPGPV9Q/4gwxgsMYrQO/0qd+TLp6gIWu6U77N/oyhC4M4RXCs24/Wjc4S33FWws/8OpfGVJUnSPMm3UYcWQq7qGoki91HYEeZZMmB8aI7BXckFVNqPn9JzP/AA+PrqU3q/JroaSPn5+Zm8P2qh+I5V6npWKEsqitxWk+SZx9Thyn0Md4VysNR1bk1sL8CdPVelkzkjE+cKtgA0C3n6TxOtRsLtjBJ4DG+T3/AHno/j4yTGa7erxP/DWMxdsrgaaHuHkfE8vqM2tiFLHGDS6m3+pmyuwxYVN7KSLFcn95LvOvGYzXofhXUHEWJZinbGGrUx4lB+KZ8RVU06UYsoNmz5Nzyxa7R3524rvF4y3Ta9g/jTN7smI+op5DkD9P/BGOPF13TtnbqKyE0Qy2BXjxt+s8YhkCkgjULU/E68fVZMpTE7Kt7F2P732mOXHO4b/aWfFjxOPTyl99weR/ibF1GTE6+mdNGyOzfWdKpjyB8wxFdDe43qDg7V9bqcLisjCitGqPaa4366qXp7fVv1Obo16o9Rrr36XZQbqvavieC2S22P6zu/hM2T8PGcKuhLIP9Tb7/pOF/wA5vYnf4l4f0UnYxkY+ftDyoEDX33M2h9Rbfc/WI/5hR+DCKH1+e0Zx7Ce0qJMaNx1a1O3ENahS/aZUo3+sir9LmOPMPzEHYqO89hWAZVI3M8TpiE6pGfswuewOqxKac88bTN5YY61qrLAD5lUX54nOz4zS42LMRahRGzdQVx42VwouzqG0z9y3ox1i29p45jqgb5k8ZDKCARe++0rY+J01k641v8wJjjGgPtdSD9pMFAoBA1NwfP0j43Wzx/epNi4quFDsoujH9JrugPpJjKzbAgnxUIzODRAH3hVxjHJhcpjXU5oeYqZQeZHriDhv1NIA48zHK5Go5ut6xg6rhyAUSLB2P/c5s/4yhsY1o1sW3/tOPqCp1AMWO1zjxZmxagSQK4mJtHqdR1mZytFcYqmXm7nJmzE5hRHg2domBXdyRq0Hfcb/ABNlRND5CwVwCRQNiiKknqhmyKunT7je98VPOzZw2QncN+wlRkyN0zZF0sfUBI02QPntV9pz5XOZ2d6s/mrv9J1kZrvwdUV6Zl1ksBq47/WRy5MjD3slKNNjcmcaNpB0tXkGKSbPa+0k4TTVPWNUPyg3U9LCPTwlnIZ7vc7DbacHSanyDHSmzZ1CxtLdbnxk+mK1KK2/xM8ptwjN1ai0BINbH5kH6fKxY1qI3JU2DfA27ymDpVoZuoNLVqD/AFTuVnJTRstAsOAJLZx8X1y/h73gZFFU1sfiLk1K2pEJs8Vz8Gdo9HpOmIZaNbnTuxnJ1GTWusMKbhRtQiXbpiDZCTlZyqlhRGngfEipOJUyBgVawQDR+hlNVa1YWWA78SBXfidJEbWSbO5JhORzvZ+0bFiDtTHT42udOHpGfOmBSCxayb9oHe5bYhMq4lVEVn9Qj3au/wDqetg6XF0jKARmyIQ2oXpJ8fS+8KdK6uykKoRbbtpnOvUaHdhlBFUFXax9TON5W9Rvwr9OG6rK+bIzY1Ftdgn4Hxcjkx4snTjIUJysdyt7fbgCdXS5wq5M2UMr8ISbod6jL1GFXAAJSxrrivHzH1fDp5LY2x5NeO0HFA7ynU5GyhzYvuPAnqJ1QGQgJiCYSdOQLzZ2oTz+qONV1a7ysfAojxtNbtTHnrk0ZNWm/iaVbCpdrcX87TTexHCNtrhG3M173cJph32nRAHuY2LjG2N9+Io2J+ICSIDhz/yI7Gtpi5vck7VcVmIG8HahAcaTt2hGNSdi1xB/6jixsdpBNgQTfNwRshs0e0XtKGUWdzKabUSQNS2Nttt4DkWtnmt5Jl00QbEc7X3mJ9pDbbQEBqOhLGl58RPbYsfUXzGCnG4J+28INhtqojbmZkIUki5tQc2eRDqo0e21QFof+4Co32lCARtQ23MRTRPxACLtvxxHVRpIPPxACSD7aB3uMlh6Ju5QMW+x33uVZbI1dxt2ihdOQhTV9pQurY/mBFiVatwPEcNtvvUV9zQN9onH3kDn24tvMCsRtNdrRF1vEvvIqwphuBFog7CBGBO5P2ll0kAneMGxMy76qnd0+ZyQyY+nJXgNi3PzLfhvR4MhP8R0xdP+QyAV+89bp/w78N1qcKM5J2CZbr6zHKzyNSVwv+K9Y2y4cIZd2rGN4U/F+qQMdOJXbsqVX1nov+FYW0v6RTIDyWJA+m+8lkw9GHONsiZSlAqF3H1nK3/TTgX8Q6xs5GJFV/8A6IAfrc7byZsVZ2L6+6niDDh6Z8+JEBRyxBYm7HxOnOuJMTri1En8nla/MT8THPeXg8j8QVv4VnOOgPaHoi6O/wBTORk6zD0S9Q/tUkaSTTfBA5r5ndl6luo6krix6sOGiqvZFjuROf8AFG6vqNJZRQ5Ve58zcsnSVHB+IZgx1EsxohuK+0p6rOyqzMCpBG9zhwUuSmWyLG8cZae02o7XvNXjNTXtZeoGNfc+LWi2hChtR7HwJ5j9OMrqWdlZgWZnXY/SpJCWyqzFigJ3J8c1PQwKWRtywO2kHavvMX/Hxr1x9UozMuMgaFBbVj/KnHY9uJ5/pt6jIBqINbd59AmDDnCAMAGugBLY+lw9NeZlUZHJAcbWK5A7R/6yGa8Tqej/AIYsqlXcHTufybePM5+oVhkIN2pC2fpPc0dN1PTjJkpjRYgCrI2/Wo3UdLiZkJfWNPAHxtE/lz1MeJjxsysWB0KCA3YHkf8AqJprEW5JUV9zPaTp1xaEIDJqpfkmbN0mIuU6cj1bquANqofSX/1mmPN/Dyodky22NxRANfp8yrpjy5fUAfLkB/mLYXWB3Hz5lcnS419UYVDlTvkLaVUAdh3syGMsetTJ1IZPdqJquBttLu3Yj0v/ANEdHlHR4s2t1psT21fX4nlv/CtofLiUDSfb078//wA17ieuPbiYJlQEL6r0TvY4JHJ+J5b4E6hPUwquFb0hS1lm+BJw5d21a88KWuu3MJUgWdt9he86fxHKW6j00RUXCNChfj57zmx4zkyKi7sxoA7TtLs1lghChiDpaxcfHZpa1E7V3Pip19PhBRsLEujnYhCAGrYg/tOBwVcr3HgyTltww9V7Rs3Fdx8QkgXtBiRnNICSe0JsEq12D3m0MNmBrcT1g2LHjBHToy1Y09z/AKnn4xjZKP5/6frc6+oAxZh0Ysuq8sADVX9Jx599NQmbqBkKNvpVvpcbLnObIoxNQQWNuD3kHOvGFOP3GgrcSKkoSKB0m5ZxiPc6fr9WPT6Sqf6QJU9WWYoylTX6feeNh6vIjqaJxDtU78HUDIhDqFrx3+kxy+osxbKcjBRiy2B3Jgw5s2PqPduDyBOXL1BBK4k9Mk7/APuVxsSmjqMB1ttYHMk2Hr0B1KbEnk1KHNTDYncXXicnTYcS5GKldKkg79/gRzkRc2lsg9TQKVe8f+nL8X5jobqqDAEMymiB/ecpzt1CtryFMYO57/aPkVACwOolAGFV/wC5NFTSaIAa6rgnvJ9b6uOfKGdlXBsKu6oUPJitiTC9k63I7re48TqA0gIzXewHmJkVMbsyUciigbl+jHOMz02Q0i5D+bVyfiDMoZAS9BTuB3+8Z2x5vYymhQpVvT94uYYsK48ZvO5BK9gvgSxAbKi9Kq4MYRGsabuj3P1ruZ52ZEDD0yb0+7cV9p3ZzhxMLGjIlEht7P0nKnS5cjnJjUEXsa2m5Z6lcZBJlMWDJlsqpIE78X4a6knKFPYC52DEUxBasDjatu0nL+WfiY8/pelGPW/VIVFe2zyftJtiTNnrHSqeRyRPSVXye043X7iFelRUvFhXVVar3/WY++9q4icCaUXETmcEWos6a+1S6lQTqHG9x76j0grZKoflXicp6bODWqwTuCambZVN1Ay58gyPQU/G4H0nOR03pV9RQ5B+Z0M2R7GRRvsaYyuHpulZbOMgE8kE/YR9Z6evMTpRk15EYjGo2sbk+JbH0BZQ2XG4rtwWM9MEJoTH6eMDYFF7R8mog22oEVq4MX+Qxx48GJcQZca15+YUzDHkBxAAnknvDmdTSs5kRixMDpLr8qNpINmz41xuMaEOy05LWG+gnn6HKatKgEdzVfM7f4NdQKHJZG/xC/QE/wBbCuNuZ0+5Eu1xg9rs1+0VmGPGF5N2b3r7Tp//AB7UKYU0qv4fiVBqtjyxGwj74mV5uTKwQrq1D44kmYvQ3LbVPZXpMC//ANMG+b7RjjxJjDJhty3agAP9xP5J+Q+a8c9L1DEH02JP6zT1S+kkhaJ533mj/wBKY+aI3jghhZG8B3EBvmelkSN4p25jXfMDdhAB3Im5MP23gXniBUCl4+8IY1tv5i7mrO3iMNN7H7QCQDyNpGqJEvxfiIRZ+veBLtHU1xMy0PiIDAqpBbfiMSWFfFXJA7/WVUi6O0BFHuEtlpgAWuuIhQ3yD3iEkGoDhO9iD9jMrDgj7+IzAlDpKkjwNzAytp/ML2/WEBWQV+aSJj4yLF7CEDvQ4lLsGtjztJsNJIE17EQKpk7AAbcxCKFjzFuu9xlN94GDb8wMY3ps24EYYXIqh+sCak+LjqoPwfpHGBgLKLX1jqK//p7/AAIwT9Iavzb/AEjDCx/LufFRiSOVYCMlNQDqg8sTUKZej6m6bBk//wBDOlMOfHjIOLKoJsgKRfzLdG/U48tYutulBHp5LsntvOrD+J5vVZ8vVOorRRok/QzlytrWOLpujz58gKtk9IHcgGx/3KemuHJkILNbWb5HxPWxfimDSwp2CilfUCCPP1kutbFkZQuLqARe5oEmttu858ra1jhPUMhHpsVOrf8A7nodJ1D5guIsisxNKzAVt+pnk9TibB0bZ1JxsKAFcsf+v0nJ0rehefIgcf8A2GxPkzXH+OWJuPpVx9N0mMYsnUF/cNQxqTv5Ncznd+mGdcj4svp16bnVpKnzXieNm/E85xnFiysMZ7LtOnpenxt1KbdUrMfai6WIrzc1/wCchuujL+EYsjHQcingLiAfbmySRH6f/wDh7E/TjJk6jPjBNKrIAfqfE9d+k9TEWfBhXIh9iuSyqD3oSfoh8j48nWF1IUeihCBf033mLys6XI8Lqum/DOlGk9VkXJd2yWf08fMOJ+l9PIcbZ3ZQCRoKAjx9562bL+FdFuyYbJI9qamvvPE/FPxbL1eQ4sDOmEcDgt9ampPpL04T1BVj6YKEGwQx2nSPxbOcekhSL3bex537Tz8lkA3vwYl0eZv4l9Y16GfrMbBMmAHEwva7UDx5Mo3VtjI9N0cgbtdhvoORPKvVv9oynvdVF4RdfTYOqbJhTJp0FqDe32gfFwjqAWLUqsvtHtHvB7zwsPWOunHkYviH9J3r6T0MedcPT5FXQ3qAEHUCcYPb6/2nnv8AHjUr1mbF/D6gVBPjtObMo0s+ZBmA07ZV3WJn6pcXp1iy49bAhWAYgDvz57GcPWZ9WRtypP59Z3Y/QScf46tr0Ot628Cq66HYkLlVaWhwF/3POGjqMzNn6ZxQBBx7D5v4+YnVZs2bFeR/U0qADqHsH0+Z1dB1eL+HOPLRKjsu5HcE9xN2ZNifpk6bCxIVMSjcsCoJRT9d78TmOFMOPT0q6mcEHK4plHgKePrvF6/qD1HVpkAKqtFFqjXn9p09A65C5KFlC/zDWoDeTuTRPpuizKqtl1IyuRTcfffj6Tm67Hr6kLjdspW03FHbvXie3k6sBFZVDEWKqiPBnFjVc9tQD6Sd2rbvM8eV36pXlBKxKKOo+6vjtGT+YyklQQaLMLno/wAFrDHKQij3kr/5wBHwYMePRkxhfzA6ie/wJ1v8kTHPlxqSjpYLEp7k2F9yfNdu0fqcbepmJZwAtNQB1bCp3nSWBQB8Y1BF0i17/rOAZvVzltvfsoYhqP0nOcrVrhXK/Tp7gG17r7tx8xlUZgzpsSdllPRx52CqVRQbav3qbN/IxgYsZRS9jIw9x8C/j4nbdZFVyYm3GrsZ09P1OH1R6uMrQ2I33+ZbF/OyBMqk5BuWU+1zOnF0eNjqVBR228zly5z9WSsoxPo9IkAm6G9zpXGuQviF+z84GxE5hgKU/TEY37MRx5lz1GlMSNhyqSrEsdgT3J8zHVaT6h0KGgLBom9yZxZiF6i2r1VYe877+PE6k6ctePHjosbDvxXj/M4eoCrS5HbIO2jevp2m+MKHUZRk0g5RsxFjv9p0dDkVlY5gQW4I2r6Tg69geopE9NgKdR2MnhynGQdwL7eJv52M729fKtY2dsoQNsFvb/z+84lctas5q+f/ADtLa8WViLStAKDcA1xfg7x26MMVyuwxbWVC8jsa7THUaJjxPirMyhlU2KPPiVz5MZCupLZO4InRixvlGUaVKoRalqAviI+FmynGVGy3fFfFTP1+1XBmOLJ+HNkzYMjZ/UKh+ANu/wDqc3RNlwvsxAbxPZ6oY1xOjaURa/llN/ufMCdFjCFgjGxyTNXnMxM7c2PrXCUwLv223lh1WtbyEpQ9wO1SxwLjchcYpe5sEyw0PjUFMVAVso3+s528VcnSumdmFMGX55HmdZx6lG4J76uBAVTHi0gbKSRfkxMeR0I3UNyd/wB5i2W7BvRLN7smjwNO8qv4Z0rilALE6hqe6Mh1HXIqnXuALFDc38zl/j8bNRYbjYEzclTXV/8AhvSIXNnYl2ulG5Pmc+dcXSgjECd68/edy5HCMuNrFWd+B8TnfGqrrLY7Bvfcf9mN2rjlByn3IBROxB7yT+qX0jIWbuOwnZkfCuPUxBUGqDbXILmSx6a1XcDYy9/0mJV6CF8j6q2obzN1pKHSprsYubFqTJS9tzzOd8eVmKvj9Mg6aqqm5xl7qV0J1BYe8kCW9fEQvBPBJkMXTLp9xY7ed5PNmRNtN3ztzJ8y+DszdWmPfGwI86eZr9VyEfXtdggTz1yesujIrAHwI69NjUhvU28neo+ZF12JkQkMy6hXB7GY5cbKTpCntU501ZD/ACyWPG0DYXQ/zCy71vJ8mnLYgRV/Tm5pNhv7W3+k0uDwlMPbfiJGG6z1uYHaDvCd154iiAYQaMW4w4+YDCzKKvNc/EkK7xw/eBmIvba4XbUPbQI/eAgFSx+wiAm4A1HvNtV1DpoXt9JgDxAAuMDv8wEci5gdt4FQdtxzGYK17Af5k9RO0srIVAZfpCObg1KBGZdQG0fQGQnGbI5UiBGIBXi+SYUPTNdhXzMAFBA38VxHC69mYgDjxGXAasPt2JEqEGNjsQB9TMcRB2Kn7x70G3Vo65VPcg/oIERha72o/MIxsOyn7zpRUYE6gN/+Ua0WrYC/O0YOcHKDum3iEZwB+W50rd/0m+KaYlR+cqhPfUN4HOnUA/m2+kI6hb3uF8auwKhSfg7GZceNbDBASe5gUx58YdbNj5G0vm6vE4/LjUVwENCcOQYwfaT9KqTZvA2MzVVbJb2AFFk0O0bE67k1fIJ3+05wx4hBs0ou+1bmQduTOVKn1DR3obcy/Su2R69W1X3s1XpHczz1AKE/sZ6mFx034Y6aVVs4DMTsQv8ASB+hMlkWKZR/EDOMWpj6YYo39FNsPknkmeX1Wd2Q4VyViBvSOL8ztwdQel6HPoW3zUbI/Ko7/rPLe2JZt7O8shSqTdi/iOMro4ZXII3sGpkRnNIrM3YKLMv0P4fk6rPobIuECydX5tvAmrUjpyZ/xDP0Pq9RnZ8JagpNWfO05sPUHp8oyIFYrwHFi/Mt/C9IvUaM34ivpqt2mMnfwB/mAfh5zBsmDqMLY8fLsSg/cczMxrtz5upzdTkL58jZGPkxLZQzkgnmA42Vj/UFO7IdS/qI6e5irbjzNMlbScVyLg0LA+CJZBtpJuj+oiutKV08bwidnz+swO9cQjcbbxQaJkUwJDTsD4z0wLuNY/KgXcj5PacdWLjoXKab9oNi+0lhHVgzLiyDJkUuA24bj71v9pbqOqwZ8fpqzMxqi2OtFc0B2M5lf+IyqG0KCPebIv6/MXqcCYSw9RSw2oKw/uO0xk1rRPUnIqLmYuEGlB4Hi5NA7F3xgsE3YgcDzIqy6lLDUAdwNrnWuPp1Qs2YAPuqY/ew+vE2jagMeNseRtdUR3v4lcWZMasPcrnkcCpwliOSf9TKxDahZruZm8TXp3l6kt/Dj1GVdTMCbrxMmPqMOQ4woYBdZBIuquS6PqMeNTetMjN+dWqlnY3V9K3U5en6PFkYZSAhq968AWR3qc7L41Mrn6brVX1UyH+URsCCQfj7zsPU49wzqgsXjQar27+PE8vrMYxZdAdrXbQwor9YML2VRnVEY0zsLqLwl7TXfi6q79MaTZLaTbG+Oe05s+XAiaUxn1CvuJJ9pv8AeSGZBqNIrEGmO4W63HeIcq50PqZEDLbaiN3vsTLOOVHT07NgwnLpAbY0TzfG32nR0mRs2c5fT1qRpCsbo/UzyizPVsTU6cPUtixenR0WCQTzHLj0SvU6bqMo6wghPTFqpXf9v8z1MeTGEZiSPb/SOSO08PpuoT+GKswxECgf8y34f1T5GOMNpSrdidq77/M48uOtSvQxNk9F3QF2cjY/0XsPtLZsRxYmGXIvHLd/vII+P+CJQM5ajpC7d9j9OYnVdYuX8L04iFCqAVq+PrHyatlw5cmE+k+QcLxt+tyGTBjGMtjRr9pa3on6DxCnUqOnWlvE4BIY+6/Mn13VMmE6KcXRK+Ilu5FDN0vTP04VfblJt3b6+fFbzmXoRjyudS5cYBAJFX9oXLZVAyIwDd/P0nR+Go/pYxlJI4KkcCb5cs4okuFenw+oVB0jgm7Md85xaxmW7Wgx3P0uXzYz6FOpAJont9p5mTJfTnGo2xkKfF/X69pif5eqrg6lPUyE1Z3qvzfB3l+nyvizZVUK+Mj3FuQBPEDhcwZrqx+Q1t8TqPVNhwoyZH1MTv2A8fWbvBJXoZ+rx+n1C46IYUAb8cyvS9b6yqdNja7JC8b/AFnhZMq9hyKr/MpiesR07jTpb4HxH/nkT6e5hydO+Jij0i3fcgxywC67VQAC2/H/AHPIx9VpxhMQ9zbkA813l0Z3cpkQ6gT7lPJ8zF4f2uuherR2KMLo8atpmyDJjKswXwR3gHSYWGze4j8g7TZsQbEgQlQAa38c/eT/AB3pXGTny5C+7uOd9oVwvato0so9wI/NOnDgyK5ayQeQBwIzt7iodVZhtQ4E1ed/EwFtSdI07Ud+3iWRlIAGIsx2Fi9vMgrIBYa7HJk83UZBkULYG2mj+omZNXXS+HI7BVCqli1egDtx9YNVKrvpVQuwHAkVzO6UdWq7B8SWTqmx4UXSPbwD3EuW9GqFmyh7UaBtd95zlVwXrYN7qDA6r+hkcWT02oBtDEHizYlevfG+IZExqjGrUd/J8Tp8/jOqK4GKw5JJ7HeImnIBVkHn4nK2XIqMCSNHFCXwlfa5DWfzE3X3izIuqhUHuN/rKA4lSigPfyYpfGuLcq5ZtlF7fMgmZdQKUKPmpnLR0rn9PJrQEMN1PgyOT1uoyWrkAXt2+ZLI7hhpOrxtsfvHwuwdtVizVAS5hrDFmJ3Kb9zNA2QhueOb3mjKPE52mAOnaYH28c7RgdqnqYTOwMUd4zcVFgYRgaMXeaA93DR8RAa8xvpAfV+3Ag3J+T2mB2viMD3O/wB+IAHfeqhJBAOwPmOGAJ1A6vMU1uRv5EImVo7kRhvV+Y6FR/SNvPeIxNnyYANg32hU7wc7GYCoVZQT7jVA7ybkkknmZWKkf2lGIc2SbA2HaBJDvzU6lyqB2qc5W63v6QaD5jR0PlUqQbMl+a6AHwIgXa7jqrnZQTCMbCi9obVgARR8xvRyncAfS5v4fL4H6wEo2dJAHeoG23BlG6fMvI/eKcbDlTClDA/E10dzBp+1wkGAVN7k7fM1+YtMT9JRU21HdZAqsNQ2veMwYZdBsFdrHaDSBsL+sviD5Dvuas2a2kvQbpsKvlGJjpXksew5P7SnV+kTieyNQOoDevAH0ETL/LZ9L6l3FjbkVEOdsmBFZr9MUtnYCSX9VR83rZGOa6qgqmgAOBXiV9PBmwPkx4dIQChrLH5J8Cec5tqlhkxriGjUGrcdvrGU11N1vUYs65On0YAopVQUBOlcHTZOldvWzHqNNkNsDfP2+s81cmMkE3Z7jaFsqatONdKXxqv9TNYmm9PGyOdaqQt7mr+PkzmfI9Bb9o4XsJmcsQG5mFMCG5lFel6zL0rasJHyrCwfqIztqbWKthZA23nKRV1MLseQYHUKJW7owshbDqsVemLh/m2oG6gk/TzHy5DpUAmwKvzJvY5Aa+0A2/8AOYHPvaa+K4reAwq5XE+NSfURnFbANW/zIA7XCDvJex0hsuMrkx6FZzShSCV+fj6yLHbclj3+v1nTj6ljgHTtkdUIINHgeKnR03RY9YYOcuIUCD/We+3YTH1nq+vOCABSGDWLI/4/WKGCsdI1fJE7cmbGjrpQ+kTWQqNivcC/uf0nA+kMdBJW9r5qal0osQa932A4gDEXR2EU8UOYL2mkVGTaVw9Rlx6zhPpkrTMOSP8AH2nMD8xgZMHX0+JRi/iMtemrVpDUzH/zvJ9RYyZQzKzK3Iax9j3kd6IHedZxtg6FtLf/AC1qBUXQ7Hxv+szeqrlFP7SQPkxdJA2m7zqXpy3Tl6AZOUvcjzUtuIphTCuVXB1KoGtHHcjiu4iud/TUnT2vaSfNkJU5GLMBW/aTsmzz8yYO7plGtBlOnXsupdmHBMbA2FMwU3pu7rdD/mcS52KlGUMp5Nbj79hOzosy4veG0vwPFV3/AGmeUWOx/wAQLGrFaSKUbkzlyu1WtlSbI7fec+ps3Ue56BO7EXXzFZioa7AIsfMk4yFrtPU5UwBtQAY2KO4r+0k/VnJSBm9K7YX+YzmV/sP8QaqWgTQ3AmvmJr2sX4iCmlsd77BdhA/VhjmxguF02CuxIIG08vD1Rxm8YAPAsWP3hxlclqwJezRBuz4nP/zkutSvV6/qG6hsWN1GJVWtJa9IHmedZTMDlKgY6bS4osO1DvIqxqtTbi/v/mdR6Dpxj19R1GRCAbIS11eL7mWT59X1z5hoy6gae72O4Pm+JLLuikPqu7Hg3KZLPRjVkU1R3FHxQPcTmbKXRUNe26PedIzRsn3E7jz3jh78+JNshZApqr224iA78y4juXSzs+PSigcXuPp5Mvi6nTnrBxZ2Y8/62nn4ze119BvLYwUbSp1lhQKHkzFi67m6l0yOGVWNH5o+Zz4+orP6l7BrAEnb5To0nULBPf5EbHgYFWCsA3t3Xz4kySDsfq3Z79VtxuD4nUHOMq2QY39RLAJ/KeL2nL02DDkUv72NGr2FjzG6lwuXTjxhFCk1f5RfkzGTyND1GXX7DyF0gn4nMMp2JoEbqfMLMchpk7DSAe3icuUguDpKfA7fSbkZdWS1BVMoO1nSLBElkzasWgUdP5mHeDHqKsgoMy1bSPqFV0iq5P1mpBbqWFL6YIVQBZETJnLjYHUCN+YqsMmnVeobk9iIufIfdjUnQN6qrMZ+Au7Og1FRpPbk/M6U6hSVD7DtQnAjlWDA1UzEa/bddgZbx1Ndi5F/iWKjvYo7TZcakkgFSd9vmcgbSp/3O3pCnp6nYUCL1H9pmzO1iOX2rwdxxcqmTWAlihHz48mVtTMfaNKg7bRelxaMnv3APmS2Yv6INIRyD8bzSpKq1gb2R9PtNM6uPCJ2qEGxF35jDfaelgpBZjQuotRq3jWao8QFGwNjmat+Now53h8nbmAlTUSeIzDeYDse8BbhE2n7QjYbcwCG3F7ib6duIobb5hvaA4bf3ExttuDtJ2O4mutoQxH2g3FRrsbfaAHxuIVhV7y1oVI9wvc1VSYWxxNpPgwGYX7tWoVvZ3gDDgC5qPioQvaAlnaufMsuZgoDFvtJldI3U/pBqv5+0DrxuXGzA/FbyofQ9k38TgUe6ya+8sMoRa1avodxKj0G6x3TRSEDe2UE/wBpumwt1Dex8QbwwA/vOLH1JUhlA2/5TZerZn1NpXvSjaTP6X/rr6rEMRCNhAbgleDIDEeCi/YwD8Qy+kuP1D6a7gADaLkzlgVLsw7dhAY9OAlnTV1yLkWFUFIr6SYcqtLsDuR5j4crL2U73ZWzJaDX9JF7QNqxm6Kg2BK9TkumDBtqJAoX4r4nMWZ6ABahcz6o5HLbc/4kwTvQPH6RdXaYnUSN6rczSMpt9t50YELaiTQPk1JIVVL52omBshPHH9pUUb2Hbfx8RtBCWaETUBuoJPaULWPO1yiTqQ61vttMNlNjfsbhff8Aqr4/xADYqwIADau93zMwINrttARobbcGHUGEKAfSynvOgvrxilqhvX9zOQnueZRGNdxfMgVltiTtcHfv4nQ7o2JUGNQRy9mz/iH0Am/UP6ViwNNsfG3+5KObiHYn5hHO0BsbQh15HiMwYXpJpuwMVa7iXZgcZKttsa8GZqlzZuqyBUyZGZQAFThfsJzupG+xvxOhCSb3Y8k9xGGNF1ZSQ+IbAE7t8VG4OQoQgNfm43iCXynW+oCjQ7xNBJmgEUH7bzfWqj1pUibCxTJ6tA6N6PBPaQVz42w4MWpNDkne+21fQ/7kQ3I4vvGcu6lmYksdRJ8nvJxBUqAe3F/WYu2pWLEkG7ud/Q/hh6r8MzdRjzIrYjshIBPnfxPOOy+TIDkLO5Y73v8AWLq0sdO3kCEMdjX6QgWb7eZRsakjYXMXo0p+0IfSNjx27xHamqthvxRkFkYijSnSb37yrjHlV8pYY6I9iDte85QAQD+8JArgk+DGCmVVI1YEYYxdFuSL7x+nxPmFBlGo1pJon6Q5M7PjVMja0UDbcVt/eRR2Z1C2WB9tGTvAcqFMrIdtBIq7qAZKJUbfeDKWxtRKXV+03Um2n53mhZGo2LBHBEZsrstlid7o+fM51bbi6jrlZNwaPYiMD/zcjjGFZmGwSrP0qKCceJidF5NitbrXf4gXIfJ5+8xb3+zk7bxip3Nf7R/Sc5PTA1P4U3JyopZTcAEf3jBy5FnfiBmU9PjG+sE39O3+YMP577DeZHdgyLsrqoHB2/f6zozdVqVcPT4yjBuByfnzPPbZVemGrse/z9Ieny6Myu3IM53jva67uk6lcZdiNKHcDwYcmVuq6rEp0LS9l2+s5nZLbSW0XagyuLPgPTsjBUyoSyMe/wATOZ2urPjxqbCMwr3aTdfE5MrN6v5QlEbfEJzsxCmyOavn5llbDjweoyl6aiCNh4lm8fUSOLKCECtqYjbz4/WDJ07HqSmk61HuVRwfEL9Sz5EbUurYBgPy13nM7uz27Nyd73O/M1NFsGDXrLtpCbc/lgzKKAxqXd99uCPp57xcmQv06ooKou5ve28ya5GVSoNA81zLl9Efr3jBWLKBZLdhEHIl8DGygqmBG+03UIxUCgKrv5MoMpUrwVHIj5MCjHiGr3nnxUkU0hg1iux2uSWUdWXOzgaidRG9eIiZihGRhqB2uD0wzsCHoAHYQpi0qWItTwSJnrFNmzYwQVLMCdjxNAmV0AYNsBQHgTSZFeaJuOIQLPNTAVzOzIG7mhI32g3HzA2/MazQPNwciplOk/MBm4q/kQG1AvvN32hojngQjA73Af7xht9ICL4kA07bmAgjiNvUA370JVA7Ct5gfjbxDt3jNR4XT5qAAw+s3tPb/EGmMF71tAKvsAq8Sw3Gx/SSUVxKK1bkD/cocJdf2qH0iDat+0UZW7GptTkitxCFKkkgt9rmCE7Aj7yit3IEBYc0Pp4hUmxEcmL6Zrfa+JRj28xVQ33v4kChBfNfEtjTGCCSxH0iEL3I+83qAcGB1A9OCNODUa31Hic76P8AiVHe4oyEChN6m2xNnmQMAvYbQiwdlo9qi2SB7qiltpKMXJBHN+ZOyDcxO5iE7/EoJazuBv4gJ/pGw7wWaodzHCnmAwAIH6RlS33odqioKFdpXUoXfeUMqhSf+RA+knze9CAMSw79hULqR7SdyP0jUK3gQWTtRpd5rB2F8wksLI4O0KzEt81EOzcCEUdrG/PxA3IHeEAgVzGHGxkzG452hVkbYi5d+oGXGTlx69KBVI2o+T5nIrRgxDAjm7mcHS5xYMKqipkykWzUSFBHHgmcvO8Z2Z2LMSxY2T5hXKVxHGqgBvzMRufj6ROgoNHaU9yP7DzRUyVxgSBVxYOplDgZApxHVVqCQf8AuN1r6HRBoyPj/Nk0c/HyIcDHSTrIY/kGktv5+JPNmyZlxq9Wo0A1Rr5nOeqmgL+3uQL/AFl8eEZcjEqFCtYrYEcV/aJ0q3ksc1VnzOhEZcoGvUQLNb/WW0jkOIes6Dc0RfzKBBjvHpUqF3273Q3+s7cXTL6uqwS3A7m+/wBIuRBkICFVIN+481/4Zn61ccD49OEECy2x87TnFe7bYKSJ6XUY1sY8Z0ivzG43T9LjOdEK0SLcv27k1/ia+siY5MznEcTBtRGMAjTQodvmS6rMuTqC2PAmEAAaUJI+u8t1PvZzvR4sft4AnLkHvAqjsDNQDUb22nU/SHH02HO5SswLBQbIAPcdpz48T5Mq41UszNpUdyTO38Sy9VgP8FmyjJ6RsMB8VzV1Lf8ASOLIaF0QZLV4ExJYj4gAJ+ZYHRmrY/aVUFhyATAAi4zd32mOcsEDn8gIFD+8yFsKaBvz8yijUAq6tR20yF2d+JbHmbGCUNMwKk12loXNhbDk0va3uLI4iGtJBW27G+IGY3bbkzByo0gfrKApINiYEnYmb44mHO/EBlsGx2mYk3f3gIr6Qj5gHGxRgRzx/iZ1pivg1DpuqjOoKB1HwfrIGfBp6fFkDajkDHSBwAah6VFYuXcoqqWJC39o+I60VE2cKwNnmU1jH+EPjAAbK6sT3oXt9O8xbfFQ6jL6jKQTSjSNXNDiKiOcZcA0DzW36xVRm2QajVmhxLHFlxqoIYK4ut9xNdTpANaQDdjm+0mWOrv/ALjOwDgBTXG4qTZrOwqIK6vbYjAtk23IUXzwJOthRHFm4uoo39jAu76H1IoX4iB6RhQvz/iTLkkGUx1kB2UG9pMwO+RmChmJNWREUWb7+PtHwoGrU2lRyT2hL2wJA+AI0Q0AVAdV1ua2qUcMzEsaA8wuhUDyeZdAx5WSytWdrriFnJbUd2H3kSxINcDmPSkAqaJ/plwdSM6oMjbg/PMUsWck9vERtSUNmBAJk0JDAhvrczirsDxYque00VmBNg7kUfE0QcNwEzATHtOiCPEPaLCfJgMtQ0OYh2hUyIat+Km1b7TUeZqBOx3gZSd+aPM1j6TNfbcTcmiIANXzNzsZitnYwUVMqmoUfMwuZTvNzztAxBhQ0dwTAKH5ib8CEH5gMWBXa7i6jdw1uKG8OmjxKADW4XfzDrbz+0IXbmoR8gn4gDUxveMqsSTuTBZ/41KBnqgJBlxkg8k/WE48g93APe5g5JAth5JgNXRG5+ICMvk7wVttUoNNCzzxFZ0XgX88QJ0IQlm7sDmHWCbqD1KBC8HYwCSQAePHkxGcm64gJ1HvNVVqv6SBdyt7wMKNE2ZRmAXSv1k6JPEDKRYuMp95uplTbeGtt4DFgV8EdpgPbRqoB+WjtX7wAbVfwIBRirUCBH9TUSVFEG6JkwpJHaMi7kmKgcDxcxJoAj63CRf3gI1bCQDYXX7wLV6n9w42MOnf+81b/BlGOkflHO9RDzuIzbCodILaW2/xAS9qjE1W2/ErRqmVQDwQIoA1jUNuRAUHfbjxG2I2FRQpb3Ej5jbAUBAyGlY7Hb/MfEASeNvPEmKuvMvjxlgdP5VWz/uSqQlmItyN+SanQXDA60tyAbv/AM3m/hyenOUIzY1YAsSBbf8AniHp+nbL7iGoAdqv/r5mLZ6Dg0FyCQo/5HgSlA5Sq5AVJ9jEbXOR20ZdIUbc0djvGJcgqSCTdgeYxXXh6lg7VS6bIN8douHL/OfUAw33O/xOHG5ViOKlFyU4KtpDc/EfJru1hMtMN0TYN47V8wPkCruBq7jvfyZyvmByGidtgw7/ADFy0VUhhqJogf3k+TT27flYsmwrkL9psfTtl6gsmlQgti35VJ734nRixgJtkV1Gxof+bxcvUbViGgartjZsd/Em/wBBV6k9DhyYMHpZQWH8306a+4B5A2/ecGRi5Ymyb3JNymU2oI2G5NnvK9P06Ph1vrGq6obCuD+s6w9cBlU9MYixc69QGmu3m4eoCDJpxjYbXd6vmFDp6fJ/96X97/1FQmV9TUv5RxfMVFLbfrBVyyo4weqVJTVpv5q6jwZdKc0dvEUkE3e3iFLZitj3DvJ8naIGNAbbxABW+3iObJg0+ZQvBuHYjcbw1vNVQARQmHebeEEQGVt6MYEAGjfwZPhob3kDKQG+JTNl1lQRYVQK+klsCD4MxNNYMmBwVoemSpAOssdj4AgOUgMFNWb22Aij3PTMF8kwOmliAwcD+ocGMDBjpokkA+YMgGslb09oUOlrF7eIdBI1DYX+Y8QJkgGu4gvczaf1go1NA3XEdGqThHNQOlMnt0ni49F2+k518yqNpG8xYNlIDBQNlAFX37x2LZCdNHfvAwDA3yeDUbGqIus7k7UDxJqlK5CKDEdgfiD0XuzuROxciPpTGQCSBRq5si5AC2pRjU3d7E/WPrFxyMp07ke2SjF1Pu4I7eYzKgxatXuPFGa1lEnaaZ6qwZpRC4SbMFwg7TQEK/MB4moVsYDVtd7TAVvVwccTAwGFw1Vm9h3i97uaiT2kDijAb7Wd4KNw3v4lRu0B3O8O83I4hQqu8wY1vvMBGAJ73A223eNQ3vniZULcC4xxuu5UgDvKF2+R8xtBY7PNQoW2r6GONKg8QgBCNtVfSFsdfmYEHvASOasfEbWexABgAoL2JP3m0EDdGoQ6gKGmjyLEJJrx/mAp1ADYj7QaQeSf1h9QA3v4jEhk/NXzzCsEAGxo+IuTSSb2+kUml2e/tCoLVYZvrxIJ6VvZr+ghoVdS4Q3/AEj7zbDdgDXkwI/+biIQb7zoZiO1GJWoWCPpyYEgL5h095nYdloTAaga3AFwFIrgXBZO0d09OgzAk8gdotixQr6SAk1tcFb7m5bAi5Xp8i4wBdkEzNjxrh1DMhYmtABsDzCJnirNXF1UQL25jGJIGJE3aJdmEntAa9q4BhA22ijtcpj076r42od4Ax4wz/EbSCaHeEKVazXF7TFhIABvsZiAEsXff6wK2wBoTWTt2lGoX/qMQum+W8RSe8y6aNkjwYU3tGOqAN3ff6Qo1HtFZNJGk3Q83X1ig9/HaB3v1jHphjUErjGy0KHkn5Mhizs2NsXqMg7AnbTIaz5/Nsb4kxYayb+hmfmLrod3VjkKDSd12oXxJ69vcalhmR0RchNj21YAC/7ucr7E8GttpYh8/syf07gH2xC29DgRbvfkxi2oi9gPEocEgE3uDtNqo8/aIzCYDYQOnGzNTWE/85gsHzudjI6jvye0fHtRsHfeTBdQQ3tKkjjggH6HmM3VAoyJhTHpXY17r7/52nKzWaXtx5g5+/7xn9roBS7+STDmX09OPYkbmjtvHwD3naz283Gbpc5yUcbamOy1v+kW9o5q7zo6lRjKYlr2oNRHcnc/3qdX4f0hPXY1z4yF1aTqS1vwZ1fiXR4vSy5EWsr5iEA3sDah4He5i859YudPDreOuJ2UuqMVWrIGwl8/R5enRWyadLbAg3N07jHkUsWGM/nA7j6d5rdmxD9F0GTqup9E/wAs1q94I27158zr6v8ABMuDpcJXVkz5MjLpXgKO86eiynpcqZ/4rH6GS2TESbA42v8Atcv1n4j06YP5eXK7uKLFb0Kd9IB/vOX3y1vJj5ojSSDViAjbadfrfw7k9K/NHUyCxOa7sk2SbM7SsErebT3Mcja4h4lQd+O0wAANn9JgDphraoCMaNQjdgOPmZhuTNRqFYjSaMGt9IWzV3XaNWoDzDQrnev1gIT2B/7lFdVSgWJ7eL+ZPTvxMFbY1AOvcmht8QGzux3jgAXvzFcV5+d4A00NyJq91A3NUZdvpAY1sOK2monYfaAftHC8HeBt19rXY7eITk81FbUzWd4hutxJga43qewjSp+e8lvNdS4MYdQK78/3iEwSgkkzTXU0BBCRQEB2EPMoWG6mqErXMAkWL8xahBvaoRv2gAD5jHnjjvABvxMBcAk95iDZgrzCB9oCgmNY+kNAf6qb7QMB88xjstEbRfvAPmAy7n5lWORBsxkrXiozOCK7yiYc+B+kOrewauCrhoEyBhkYcMYAxvkwaeKMwUwHLbd7mt9/cf1i0RDR8wHXVQtb+sqG1Hcb9tpGz/y+8a3rt+8ouoUEkqfqBEd17FufMS3PcKPiDTY9zftvAxybcCKH8gfWEqK27waAdqMgJfVtV/MUknhfvNRGwBAmZdzufiBgs1e4CwPkzAHTYuuLlcGAZTqfIEUcnk/pIDkyYxiKYUKhq1FzZNePEgBOjN05xlgHRtN8Ht5kgjFQVBNmr7XCCdCpQ3YiySOPiTUgMCRYu68wE1tMtKw1g13A2MCrOjA68QBItWXavtI/38S+bFpQZiMeh9lCPek/MgSBXxAXvDdzCjxMKDeTCnW652h18ad77mZSrAarNfMFAir4gMHIWidourzC4CrudzvUmDv3hFDyO/mCxcB23PHaKWFbWIUxNn4hFqN++/MVfGmzzN2vvAo2wHAIO4k2begdq794dQAsjfte8mSNzUCoWk1N9K+Yneo6t7QSL7iITcgsy6MaihZFnvJMy3xX9pu1RavfwLqAfyjetzATd0YwQsxJ777TZFAqhX3uUBZibFgVAveYj9YBDb+ZRdhf6SXB35jKe0C6proLpvfc7SZ2IFVCHI2/v2mJvn9pA+rTZC2fBnodR1rZejwLt6qDSWo3XYEnmca4lIW8qID+bUeP9wBTZSyVB/lkpWoefpOdktV6P4V1WQ9UvTsoQsxLZEux9RwZbrc/TZcJytoZ/cuPHp+fzHxPM1hUKAqrk3rF2fj6RURihYUSpOrfn5mLwm6uutc+AKWcvkQAAJkIJvvOjHjx5tLLjXKE9ill2H/+M5+k6DLk6gGgCGsoyniv0+09jBk6XokxDRqy2Bdb6vF9pjnZPFk1k/CcObCz9ViCv2GM6dNdp5PW40x9SRiJBU6Qh3oCqnr48/V+v05yABAf5ig7sAd2PxvPO/Ehkz9d1T9N7sPrWCtGz8Rwlndq3x5uXEPVIG/BJA2+spg/DM+fHkyppCYxepjQb7xQz4nIyKdQ332IlM2f1sAU7VsFGwr/AHc7W8vxjpzvgdemXJp9r73Y4kAtmo72SSeZkHk0PM2jBbFmgPMDCvpKFwx0qNPjePpVVpls996k0c4AJitzUq30iuPPMsExsIfzV5hUbkGiKg4lGH5rMOqxxCVtgeARMy6SaHEmhSdrg+sB4jqupdyBKBW9XUbSU+/iDS2oVvtcZmYi+3G8I2Ogaaq+I5APf95K9qENkc7SgkUO8VSSd+R2gJ8GHHuxuVT1pNyT8muDOsIrYrbIqt2BHP3nOwDcbSdUQOxmj6RFIgCpppoAAuYCMNhNKMBDUB2hEqBpHPE1bwgwHYyDTcTcQQojbmEUNxAIwEAUD3hugO/1m+eZiAIGqzyBAftDXzDUBNoaHiHzNXeBquEL+kIP6wj3GAaG1QG5iK4ImUG+f1lGsir2uAixtYj14H3hA7wEXUvEtq1KOAIprgjeD2rzAJJU8gxC6nzHLYq9uS/tUBCWKO/1gJZvbVU2o949+DXzUUgSBnyAnTjDUBW8SxfmFCFcMVDAHg94zZmbIzkAFuaEBCDVkEDtL4Mi4sZoe4kX8jx+sObqGzdOPUyAtqvSFnNcI6fVXLkC5D7Ls18DtN1eZcgREtVXhSBQnIbO4hYg9gN40wVS2GlSx8VcU6mIDX43hsg3dfMrkzl0K5GLOeDUgy5WxaipUlhTWoInMQBzv8CE8C7g+sDLW5/vCL1bzabG28qqBOTf0gBEvbtLBF2oUYi1tdQ3RAB/9S/gR1sXXO1yZQg7ipc7gm++wmXfbYQJlTpruxsRAv8Aybnit5W9mrcd7PEDICtmhJikW/6SBvzDlsWTuD+0IUBQYrA8GAo/NeqhCyVuvHmCj25mNhSOYAsbgWZlB7jaL/UL/aOGa7BgUGkJydXYdoVxqW24I4iKPduOZ0roBDUGA5HEuAPh0sQyBaHIapIIncsR27TpVkY3kByGuLi48beurFAwo0F2/vCOZ00Cz3En7muhsBZqdDpqHu1KQaNyS0quOSRXNSKmJUYslgAbsDX2iAVLH1ggzbhbO/yZev0TQamA2H1NQkkMRt9oAZuR95BVdOoFtTeN53nrFzuVd1xKB+cAktQ2AnnDceDOhcQx6MmQoVP9INznykvqyosMjkZGRwWNWe5hTJkQlUb81WPM9But9bpfRx4wWB/lpydR5P8A1PMKsmTQykOuxBiXfSvc6T8T9LpmxZ+nDOHAanIdj5+o8yPXYfxRulVs7Nlx6y+gHUyk72annYsipkGQltQIIo1OjD+L9R03UO6G8b3eNjtRmPmy/wCK7/YYet6jC2vHmcXzvyPECdb1CHJjxsgV2JpV4J/4+Jzaf5AdQ2x0k9pgQFZask7HxN5E1ZjmehkZqU17jxFIYkpW4k/XOxHINn5lGGX0xlcWHOzXvL4Ah2NjUB2gJ/pAoE8Q42F3wbnWOhIypeXFocWG1bSWyejiUe65QsCwNUePvOnqcGliuYrjarIAsjxONyAfbddriX6PDlbs9hzE2s3RriZX9pBOx5mdQCKNk89qlRtPLdiak9JZqAJMqp1Dcjx9o6MFB1VQ/eNwTI0BR/VW81e3cA77fMag2Qn7whGJHgb/AEhUioPtHN77RK920voIZiARzVwripQT+kahMa6VBJXc8d4pXubrzKmtW29bRG48SygKBdkGorkEmuO0KnfeEqDNaJouptzUOkg7R14gLnvAAfajM1VsYh3JMF0ZQSKindYTvMIE5o5E0gXvDQgHmG5UaGqi3D2ga4DCYO0K1zTVNA0I52guNAPE32m+e03PeAQBcI4+kAO+8N+OIANHj94CO5oDxcY7/aAL3qBgAeIboVYgh0itoBRgDbfsJRKY7V9xUlVHao6sByIFkA1VQbzU6WxYVA9RQDV0rX+okcYxuBqQUO/Fzrx+gi68eHGSWoXyPoP8mYtbkc+Xp0VARtfYnf8AScrGhpZf1FT0uo67QxZMSjVWzHx8CedkzM5JKKLPYSy1LISwDYBvzxNrH0+olV6V3xDJVA3VxcnTOh0miR2Bua1nEgwDAmjvvLZc5zblUVVFAAAf+5zlTW8wG+/EDoB1dOdIVRdHybkG9pqwfpMPELqALDA/AhDnMvoKgRb7momksTp9wvkSRMdXZMRW9m7SDNQNA35qGiANW1+ZPsTCw2u7viASRZ0m/mKdxxtFjWO133gAG2PiDe6jbDbzCNhZqAyoKGoxwaN8/wCIgBIs7X37TWD5+sBib3G0B2/wfMB7HgdoaBG43hTJZsmqmakPJ8ERQKFcQNt9pRiRqNcSijVs0gDZjk0fgyAvd7d44FjTX1iLk03XeUU2CcnJ3vvLiBRxgsou9rPaIQW9xAlG42A38xOB+U3EAC2R7RLDEgFswC/3k8TJrt7K/pLtl6chgMLH/iS5uUc7AAkpagee8UN3PMLtZPYeIpQ/1Aj7SKfEGKli+lTzAxGrYmhwTEG236GYC94DhyQdRuAb/wDY5irYb4E6AwIogMT3EAYRvv8ApOvqSCujagKIA422nNjYKrfy11Xye0QuQ51Hc8yWdiPHMF7bzpYJkVURArf8u5M5mtbBG4O8gZbYxuQdPHe5PVXE1m7uAykrZ3v6xdRJ3NwjwYpG9wOpXxpjUYQzZrNvWwHav8zdQo9FGxEMgADGqIY8zmV2RrBo/EJ34kxdFGNaR3hY1amxRrfmSBIMriT1HOpqA3YmVGYhm2GkRsTU2k8fWojUrEKbXzVXB2gdD5BkcaQSBsLoGvmUyki1xspA3BG/1qciuUNqxU8GUxsSRXb9pnBVnLr7iSSZLJseQZTKAFGllBG93uftINubJtjEB7CNY0V38xVNpQ58zZSbAJBNVtNCiGgSYNRLWeOK+ItkA/5jYSC1kjaRV+lUjICfy/Mvl0g6EGkc2e8krfy7VDq7VFD2bNn6mYzapygI1GxF0ktvtOnphgc6eo9QKeGXevtOrD+FZuo93S06XsSQD9xHhjymWgfiF8iP02PEmIBlJLP3P/UrlxMrtjdSGBog+YmBAMpDAgd65mv9oRMRLAbSbqUemFT3VXp+mxKwxFwRuHH5fn5Ejm6PBnb1AyjelVf6v9TnP5e/9NfLx0Ne49ozshRNIGq950dfhxo4XGuk1uAbnFkGna504367ZvQbhjtFJ8R/6S10ZPvNow+YwqBRZqEqQd5RjNBc0BBNFjCA3MBmupr2hAMEJ3m7QrXtNDVGoCO8AgTXBdCba4BjRb3hHEA8bGYfHM0I+0Dbnx+s33jhG70B8wqq3WsE+AJROvMxBB73HZaYVuPpFfYyAA2d/wBYS3iKT8Tdr4gMHIjeqwFftJ/SEc3zA6sZwrjA0Bm7k3vOkDphjDIrbHcE0BOAE+YwyMvDVJYuu8Z8KoAmJwu9kmgfuf8AE4eo6nLkJB0gXYCj/PM3rOxNuTfzJNp8neScS0TnYoq0PbfbmKXBX8u8VqHG8WVB1QEm9oNhzATAopXUtqKPJgye7K1edogIA7kwlz22EAilB1C/iBmutgPgRSdXavpNAK/MI7+IAN5uRUB13O0agexvvFxnSb8xy1D4hArz3h00Ow8TA7Xz4hYgmzAF9h2E11wd77xT+bbjmDvv+0ByeaFCKTe0B2/NAdgAIUvfaNd/aLxyd/E335gOIxYk2YoNCjNdbAShtVnvcBazU7enKBQcXtYCyT3lX9JxbKjse4Fy4mvMuhHwgMffdf3nZn6TCN1YGtzRrbxvBl9PKUxrl9NAPFhRJhqGhCduOQY7MaYNZHNSLIyMwU61HDCPjIbY9+TKCcIAvn6dpPOAoAWPly7UB+0mTZom4DJiLDwB3JjlWp3cKvgDv9Iqvoequu0LsrAqBfz4gSsqfabE2q+d4QpBIvarEX22K5uRVvUrCEoA3eruZznuexlmXa+REONgoNjSTFEgaG0IYkgbRzpB4uYst7CZCkgd94GY8VQmOmrHJ4i/WUMD8Qbgzc8bQAmA1aj2EYBgO9GBW08c/MYZTxe13R8yCmZAmNLUo9Wb/qvv8bSLAqPMfI+vIXOxMxUgX2/vJArexyDRI8d4+oAV3ik7DbiITvKLWuxUUe/iI2zeKgUgAnv2jaRo1ahd/l7/AFgFSoxtzq/aKv8AaEVdHYE8xioViAdVHtwYCPbVQMYKVxaj/VtLIFK3roDsBDk/mGzsAoAk0JjfcGzcZCL8jxJLYYkHcSmMgt4hX0fQL0OHpw5HqEjdnFBT4g/isK9QMeP0sLPw637fgzyG6oqyjGAETcA77+Zz5s5y5Wc8kzjP47u1v6yPeHRJTermw+7gKdwR3EfGmP0zkcYiCKNruB5nkdH12TEoW9QBuiJ3Y8g6wuRkOMUSRp2Ezy48v1ZZSdR1OI9JSgKPyg+B4nndN1LYcmxB3sWL3nd/CYseErnYBn/KSdhPJYAMwBsA7HzOnCcbLIzy316L5WXIPX0BiLD8zh6p8eV7Qb9yBQMn6jVR3HFGIJvjwxm0e1GDTLIpdaHMDoVJsURNahcYA3PPaO7lkAIuu/eNi0EnX9o/tDgUtdzH6rkYXNLZ8WimUght68TS6jihmmlGM3aaaAYJpoDMf3mmmkG5gmmlGPMZBfeaaBjtBZmmgPqJAB4g4O000BgdqEBNfM00ADcw3vNNA02wP0mmgNe11BvU00oxO1TccTTSIzLXeT7iaaKFPMwHczTQrNvvFM00ArsYW2+800AdoyzTSB7jL7laxv2M00BSxAr95ibqaaEKSTZjYzp91WfmaaFDKS3uYkkmIpN8zTQFPMK/M00odSSd+3aZtiDc00B0Yodp6WDq2THjVEVSN9QG/wBJpovgfP1ZOL+ITGqu7VZ3qeUzMXLE7nmaaEIWNUDQj4TufpNNKo1qdr7TUCRNNAXIPeK2jr7mW/pQmmk/QMwrMwGwBknmmgU9Q6QaFy4IGEbAjxNNA53kiZppAV3NGEgEGhVTTQFJ2mmmgNscZ8iZN2mmgFhUYuSoFVU00gUEx3UHCclUdWmppookJRFBBJ7CaaKDVAnmBmJAmmgUxmiDzKPaoaN795ppL6N041lieVW+PEcg5EbMSAb4AqaaS+tfhCaG0jZuaaajJ1JBnsfhGUKuS0DHTY3mmnP+Sf4tcfXndbmy5c7DI5bSaHxIhfZqvvNNNTqJSsIt7zTTUR6GIB+nOwBBG4EZ8QHR5HvcfE005XqtRwX7al8dDETW8007fiRgoHTljudQG8000D//2Q==');
    /* 背景レイヤーの可視性 */
    --bg-photo-op:.42;
    --lamp-op:.55;
    --noise-op:.07;
    --humid-op:.55;
    --vignette:.40;
    --scrim:.46;
  }
  /* ===== ライト(点灯)モード = 明滅なしの明るいライトアップ ===== */
  body.light, html.light{
    --bg:#f6f3ea;
    --bg2:#efeadd;
    --card:#fffdf6;
    --card2:#f5f0e3;
    --line:#ddd3bd;
    --gold:#c67a3c;
    --gold-dim:#a4632d;
    --gold-strong:#c67a3c;
    --grad-a:#c67a3c;
    --grad-b:#bd7a44;
    --txt:#2b2a22;
    --txt-dim:#736d5c;
    --red:#cc3a2d;
    --blue:#2f6fc0;
    --green:#3f8a3f;
    --wt:#3f8a3f;
    --bg-photo-op:.10;
    --lamp-op:.22;
    --noise-op:0;
    --humid-op:.16;
    --vignette:.06;
    --scrim:.04;
  }
  *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
  html{background:#0a0a08}
  html,body{color:var(--txt);max-width:100%;
    font-family:'Noto Serif JP',-apple-system,"Hiragino Mincho ProN",serif;
    font-size:15px;line-height:1.6}
  body{background:transparent;min-height:100vh;padding-bottom:40px;overflow-x:hidden;
    position:relative}

  /* ================= 背景世界観レイヤー (コンセプト移植・全画面共通) ================= */
  .bgfx{position:fixed;top:0;left:0;width:100%;height:100vh;
    pointer-events:none;z-index:0;overflow:hidden;
    transform:translateZ(0);will-change:transform;backface-visibility:hidden;
    background:
      radial-gradient(circle at 50% 15%, rgba(74,98,48,.22), transparent 42%),
      linear-gradient(180deg, #1a1f17 0%, #161b14 35%, #0c0c0c 100%)}

  /* VHS 走査線 */
  .bgfx::before{content:"";position:absolute;inset:0;pointer-events:none;z-index:9;
    opacity:.12;mix-blend-mode:overlay;
    background:repeating-linear-gradient(to bottom,
      rgba(255,255,255,.03) 0px, rgba(255,255,255,.03) 1px,
      rgba(0,0,0,.08) 2px, rgba(0,0,0,.08) 4px)}
  /* ヴィネット */
  .bgfx::after{content:"";position:absolute;inset:0;pointer-events:none;z-index:8;
    background:radial-gradient(circle at center, rgba(0,0,0,0) 50%, rgba(0,0,0,.5) 100%);
    transition:background .6s ease}

  /* 暗モード背景: 闇の岩峰。文字可読性のため暗いベールを重ねる */
  .bg-photo{position:absolute;inset:0;overflow:hidden;z-index:1;
    background:
      linear-gradient(180deg, rgba(12,14,12,.42) 0%, rgba(10,12,10,.30) 45%, rgba(8,9,7,.66) 100%),
      var(--bgimg) center 30% / cover no-repeat;
    opacity:.95;
    filter:contrast(1.04) brightness(.82) saturate(.9);
    transition:opacity .6s ease, filter .6s ease}
  .bg-photo::before{content:""}

  /* カビ (四隅の闇・控えめ) */
  .mold{position:absolute;inset:0;z-index:2;mix-blend-mode:multiply;
    background:
      radial-gradient(circle at 0% 0%, rgba(0,0,0,.62), transparent 40%),
      radial-gradient(circle at 100% 0%, rgba(0,0,0,.58), transparent 40%),
      radial-gradient(circle at 0% 100%, rgba(0,0,0,.66), transparent 42%),
      radial-gradient(circle at 100% 100%, rgba(0,0,0,.66), transparent 42%);
    transition:opacity .6s ease}

  /* ノイズ */
  .noise{position:absolute;inset:0;opacity:.07;z-index:7;mix-blend-mode:overlay;
    transition:opacity .6s ease;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='3'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}

  /* 湿度 (上部の光は消す) */
  .humidity{position:absolute;inset:0;z-index:5;mix-blend-mode:screen;
    background:
      radial-gradient(circle at 18% 78%, rgba(40,70,28,.20), transparent 46%),
      radial-gradient(circle at 88% 38%, rgba(70,90,38,.12), transparent 36%)}

  /* 灯火 (明滅は読み込み中のみ) — 大きく全体に及ぶ */
  .lamp{position:absolute;top:-200px;left:50%;transform:translateX(-50%);
    width:820px;height:820px;border-radius:50%;z-index:6;
    background:radial-gradient(circle, rgba(255,212,125,.40) 0%, rgba(210,150,60,.20) 26%,
      rgba(130,90,35,.09) 48%, rgba(0,0,0,0) 76%);
    filter:blur(14px);mix-blend-mode:screen;opacity:.9}
  body.loading .lamp{animation:flicker 11s infinite ease-in-out}
  /* 不規則でゆっくりな明滅 */
  @keyframes flicker{
    0%{opacity:.9;transform:translateX(-50%) scale(1)}
    6%{opacity:.5;transform:translateX(-50%) scale(.98)}
    10%{opacity:.95;transform:translateX(-51%) scale(1.02)}
    13%{opacity:.4;transform:translateX(-50%) scale(.96)}
    22%{opacity:.9;transform:translateX(-49%) scale(1.01)}
    27%{opacity:1;transform:translateX(-50%) scale(1.03)}
    34%{opacity:.38;transform:translateX(-50%) scale(.95)}
    38%{opacity:.85;transform:translateX(-51%) scale(1.01)}
    47%{opacity:.55;transform:translateX(-50%) scale(.98)}
    54%{opacity:1;transform:translateX(-49%) scale(1.04)}
    59%{opacity:.42;transform:translateX(-50%) scale(.96)}
    67%{opacity:.88;transform:translateX(-50%) scale(1)}
    72%{opacity:.34;transform:translateX(-51%) scale(.94)}
    78%{opacity:.95;transform:translateX(-50%) scale(1.02)}
    85%{opacity:.5;transform:translateX(-49%) scale(.98)}
    91%{opacity:1;transform:translateX(-50%) scale(1.03)}
    96%{opacity:.46;transform:translateX(-50%) scale(.96)}
    100%{opacity:.9;transform:translateX(-50%) scale(1)}}

  /* 画面全体が灯火と同じリズムで明るくなる層 (読み込み中のみ) */
  .flicker-all{position:absolute;inset:0;z-index:6;mix-blend-mode:screen;opacity:.85;
    background:
      radial-gradient(circle at 50% 18%, rgba(255,208,125,.20), rgba(255,190,110,.05) 50%, rgba(0,0,0,0) 78%),
      radial-gradient(circle at 50% 60%, rgba(255,200,120,.08), rgba(0,0,0,0) 70%)}
  body.loading .flicker-all{animation:flicker-screen 11s infinite ease-in-out}
  @keyframes flicker-screen{
    0%{opacity:.85}6%{opacity:.4}10%{opacity:1}13%{opacity:.32}22%{opacity:.85}
    27%{opacity:1}34%{opacity:.3}38%{opacity:.85}47%{opacity:.5}54%{opacity:1}
    59%{opacity:.35}67%{opacity:.85}72%{opacity:.28}78%{opacity:.95}85%{opacity:.45}
    91%{opacity:1}96%{opacity:.4}100%{opacity:.85}}

  /* 灯火が弱まった瞬間に画面全体が沈む層 (読み込み中のみ) */
  .flicker-dark{position:absolute;inset:0;z-index:10;background:rgba(0,0,0,1);
    mix-blend-mode:multiply;opacity:0}
  body.loading .flicker-dark{animation:flicker-dim 11s infinite ease-in-out}
  @keyframes flicker-dim{
    0%{opacity:.04}6%{opacity:.26}10%{opacity:0}13%{opacity:.3}22%{opacity:.05}
    27%{opacity:0}34%{opacity:.32}38%{opacity:.05}47%{opacity:.2}54%{opacity:0}
    59%{opacity:.28}67%{opacity:.05}72%{opacity:.32}78%{opacity:.02}85%{opacity:.2}
    91%{opacity:0}96%{opacity:.26}100%{opacity:.04}}

  /* 全体を持ち上げる照明レイヤー (点灯時のみ) */
  .lit-overlay{position:absolute;inset:0;z-index:6;opacity:0;mix-blend-mode:screen;
    transition:opacity .6s ease;
    background:radial-gradient(circle at 50% 30%, rgba(255,214,140,.22),
      rgba(255,200,120,.06) 55%, rgba(0,0,0,0) 80%)}

  /* ===== 点灯(明)モード: 明滅を止めて全体を明るく ===== */
  body.light .lamp, html.light .lamp{animation:none;opacity:1;
    transform:translateX(-50%) scale(1.05)}
  body.light .flicker-all, html.light .flicker-all{animation:none;opacity:1}
  body.light .flicker-dark, html.light .flicker-dark{animation:none;opacity:0}
  body.light .lit-overlay, html.light .lit-overlay{opacity:1}
  body.light .bgfx, html.light .bgfx{
    background:
      radial-gradient(circle at 50% 12%, rgba(220,200,150,.30), transparent 50%),
      linear-gradient(180deg, #f6f3ea 0%, #efe9da 40%, #e8e0cc 100%)}
  body.light .bgfx::after, html.light .bgfx::after{
    background:radial-gradient(circle at center, rgba(0,0,0,0) 65%, rgba(60,40,10,.10) 100%)}
  body.light .bgfx::before, html.light .bgfx::before{opacity:.04}
  body.light .bg-photo, html.light .bg-photo{opacity:.78;
    background:
      linear-gradient(180deg, rgba(250,247,240,.40) 0%, rgba(250,247,240,.26) 45%, rgba(245,241,232,.48) 100%),
      var(--bgimg-light) center 32% / cover no-repeat;
    filter:contrast(1.0) brightness(1.03) saturate(.86)}
  body.light .bg-photo::before, html.light .bg-photo::before{content:"";opacity:.05}
  body.light .mold, html.light .mold{opacity:.08}
  body.light .noise, html.light .noise{opacity:0}

  /* ===== 暗モード限定 フィルムエフェクト (最前面・軽量・操作は透過) ===== */
  .film-fx{position:fixed;inset:0;z-index:60;pointer-events:none;opacity:0;
    background-image:
      repeating-linear-gradient(to bottom, rgba(255,255,255,.025) 0px, rgba(255,255,255,.025) 1px, transparent 2px, transparent 4px),
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='f'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23f)' opacity='0.5'/%3E%3C/svg%3E");
    mix-blend-mode:overlay}
  /* 暗モードでだけ表示し、粒子の動き+かすかな明滅 */
  body:not(.light) .film-fx{opacity:.10;
    animation:filmGrain .5s steps(2) infinite, filmFlick 6s ease-in-out infinite}
  @keyframes filmGrain{
    0%{background-position:0 0} 50%{background-position:18px -12px}
    100%{background-position:-10px 14px}}
  @keyframes filmFlick{
    0%{opacity:.10} 18%{opacity:.13} 24%{opacity:.07} 47%{opacity:.11}
    63%{opacity:.06} 78%{opacity:.12} 100%{opacity:.10}}

  /* body全体の明るさ補正 */
  body.light, html.light{filter:none}
  body.light, html.light{background:transparent}
  html.light{background:#f6f3ea}

  /* 全コンテンツを背景レイヤーの上に */
  .topbar, #venueStrip, #raceGrid, #status, #detail{position:relative;z-index:1}

  /* ヘッダー */
  .topbar{position:sticky;top:0;z-index:50;
    background:color-mix(in srgb, var(--bg2) 82%, transparent);
    backdrop-filter:blur(10px);
    border-bottom:1px solid var(--line);
    padding:9px 12px;display:flex;align-items:center;justify-content:center;gap:9px}
  /* 日付+表示ボタンを中央に */
  .date-field{display:flex;align-items:center;gap:10px;position:relative}
  /* 曜日付き日付表示 (コンセプト風・枠なし明朝体) */
  .date-disp{display:flex;align-items:center;gap:6px;cursor:pointer;
    color:#ca3125;font-family:'Noto Serif JP',serif;font-size:19px;font-weight:600;
    letter-spacing:.08em;padding:4px 2px;
    text-shadow:1px 1px 0 rgba(0,0,0,.6), 0 0 6px rgba(0,0,0,.7)}
  body.light .date-disp, html.light .date-disp{color:#c0291d;text-shadow:none}
  /* 明暗切替ボタン(太陽/月)を右端に固定 */
  .spark{position:absolute;right:12px;top:50%;transform:translateY(-50%);
    height:36px;width:36px;box-sizing:border-box;cursor:pointer;
    padding:7px;background:transparent;
    border:none;border-radius:0;
    color:var(--gold-strong);overflow:hidden;
    filter:drop-shadow(0 0 6px rgba(255,200,110,.35));
    transition:filter .3s ease}
  .spark:active{filter:drop-shadow(0 0 10px rgba(255,200,110,.7))}
  /* 読み込み中: アイコンが脈動して発光 */
  .spark.thinking{animation:sparkGlow 1.6s ease-in-out infinite}
  @keyframes sparkGlow{
    0%,100%{filter:drop-shadow(0 0 4px rgba(255,200,110,.2))}
    50%{filter:drop-shadow(0 0 12px rgba(255,200,110,.9))}}
  .theme-btn{background:var(--card);border:1px solid var(--gold-dim);
    color:var(--gold);border-radius:2px;width:34px;height:34px;font-size:15px;
    flex:0 0 auto;padding:0}
  /* ネイティブinputは隠す(ピッカー機能のみ利用) */
  .date-field input{position:absolute;left:0;top:0;width:1px;height:1px;
    opacity:0;pointer-events:none;color-scheme:dark}
  body.light .date-field input, html.light .date-field input{color-scheme:light}
  /* 託宣ボタン: 枠なし・下線のみのコンセプト質感 */
  .date-field button{background:transparent;color:var(--gold-strong);
    border:none;border-bottom:1px solid var(--gold);
    height:32px;padding:0 0 3px;font-weight:600;font-size:14px;flex:0 0 auto;
    letter-spacing:.3em;text-indent:.3em;font-family:'Noto Serif JP',serif;transition:.15s;
    text-shadow:0 0 8px rgba(255,200,110,.4)}
  .date-field button:active{color:#fff;border-bottom-color:#fff}
  /* 読み込み中: 託宣ボタンの文字が明滅する(託宣ボタン押下時・会場選択時) */
  body.btn-glow #oracleBtn{animation:oracleGlow 1.4s ease-in-out infinite}
  @keyframes oracleGlow{
    0%{color:var(--gold-strong);text-shadow:0 0 8px rgba(255,200,110,.4)}
    50%{color:#fff;text-shadow:0 0 14px rgba(255,224,150,.95),0 0 22px rgba(255,200,110,.6)}
    100%{color:var(--gold-strong);text-shadow:0 0 8px rgba(255,200,110,.4)}}

  /* 託宣の意味 (託宣ボタン押下時のみ) */
  .oracle-meaning{color:var(--txt-dim);font-size:11.5px;line-height:2.1;
    letter-spacing:.14em;margin-top:14px;padding:0 6px;
    font-family:'Noto Serif JP',serif;opacity:.8}
  .oracle-meaning .w2{display:block;text-align:center;color:var(--txt-dim);
    letter-spacing:.4em;text-indent:.4em;font-size:13px;margin-bottom:10px}
  .oracle-meaning .w2 .gw{color:var(--gold)}
  .oracle-meaning .mtxt{display:block;text-align:left;min-height:1.2em}
  .oracle-meaning .w2{min-height:1.2em}

  /* 起動時ベースデータ読込中 (Rボタン非表示=raceGridが空) のみ:
     Rボタン2段分の帯 (rcell70px×2+gap+padding=170px) を確保して、
     マリア・託宣大文字・「託宣とは」の位置関係をRボタン表示時と同じにする。
     raceGridに中身が入った瞬間 :empty が外れるので、R表示時の見た目には一切影響しない */
  body.venue-loading #raceGrid:empty{min-height:170px}


  .wrap{padding:12px 12px 0}

  .status{color:var(--txt-dim);font-size:12.5px;padding:14px 4px;text-align:center;
    letter-spacing:.2em}

  /* 会場チップ横スクロール */
  .venue-strip{display:flex;gap:8px;overflow-x:auto;padding:12px 12px 10px;
    scrollbar-width:none}
  .venue-strip::-webkit-scrollbar{display:none}
  .vchip{flex:0 0 auto;background:linear-gradient(90deg, rgba(0,0,0,.32), rgba(20,24,15,.10));
    border:none;border-left:2px solid rgba(150,168,170,.5);
    border-radius:0;padding:9px 15px;font-size:13px;color:var(--txt-dim);
    white-space:nowrap;transition:.15s;letter-spacing:.06em}
  .vchip .nm{color:var(--txt);font-weight:700;margin-right:6px}
  .vchip.active{background:linear-gradient(90deg, rgba(255,190,100,.20), rgba(120,80,30,.05));
    border-left:2px solid var(--gold);color:var(--gold);
    box-shadow:inset 0 0 14px rgba(255,190,100,.18)}
  .vchip.active .nm{color:var(--gold)}
  /* --- 消灯時 --- 文字も数値も沈めておく */
  .vchip{transition:box-shadow .28s ease, background .28s ease,
                    border-color .28s ease}
  .vchip .nm{color:#9d9483;text-shadow:none;transition:.28s ease}
  .vchip .vfp,
  .vchip .bk-l,
  .vchip .bk-a{color:#6d6555;transition:.28s ease}
  .vchip .bk-v{color:#8d8471;text-shadow:none !important;transition:.28s ease}
  .vchip .vticker{color:#6d6555;text-shadow:none;transition:.28s ease}

  /* --- 点灯時 --- 選んだ会場だけ、すべての文字に灯を入れる --- */
  .vchip.active{border-left-color:#ffcf7a;
    background:linear-gradient(90deg, rgba(255,190,110,.20), rgba(40,30,12,.10));
    box-shadow:inset 0 0 26px rgba(255,190,100,.20),
               0 0 16px rgba(255,180,90,.28)}
  .vchip.active .nm{color:#ffe9b0;
    text-shadow:0 0 10px rgba(255,210,130,.9),0 0 22px rgba(255,180,80,.5)}
  .vchip.active .vfp{color:#e8d3a6;text-shadow:0 0 8px rgba(255,200,120,.5)}
  .vchip.active .bk-l{color:#c3b596}
  .vchip.active .bk-a{color:#a3977c}
  .vchip.active .bk-v{color:#f6ecd6;
    text-shadow:0 0 9px rgba(255,215,150,.6) !important}
  .vchip.active .bk-v.hi{color:#ff8f7e;
    text-shadow:0 0 10px rgba(255,100,80,.8) !important}
  .vchip.active .bk-v.lo{color:#9fdcf2;
    text-shadow:0 0 10px rgba(100,200,240,.7) !important}
  .vchip.active .vticker{color:#ffc766;font-weight:700;
    text-shadow:0 0 10px rgba(255,190,90,.7)}
  .vchip.active::after{height:2px;
    background:linear-gradient(90deg,
      rgba(255,225,160,0),rgba(255,225,160,.95),rgba(255,225,160,0));
    background-size:52% 100%;background-repeat:no-repeat;
    animation:vsheen 2.6s linear infinite}
  @keyframes vsheen{
    0%{background-position:-60% 0}
    100%{background-position:160% 0}}
  /* 会場名の隣に第1レースの発走 */
  .vchip .vhead{display:flex;align-items:baseline;gap:8px;margin:0 0 7px}
  .vchip .nm{margin:0}
  .vchip .vfp{font-size:10.5px;letter-spacing:.06em;color:#a2977e;
    font-family:'Noto Serif JP',serif;font-variant-numeric:tabular-nums;
    white-space:nowrap}
  .vchip.active .vfp{color:#e8d3a6}

  /* ============================================================
     v331: 会場ボタン。明朝で統一し、数値だけ発光させる。
       ・全文字を明朝に (bk-v も含む。等幅は tabular-nums で揃える)
       ・周長の Ave. は出さない (400/500 の二択で平均に意味がない)
       ・ラベルと Ave. のコントラストを上げ、背景に沈まないようにする
       ・平均比は色ではなく発光の強さで示す (地味さの解消)
     ============================================================ */
  .vchip{border-radius:0 8px 8px 0;padding:10px 16px 11px 13px;
    min-width:158px;position:relative}
  .vchip::after{content:"";position:absolute;left:0;right:0;top:0;height:1px;
    background:linear-gradient(90deg,rgba(255,200,110,.5),transparent)}
  .vchip .nm{display:block;margin:0 0 7px;font-size:15px;letter-spacing:.16em;
    font-family:'Noto Serif JP',serif;font-weight:700;color:var(--txt);
    text-shadow:0 0 10px rgba(255,210,150,.18)}
  .vchip .bk{display:block;font-size:11px;line-height:1.9;letter-spacing:.06em;
    font-family:'Noto Serif JP',serif}
  .vchip .bk-r{display:flex;gap:7px;align-items:baseline;white-space:nowrap}
  .vchip .bk-l{font-style:normal;color:#9a8f77;min-width:38px;flex:0 0 auto;
    font-size:10px;letter-spacing:.12em}
  .vchip .bk-v{color:#f0e6cf;font-weight:700;font-family:'Noto Serif JP',serif;
    font-variant-numeric:tabular-nums;font-size:12.5px;letter-spacing:.02em}
  /* 平均より上=金の発光 / 下=青の発光。色差ではなく光量で見せる */
  .vchip .bk-v.hi{color:#ff7a6a;text-shadow:0 0 9px rgba(255,90,70,.6)}
  .vchip .bk-v.lo{color:#8fd0e8;text-shadow:0 0 9px rgba(90,190,230,.5)}
  .vchip .bk-a{font-style:normal;color:#8a806b;font-size:9.5px;
    letter-spacing:.04em}
  /* 値が取れていない行 */
  .vchip .bk-v.bk-na{color:#6b6357;text-shadow:none}

  /* v336: 狙いレースの有無で色を変えるのはやめた。
     会場ボタンは「押すと灯る照明」として扱う。
     非選択はすべて消灯。選択したものだけが灯る。 */
  .vchip.has-target,
  .vchip.has-yosou{border-left-color:rgba(150,168,170,.45)}
  .vchip.has-target .nm,
  .vchip.has-yosou .nm{color:inherit;text-shadow:none}

  /* 会場ボタン下部を流れる報せ */
  /* v332: 狙いレースの行。高さ不足で文字が切れていたので実寸を確保し、
     流さずそのまま出す (会場ボタン自体を広げて収める)。 */
  .vchip .vticker{display:block;margin-top:9px;padding-top:7px;
    border-top:1px solid rgba(255,255,255,.09);
    font-size:11px;line-height:1.5;letter-spacing:.08em;
    font-family:'Noto Serif JP',serif;white-space:nowrap;color:#8a806b}
  .vchip .vticker i{font-style:normal}
  .vchip.has-target .vticker{color:#ffc766;font-weight:700;
    text-shadow:0 0 10px rgba(255,190,90,.7)}

  /* ===== 託宣バー (的中レース選択時) =====
     Rボタンとカードの間隔は変えない: バーは隙間に absolute で重ねるだけ。
     文章は折り返して全表示(最大2行)。1行なら隙間の中央に配置。 */
  .oracle-wrap{position:relative;z-index:2;height:0}  /* 高さを持たずレイアウトに影響しない */
  .oracle-bar{position:absolute;left:0;right:0;
    top:-14px;                 /* グリッド下の隙間に食い込ませる */
    display:flex;align-items:center;justify-content:center;
    pointer-events:none;
    opacity:0;transition:opacity .6s ease;
    padding:0 20px}
  .oracle-bar.on{opacity:1}
  .oracle-txt{text-align:left;width:100%;
    font-family:'Noto Serif JP',serif;
    font-size:13px;line-height:1.32;letter-spacing:.1em;
    color:rgba(224,214,196,.78);
    text-shadow:1px 1px 0 rgba(0,0,0,.7), 0 0 4px rgba(0,0,0,.85);
    white-space:normal;word-break:break-all;overflow-wrap:anywhere;
    display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow:hidden;
    max-height:2.7em;
    opacity:0;
    transition:opacity 1.4s ease}
  .oracle-txt.show{opacity:1}
  .oracle-txt .o{color:rgba(225,151,56,.85)}
  .oracle-txt .g{color:rgba(123,178,101,.85)}
  .oracle-txt .r{color:rgba(223,67,56,.86)}
  body.light .oracle-txt, html.light .oracle-txt{
    color:rgba(58,53,40,.78);text-shadow:none}
  body.light .oracle-txt .o, html.light .oracle-txt .o{color:rgba(176,106,30,.85)}
  body.light .oracle-txt .g, html.light .oracle-txt .g{color:rgba(63,138,63,.85)}
  body.light .oracle-txt .r, html.light .oracle-txt .r{color:rgba(192,41,29,.88)}

  /* レース番号グリッド */
  .grid-wrap{position:relative}
  /* ===== 起動初期画面(intro) ===== */
  body.intro .grid-wrap{min-height:52vh}
  /* gridOracle を縦flexにし、託宣→メッセージを縦に積む(重なりを物理的に排除) */
  body.intro #gridOracle{opacity:1;
    flex-direction:column;
    align-items:center; justify-content:flex-start;
    padding-top:14vh; gap:0;
    animation:none;
    color:#ca3125;
    text-shadow:1px 1px 0 rgba(0,0,0,.6), 0 0 6px rgba(0,0,0,.7);
    line-height:1.0}
  body.light.intro #gridOracle{animation:none;
    color:#c0291d;
    text-shadow:none}
  /* intro ではマリアと説明文は出さない */
  body.intro #gridMaria{display:none}
  body.intro .oracle-meaning{display:none !important}
  /* intro の赤字メッセージ(ヘッダー日付と完全に同色・同陰影) */
  #introMsg{display:none}
  body.intro #introMsg{display:block;
    text-align:center; pointer-events:none;
    animation:none;
    color:#ca3125;
    text-shadow:1px 1px 0 rgba(0,0,0,.6), 0 0 6px rgba(0,0,0,.7);
    font-family:'Noto Serif JP',serif; font-weight:600;
    font-size:clamp(15px,4.4vw,21px); letter-spacing:.08em; line-height:1.3;
    margin-top:0.55em}
  body.light.intro #introMsg{color:#c0291d; text-shadow:none}
  /* intro の KEIRIN (託宣と同色・横幅は JS で託宣に揃える) */
  #introKeirin{display:none}
  body.intro #introKeirin{display:block;
    text-align:center; pointer-events:none;
    color:#ca3125;
    text-shadow:1px 1px 0 rgba(0,0,0,.6), 0 0 6px rgba(0,0,0,.7);
    font-family:'Cinzel','Noto Serif JP',serif; font-weight:700;
    font-size:clamp(22px,7vw,40px); letter-spacing:.06em; line-height:1.0;
    margin-top:0.40em}
  body.intro #introKeirin > span{display:inline-block; transform-origin:center top; transform:scaleX(1.18)}
  body.intro .g-oracle-jp{display:inline-block}
  body.light.intro #introKeirin{color:#c0291d; text-shadow:none}
  /* 読み込み中に背後へうっすら浮かぶ聖母マリア(明暗共通) */
  .grid-maria{position:absolute;left:50%;top:-18px;transform:translateX(-50%);
    width:auto;height:calc(100% + 40px);aspect-ratio:603/900;z-index:0;pointer-events:none;
    background-image:url('data:image/png;base64,__MARIA_B64__');
    background-size:contain;background-repeat:no-repeat;background-position:center;
    opacity:0;transition:opacity 1.1s ease;filter:saturate(.7)}
  body.venue-loading .grid-maria{animation:mariaGlow 4.2s ease-in-out infinite}
  @keyframes mariaGlow{0%{opacity:.05}50%{opacity:.16}100%{opacity:.05}}
  /* 明モードは少し濃く・琥珀寄りに */
  body.light.venue-loading .grid-maria{animation:mariaGlowLight 4.2s ease-in-out infinite}
  @keyframes mariaGlowLight{0%{opacity:.08}50%{opacity:.22}100%{opacity:.08}}
  /* 読み込み中にグリッド全面へ浮かぶ大きな「託宣」 */
  .grid-oracle{position:absolute;inset:0;z-index:0;pointer-events:none;
    display:flex;align-items:center;justify-content:center;
    font-family:'Noto Serif JP',serif;font-weight:700;
    font-size:clamp(64px,26vw,140px);letter-spacing:.18em;text-indent:.18em;
    color:rgba(217,162,94,.0);
    text-shadow:0 0 30px rgba(0,0,0,.4);
    opacity:0;transition:opacity .8s ease}
  /* 暗モード: 薄く透明な赤 */
  body.venue-loading .grid-oracle{opacity:1;animation:gridOracleGlowDark 2.6s ease-in-out infinite}
  @keyframes gridOracleGlowDark{
    0%{color:rgba(223,67,56,.08);text-shadow:0 0 18px rgba(223,67,56,.10)}
    50%{color:rgba(223,67,56,.30);text-shadow:0 0 28px rgba(223,67,56,.30)}
    100%{color:rgba(223,67,56,.08);text-shadow:0 0 18px rgba(223,67,56,.10)}}
  /* 明モード: 薄い琥珀色 */
  body.light.venue-loading .grid-oracle{animation:gridOracleGlowLight 2.6s ease-in-out infinite}
  @keyframes gridOracleGlowLight{
    0%{color:rgba(198,122,60,.10);text-shadow:0 0 18px rgba(198,122,60,.10)}
    50%{color:rgba(198,122,60,.34);text-shadow:0 0 26px rgba(214,150,80,.30)}
    100%{color:rgba(198,122,60,.10);text-shadow:0 0 18px rgba(198,122,60,.10)}}
  /* v329: 格子 -> 横長バー。会場を押した瞬間に横一列でサッと出る。
     縦に伸びないので、Rを選ぶまでの視線移動が短くなる。 */
  /* v329: 4列の格子 -> 横長のボタンを縦に並べる。
     1レース1行。左からR番号/発走/印/予想/結果。 */
  .race-grid{display:flex;flex-direction:column;gap:5px;
    padding:6px 12px 14px;width:100%;max-width:100%;box-sizing:border-box;
    position:relative;z-index:1}
  .race-grid > .rcell{width:100%}
  /* v330: 読み込み中の点滅は目障りなのでやめた */
  body.loading .race-grid > .rcell{animation:none}
  body.loading .race-grid > .rcell:nth-child(1){animation-delay:0s}
  body.loading .race-grid > .rcell:nth-child(2){animation-delay:.12s}
  body.loading .race-grid > .rcell:nth-child(3){animation-delay:.24s}
  body.loading .race-grid > .rcell:nth-child(4){animation-delay:.36s}
  body.loading .race-grid > .rcell:nth-child(5){animation-delay:.48s}
  body.loading .race-grid > .rcell:nth-child(6){animation-delay:.6s}
  body.loading .race-grid > .rcell:nth-child(7){animation-delay:.72s}
  body.loading .race-grid > .rcell:nth-child(8){animation-delay:.84s}
  body.loading .race-grid > .rcell:nth-child(9){animation-delay:.96s}
  body.loading .race-grid > .rcell:nth-child(10){animation-delay:1.08s}
  body.loading .race-grid > .rcell:nth-child(11){animation-delay:1.2s}
  body.loading .race-grid > .rcell:nth-child(12){animation-delay:1.32s}
  @keyframes rcellWave{
    0%{border-left-color:rgba(150,168,170,.5);box-shadow:inset 0 0 14px rgba(0,0,0,.40)}
    50%{border-left-color:rgba(255,210,130,.95);box-shadow:inset 0 0 22px rgba(255,190,90,.18),0 0 10px rgba(255,200,110,.12)}
    100%{border-left-color:rgba(150,168,170,.5);box-shadow:inset 0 0 14px rgba(0,0,0,.40)}}

  .race-grid > .rcell{min-width:0}
  /* Rボタン: 固定の縦3段。左ボーダー・薄く・背景の明滅が透ける */
  /* v330: 荒れ期待度バーを入れるため高さを広げ、2段構成にした */
  .rcell{background:linear-gradient(90deg, rgba(0,0,0,.30), rgba(20,24,15,.10));
    border:none;border-left:2px solid rgba(150,168,170,.5);border-radius:0;
    padding:9px 12px 9px 10px;transition:.15s;position:relative;
    min-height:62px;
    display:flex;flex-direction:column;justify-content:center;gap:7px;
    cursor:pointer;box-shadow:inset 0 0 14px rgba(0,0,0,.40)}
  .rcell .rline1{display:flex;flex-direction:row;align-items:center;gap:10px;
    min-width:0}
  .rcell .rline2{display:flex;flex-direction:row;align-items:center;gap:8px;
    min-width:0}
  /* v335: 3段目 = 的中の流れる表示だけ。
     的中していないレースでは段ごと出さない (無駄に背が高くならない)。 */
  .rcell .rline3{display:none}
  .rcell.hit .rline3{display:flex;flex-direction:row;align-items:center;
    min-width:0;flex-wrap:nowrap;overflow:hidden;
    padding-top:7px;border-top:1px solid rgba(255,255,255,.09)}
  /* 1段目: 印は種別の隣、終了は右端 */
  .rcell .rfin{flex:1;min-width:0;display:flex;align-items:center;
    justify-content:flex-end;white-space:nowrap}
  /* Rボタン内の荒れ期待度バー */
  .rbar{flex:1;min-width:0;height:5px;border-radius:3px;position:relative;
    background:rgba(0,0,0,.5);overflow:hidden;
    border:1px solid rgba(255,255,255,.06)}
  .rbar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px;
    transition:width .45s cubic-bezier(.2,.8,.3,1)}
  .rbar i.lv1,.rbar i.lv2{background:linear-gradient(90deg,#3d5570,#5b7fa6)}
  .rbar i.lv3{background:linear-gradient(90deg,#6b5a30,#c9a94e);
    box-shadow:0 0 5px rgba(210,175,90,.5)}
  .rbar i.lv4{background:linear-gradient(90deg,#8a5a1c,#ffc760);
    box-shadow:0 0 7px rgba(255,190,80,.65)}
  .rbar i.lv5{background:linear-gradient(90deg,#8a2f14,#ff7a3c,#ffd08a);
    box-shadow:0 0 10px rgba(255,110,50,.85)}
  .rbarlab{flex:0 0 auto;font-size:10px;color:#8b8069;letter-spacing:.06em}
  /* 狙いレース: オレンジの枠。枠線の上に見出しを載せる */
  .rcell.target{border:1px solid #e08a2e;border-left:3px solid #ffab44;
    border-radius:6px;margin:9px 0 2px;
    background:linear-gradient(90deg, rgba(255,150,60,.14), rgba(60,35,10,.05));
    box-shadow:0 0 9px rgba(255,140,50,.22),inset 0 0 12px rgba(255,140,50,.07)}
  .rcell.target::before{content:"狙いレース";position:absolute;
    top:-8px;left:12px;padding:0 7px;font-size:10px;font-weight:800;
    letter-spacing:.14em;color:#ffcf8a;background:#14120d;
    border:1px solid #e08a2e;border-radius:4px;
    text-shadow:0 0 5px rgba(255,170,70,.8)}
  .tgtmark{flex:0 0 auto;font-size:10px;font-weight:800;letter-spacing:.1em;
    color:#ffe6a8;border:1px solid #d8b24a;border-radius:5px;padding:1px 7px;
    background:rgba(90,70,20,.35);
    text-shadow:0 0 4px rgba(255,215,120,.85);
    box-shadow:0 0 5px rgba(255,200,80,.4)}
  /* v330: グレードマークと種別 */
  .rcell .rgd{flex:0 0 auto;display:flex;align-items:center;gap:6px;min-width:0}
  /* v334: 色はWINTICKET準拠 (G3=緑 / F1=橙 / F2=青緑)。
     そのうえで縁を光らせてネオンにする。 */
  .rcell .gmark{display:inline-block;padding:1px 6px;border-radius:3px;
    font-size:9.5px;font-weight:800;letter-spacing:.08em;line-height:1.45;
    font-family:'Helvetica Neue',Arial,sans-serif;
    border:1px solid rgba(255,255,255,.34);
    text-shadow:0 0 6px rgba(255,255,255,.55)}
  .rcell .g-g1{background:#d8343f;color:#fff;
    box-shadow:0 0 9px rgba(216,52,63,.75),inset 0 0 7px rgba(255,255,255,.22)}
  .rcell .g-g2{background:#7d4bd0;color:#fff;
    box-shadow:0 0 9px rgba(125,75,208,.75),inset 0 0 7px rgba(255,255,255,.22)}
  .rcell .g-g3{background:#22b04f;color:#fff;
    box-shadow:0 0 9px rgba(34,176,79,.8),inset 0 0 7px rgba(255,255,255,.22)}
  .rcell .g-f1{background:#e8862a;color:#fff;
    box-shadow:0 0 9px rgba(232,134,42,.8),inset 0 0 7px rgba(255,255,255,.22)}
  .rcell .g-f2{background:#12b5c4;color:#04343a;
    box-shadow:0 0 9px rgba(18,181,196,.8),inset 0 0 7px rgba(255,255,255,.3);
    text-shadow:none}
  .rcell .g-other{background:#3a3527;color:#cfc4aa;box-shadow:none;
    border-color:rgba(255,255,255,.16);text-shadow:none}
  .rcell .gkind{font-size:10px;color:#8b8069;letter-spacing:.04em;
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:88px}
  .rcell .rno{flex:0 0 auto;min-width:42px;display:flex;align-items:baseline;
    font-weight:700;font-size:19px;color:#f0ece0;line-height:1;
    font-family:'Noto Serif JP',serif;
    text-shadow:0 1px 2px rgba(0,0,0,.7)}
  .rcell .rt{flex:0 0 auto;font-size:11px;color:var(--txt-dim);
    line-height:1;letter-spacing:.04em}
  .rcell .rtop{flex:0 0 auto;display:flex;gap:3px;align-items:center}
  .rcell .rpred{flex:0 0 auto;font-size:11px;line-height:1;letter-spacing:.04em}
  .rcell .rbot{flex:1;min-width:0;display:flex;align-items:center;overflow:hidden}
  /* 予想: 該当あり/見送り */
  .rcell .rpred .yes{color:var(--gold-strong);
    border:1px solid rgba(200,160,60,.55);border-radius:3px;padding:1px 6px}
  .rcell .rpred .no{color:#6f6552}
  /* v329: 判定が届くまでの表示。会場を押した直後の空白をなくす。 */
  .skip-diag{margin-top:8px;font-size:11px;color:#8b7f68;line-height:1.7;
    letter-spacing:.03em}
  .refetch-btn{margin-top:9px;background:#2a2418;color:#d9c9a3;
    border:1px solid #6a5a38;border-radius:6px;padding:7px 15px;font-size:13px;
    font-family:inherit;letter-spacing:.06em}
  .refetch-btn:active{background:#3a2e12;color:#ffe6a8}
  .rcell .rpred .wait{color:#6a6252;animation:predWait 1.1s ease-in-out infinite}
  @keyframes predWait{0%,100%{opacity:.35}50%{opacity:.85}}
  .rcell.noline{opacity:.32}
  .rcell:active{transform:scale(.95)}
  .rcell.sel{border-left:2px solid var(--gold);
    background:linear-gradient(90deg, rgba(255,190,100,.20), rgba(120,80,30,.06));
    box-shadow:inset 0 0 18px rgba(255,190,100,.20)}
  .rcell.sel .rno{color:var(--gold-strong);text-shadow:0 0 12px rgba(255,200,110,.6)}
  /* 的中レースは数字もRも赤 */
  .rcell.hit .rno{color:var(--red);text-shadow:0 0 10px color-mix(in srgb,var(--red) 55%,transparent)}
  .rcell.hit .rnum{color:var(--red)}
  .rcell.hit.sel .rno, .rcell.hit.sel .rnum{color:var(--red)}
  /* 選択時にR番号がぴょこんと跳ねて軽く回転 */
  .rcell.sel .rno{animation:rnoHop .5s cubic-bezier(.34,1.56,.64,1) both}
  @keyframes rnoHop{
    0%{transform:translateY(0) rotate(0) scale(1)}
    35%{transform:translateY(-7px) rotate(-9deg) scale(1.18)}
    60%{transform:translateY(0) rotate(5deg) scale(1.05)}
    80%{transform:translateY(-2px) rotate(-2deg) scale(1)}
    100%{transform:translateY(0) rotate(0) scale(1)}}
  /* v329: displayable(旧ロジックで計算可能)では色を変えない。
     託宣カラーは「稼働条件に該当した(=買う)」レースだけの印にする。 */
  .rcell.displayable{}
  /* v329: 稼働条件に該当したレース = 託宣カラー。
     従来の displayable (予想可能) とは別の意味なので、より強く光らせる。 */
  .rcell.yosou{border-left:3px solid var(--gold-strong);
    background:linear-gradient(90deg, rgba(255,190,100,.14), rgba(120,80,30,.04));
    box-shadow:inset 0 0 16px rgba(255,190,100,.14)}
  .rcell.yosou .rnum{color:var(--gold-strong)}
  /* v329: 予想を含む会場は左端を託宣カラーに */
  .vchip.has-yosou{border-left:3px solid var(--gold-strong)}
  .vchip.has-yosou .nm{color:var(--gold-strong)}
  /* v330: Rボタンの点滅もやめた。読み込みは上のバーで示す */
  .rcell.checking{animation:none}
  @keyframes rblink{0%,100%{opacity:1}50%{opacity:.4}}

  /* 出現アニメ: フェード+下から浮き上がり */
  @keyframes fadeUp{
    0%{opacity:0;transform:translateY(10px)}
    100%{opacity:1;transform:translateY(0)}}
  .anim{animation:fadeUp .42s cubic-bezier(.22,.61,.36,1) both}
  /* レース詳細カード: 左から差し込む */
  @keyframes slideInL{
    0%{opacity:0;transform:translateX(-40px)}
    100%{opacity:1;transform:translateX(0)}}
  .animL{animation:slideInL .44s cubic-bezier(.22,.61,.36,1) both}
  /* スタガー(時間差)用 */
  .anim.d1{animation-delay:.04s}
  .anim.d2{animation-delay:.08s}
  .anim.d3{animation-delay:.12s}
  .anim.d4{animation-delay:.16s}
  .anim.d5{animation-delay:.20s}
  .anim.d6{animation-delay:.24s}
  .anim.d7{animation-delay:.28s}
  .anim.d8{animation-delay:.32s}
  /* v329: 予想タブ (託宣の色調に合わせる) */
  .yos-head{margin:2px 0 10px}
  .yos-tag{display:inline-block;background:#2a2418;border:1px solid #5a4c33;
    border-radius:4px;padding:2px 9px;margin-right:6px;font-size:12px;color:#d9c9a3}
  .yos-tag.on{background:#3a2e12;border-color:#a8862f;color:#ffe6a8}
  /* 荒れ期待度バー (v330: ★の代わり) */
  .ar-wrap{margin:2px 0 12px;padding:11px 12px 12px;border-radius:10px;
    background:linear-gradient(180deg,rgba(24,21,15,.9),rgba(16,14,10,.9));
    border:1px solid #3a3226}
  .ar-top{display:flex;justify-content:space-between;align-items:center;
    margin-bottom:7px}
  .ar-right{display:flex;align-items:center;gap:9px}
  .reschk{background:#221e16;color:#c9b27a;border:1px solid #4f4531;
    border-radius:6px;padding:4px 10px;font-family:inherit;font-size:11px;
    letter-spacing:.06em}
  .reschk:active{background:#3a2e12;color:#ffe6a8}
  .reschk:disabled{opacity:.6}
  .ar-ttl{font-size:12px;color:#9c8f76;letter-spacing:.1em}
  .ar-val{font-size:13px;font-weight:800;letter-spacing:.06em}
  .ar-val.lv1,.ar-val.lv2{color:#7f8fa6}
  .ar-val.lv3{color:#c9b27a}
  .ar-val.lv4{color:#ffcf6a;text-shadow:0 0 6px rgba(255,190,70,.5)}
  .ar-val.lv5{color:#ff8a5c;text-shadow:0 0 8px rgba(255,110,60,.75)}
  .ar-track{position:relative;height:12px;border-radius:7px;overflow:hidden;
    background:rgba(0,0,0,.55);border:1px solid rgba(255,255,255,.07)}
  .ar-fill{position:absolute;left:0;top:0;bottom:0;border-radius:7px;
    transition:width .5s cubic-bezier(.2,.8,.3,1)}
  .ar-fill.lv1,.ar-fill.lv2{
    background:linear-gradient(90deg,#3d5570,#5b7fa6);
    box-shadow:0 0 7px rgba(90,140,190,.5)}
  .ar-fill.lv3{background:linear-gradient(90deg,#6b5a30,#c9a94e);
    box-shadow:0 0 8px rgba(210,175,90,.55)}
  .ar-fill.lv4{background:linear-gradient(90deg,#8a5a1c,#ffc760);
    box-shadow:0 0 11px rgba(255,190,80,.7)}
  .ar-fill.lv5{background:linear-gradient(90deg,#8a2f14,#ff7a3c,#ffd08a);
    box-shadow:0 0 15px rgba(255,110,50,.9),0 0 26px rgba(255,80,30,.45)}
  .ar-tick{position:absolute;top:0;bottom:0;width:1px;
    background:rgba(255,255,255,.13)}
  .ar-sub{margin-top:6px;font-size:11px;color:#8b7f68;letter-spacing:.04em}
  /* 絞り込みの段階選び */
  .stepbar{display:flex;gap:5px;flex-wrap:wrap;margin:2px 0 9px}
  .stepbtn{flex:1 1 auto;min-width:62px;background:#221e16;color:#a99a7e;border:1px solid #3f3729;border-radius:7px;
    padding:6px 4px 5px;font-family:inherit;font-size:12px;line-height:1.3;
    display:flex;flex-direction:column;align-items:center;gap:1px}
  .stepbtn .stepsub{font-size:9px;color:#6f6552;letter-spacing:.02em}
  .stepbtn.on{background:linear-gradient(180deg,#4a3a12,#2e2410);
    color:#ffe6a8;border-color:#a8862f;
    box-shadow:0 0 7px rgba(255,200,80,.35),inset 0 0 6px rgba(255,200,80,.12)}
  .stepbtn.on .stepsub{color:#c9b27a}
  .stepbtn:active{transform:scale(.96)}
  .stepnow{font-size:11px;color:#8b7f68;margin:2px 0 5px;letter-spacing:.05em}
  .yos-card.tgt{border-color:#a8862f;
    background:linear-gradient(180deg,rgba(58,44,16,.55),rgba(27,24,17,.72))}
  .yos-card.tgt h4{display:flex;align-items:center;gap:4px;flex-wrap:wrap}

  /* 的中した目は緑。発光はさせない (酒場の的中と同じ色) */
  .yos-form .fhit{color:var(--green);font-weight:800}
  .yos-form .fmark{margin-left:9px;font-size:10px;font-weight:700;
    letter-spacing:.1em;color:var(--green);
    border:1px solid color-mix(in srgb,var(--green) 55%,transparent);
    border-radius:5px;padding:1px 7px;
    background:color-mix(in srgb,var(--green) 14%,transparent)}
  .yos-form{font-family:'Noto Serif JP',serif;font-variant-numeric:tabular-nums;font-size:15px;color:#ffe6a8;
    letter-spacing:.1em;padding:4px 2px;line-height:1.6}
  .yos-combo.sm{font-size:11px;opacity:.85}

  .yos-card{background:rgba(27,24,17,.72);border:1px solid #3a3226;
    border-radius:9px;padding:10px 12px;margin-bottom:10px}
  .yos-card h4{margin:0 0 7px;font-size:14px;color:#ffe6a8;font-weight:normal;
    letter-spacing:.06em}
  .yos-row{margin-bottom:8px}
  .yos-sub{font-size:11px;color:#9a8c70;margin-bottom:4px}
  .yos-prev{margin-left:8px;color:#7d715b}
  .yos-combo{display:inline-block;background:#2f2a1c;border:1px solid #6a5a38;
    border-radius:5px;padding:3px 10px;margin:3px 5px 0 0;font-size:15px;
    letter-spacing:1px;color:#f0e4c6}
  .yos-none{color:#9a8c70;padding:14px 2px;font-size:14px}
  .yos-none b{color:#d9c9a3}
  .yos-note{font-size:11px;color:#7d715b;margin-top:12px;line-height:1.7}
  .yos-warn{color:#c08a4a}
  /* v329: 集計明細のレース素性 */
  .bt-meta{margin:7px 0 9px;padding:7px 9px;background:rgba(0,0,0,.28);
    border-left:2px solid #4a4030;border-radius:0 5px 5px 0}
  .bt-meta-tags{margin-bottom:5px}
  .bt-mt{display:inline-block;background:#2a2418;border:1px solid #5a4c33;
    border-radius:3px;padding:1px 7px;margin:0 5px 3px 0;font-size:11px;
    color:#c9bda0}
  .bt-mt.on{background:#3a2e12;border-color:#a8862f;color:#ffe6a8}
  .bt-meta-row{font-size:11px;line-height:1.85;display:flex;gap:8px}
  .bt-ml{color:#7d715b;min-width:88px;flex:0 0 auto}
  .bt-mv{color:#cfc4aa;letter-spacing:.06em;font-family:'Noto Serif JP',serif;font-variant-numeric:tabular-nums}
  .bt-meta-kim{font-size:12px;color:#d9c9a3;margin-bottom:5px;
    letter-spacing:.04em}
  /* v329: 本日の集計 */
  .vchip.vsum .nm{color:var(--gold-strong)}
  .sum-grid{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:14px;color:#e8dfc9}
  .sum-grid > div{min-width:96px}
  .sum-grid span{display:block;font-size:10px;color:#8b7f68;letter-spacing:.08em}
  .sum-row{border-top:1px solid #332c20;padding:8px 0 9px}
  .sum-row:first-of-type{border-top:none}
  .sum-top{font-size:13px;color:#e0d5bb;margin-bottom:5px}
  .sum-st{color:#9a8c70;font-size:11px;margin-left:4px}
  .sum-hit{color:var(--red);border:1px solid rgba(200,70,60,.5);
    border-radius:3px;padding:0 6px;font-size:11px;margin-left:4px}
  .sum-miss{color:#6f6552;font-size:11px;margin-left:4px}
  .sum-wait{color:#7f8f6a;font-size:11px;margin-left:4px}
  .sum-res{font-size:11px;color:#9a8c70;margin-top:5px}
  .yos-combo.won{border-color:var(--red);color:#ffd9c8;
    box-shadow:0 0 10px rgba(200,70,60,.35)}
  /* v329: Rボタンは左から順に素早く流れ込む(サッと出す)。
     縦のフェードアップだと横バーでは動きが合わないため専用にした。 */
  @keyframes rcellSweep{
    0%{opacity:0;transform:translateX(14px) scale(.96)}
    100%{opacity:1;transform:translateX(0) scale(1)}}
  .race-grid > .rcell.anim{
    animation:rcellSweep .26s cubic-bezier(.22,.61,.36,1) both}
  .race-grid > .rcell.anim:nth-child(1){animation-delay:.00s}
  .race-grid > .rcell.anim:nth-child(2){animation-delay:.022s}
  .race-grid > .rcell.anim:nth-child(3){animation-delay:.044s}
  .race-grid > .rcell.anim:nth-child(4){animation-delay:.066s}
  .race-grid > .rcell.anim:nth-child(5){animation-delay:.088s}
  .race-grid > .rcell.anim:nth-child(6){animation-delay:.110s}
  .race-grid > .rcell.anim:nth-child(7){animation-delay:.132s}
  .race-grid > .rcell.anim:nth-child(8){animation-delay:.154s}
  .race-grid > .rcell.anim:nth-child(9){animation-delay:.176s}
  .race-grid > .rcell.anim:nth-child(10){animation-delay:.198s}
  .race-grid > .rcell.anim:nth-child(11){animation-delay:.220s}
  .race-grid > .rcell.anim:nth-child(12){animation-delay:.242s}
  @media (prefers-reduced-motion: reduce){
    .anim{animation:none}}
  /* 上段ラベル (穴/弱) */
  .rtag{font-size:8px;font-weight:800;line-height:1;padding:1px 3px;
    border-radius:2px;border:1px solid}
  .rtag.ana{color:var(--blue);border-color:color-mix(in srgb,var(--blue) 55%,transparent);
    background:color-mix(in srgb,var(--blue) 14%,var(--card))}
  .rtag.weak{color:var(--red);border-color:color-mix(in srgb,var(--red) 55%,transparent);
    background:color-mix(in srgb,var(--red) 14%,var(--card))}
  .rtag.layoff{color:var(--green);border-color:color-mix(in srgb,var(--green) 55%,transparent);
    background:color-mix(in srgb,var(--green) 14%,var(--card))}
  /* 的中バッジ (v330: 流れるマーキーを廃止して置き換えた) */
  .hitline{display:flex;align-items:center;gap:6px;white-space:nowrap;
    overflow:hidden;height:100%}
  /* 的中は横に流す。幅に収まらなくても全部読める */
  .hitflow{width:100%;height:100%;overflow:hidden;white-space:nowrap;
    display:flex;align-items:center}
  /* v336: 同じ内容を2つ並べて -50% まで動かす。
     左に余白を足すと2つの幅が揃わなくなり、そこで切れて見える。
     余白は「文と文の間の1マス」として seg の末尾に持たせる。 */
  .hittrack{display:inline-block;
    animation:hitFlow 15s linear infinite}
  .hittrack > span{margin-right:7px}
  .hittrack .hitgap{margin-right:0;letter-spacing:0}
  @keyframes hitFlow{0%{transform:translateX(0)}
    100%{transform:translateX(-50%)}}
  .hitbadge{display:inline-block;padding:1px 7px;border-radius:6px;
    font-size:11px;font-weight:800;letter-spacing:.08em;line-height:1.5}
  /* 狙い予想の的中: 赤のネオン (最上位) */
  .hitbadge.target{color:#ffc9c9;border:1px solid #e05050;
    background:rgba(95,20,20,.4);font-weight:900;
    text-shadow:0 0 5px rgba(255,110,110,1),0 0 11px rgba(255,50,50,.7);
    box-shadow:0 0 7px rgba(255,80,80,.6),inset 0 0 6px rgba(255,80,80,.2)}
  /* 段階の的中: 金のネオン */
  .hitbadge.step{color:#ffe9a8;border:1px solid #d8b24a;
    background:rgba(90,70,20,.35);
    text-shadow:0 0 4px rgba(255,215,120,.9),0 0 9px rgba(255,190,60,.55);
    box-shadow:0 0 5px rgba(255,200,80,.45),inset 0 0 5px rgba(255,200,80,.18)}
  /* 酒場の的中: 青のネオン (保留) */
  .hitbadge.tavern{color:#bfe0ff;border:1px solid #4a90d8;
    background:rgba(20,45,90,.35);
    text-shadow:0 0 4px rgba(110,180,255,.95),0 0 9px rgba(60,140,255,.6);
    box-shadow:0 0 5px rgba(80,160,255,.5),inset 0 0 5px rgba(80,160,255,.18)}
  .hitmk{color:var(--green);font-size:11px;font-weight:700}
  .hittri{color:var(--txt);font-size:12px;letter-spacing:.06em}
  .hityen{color:var(--gold-strong);font-size:12px;font-weight:700}

  /* 下段の結果マーキー (rbot内いっぱい・横スクロール) */
  .rmarquee{width:100%;height:100%;overflow:hidden;white-space:nowrap;
    display:flex;align-items:center;
    -webkit-mask-image:linear-gradient(90deg,transparent,#000 16%,#000 84%,transparent);
    mask-image:linear-gradient(90deg,transparent,#000 16%,#000 84%,transparent)}
  .rmarquee .track{display:inline-block;padding-left:100%;
    animation:rmar 8s linear infinite;font-size:10px;font-weight:700;
    font-variant-numeric:tabular-nums}
  .rmarquee .seg{margin-right:16px}
  .rmarquee .hit{color:var(--green)}
  .rmarquee .tri{color:var(--txt-dim)}
  .rmarquee .yen{color:var(--txt-dim)}
  @keyframes rmar{0%{transform:translateX(0)}100%{transform:translateX(-100%)}}
  .rt .finmk{color:var(--txt-dim);font-weight:700}
  /* 結果カード (薄く・左側のみ緑枠) */
  .resultcard{background:linear-gradient(90deg, rgba(0,40,15,.30), rgba(20,24,15,.10));
    border:none;border-left:2px solid color-mix(in srgb,var(--green) 80%,transparent);
    border-radius:0;margin-bottom:12px;overflow:hidden;
    box-shadow:inset 0 0 18px rgba(0,0,0,.40)}
  body.light .resultcard, html.light .resultcard{
    background:rgba(255,253,246,.62);
    border-left:2px solid var(--green)}
  .resultcard .rc-h{padding:11px 15px;border-bottom:1px solid color-mix(in srgb,var(--green) 28%,transparent);
    display:flex;align-items:center;gap:8px;
    background:linear-gradient(90deg, rgba(0,80,30,.16), transparent)}
  body.light .resultcard .rc-h, html.light .resultcard .rc-h{
    background:linear-gradient(90deg, rgba(60,140,60,.12), transparent)}
  .resultcard .rc-h .ttl{font-weight:700;font-size:14px;color:var(--txt)}
  .resultcard .rc-h .ttl::before{content:"結果";font-size:10px;background:var(--green);
    color:var(--card);padding:2px 6px;border-radius:2px;margin-right:8px;font-weight:800}
  .resultcard .rc-b{padding:12px 15px}
  .res-tri{font-size:22px;font-weight:800;letter-spacing:.08em;
    font-variant-numeric:tabular-nums;display:inline-flex;align-items:center;gap:5px}
  .res-tri .ba{color:var(--green)}
  /* 結果3連単の車番色マーク */
  .res-tri .tri-bk{width:30px;height:30px;border-radius:2px;font-size:17px;
    font-weight:800;display:inline-flex;align-items:center;justify-content:center;
    border:1px solid var(--line);box-shadow:inset 0 0 6px rgba(0,0,0,.6);filter:brightness(.94) saturate(.74)}
  .res-tri .tri-sep{color:var(--txt-dim);font-weight:700;margin:0 1px}
  .res-marks{font-size:18px;font-weight:800;letter-spacing:.18em;
    margin-top:2px}
  .res-marks .mkgold{color:var(--txt)}
  .res-marks .mktxt{color:var(--txt)}
  .res-marks .mksep{color:var(--txt-dim)}
  .res-refund{font-size:14px;color:var(--txt);font-weight:700;margin-top:6px}
  .res-order{margin-top:10px;font-size:12px;color:var(--txt-dim);
    display:flex;flex-wrap:wrap;gap:6px}
  .res-od{background:color-mix(in srgb,var(--green) 8%,var(--card));border:1px solid color-mix(in srgb,var(--green) 35%,var(--line));border-radius:2px;padding:3px 8px}
  .res-od b{color:var(--green)}
  /* 買い目カード的中 = 左側のみ緑枠・他カードと同じ薄さ */
  .pcard.hit{border:none;border-left:3px solid var(--green);
    background:linear-gradient(90deg, rgba(0,40,15,.30), rgba(20,24,15,.10));
    box-shadow:inset 0 0 16px rgba(0,0,0,.40)}
  body.light .pcard.hit, html.light .pcard.hit{
    border:none;border-left:3px solid var(--green);
    background:rgba(255,253,246,.62)}
  .form.hitform{color:var(--green)}
  .form.hitform .b1{color:var(--green)}
  .form .hitmark{color:var(--green);font-weight:800;margin-left:6px}

  /* 穴/勝負弱ラベル */
  .plabel{display:inline-block;font-size:10px;font-weight:700;
    padding:1px 7px;border-radius:2px;margin-left:6px;vertical-align:1px;
    letter-spacing:.02em}
  .plabel.ana{background:rgba(90,160,255,.16);color:var(--blue);
    border:1px solid rgba(90,160,255,.5)}
  .plabel.weak{background:rgba(255,90,90,.16);color:var(--red);
    border:1px solid rgba(255,90,90,.5)}
  .plabel.layoff{background:color-mix(in srgb,var(--green) 16%,transparent);
    color:var(--green);border:1px solid color-mix(in srgb,var(--green) 50%,transparent)}
  /* 出走表内のラベルは小さく */
  .rost-item .plabel,.grp-title .plabel{font-size:8.5px;padding:0 5px;margin-left:5px;border-radius:2px}
  /* ヘッダー判定タグ並びのラベル */
  .plabel.hdlabel{font-size:10px;padding:3px 9px;margin-left:0;vertical-align:0;
    align-self:center;border-radius:2px}
  /* 全車ラベル一覧 */
  .roster{margin-top:12px;border-top:1px solid var(--line);padding-top:10px}
  .roster .rl{font-size:10px;color:var(--txt-dim);margin-bottom:6px}
  .rost-item{display:flex;align-items:center;gap:8px;padding:4px 0;font-size:13px}
  .rost-item .rb{width:24px;height:24px;border-radius:2px;background:var(--bg);
    border:1px solid var(--line);display:flex;align-items:center;justify-content:center;
    font-weight:700;font-size:12px;color:var(--txt);flex:0 0 auto;
    box-shadow:inset 0 0 6px rgba(0,0,0,.7);filter:brightness(.94) saturate(.72)}
  /* 競輪の車番色 (鮮やかなベタ塗り)。出走表・買い目カード共通。
     .rb / .maru と併用するため詳細度を上げて確実に背景を上書き */
  .rb.bcol1,.maru.bcol1,.tri-bk.bcol1,.gbike.bcol1,.ora-bk.bcol1,.strend-bk.bcol1{background:#f5f5f5;color:#1a1a1a;border-color:#cfcfcf}
  .rb.bcol2,.maru.bcol2,.tri-bk.bcol2,.gbike.bcol2,.ora-bk.bcol2,.strend-bk.bcol2{background:#1c1c1c;color:#ffffff;border-color:#000000}
  .rb.bcol3,.maru.bcol3,.tri-bk.bcol3,.gbike.bcol3,.ora-bk.bcol3,.strend-bk.bcol3{background:#e23b3b;color:#ffffff;border-color:#cc2222}
  .rb.bcol4,.maru.bcol4,.tri-bk.bcol4,.gbike.bcol4,.ora-bk.bcol4,.strend-bk.bcol4{background:#2f7be2;color:#ffffff;border-color:#1f5fc0}
  .rb.bcol5,.maru.bcol5,.tri-bk.bcol5,.gbike.bcol5,.ora-bk.bcol5,.strend-bk.bcol5{background:#f1c40f;color:#1a1a1a;border-color:#caa400}
  .rb.bcol6,.maru.bcol6,.tri-bk.bcol6,.gbike.bcol6,.ora-bk.bcol6,.strend-bk.bcol6{background:#2bb24d;color:#ffffff;border-color:#1f9440}
  .rb.bcol7,.maru.bcol7,.tri-bk.bcol7,.gbike.bcol7,.ora-bk.bcol7,.strend-bk.bcol7{background:#e8772e;color:#ffffff;border-color:#c75f1c}
  .rb.bcol8,.maru.bcol8,.tri-bk.bcol8,.gbike.bcol8,.ora-bk.bcol8,.strend-bk.bcol8{background:#e261b0;color:#ffffff;border-color:#c54897}
  .rb.bcol9,.maru.bcol9,.tri-bk.bcol9,.gbike.bcol9,.ora-bk.bcol9,.strend-bk.bcol9{background:#8a5cd1;color:#ffffff;border-color:#6f45b3}
  .ora-chip.bcol1{background:#f5f5f5;color:#1a1a1a}
  .ora-chip.bcol2{background:#1c1c1c;color:#ffffff}
  .ora-chip.bcol3{background:#e23b3b;color:#ffffff}
  .ora-chip.bcol4{background:#2f7be2;color:#ffffff}
  .ora-chip.bcol5{background:#f1c40f;color:#1a1a1a}
  .ora-chip.bcol6{background:#2bb24d;color:#ffffff}
  .ora-chip.bcol7{background:#e8772e;color:#ffffff}
  .ora-chip.bcol8{background:#e261b0;color:#ffffff}
  .ora-chip.bcol9{background:#8a5cd1;color:#ffffff}
  .rost-item .rn{font-weight:600}
  .rost-item .rs{font-size:11px;color:var(--txt-dim)}
  .rost-item .det{margin-left:auto;font-size:10px;color:var(--txt-dim)}
  /* 出走表テーブル (表組み・車選手を左固定・横スクロール) */
  .card-roster{background:#14180f !important;border:1px solid var(--line)}
  .kimari-mode-bar{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin:0 0 6px 0}
  .kimari-mode-bar .kmm-lab{font-size:11px;color:var(--txt-dim)}
  .kimari-mode-bar .kmm-seg{display:inline-flex;gap:0;border:1px solid rgba(140,140,130,.35);
    border-radius:7px;overflow:hidden}
  .kimari-mode-bar .kmm-btn{font-size:11px;font-weight:700;padding:4px 12px;border:none;cursor:pointer;
    background:transparent;color:var(--txt-dim);line-height:1.4;-webkit-tap-highlight-color:transparent}
  .kimari-mode-bar .kmm-btn+.kmm-btn{border-left:1px solid rgba(140,140,130,.35)}
  .kimari-mode-bar .kmm-btn.on{background:var(--gold-strong);color:#1a1a1a}
  body.light .card-roster, html.light .card-roster{background:#f7f4ec !important}
  .roster-wrap{margin-top:10px;overflow-x:auto;-webkit-overflow-scrolling:touch;
    background:#14180f;border-radius:6px}
  .rost-tbl{border-collapse:separate;border-spacing:0;font-size:13px;white-space:nowrap;background:#14180f}
  .rost-tbl th{font-size:10px;color:var(--txt-dim);font-weight:600;
    text-align:center;padding:4px 6px;border-bottom:1px solid var(--line);background:#14180f}
  .rost-tbl td{padding:5px 6px;border-bottom:1px solid rgba(120,120,110,.28);
    vertical-align:middle;text-align:center;background:#14180f}
  /* 列幅 (車番+選手を1セルに統合) */
  .rost-tbl .c-id{box-sizing:border-box;text-align:left;padding-left:6px;padding-right:8px;
    width:138px;min-width:138px;max-width:138px;overflow:hidden;text-overflow:ellipsis}
  .rost-tbl .c-id .rb{margin-right:7px}
  .rost-tbl .c-rl{width:62px;padding-left:10px}
  .rost-tbl .c-st{width:26px}
  .rost-tbl .c-mk{width:24px}
  .rost-tbl .c-lb{width:42px}
  .rost-tbl .c-km{width:48px}
  .rost-tbl .c-sb{width:40px;padding-left:5px;padding-right:5px}
  /* 統合セルの中身を横並び */
  .rost-tbl td.c-id, .rost-tbl th.c-id{white-space:nowrap}
  .rost-tbl .c-id .rb{display:inline-flex;vertical-align:middle}
  .rost-tbl .c-id .rn{display:inline;vertical-align:middle;font-weight:600}
  /* 車番マーク (色付き・行幅に合う22px) */
  .rost-tbl .rb{width:22px;height:22px;border-radius:2px;
    display:inline-flex;align-items:center;justify-content:center;
    font-weight:700;font-size:12px;flex:0 0 auto;
    box-shadow:inset 0 0 6px rgba(0,0,0,.7);filter:brightness(.94) saturate(.72)}
  .rost-tbl .rn{font-weight:600}
  .rost-tbl .rrole{font-size:10px;color:var(--txt-dim)}
  .rost-tbl .rs{font-size:11px;color:var(--txt-dim)}
  .rost-tbl .kmark{font-size:14px;font-weight:800;line-height:1}
  .rost-tbl .c-lb .plabel{margin-left:0;font-size:8.5px;padding:0 4px;display:block;margin-bottom:1px}
  /* 決まり手セル: 率(大)+分数(小)の2段。最上位は赤字 */
  .rost-tbl .km-c{line-height:1.05}
  .rost-tbl .km-r{display:block;font-size:12px;font-weight:700;color:var(--txt)}
  .rost-tbl .km-r.kr1{color:var(--gold-strong);
    text-shadow:0 0 6px rgba(255,200,110,.45);
    animation:cellGlow 1.6s ease-in-out infinite}
  .rost-tbl .km-r.kr2{color:var(--green)}
  .rost-tbl .km-r.kr3{color:var(--red)}
  @keyframes cellGlow{
    0%{text-shadow:0 0 5px rgba(255,200,110,.4)}
    50%{text-shadow:0 0 11px rgba(255,224,150,.9),0 0 16px rgba(255,200,110,.5)}
    100%{text-shadow:0 0 5px rgba(255,200,110,.4)}}
  .rost-tbl .km-r.km-top{color:var(--red)}
  .rost-tbl .km-f{display:block;font-size:9px;color:var(--txt-dim)}
  .rost-tbl .km-na{color:var(--txt-dim);font-size:12px}
  /* 左固定列 (車・選手を統合した1セルのみ)。継ぎ目なし */
  .rost-tbl td.fix, .rost-tbl th.fix{position:sticky;z-index:2;background:#14180f}
  .rost-tbl thead th.fix{z-index:3;background:#14180f}
  .rost-tbl .fix1{left:0}
  body.light .roster-wrap, html.light .roster-wrap{background:#f7f4ec}
  body.light .rost-tbl, html.light .rost-tbl{background:#f7f4ec}
  body.light .rost-tbl th, html.light .rost-tbl th{background:#f7f4ec}
  body.light .rost-tbl td, html.light .rost-tbl td{background:#f7f4ec}
  body.light .rost-tbl td.fix, html.light .rost-tbl td.fix{background:#f7f4ec}
  .rchart-wrap{margin-top:14px;padding:12px 10px 8px;border-radius:10px;
    background:rgba(20,24,15,.55);border:1px solid rgba(120,120,110,.22)}
  .rchart-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:2px}
  .rchart-title{font-size:11px;color:var(--txt-dim);font-weight:700;
    letter-spacing:.04em}
  .rchart-btns{display:inline-flex;gap:0;border:1px solid rgba(140,140,130,.35);
    border-radius:7px;overflow:hidden}
  .rchart-btn{font-size:11px;font-weight:700;padding:4px 12px;border:none;cursor:pointer;
    background:transparent;color:var(--txt-dim);line-height:1.4;-webkit-tap-highlight-color:transparent}
  .rchart-btn+.rchart-btn{border-left:1px solid rgba(140,140,130,.35)}
  .rchart-btn.active{background:var(--gold-strong);color:#1a1a1a}
  .rchart-svg{margin-top:4px}
  .rchart-legend{display:flex;gap:16px;margin:2px 0 6px;font-size:11px;color:var(--txt-dim)}
  .rchart-legend .rcl-item{display:inline-flex;align-items:center;gap:6px}
  .rchart-legend .rcl-swatch{width:14px;height:10px;border-radius:2px;display:inline-block}
  .rchart-legend .sw-score{background:rgba(91,155,213,.55);border:1.5px solid #5b9bd5}
  .rchart-legend .sw-match{background:#fff;border:2px solid #ff8a3d;border-radius:50%;width:11px;height:11px}
  /* score推移グラフ */
  .strend-wrap{margin-top:12px;padding:12px 10px 8px;border-radius:10px;
    background:rgba(20,24,15,.55);border:1px solid rgba(120,120,110,.22)}
  .strend-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
  .strend-title{font-size:11px;color:var(--txt-dim);font-weight:700;letter-spacing:.04em}
  .strend-legend{display:flex;gap:14px;font-size:10px;color:var(--txt-dim);align-items:center}
  .strend-legend .stl-clear{cursor:pointer;font-size:11px;font-weight:700;color:var(--txt-dim);
    border:1px solid rgba(140,140,130,.4);border-radius:6px;padding:2px 9px;line-height:1.3;
    -webkit-tap-highlight-color:transparent;user-select:none}
  .strend-legend .stl-clear:active{background:rgba(140,140,130,.18);transform:scale(.94)}
  .strend-legend .stl-item{display:inline-flex;align-items:center;gap:5px}
  .strend-legend .stl-area{width:13px;height:9px;border-radius:2px;display:inline-block;
    background:#8f8f8f;opacity:.5}
  .strend-legend .stl-line{width:14px;height:0;border-top:2px solid #8f8f8f;display:inline-block}
  .strend-btns{display:flex;align-items:stretch;gap:0;margin:0 2px 8px;flex-wrap:nowrap}
  .strend-bk{flex:1 1 0;min-width:0;margin:0 3px;height:30px;border-radius:7px;
    border:1.5px solid transparent;font-size:14px;font-weight:800;
    display:flex;align-items:center;justify-content:center;cursor:pointer;opacity:.32;
    transition:opacity .12s,box-shadow .12s,transform .08s;user-select:none;
    -webkit-tap-highlight-color:transparent}
  .strend-bk:active{transform:scale(.94)}
  .strend-bk.on{opacity:1;box-shadow:0 0 0 1px rgba(255,255,255,.25),0 2px 6px rgba(0,0,0,.5)}
  .strend-sep{width:1px;align-self:stretch;background:rgba(255,255,255,.22);margin:3px 4px;flex:0 0 auto}
  .strend-svg{margin-top:2px}
  body.light .strend-wrap, html.light .strend-wrap{background:#eef2f7;border-color:rgba(80,90,110,.2)}
  body.light .rchart-wrap, html.light .rchart-wrap{background:#eef2f7;border-color:rgba(80,90,110,.2)}
  body.light .rost-tbl th.fix, html.light .rost-tbl th.fix{background:#f7f4ec}
  .kmark{font-size:16px;font-weight:800;width:22px;text-align:center;flex:0 0 auto;
    line-height:1}
  .kmark.r1{color:var(--txt)}
  .kmark.r2,.kmark.r3,.kmark.r4,
  .kmark.r5,.kmark.r6,.kmark.r7{color:var(--txt)}

  /* 詳細 */
  #detail{padding:0 12px;overflow-x:hidden}
  .card{
    background:linear-gradient(90deg, rgba(0,0,0,.38), rgba(20,24,15,.14));
    border:none;border-left:2px solid rgba(160,178,180,.6);
    border-radius:0;
    margin-bottom:12px;overflow:hidden;
    box-shadow:inset 0 0 24px rgba(0,0,0,.45)}
  body.light .card, html.light .card{
    background:rgba(255,253,246,.64);
    border-left:2px solid var(--gold-dim);
    box-shadow:inset 0 0 24px color-mix(in srgb,#000 4%,transparent)}
  /* 明モード: 各カード類を半透明にして背景を透かす */
  body.light .rcell, html.light .rcell{
    background:rgba(255,253,246,.62);
    border-left:2px solid rgba(150,130,90,.5);
    box-shadow:inset 0 0 10px rgba(120,90,40,.06)}
  body.light .rcell .rno, html.light .rcell .rno{color:#2b2a22;text-shadow:none}
  body.light .rcell.displayable, html.light .rcell.displayable{
    border-left:2px solid var(--gold)}
  body.light .rcell.hit .rno, html.light .rcell.hit .rno{
    color:var(--red);
    text-shadow:0 0 8px color-mix(in srgb,var(--red) 40%,transparent)}
  body.light .rcell.hit .rnum, html.light .rcell.hit .rnum{color:var(--red)}
  body.light .rcell.sel, html.light .rcell.sel{
    background:linear-gradient(90deg, rgba(230,170,90,.30), rgba(255,253,246,.4));
    border-left:2px solid var(--gold)}
  body.light .vchip, html.light .vchip{
    background:rgba(255,253,246,.62);
    border-left:2px solid rgba(150,130,90,.5)}
  body.light .vchip.active, html.light .vchip.active{
    background:linear-gradient(90deg, rgba(230,170,90,.32), rgba(255,253,246,.4));
    border-left:2px solid var(--gold)}
  body.light .pcard, html.light .pcard{
    background:rgba(255,253,246,.62);
    border-left:2px solid rgba(150,130,90,.5)}
  body.light .pcard.top, html.light .pcard.top{
    background:rgba(255,251,240,.72);
    border-left:3px solid var(--gold)}
  body.light .pc-h, html.light .pc-h{background:transparent}
  body.light .tag, html.light .tag{background:rgba(255,253,246,.64)}
  body.light .tabbar, html.light .tabbar{background:rgba(255,253,246,.62)}
  body.light .tabbar .tab.on, html.light .tabbar .tab.on{
    background:rgba(230,170,90,.18)}
  body.light .subbar .sub, html.light .subbar .sub{background:rgba(255,253,246,.64)}
  body.light .scope-sw .sw, html.light .scope-sw .sw{background:rgba(255,253,246,.64)}
  body.light .scope-sw .sw.on, html.light .scope-sw .sw.on{
    background:var(--gold);color:#fff}
  body.light .kbtn, html.light .kbtn{background:rgba(255,253,246,.58)}
  body.light .skip-note, html.light .skip-note{background:rgba(255,253,246,.62)}
  body.light .card-h, html.light .card-h{
    background:linear-gradient(90deg, rgba(220,160,80,.12), transparent)}
  .card-h{padding:13px 15px;border-bottom:1px solid rgba(150,168,170,.25);
    display:flex;align-items:center;gap:8px;
    background:linear-gradient(90deg, rgba(120,90,40,.10), transparent)}
  .card-h .ttl{font-weight:700;font-size:14px;letter-spacing:.14em;
    font-family:'Noto Serif JP',serif}
  .card-h .ttl::before{content:"";display:inline-block;width:3px;height:15px;
    background:var(--gold);margin-right:9px;vertical-align:-2px;border-radius:0}
  .card-b{padding:13px 15px}

  /* レースヘッダー */
  .rhead .venue{font-size:22px;font-weight:700;letter-spacing:.12em;
    font-family:'Noto Serif JP',serif}
  .rhead .rno{color:var(--gold);font-weight:700;margin-left:8px}
  .rhead .post{color:var(--txt-dim);font-size:13px;margin-left:auto;letter-spacing:.08em}
  .rhead-top{display:flex;align-items:center}
  /* WINTICKETレースへ飛ぶ細枠ボタン */
  .wt-btn{display:inline-flex;align-items:center;justify-content:center;
    width:28px;height:28px;margin-left:10px;flex:0 0 auto;
    border:1px solid var(--line);border-radius:2px;background:transparent;
    color:var(--wt);font-weight:900;font-size:15px;line-height:1;
    text-decoration:none}
  .wt-btn:active{background:color-mix(in srgb,var(--wt) 12%,transparent)}
  .meta-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
  .meta-row.side-row{margin-top:7px}
  .tag{background:color-mix(in srgb,var(--card2) 70%,transparent);
    border:1px solid var(--line);border-radius:2px;
    padding:4px 10px;font-size:12px;color:var(--txt-dim);letter-spacing:.06em}
  .tag.wind{color:var(--blue);border-color:color-mix(in srgb,var(--blue) 40%,var(--line))}
  .tag.judge{color:#d99a00;border-color:color-mix(in srgb,#d99a00 40%,var(--line))}
  body.light .tag.judge, html.light .tag.judge{color:#b9860f}
  .line-block{margin-top:12px;font-variant-numeric:tabular-nums;
    display:flex;align-items:center;gap:8px}
  .line-block .line-main{flex:1;min-width:0}
  .line-ora-btn{flex:none;align-self:stretch;padding:4px 12px;font-size:14px;font-weight:800;
    border:1px solid var(--gold);border-radius:0;cursor:pointer;
    background:color-mix(in srgb,var(--gold) 16%,var(--card));color:var(--gold);
    font-family:'Noto Serif JP',serif;letter-spacing:.06em;
    display:flex;flex-direction:column;align-items:center;justify-content:center;line-height:1.2}
  .line-ora-btn:active{background:color-mix(in srgb,var(--gold) 30%,var(--card))}
  .line-ora-btn .loa-sub{font-size:9px;font-weight:600;opacity:.8;letter-spacing:.02em}
  .line-block .lab{color:var(--txt-dim);font-size:11px;display:inline-block;width:74px;
    letter-spacing:.08em}
  .line-block .val{font-size:16px;font-weight:700;letter-spacing:.14em;
    font-family:'Noto Serif JP',serif}
  .line-block .val.rank{color:var(--txt);letter-spacing:.14em}
  .line-top3{color:var(--green)}

  /* 予想カード */
  .pcard{border:none;border-left:2px solid rgba(150,168,170,.5);
    border-radius:0;margin-bottom:10px;
    overflow:hidden;background:linear-gradient(90deg, rgba(0,0,0,.34), rgba(20,24,15,.10));
    box-shadow:inset 0 0 16px rgba(0,0,0,.40)}
  .pcard.top{border-left:3px solid var(--gold);
    background:linear-gradient(90deg, rgba(255,180,90,.14), rgba(20,24,15,.08));
    box-shadow:inset 0 0 20px rgba(255,190,100,.12)}
  .pc-h{padding:11px 13px;display:flex;align-items:center;gap:10px;
    background:transparent}
  .maru{width:30px;height:30px;border-radius:2px;display:flex;
    align-items:center;justify-content:center;font-weight:700;font-size:15px;
    font-family:'Noto Serif JP',serif;
    background:var(--bg);color:var(--txt);border:1px solid var(--line);flex:0 0 auto;
    box-shadow:inset 0 0 8px rgba(0,0,0,.7);filter:brightness(.94) saturate(.72)}
  .pc-name{font-weight:700;font-size:15px;letter-spacing:.06em}
  .pc-style{font-size:11px;color:var(--txt-dim);margin-left:2px}
  .kh{margin-left:auto;text-align:right}
  .kh .lab{font-size:10px;color:var(--txt-dim)}
  .kh .v{font-weight:700;font-size:18px;color:var(--gold);line-height:1;
    font-family:'Noto Serif JP',serif}
  .pc-stats{display:flex;gap:0;border-top:1px solid var(--line)}
  .stat{flex:1;padding:9px 4px;text-align:center;border-right:1px solid var(--line)}
  .stat:last-child{border-right:none}
  .stat .l{font-size:10px;color:var(--txt-dim)}
  .stat .n{font-weight:700;font-size:14px;margin-top:2px}
  .stat .sub{font-size:10px;color:var(--txt-dim)}
  .forms{padding:10px 13px;border-top:1px solid var(--line)}
  .forms .fl{font-size:10px;color:var(--txt-dim);margin-bottom:5px}
  .form{font-variant-numeric:tabular-nums;font-size:16px;font-weight:700;
    letter-spacing:.06em;padding:3px 0;color:var(--txt)}
  .form .b1{color:var(--txt)}

  /* 帯メニュー(タブバー) 出走表/分析/オッズ/買い目 */
  .tabbar{display:flex;background:color-mix(in srgb,var(--card) 50%,transparent);
    border-radius:2px;overflow:hidden;
    margin:14px 0 12px;border:1px solid var(--line)}
  .tabbar .tab{flex:1;text-align:center;padding:12px 4px;font-size:13px;
    font-weight:700;color:var(--txt-dim);cursor:pointer;border:none;background:transparent;
    border-bottom:2px solid transparent;transition:.15s;white-space:nowrap;
    letter-spacing:.1em;font-family:'Noto Serif JP',serif}
  .tabbar .tab+.tab{border-left:1px solid var(--line)}
  .tabbar .tab.on{color:var(--gold-strong);border-bottom-color:var(--gold-strong);
    background:color-mix(in srgb,var(--gold) 14%,transparent)}
  /* サブメニュー (分析データ内など) */
  .subbar{display:flex;gap:8px;margin:12px 0 4px;flex-wrap:wrap}
  .subbar .sub{padding:7px 14px;font-size:12px;font-weight:700;border-radius:2px;
    background:color-mix(in srgb,var(--card2) 60%,transparent);
    border:1px solid var(--line);border-left:2px solid var(--gold-dim);color:var(--txt-dim);
    cursor:pointer;letter-spacing:.06em}
  .subbar .sub.on{background:color-mix(in srgb,var(--gold) 14%,var(--card));
    border-color:var(--gold);border-left:2px solid var(--gold);color:var(--gold)}
  .tabpane{margin-top:4px}
  /* 帯メニュー固定スイッチ */
  .tablock{margin:-6px 0 8px;padding:0 2px}
  .tablock-sw{display:inline-flex;align-items:center;gap:6px;font-size:11px;
    color:var(--txt-dim);cursor:pointer;user-select:none}
  .tablock-sw input{width:14px;height:14px;accent-color:var(--gold-strong);cursor:pointer}
  /* score順位グラフのスコープ切替スイッチ */
  .scope-sw{display:flex;gap:0;margin:4px 0 12px;border:1px solid var(--line);
    border-radius:2px;overflow:hidden;width:fit-content}
  .scope-sw .sw{padding:7px 16px;font-size:12px;font-weight:700;cursor:pointer;
    color:var(--txt-dim);background:color-mix(in srgb,var(--card) 50%,transparent);
    letter-spacing:.06em}
  .scope-sw .sw.on{background:var(--gold);color:#1a1400}
  .grp-title{font-size:13px;font-weight:700;margin:14px 0 6px;color:var(--txt);
    display:flex;align-items:center;gap:8px;letter-spacing:.08em}
  .grp-title .gbike{width:22px;height:22px;border-radius:2px;font-size:11px;
    display:inline-flex;align-items:center;justify-content:center;font-weight:700;
    border:1px solid var(--line);flex:0 0 auto}
  .grp-title .gn{font-size:11px;color:var(--txt-dim);font-weight:600;margin-left:auto}
  .nodata{color:var(--txt-dim);font-size:12px;padding:10px 0}
  /* 決まり手グラフ */
  .bar-row{display:flex;align-items:center;gap:8px;margin:4px 0;font-size:13px}
  .bar-row .lb{width:54px;flex:0 0 auto;color:var(--txt-dim);font-size:12px}
  .bar-row .lb b{color:var(--txt)}
  /* 選択中の1着決まり手行: 左にテラコッタ縦ライン + 薄ハイライト背景 (位置不動) */
  .bar-row.sel-row{background:color-mix(in srgb,var(--gold) 10%,transparent);
    border-left:3px solid var(--gold);border-radius:0;
    margin-left:-9px;padding-left:6px}
  .lb-dot{display:none}
  /* メインバー(上,太)+基準ラインバー(下,細)の2段トラック */
  .bar-track{flex:1;display:flex;flex-direction:column;gap:2px;min-width:0}
  .bar-main{height:11px;background:rgba(0,0,0,.32);
    border-radius:0;overflow:hidden;box-shadow:inset 0 0 6px rgba(0,0,0,.5)}
  .bar-fill{height:100%;width:0;border-radius:0;
    background:linear-gradient(90deg, color-mix(in srgb,var(--gold) 75%,#000), var(--gold));
    transition:width .6s cubic-bezier(.3,0,.2,1)}
  /* 基準値ライン (細い・テラコッタの薄色) */
  .bar-base{height:3px;background:rgba(0,0,0,.28);
    border-radius:0;overflow:hidden}
  .bar-base-fill{height:100%;width:0;border-radius:0;
    background:color-mix(in srgb,var(--gold) 50%,transparent);
    transition:width .6s cubic-bezier(.3,0,.2,1)}
  /* 値表示: 今回%(上)と基準%(下,括弧) を縦並び・小さめ */
  .bar-row .pvwrap{width:52px;flex:0 0 auto;text-align:right;line-height:1.15;
    display:flex;flex-direction:column;align-items:flex-end}
  .bar-row .pv{font-size:12px;font-weight:700;font-variant-numeric:tabular-nums}
  .bar-row .bvsub{font-size:9.5px;color:var(--gold-dim);font-variant-numeric:tabular-nums}
  /* 明モードのグラフは託宣の文字色(琥珀)に揃える。黒くすみグラデは使わない */
  body.light .bar-main, html.light .bar-main{
    background:rgba(198,180,150,.28);box-shadow:none}
  body.light .bar-fill, html.light .bar-fill{
    background:#c67a3c}
  body.light .bar-base, html.light .bar-base{background:rgba(150,120,80,.3)}
  body.light .bar-base-fill, html.light .bar-base-fill{
    background:#8a5a2a}
  /* 明モードの車番マークはデフォルトカラー(くすみ・内側影なし) */
  body.light .rb, html.light .rb,
  body.light .maru, html.light .maru{
    filter:none;box-shadow:none}
  /* 明モードはカード内側の黒い影(くすみ)を消す */
  body.light .card, html.light .card,
  body.light .pcard, html.light .pcard,
  body.light .resultcard, html.light .resultcard,
  body.light .rcell, html.light .rcell,
  body.light .vchip, html.light .vchip,
  body.light .skip-note, html.light .skip-note{
    box-shadow:none}
  .link-grp{margin-top:12px}
  .link-grp .gh{font-size:12px;color:var(--gold);margin-bottom:5px;
    border-bottom:1px solid var(--line);padding-bottom:4px}
  /* 3着展開トリガー: %表示部分を枠ボタンに (全項目サイズ統一) */
  .pvwrap.pvbtn{cursor:pointer;border:1px solid var(--line);
    border-radius:2px;padding:2px 0;box-sizing:border-box;width:64px;align-items:center;
    transition:background .12s,border-color .12s}
  .pvwrap.pvbtn:active{background:color-mix(in srgb,var(--gold) 12%,transparent)}
  .pvwrap.pvbtn.pvopen{background:color-mix(in srgb,var(--gold) 12%,transparent);
    border-color:var(--gold)}
  /* 3着グラフ展開部 */
  .third-grp{margin:4px 0 8px 16px;padding:8px 10px;border-left:2px solid var(--gold);
    background:color-mix(in srgb,var(--gold) 6%,transparent);border-radius:0 4px 4px 0}
  .third-grp .th-h{font-size:11px;color:var(--txt-dim);margin-bottom:5px;font-weight:600}
  /* 1着決まり手比率の見出し + 逃/捲/差 切替ボタン */
  .kimari-head{display:flex;align-items:center;justify-content:space-between;
    margin-bottom:8px;gap:8px}
  .kimari-head .kh-ttl{font-size:12px;color:var(--txt-dim)}
  .kimari-btns{display:flex;gap:5px;flex:0 0 auto}
  .kbtn{min-width:34px;padding:5px 11px;font-size:13px;font-weight:700;
    border:1px solid var(--line);border-radius:2px;background:transparent;
    color:var(--txt-dim);cursor:pointer;line-height:1;
    font-family:'Noto Serif JP',serif;letter-spacing:.06em}
  .kbtn.on{border-color:var(--gold);color:var(--gold);
    background:color-mix(in srgb,var(--gold) 16%,var(--card))}

  .skip-note{color:var(--txt-dim);font-size:13px;padding:16px;text-align:center;
    background:color-mix(in srgb,var(--card) 50%,transparent);
    border:1px dashed var(--line);border-radius:2px}

  /* 御告タブ(買い目生成) */
  .ora-sec{margin-bottom:14px}
  /* 確定ロジック検証 (v283: 8パターン) */
  .fl-chips{display:flex;flex-wrap:wrap;gap:4px;margin:8px 0 4px}
  .fl-chip{font-size:11px;padding:4px 8px;border-radius:12px;cursor:pointer;
    border:1px solid var(--line);color:var(--txt-dim);
    background:color-mix(in srgb,var(--card) 60%,transparent);
    -webkit-tap-highlight-color:transparent;white-space:nowrap}
  .fl-chip.on{color:#0d1b12;background:#d4a017;border-color:#d4a017;font-weight:700}
  .fl-chip.alt{border-style:dashed;opacity:.85}
  .fl-scroll{overflow-x:auto}
  .fl-tbl{border-collapse:collapse;font-size:10px}
  .fl-hd{background:color-mix(in srgb,var(--card) 80%,transparent)}
  .fl-td{border:1px solid var(--line);padding:2px;font-size:10px;
    white-space:nowrap;color:var(--txt)}
  .fl-q{white-space:nowrap;font-weight:700}
  .fl-na{color:var(--txt-dim);opacity:.65}
  .fl-na2{color:var(--txt-dim);font-size:12px;margin-top:8px}
  .fl-ok{color:#e8c14a}
  .fl-good{color:#0d1b12;background:#3ddc84;font-weight:700}
  .fl-ttl{font-size:12px;margin:10px 0 2px;color:#8fd}
  .fl-ttl2{font-size:12px;margin:12px 0 4px;color:#d4a017}
  .fl-cap{font-size:11px;color:#d4a017}
  .fl-legend{color:var(--txt-dim);font-size:10px}
  .fl-sum{font-size:12px;margin:6px 0;color:#d4a017}
  .fl-bet{font-family:'Noto Serif JP',serif;font-variant-numeric:tabular-nums}
  .fl-hit{color:#0d1b12;background:#3ddc84;border-radius:3px;
    padding:0 3px;font-weight:700}
  .fl-hitn{color:#3ddc84;font-size:9px}
  .fl-hitrow{background:rgba(61,220,132,.12)}
  .fl-refresh{background:#2a3a2a;border-color:#3ddc84;color:#8fe}
  .fl-today{margin:8px 0 14px}
  .fl-sub{font-size:11px;color:var(--txt-dim);margin:8px 0 4px}
  .fl-card{border:1px solid var(--line);border-radius:6px;
    padding:6px 8px;margin-bottom:6px;
    background:color-mix(in srgb,var(--card) 55%,transparent)}
  .fl-cardh{font-size:12px;color:var(--txt);margin-bottom:4px}
  .fl-cardh b{color:#d4a017}
  .fl-fav{font-size:10px;color:var(--txt-dim)}
  .fl-res{font-size:10px;color:#3ddc84}
  .fl-brow{display:flex;align-items:flex-start;gap:6px;margin:2px 0}
  .fl-blab{font-size:10px;color:var(--txt-dim);min-width:104px;
    white-space:nowrap}
  .fl-buy{display:inline-block;font-family:'Noto Serif JP',serif;font-variant-numeric:tabular-nums;font-size:12px;
    border:1px solid var(--line);border-radius:4px;padding:1px 4px;
    margin:1px 2px 1px 0;white-space:nowrap;color:var(--txt)}
  .fl-drop{display:inline-block;font-family:'Noto Serif JP',serif;font-variant-numeric:tabular-nums;font-size:12px;
    border:1px dashed var(--line);border-radius:4px;padding:1px 4px;
    margin:1px 2px 1px 0;white-space:nowrap;
    color:var(--txt-dim);text-decoration:line-through;opacity:.55}
  .fl-od{font-size:9px;color:#d4a017;margin-left:3px}
  .fl-qrow{margin-top:2px}
  .fl-qlab{font-size:10px;color:var(--txt-dim);align-self:center;
    margin-right:2px}
  .fl-jump{font-size:10px;padding:3px 8px;margin-left:6px;border-radius:5px;
    border:1px solid var(--line);background:transparent;color:#8fd;
    -webkit-tap-highlight-color:transparent}
  .fl-tm{white-space:nowrap;font-size:9px;color:var(--txt-dim)}
  .fl-close{font-size:10px;color:#e8843a}
  .fl-closed{font-size:9px;color:#0d1b12;background:#8a8a8a;
    border-radius:3px;padding:0 4px}
  .fl-rowpat{font-size:10px;color:var(--txt-dim);font-weight:400;margin-left:6px}
  .fl-rowpat select{font-size:10px;padding:1px 2px}

  /* 大聖堂タブ (新エンジン) */
  .cat-modes{display:flex;gap:4px;margin-bottom:14px}
  .cat-mode{flex:1;padding:9px 4px;font-size:13px;font-weight:700;
    border:1px solid var(--line);border-radius:6px;cursor:pointer;
    background:color-mix(in srgb,var(--card) 60%,transparent);color:var(--txt-dim);
    font-family:'Noto Serif JP',serif;letter-spacing:.03em;
    -webkit-tap-highlight-color:transparent;transition:color .12s,border-color .12s,background .12s}
  .cat-mode.on{color:var(--gold-strong);border-color:var(--gold);
    background:color-mix(in srgb,var(--gold) 14%,var(--card))}
  .cat-mode:active{background:color-mix(in srgb,var(--gold) 22%,var(--card))}
  .cat-sw{display:flex;gap:0}
  .cat-swb{padding:7px 16px;font-size:13px;font-weight:700;cursor:pointer;
    border:1px solid var(--line);background:var(--card);color:var(--txt-dim);
    font-family:inherit;-webkit-tap-highlight-color:transparent}
  .cat-swb:first-child{border-radius:6px 0 0 6px}
  .cat-swb:last-child{border-radius:0 6px 6px 0;border-left:0}
  .cat-swb.on{color:var(--gold-strong);border-color:var(--gold);
    background:color-mix(in srgb,var(--gold) 14%,var(--card))}
  .cat-sw-dis{opacity:.4;pointer-events:none}
  .cat-maria-note{font-size:12.5px;color:var(--txt-dim);line-height:1.8;
    padding:8px 10px;border-left:2px solid var(--gold);
    background:color-mix(in srgb,var(--gold) 7%,transparent)}
  .cat-res-head{font-size:12px;color:var(--txt-dim);font-weight:600;
    margin:4px 0 8px;letter-spacing:.02em}
  .cat-res-list{display:flex;flex-direction:column;gap:5px}
  .cat-res-row{display:flex;align-items:center;justify-content:space-between;
    padding:6px 10px;border:1px solid var(--line);border-radius:6px;
    background:color-mix(in srgb,var(--card) 50%,transparent)}
  .cat-res-bikes{display:flex;align-items:center;gap:4px}
  .cat-res-bikes .rb{width:26px;height:26px;border-radius:3px;display:flex;
    align-items:center;justify-content:center;font-size:14px;font-weight:800;
    line-height:1;border:1px solid var(--line);box-sizing:border-box}
  .cat-arrow{color:var(--txt-dim);font-size:11px;margin:0 1px}
  .cat-res-pct{font-size:14px;font-weight:800;color:var(--gold-strong);
    font-variant-numeric:tabular-nums}
  .cat-src{font-size:10px;color:var(--txt-dim);border:1px solid var(--line);
    border-radius:3px;padding:1px 5px;margin-left:4px}
  .cat-result-bar{display:flex;align-items:center;gap:8px;margin:4px 0 10px;
    padding:7px 10px;border-radius:6px;border:1px solid var(--line)}
  .cat-result-bar.cat-hit{border-color:var(--green);
    background:color-mix(in srgb,var(--green) 12%,transparent)}
  .cat-result-bar.cat-miss{border-color:var(--line);
    background:color-mix(in srgb,var(--card) 50%,transparent)}
  .cat-result-lbl{font-size:11px;color:var(--txt-dim);font-weight:700;
    flex:0 0 auto}
  .cat-result-judge{margin-left:auto;font-size:12px;font-weight:800;
    font-variant-numeric:tabular-nums}
  .cat-hit .cat-result-judge{color:var(--green)}
  .cat-miss .cat-result-judge{color:var(--txt-dim)}
  .cat-result-bar .rb{width:24px;height:24px;border-radius:3px;display:flex;
    align-items:center;justify-content:center;font-size:13px;font-weight:800;
    line-height:1;border:1px solid var(--line);box-sizing:border-box}
  .cat-res-row.cat-res-hit{border-color:var(--green);
    background:color-mix(in srgb,var(--green) 14%,var(--card))}
  .ora-lbl{font-size:12px;color:var(--txt-dim);font-weight:600;margin-bottom:6px}
  .ora-chips{display:flex;flex-wrap:wrap;gap:4px}
  .ora-chip{display:flex;flex-direction:column;align-items:center;justify-content:flex-start;
    gap:2px;width:30px;padding:2px 0 1px;border-radius:5px;border:0;
    background:transparent;cursor:pointer;
    opacity:.45;filter:grayscale(.4);transition:opacity .12s,filter .12s;
    -webkit-tap-highlight-color:transparent}
  .ora-chip .rb{width:28px;height:28px;border-radius:3px;display:flex;align-items:center;
    justify-content:center;font-size:15px;font-weight:800;line-height:1;
    border:1px solid var(--line);box-sizing:border-box;
    transition:box-shadow .12s}
  .ora-chip-mk{height:13px;line-height:13px;font-size:12px;font-weight:800;
    color:var(--txt);text-align:center}
  .ora-chip-lb{height:12px;line-height:12px;min-width:14px;font-size:8.5px;font-weight:700;
    border-radius:2px;text-align:center;padding:0 2px}
  .ora-chip-lb.ana{background:rgba(90,160,255,.16);color:var(--blue)}
  .ora-chip-lb.weak{background:rgba(255,90,90,.16);color:var(--red)}
  .ora-chip-lb.layoff{background:color-mix(in srgb,var(--green) 16%,transparent);color:var(--green)}
  .ora-chip.on{opacity:1;filter:none}
  .ora-chip.on .rb{box-shadow:0 0 0 2px var(--gold),0 1px 4px rgba(0,0,0,.4)}
  .ora-chip.ora-dis{opacity:.15;cursor:not-allowed}
  .ora-chip.ora-dis .rb{box-shadow:none}
  .ora-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .ora-row .ora-lbl{margin-bottom:0;width:56px;flex:0 0 56px}
  .ora-axsel-lbl{font-size:12px;color:var(--txt-dim);width:56px;flex:0 0 56px}
  .ora-num{width:60px;padding:6px 8px;font-size:14px;border:1px solid var(--line);
    border-radius:4px;background:var(--card);color:var(--txt);text-align:center}
  .ora-unit{font-size:12px;color:var(--txt-dim)}
  .ora-gen{margin-left:auto;padding:8px 16px;font-size:13px;font-weight:700;
    border:1px solid var(--gold);border-radius:0;cursor:pointer;
    background:color-mix(in srgb,var(--gold) 16%,var(--card));color:var(--gold);
    font-family:'Noto Serif JP',serif;letter-spacing:.04em}
  .ora-gen:active{background:color-mix(in srgb,var(--gold) 28%,var(--card))}
  .ora-row-alt{justify-content:space-between;margin-top:6px}
  .ora-gen-alt{margin-left:0}
  /* 御告 ドロップリスト(点数/軸人数共通) */
  .ora-sel{width:84px;box-sizing:border-box;padding:6px 28px 6px 10px;font-size:13px;font-weight:700;
    border:1px solid var(--line);border-radius:6px;cursor:pointer;
    background:color-mix(in srgb,var(--card) 70%,transparent);color:var(--txt);
    font-family:inherit;font-variant-numeric:tabular-nums;
    -webkit-appearance:none;appearance:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%23a98b4a' stroke-width='3'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 9px center}
  .ora-sel:focus{outline:none;border-color:var(--gold)}
  .ora-axsel{display:flex;align-items:center;gap:8px}
  .ora-result{margin-top:6px}
  .ora-warn{font-size:13px;color:var(--gold);padding:12px;text-align:center;
    border:1px dashed var(--line);border-radius:4px}
  .ora-rhead{font-size:12px;color:var(--txt-dim);font-weight:700;margin:8px 0 6px}
  .ora-list{display:flex;flex-direction:column;gap:5px}
  .ora-item{display:flex;align-items:center;gap:10px;padding:7px 10px;
    background:color-mix(in srgb,var(--card) 60%,transparent);
    border:1px solid var(--line);border-radius:6px}
  .ora-rank{font-size:11px;color:var(--txt-dim);min-width:18px;text-align:center;font-weight:700}
  .ora-bikes{display:flex;align-items:center;gap:6px;flex:1}
  .ora-bk{display:inline-flex;align-items:center;justify-content:center;
    width:26px;height:26px;border-radius:2px;font-size:13px;font-weight:700;line-height:1;
    border:1px solid var(--line);
    box-shadow:inset 0 0 6px rgba(0,0,0,.7);filter:brightness(.94) saturate(.72)}
  .ora-arr{color:var(--txt-dim);font-size:12px}
  .ora-item-hit{border-color:var(--green);
    background:color-mix(in srgb,var(--green) 14%,var(--card))}
  .ora-hit{font-size:10px;font-weight:800;color:var(--green);
    padding:1px 6px;border:1px solid var(--green);border-radius:4px;
    background:color-mix(in srgb,var(--green) 18%,transparent);white-space:nowrap}
  .ora-score{font-size:13px;font-weight:700;color:var(--txt);min-width:36px;text-align:right}
  /* オッズ表示(御告買い目セル & オッズタブ共通) */
  .ora-score.od-odds{min-width:54px;font-variant-numeric:tabular-nums}
  .odds-bai{font-size:9px;font-weight:600;color:var(--txt-dim);margin-left:1px}
  .od-loading{color:var(--txt-dim);font-weight:400}
  .od-na{color:var(--txt-dim)}
  .od-honmei{color:var(--gold-strong)}
  .od-taikou{color:var(--blue)}
  .od-ana{color:#e8772e}
  .od-oketa{color:#e23b3b}
  /* オッズタブ */
  .odds-refresh{margin-left:auto;padding:5px 16px;font-size:12px;font-weight:700;
    border:1px solid var(--gold);border-radius:0;cursor:pointer;
    background:color-mix(in srgb,var(--gold) 18%,var(--card));color:var(--gold);
    font-family:'Noto Serif JP',serif;letter-spacing:.08em}
  .odds-refresh:active{background:color-mix(in srgb,var(--gold) 34%,var(--card))}
  .odds-meta{font-size:10.5px;color:var(--txt-dim);margin-bottom:8px}
  .od-mine-lab{color:var(--gold)}
  .odds-list{display:flex;flex-direction:column;gap:4px}
  .odds-item{display:flex;align-items:center;gap:9px;padding:6px 10px;
    background:color-mix(in srgb,var(--card) 60%,transparent);
    border:1px solid var(--line);border-radius:6px}
  .odds-rank{font-size:11px;color:var(--txt-dim);min-width:26px;text-align:center;font-weight:700}
  .od-mine-dot{color:var(--gold);font-size:10px;line-height:1;margin-left:4px;vertical-align:middle}
  .odds-bikes{display:flex;align-items:center;gap:5px;flex:1}
  .odds-val{font-size:14px;font-weight:800;min-width:60px;text-align:right;
    font-variant-numeric:tabular-nums}
  .odds-item.od-mine{border-color:color-mix(in srgb,var(--gold) 55%,var(--line));
    background:color-mix(in srgb,var(--gold) 9%,var(--card))}
  .odds-item.od-hit{border-color:var(--green);
    background:color-mix(in srgb,var(--green) 14%,var(--card))}
  .od-hit-badge{font-size:10px;font-weight:800;color:var(--green);
    padding:1px 6px;border:1px solid var(--green);border-radius:4px;
    background:color-mix(in srgb,var(--green) 18%,transparent);white-space:nowrap}
  /* 実績バックテスト */
  .bt-open-btn{position:absolute;left:10px;top:50%;transform:translateY(-50%);
    padding:5px 11px;font-size:12px;font-weight:800;
    border:1px solid var(--gold);border-radius:7px;cursor:pointer;
    background:color-mix(in srgb,var(--gold) 18%,var(--card));color:var(--gold);
    font-family:'Noto Serif JP',serif;letter-spacing:.08em;z-index:2}
  .bt-open-btn:active{background:color-mix(in srgb,var(--gold) 34%,var(--card))}
  /* ハンバーガーメニュー */
  .menu-btn-wrap{position:absolute;left:10px;top:50%;transform:translateY(-50%);
    display:inline-block;z-index:2}
  .menu-btn{position:relative;display:flex;flex-direction:column;
    width:40px;height:32px;justify-content:center;align-items:center;gap:5px;padding:0;
    border:none;border-radius:0;cursor:pointer;background:transparent}
  .menu-btn span{display:block;width:22px;height:2px;background:#9a9a9a;
    border-radius:0}
  .menu-btn:active span{background:#cfcfcf}
  .menu-overlay{display:none;position:fixed;inset:0;z-index:240;
    background:rgba(0,0,0,.55);backdrop-filter:blur(2px)}
  .menu-panel{position:absolute;left:0;top:0;bottom:0;width:250px;max-width:78vw;
    background:var(--card);border-right:1px solid var(--gold);border-radius:0;
    box-shadow:6px 0 30px rgba(0,0,0,.45);display:flex;flex-direction:column;
    font-family:'Noto Serif JP',serif;
    animation:menuSlide .18s ease-out}
  @keyframes menuSlide{from{transform:translateX(-100%)}to{transform:translateX(0)}}
  .menu-head{padding:16px 18px;border-bottom:1px solid var(--line);
    color:var(--gold);font-weight:800;letter-spacing:.16em;font-size:14px}
  .menu-item{padding:15px 18px;font-size:13px;font-weight:700;color:var(--txt);
    border-bottom:1px solid var(--line);cursor:pointer;letter-spacing:.08em}
  .menu-item:active{background:color-mix(in srgb,var(--gold) 16%,transparent)}
  .menu-item-row{display:flex;align-items:center;justify-content:space-between}
  /* 未読バッジ(赤丸) */
  .err-badge{display:none;min-width:16px;height:16px;line-height:16px;padding:0 4px;
    border-radius:8px;background:#d9342b;color:#fff;font-size:10px;font-weight:800;
    text-align:center;box-shadow:0 0 0 1px rgba(0,0,0,.25)}
  .err-badge.on{display:inline-block}
  /* メニューボタン上の小バッジ: 3本線と並ばないよう右上に絶対配置 */
  .menu-btn-wrap .err-badge{position:absolute;top:-7px;right:-7px;margin:0;
    pointer-events:none;z-index:3}
  .menu-btn-wrap .err-badge.on{display:inline-block}
  /* メニュー内項目の未読数は赤文字バッジ(背景なし) */
  #errlogMenuBadge{background:transparent;color:#e5453b;box-shadow:none;
    font-weight:800;padding:0;min-width:0;height:auto;line-height:1.2}
  /* エラーログ オーバーレイ */
  .errlog-overlay{display:none;position:fixed;inset:0;z-index:60;
    background:rgba(0,0,0,.55);backdrop-filter:blur(2px)}
  .errlog-panel{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
    width:94vw;max-width:560px;max-height:86vh;display:flex;flex-direction:column;
    background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .errlog-head{display:flex;align-items:center;justify-content:space-between;
    padding:12px 14px;border-bottom:1px solid var(--line)}
  .errlog-head .ttl{font-size:14px;font-weight:800;color:var(--gold);letter-spacing:.06em}
  .errlog-tools{display:flex;gap:8px;align-items:center}
  .errlog-btn{padding:6px 11px;font-size:12px;font-weight:700;border-radius:7px;
    border:1px solid var(--line);background:transparent;color:var(--txt);cursor:pointer}
  .errlog-btn:active{background:color-mix(in srgb,var(--gold) 18%,transparent)}
  .errlog-close{font-size:20px;line-height:1;color:var(--txt-dim);cursor:pointer;
    background:none;border:none;padding:2px 6px}
  .errlog-body{overflow:auto;padding:10px 12px;flex:1}
  .errlog-empty{color:var(--txt-dim);font-size:13px;text-align:center;padding:30px 0}
  .errlog-item{border:1px solid var(--line);border-radius:8px;margin-bottom:9px;
    background:rgba(0,0,0,.18);overflow:hidden}
  .errlog-item.unread{border-color:#d9342b}
  .errlog-itop{display:flex;align-items:center;justify-content:space-between;
    padding:8px 10px;gap:8px}
  .errlog-cat{font-size:12px;font-weight:800;color:var(--txt)}
  .errlog-cat .dot{display:inline-block;width:7px;height:7px;border-radius:50%;
    background:#d9342b;margin-right:6px;vertical-align:middle}
  .errlog-item.read .errlog-cat .dot{background:var(--txt-dim);opacity:.4}
  .errlog-time{font-size:10px;color:var(--txt-dim)}
  .errlog-detail{font-family:monospace;font-size:11px;color:var(--txt-dim);
    white-space:pre-wrap;word-break:break-all;padding:0 10px 8px;max-height:170px;overflow:auto}
  .errlog-copy{padding:4px 9px;font-size:11px;font-weight:700;border-radius:6px;
    border:1px solid var(--line);background:transparent;color:var(--gold);cursor:pointer}
  .errlog-copy:active{background:color-mix(in srgb,var(--gold) 22%,transparent)}
  .menu-sec{margin-top:auto;padding:14px 18px 18px;border-top:1px solid var(--line)}
  .menu-sec-ttl{font-size:11px;color:var(--gold);letter-spacing:.12em;
    margin-bottom:8px;font-weight:700}
  .mn-body{font-size:12px;color:var(--txt-dim)}
  .mn-row{display:flex;justify-content:space-between;padding:3px 0}
  .mn-row b{color:var(--txt);font-weight:800}
  .bt-overlay{display:none;position:fixed;inset:0;z-index:200;
    background:rgba(0,0,0,.62);backdrop-filter:blur(3px);overflow-y:auto}
  .bt-panel{max-width:520px;margin:40px auto;background:var(--card);
    border:1px solid var(--gold);border-radius:14px;overflow:hidden;
    box-shadow:0 14px 48px rgba(0,0,0,.5)}
  .bt-head{display:flex;align-items:center;padding:14px 18px;
    border-bottom:1px solid var(--line)}
  .bt-ttl{font-family:'Noto Serif JP',serif;font-weight:800;font-size:16px;
    letter-spacing:.16em;color:var(--gold)}
  .bt-close{margin-left:auto;background:none;border:none;color:var(--txt-dim);
    font-size:24px;cursor:pointer;line-height:1;padding:0 4px}
  .bt-body{padding:18px}
  .bt-row{display:flex;align-items:center;gap:12px;margin-bottom:12px}
  .bt-row label{font-size:13px;color:var(--txt-dim);min-width:40px}
  .bt-row input[type=date]{flex:1;padding:8px 10px;border:1px solid var(--line);
    border-radius:7px;background:color-mix(in srgb,var(--card) 70%,transparent);
    color:var(--txt);font-size:14px;font-family:inherit}
  .bt-note{font-size:11px;color:var(--txt-dim);line-height:1.6;margin:10px 0 14px}
  .bt-force{display:flex;justify-content:center;margin:0 0 10px 0}
  .bt-force label{display:flex;align-items:center;gap:6px;
    font-size:12px;color:var(--txt);cursor:pointer;
    padding:6px 10px;border:1px solid var(--line);border-radius:6px;
    background:color-mix(in srgb,var(--card) 70%,transparent)}
  .bt-force input{accent-color:var(--gold);width:14px;height:14px}
  .bt-actions{display:flex;justify-content:center;margin-bottom:6px}
  .bt-run{width:180px;max-width:70%;padding:10px 0;font-size:14px;font-weight:800;border:none;
    border-radius:0;cursor:pointer;color:#1a1208;text-align:center;
    background:linear-gradient(135deg,var(--gold),var(--gold-strong,#d4a843));
    font-family:'Noto Serif JP',serif;letter-spacing:.12em}
  .bt-run:active{filter:brightness(.92)}
  .bt-progress{font-size:11.5px;color:var(--txt-dim);text-align:center;
    margin-top:12px;min-height:16px}
  .bt-result{margin-top:14px}
  .bt-meta{font-size:11px;color:var(--txt-dim);margin-bottom:8px;text-align:center}
  .bt-table{width:100%;border-collapse:collapse;font-size:13px}
  .bt-table th{font-size:11px;color:var(--txt-dim);font-weight:600;
    padding:7px 4px;border-bottom:1px solid var(--line);text-align:center}
  .bt-table td{padding:7px 4px;text-align:center;border-bottom:1px solid var(--line);
    font-variant-numeric:tabular-nums}
  .bt-table tr.bt-best{background:color-mix(in srgb,var(--gold) 14%,transparent)}
  .bt-table tr.bt-best td{font-weight:800}
  .bt-pos{color:var(--green,#36b37e);font-weight:700}
  .bt-neg{color:#e23b3b}
  .bt-best-note{font-size:12px;color:var(--gold);text-align:center;
    margin-top:12px;font-weight:700}
  .bt-axsel{flex:1;padding:8px 10px;border:1px solid var(--line);border-radius:7px;
    background:color-mix(in srgb,var(--card) 70%,transparent);color:var(--txt);
    font-size:14px;font-family:inherit}
  .bt-detail-h{font-size:12px;color:var(--txt-dim);font-weight:700;
    margin:18px 0 8px;padding-top:14px;border-top:1px solid var(--line);
    display:flex;align-items:center;gap:10px}
  .bt-detail-sel{margin-left:auto;padding:6px 26px 6px 10px;font-size:12px;
    font-weight:700;border:1px solid var(--line);border-radius:6px;cursor:pointer;
    background:color-mix(in srgb,var(--card) 70%,transparent);color:var(--txt);
    font-family:inherit;-webkit-appearance:none;appearance:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%23a98b4a' stroke-width='3'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 8px center}
  .bt-detail-sel:focus{outline:none;border-color:var(--gold)}
  .bt-scope{margin:6px 0 2px 0;font-size:12px;color:var(--txt-dim)}
  .bt-scope-sel{margin-left:6px;font-size:12px;padding:2px 4px;
    background:var(--card);color:var(--txt);border:1px solid var(--line);
    border-radius:4px}
  .bt-scope-note{font-size:10.5px;margin-top:2px;opacity:.8}
  .bt-dtable{width:100%;border-collapse:collapse;font-size:12px}
  /* v318: 期待値タブの買い目内訳 */
  .bt-dt-exp{cursor:pointer}
  .bt-dt-exp:active{background:rgba(255,255,255,.05)}
  .bt-caret{font-size:9px;color:var(--txt-dim)}
  .bt-picks{padding:6px 4px 8px 4px}
  .bt-picks-h{font-size:11px;color:var(--txt-dim);margin-bottom:4px}
  .bt-pick{display:flex;gap:6px;align-items:center;font-size:11.5px;
    padding:3px 4px;border-radius:4px;margin-bottom:2px;flex-wrap:wrap}
  .bt-pick-on{background:rgba(120,200,120,.13)}
  .bt-pick-off{background:rgba(255,255,255,.03);color:var(--txt-dim)}
  .bt-pick-hit{outline:1px solid rgba(120,200,120,.75)}
  .bt-pk-no{min-width:14px;text-align:right;color:var(--txt-dim)}
  .bt-pk-c{font-weight:700;min-width:52px}
  .bt-pk-p{min-width:66px}
  .bt-pk-o{min-width:56px}
  .bt-pk-ev{min-width:56px;font-weight:700}
  .bt-pk-v{min-width:42px}
  .bt-pk-hit{color:#7fd67f;font-weight:700}
  .bt-post{font-size:9.5px;color:var(--txt-dim)}
  .bt-pk-pay{color:#d8b878}
  /* v322: 未定義だったクラスを補う(表示崩れ防止) */
  .bt-pick-row{background:rgba(255,255,255,.02)}
  .bt-pick-row td{padding:0}
  .bt-dt-row{}
  .bt-dt-tri{font-weight:600}
  .bt-dt-ref{text-align:right;white-space:nowrap}
  .bt-refresh{font-size:11px;color:var(--txt-dim)}
  .bt-dtable th{font-size:10.5px;color:var(--txt-dim);font-weight:600;
    padding:6px 3px;border-bottom:1px solid var(--line);text-align:center}
  .bt-dtable td{padding:6px 3px;text-align:center;border-bottom:1px solid var(--line);
    font-variant-numeric:tabular-nums}
  .bt-am{font-weight:800;font-size:15px;color:var(--txt)}
  .bt-dtable{table-layout:fixed}
  .bt-dtable th.bt-c-date, .bt-dtable td.bt-c-date{width:15%;text-align:center}
  .bt-dtable th.bt-c-race, .bt-dtable td.bt-c-race{width:19%;text-align:center}
  .bt-dtable th.bt-c-lab, .bt-dtable td.bt-c-lab{width:7%;text-align:center;padding:4px 2px}
  .bt-dtable th.bt-c-am, .bt-dtable td.bt-c-am{width:9%;text-align:center}
  .bt-dtable th.bt-c-tri, .bt-dtable td.bt-c-tri{width:17%;text-align:center;letter-spacing:.02em}
  .bt-dtable th.bt-c-ref, .bt-dtable td.bt-c-ref{width:26%;text-align:center;font-weight:700;white-space:nowrap}
  .bt-dtable th.bt-c-hit, .bt-dtable td.bt-c-hit{width:7%;text-align:center;padding:6px 0}
  .bt-dtable td.bt-c-lab{vertical-align:middle}
  .bt-lab-stack{display:flex;flex-direction:column;align-items:center;gap:2px;line-height:0}
  .bt-bar{display:block;width:14px;height:3px;border-radius:1px}
  .bt-past{margin-top:10px;background:linear-gradient(135deg,var(--gold),var(--gold-strong,#d4a843))}
  .bt-tabs-main{display:flex;gap:6px;margin:14px 0 8px}
  .bt-tabm{flex:1;padding:9px 4px;font-size:14px;font-weight:800;cursor:pointer;
    border:1px solid var(--line);border-radius:8px;background:color-mix(in srgb,var(--card) 60%,transparent);
    color:var(--txt-dim);font-family:'Noto Serif JP',serif;letter-spacing:.1em}
  .bt-tabm.on{border-color:var(--gold);color:var(--gold);
    background:color-mix(in srgb,var(--gold) 16%,var(--card))}
  .bt-tabs-sub{display:flex;gap:5px;margin-bottom:10px}
  .bt-tabs{flex:1;padding:7px 3px;font-size:13px;font-weight:700;cursor:pointer;
    border:1px solid var(--line);border-radius:6px;background:color-mix(in srgb,var(--card) 55%,transparent);
    color:var(--txt-dim);font-family:'Noto Serif JP',serif;letter-spacing:.06em}
  .bt-tabs.on{border-color:var(--gold);color:var(--gold);
    background:color-mix(in srgb,var(--gold) 14%,var(--card));font-weight:800}
  .bt-result-body{margin-top:4px;max-height:420px;overflow-y:auto;
    -webkit-overflow-scrolling:touch}
  /* 過去集計: 年バー・日付バー */
  .bt-year-bar{display:flex;align-items:center;justify-content:space-between;width:100%;
    padding:11px 16px;margin-bottom:6px;border:1px solid var(--gold);border-radius:8px;
    cursor:pointer;background:color-mix(in srgb,var(--gold) 12%,var(--card));color:var(--gold);
    font-family:'Noto Serif JP',serif;font-size:15px;font-weight:800;letter-spacing:.08em}
  .bt-year-cnt{font-size:12px;color:var(--txt-dim);font-weight:600}
  .bt-day-bar{display:flex;align-items:center;justify-content:space-between;width:100%;
    padding:9px 14px;margin:0 0 5px 0;
    border:1px solid var(--line);border-radius:7px;cursor:pointer;
    background:color-mix(in srgb,var(--card) 60%,transparent);color:var(--txt);
    font-size:14px;font-weight:700;font-family:'Noto Serif JP',serif;letter-spacing:.06em}
  .bt-day-cnt{font-size:11px;color:var(--txt-dim)}
  .bt-month-bar{display:flex;align-items:center;justify-content:space-between;width:100%;
    padding:10px 14px;margin:4px 0 4px 0;
    border:1px solid color-mix(in srgb,var(--gold) 40%,var(--line));border-radius:7px;
    cursor:pointer;background:color-mix(in srgb,var(--gold) 6%,var(--card));
    color:var(--txt);font-size:14px;font-weight:800;
    font-family:'Noto Serif JP',serif;letter-spacing:.07em}
  .bt-month-label{color:var(--gold)}
  .bt-month-cnt{font-size:11px;color:var(--txt-dim);font-weight:600}
  .bt-month-sum{background:color-mix(in srgb,var(--gold) 18%,var(--card));
    border-color:var(--gold);color:var(--gold)}
  .bt-venue-h{margin:6px 0;font-size:13px;color:var(--txt);
    display:flex;align-items:center;gap:6px;font-weight:700}
  .bt-day-result{margin:2px 0 10px 12px;padding:8px 0;
    border-left:2px solid color-mix(in srgb,var(--gold) 40%,transparent);padding-left:10px}
  .bt-day-result-full{margin:4px 0 12px 0;padding:8px 0 0;
    border-top:1px solid color-mix(in srgb,var(--gold) 35%,transparent)}
  .bt-pastlist{margin-top:10px}
  .bt-pastlist[data-open="1"]{margin-bottom:6px}
  .bt-past-load{font-size:12px;color:var(--txt-dim);text-align:center;padding:8px}
  .bt-past-item{display:flex;align-items:center;justify-content:space-between;
    width:100%;padding:9px 14px;margin-bottom:6px;border:1px solid var(--line);
    border-radius:8px;cursor:pointer;background:color-mix(in srgb,var(--card) 60%,transparent);
    color:var(--txt);font-family:inherit}
  .bt-past-item:active{background:color-mix(in srgb,var(--gold) 16%,var(--card))}
  .bt-past-date{font-size:13px;font-weight:700}
  .bt-past-meta{font-size:11px;color:var(--txt-dim)}
  .bt-bar-ana{background:var(--blue)}
  .bt-bar-weak{background:var(--red)}
  .bt-bar-layoff{background:var(--green)}
  .bt-hit-badge{font-size:10px;font-weight:800;color:var(--green,#36b37e);
    padding:1px 5px;border:1px solid var(--green,#36b37e);border-radius:4px;
    background:color-mix(in srgb,var(--green,#36b37e) 16%,transparent);
    white-space:nowrap}
  .bt-detail-note{font-size:10px;color:var(--txt-dim);margin-top:8px;line-height:1.6}
  .ora-range{flex:1;margin:0 8px;accent-color:var(--gold);min-width:80px}
  .ora-pill{font-size:10px;color:var(--txt-dim);font-variant-numeric:tabular-nums;
    padding:1px 6px;border:1px solid var(--line);border-radius:8px;margin-right:6px;white-space:nowrap}
  .ora-pill-new{color:var(--gold);border-color:color-mix(in srgb,var(--gold) 50%,var(--line));
    font-weight:700}
  .ora-pill-legend{font-size:9.5px;color:var(--txt-dim);font-weight:400;margin-left:8px}
  .ora-detail{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}
  .ora-dh{font-size:11px;color:var(--txt-dim);font-weight:600;margin-bottom:6px}
  .ora-drow{display:flex;align-items:center;gap:10px;padding:3px 0}
  .ora-dpts{font-size:13px;font-weight:700;color:var(--txt);min-width:30px}
  .ora-drole{font-size:11px;color:var(--txt-dim)}
  .ora-foot{margin-top:12px;font-size:10.5px;color:var(--txt-dim);line-height:1.5}
  /* 託宣(おまかせ)ボタン: カードヘッダ右上, 琥珀 */
  .ora-omakase{margin-left:auto;padding:6px 18px;font-size:14px;font-weight:800;
    border:1px solid var(--gold);border-radius:6px;cursor:pointer;
    background:color-mix(in srgb,var(--gold) 22%,var(--card));color:var(--gold);
    font-family:'Noto Serif JP',serif;letter-spacing:.12em;
    box-shadow:0 0 10px color-mix(in srgb,var(--gold) 30%,transparent)}
  .ora-omakase:active{background:color-mix(in srgb,var(--gold) 38%,var(--card))}
  .card-h{display:flex;align-items:center}
  /* 層ブロック */
  .ora-layer{margin:10px 0 4px;padding:8px 10px;border-radius:8px;
    border:1px solid var(--line)}
  .ora-honsen{border-left:3px solid var(--gold-strong);
    background:color-mix(in srgb,var(--gold) 6%,transparent)}
  .ora-osae{border-left:3px solid #5b9bd5;
    background:color-mix(in srgb,#5b9bd5 6%,transparent)}
  .ora-ana{border-left:3px solid #e8772e;
    background:color-mix(in srgb,#e8772e 7%,transparent)}
  .ora-lh{font-size:13px;font-weight:800;margin-bottom:6px;color:var(--txt)}
  .ora-lc{font-size:11px;font-weight:600;color:var(--txt-dim);margin-left:4px}
  .ora-tag{font-size:10px;font-weight:700;padding:1px 7px;border-radius:10px;
    border:1px solid var(--line);color:var(--txt-dim);margin-left:6px;vertical-align:middle}
  .ora-axkim{display:inline-flex;align-items:center;gap:3px;margin-right:8px}
  .ora-kim{font-size:11px;font-weight:700;color:var(--gold);
    padding:1px 5px;border:1px solid var(--gold);border-radius:4px}
  .ora-tags{display:inline-flex;gap:3px;margin-right:6px}
  .ora-mini{font-size:9px;font-weight:800;padding:1px 4px;border-radius:3px;line-height:1.3}
  .ora-mini.ana{background:rgba(90,160,255,.16);color:var(--blue);border:1px solid color-mix(in srgb,var(--blue) 55%,transparent)}
  .ora-mini.weak{background:rgba(226,59,59,.2);color:#e23b3b;border:1px solid #e23b3b}
  .ora-mini.lay{background:rgba(43,178,77,.2);color:#2bb24d;border:1px solid #2bb24d}

  /* ローディング */
  .spin{display:none}
  @keyframes sp{to{transform:rotate(360deg)}}
  /* 読み込み中ブロック (プログレスバー + % を横一列。ロゴは右上が考え中になる) */
  .loadwrap{display:flex;flex-direction:row;align-items:center;justify-content:center;
    padding:34px 20px;gap:12px}
  /* 段階表示 (v330): 光条が段階ごとに灯る */
  .loadphase{margin:14px auto;max-width:360px;padding:0 10px}
  /* 語り (タイプライター) */
  /* v332: 語りは「託宣とは」の説明文と同じ字面にする。
     色・大きさ・行間・字間をすべて .oracle-meaning に揃え、
     冒頭だけ色を変える扱いも廃止した。 */
  .maria{margin-top:20px;text-align:left}
  .maria .mline{margin:0 0 14px;font-size:11.5px;line-height:2.1;
    letter-spacing:.14em;color:var(--txt-dim)}
  .loadphase .pline{display:flex;gap:4px;height:3px;margin-bottom:9px}
  .loadphase .pseg{flex:1;border-radius:2px;background:rgba(255,255,255,.07);
    position:relative;overflow:hidden;transition:background .35s}
  .loadphase .pseg.done{
    background:linear-gradient(90deg,#8a6a1e,#ffd98a);
    box-shadow:0 0 7px rgba(255,200,90,.55)}
  .loadphase .pseg.now{
    background:linear-gradient(90deg,rgba(255,200,90,.15),rgba(255,200,90,.5));
    box-shadow:0 0 6px rgba(255,190,80,.35)}
  .loadphase .pseg.now::after{content:"";position:absolute;top:0;bottom:0;
    width:36%;background:linear-gradient(90deg,transparent,#ffe6a8,transparent);
    animation:pscan 1.15s ease-in-out infinite}
  @keyframes pscan{0%{left:-40%}100%{left:104%}}
  .loadphase .ptext{display:flex;align-items:baseline;gap:9px;
    justify-content:center;flex-wrap:wrap}
  .loadphase .pnum{font-size:11px;color:#7d715b;letter-spacing:.12em;
    font-family:monospace}
  .loadphase .pttl{font-size:14px;color:#ffe6a8;letter-spacing:.18em;
    text-shadow:0 0 8px rgba(255,200,90,.45)}
  .loadphase .psub{font-size:10px;color:#6f6552;letter-spacing:.05em}

  .loadbar{flex:1 1 auto;max-width:200px;height:6px;border-radius:1px;
    background:color-mix(in srgb,var(--line) 60%,transparent);overflow:hidden}
  .loadbar-fill{height:100%;width:0;border-radius:1px;
    background:color-mix(in srgb,var(--gold) 55%,transparent);
    transition:width .25s ease}
  .loadpct{flex:0 0 auto;font-size:12px;font-weight:700;color:var(--txt-dim);
    letter-spacing:.06em;min-width:34px;text-align:right}
  .fade{animation:fd .3s ease}
  @keyframes fd{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  /* ============================================================
     v333: 角の統一。※必ずCSSの最後に置くこと。
       会場ボタンの形 (左は角のまま / 右上下だけ丸める) を全体へ広げる。
       v332では先頭側に書いたため、後方の border-radius:0 に上書きされて
       まったく効いていなかった。同じ強さの指定は後に書いた方が勝つ。
       色や余白は触らず、形だけを揃える。
       左に色の縦線を持つものは 0 8px 8px 0、
       線を持たない箱は四隅を 8px にする。
     ============================================================ */
  .rcell,
  .resultcard,
  .pcard,
  .bar-row.sel-row,
  .third-grp,
  .bt-meta{border-radius:0 8px 8px 0}

  /* 立体感: 左上にだけ光を置き、内側に影を落とす。
     会場ボタンと同じ質感を、カードとボタンにも与える。 */
  .rcell,
  .resultcard,
  .pcard,
  .third-grp,
  .bt-meta,
  .line-ora-btn,
  .ora-gen,
  .odds-refresh,
  .bt-run{position:relative;
    background-image:radial-gradient(120% 130% at 0% 0%,
      rgba(255,214,150,.13) 0%, rgba(255,190,110,.05) 34%,
      rgba(0,0,0,0) 62%);
    box-shadow:inset 0 1px 0 rgba(255,225,180,.14),
               inset 0 -8px 16px rgba(0,0,0,.30),
               0 2px 6px rgba(0,0,0,.34)}
  .rcell::after,
  .resultcard::after,
  .pcard::after,
  .third-grp::after{content:"";position:absolute;left:0;top:0;
    width:52%;height:1px;pointer-events:none;
    background:linear-gradient(90deg,rgba(255,210,150,.55),transparent)}
  .rcell.target{box-shadow:0 0 9px rgba(255,140,50,.22),
    inset 0 1px 0 rgba(255,225,180,.20),
    inset 0 -8px 16px rgba(0,0,0,.26)}

  .line-ora-btn,
  .ora-gen,
  .odds-refresh,
  .bt-run,
  .tabbar,
  .subbar .sub,
  .cat-result-bar,
  .rchart-btn-wrap{border-radius:8px}

  /* 帯の中で並ぶものは、両端だけ丸めて中は角のままにする */
  .tabbar{overflow:hidden}
  .cat-swb:first-child{border-radius:8px 0 0 8px}
  .cat-swb:last-child{border-radius:0 8px 8px 0}

  /* v336: 会場ボタンは選択で灯す方式にしたので、
     狙い/予想の有無による色付けはここで打ち消す。 */
  .vchip.has-yosou{border-left:3px solid rgba(150,168,170,.45)}
  .vchip.has-yosou .nm{color:#9d9483}
  .vchip.active.has-yosou{border-left-color:#ffcf7a}
  .vchip.active.has-yosou .nm{color:#ffe9b0}

  /* ============================================================
     v336: Rボタンも会場ボタンと同じ「消灯/点灯」にする。
       選んだ1本だけ、発走時刻・種別・荒れの数値に灯が入る。
     ============================================================ */
  .rcell{transition:box-shadow .28s ease, background .28s ease,
                    border-color .28s ease}
  .rcell .rt,
  .rcell .gkind,
  .rcell .rbarlab{transition:.28s ease}
  .rcell .rno{transition:.28s ease}

  .rcell.sel{border-left-color:#ffcf7a;
    background-image:radial-gradient(120% 130% at 0% 0%,
      rgba(255,214,150,.22) 0%, rgba(255,190,110,.09) 36%,
      rgba(0,0,0,0) 66%);
    box-shadow:inset 0 1px 0 rgba(255,230,190,.24),
               inset 0 0 26px rgba(255,190,100,.16),
               0 0 16px rgba(255,180,90,.26)}
  .rcell.sel .rno{color:#fff3d8;
    text-shadow:0 0 12px rgba(255,210,140,.85)}
  .rcell.sel .rt{color:#e8d3a6;text-shadow:0 0 8px rgba(255,200,120,.5)}
  .rcell.sel .gkind{color:#d6c8a8;text-shadow:0 0 8px rgba(255,200,120,.35)}
  .rcell.sel .rbarlab{color:#e2d2ae;text-shadow:0 0 8px rgba(255,200,120,.45)}
  .rcell.sel .finmk{color:#d8c49c}

  /* v337: 「狙いレースなし」は選択しても灯さない (Ave.と同じ沈んだ色) */
  .vchip.active .vticker.none{color:#a3977c;font-weight:400;text-shadow:none}

  /* v337: 予想まわりの字を明朝に統一する。
     数字は tabular-nums で桁を揃え、等幅をやめても崩れないようにする。 */
  .yos-card, .yos-card *,
  .fl-wrap, .fl-wrap *,
  .stepbar, .stepbar *,
  .ar-card, .ar-card *,
  .third-grp, .third-grp *{font-family:'Noto Serif JP',serif}
  .yos-form, .fl-bet, .fl-buy, .fl-drop, .bt-mv,
  .yos-card .num, .fl-wrap .num{font-variant-numeric:tabular-nums;
    letter-spacing:.04em}

  /* v339: メニュー内の本日の的中集計 */
  .hit-tally{display:none}
  .hit-tally.open{display:block;padding:4px 18px 14px;
    font-family:'Noto Serif JP',serif;font-variant-numeric:tabular-nums}
  .ht-prog{font-size:12px;color:var(--txt-dim);display:flex;
    align-items:center;gap:8px;padding:6px 0}
  .ht-none{font-size:12px;color:var(--txt-dim);padding:6px 0}
  .ht-sum{font-size:12px;color:var(--gold-strong);padding:4px 0 8px;
    letter-spacing:.06em;border-bottom:1px solid var(--line);margin-bottom:6px}
  .ht-row{display:flex;align-items:baseline;gap:8px;font-size:11.5px;
    padding:4px 0;letter-spacing:.04em;white-space:nowrap}
  .ht-vn{color:#d9cbab;flex:0 0 auto;min-width:66px}
  .ht-nm{color:#ffe9a8;flex:0 0 auto}
  .ht-nm.target{color:#ffb0b0;text-shadow:0 0 7px rgba(224,80,80,.5)}
  .ht-tri{color:#f0e6cf;font-weight:700;flex:0 0 auto}
  .ht-yen{color:var(--gold-strong);font-weight:700;flex:1;text-align:right}

</style>
</head>
<body>

<div class="bgfx">
  <div class="bg-photo"></div>
  <div class="mold"></div>
  <div class="humidity"></div>
  <div class="lamp"></div>
  <div class="flicker-all"></div>
  <div class="noise"></div>
  <div class="lit-overlay"></div>
  <div class="flicker-dark"></div>
</div>
<div class="film-fx" aria-hidden="true"></div>

<div class="topbar">
  <span id="menuBtnWrap" class="menu-btn-wrap">
    <button id="menuBtn" class="menu-btn" onclick="toggleMenu()" aria-label="メニュー"><span></span><span></span><span></span></button>
    <span id="menuBtnBadge" class="err-badge"></span>
  </span>
  <div class="date-field">
    <div class="date-disp" id="dateDisp" onclick="openDatePicker()">
      <span id="dateDispText">— —</span>
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" style="opacity:.7"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </div>
    <input id="dateInput" type="date" onchange="onDateChange()">
    <button id="oracleBtn" onclick="loadVenues()">託宣</button>
  </div>
  <svg class="spark" viewBox="0 0 24 24" fill="none" aria-label="theme" onclick="toggleTheme()"><g class="sun-ic"><circle cx="12" cy="12" r="4.2" stroke="currentColor" stroke-width="1.6"/><path d="M12 2.2v3M12 18.8v3M2.2 12h3M18.8 12h3M5 5l2.1 2.1M16.9 16.9l2.1 2.1M19 5l-2.1 2.1M7.1 16.9L5 19" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></g><g class="moon-ic" style="display:none"><path d="M20 14.5A8 8 0 0 1 9.5 4a7 7 0 1 0 10.5 10.5z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></g></svg>
</div>

<div id="menuOverlay" class="menu-overlay" onclick="if(event.target===this)closeMenu()">
  <div class="menu-panel">
    <div class="menu-head">メニュー</div>
    <div class="menu-item" onclick="menuHitTally()">本日の的中集計</div>
    <div id="hitTallyBox" class="hit-tally"></div>
    <div class="menu-item" id="menuSyncDbItem" onclick="menuSyncDb()">DB同期</div>
    <div class="menu-item" onclick="errlogOpen()">
      <div class="menu-item-row"><span>エラー診断ログ</span>
        <span id="errlogMenuBadge" class="err-badge"></span></div>
    </div>
    <div class="menu-sec">
      <div class="menu-sec-ttl">データベース</div>
      <div id="dbStatusBody" class="mn-body">—</div>
    </div>
  </div>
</div>

<div id="errlogOverlay" class="errlog-overlay" onclick="if(event.target===this)errlogClose()">
  <div class="errlog-panel">
    <div class="errlog-head">
      <span class="ttl">エラー診断ログ</span>
      <div class="errlog-tools">
        <button class="errlog-btn" onclick="errlogCopyAll()">全文コピー</button>
        <button class="errlog-btn" onclick="errlogMarkAllRead()">全て既読</button>
        <button class="errlog-close" onclick="errlogClose()" aria-label="閉じる">&times;</button>
      </div>
    </div>
    <div id="errlogBody" class="errlog-body"></div>
  </div>
</div>

<div id="venueStrip" class="venue-strip"></div>
<div class="grid-wrap"><div id="gridMaria" class="grid-maria" aria-hidden="true"></div><div id="gridOracle" class="grid-oracle" aria-hidden="true"><span class="g-oracle-jp">託宣</span><div id="introKeirin"><span>KEIRIN</span></div><div id="introMsg">汝、悟らば─、また託されん。</div></div><div id="raceGrid" class="race-grid"></div></div>
<div class="oracle-wrap"><div id="oracleBar" class="oracle-bar"></div></div>
<div id="status" class="status"></div>
<div id="detail"></div>

<script>
var DATE = "";
var KIMARI_MODE = "今回";  // 決まり手表示: '今回'(役割別) / '通算'(全成績)
var VENUES = [];
var CUR_VENUE = null;
var IS_LIGHT = false;

function toggleTheme(){
  IS_LIGHT = !IS_LIGHT;
  // oracle-on 等の状態クラスを保持したまま light を切替
  document.body.classList.toggle("light", IS_LIGHT);
  document.documentElement.classList.toggle("light", IS_LIGHT);
  updateThemeIcon();
  // 表示中の託宣を新しい視点に切り替える
  refreshOracleForTheme();
}
// 明=太陽 / 暗=月 を表示
function updateThemeIcon(){
  var sun=document.querySelector(".spark .sun-ic");
  var moon=document.querySelector(".spark .moon-ic");
  if(sun) sun.style.display = IS_LIGHT ? "" : "none";
  if(moon) moon.style.display = IS_LIGHT ? "none" : "";
}
// 日付フィールド: 曜日付き明朝体表示 (例 6 / 1 (SUN))
var _WD=["SUN","MON","TUE","WED","THU","FRI","SAT"];
// v330: GitHub に置いてあるのは4日分だけなので、
//   それより前は選べないようにする。選んでも中身が無く、
//   「データがありません」と出るだけで分かりにくいため。
var PICK_DAYS = 4;

function applyDateLimit(){
  var di=document.getElementById("dateInput");
  if(!di) return;
  var t=new Date();
  var lo=new Date(t.getTime() - (PICK_DAYS-1)*86400000);
  function f(d){
    var m=String(d.getMonth()+1); if(m.length<2) m="0"+m;
    var da=String(d.getDate()); if(da.length<2) da="0"+da;
    return d.getFullYear()+"-"+m+"-"+da;
  }
  di.min=f(lo);
  di.max=f(t);
}

function openDatePicker(){
  var di=document.getElementById("dateInput");
  applyDateLimit();
  if(di.showPicker){ try{di.showPicker();return;}catch(e){} }
  di.focus(); di.click();
}
// 起動時にも範囲を効かせる
if(document.readyState==="loading"){
  document.addEventListener("DOMContentLoaded", function(){ applyDateLimit(); });
}else{
  setTimeout(function(){ applyDateLimit(); }, 0);
}

function updateDateDisp(){
  var di=document.getElementById("dateInput");
  var t=document.getElementById("dateDispText");
  if(!di||!t) return;
  var v=di.value; // YYYY-MM-DD
  if(!v){ t.textContent="— —"; return; }
  var p=v.split("-");
  var d=new Date(parseInt(p[0]),parseInt(p[1])-1,parseInt(p[2]));
  t.textContent = _WD[d.getDay()]+" "+parseInt(p[1])+"/"+parseInt(p[2]);
}
function onDateChange(){
  // 範囲外を選ばれたら戻す (端末によっては min/max を無視するため)
  var di=document.getElementById("dateInput");
  if(di && di.value){
    if(di.min && di.value < di.min){ di.value = di.min; }
    if(di.max && di.value > di.max){ di.value = di.max; }
  }
  updateDateDisp();
}

function menuSyncDb(){
  // メニュー「DB同期」: 明示的に sync=1 で呼び、GitHubの月別DB差分と
  // 統計ファイルを取り込む。託宣ボタンは同期しない(軽さ維持)ため、
  // DBを最新にしたいときはここから実行する。
  var el = document.getElementById("menuSyncDbItem");
  if (el && el.getAttribute("data-busy") === "1") { return; }
  if (el) {
    el.setAttribute("data-busy", "1");
    el.innerHTML = 'DB同期 <span class="spin"></span><span style="font-size:11px;opacity:.8"> 同期中…(数分)</span>';
  }
  var d = (typeof DATE !== "undefined" && DATE) ? DATE : "";
  var url = "/api/build_cache?sync=1" + (d ? ("&date=" + d) : "");
  fetch(url)
    .then(function(r){ return r.json(); })
    .then(function(res){
      var msg = "";
      try { msg = String(res.db_sync || ""); } catch(_e) { msg = ""; }
      if (el) {
        el.removeAttribute("data-busy");
        var ok = (msg.indexOf("エラー") < 0 && msg.indexOf("失敗") < 0);
        el.innerHTML = 'DB同期 <span style="font-size:11px;opacity:.85">'
                     + (ok ? '完了' : '失敗') + '</span>';
        setTimeout(function(){ el.innerHTML = 'DB同期'; }, 4000);
      }
      if (msg && (msg.indexOf("エラー") >= 0 || msg.indexOf("失敗") >= 0)) {
        try { logError('DB同期', msg); } catch(_e) {}
      }
      // DB情報(最新日/サイズ)を再取得して表示更新
      try { if (typeof loadDbStatus === "function") { loadDbStatus(); } } catch(_e) {}
    })
    .catch(function(e){
      if (el) {
        el.removeAttribute("data-busy");
        el.innerHTML = 'DB同期 <span style="font-size:11px;opacity:.85">失敗</span>';
        setTimeout(function(){ el.innerHTML = 'DB同期'; }, 4000);
      }
      try { logError('DB同期', '通信失敗: ' + e); } catch(_e) {}
    });
}

// v341: 「結果分析」「確定ロジック検証」は廃止した。
//   これから見るのは当日の実績だけで、過去日の検証はしない。
//   入口をここで断つ。以降の bt*/fl* 群は到達しない。
// ===== 確定ロジック検証 (v282: 3軸×2ナガシ = 6パターン) =====
var _FL_PATS  = ["r1k","r1m","r2k","r2m","khk","khm","tkk","tkm"];
var _FL_LABELS = {r1k:"rs1軸\u00d7気配順", r1m:"rs1軸\u00d7適合順",
                  r2k:"rs2軸\u00d7気配順", r2m:"rs2軸\u00d7適合順",
                  khk:"気配1軸\u00d7気配順", khm:"気配1軸\u00d7適合順",
                  tkk:"適合1軸\u00d7気配順", tkm:"適合1軸\u00d7適合順"};
var _FL_SEL   = {r1k:true, r1m:true};   // 初期選択(従来の2パターン)
var _FL_LAST  = null;                   // 直近のAPIレスポンス
var _FL_MODE  = "";                     // "run" | "stats"
var _FL_VENUE = "";                     // "" = 全会場
var _FL_DAY   = "";                     // "" = 全期間 (累積時のみ)
var _FL_QSEL  = {1:true,2:true,3:true,4:true,5:true};  // Q1-5の表示選択
var _FL_MINODDS = 0;                    // 最低オッズ。これ未満の買い目は買わない
var _FL_WINDOW  = 90;                   // 締切まで何分以内のレースのオッズを取るか
var _FL_BUSY  = false;                  // 更新中フラグ

function openFixedLogic(){
  var ov=document.getElementById("flOverlay");
  if(!ov){
    ov=document.createElement("div");
    ov.id="flOverlay";
    ov.className="bt-overlay";
    document.body.appendChild(ov);
  }
  ov.style.display="block";
  ov.innerHTML=__flFormHtml();
}
function closeFixedLogic(){
  var ov=document.getElementById("flOverlay");
  if(ov) ov.style.display="none";
}
function __flYmd(v){ return (v||"").replace(/-/g,""); }
function __flFormHtml(){
  var di=document.getElementById("dateInput");
  var dv = (di && di.value) ? di.value : "";
  var h='';
  h+='<div class="bt-panel">';
  h+='<div class="bt-head"><span class="bt-ttl">確定ロジック検証</span>';
  h+='<button class="bt-close" onclick="closeFixedLogic()">\u00d7</button></div>';
  h+='<div class="bt-body">';
  h+='<div class="bt-row"><label>対象日</label>';
  h+='<input type="date" id="flDate" value="'+dv+'"></div>';
  h+='<div class="bt-row"><label>人気薄</label>';
  h+='<select id="flFav" onchange="__flRefetch()">';
  h+='<option value="1">共通(rs1が人気1位でないR)</option>';
  h+='<option value="self">各パターンの軸が人気1位でないR</option>';
  h+='<option value="0">フィルタなし(全R)</option>';
  h+='</select></div>';
  h+='<div class="bt-note">オッズは発売中のレースしか取れないため、'
    +'締切が近いレースだけを取りにいく。範囲を広げるほど遅くなる。<br>'
    +'軸4種(rs1=rawscore1位 / rs2=rawscore2位 / 気配1=気配値1位 / 適合1=適合率1位)'
    +'\u00d7 ナガシ順2種(気配順/適合順) = 8パターンの2連単ナガシ。'
    +'拮抗度Q1(拮抗)〜Q5(格差)\u00d71〜6点で的中率・回収率を集計。'
    +'パターンを複数選ぶと「それぞれ別に買った場合」の合算を上に表示する。'
    +'「本日集計＆記録」でオッズ取得(時間がかかる)＋ログ蓄積、'
    +'「累積実績」で貯めた全日を集計。</div>';
  h+='<div class="bt-row"><label>取得範囲</label>';
  h+='<select id="flWindow" onchange="__flOnWindow(this.value)">';
  h+='<option value="30">締切30分前まで(最速)</option>';
  h+='<option value="60">締切60分前まで</option>';
  h+='<option value="90" selected>締切90分前まで</option>';
  h+='<option value="180">締切3時間前まで</option>';
  h+='<option value="0">締切前すべて(遅い)</option>';
  h+='</select></div>';
  h+='<div class="bt-row"><label>最低オッズ</label>';
  h+='<select id="flMinOdds" onchange="__flOnMinOdds(this.value)">';
  h+='<option value="0">制限なし</option>';
  h+='<option value="3">3倍以上</option>';
  h+='<option value="5">5倍以上</option>';
  h+='<option value="8">8倍以上</option>';
  h+='<option value="12">12倍以上</option>';
  h+='<option value="20">20倍以上</option>';
  h+='</select></div>';
  h+='<div class="bt-actions">';
  h+='<button class="bt-run" onclick="runFlRun()">本日集計＆記録</button> ';
  h+='<button class="bt-run bt-refresh" onclick="runFlRefresh()">'
    +'オッズ更新</button> ';
  h+='<button class="bt-run" onclick="runFlStats()">累積実績</button>';
  h+='</div>';
  h+='<div id="flFilters"></div>';
  h+=__flChipsHtml();
  h+='<div id="flProgress" class="bt-progress"></div>';
  h+='<div id="flResult" class="bt-result"></div>';
  h+='</div></div>';
  return h;
}
function __flFav(){ var c=document.getElementById("flFav"); return c?c.value:"1"; }
function __flDateLabel(d){
  if(!d || d.length<8) return d||"";
  return d.slice(0,4)+"-"+d.slice(4,6)+"-"+d.slice(6,8);
}
function __flFiltersHtml(){
  var j=_FL_LAST;
  var h='<div class="bt-row"><label>会場</label>';
  h+='<select id="flVenue" onchange="__flOnVenue(this.value)">';
  h+='<option value="">全会場</option>';
  if(j && j.venues){
    var i=0;
    while(i<j.venues.length){
      var v=j.venues[i];
      var sl=(v.v===_FL_VENUE)?' selected':'';
      h+='<option value="'+v.v+'"'+sl+'>'+v.v+' ('+v.n+'R)</option>';
      i=i+1;
    }
  }
  h+='</select>';
  h+='<button class="fl-jump" onclick="__flJumpDetail()">結果明細へ</button>';
  h+='</div>';
  if(_FL_MODE==="stats"){
    h+='<div class="bt-row"><label>日付</label>';
    h+='<select id="flDay" onchange="__flOnDay(this.value)">';
    h+='<option value="">全期間</option>';
    if(j && j.days){
      var n=0;
      while(n<j.days.length){
        var d=j.days[n];
        var s2=(d.d===_FL_DAY)?' selected':'';
        h+='<option value="'+d.d+'"'+s2+'>'+__flDateLabel(d.d)+' ('+d.n+'R)</option>';
        n=n+1;
      }
    }
    h+='</select></div>';
  }
  return h;
}
function __flSyncFilters(){
  var el=document.getElementById("flFilters");
  if(el) el.innerHTML=__flFiltersHtml();
}
function __flOnVenue(v){ _FL_VENUE=v; __flRefetch(); }
function __flJumpDetail(){
  var el=document.getElementById("flDetail");
  if(el && el.scrollIntoView){ el.scrollIntoView({behavior:"smooth"}); }
}
function __flOnDay(d){ _FL_DAY=d; runFlStats(); }
function __flRowPats(){ return __flSelList(); }
function __flQList(){
  var out=[], q=1;
  while(q<=5){ if(_FL_QSEL[q]) out.push(q); q=q+1; }
  return out;
}
function __flQOn(q){ return !!_FL_QSEL[q]; }
function __flToggleQ(q){
  _FL_QSEL[q] = !_FL_QSEL[q];
  __flSyncChips();
  __flRender();
}
function __flQAll(on){
  var q=1;
  while(q<=5){ _FL_QSEL[q] = !!on; q=q+1; }
  __flSyncChips();
  __flRender();
}
function __flSelList(){
  var out=[], i=0;
  while(i<_FL_PATS.length){
    if(_FL_SEL[_FL_PATS[i]]) out.push(_FL_PATS[i]);
    i=i+1;
  }
  return out;
}
function __flChipsHtml(){
  var h='<div id="flChipsWrap"><div class="fl-chips">';
  var i=0;
  while(i<_FL_PATS.length){
    var pk=_FL_PATS[i];
    var on=_FL_SEL[pk]?" on":"";
    h+='<span class="fl-chip'+on+'" onclick="__flToggle(\''+pk+'\')">'+_FL_LABELS[pk]+'</span>';
    i=i+1;
  }
  h+='<span class="fl-chip alt" onclick="__flSelAll(1)">合計(全8)</span>';
  h+='<span class="fl-chip alt" onclick="__flSelAll(0)">解除</span>';
  h+='</div>';
  h+='<div class="fl-chips fl-qrow">';
  h+='<span class="fl-qlab">拮抗度</span>';
  var q=1;
  while(q<=5){
    var on=__flQOn(q)?" on":"";
    var nm="Q"+q;
    if(q===1) nm="Q1拮抗";
    if(q===5) nm="Q5格差";
    h+='<span class="fl-chip'+on+'" onclick="__flToggleQ('+q+')">'+nm+'</span>';
    q=q+1;
  }
  h+='<span class="fl-chip alt" onclick="__flQAll(1)">全Q</span>';
  h+='<span class="fl-chip alt" onclick="__flQAll(0)">解除</span>';
  h+='</div></div>';
  return h;
}
function __flSyncChips(){
  // ★入れ物ごと差し替える。戦略行だけを置換するとQ行が増殖する。
  var el=document.getElementById("flChipsWrap");
  if(el) el.outerHTML=__flChipsHtml();
}
function __flToggle(pk){
  _FL_SEL[pk] = !_FL_SEL[pk];
  __flSyncChips();
  __flRender();
}
function __flSelAll(on){
  var i=0;
  while(i<_FL_PATS.length){ _FL_SEL[_FL_PATS[i]] = !!on; i=i+1; }
  __flSyncChips();
  __flRender();
}
function __flRefetch(){
  if(_FL_MODE==="stats"){ runFlStats(); }
  else if(_FL_MODE==="run"){ runFlRun(); }
}
function runFlRun(){
  var dEl=document.getElementById("flDate");
  var ds = dEl ? __flYmd(dEl.value) : "";
  var prog=document.getElementById("flProgress");
  var rEl=document.getElementById("flResult");
  if(rEl) rEl.innerHTML="";
  if(prog) prog.textContent="集計中...(オッズ取得に時間がかかります)";
  var url="/api/fl_run?fav="+__flFav()
    +"&window="+_FL_WINDOW
    +"&venue="+encodeURIComponent(_FL_VENUE);
  if(ds){ url=url+"&date="+ds; }
  fetch(url).then(function(r){ return r.json(); }).then(function(j){
    if(prog) prog.textContent="";
    if(!j.ok){ if(rEl) rEl.innerHTML="<p>取得失敗</p>"; return; }
    _FL_LAST=j; _FL_MODE="run";
    __flRender();
  }).catch(function(e){ if(prog) prog.textContent="エラー: "+e; });
}
function __flOnWindow(v){
  _FL_WINDOW = parseInt(v, 10);
  if(isNaN(_FL_WINDOW)) _FL_WINDOW = 90;
}
function __flOnMinOdds(v){
  _FL_MINODDS = parseFloat(v) || 0;
  __flRender();
}
function runFlRefresh(){
  // オッズを取り直して、対象レースと買い目を最新にする。
  // 発売中のレースしかオッズは取れないので、締切前に押すこと。
  if(_FL_BUSY){ return; }
  _FL_BUSY = true;
  var dEl=document.getElementById("flDate");
  var ds = dEl ? __flYmd(dEl.value) : "";
  var prog=document.getElementById("flProgress");
  if(prog) prog.textContent="オッズを取り直しています...(時間がかかります)";
  var url="/api/fl_run?refresh=1&fav="+__flFav()
    +"&window="+_FL_WINDOW
    +"&venue="+encodeURIComponent(_FL_VENUE);
  if(ds){ url=url+"&date="+ds; }
  fetch(url).then(function(r){ return r.json(); }).then(function(j){
    _FL_BUSY=false;
    if(prog) prog.textContent="";
    if(!j.ok){ if(prog) prog.textContent="取得失敗"; return; }
    _FL_LAST=j; _FL_MODE="run";
    __flRender();
  }).catch(function(e){
    _FL_BUSY=false;
    if(prog) prog.textContent="エラー: "+e;
  });
}
function runFlStats(){
  var prog=document.getElementById("flProgress");
  var rEl=document.getElementById("flResult");
  if(rEl) rEl.innerHTML="";
  if(prog) prog.textContent="累積集計中...";
  var url="/api/fl_stats?fav="+__flFav()
    +"&venue="+encodeURIComponent(_FL_VENUE)
    +"&day="+encodeURIComponent(_FL_DAY);
  fetch(url).then(function(r){ return r.json(); }).then(function(j){
    if(prog) prog.textContent="";
    if(!j.ok){ if(rEl) rEl.innerHTML="<p>取得失敗</p>"; return; }
    _FL_LAST=j; _FL_MODE="stats";
    __flRender();
  }).catch(function(e){ if(prog) prog.textContent="エラー: "+e; });
}
function __flFavLabel(m){
  if(m==="0") return " (全レース)";
  if(m==="self") return " (軸が人気1位でないR)";
  return " (人気薄のみ)";
}
function __flRender(){
  var rEl=document.getElementById("flResult");
  if(!rEl) return;
  var j=_FL_LAST;
  if(!j){ rEl.innerHTML=""; return; }
  __flSyncFilters();
  var vl = _FL_VENUE ? (" ["+_FL_VENUE+"]") : "";
  var html="";
  if(_FL_MODE==="run"){
    html+='<div class="fl-sum">'+j.date+vl+' 対象'+j.n_target+'R / 全'+j.n_race+'R'
      +__flFavLabel(j.fav_mode)+'</div>';
  }else if(j.day){
    html+='<div class="fl-sum">'+__flDateLabel(j.day)+vl+' の実績 / '+j.n_race+'R'
      +__flFavLabel(j.fav_mode)+'</div>';
  }else{
    html+='<div class="fl-sum">累積 '+j.n_days+'日 / '+j.n_race+'R'+vl
      +__flFavLabel(j.fav_mode)+'</div>';
  }
  if(_FL_MODE==="run" && j.is_today){
    html+=__flTodayHtml(j);
  }
  html+=__flMatrixHtml(j.matrix);
  html+=__flRacesHtml(j.races);
  rEl.innerHTML=html;
}
function __flCell(cell){
  if(!cell || !cell.tgt){ return '<td class="fl-td fl-na">-</td>'; }
  var hr = (cell.hr!=null)?cell.hr:0;
  var roi = (cell.roi!=null)?cell.roi:0;
  var cls = "fl-td";
  if(roi>=100){ cls="fl-td fl-good"; }
  else if(roi>=90){ cls="fl-td fl-ok"; }
  return '<td class="'+cls+'">'+cell.hit+'/'+cell.tgt
    +'<br>的'+Math.round(hr)+'% 回'+Math.round(roi)+'%</td>';
}
function __flOneTable(qmap, title){
  var qlabels={1:"Q1拮抗",2:"Q2",3:"Q3",4:"Q4",5:"Q5格差"};
  var h='<div class="fl-ttl">'+title+'</div>';
  h+='<div class="fl-scroll"><table class="fl-tbl">';
  h+='<tr class="fl-hd"><th class="fl-td">Q</th>';
  var k=1;
  while(k<=6){ h+='<th class="fl-td">'+k+'点</th>'; k=k+1; }
  h+='</tr>';
  var q=1;
  while(q<=5){
    if(!__flQOn(q)){ q=q+1; continue; }
    h+='<tr><td class="fl-td fl-q">'+qlabels[q]+'</td>';
    var row = qmap ? qmap[q] : null;
    k=1;
    while(k<=6){
      var cell = row ? row[k] : null;
      h+=__flCell(cell);
      k=k+1;
    }
    h+='</tr>';
    q=q+1;
  }
  h+='</table></div>';
  return h;
}
function __flSumQmaps(pat, sel){
  var out={};
  var q=1;
  while(q<=5){
    if(!__flQOn(q)){ q=q+1; continue; }
    out[q]={};
    var k=1;
    while(k<=6){
      var hit=0, tgt=0, pay=0, i=0;
      while(i<sel.length){
        var pm=pat[sel[i]];
        var c=(pm && pm[q]) ? pm[q][k] : null;
        if(c){ hit=hit+c.hit; tgt=tgt+c.tgt; pay=pay+c.pay; }
        i=i+1;
      }
      var stake=k*100*tgt;
      out[q][k]={hit:hit, tgt:tgt, pay:pay,
                 hr: tgt?Math.round(1000*hit/tgt)/10:0,
                 roi: stake?Math.round(1000*pay/stake)/10:0};
      k=k+1;
    }
    q=q+1;
  }
  return out;
}
function __flMatrixHtml(mat){
  if(!mat || !mat.pat){ return '<div class="fl-na2">データなし</div>'; }
  var sel=__flSelList();
  if(!sel.length){ return '<div class="fl-na2">パターンを1つ以上選んでください</div>'; }
  var h='<div style="margin-top:6px">';
  h+='<div class="fl-cap">各セル: 的中R/対象R ・ 的中率 ・ 回収率'
    +'<span class="fl-legend"> 回収100%以上=緑(損益分岐超) / 90%以上=黄</span></div>';
  if(sel.length>1){
    var lbls=[], i=0;
    while(i<sel.length){ lbls.push(_FL_LABELS[sel[i]]); i=i+1; }
    h+=__flOneTable(__flSumQmaps(mat.pat, sel),
                    "◆合算 ("+sel.length+"パターン別々に購入) "+lbls.join(" + "));
  }
  var n=0;
  while(n<sel.length){
    h+=__flOneTable(mat.pat[sel[n]], _FL_LABELS[sel[n]]);
    n=n+1;
  }
  h+='</div>';
  return h;
}
function __flOddsOf(r, axis, bike){
  if(!r || !r.odds) return null;
  var v=r.odds[String(axis)+"-"+String(bike)];
  if(v===undefined || v===null) return null;
  return v;
}
function __flPartnersHtml(axis, list, k, actual2, r){
  if(axis===null || axis===undefined || !list || !list.length){
    return '<span class="fl-na">-</span>';
  }
  var lead="", second="";
  if(actual2 && actual2.indexOf("-")>=0){
    var ps=actual2.split("-"); lead=ps[0]; second=ps[1];
  }
  var leadOk = (String(axis)===String(lead));
  var out="";
  var i=0, hitIdx=-1, nbuy=0;
  while(i<list.length && i<k){
    var b=String(list[i]);
    var od=__flOddsOf(r, axis, b);
    var drop=(_FL_MINODDS>0 && od!==null && od<_FL_MINODDS);
    var cell='<span class="'+(drop?"fl-drop":"fl-buy")+'">'
      +String(axis)+"-"+b;
    if(od!==null){ cell=cell+'<span class="fl-od">'+od+'</span>'; }
    cell=cell+'</span>';
    if(leadOk && b===String(second)){
      hitIdx=i+1;
      cell='<span class="fl-hit">'+String(axis)+"-"+b
        +(od!==null?('<span class="fl-od">'+od+'</span>'):"")+'</span>';
    }
    if(!drop){ nbuy=nbuy+1; }
    out=out+cell+" ";
    i=i+1;
  }
  if(hitIdx>0){ out=out+'<span class="fl-hitn">'+hitIdx+'点目的中</span>'; }
  return out;
}
function __flRaceHits(r, sel){
  // 選択パターンのうち、6点以内で的中しているものがあるか
  var i=0;
  while(i<sel.length){
    var pk=sel[i];
    if(r.elig && r.elig[pk]){
      var ax=r.ax?r.ax[pk.slice(0,pk.length-1)]:null;
      var lst=r.pt?r.pt[pk]:null;
      if(ax!==null && ax!==undefined && lst && r.actual2 && r.actual2.indexOf("-")>=0){
        var ps=r.actual2.split("-");
        if(String(ax)===String(ps[0])){
          var n=0;
          while(n<lst.length && n<6){
            if(String(lst[n])===String(ps[1])) return true;
            n=n+1;
          }
        }
      }
    }
    i=i+1;
  }
  return false;
}
function __flTodayHtml(j){
  // 当日用。締切前のレースを上に、確定済みを下に。買う券をそのまま並べる。
  var sel=__flRowPats();
  if(!sel.length){ return ''; }
  var races=j.races||[];
  if(!races.length){ return '<div class="fl-na2">本日の対象レースはありません</div>'; }
  var wait=[], done=[];
  var i=0;
  while(i<races.length){
    if(races[i].actual2){ done.push(races[i]); } else { wait.push(races[i]); }
    i=i+1;
  }
  wait.sort(function(a,b){
    var x=(a.post||"99:99"), y=(b.post||"99:99");
    if(x<y) return -1;
    if(x>y) return 1;
    return 0;
  });
  var h='<div class="fl-today">';
  h+='<div class="fl-ttl2">本日の買い目 '+j.date
    +' <span class="fl-rowpat">取得 '+(j.now||'')+'</span></div>';
  if(_FL_MINODDS>0){
    h+='<div class="fl-cap">オッズ'+_FL_MINODDS
      +'倍未満は取り消し線。「オッズ更新」で最新に取り直せます。</div>';
  }else{
    h+='<div class="fl-cap">「オッズ更新」で最新オッズを取り直します。'
      +'人気が動くと対象レースも変わります。</div>';
  }
  h+='<div class="fl-sub">未確定 '+wait.length+'R</div>';
  h+=__flTodayRows(wait, sel, true);
  if(done.length){
    h+='<div class="fl-sub">確定済み '+done.length+'R</div>';
    h+=__flTodayRows(done, sel, false);
  }
  h+='</div>';
  return h;
}
function __flTodayRows(list, sel, waiting){
  var h='';
  var i=0;
  while(i<list.length){
    var r=list[i];
    var any=false;
    var s2=0;
    while(s2<sel.length){
      if(!r.elig || r.elig[sel[s2]]){ any=true; }
      s2=s2+1;
    }
    if(!any || !__flQOn(r.q)){ i=i+1; continue; }
    var hitrow = (!waiting && __flRaceHits(r, sel)) ? ' fl-hitrow' : '';
    h+='<div class="fl-card'+hitrow+'">';
    var tm2="";
    if(r.post){
      tm2='<b>発走 '+r.post+'</b> <span class="fl-close">締切 '
        +(r.close||"-")+'</span> ';
    }
    h+='<div class="fl-cardh">'+tm2
      +r.venue+r.rno+'R <span class="fl-q">Q'+r.q+'</span>';
    if(r.closed){ h+=' <span class="fl-closed">締切済</span>'; }
    if(r.fav!==null && r.fav!==undefined){
      h+=' <span class="fl-fav">人気1位 '+r.fav+'</span>';
    }
    if(r.actual2){
      h+=' <span class="fl-res">結果 '+r.actual2
        +(r.payout?(' '+r.payout+'円'):'')+'</span>';
    }
    h+='</div>';
    var s3=0;
    while(s3<sel.length){
      var pk=sel[s3];
      var akey=pk.slice(0,pk.length-1);
      h+='<div class="fl-brow"><span class="fl-blab">'+_FL_LABELS[pk]+'</span>';
      if(r.elig && !r.elig[pk]){
        h+='<span class="fl-na">対象外</span>';
      }else{
        h+='<span class="fl-bet">'
          +__flPartnersHtml(r.ax?r.ax[akey]:null, r.pt?r.pt[pk]:null,
                            6, r.actual2, r)+'</span>';
      }
      h+='</div>';
      s3=s3+1;
    }
    h+='</div>';
    i=i+1;
  }
  if(!h){ h='<div class="fl-na2">該当なし</div>'; }
  return h;
}
function __flRacesHtml(races){
  if(!races || !races.length){ return ''; }
  var sel=__flRowPats();
  if(!sel.length){ return ''; }
  var qs=__flQList();
  if(!qs.length){ return '<div class="fl-na2">拮抗度を1つ以上選んでください</div>'; }
  var h='<div id="flDetail" class="fl-ttl2">結果明細 (6点まで表示・的中は緑)</div>';
  h+='<div class="fl-scroll"><table class="fl-tbl" style="width:100%">';
  h+='<tr class="fl-hd"><th class="fl-td">会場R</th>'
    +'<th class="fl-td">発走/締切</th><th class="fl-td">Q</th>';
  var s=0;
  while(s<sel.length){ h+='<th class="fl-td">'+_FL_LABELS[sel[s]]+'</th>'; s=s+1; }
  h+='<th class="fl-td">結果</th></tr>';
  var i=0;
  while(i<races.length){
    var r=races[i];
    if(!__flQOn(r.q)){ i=i+1; continue; }
    var res="";
    if(r.actual2){
      res=r.actual2+(r.payout?(" ("+r.payout+"円)"):"");
    }else{ res='<span class="fl-na">結果待ち</span>'; }
    var rowcls = __flRaceHits(r, sel) ? ' class="fl-hitrow"' : '';
    var tm=(r.post||"-")+" / "+(r.close||"-");
    h+='<tr'+rowcls+'><td class="fl-td fl-q">'+r.venue+r.rno+'R</td>'
      +'<td class="fl-td fl-tm">'+tm+'</td>'
      +'<td class="fl-td">Q'+r.q+'</td>';
    var s2=0;
    while(s2<sel.length){
      var pk=sel[s2];
      var akey=pk.slice(0,pk.length-1);
      var cellHtml;
      if(r.elig && !r.elig[pk]){
        cellHtml='<span class="fl-na">対象外</span>';
      }else{
        cellHtml=__flPartnersHtml(r.ax?r.ax[akey]:null,
                                  r.pt?r.pt[pk]:null, 6, r.actual2, r);
      }
      h+='<td class="fl-td fl-bet">'+cellHtml+'</td>';
      s2=s2+1;
    }
    h+='<td class="fl-td">'+res+'</td></tr>';
    i=i+1;
  }
  h+='</table></div>';
  return h;
}

// ===== 託宣文 (的中レース選択時、一文字ずつ綴り→ゆっくり消える→次の文) =====
var _ORACLE_OBJ=null;   // {light:[...], dark:[...]} or null
var _ORACLE_LINES=[];
var _ORACLE_IDX=0;
var _ORACLE_TIMER=null;
var _ORACLE_TYPER=null;   // タイプライターの interval
function _oraclePick(){
  if(!_ORACLE_OBJ) return [];
  if(Array.isArray(_ORACLE_OBJ)) return _ORACLE_OBJ;
  var isLight = document.body.classList.contains("light") || document.documentElement.classList.contains("light");
  var arr = isLight ? _ORACLE_OBJ.light : _ORACLE_OBJ.dark;
  return arr || [];
}
// HTML文字列を [タグ片 or 1文字] のトークン列に分解 (色spanを壊さない)
function _oracleTokens(html){
  var toks=[], i=0;
  while(i<html.length){
    if(html[i]==="<"){
      var j=html.indexOf(">", i);
      if(j<0){ toks.push(html.slice(i)); break; }
      toks.push(html.slice(i, j+1));  // タグはまとめて1トークン
      i=j+1;
    }else if(html[i]==="&"){
      var k=html.indexOf(";", i);     // 実体参照は1トークン
      if(k>=0 && k-i<=8){ toks.push(html.slice(i, k+1)); i=k+1; }
      else { toks.push(html[i]); i++; }
    }else{
      toks.push(html[i]); i++;
    }
  }
  return toks;
}
function _clearOracleTimers(){
  if(_ORACLE_TIMER){ clearTimeout(_ORACLE_TIMER); _ORACLE_TIMER=null; }
  if(_ORACLE_TYPER){ clearInterval(_ORACLE_TYPER); _ORACLE_TYPER=null; }
}
// 一文字ずつ綴る。綴り終えたら done() を呼ぶ
function typeOracle(t, html, done){
  if(_ORACLE_TYPER){ clearInterval(_ORACLE_TYPER); _ORACLE_TYPER=null; }
  var toks=_oracleTokens(html);
  var built="", visibleCount=0, n=0;
  // 表示中の文字数を数える(タグ以外)
  for(var x=0;x<toks.length;x++){ if(toks[x][0] !== "<") visibleCount++; }
  t.innerHTML="";
  t.classList.add("show");   // タイプ中は表示状態
  _ORACLE_TYPER=setInterval(function(){
    if(n>=toks.length){
      clearInterval(_ORACLE_TYPER); _ORACLE_TYPER=null;
      if(done) done();
      return;
    }
    // タグは一気に、文字は1つずつ
    built+=toks[n]; n++;
    while(n<toks.length && toks[n][0]==="<"){ built+=toks[n]; n++; }
    t.innerHTML=built;
  }, 85);  // 一文字あたりの速度(ms)
}
function setOracle(obj){
  var bar=document.getElementById("oracleBar");
  _clearOracleTimers();
  if(!obj || (Array.isArray(obj) && obj.length===0) ||
     (!Array.isArray(obj) && (!obj.light || obj.light.length===0) && (!obj.dark || obj.dark.length===0))){
    _ORACLE_OBJ=null; _ORACLE_LINES=[]; _ORACLE_IDX=0;
    if(bar){ bar.className="oracle-bar"; bar.innerHTML=""; }
    document.body.classList.remove("oracle-on");
    return;
  }
  _ORACLE_OBJ = obj;
  _ORACLE_LINES = _oraclePick();
  _ORACLE_IDX = 0;
  if(!bar) return;
  bar.className="oracle-bar on";
  document.body.classList.add("oracle-on");
  bar.innerHTML='<div class="oracle-txt" id="oracleTxt"></div>';
  var t=document.getElementById("oracleTxt");
  if(t && _ORACLE_LINES.length){
    typeOracle(t, _ORACLE_LINES[0], scheduleNextOracle);
  }
}
// 綴り終えた後、表示を保ってから次へ
function scheduleNextOracle(){
  if(_ORACLE_LINES.length<=1) return;
  if(_ORACLE_IDX >= _ORACLE_LINES.length - 1) return;  // 最後なら停止
  _ORACLE_TIMER=setTimeout(nextOracleLine, 3200);  // 読む余韻
}
function refreshOracleForTheme(){
  if(!_ORACLE_OBJ) return;
  var bar=document.getElementById("oracleBar");
  if(!bar || !bar.classList.contains("on")) return;
  _clearOracleTimers();
  _ORACLE_LINES = _oraclePick();
  _ORACLE_IDX = 0;
  var t=document.getElementById("oracleTxt");
  if(t){
    t.classList.remove("show");
    setTimeout(function(){
      if(_ORACLE_LINES.length) typeOracle(t, _ORACLE_LINES[0], scheduleNextOracle);
    }, 450);
  }
}
function nextOracleLine(){
  var t=document.getElementById("oracleTxt");
  if(!t || !_ORACLE_LINES.length) return;
  if(_ORACLE_IDX >= _ORACLE_LINES.length - 1){ _ORACLE_TIMER=null; return; }
  t.classList.remove("show");   // ゆっくりフェードアウト
  setTimeout(function(){
    _ORACLE_IDX = _ORACLE_IDX + 1;
    typeOracle(t, _ORACLE_LINES[_ORACLE_IDX], scheduleNextOracle);  // 次を一文字ずつ
  }, 1500);
}

function ymdToday(){
  var d=new Date();
  var m=("0"+(d.getMonth()+1)).slice(-2);
  var dd=("0"+d.getDate()).slice(-2);
  return ""+d.getFullYear()+m+dd;
}

// "2026-05-29" → "20260529"
function isoToYmd(iso){ return iso ? iso.replace(/-/g,"") : ""; }
// "20260529" → "2026-05-29"
function ymdToIso(ymd){
  if(!ymd || ymd.length!==8) return "";
  return ymd.slice(0,4)+"-"+ymd.slice(4,6)+"-"+ymd.slice(6,8);
}
function ymdOffset(days){
  var d=new Date(); d.setDate(d.getDate()+days);
  var m=("0"+(d.getMonth()+1)).slice(-2);
  var dd=("0"+d.getDate()).slice(-2);
  return ""+d.getFullYear()+m+dd;
}

function _pick(arr){ return arr[Math.floor(Math.random()*arr.length)]; }
// 全会場スクレイピング中(重い処理)のメッセージ
function scrapeMsg(){
  return _pick([
    '只今、託宣を受けています…。',
    '神が託宣のためにスクレイピング中…。',
    '神が託宣を書き起こしています…。',
    '神が夢のお告げを書き起こしています…。'
  ]);
}
// ライン情報が無いレースのライン取得時のメッセージ
function lineMsg(name){
  return _pick([
    '神が託宣のためにライン情報を確認中…。',
    '神が託宣のために布陣を確認中…。',
    '神が託宣のために隊列を確認中…。'
  ]);
}

function stopLoadingUI(){
  // 読み込み中の演出(マリア・大文字・発光・点滅・スピナー判定)を確実に解除
  document.body.classList.remove("loading");
  document.body.classList.remove("venue-loading");
  document.body.classList.remove("oracle-loading");
  document.body.classList.remove("btn-glow");
  try{ stopFakeProgress(); }catch(e){}
  try{ setLogoThinking(false); }catch(e){}
}

function setStatus(html){
  var on = html.indexOf("spin")>=0;
  // 託宣ボタン押下時の読み込み中は「託宣とは」の文章のみ表示(他メッセージは出さない)
  if(on && document.body.classList.contains("oracle-loading")){
    // v330: 読み込み中は何度も setStatus が呼ばれる。
    //   すでに語りが流れていれば作り直さない (作り直すと最初に戻ってしまう)。
    var _st = document.getElementById("status");
    if(!_st.querySelector(".oracle-meaning")){
      _st.innerHTML = '<div class="oracle-meaning">'
        + '<span class="w2"><span class="gw">託宣</span>とは——。</span>'
        + '<span class="mtxt">神仏が人に乗り移り、また夢に現れて、'
        + 'その意志を告げ知らせること。</span>'
        + '<div class="maria"></div></div>';
      typeOracle(_st.querySelector(".oracle-meaning"));
    }
    setLoading(on);
    return;
  }
  document.getElementById("status").innerHTML = html;
  setLoading(on);
}
function setLoading(on){
  document.body.classList.toggle("loading", !!on);
  // v330: 読み込みが終われば語りも止める (止めないと消えた後もタイマーが回る)
  if(!on){ try{ stopMaria(); }catch(e){} }
  // 読み込みが終わったら託宣ボタン発光・聖母マリア表示も止める
  if(!on){
    document.body.classList.remove("oracle-loading");
    document.body.classList.remove("venue-loading");
    document.body.classList.remove("btn-glow");
  }
}

// ============================================================
// v329: 画面が白くなったとき、原因が分からないままになるのを防ぐ。
//   スマホでは開発者ツールが開けないので、JSの例外を画面に出す。
//   これが出たら、その文言をそのまま報告すれば原因が特定できる。
// ============================================================
(function(){
  function showErr(msg){
    try{
      var b=document.getElementById("jsErrBar");
      if(!b){
        b=document.createElement("div");
        b.id="jsErrBar";
        b.style.cssText="position:fixed;left:0;right:0;bottom:0;z-index:9999;"
          +"background:#3a1414;color:#ffcfc4;font-size:11px;padding:8px 10px;"
          +"line-height:1.6;max-height:38vh;overflow:auto;"
          +"border-top:1px solid #a04040";
        b.onclick=function(){ b.style.display="none"; };
        document.body.appendChild(b);
      }
      b.style.display="block";
      b.innerHTML += "<div>"+String(msg).substring(0,300)+"</div>";
    }catch(e){}
  }
  window.addEventListener("error", function(ev){
    showErr("JSエラー: "+(ev.message||"")+" @"+(ev.lineno||"?"));
  });
  window.addEventListener("unhandledrejection", function(ev){
    showErr("通信エラー: "+String((ev.reason&&ev.reason.message)||ev.reason||""));
  });
  window.__showErr=showErr;
})();

function loadVenues(){
  document.body.classList.remove("intro");  // 起動初期画面を閉じる
  var iso=document.getElementById("dateInput").value;
  DATE = isoToYmd(iso) || ymdToday();
  document.getElementById("dateInput").value = ymdToIso(DATE);
  // v329: 日付が変わったら前の日の集計・判定を全部捨てる。
  //   捨てないと 8/15 の集計が 8/13 に出てしまう。
  _FEED_ROWS=[]; _FEED_DONE=false;   // v340: 日付が変わったら集計もやり直す
  _QF_CACHE={}; _VF_CACHE={}; _PAY_CACHE={};
  _YOSOU_HITS={}; _DISPLAYABLE={}; _RESULTMAP={}; _YHIT_CHECKED={};
  document.getElementById("venueStrip").innerHTML="";
  // 託宣ボタン: 段階表示を始め、待ち時間に語りを流す
  beginProgress("takusen");
  document.getElementById("raceGrid").innerHTML="";
  ensureDetailEl().innerHTML="";
  var ob=document.getElementById("oracleBar"); if(ob){ ob.innerHTML=""; ob.className="oracle-bar"; }
  setOracle(null);
  document.body.classList.add("oracle-loading");  // 託宣ボタン起点(メッセージ=託宣とは)
  document.body.classList.add("btn-glow");         // 託宣ボタンの文字を発光
  document.body.classList.add("venue-loading");    // 聖母マリア・大託宣文字を表示
  setStatus('<span class="spin"></span>神々の社を巡っています…');
  // v329: 託宣ボタンは会場一覧を読むだけにした。
  //   従来は build_cache (GitHub同期+スクレイピング) を毎回走らせていたため、
  //   会場を見たいだけでも長く待たされていた。
  //   同期は「同期」ボタン(syncThenLoadVenues)に分けてある。
  fetchVenuesOnly();
}

function syncThenLoadVenues(){
  // build_cache を先に1回呼んで sync_db_months を必ず実行する。
  // 同期結果は診断ログに記録。完了後に会場リストを取得して表示。
  fetch("/api/build_cache?date="+DATE)
    .then(function(r){return r.json()})
    .then(function(res){
      try{
        if(typeof res.db_sync!=='undefined'){
          var msg=String(res.db_sync||'(空)');
          if(msg.indexOf('エラー')>=0 || msg.indexOf('失敗')>=0){
            logError('DB同期エラー', '日付='+DATE+'\n'+msg);
          }else{
            logError('DB同期', '日付='+DATE+'\n結果: '+msg);
          }
        }else{
          logError('DB同期', '日付='+DATE+'\nレスポンスに db_sync が含まれていません');
        }
      }catch(e){}
      // build_cache 後に会場リストを取得 (afterBuild=true で空なら開催なし扱い)
      fetchVenuesOnce(true);
    })
    .catch(function(e){
      try{ logError('DB同期エラー', '日付='+DATE+'\nbuild_cache通信失敗: '+e); }catch(_e){}
      // 同期に失敗しても会場読み込みは続行 (従来動作にフォールバック)
      fetchVenuesOnce(false);
    });
}

// v329: 同期をせずに会場一覧だけを取る。託宣ボタンはこれを使う。
//   キャッシュが無い日だけ build_cache に落ちる(初回のみ)。
function fetchVenuesOnly(){
  fetchVenuesOnce(false);
}

function fetchVenuesOnce(afterBuild){
  fetch("/api/venues?date="+DATE)
    .then(function(r){return r.json()})
    .then(function(j){
      var venues = j.venues || [];
      if(!venues.length){
        if(afterBuild){
          // 生成後も無ければ開催なし(読み込み表示を解除してメッセージ表示)
          stopLoadingUI();
          setStatus(j.message ? j.message : "開催レースなし");
          return;
        }
        // キャッシュが無い → 当日分を取得(スクレイピング)してから再取得
        buildCacheThenReload();
        return;
      }
      stepProgress();      // 1/4 出典を紡ぐ 完了
      VENUES = venues;
      if(j && j.bank_avg) BANK_AVG = j.bank_avg;
      renderVenues();
      stepProgress();      // 2/4 盤面を検める 完了
      // v329: 自動選択しない。上位ボタンが下位の読み込みを始めない設計。
      //   会場を押した時にだけ、その会場のRを描く。
      stopLoadingUI();
      setStatus("");
      // 3/4 兆しを量る: 買い目と荒れ期待度を先に取る
      loadPicks(function(){
        stepProgress();
        fetchVenueMarks();
        finishProgress();  // 4/4 卓に並べる
      });
      // v329: 背景集計はやめた。
      //   裏で /api/venue_flags と /api/race を回し続けると
      //   アプリが処理しきれず、会場を押しても何も出なくなった。
      //   集計はボタンを押したときだけ行う。
    })
    .catch(function(e){
      stopLoadingUI();
      setStatus("読み込みエラー: "+e);
      if(window.__showErr) window.__showErr("会場取得に失敗: "+e);
    });
}

function buildCacheThenReload(){
  setStatus('<span class="spin"></span>神々の社を巡っています…');
  fetch("/api/build_cache?date="+DATE)
    .then(function(r){return r.json()})
    .then(function(res){
      // DB同期結果を診断ログに記録 (自動同期が効いているか確認用)
      try{
        if(typeof res.db_sync!=='undefined'){
          var msg=String(res.db_sync||'(空)');
          // エラー/失敗を含むメッセージは目立つように記録、それ以外も記録して可視化
          if(msg.indexOf('エラー')>=0 || msg.indexOf('失敗')>=0){
            logError('DB同期エラー', '日付='+DATE+'\n'+msg);
          }else{
            logError('DB同期', '日付='+DATE+'\n結果: '+msg);
          }
        }else{
          logError('DB同期', '日付='+DATE+'\nレスポンスに db_sync が含まれていません');
        }
      }catch(e){}
      if(res.built && res.races){
        setStatus('<span class="spin"></span>神々の社を巡っています…');
        fetchVenuesOnce(true);
      }else if(res.races){
        // 既存キャッシュあり等(built=false でも races がある) → 正常としてそのまま表示
        fetchVenuesOnce(true);
      }else{
        stopLoadingUI();
        setStatus(res.error ? ("取得失敗: "+res.error) : "開催レースなし（メンテナンス中の可能性があります）");
      }
    })
    .catch(function(e){ stopLoadingUI(); setStatus("取得エラー: "+e+"（メンテナンス中の可能性があります）"); });
}

// v329: #detail はRボタンの下に差し込む作りにしたので、
//   raceGrid を描き直すと巻き込まれて消えることがあった。
//   ここで必ず「grid-wrap の直下」に戻し、無ければ作り直す。
//   これを入れる前は、会場を切り替えた瞬間に detail が null になり、
//   以降すべての操作が落ちていた。
function ensureDetailEl(){
  // 無ければ作るだけ。位置は動かさない。
  //   毎回「元の位置へ戻す」ようにしていたため、Rボタンの下に差し込んでも
  //   renderDetail の冒頭で末尾へ戻されていた。
  var d=document.getElementById("detail");
  if(d) return d;
  var g=document.getElementById("raceGrid");
  var wrap=(g && g.parentNode) ? g.parentNode : document.body;
  d=document.createElement("div");
  d.id="detail";
  wrap.appendChild(d);
  return d;
}

// 詳細をレース一覧の外(元の位置)へ戻す。会場切替と集計表示のときだけ使う。
function detachDetail(){
  var d=ensureDetailEl();
  var g=document.getElementById("raceGrid");
  var wrap=(g && g.parentNode) ? g.parentNode : document.body;
  if(d.parentNode!==wrap){ wrap.appendChild(d); }
  return d;
}

function renderVenues(){
  try{ renderVenuesInner(); }
  catch(e){
    if(window.__showErr) window.__showErr("会場ボタンの描画に失敗: "+e);
  }
}

// v330: 会場ボタンにバンク要目を出す。
//   周長・カント・直線長を、全国平均を添えて並べる。
//   平均より大きいか小さいかが一目で分かれば、脚質の効き方を読める。
//   (旧表示の「発走時刻 / 開催R数」はここで廃止した)
var BANK_AVG = {};

function _bkRow(lab, val, avg, unit){
  // v333: 値が無いときも行は残す。行ごと消すと「何が欠けたか」が見えなくなる。
  if(val === null || typeof val === "undefined"){
    return '<span class="bk-r"><em class="bk-l">'+lab+'</em>'
         + '<b class="bk-v bk-na">—</b></span>';
  }
  var a = (avg === null || typeof avg === "undefined") ? "" :
          '<i class="bk-a">Ave.'+avg+'</i>';
  var cls = "bk-v";
  if(avg !== null && typeof avg !== "undefined"){
    if(val > avg) cls += " hi";
    else if(val < avg) cls += " lo";
  }
  return '<span class="bk-r"><em class="bk-l">'+lab+'</em>'
       + '<b class="'+cls+'">'+val+unit+'</b>'+a+'</span>';
}

function renderVenuesInner(){
  var s=document.getElementById("venueStrip");
  var html="";
  for(var i=0;i<VENUES.length;i++){
    var v=VENUES[i];
    var dly=" d"+((i%8)+1);
    var b=v.bank||{};
    // v331: 周長は 400/500 の二択なので Ave. を出さない (比較に意味がない)
    var bk='<div class="bk">'
      + _bkRow("周長", b.circ,     null,              "m")
      + _bkRow("カント", b.cant,   BANK_AVG.cant,     "\u00B0")
      + _bkRow("直線", b.straight, BANK_AVG.straight, "m")
      + '</div>';
    // v335: 会場名の隣に第1レースの発走時刻を添える
    var fp = String(v.first_post||"").trim();
    var fpTxt = (!fp || fp.indexOf("-")>=0) ? "" : (fp+"〜");
    html+='<div class="vchip anim'+dly+'" data-i="'+i+'" data-v="'+esc(v.name)+'"'
        + ' onclick="selectVenue('+i+')">'
        + '<span class="vhead"><span class="nm">'+v.name+'</span>'
        +   '<span class="vfp">'+fpTxt+'</span></span>'
        + bk
        + '<span class="vticker" id="vt_'+i+'"><i></i></span>'
        + '</div>';
  }
  // v329: 会場ボタンの最後に「本日の集計」を同じ形で置く。
  //   押したときに初めて全会場を調べる (上位ボタンは何も読まない方針を守る)。
  // v336: 「本日の集計」は会場ボタンから外し、メニューへ移した。
  //   会場と並ぶと会場の一つに見えてしまうため。
  s.innerHTML=html;
}

// ============================================================
// v329: 本日の集計
//   稼働条件に該当したレースだけを全会場から集め、
//   買い目と結果を突き合わせて回収率まで出す。
//   予想の無いレースは計算しないので、全レースを回すより軽い。
// ============================================================
// ============================================================
// v329: 本日の集計を裏で先に作っておく。
//   託宣を押した直後から少しずつ進め、押されたら即座に出す。
//   まだ途中なら、進捗を出しつつ続きを待つ。
// ============================================================
// 利用者が操作している間は背景集計を止める。
//   止めないと /api/race と /api/venue_flags が裏で走り続け、
//   アプリが応答しなくなる (会場を押しても何も出ない状態になった)。
var _BUSY_UNTIL=0;
function markBusy(ms){ _BUSY_UNTIL = Date.now() + (ms||6000); }
function isBusy(){ return Date.now() < _BUSY_UNTIL; }
function waitIdle(cb){
  // v340: 旧集計の中断フラグ(_SUM_ABORT)を見ていたが、その集計を廃止したので外した
  if(!isBusy()){ setTimeout(cb, 250); return; }
  setTimeout(function(){ waitIdle(cb); }, 800);
}

// v340: 旧「本日の集計」(startDaySummaryBg / showDaySummary /
//   collectDaySummary / drawSummary) は廃止した。
//   メニューの「本日の的中集計」と役割が重なり、両方走ると
//   /api/race と /api/venue_flags を裏で二重に叩いてしまうため。
//   集計はメニュー内の menuHitTally() に一本化する。

function selectVenue(i, auto){
  markBusy(8000);   // 背景集計を止めて、会場の読み込みを最優先にする
  CUR_VENUE=i;
  setOracle(null);   // 会場切替時は前レースの託宣を消す
  var chips=document.querySelectorAll(".vchip");
  for(var k=0;k<chips.length;k++){
    var isAct=(parseInt(chips[k].dataset.i)===i);
    chips[k].classList.toggle("active", isAct);
  }
  // ★順序が重要: 先に detail を grid の外へ戻してから grid を描き直す。
  //   逆にすると innerHTML の書き換えで detail ごと消える。
  var dEl=detachDetail();
  dEl.innerHTML = "";
  renderRaceGrid(i);
  var v=VENUES[i];
  // 託宣ボタン起点の自動選択(auto)なら、託宣ボタンの読み込み中表示を維持する
  // v329: 印・結果・予想をまとめて1回で取る。
  //   /api/venue_flags は全レースで予想計算を走らせるので使わない。
  //   重い判定は「買う候補のレース」だけに絞ってある。
  fetchVFlags(v.name);

  // ラインが空のRがある場合だけ、裏で取り直す。
  //   完了しても画面は作り直さない (押した後に反映される)。
  var hasEmpty=false;
  for(var j=0;j<v.races.length;j++){ if(!v.races[j].has_line){ hasEmpty=true; break; } }
  if(hasEmpty){
    fetch("/api/refetch_lines?date="+DATE+"&venue="+encodeURIComponent(v.name))
      .then(function(r){return r.json()})
      .then(function(res){
        if(res.updated && res.updated>0 && CUR_VENUE===i){
          // 取れたぶんだけ静かに反映する
          fetch("/api/venues?date="+DATE)
            .then(function(r2){return r2.json()})
            .then(function(j2){
              if(!j2.venues || CUR_VENUE!==i) return;
              VENUES=j2.venues;
              if(j2.bank_avg) BANK_AVG=j2.bank_avg;
              renderRaceGrid(i);
              fetchVenueFlags(VENUES[i].name);
            })
            .catch(function(){});
        }
      })
      .catch(function(){});
  }
}

// v330: グレードマークと種別。
//   グレードは色で見分ける (G1赤 / G2紫 / G3緑 / F1青 / F2灰)。
//   種別は「A級一般」「S級準決勝」などをそのまま短く出す。
function __gradeHtml(r){
  var g=String(r.grade||"").replace(/[Ｇｇ]/g,"G").replace(/[Ｆｆ]/g,"F")
        .replace(/[０-９]/g,function(c){return String.fromCharCode(c.charCodeAt(0)-65248);})
        .toUpperCase().replace(/\s/g,"");
  var kind=String(r.race_kind||"");
  if(!g && !kind) return '<div class="rgd"></div>';
  var cls="g-other";
  if(g.indexOf("G1")>=0||g.indexOf("GI")===0) cls="g-g1";
  else if(g.indexOf("G2")>=0) cls="g-g2";
  else if(g.indexOf("G3")>=0) cls="g-g3";
  else if(g.indexOf("F1")>=0) cls="g-f1";
  else if(g.indexOf("F2")>=0) cls="g-f2";
  var h='<div class="rgd">';
  if(g) h+='<span class="gmark '+cls+'">'+esc(g)+'</span>';
  if(kind) h+='<span class="gkind">'+esc(kind)+'</span>';
  h+='</div>';
  return h;
}

function renderRaceGrid(i){
  setOracle(null);   // レース未選択状態では託宣を出さない
  var v=VENUES[i];
  var g=document.getElementById("raceGrid");
  var html="";
  for(var j=0;j<v.races.length;j++){
    var r=v.races[j];
    var cls="rcell checking anim"+(r.has_line?"":" noline");
    // v329: 1レース1行の横長。左からR番号/時刻/印/予想/結果。
    //   rtop に印、rbot に結果が後から入る(fetchVenueFlags)。
    //   rpred は そのレースを開いて計算したときにだけ埋まる。
    // v335: 3段構成。
    //   1段目 R番号 / 発走 / グレード / 種別 / 印(穴弱離) / 狙い印 …右端に 終了
    //   2段目 荒れ期待度バー
    //   3段目 的中の流れる表示だけ (的中が無ければ段ごと出さない)
    html+='<div class="'+cls+'" id="rc_'+cssId(r.key)+'" data-key="'+r.key+'" onclick="tapRace(\''+r.key+'\',this)">'
        + '<div class="rline1">'
        +   '<div class="rno"><span class="rnum">'+r.race_no+'</span>R</div>'
        +   '<div class="rt">'+r.post_time+'</div>'
        +   __gradeHtml(r)
        +   '<div class="rtop"></div>'
        +   '<div class="rpred" id="rp_'+cssId(r.key)+'"></div>'
        +   '<div class="rfin"></div>'
        + '</div>'
        + '<div class="rline2">'
        +   '<div class="rbar" id="rb_'+cssId(r.key)+'"><i class="lv0" style="width:0%"></i></div>'
        +   '<div class="rbarlab" id="rl_'+cssId(r.key)+'"></div>'
        + '</div>'
        + '<div class="rline3"><div class="rbot"></div></div>'
        + '</div>';
  }
  g.innerHTML=html;
}

function refreshVenuesThen(i){
  // /api/venues を取り直して該当会場を再描画
  fetch("/api/venues?date="+DATE)
    .then(function(r){return r.json()})
    .then(function(j){
      VENUES = j.venues || VENUES;
      if(j.bank_avg) BANK_AVG = j.bank_avg;
      renderRaceGrid(i);
      fetchVenueFlags(VENUES[i].name);
    })
    .catch(function(){ fetchVenueFlags(VENUES[i].name); });
}

// v330: 会場名(日本語)を "_" に潰すと 岐阜_1 も 防府_1 も "___1" になり、
//   別の会場のRボタンを同じIDで指してしまう。
//   実際、ある会場の的中が別の会場に出る不具合が起きた。
//   日本語をコード番号に置き換えて、会場ごとに違うIDにする。
function cssId(key){
  var s = String(key);
  var out = "";
  var i = 0;
  while (i < s.length) {
    var c = s.charAt(i);
    if (/[A-Za-z0-9_]/.test(c)) { out += c; }
    else { out += "u" + s.charCodeAt(i).toString(36); }
    i = i + 1;
  }
  return out;
}

// v329: 稼働条件に該当するレースだけ、Rボタンを託宣カラーにする。
//   拮抗度Qは出走表の競走得点と直近着順から出せるので、
//   予想計算(重い処理)を呼ばずに会場ぶんまとめて判定できる。
var _YOSOU_HITS={};      // key -> {q,line,hits[]}
var _DISPLAYABLE={};     // key -> 計算可能か (venue_flags 由来)
var _RESULTMAP={};       // key -> venue_flags のレース情報
var _QF_DONE=false;      // qflags が届いたか
var _YHIT_CHECKED={};    // 予想的中の判定済み

// v329: 予想があるレースを含む会場は、会場ボタンの左端を託宣カラーにする。
//   拮抗度Qと分戦だけで判定できるので、予想計算を呼ばずに全会場ぶん出せる。
function fetchVenueMarks(){
  fetch("/race/venue_marks?date="+DATE)
    .then(function(r){return r.json()})
    .then(function(j){
      var vs=j.venues||{};
      var chips=document.querySelectorAll(".vchip");
      for(var k=0;k<chips.length;k++){
        var idx=parseInt(chips[k].dataset.i);
        if(isNaN(idx) || idx<0) continue;
        var nm=(VENUES[idx]||{}).name||"";
        if(vs[nm]){ chips[k].classList.add("has-yosou"); }
        else { chips[k].classList.remove("has-yosou"); }
      }
    })
    .catch(function(){});
  markTargetVenues();
}

// v330: 狙いレースを含む会場は、左端を狙い色にする。
//   picks は会場_R番号 の鍵なので、会場名だけ取り出して突き合わせる。
function markTargetVenues(){
  loadPicks(function(){
    var has={};   // 会場名 -> 狙いレース本数
    var nums={};  // 会場名 -> R番号の配列
    for(var key in _PICKS){
      var p=_PICKS[key];
      if(!p || !p.target) continue;
      var parts=String(key).split("_");
      var vn=parts[0];
      has[vn]=(has[vn]||0)+1;
      if(!nums[vn]) nums[vn]=[];
      var rno=parseInt(parts[1]);
      if(!isNaN(rno)) nums[vn].push(rno);
    }
    var chips=document.querySelectorAll(".vchip");
    for(var k=0;k<chips.length;k++){
      var idx=parseInt(chips[k].dataset.i);
      if(isNaN(idx) || idx<0) continue;
      var nm=(VENUES[idx]||{}).name||"";
      if(has[nm]) chips[k].classList.add("has-target");
      else chips[k].classList.remove("has-target");
      // v331: ボタン下部に流れる報せ。狙いレースがあれば本数と R番号を出す。
      var tk=chips[k].querySelector(".vticker i");
      if(tk){
        var n=(has[nm]||0);
        // v333: R番号を並べると会場ボタンからはみ出すので、有無だけを示す。
        // v337: 「なし」は選択しても灯さない。Ave. と同じ扱いにする。
        if(n>0){
          tk.textContent = "狙いレース有";
          tk.parentNode.classList.remove("none");
        }else{
          tk.textContent = "狙いレースなし";
          tk.parentNode.classList.add("none");
        }
      }
    }
  });
}

var _VF_CACHE={};   // 会場ごとの表示情報。戻ったときは通信ゼロ。

// 会場ぶんの表示情報を1回で取り、届いた順に描く。
function fetchVFlags(venueName){
  var ck=DATE+"|"+venueName;
  if(_VF_CACHE[ck]){ applyVFlags(_VF_CACHE[ck]); finishProgress(); return; }
  beginProgress("venue");
  fetch("/race/vflags?date="+DATE+"&venue="+encodeURIComponent(venueName))
    .then(function(r){return r.json()})
    .then(function(j){
      stepProgress();          // 1/4 出典を紡ぐ 完了
      var fl=j.flags||{};
      _VF_CACHE[ck]=fl;
      applyVFlags(fl);
      finishProgress();        // 4/4 卓に並べる
    })
    .catch(function(e){
      if(window.__showErr) window.__showErr("会場情報の取得に失敗: "+e);
    });
}

function applyVFlags(fl){
  for(var key in fl){
    var f=fl[key];
    _YOSOU_HITS[key]=f;
    _RESULTMAP[key]=f;
    _DISPLAYABLE[key]=(f.displayable===true);
    var el=document.getElementById("rc_"+cssId(key));
    if(!el) continue;
    el.classList.remove("checking");

    // 印 (2/4 影を数える)
    var labs=f.labels||{};
    var rtop=el.querySelector(".rtop");
    if(rtop){
      var tags="";
      if(labs.ana) tags+='<span class="rtag ana">穴</span>';
      if(labs.weak) tags+='<span class="rtag weak">弱</span>';
      if(labs.layoff) tags+='<span class="rtag layoff">離</span>';
      rtop.innerHTML=tags;
    }
    // v330: 予想の表示は paintYosou (picks) が受け持つ。
    //   ここで書くと旧ロジックの「予想N点」が上書きしてしまう。
    // 結果
    // v335: 終了は1段目の右端。3段目は的中専用にした。
    var rfin=el.querySelector(".rfin");
    if(rfin && f.finished===true){
      rfin.innerHTML='<span class="rt finmk">終了</span>';
    }
  }
  stepProgress();   // 2/4 影を数える 完了
  stepProgress();   // 3/4 結末を照らす 完了
  // 予想と荒れ期待度は picks が受け持つ。結果を描いた後に重ねる。
  paintYosou();
}

// v330: 買い目は GitHub Actions が作ったものを読むだけ。
//   ★の計算も条件判定もアプリでは行わない。
//   端末の辞書に依存しないので、誰が起動しても同じ結果になる。
var _PICKS={};        // key -> 買い目
var _PICKS_META={};
var _PICKS_DATE="";
var _PICKS_LOADING=false;

function loadPicks(cb){
  if(_PICKS_DATE===DATE){ if(cb) cb(); return; }
  if(_PICKS_LOADING){ if(cb) setTimeout(function(){ loadPicks(cb); },300); return; }
  _PICKS_LOADING=true;
  fetch("/race/picks?date="+DATE)
    .then(function(r){return r.json()})
    .then(function(j){
      _PICKS=j.picks||{};
      _PICKS_META=j.meta||{};
      _PICKS_DATE=DATE;
      _PICKS_LOADING=false;
      if(cb) cb();
    })
    .catch(function(){
      _PICKS={}; _PICKS_META={}; _PICKS_DATE=DATE; _PICKS_LOADING=false;
      if(cb) cb();
    });
}

var _QF_CACHE={};   // 会場ごとに保持。戻ったときは即座に出す。
function fetchYosouFlags(venueName){
  var ck=DATE+"|"+venueName;
  if(_QF_CACHE[ck]){
    var c=_QF_CACHE[ck];
    for(var k0 in c){ _YOSOU_HITS[k0]=c[k0]; }
    _QF_DONE=true;
    paintYosou();
    return;
  }
  _QF_DONE=false;
  fetch("/race/qflags?date="+DATE+"&venue="+encodeURIComponent(venueName))
    .then(function(r){return r.json()})
    .then(function(j){
      var fl=j.flags||{};
      _QF_CACHE[ck]=fl;
      for(var key in fl){ _YOSOU_HITS[key]=fl[key]; }
      _QF_DONE=true;
      paintYosou();
    })
    .catch(function(){ _QF_DONE=true; });
}

// 予想欄の描画。
//   qflags(Q・分戦の該当) と venue_flags(計算可能か) の両方が要る。
//   条件に該当しても計算できないレース(9車立て・風判定不可・該当セル無し)は
//   買い目が作れないので「予想あり」にしてはいけない。
//   この突き合わせを入れる前は、予想ありと出るのにページが出ない矛盾が起きていた。
function paintYosou(){
  // v330: picks に載っているレースだけが「買うレース」。
  //   アプリ側では条件判定をしないので _QF_DONE は待たない。
  loadPicks(function(){
    var cells=document.querySelectorAll(".rcell");
    for(var i=0;i<cells.length;i++){
      var el=cells[i];
      var key=el.dataset.key;
      if(!key) continue;
      var box=document.getElementById("rp_"+cssId(key));
      if(!box) continue;
      var p=_PICKS[key];
      var bar=document.getElementById("rb_"+cssId(key));
      var lab=document.getElementById("rl_"+cssId(key));
      if(!p){
        // 買い目が無い理由を出す。ただ「見送り」だと原因が分からない。
        el.classList.remove("target");
        var why="";
        if(_PICKS_DATE===DATE){
          var sk=_PICKS_META.skips||{};
          why = sk[key] || "対象外";
        }
        box.innerHTML = why ? '<span class="no">'+esc(why)+'</span>' : "";
        // 対象外はメーター自体を消す (0%のバーが残ると紛らわしい)
        var l2=el.querySelector(".rline2");
        if(l2) l2.style.display="none";
        continue;
      }
      // 狙いレースは枠と見出しで示す。点数表記はしない。
      if(p.target){
        el.classList.add("target");
        box.innerHTML="";
      }else{
        el.classList.remove("target");
        box.innerHTML="";
      }
      // 荒れ期待度バー
      var l3=el.querySelector(".rline2");
      if(l3) l3.style.display="";
      var st=p.star||0;
      if(bar){
        var w=Math.max(4, Math.min(100, st*20));
        bar.innerHTML='<i class="lv'+st+'" style="width:'+w+'%"></i>';
      }
      if(lab){ lab.textContent = st ? ("荒れ "+st+"/5") : ""; }
    }
    checkYosouHits();
  });
}

// 予想が当たったレースにマーキーを出す。
//   買い目の生成に payload が要るので、
//   「予想あり かつ 終了済み」のレースだけ、裏で順に確かめる。
//   1会場あたい数レースなので待たされない。
// 的中判定は payload が要る (1件2.5秒)。
//   会場を見ている最中に走らせると操作と取り合いになるので、
//   手が止まってから静かに始める。
function checkYosouHits(){
  // v330: payload を取らないので待つ必要がない。すぐ描く。
  //   買い目は picks、結果は vflags から来ているので通信ゼロ。
  for(var key in _PICKS){
    try{ markYosouHit(key); }catch(e){}
  }
}

// ============================================================
// v339: 本日の的中集計。メニューから開いたときだけ走る。
//   その日の全会場の結果を集め、picks と突き合わせる。
//   picks は最初から全会場ぶん手元にあるので、足りないのは結果だけ。
//   会場ごとに /api/venue_flags を1回ずつ、順番に叩く。
//   操作中(isBusy)は待つので、レース計算と取り合いにならない。
// ============================================================
var _FEED_ROWS = [];
var _FEED_DONE = false;

function startHitFeed(done, prog){
  loadPicks(function(){
    var names=[];
    for(var i=0;i<VENUES.length;i++){
      if(VENUES[i] && VENUES[i].name) names.push(VENUES[i].name);
    }
    if(!names.length){ _FEED_DONE=true; if(done) done([]); return; }
    var vi=0;
    function next(){
      if(vi>=names.length){
        _FEED_DONE=true; collectFeed();
        if(done) done(_FEED_ROWS);
        return;
      }
      if(isBusy()){ setTimeout(next, 900); return; }
      var vn=names[vi]; vi++;
      if(prog) prog(vn+" を調べています ("+vi+"/"+names.length+")");
      fetch("/api/venue_flags?date="+DATE+"&venue="+encodeURIComponent(vn))
        .then(function(r){return r.json()})
        .then(function(j){
          var fl=(j||{}).flags||{};
          for(var key in fl){ _RESULTMAP[key]=fl[key]; }
        })
        .catch(function(){})
        .then(function(){ setTimeout(next, 200); });
    }
    next();
  });
}

// v339: メニュー内で集計し、メニュー内に出す。画面は切り替えない。
function menuHitTally(){
  var box=document.getElementById("hitTallyBox");
  if(!box) return;
  if(box.classList.contains("open")){   // もう一度押したら閉じる
    box.classList.remove("open"); box.innerHTML=""; return;
  }
  box.classList.add("open");
  if(_FEED_DONE){ drawHitTally(); return; }
  box.innerHTML='<div class="ht-prog"><span class="spin"></span>'
    + '<span id="htProg">本日の結果を集めています…</span></div>';
  startHitFeed(function(){ drawHitTally(); },
               function(msg){
                 var e=document.getElementById("htProg");
                 if(e) e.textContent=msg;
               });
}

function drawHitTally(){
  var box=document.getElementById("hitTallyBox");
  if(!box) return;
  var rows=_FEED_ROWS||[];
  if(!rows.length){
    box.innerHTML='<div class="ht-none">本日の的中はまだありません</div>';
    return;
  }
  var yen=0, nTarget=0;
  for(var i=0;i<rows.length;i++){
    yen += (rows[i].yen||0);
    if(rows[i].kind==="target") nTarget++;
  }
  var h='<div class="ht-sum">的中 '+rows.length+'件'
      + (nTarget ? '（うち狙い予想 '+nTarget+'件）' : '')
      + '　払戻金 計'+yen.toLocaleString()+'円</div>';
  for(var k=0;k<rows.length;k++){
    var r=rows[k];
    var nm=String(r.name).replace(/（[^）]*）/,"");
    h += '<div class="ht-row">'
       +   '<span class="ht-vn">'+esc(r.venue)+' '+esc(r.rno)+'R</span>'
       +   '<span class="ht-nm '+r.kind+'">'+esc(nm)+'</span>'
       +   '<span class="ht-tri">'+esc(r.tri)+'</span>'
       +   '<span class="ht-yen">'+(r.yen ? Number(r.yen).toLocaleString()+'円' : '')+'</span>'
       + '</div>';
  }
  box.innerHTML=h;
}

function collectFeed(){
  var rows=[], seen={};
  for(var key in _PICKS){
    if(seen[key]) continue;
    var h=null;
    try{ h=computeHit(key); }catch(e){ h=null; }
    if(!h) continue;
    seen[key]=1;
    var parts=String(key).split("_");
    rows.push({venue:parts[0], rno:parts[1], kind:h.kind,
               name:h.name, tri:h.tri, yen:h.yen});
  }
  rows.sort(function(a,b){
    if(a.venue!==b.venue) return a.venue<b.venue ? -1 : 1;
    return (parseInt(a.rno)||0)-(parseInt(b.rno)||0);
  });
  _FEED_ROWS=rows;
}

// v338: 的中の判定を1か所にまとめた。
//   Rボタンの中の表示と、会場ボタン下の配信の両方から使う。
//   判定に必要なのは picks(全会場ぶん) と結果だけで、/api/race は要らない。
function computeHit(key){
  var p=_PICKS[key];
  if(!p) return null;
  var rf=_RESULTMAP[key];
  if(!rf || rf.finished!==true) return null;
  var tri=String(rf.trifecta||"");
  if(!tri) return null;
  var yen=rf.refund_3t||0;

  function inList(list){
    if(!list) return false;
    for(var k=0;k<list.length;k++){
      var t=list[k].t ? list[k].t : list[k];
      if(String(t)===tri) return true;
    }
    return false;
  }

  if(p.target && inList(p.combos)){
    return {kind:"target", name:"狙い予想", tri:tri, yen:yen};
  }
  var st=p.steps||[];
  for(var m=0;m<st.length;m++){
    if(inList(st[m].combos)){
      // 先頭ほど絞っているので最初に当たったものを使う
      return {kind:"step", name:st[m].name+"（約"+st[m].approx+"点）",
              tri:tri, yen:yen};
    }
  }
  return null;
}

function markYosouHit(key){
  // v330: payload を取らずに判定する。
  //   買い目は picks にあり、結果は vflags から来ているので、
  //   /api/race (1件2.5秒) を呼ぶ必要がない。
  //
  //   狙い予想が当たった -> [狙い予想] 赤ネオン (最優先)
  //   段階が当たった     -> 当たった中で最も絞ったもの 金ネオン
  //   どちらも外れ       -> 何も出さない
  var el=document.getElementById("rc_"+cssId(key));
  if(!el) return;
  var h=computeHit(key);
  if(!h) return;
  var kind=h.kind, name=h.name, tri=h.tri, yen=h.yen;

  // 的中はバッジから先を流す。幅に収まらず切れるのを防ぐ。
  var seg='<span class="hitbadge '+kind+'">'+name+'</span>'
        + '<span class="hitmk">的中</span>'
        + '<span class="hittri">'+tri+'</span>';
  if(yen) seg+='<span class="hityen">'+Number(yen).toLocaleString()+'円</span>';
  // v336: 文と文の間に1マス (全角空白)。これも seg に含めることで、
  //   2つ並べたときの折り返し位置がぴたりと合う。
  seg += '<span class="hitgap">\u3000</span>';
  var rbot=el.querySelector(".rbot");
  if(rbot){
    rbot.innerHTML='<div class="hitflow"><span class="hittrack">'
      + seg + seg + '</span></div>';
  }
  el.classList.add("hit");
}

function fetchVenueFlags(venueName){
  fetch("/api/venue_flags?date="+DATE+"&venue="+encodeURIComponent(venueName))
    .then(function(r){return r.json()})
    .then(function(j){
      var flags=j.flags||{};
      var cells=document.querySelectorAll(".rcell");
      for(var k=0;k<cells.length;k++){
        var el=cells[k];
        el.classList.remove("checking");
        var key=el.dataset.key;
        var f=flags[key];
        if(!f) continue;
        if(f.displayable===true) el.classList.add("displayable");
        _DISPLAYABLE[key]=(f.displayable===true);
        _RESULTMAP[key]=f;
        var labs=f.labels||{};
        var rtop=el.querySelector(".rtop");
        if(rtop && (labs.ana || labs.weak || labs.layoff)){
          var tags='';
          if(labs.ana) tags+='<span class="rtag ana">穴</span>';
          if(labs.weak) tags+='<span class="rtag weak">弱</span>';
          if(labs.layoff) tags+='<span class="rtag layoff">離</span>';
          rtop.innerHTML=tags;
        }
        // v329: 的中マーキーは「予想が当たったとき」だけに変えた。
        //   従来は大聖堂ロジックの的中で光っていたが、
        //   稼働している条件の的中とは別物なので紛らわしかった。
        //   判定は payload が要るので paintYosouHits() が後から行う。
        if(f.finished===true && false){
        }else if(f.finished===true){
          // 終了済み(非的中)は「終了」だけ。v335で1段目の右端へ移した。
          var rfin2=el.querySelector(".rfin");
          if(rfin2) rfin2.innerHTML='<span class="rt finmk">終了</span>';
        }
      }
      finishProgress();
      setStatus("");
      var dd=ensureDetailEl();
      if(dd && dd.querySelector(".loadwrap")) dd.innerHTML="";
      paintYosou();
    })
    .catch(function(){
      var cells=document.querySelectorAll(".rcell");
      for(var k=0;k<cells.length;k++) cells[k].classList.remove("checking");
      stopFakeProgress();
      setStatus("");
      setLogoThinking(false);
      var dd=ensureDetailEl();
      if(dd && dd.querySelector(".loadwrap")) dd.innerHTML="";
    });
}

// v329: 同じレースの payload を2回取りに行かないようにする。
//   Rを押したときと、的中判定のときで重複していた (1件2.5秒の無駄)。
var _PAY_CACHE={};
var _PAY_INFLIGHT={};   // 取得中のものを覚えておく
function getPayload(key){
  if(_PAY_CACHE[key]) return Promise.resolve(_PAY_CACHE[key]);
  // 完了前に同じレースをもう一度頼まれたら、走っている方を待たせる。
  //   これが無いと 1件2.5秒の計算が二重に走っていた。
  if(_PAY_INFLIGHT[key]) return _PAY_INFLIGHT[key];
  var pr=fetch("/api/race?date="+DATE+"&key="+encodeURIComponent(key))
    .then(function(r){return r.json()})
    .then(function(j){
      _PAY_CACHE[key]=j;
      delete _PAY_INFLIGHT[key];
      return j;
    })
    .catch(function(e){
      delete _PAY_INFLIGHT[key];
      throw e;
    });
  _PAY_INFLIGHT[key]=pr;
  return pr;
}

function tapRace(key,el){
  // 同じRをもう一度押したら閉じる
  if(_CUR_KEY===key && el && el.classList.contains("sel")){
    el.classList.remove("sel");
    _CUR_KEY="";
    setOracle(null);
    var dc=detachDetail();
    dc.innerHTML="";
    return;
  }
  markBusy(8000);   // 背景集計を止めて、このレースの計算を最優先にする
  _CUR_KEY=key;
  _ODDS_CACHE=null;   // レース切替でオッズキャッシュ破棄
  var cells=document.querySelectorAll(".rcell");
  for(var k=0;k<cells.length;k++) cells[k].classList.remove("sel");
  el.classList.add("sel");
  // R番号の跳ねアニメを毎回再生させる
  var rno=el.querySelector(".rno");
  if(rno){
    rno.style.animation="none";
    void rno.offsetWidth; // リフロー強制
    rno.style.animation="";
  }
  var d=ensureDetailEl();
  // v329: 詳細は一番下ではなく、押したRボタンの直下に差し込む。
  //   Rが縦に並ぶので、下まで飛ばずにその場で開けるようにした。
  try{
    if(el && el.parentNode && d.parentNode !== el.parentNode){
      el.parentNode.insertBefore(d, el.nextSibling);
    }else if(el && el.nextSibling !== d){
      el.parentNode.insertBefore(d, el.nextSibling);
    }
  }catch(e){}
  setOracle(null);   // 読み込み中は託宣を出さない
  d.innerHTML = loadingBlock();
  beginProgress("race");
  setLoading(true);
  getPayload(key)
    .then(function(j){
      stepProgress();          // 1/3 出典を紡ぐ 完了
      stepProgress();          // 2/3 地の理を測る 完了
      renderDetail(j);
      finishProgress();        // 3/3 卓に並べる
      setLoading(false);
    })
    .catch(function(e){
      setLogoThinking(false);
      d.innerHTML='<div class="skip-note">計算エラー: '+e+'</div>';
      setLoading(false);
    });
}

// 読み込み中ブロック: プログレスバー + 進捗% (ロゴは右上が考え中になる)
function loadingBlock(){
  return '<div id="loadphase" class="loadphase"></div>';
}

// 右上ロゴを「考え中(AIが思考している)」状態に切替/復帰 (自然に遷移)
function setLogoThinking(on){
  var sp=document.querySelector(".topbar .spark");
  if(sp){
    if(on) sp.classList.add("thinking");
    else sp.classList.remove("thinking");
  }
  // 読み込み中は背景の灯火を明滅させる
  if(on) document.body.classList.add("loading");
  else document.body.classList.remove("loading");
}

// ============================================================
// 読み込みの段階表示 (v330)
//   これまでは擬似的に伸ばして95%で止めていたので、
//   実際の進み具合と関係がなく、止まって見えていた。
//   実処理の節目で stepProgress() を呼び、本当に進んだときだけ進める。
// ============================================================
// 託宣ボタンを押したときに語られる文。読み込みの待ち時間に流す。
// 聖母マリアの語りとして5篇。押すたびにどれかが選ばれる。
var MARIA = [
 ["競輪の行方は、人の知恵では測れぬもの。あの直線で誰が抜け出すのか、それを定めるのは天のみです。",
  "けれど、案ずることはありません。天は黙してはおられない。風の向きに、脚の型に、並びの綾に——そっと兆しを置いていかれる。",
  "信じなさい。疑う心の前に、託宣は降りてまいりません。幾度も外し、それでも天を仰ぎ続けた者だけが、やがて御言葉を託される者となるのです。",
  "さあ、参りましょう。今日の兆しを、共に読み解きましょう。"],
 ["外れた日のことは、わたしもよく存じております。あれほど確かに見えた兆しが、砂のように崩れてゆく——その痛みを、天は見ておられぬわけではありません。",
  "けれど覚えておきなさい。外れは罰ではなく、問いなのです。その問いに向き合った回数だけ、あなたの目は澄んでゆきます。",
  "さあ、涙を拭いて。今日の盤面が、待っています。"],
 ["大きな配当に心を奪われてはなりません。欲は目を曇らせ、見えていたはずの兆しさえ覆い隠してしまう。",
  "天は、慎み深い者を好まれます。買うべきでない日に手を引く——それもまた、立派な信仰なのです。",
  "さあ、静かに息を整えて。兆しは、落ち着いた目にだけ姿を見せるのですから。"],
 ["実力の順が乱れ、堅いはずの筋が崩れる。けれど乱れた盤面こそ、天が最も雄弁に語られる場なのです。",
  "皆が惑い、目を伏せる日にこそ、託宣は意味を持つ。荒れを恐れず、されど侮らず。兆しの重なりだけを頼りになさい。",
  "さあ、参りましょう。今日の風を、共に読み解きましょう。"],
 ["一日の勝ち負けに、心を大きく動かしてはなりません。天が見ておられるのは、ひと月、ひと年と積み重ねた先の姿です。",
  "続けなさい。ただし、同じ過ちを繰り返さぬように。記し、省み、また仰ぐ。その営みを絶やさぬ者だけが、やがて託される者となるのです。",
  "さあ、今日も静かに始めましょう。"]
];

var _typeTimer = null;

// v330: 「託宣とは」の説明から語りまで、通しで一文字ずつ出す。
//   置き場所は #status の中。raceGrid は読み込み中に空にされるので使えない。
//   (v329ではraceGridに入れていたため、直後の innerHTML="" で消えていた)
function typeOracle(root){
  if(!root) return;
  stopMaria();
  var box = root.querySelector(".maria");
  if(!box) return;

  // v332: 「託宣とは」の説明はタイプライターにしない。押した瞬間から出す。
  //   一文字ずつ出すのは、そのあとに続く語りだけ。
  var para = MARIA[Math.floor(Math.random()*MARIA.length)];
  var pi=0, ci=0, cur=null;
  function tick(){
    if(pi >= para.length){ _typeTimer=null; return; }
    if(cur === null){
      cur=document.createElement("p"); cur.className="mline";
      box.appendChild(cur); ci=0;
    }
    var txt = para[pi];
    if(ci < txt.length){
      ci = ci + 1;
      cur.textContent = txt.substring(0, ci);
      _typeTimer = setTimeout(tick, 55);
    }else{
      pi = pi + 1; cur = null;
      _typeTimer = setTimeout(tick, 640);
    }
  }
  tick();
}

function stopMaria(){
  if(_typeTimer){ clearTimeout(_typeTimer); _typeTimer=null; }
}

var PHASES = {
  takusen: [
    ["出典を紡ぐ",   "開催データの取得"],
    ["盤面を検める", "レース一覧の確認"],
    ["兆しを量る",   "荒れ期待度の割り当て"],
    ["卓に並べる",   "画面への反映"]
  ],
  venue: [
    ["出典を紡ぐ",   "この会場のデータ"],
    ["影を数える",   "選手の印を判定"],
    ["結末を照らす", "確定した結果の照合"],
    ["卓に並べる",   "画面への反映"]
  ],
  race: [
    ["出典を紡ぐ",   "出走表の取得"],
    ["地の理を測る", "バンクと風の判定"],
    ["卓に並べる",   "画面への反映"]
  ],
  tab: [
    ["帳を開く",     "必要な記録を引く"],
    ["託宣を編む",   "予想の計算"],
    ["卓に並べる",   "画面への反映"]
  ]
};
var _phase = null;
var _phaseStep = 0;

var _phaseTimer = null;

// 4秒経っても進まなければ、待たせすぎないよう先へ進める。
// 実際の完了で進むのが基本で、これは止まって見えるのを防ぐ保険。
function _armPhaseTimer(){
  if(_phaseTimer) clearTimeout(_phaseTimer);
  _phaseTimer = setTimeout(function(){
    if(!_phase) return;
    if(_phaseStep < _phase.length - 1){
      _phaseStep = _phaseStep + 1;
      drawProgress();
      _armPhaseTimer();
    }
  }, 4000);
}

function beginProgress(kind){
  _phase = PHASES[kind] || PHASES.race;
  _phaseStep = 0;
  setLogoThinking(true);
  drawProgress();
  _armPhaseTimer();
}

function stepProgress(){
  if(!_phase) return;
  if(_phaseStep < _phase.length) _phaseStep = _phaseStep + 1;
  drawProgress();
  _armPhaseTimer();
}

function drawProgress(){
  if(!_phase) return;
  var n = _phase.length;
  var done = Math.min(_phaseStep, n);
  var idx = Math.min(done, n - 1);
  var cur = _phase[idx];
  var pct = Math.round(100 * done / n);
  var wrap = document.getElementById("loadphase");
  if(wrap){
    var seg = "";
    var i = 0;
    while (i < n) {
      var st = (i < done) ? " done" : ((i === done) ? " now" : "");
      seg += '<i class="pseg' + st + '"></i>';
      i = i + 1;
    }
    wrap.innerHTML =
        '<div class="pline">' + seg + '</div>'
      + '<div class="ptext"><span class="pnum">' + done + ' / ' + n
      + '</span><span class="pttl">' + cur[0] + '</span>'
      + '<span class="psub">' + cur[1] + '</span></div>';
  }
  var fill=document.getElementById("loadbarfill");
  var lab=document.getElementById("loadpct");
  if(fill) fill.style.width=pct+"%";
  if(lab) lab.textContent=done + "/" + n;
}

// 互換のために名前は残す。中身は段階制に置き換えた。
function startFakeProgress(){ beginProgress("race"); }
function stopFakeProgress(){}

function finishProgress(){
  if(_phaseTimer){ clearTimeout(_phaseTimer); _phaseTimer=null; }
  stopMaria();
  if(_phase){ _phaseStep = _phase.length; drawProgress(); }
  setLogoThinking(false);
  setTimeout(function(){
    var wrap=document.getElementById("loadphase");
    if(wrap) wrap.innerHTML="";
    _phase=null; _phaseStep=0;
  }, 260);
}

function esc(s){ return (""+s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); }

var _RACE=null;          // 現在表示中のレースデータ
var _CUR_KEY=null;       // 現在表示中のレースキー (place_raceNo)
var _ODDS_CACHE=null;    // 直近の /api/odds 結果 {ts, items, ok, map}
var _CAT_RESULT3T='';    // maria表示中レースの結果3連単(的中目のオッズ=払戻由来に使う)
var _CAT_REFUND3=0;      // その払戻(円)
var _TAB="roster";       // 現在のタブ (roster/analysis/odds/card)。デフォルト=出走表
var _TAB_LOCK=false;     // 帯メニュー固定 (ONなら他レースでもタブ維持)
var _RS_SCOPE="all";     // score順位グラフのスコープ (all/cond)
var _RS_CACHE={};        // /api/rsrank の結果キャッシュ (scope別)

function renderDetail(j){
  var d=ensureDetailEl();
  if(j.error){ setOracle([]); d.innerHTML='<div class="skip-note">'+esc(j.error)+'</div>'; return; }
  var h=j.header||{};

  if(j.status==="skip"){
    setOracle([]);
    var msg={no_line:"ライン情報なし",kojinsen:"個人戦",
      predict_none:"ライン解析不可",predict_invalid:"計算対象外"}[j.reason]||j.reason;
    if(j.detail) msg+=" ("+esc(j.detail)+")";
    // v329: 原因を推測せず、素材の状態をそのまま出す。
    //   どの選手の競走得点が欠けているのかが分かれば、
    //   出走表の取得漏れなのか、別の理由なのかを切り分けられる。
    if(j.diag){
      var g=j.diag;
      msg+='<div class="skip-diag">出走 '+g.n_players+'人 / 競走得点あり '
        + g.n_points+'人 / 直近着順あり '+g.n_hist+'人<br>'
        + 'ライン「'+esc(g.line||"(なし)")+'」 発走 '+esc(g.post||"--:--")+'</div>';
      if(g.missing && g.missing.length){
        msg+='<div class="skip-diag">得点が取れない車番: '+g.missing.join(",")+'</div>';
      }
      // 戦績が1人も取れていない = 取り込み漏れ。元サイトから取り直せる。
      if(g.n_hist===0 && g.n_players>0){
        msg+='<div class="skip-diag">直近着順が1件も入っていません。'
          + '出走表の取り込み漏れです。</div>'
          + '<button class="refetch-btn" onclick="refetchRace()">'
          + 'この会場を取り直す</button>'
          + '<div id="refetchMsg" class="skip-diag"></div>';
      }
    }
    d.innerHTML = headerCard(h) + '<div class="skip-note">'+msg+'</div>';
    return;
  }

  _RACE=j;
  if(!_TAB_LOCK) _TAB="roster";  // 固定OFFなら毎回 出走表 に戻す
  _RS_CACHE={};

  // 託宣文 (的中レースのみ・Rボタンとカードの隙間に一文ずつ切替表示)
  setOracle((j.race_result && j.race_result.oracle) ? j.race_result.oracle : []);

  var html = injectAnim(headerCard(h), 1, true);
  // 結果カード (終了済みレースのみ、ヘッダー直後に常時表示)
  if(j.race_result){
    html += injectAnim(resultCard(j.race_result), 2, true);
  }
  // 帯メニュー + 固定スイッチ + タブ内容コンテナ
  html += tabbarHtml();
  html += '<div class="tablock"><label class="tablock-sw"><input type="checkbox" id="tablockCb"'
        + (_TAB_LOCK?" checked":"") + ' onchange="toggleTabLock(this.checked)"> 帯メニューを固定</label></div>';
  html += '<div id="tabpane" class="tabpane"></div>';
  d.innerHTML = html;
  renderTab();
}

// v329: 戦績が落ちているレースを、元サイトから取り直す。
//   会場単位で取り直し、欠けているレースだけ差し替える。
//   正常に取れているレースには触らない。
function refetchRace(){
  var m=document.getElementById("refetchMsg");
  // skip表示のときは _RACE に値が入らない。選択中の会場から取る。
  var ven="";
  try{ ven=VENUES[CUR_VENUE].name||""; }catch(e){ ven=""; }
  if(!ven && _CUR_KEY){ ven=String(_CUR_KEY).split("_")[0]; }
  if(!ven){ if(m) m.innerHTML="会場が分かりません"; return; }
  if(m) m.innerHTML='<span class="spin"></span>取り直しています…';
  markBusy(30000);
  fetch("/api/refetch_race?date="+DATE+"&venue="+encodeURIComponent(ven))
    .then(function(r){return r.json()})
    .then(function(j){
      if(!j.ok){ if(m) m.innerHTML="失敗: "+esc(j.error||""); return; }
      if(!j.fixed){
        if(m) m.innerHTML="取り直しましたが、元サイトにも戦績がありませんでした ("
          + "対象"+j.target+"件)";
        return;
      }
      if(m) m.innerHTML=j.fixed+"件を取り直しました。読み込み直します…";
      // キャッシュを捨てて会場を読み直す
      _QF_CACHE={}; _YOSOU_HITS={}; _DISPLAYABLE={};
      _RESULTMAP={}; _YHIT_CHECKED={};
      _FEED_ROWS=[]; _FEED_DONE=false;
      setTimeout(function(){
        var i=CUR_VENUE;
        fetch("/api/venues?date="+DATE)
          .then(function(r){return r.json()})
          .then(function(v){
            if(v.venues){ VENUES=v.venues; }
            selectVenue(i);
          })
          .catch(function(){ selectVenue(i); });
      }, 400);
    })
    .catch(function(e){ if(m) m.innerHTML="通信失敗: "+e; });
}

function toggleTabLock(on){ _TAB_LOCK=!!on; }

function tabbarHtml(){
  // v329: 大聖堂を外し「予想」を入れた。
  //   予想 = 稼働中の条件(conditions.json)に該当したときだけ買い目を出す。
  var tabs=[["roster","出走表"],["analysis","分析"],
            ["odds","オッズ"],["tavern","酒場"],["yosou","予想"]];
  var s='<div class="tabbar">';
  for(var i=0;i<tabs.length;i++){
    var on=tabs[i][0]===_TAB?" on":"";
    s+='<button class="tab'+on+'" onclick="switchTab(\''+tabs[i][0]+'\')">'+tabs[i][1]+'</button>';
  }
  s+='</div>';
  return s;
}

function switchTab(t){
  _TAB=t;
  // タブバーのon付け替え
  var bar=document.querySelector(".tabbar");
  if(bar){
    var btns=bar.querySelectorAll(".tab");
    var order=["roster","analysis","odds","tavern","yosou"];
    for(var i=0;i<btns.length;i++){
      if(order[i]===t) btns[i].classList.add("on");
      else btns[i].classList.remove("on");
    }
  }
  renderTab();
}

// レース情報カードから大聖堂タブへ移動し、メニューが見える位置までスクロール。
function gotoCathedral(){
  switchTab("yosou");
  var bar=document.querySelector(".tabbar");
  if(bar && bar.scrollIntoView){ bar.scrollIntoView({behavior:"smooth", block:"start"}); }
}

function renderTab(){
  var pane=document.getElementById("tabpane");
  // v330: 重いタブだけ段階表示を出す。予想タブは picks を読むだけで軽い。
  if(!pane || !_RACE) return;
  var j=_RACE, h=j.header||{};
  if(_TAB==="roster"){
    pane.innerHTML='<div class="card card-roster fade"><div class="card-b">'+rosterHtml(h)+'</div></div>';
  }else if(_TAB==="tavern"){
    var html='<div class="card fade"><div class="card-h"><span class="ttl">酒場（気配値順）</span></div><div class="card-b">';
    var ps=j.patterns||[];
    for(var i=0;i<ps.length;i++){ html+=patternCard(ps[i], i===0, j); }
    html+='</div></div>';
    pane.innerHTML=html;
  }else if(_TAB==="odds"){
    renderOdds(pane);
  }else if(_TAB==="analysis"){
    renderAnalysis(pane);
  }else if(_TAB==="yosou"){
    renderYosou(pane);
  }
}

// 分析タブ: サブメニュー(score順位 / 決まり手)
var _ASUB="rsrank";  // 分析のサブタブ
function renderAnalysis(pane){
  var html='<div class="subbar">'
    + '<div class="sub'+(_ASUB==="rsrank"?" on":"")+'" onclick="setAsub(\'rsrank\')">score順位</div>'
    + '<div class="sub'+(_ASUB==="kimari"?" on":"")+'" onclick="setAsub(\'kimari\')">決まり手</div>'
    + '</div><div id="asubpane"></div>';
  pane.innerHTML=html;
  renderAsub();
}
function setAsub(s){
  _ASUB=s;
  var subs=document.querySelectorAll(".subbar .sub");
  if(subs.length>=2){
    subs[0].classList.toggle("on", s==="rsrank");
    subs[1].classList.toggle("on", s==="kimari");
  }
  renderAsub();
}
function renderAsub(){
  var box=document.getElementById("asubpane");
  if(!box || !_RACE) return;
  var j=_RACE;
  if(_ASUB==="kimari"){
    if(j.kimari && j.kimari.exists){
      box.innerHTML='<div class="card fade"><div class="card-h"><span class="ttl">決まり手</span></div><div class="card-b">'
        + kimariBody(j.kimari) + '</div></div>';
      animateBars(box);
    }else{
      box.innerHTML='<div class="card fade"><div class="card-b"><div class="nodata">決まり手データがありません</div></div></div>';
    }
  }else{
    box.innerHTML='<div class="card fade"><div class="card-h"><span class="ttl">score順位</span></div><div class="card-b">'
      + '<div class="scope-sw">'
      + '<div class="sw'+(_RS_SCOPE==="all"?" on":"")+'" onclick="setRsScope(\'all\')">選手合計</div>'
      + '<div class="sw'+(_RS_SCOPE==="cond"?" on":"")+'" onclick="setRsScope(\'cond\')">今回区分</div>'
      + '</div>'
      + '<div id="rsrankbox"><div class="loadwrap"><div class="loadbar"><div class="loadbar-fill" style="width:0%"></div></div><div class="loadpct">0%</div></div></div>'
      + '</div></div>';
    loadRsRank();
  }
}

function setRsScope(s){
  _RS_SCOPE=s;
  var sws=document.querySelectorAll(".scope-sw .sw");
  if(sws.length>=2){
    sws[0].classList.toggle("on", s==="all");
    sws[1].classList.toggle("on", s==="cond");
  }
  loadRsRank();
}

var _rsTimer=null;
function loadRsRank(){
  var box=document.getElementById("rsrankbox");
  if(!box || !_RACE) return;
  if(_RS_CACHE[_RS_SCOPE]){ box.innerHTML=rsrankHtml(_RS_CACHE[_RS_SCOPE]); animateBars(box); return; }
  box.innerHTML='<div class="loadwrap"><div class="loadbar"><div id="rsfill" class="loadbar-fill" style="width:0%"></div></div><div id="rspct" class="loadpct">0%</div></div>';
  // 進捗%アニメ (95%頭打ち、完了で100%)
  if(_rsTimer){ clearInterval(_rsTimer); _rsTimer=null; }
  var pct=0;
  _rsTimer=setInterval(function(){
    var inc = pct<60?7:(pct<85?2:0.6);
    pct=Math.min(95, pct+inc);
    var f=document.getElementById("rsfill"); var l=document.getElementById("rspct");
    if(f) f.style.width=pct+"%";
    if(l) l.textContent=Math.floor(pct)+"%";
    if(pct>=95){ clearInterval(_rsTimer); _rsTimer=null; }
  }, 90);
  var finishRs=function(){
    if(_rsTimer){ clearInterval(_rsTimer); _rsTimer=null; }
    var f=document.getElementById("rsfill"); var l=document.getElementById("rspct");
    if(f) f.style.width="100%"; if(l) l.textContent="100%";
  };
  var key=_RACE.header.venue+"_"+_RACE.header.race_no;
  fetch("/api/rsrank?date="+DATE+"&key="+encodeURIComponent(key)+"&scope="+_RS_SCOPE)
    .then(function(r){return r.json()})
    .then(function(d){ finishRs(); _RS_CACHE[_RS_SCOPE]=d; var b=document.getElementById("rsrankbox"); if(b){ b.innerHTML=rsrankHtml(d); animateBars(b); } })
    .catch(function(){ if(_rsTimer){clearInterval(_rsTimer);_rsTimer=null;} var b=document.getElementById("rsrankbox"); if(b) b.innerHTML='<div class="nodata">取得に失敗しました</div>'; });
}

// ============ 御告タブ (買い目生成: 決まり手遷移予想エンジン) ============
// 主軸: 決まり手遷移(軸基準・ライン構造込み)の「本レース率÷baseline率」揺らぎ。
//   そこへ選手個別力(score/適合/決まり手%)を掛け合わせ、3連単優先度を決める。
// データ源:
//   _RACE.header.players : 各車 bike/name/role/raw_score/match_score/rr/kimari
//   _RACE.kimari         : build_kimari_payload (kimari_link / third に rate & base_rate)
//   _RACE.header.line_display : ライン文字列 (例 "725-146-3")
var _ORA_AXIS=[];       // 軸の車番(string)配列。複数可(軸ながし)。
var _ORA_RIVAL=[];      // 対抗(2着固定)の車番配列。空ならおまかせ。
var _ORA_N=6;           // 点数上限
var _ORA_OMK_AXIS=0;    // 託宣の軸人数: 0=自動(閾値選出) / 1,2,3=上位N軸を強制
var _ORA_LAST=[];       // 直近に生成した買い目 combo("a-b-c")配列(オッズ印用)
var _ORA_RATIO_CAP=3.0; // 揺らぎ(比)の上限
var _ORA_CONF_N=20;     // 信頼度補正の基準母数(これ未満は揺らぎを1に近づける)
var _ORA_RSR_CONFN=10;  // rsrank揺らぎの信頼度基準母数(all母数。これ未満は揺らぎを1へ収束)

// ---- ライン解析: 車番→{group:ラインindex, pos:ライン内位置(1始まり), size} ----
function __oraParseLines(lineDisp){
  var info={};   // bike(str) -> {group, pos, size}
  var groups=(lineDisp||'').split('-');
  var gi=0;
  for(var g=0; g<groups.length; g++){
    var grp=(groups[g]||'').replace(/[^0-9]/g,'');
    if(!grp) continue;
    for(var c=0; c<grp.length; c++){
      info[grp.charAt(c)]={group:gi, pos:c+1, size:grp.length};
    }
    gi++;
  }
  return info;
}

// ---- 2着/3着ラベルを解析: "同2番手マ" -> {side:'同', pos:2, kim:'マ'} ----
//   位置: 先頭=1, N番手=N。3着ラベルは決まり手なし(kim='')
function __oraParseLabel(lab){
  if(!lab) return null;
  var side='';
  var rest=lab;
  if(rest.charAt(0)==='同'){ side='同'; rest=rest.slice(1); }
  else if(rest.charAt(0)==='別'){ side='別'; rest=rest.slice(1); }
  else if(rest.indexOf('単騎')===0){ side='単騎'; rest=rest.slice(2); }
  // 単騎ラベルの末尾に決まり手が付く場合 ("単騎差")
  if(side==='単騎'){
    var kimS=rest; // 残りが決まり手 or 空
    return {side:'単騎', pos:0, kim:kimS};
  }
  // rest: "先頭マ" / "2番手差" / "先頭" / "2番手" ...
  var pos=0, kim='';
  if(rest.indexOf('先頭')===0){ pos=1; kim=rest.slice(2); }
  else {
    var m=rest.match(/^(\d+)番手(.*)$/);
    if(m){ pos=parseInt(m[1],10); kim=m[2]||''; }
    else { return null; }
  }
  return {side:side, pos:pos, kim:kim};
}

// ---- ラベル(side,pos)を軸基準で実車番リストに変換 ----
//   axisBike: 軸車番(str), lineInfo: __oraParseLines結果, players: 全車
//   戻り値: 該当車番(str)の配列(複数可)。
function __oraLabelToBikes(side, pos, axisBike, lineInfo, allBikes){
  var ax=lineInfo[axisBike];
  var out=[];
  if(side==='単騎'){
    // 単騎ライン(size=1)の全車(軸自身は除く)
    for(var i=0;i<allBikes.length;i++){
      var b=allBikes[i]; if(b===axisBike) continue;
      var inf=lineInfo[b];
      if(inf && inf.size===1) out.push(b);
    }
    return out;
  }
  for(var j=0;j<allBikes.length;j++){
    var bk=allBikes[j]; if(bk===axisBike) continue;
    var bi=lineInfo[bk];
    if(!bi) continue;
    if(bi.size===1) continue;          // 単騎は別区分
    if(bi.pos!==pos) continue;         // 位置一致
    var sameLine = (ax && bi.group===ax.group);
    if(side==='同' && sameLine) out.push(bk);
    else if(side==='別' && !sameLine) out.push(bk);
  }
  return out;
}

// ---- 選手個別の複合力(0-1) ----
//   score/適合/SRを正規化合成。決まり手適性は別途ラベルの決まり手で評価。
function __oraPlayerBase(players){
  var sMin=Infinity,sMax=-Infinity,mMin=Infinity,mMax=-Infinity,rMin=Infinity,rMax=-Infinity;
  for(var i=0;i<players.length;i++){
    var p=players[i];
    if(p.raw_score!=null){ if(p.raw_score<sMin)sMin=p.raw_score; if(p.raw_score>sMax)sMax=p.raw_score; }
    if(p.match_score!=null){ if(p.match_score<mMin)mMin=p.match_score; if(p.match_score>mMax)mMax=p.match_score; }
    if(p.rr!=null){ if(p.rr<rMin)rMin=p.rr; if(p.rr>rMax)rMax=p.rr; }
  }
  function nz(v,lo,hi){ if(v==null||hi<=lo) return 0.5; return (v-lo)/(hi-lo); }
  var out={};
  for(var j=0;j<players.length;j++){
    var q=players[j];
    out[String(q.bike)]={
      sN:nz(q.raw_score,sMin,sMax),
      mN:nz(q.match_score,mMin,mMax),
      rN:nz(q.rr,rMin,rMax),
      role:q.role, name:q.name, kimari:q.kimari,
      raw_score:q.raw_score, match_score:q.match_score, rr:q.rr
    };
  }
  return out;
}

// ---- 指定車の指定決まり手の率(0-1) ----
function __oraKimRate(kimari, kk){
  if(!kimari || !kimari.items || !kk) return null;
  for(var i=0;i<kimari.items.length;i++){
    if(kimari.items[i].k===kk) return kimari.items[i].rate;
  }
  return 0;
}

// ---- 揺らぎ: 本レース率/baseline率。上限cap、母数nで信頼度補正 ----
function __oraFluct(rate, baseRate, n){
  var r=(rate!=null)?rate:0;
  var b=(baseRate!=null)?baseRate:0;
  var ratio;
  if(b<=0.0001){ ratio=(r>0)?_ORA_RATIO_CAP:1.0; }
  else { ratio=r/b; }
  if(ratio>_ORA_RATIO_CAP) ratio=_ORA_RATIO_CAP;
  if(ratio<0.001) ratio=0.001;
  // 信頼度: n>=CONF_Nで生の比、小さいほど1へ収束 (conf=min(1,n/CONF_N))
  var conf=(n!=null && n>0)? Math.min(1, n/_ORA_CONF_N) : 0;
  var adj=1.0 + (ratio-1.0)*conf;   // confが0なら1(揺らぎ無効)、1なら生の比
  return adj;
}

// ---- 軸の1着決まり手分布を取得(本レース率と揺らぎ) ----
//   kimari payload の kimari_1st: [{label,rate,base_rate}] (rateは%表記)
function __oraAxis1st(kimariPayload){
  var out=[];
  if(!kimariPayload || !kimariPayload.kimari_1st) return out;
  var k1=kimariPayload.kimari_1st;
  var cellN=kimariPayload.cell_n||0;
  for(var i=0;i<k1.length;i++){
    var it=k1[i];
    var rate=(it.rate!=null)?it.rate/100.0:0;
    var base=(it.base_rate!=null)?it.base_rate/100.0:null;
    var n=Math.round(rate*cellN);
    out.push({kim:it.label, rate:rate, fluct:__oraFluct(rate, base, n)});
  }
  return out;
}

// ---- 軸の決まり手kに対する2着遷移リストを取得 ----
//   kimari_link: [{kimari, n, items:[{label,rate,base_rate,third:[{label,rate,base_rate}],third_n}]}]
function __oraLink(kimariPayload, axisKim){
  if(!kimariPayload || !kimariPayload.kimari_link) return null;
  for(var i=0;i<kimariPayload.kimari_link.length;i++){
    if(kimariPayload.kimari_link[i].kimari===axisKim) return kimariPayload.kimari_link[i];
  }
  return null;
}

// ---- メイン予想: 軸→3連単スコアリスト ----
//   各3連単 = {b:[b1,b2,b3], total, parts:{...}}
function __oraPredict(d){
  var players=d.players;
  var kimP=(_RACE && _RACE.kimari && _RACE.kimari.exists)? _RACE.kimari : null;
  var lineDisp=(_RACE && _RACE.header)? (_RACE.header.line_display||'') : '';
  var lineInfo=__oraParseLines(lineDisp);
  var base=__oraPlayerBase(players);
  var allBikes=[]; for(var i=0;i<players.length;i++){ allBikes.push(String(players[i].bike)); }
  var axes=_ORA_AXIS.slice();
  if(!axes.length){ return {error:'軸を1車以上選んでください。'}; }
  if(!kimP){ return {error:'決まり手遷移データがありません。'}; }

  // 軸ながし: 各軸を1着にした予想を合流(スコア加算) … 第一柱(決まり手遷移)
  var comboMap={};
  for(var ai=0; ai<axes.length; ai++){
    __oraAccumAxis(axes[ai], kimP, lineInfo, base, allBikes, comboMap);
  }

  // === 第二柱(rsrank揺らぎ) ===
  // α>0 のとき、軸×全2着×全3着の総当たりで rsrank スコアを生成する。
  // これにより、決まり手遷移リストに存在しない組(例 2-4-6)も候補に昇格しうる。
  var alpha=1.0; // 手動軸ではrsrank補完を常時使用(αなし設計)
  var rsrMap=__oraRsrMap(players);
  var rsrMapScore={};   // key -> rsrank柱スコア(生)
  if(alpha>0){
    for(var ax=0; ax<axes.length; ax++){
      var axis=axes[ax];
      for(var u=0;u<allBikes.length;u++){
        var bb2=allBikes[u]; if(bb2===axis) continue;
        for(var v=0;v<allBikes.length;v++){
          var bb3=allBikes[v]; if(bb3===axis||bb3===bb2) continue;
          var k2=axis+'-'+bb2+'-'+bb3;
          var sc=__oraRsrScore(rsrMap, axis, bb2, bb3);
          rsrMapScore[k2]=(rsrMapScore[k2]||0)+sc;
        }
      }
    }
  }

  // === 2柱の個別正規化(各最大=1)してから合成 ===
  // スケールが大きく異なる(遷移=確率積で微小, rsrank=揺らぎ積で1前後)ため、
  // それぞれ最大値で割って [0,1] に揃えてから α合成する。
  var maxT=0; for(var kt in comboMap){ if(comboMap.hasOwnProperty(kt) && comboMap[kt]>maxT) maxT=comboMap[kt]; }
  var maxR=0; for(var kr in rsrMapScore){ if(rsrMapScore.hasOwnProperty(kr) && rsrMapScore[kr]>maxR) maxR=rsrMapScore[kr]; }
  if(maxT<=0)maxT=1; if(maxR<=0)maxR=1;

  // 合成対象キーの和集合(遷移にある組 ∪ rsrankで生成した組)
  var allKeys={};
  for(var ka in comboMap){ if(comboMap.hasOwnProperty(ka)) allKeys[ka]=1; }
  if(alpha>0){ for(var kb in rsrMapScore){ if(rsrMapScore.hasOwnProperty(kb)) allKeys[kb]=1; } }

  var blended={};
  for(var key in allKeys){
    if(!allKeys.hasOwnProperty(key)) continue;
    var tN=(comboMap[key]||0)/maxT;            // 正規化 遷移スコア
    var rN=(alpha>0)?((rsrMapScore[key]||0)/maxR):0;  // 正規化 rsrankスコア
    blended[key]=(1-alpha)*tN + alpha*rN;
  }

  // 対抗(2着)固定が指定されていれば、その車が2着の組のみ残す
  var combos=[];
  for(var key2 in blended){
    if(!blended.hasOwnProperty(key2)) continue;
    var parts=key2.split('-');
    if(_ORA_RIVAL.length>0 && _ORA_RIVAL.indexOf(parts[1])<0) continue;
    if(blended[key2]<=0) continue;
    combos.push({b:parts, total:blended[key2],
                 _t:(comboMap[key2]||0)/maxT, _r:(alpha>0)?((rsrMapScore[key2]||0)/maxR):0});
  }
  if(!combos.length){ return {error:'条件に合う買い目がありません(遷移データ不足の可能性)。'}; }
  // 正規化(最大を100に)
  var mx=0; for(var m=0;m<combos.length;m++){ if(combos[m].total>mx)mx=combos[m].total; }
  if(mx<=0) mx=1;
  for(var z=0;z<combos.length;z++){ combos[z].score=Math.round(combos[z].total/mx*1000)/10; }
  combos.sort(function(a,b){ return b.total-a.total; });
  return {combos:combos, base:base, lineInfo:lineInfo, alpha:alpha};
}

// 1つの軸を1着にした予想スコアを comboMap に加算
//   fluctMap(任意): 同keyに揺らぎ寄与(fl2*fl3 を sc3 で重み付け)を加算
function __oraAccumAxis(axis, kimP, lineInfo, base, allBikes, comboMap, fluctMap){
  // 軸の1着決まり手分布(揺らぎ込み) × 軸選手のその決まり手適性
  var axisKimari=base[axis]?base[axis].kimari:null;
  var a1=__oraAxis1st(kimP);
  var axis1stByKim={};
  var axis1stSum=0;
  for(var x=0;x<a1.length;x++){
    var kk=a1[x].kim;
    var pr=__oraKimRate(axisKimari, kk);
    var prv=(pr!=null)?pr:0.25;
    var w=a1[x].rate*a1[x].fluct*(0.3+0.7*prv);
    axis1stByKim[kk]=w; axis1stSum+=w;
  }
  if(axis1stSum<=0) axis1stSum=1;

  for(var ki in axis1stByKim){
    if(!axis1stByKim.hasOwnProperty(ki)) continue;
    var w1=axis1stByKim[ki]/axis1stSum;
    if(w1<=0) continue;
    var link=__oraLink(kimP, ki);
    if(!link || !link.items) continue;
    var linkN=link.n||0;
    for(var s=0;s<link.items.length;s++){
      var it2=link.items[s];
      var p2=__oraParseLabel(it2.label);
      if(!p2) continue;
      var bikes2=__oraLabelToBikes(p2.side, p2.pos, axis, lineInfo, allBikes);
      if(!bikes2.length) continue;
      var rate2=(it2.rate!=null)?it2.rate/100.0:0;
      var base2=(it2.base_rate!=null)?it2.base_rate/100.0:null;
      var n2=Math.round(rate2*linkN);
      var fl2=__oraFluct(rate2, base2, n2);
      var cand2=__oraRankCandidates(bikes2, base, p2.kim);
      for(var c2=0;c2<cand2.length;c2++){
        var b2=cand2[c2].bike;
        if(b2===axis) continue;
        var sc2=w1 * rate2 * fl2 * cand2[c2].apt;
        var thirds=it2.third||[];
        if(!thirds.length){ continue; }
        var thirdN=it2.third_n||0;
        for(var t=0;t<thirds.length;t++){
          var it3=thirds[t];
          var p3=__oraParseLabel(it3.label);
          if(!p3) continue;
          var bikes3=__oraLabelToBikes(p3.side, p3.pos, axis, lineInfo, allBikes);
          if(!bikes3.length) continue;
          var rate3=(it3.rate!=null)?it3.rate/100.0:0;
          var base3=(it3.base_rate!=null)?it3.base_rate/100.0:null;
          var n3=Math.round(rate3*thirdN);
          var fl3=__oraFluct(rate3, base3, n3);
          var cand3=__oraRankCandidates(bikes3, base, '');
          for(var c3=0;c3<cand3.length;c3++){
            var b3=cand3[c3].bike;
            if(b3===axis || b3===b2) continue;
            var sc3=sc2 * rate3 * fl3 * cand3[c3].apt;
            var key=axis+'-'+b2+'-'+b3;
            comboMap[key]=(comboMap[key]||0)+sc3;
            if(fluctMap){
              // 揺らぎ寄与: この組がどれだけ「条件で浮上」したか(fl2*fl3)を、スコアで重み付け
              fluctMap[key]=(fluctMap[key]||0)+sc3*(fl2*fl3);
            }
          }
        }
      }
    }
  }
}

// ---- ラベル該当車を選手力で順位づけ ----
//   kim: 評価対象の決まり手(2着ラベルの決まり手)。空なら総合力のみ。
//   apt(0-1) = 選手複合力 × (kim指定時その決まり手率を加味)
function __oraRankCandidates(bikes, base, kim){
  var arr=[];
  for(var i=0;i<bikes.length;i++){
    var b=bikes[i]; var o=base[b];
    if(!o){ arr.push({bike:b, apt:0.3}); continue; }
    var force=0.45*o.sN + 0.30*o.mN + 0.10*o.rN + 0.15; // 基礎力(0.15..1.0)
    var kr=1.0;
    if(kim){
      var rr=__oraKimRate(o.kimari, kim);
      kr=(rr!=null)? (0.3+0.7*rr) : 0.5;   // その決まり手をどれだけ打てるか
    }
    arr.push({bike:b, apt:force*kr});
  }
  arr.sort(function(a,b){ return b.apt-a.apt; });
  return arr;
}

// ============ 第二柱: rsrank着順揺らぎ ============
// 各車の rsr = {self:{pct:[7],n}, base:{pct:[7],n}} (cond区分・サーバー注入)
//   pct index: 0=1着,1=2着,...,6=7着
// 揺らぎ(着rank=1..7) = (self該当着% / base該当着%) を cap し、母数nで信頼度補正。
// bike(str) -> rsr を引く表を作る
function __oraRsrMap(players){
  var m={};
  for(var i=0;i<players.length;i++){
    m[String(players[i].bike)] = players[i].rsr || null;
  }
  return m;
}
// rsrank揺らぎ: bike が「rank着(1..7)」になる揺らぎ(信頼度補正込み)。
//   データ無し/母数0 のときは 1.0(寄与なし=中立) を返す。
function __oraRsrFluct(rsr, rank){
  if(!rsr || !rsr.self || !rsr.base) return 1.0;
  var idx=rank-1;
  if(idx<0 || idx>6) return 1.0;
  var sp=rsr.self.pct, bp=rsr.base.pct;
  if(!sp || !bp) return 1.0;
  var s=(sp[idx]!=null)?sp[idx]:0;    // %
  var b=(bp[idx]!=null)?bp[idx]:0;    // %
  var ratio;
  if(b<=0.0001){ ratio=(s>0)?_ORA_RATIO_CAP:1.0; }
  else { ratio=s/b; }
  if(ratio>_ORA_RATIO_CAP) ratio=_ORA_RATIO_CAP;
  if(ratio<0.001) ratio=0.001;
  // 信頼度: 本人cond母数 self.n で補正(母数小→揺らぎを1へ収束)
  var n=(rsr.self.n!=null)?rsr.self.n:0;
  var conf=(n>0)? Math.min(1, n/_ORA_RSR_CONFN) : 0;
  return 1.0 + (ratio-1.0)*conf;
}
// 3連単(軸-b2-b3)の rsrank柱スコア = 軸1着揺らぎ × b2の2着揺らぎ × b3の3着揺らぎ
function __oraRsrScore(rsrMap, axis, b2, b3){
  var fa=__oraRsrFluct(rsrMap[axis], 1);
  var f2=__oraRsrFluct(rsrMap[b2], 2);
  var f3=__oraRsrFluct(rsrMap[b3], 3);
  return fa*f2*f3;
}
// rsrankの指定着(1..7)の「絶対率%」を取り出す。データ無は null。
function __oraRsrSelfPct(rsr, rank){
  if(!rsr || !rsr.self) return null;
  var idx=rank-1;
  if(idx<0 || idx>6) return null;
  var sp=rsr.self.pct;
  if(!sp || sp[idx]==null) return null;
  return sp[idx];
}
// 軸選定用 第二柱スコア = 絶対1着率(%) × conf補正 × 1着揺らぎ。
//   self1% に母数信頼度(conf=min(1,n/CONFN))を掛けることで、
//   母数が薄い選手(n=3等)の高率が突出してaxisScを歪めるのを防ぐ。
//   データ無は 0(軸として浮上させない)。
function __oraRsrAxisScore(rsr){
  var sp=__oraRsrSelfPct(rsr, 1);
  if(sp==null) return 0;
  var n=(rsr && rsr.self && rsr.self.n!=null)?rsr.self.n:0;
  var conf=(n>0)?Math.min(1, n/_ORA_RSR_CONFN):0;
  var fl=__oraRsrFluct(rsr, 1);
  return sp*conf*fl;
}

// ============ オッズタブ(3連単・人気順) ============
// winticket から 3連単オッズを取得し人気順(オッズ昇順)で表示。
// 御告で生成済みの買い目があれば「印」を付ける。
function __oddsFmt(v){
  if(v==null) return '-';
  var n=Number(v);
  if(isNaN(n)) return '-';
  return n.toFixed(1);
}
// オッズの大小で色分け(本命=低オッズ=琥珀、中穴、大穴=赤)
function __oddsClass(v){
  var n=Number(v);
  if(isNaN(n)) return '';
  if(n<10) return 'od-honmei';
  if(n<30) return 'od-taikou';
  if(n<100) return 'od-ana';
  return 'od-oketa';
}
function renderOdds(pane){
  pane.innerHTML='<div class="card fade"><div class="card-h">'
    + '<span class="ttl">オッズ（3連単・人気順）</span>'
    + '<button class="odds-refresh" onclick="oddsRefresh()">更新</button>'
    + '</div>'
    + '<div class="card-b"><div id="oddsbox"><div class="nodata">読み込み中…</div></div></div></div>';
  oddsLoad(false);
}
// 取得して描画。force=true で winticket 再取得(キャッシュ無視)。
function oddsLoad(force){
  var box=document.getElementById("oddsbox");
  if(!box) return;
  if(!_CUR_KEY){ box.innerHTML='<div class="nodata">レースを選択してください。</div>'; return; }
  // キャッシュがあり force でなければ即描画
  if(!force && _ODDS_CACHE && _ODDS_CACHE.key===_CUR_KEY){
    box.innerHTML=__oddsHtml(_ODDS_CACHE);
    return;
  }
  box.innerHTML='<div class="nodata">オッズ取得中…</div>';
  var url="/api/odds?date="+DATE+"&key="+encodeURIComponent(_CUR_KEY)+(force?"&force=1":"");
  fetch(url).then(function(r){return r.json()}).then(function(j){
    var map={};
    var items=j.items||[];
    for(var i=0;i<items.length;i++){ map[items[i].combo]=items[i]; }
    _ODDS_CACHE={key:_CUR_KEY, ok:j.ok, items:items, map:map,
                 count:j.count||0, ts:Date.now(), diag:j.diag||{}};
    // 取得失敗(0件)はエラーログに記録
    if(!items.length){
      logError('オッズ取得失敗', __oddsDiagText(_CUR_KEY, j.diag||{}));
    }
    var b=document.getElementById("oddsbox");
    if(b) b.innerHTML=__oddsHtml(_ODDS_CACHE);
  }).catch(function(e){
    logError('オッズ取得エラー', 'key: '+_CUR_KEY+'\n'+esc(e));
    var b=document.getElementById("oddsbox");
    if(b) b.innerHTML='<div class="ora-warn">オッズ取得エラー: '+esc(e)+'</div>';
  });
}
// 診断をコピペ用テキストへ整形(オッズ)
function __oddsDiagText(key, dg){
  var L=[];
  L.push('race_key: '+key);
  L.push('date: '+DATE);
  if(dg.step) L.push('step: '+dg.step);
  var gg=dg.gamboo||(dg.tried&&!dg.winticket?dg:null);
  if(gg){
    L.push('--- gamboo ---');
    if(gg.step) L.push('step: '+gg.step);
    if(gg.last_err) L.push('err: '+gg.last_err);
    if(gg.n!=null) L.push('n: '+gg.n);
    var gt=gg.tried||[];
    for(var i=0;i<gt.length;i++){ L.push('url'+(i+1)+': '+gt[i]); }
  }
  var w=dg.winticket||null;
  if(w){
    L.push('--- winticket ---');
    if(w.step) L.push('step: '+w.step);
    if(w.ids_step) L.push('ids_step: '+w.ids_step);
    if(w.cup_id) L.push('cup_id: '+w.cup_id);
    if(w.used_day!=null) L.push('used_day: '+w.used_day);
    if(w.n!=null) L.push('n: '+w.n);
    if(w.url) L.push('url: '+w.url);
    if(w.raw_keys) L.push('raw_keys: '+JSON.stringify(w.raw_keys));
    if(w.odds_keys) L.push('odds_keys: '+JSON.stringify(w.odds_keys));
    if(w.raw_sample) L.push('sample: '+String(w.raw_sample).slice(0,400));
  }
  if(L.length<=2) L.push('diag: '+JSON.stringify(dg).slice(0,600));
  return L.join('\n');
}
function oddsRefresh(){ oddsLoad(true); }

// 御告で生成済みの買い目 combo 集合(印用)。無ければ空。
function __oddsMyCombos(){
  var set={};
  try{
    var res=document.getElementById("ora-result");
    // 直近の託宣/御告は _ORA_LAST に保持(無ければ DOM 非依存で空)
    if(typeof _ORA_LAST!=='undefined' && _ORA_LAST && _ORA_LAST.length){
      for(var i=0;i<_ORA_LAST.length;i++){ set[_ORA_LAST[i]]=1; }
    }
  }catch(e){}
  return set;
}

function __oddsHtml(c){
  if(!c.items || !c.items.length){
    var dg=c.diag||{};
    var hint=(dg.step&&dg.step!=='ok')?('（'+esc(dg.step)+'）'):'';
    // --- 診断情報を画面に表示(原因特定用) ---
    var dbg='';
    try{
      var g=dg.gamboo||(dg.tried?dg:null);
      var w=dg.winticket||null;
      var lines=[];
      if(dg.step) lines.push('step: '+dg.step);
      // gamboo診断
      var gg=dg.gamboo||(dg.tried&&!dg.winticket?dg:null);
      if(gg){
        lines.push('— gamboo —');
        if(gg.step) lines.push('  step: '+gg.step);
        if(gg.last_err) lines.push('  err: '+gg.last_err);
        if(gg.n!=null) lines.push('  n: '+gg.n);
        var gt=gg.tried||[];
        for(var i=0;i<gt.length;i++){ lines.push('  url'+(i+1)+': '+gt[i]); }
      }
      // winticket診断
      if(w){
        lines.push('— winticket —');
        if(w.step) lines.push('  step: '+w.step);
        if(w.ids_step) lines.push('  ids_step: '+w.ids_step);
        if(w.cup_id) lines.push('  cup_id: '+w.cup_id);
        if(w.used_day!=null) lines.push('  used_day: '+w.used_day);
        if(w.n!=null) lines.push('  n: '+w.n);
        if(w.raw_keys) lines.push('  raw_keys: '+JSON.stringify(w.raw_keys));
        if(w.odds_keys) lines.push('  odds_keys: '+JSON.stringify(w.odds_keys));
        if(w.url) lines.push('  url: '+w.url);
        if(w.raw_sample) lines.push('  sample: '+String(w.raw_sample).slice(0,400));
      }
      if(!lines.length) lines.push(JSON.stringify(dg).slice(0,600));
      dbg='<div style="margin-top:10px;padding:8px;border:1px solid var(--line);'
        + 'border-radius:6px;background:rgba(0,0,0,.25);font-size:11px;'
        + 'color:var(--txt-dim);white-space:pre-wrap;word-break:break-all;'
        + 'font-family:monospace">'+esc(lines.join('\n'))+'</div>';
    }catch(e){ dbg='<div class="nodata">diag表示エラー: '+esc(e)+'</div>'; }
    return '<div class="nodata">オッズを取得できませんでした'+hint+'。<br>'
         + '時間をおいて「更新」を押してください。</div>'+dbg;
  }
  var mine=__oddsMyCombos();
  var rt=(_RACE&&_RACE.race_result)?_RACE.race_result.trifecta:null;
  var html='<div class="odds-meta">'+c.count+'点 / オッズ昇順（人気順）'
         + (Object.keys(mine).length?' ・<span class="od-mine-lab">■=御告・託宣買い目</span>':'')
         + '</div>';
  html+='<div class="odds-list">';
  var n=c.items.length;
  for(var i=0;i<n;i++){
    var it=c.items[i];
    var parts=String(it.combo).split('-');
    var isMine=mine[it.combo]?' od-mine':'';
    var isHit=(rt&&String(rt).replace(/\s/g,'')===it.combo)?' od-hit':'';
    var bikes='';
    for(var p=0;p<parts.length;p++){
      bikes+='<span class="ora-bk bcol'+esc(parts[p])+'">'+esc(parts[p])+'</span>';
      if(p<parts.length-1) bikes+='<span class="ora-arr">&#8594;</span>';
    }
    html+='<div class="odds-item'+isHit+'">'
       + '<span class="odds-rank">'+it.rank+'</span>'
       + '<span class="odds-bikes">'+bikes+(isMine?'<span class="od-mine-dot">■</span>':'')+'</span>'
       + '<span class="odds-val '+__oddsClass(it.odds)+'">'+__oddsFmt(it.odds)+'<span class="odds-bai">倍</span></span>'
       + '</div>';
  }
  html+='</div>';
  return html;
}

// ============================================================
// 大聖堂タブ (新予測エンジン: predict_cathedral)
//   3モード: priest(牧師) / bishop(大司教) / maria(聖母マリア)
// ============================================================
var _CAT_MODE="maria";     // maria | priest | bishop
var _CAT_AXIS=null;        // 牧師: 軸 車番(string) 1台
var _CAT_RIVAL=null;       // 牧師: 対抗 車番(string) 任意1台
var _CAT_RMODE="fixed";    // 牧師: fixed(固定) | renta(連対)
var _CAT_BIKES=[];         // 大司教: 指定車番(string) 1-2台
var _CAT_N=6;              // 点数上限

function _catPlayers(){
  if(_RACE && _RACE.header && _RACE.header.players && _RACE.header.players.length){
    return _RACE.header.players.slice().sort(function(a,b){return (a.bike-b.bike);});
  }
  return [];
}

function _catChipInner(pl){
  var bk=String(pl.bike);
  return '<span class="rb bcol'+esc(bk)+'">'+esc(bk)+'</span>';
}

// ============================================================
// v329: 予想タブ
//   拮抗度Q と 分戦(ライン本数) を判定し、
//   conditions.json の稼働条件に一致したときだけ買い目を出す。
//   買い目は oracle_core.js (__btUnionRace) で作る。
//   端末の集計と同じ関数なので、表示と検証結果がずれない。
//   重複は排除していない (同じレースに複数の点数が出ることがある)。
// ============================================================
var _COND=null, _COND_TRIED=false;

function loadConditions(cb){
  if(_COND){ cb(_COND); return; }
  fetch("/race/conditions").then(function(r){return r.json()})
    .then(function(j){ _COND=j; cb(j); })
    .catch(function(){ _COND_TRIED=true; cb(null); });
}

function yosouQ(){
  var ps=((_RACE||{}).header||{}).players||[];
  var v=[];
  for(var i=0;i<ps.length;i++){
    var r=Number(ps[i].raw_score);
    if(!isNaN(r)) v.push(r);
  }
  if(v.length<2) return "";
  var mx=Math.max.apply(null,v);
  if(mx<=0) return "";
  var t=0;
  for(var j=0;j<v.length;j++) t+=v[j]/mx*10;
  var a=t/v.length;
  var b=[9.3637,9.1749,8.9724,8.6605];
  for(var k=0;k<b.length;k++){ if(a>=b[k]) return "Q"+(k+1); }
  return "Q5";
}

function yosouLineClass(){
  var h=(_RACE||{}).header||{};
  var ln=String(h.line_display||h.line||"");
  var parts=ln.replace(/[ー−―]/g,"-").split("-");
  var n=0;
  for(var i=0;i<parts.length;i++){ if(/[0-9]/.test(parts[i])) n++; }
  if(n<=1) return "一本棒";
  if(n===2) return "二分戦";
  if(n===3) return "三分戦";
  if(n===4) return "四分戦";
  return "細切戦";
}

function renderYosou(pane){
  if(!_RACE){ pane.innerHTML=""; return; }
  pane.innerHTML='<div class="skip-note"><span class="spin"></span>買い目を読んでいます…</div>';
  loadPicks(function(){ drawYosou(pane); });
}

// 荒れ期待度のバー。★5段階ではなく、幅と光で強さを表す。
function arareBar(star, predPay){
  var pct = Math.max(6, Math.min(100, star * 20));
  var lv = "lv" + star;
  var s = '<div class="ar-wrap">';
  s += '<div class="ar-top"><span class="ar-ttl">荒れ期待度</span>';
  s += '<span class="ar-right">';
  s += '<button class="reschk" onclick="refreshResult()">結果を確かめる</button>';
  s += '<span class="ar-val ' + lv + '">' + star + ' / 5</span>';
  s += '</span></div>';
  s += '<div class="ar-track">';
  s += '<div class="ar-fill ' + lv + '" style="width:' + pct + '%"></div>';
  // 目盛り
  var g = 1;
  while (g < 5) {
    s += '<div class="ar-tick" style="left:' + (g * 20) + '%"></div>';
    g = g + 1;
  }
  s += '</div>';
  if (predPay) {
    s += '<div class="ar-sub">想定配当 ' + Number(predPay).toLocaleString()
       + '円 相当</div>';
  }
  s += '</div>';
  return s;
}

// 結果はレースが終わってから確定する。自動で待つと遅いので、
// 押したときだけ取り直す。
function refreshResult(){
  if(!_CUR_KEY) return;
  var vn = (_CUR_KEY.split("_"))[0];
  var btn = document.querySelector(".reschk");
  if(btn){ btn.textContent = "照合中…"; btn.disabled = true; }
  var ck = DATE + "|" + vn;
  delete _VF_CACHE[ck];
  fetch("/race/vflags?date=" + DATE + "&venue=" + encodeURIComponent(vn))
    .then(function(r){ return r.json(); })
    .then(function(j){
      var fl = j.flags || {};
      _VF_CACHE[ck] = fl;
      applyVFlags(fl);
      var pane = document.getElementById("tabpane");
      if(pane) drawYosou(pane);
    })
    .catch(function(){
      if(btn){ btn.textContent = "結果を確かめる"; btn.disabled = false; }
    });
}

var _STEP_SEL = {};   // key -> 選んだ段階の番号

function selectStep(n){
  _STEP_SEL[_CUR_KEY] = n;
  var pane = document.getElementById("tabpane");
  if(pane) drawYosou(pane);
}

function comboHtml(list){
  var t = "";
  var k = 0;
  while (k < list.length) {
    var v = list[k];
    t += '<span class="yos-combo">' + esc(v.t ? v.t : v) + '</span>';
    k = k + 1;
  }
  return t;
}

// 結果が出ていれば、的中した目を緑にして印を添える。
//   3-1-245 のうち 3-1-4 が当たったなら、その 4 だけを緑にする。
function hitTriOf(){
  var rf=_RESULTMAP[_CUR_KEY];
  if(!rf || rf.finished!==true) return "";
  return String(rf.trifecta||"");
}

function formHtml(formStr, list){
  var tri = hitTriOf();
  var tp = tri ? tri.split("-") : null;
  if (formStr) {
    var segs = String(formStr).split(" / ");
    var s = "";
    var i = 0;
    while (i < segs.length) {
      var seg = segs[i];
      i = i + 1;
      var pp = seg.split("-");
      var body = "";
      if (tp && pp.length === 3) {
        // その行が的中しているか。1着2着が一致し、3着が含まれていること。
        var headOk = (pp[0] === tp[0] && pp[1] === tp[1]);
        var th = pp[2];
        var won = (headOk && th.indexOf(tp[2]) >= 0);
        // 的中した行は 3つとも緑にする。外れた行は何もしない。
        body = '<span class="' + (won ? "fhit" : "") + '">'
             + esc(pp[0]) + '</span>-'
             + '<span class="' + (won ? "fhit" : "") + '">'
             + esc(pp[1]) + '</span>-';
        var k = 0;
        while (k < th.length) {
          var c = th.charAt(k);
          k = k + 1;
          var on = (won && c === tp[2]);
          body += '<span class="' + (on ? "fhit" : "") + '">' + esc(c)
                + '</span>';
        }
        if (won) { body += '<span class="fmark">的中</span>'; }
      } else {
        body = esc(seg);
      }
      s += '<div class="yos-form">' + body + '</div>';
    }
    return s;
  }
  return '<div class="yos-row">' + comboHtml(list || []) + '</div>';
}

function drawYosou(pane){
  var p = _PICKS[_CUR_KEY];
  if(!p){
    var s0 = '<div class="yos-none">このレースの買い目はありません</div>';
    if(_PICKS_DATE===DATE && !Object.keys(_PICKS).length){
      s0 = '<div class="skip-note">この日の買い目はまだ作られていません。<br>'
         + 'GitHub Actions の生成を待ってください。</div>';
    }
    pane.innerHTML = s0;
    return;
  }

  var s = arareBar(p.star || 0, p.pred_pay || 0);

  // レースの素性
  s += '<div class="yos-head">';
  if (p.q) s += '<span class="yos-tag">' + esc(p.q) + '</span>';
  if (p.race_type) s += '<span class="yos-tag">' + esc(p.race_type) + '</span>';
  if (p.line_config) s += '<span class="yos-tag">' + esc(p.line_config) + '</span>';
  s += '</div>';

  // 狙いレース: 3条件の買い目
  if (p.target && p.points) {
    var fr = p.from || [];
    var lb = "";
    var i = 0;
    while (i < fr.length) {
      lb += '<span class="yos-tag on">' + esc(String(fr[i].label))
          + String(fr[i].points) + '</span>';
      i = i + 1;
    }
    s += '<div class="yos-card tgt"><h4><span class="tgtmark">狙い</span>　'
       + lb + '　計' + p.points + '点</h4>';
    s += formHtml(p.formation, p.combos);
    s += '</div>';
  }

  // 絞り込み6段階
  var steps = p.steps || [];
  if (steps.length) {
    var sel = _STEP_SEL[_CUR_KEY];
    if (sel === undefined || sel === null) sel = 1;   // 既定は峻別
    if (sel >= steps.length) sel = steps.length - 1;
    s += '<div class="yos-card"><h4>絞り込み</h4>';
    s += '<div class="stepbar">';
    var j2 = 0;
    while (j2 < steps.length) {
      var st = steps[j2];
      var on = (j2 === sel) ? " on" : "";
      s += '<button class="stepbtn' + on + '" onclick="selectStep('
         + j2 + ')">' + esc(st.name)
         + '<span class="stepsub">約' + st.approx + '点</span></button>';
      j2 = j2 + 1;
    }
    s += '</div>';
    var cur = steps[sel];
    if (cur) {
      s += '<div class="stepnow">' + esc(cur.name) + '　実 ' + cur.points
         + '点</div>';
      s += formHtml(cur.formation, cur.combos);
    }
    s += '</div>';
  }

  s += '<div class="yos-note">'
     + (_PICKS_META.conditions_updated
        ? '条件 ' + esc(_PICKS_META.conditions_updated) + ' 時点　' : '')
     + (_PICKS_META.generated ? '生成 ' + esc(_PICKS_META.generated) : '')
     + '</div>';
  pane.innerHTML = s;
}

function renderCathedral(pane){
  var ps=_catPlayers();
  if(!ps.length){
    pane.innerHTML='<div class="card fade"><div class="card-h"><span class="ttl">大聖堂</span></div>'
      + '<div class="card-b"><div class="nodata">出走データがありません</div></div></div>';
    return;
  }
  var html='<div class="card fade"><div class="card-h"><span class="ttl">大聖堂 — 御託宣</span></div><div class="card-b">';
  html+='<div class="cat-modes">'
    + '<button class="cat-mode'+(_CAT_MODE==="priest"?" on":"")+'" onclick="catSetMode(\'priest\')">牧師</button>'
    + '<button class="cat-mode'+(_CAT_MODE==="bishop"?" on":"")+'" onclick="catSetMode(\'bishop\')">大司教</button>'
    + '<button class="cat-mode'+(_CAT_MODE==="maria"?" on":"")+'" onclick="catSetMode(\'maria\')">聖母マリア</button>'
    + '</div>';
  html+='<div id="cat-form">'+_catFormHtml(ps)+'</div>';
  html+='<div id="cat-result" class="ora-result"></div>';
  html+='<div class="ora-foot">大聖堂は周回中並び予測×展開分岐辞書から買い目を生成する。'
    + '牧師=軸(対抗)指定、大司教=車番指定、聖母マリア=おまかせ。</div>';
  html+='</div></div>';
  pane.innerHTML=html;
}

function _catRerenderForm(){
  var box=document.getElementById("cat-form");
  if(box){ box.innerHTML=_catFormHtml(_catPlayers()); }
}

function _catFormHtml(ps){
  var html='';
  if(_CAT_MODE==="priest"){
    html+='<div class="ora-sec"><div class="ora-lbl">軸（1着に置く・1台）</div><div class="ora-chips">';
    for(var i=0;i<ps.length;i++){
      var bk=String(ps[i].bike);
      var on=(_CAT_AXIS===bk)?" on":"";
      html+='<button class="ora-chip'+on+'" onclick="catSetAxis(\''+bk+'\')">'+_catChipInner(ps[i])+'</button>';
    }
    html+='</div></div>';
    html+='<div class="ora-sec"><div class="ora-lbl">対抗（任意・1台／空=軸ながし）</div><div class="ora-chips">';
    for(var j=0;j<ps.length;j++){
      var bk2=String(ps[j].bike);
      var dis=(_CAT_AXIS===bk2);
      var on2=(_CAT_RIVAL===bk2)?" on":"";
      var cls="ora-chip"+on2+(dis?" ora-dis":"");
      html+='<button class="'+cls+'" '+(dis?'disabled':'onclick="catToggleRival(\''+bk2+'\')"')+'>'+_catChipInner(ps[j])+'</button>';
    }
    html+='</div></div>';
    var swDis=(_CAT_RIVAL===null);
    html+='<div class="ora-sec ora-row"><div class="ora-lbl">着順</div>'
      + '<div class="cat-sw'+(swDis?" cat-sw-dis":"")+'">'
      + '<button class="cat-swb'+(_CAT_RMODE==="fixed"?" on":"")+'" '+(swDis?'disabled':'onclick="catSetRmode(\'fixed\')"')+'>固定</button>'
      + '<button class="cat-swb'+(_CAT_RMODE==="renta"?" on":"")+'" '+(swDis?'disabled':'onclick="catSetRmode(\'renta\')"')+'>連対</button>'
      + '</div></div>';
  }else if(_CAT_MODE==="bishop"){
    html+='<div class="ora-sec"><div class="ora-lbl">指定車番（1〜2台／この車を含む買い目）</div><div class="ora-chips">';
    for(var k=0;k<ps.length;k++){
      var bk3=String(ps[k].bike);
      var on3=(_CAT_BIKES.indexOf(bk3)>=0)?" on":"";
      html+='<button class="ora-chip'+on3+'" onclick="catToggleBike(\''+bk3+'\')">'+_catChipInner(ps[k])+'</button>';
    }
    html+='</div></div>';
  }else{
    html+='<div class="ora-sec"><div class="cat-maria-note">聖母マリアにすべてを委ねる。車番指定なしで上位買い目を生成します。</div></div>';
  }
  html+='<div class="ora-sec ora-row"><div class="ora-lbl">点数上限</div>'
     + '<select class="ora-sel" id="catNSel" onchange="catSetN(this.value)">'+_catNSelHtml()+'</select>'
     + '<button class="ora-gen" onclick="catGenerate()">御託宣を仰ぐ</button></div>';
  return html;
}

function _catNSelHtml(){
  var opts=[3,4,5,6,8,10,12,15,20];
  var s='';
  for(var i=0;i<opts.length;i++){
    var sel=(opts[i]===_CAT_N)?" selected":"";
    s+='<option value="'+opts[i]+'"'+sel+'>'+opts[i]+'点</option>';
  }
  return s;
}

function catSetMode(m){
  _CAT_MODE=m;
  renderCathedral(document.getElementById("tabpane"));
}
function catSetAxis(bike){
  if(_CAT_AXIS===bike){ _CAT_AXIS=null; }
  else {
    _CAT_AXIS=bike;
    if(_CAT_RIVAL===bike) _CAT_RIVAL=null;
  }
  _catRerenderForm();
}
function catToggleRival(bike){
  if(_CAT_AXIS===bike) return;
  if(_CAT_RIVAL===bike){ _CAT_RIVAL=null; }
  else { _CAT_RIVAL=bike; }
  _catRerenderForm();
}
function catSetRmode(m){ _CAT_RMODE=m; _catRerenderForm(); }
function catToggleBike(bike){
  var idx=_CAT_BIKES.indexOf(bike);
  if(idx>=0){ _CAT_BIKES.splice(idx,1); }
  else {
    if(_CAT_BIKES.length>=2){ _CAT_BIKES.shift(); }
    _CAT_BIKES.push(bike);
  }
  _catRerenderForm();
}
function catSetN(v){ var n=parseInt(v,10); if(isNaN(n)||n<1)n=1; if(n>20)n=20; _CAT_N=n; }

function _catReasonMsg(reason){
  var m={
    "engine_unavailable":"大聖堂エンジンが未配置です（辞書ファイルを確認してください）",
    "dict_not_loaded":"辞書が読み込まれていません",
    "s_missing":"このレースはS値が欠損しているため予想できません",
    "no_players_info":"出走データがありません",
    "priest_no_axis":"軸を1台選んでください",
    "priest_rival_only":"対抗のみの指定はできません。軸を選んでください",
    "bishop_no_bike":"車番を1〜2台選んでください",
    "bishop_too_many":"車番は2台までです",
    "form_not_supported":"この並び形は未対応です",
    "cell_miss":"該当する展開データがありません",
    "no_candidate_after_filter":"条件に合う買い目がありませんでした"
  };
  return m[reason] || ("予想できませんでした（"+esc(reason)+"）");
}

function catGenerate(){
  var box=document.getElementById("cat-result");
  if(!box) return;
  if(_CAT_MODE==="priest" && !_CAT_AXIS){
    box.innerHTML='<div class="ora-warn">軸を1台選んでください</div>'; return;
  }
  if(_CAT_MODE==="bishop" && _CAT_BIKES.length===0){
    box.innerHTML='<div class="ora-warn">車番を1〜2台選んでください</div>'; return;
  }
  box.innerHTML='<div class="nodata">御託宣を仰いでいます…</div>';
  var url="/api/cathedral?date="+DATE+"&key="+encodeURIComponent(_CUR_KEY||"")
        + "&mode="+_CAT_MODE+"&top_n="+_CAT_N;
  if(_CAT_MODE==="priest"){
    url+="&axis="+encodeURIComponent(_CAT_AXIS);
    if(_CAT_RIVAL){ url+="&rival="+encodeURIComponent(_CAT_RIVAL)+"&rival_mode="+_CAT_RMODE; }
  }else if(_CAT_MODE==="bishop"){
    url+="&bikes="+encodeURIComponent(_CAT_BIKES.join(","));
  }
  fetch(url).then(function(r){return r.json()}).then(function(j){
    var b=document.getElementById("cat-result");
    if(!b) return;
    if(!j || !j.ok){
      b.innerHTML='<div class="ora-warn">'+_catReasonMsg(j?j.reason:"unknown")+'</div>';
      return;
    }
    var cands=j.candidates||[];
    if(!cands.length){
      b.innerHTML='<div class="ora-warn">'+_catReasonMsg("no_candidate_after_filter")+'</div>';
      return;
    }
    b.innerHTML=_catResultHtml(cands, j.metadata||{}, j.race||{});
    try{
      var _rc=j.race||{};
      _CAT_RESULT3T=_rc.result_3t||'';
      _CAT_REFUND3=Number(_rc.refund_3t||0)||0;
    }catch(e){ _CAT_RESULT3T=''; _CAT_REFUND3=0; }
    try{
      if(typeof _ORA_LAST!=='undefined'){
        _ORA_LAST=[];
        for(var i=0;i<cands.length;i++){ _ORA_LAST.push(cands[i]["3t"]); }
      }
    }catch(e){}
    try{ __catApplyOdds(); }catch(e){}
  }).catch(function(e){
    var b=document.getElementById("cat-result");
    if(b) b.innerHTML='<div class="ora-warn">通信エラー: '+esc(e)+'</div>';
  });
}

function _catResultHtml(cands, meta, race){
  race = race || {};
  var result3t = race.result_3t || "";
  var refund = race.refund_3t || 0;
  var srcLabel = (meta.source==="cache") ? "事前計算" : (meta.source==="live" ? "その場計算" : "");
  var html='<div class="cat-res-head">買い目 '+cands.length+'点';
  if(meta.total_coverage!=null){ html+=' ／ 網羅率 '+esc(meta.total_coverage)+'%'; }
  if(srcLabel){ html+=' <span class="cat-src">'+srcLabel+'</span>'; }
  html+='</div>';
  // 確定結果 + 的中判定
  var hitIndex=-1;
  if(result3t){
    for(var t=0;t<cands.length;t++){ if(cands[t]["3t"]===result3t){ hitIndex=t; break; } }
    var rb=result3t.split("-");
    var rchips='';
    for(var m=0;m<rb.length;m++){
      rchips+='<span class="rb bcol'+esc(rb[m])+'">'+esc(rb[m])+'</span>';
      if(m<rb.length-1) rchips+='<span class="cat-arrow">→</span>';
    }
    var hitTxt = (hitIndex>=0) ? ('的中 '+(hitIndex+1)+'番手'+(refund?(' ／ '+refund.toLocaleString()+'円'):'')) : '不的中';
    var hitCls = (hitIndex>=0) ? 'cat-hit' : 'cat-miss';
    html+='<div class="cat-result-bar '+hitCls+'"><span class="cat-result-lbl">結果</span>'
        + '<div class="cat-res-bikes">'+rchips+'</div>'
        + '<span class="cat-result-judge">'+hitTxt+'</span></div>';
  }
  html+='<div class="cat-res-list">';
  for(var i=0;i<cands.length;i++){
    var c=cands[i];
    var bikes=(""+c["3t"]).split("-");
    var chips='';
    for(var k=0;k<bikes.length;k++){
      chips+='<span class="rb bcol'+esc(bikes[k])+'">'+esc(bikes[k])+'</span>';
      if(k<bikes.length-1) chips+='<span class="cat-arrow">→</span>';
    }
    var rowCls = (i===hitIndex) ? ' cat-res-hit' : '';
    html+='<div class="cat-res-row'+rowCls+'"><div class="cat-res-bikes">'+chips+'</div>'
        + '<span class="ora-score od-odds cat-res-odds" data-combo="'+esc(c["3t"])+'">'
        +   '<span class="od-loading">…</span></span></div>';
  }
  html+='</div>';
  return html;
}

// maria買い目セル(.cat-res-odds[data-combo])に表示時点の3連単オッズを反映。
//   v264の __oraApplyOdds と同方式。既存の /api/odds・__oddsFmt・__oddsClass を再利用。
//   オッズ未取得(過去レース等)は「―」。的中緑枠は cat-res-hit が別途付く。
function __catApplyOdds(){
  if(!_CUR_KEY) return;
  function paint(map){
    var cells=document.querySelectorAll('.cat-res-odds[data-combo]');
    for(var i=0;i<cells.length;i++){
      var cb=cells[i].getAttribute('data-combo');
      // 的中目は払戻由来(払戻÷100)を正として表示。live取得値のズレを回避。
      if(cb && cb===_CAT_RESULT3T && _CAT_REFUND3>0){
        var od0=_CAT_REFUND3/100.0;
        cells[i].innerHTML=__oddsFmt(od0)+'<span class="odds-bai">倍</span>';
        cells[i].className='ora-score od-odds cat-res-odds '+__oddsClass(od0);
        // 的中行に緑枠を確実に付与(hitIndex経路が外れても連動させる)
        try{
          var _row=cells[i].parentNode;
          if(_row && _row.classList){ _row.classList.add('cat-res-hit'); }
        }catch(e){}
        continue;
      }
      var it=map[cb];
      if(it && it.odds!=null){
        cells[i].innerHTML=__oddsFmt(it.odds)+'<span class="odds-bai">倍</span>';
        cells[i].className='ora-score od-odds cat-res-odds '+__oddsClass(it.odds);
      }else{
        cells[i].innerHTML='<span class="od-na">―</span>';
        cells[i].className='ora-score od-odds cat-res-odds';
      }
    }
  }
  if(_ODDS_CACHE && _ODDS_CACHE.key===_CUR_KEY && _ODDS_CACHE.map){
    paint(_ODDS_CACHE.map); return;
  }
  var url="/api/odds?date="+DATE+"&key="+encodeURIComponent(_CUR_KEY);
  fetch(url).then(function(r){return r.json()}).then(function(j){
    var map={}; var items=j.items||[];
    for(var i=0;i<items.length;i++){ map[items[i].combo]=items[i]; }
    _ODDS_CACHE={key:_CUR_KEY, ok:j.ok, items:items, map:map,
                 count:j.count||0, ts:Date.now(), diag:j.diag||{}};
    paint(map);
  }).catch(function(){ /* オッズ不可なら ― のまま */ });
}

function renderOracle(pane){
  pane.innerHTML='<div class="card fade"><div class="card-h">'
    + '<span class="ttl">御告 — 買い目生成</span>'
    + '</div>'
    + '<div class="card-b"><div id="orabox"><div class="nodata">読み込み中…</div></div></div></div>';
  __oraEnsureData(function(d){
    var box=document.getElementById("orabox");
    if(!box) return;
    if(!d || !d.players || !d.players.length){
      box.innerHTML='<div class="nodata">出走データがありません</div>';
      return;
    }
    box.innerHTML=__oraFormHtml(d.players);
  });
}
function __oraEnsureData(cb){
  if(_RACE && _RACE.header && _RACE.header.players && _RACE.header.players.length){
    cb({players:_RACE.header.players}); return;
  }
  cb(null);
}
// チップ内部: 車番マーク(出走表と同色) + 下に小印 + ラベルバー
function __oraChipInner(pl){
  var bk=String(pl.bike);
  var inner='<span class="rb bcol'+esc(bk)+'">'+esc(bk)+'</span>';
  var mk=(pl.keihai_mark)?'<span class="ora-chip-mk r'+(pl.keihai_rank||9)+'">'+pl.keihai_mark+'</span>':'<span class="ora-chip-mk"></span>';
  inner+=mk;
  var lb='';
  if(pl.label_kind){ lb='<span class="ora-chip-lb '+pl.label_kind+'">'+esc(shortLabel(pl.label_text))+'</span>'; }
  else if(pl.layoff_kind){ lb='<span class="ora-chip-lb layoff">'+esc(shortLabel(pl.layoff_text))+'</span>'; }
  else { lb='<span class="ora-chip-lb"></span>'; }
  inner+=lb;
  return inner;
}
function __oraFormHtml(players){
  var ps=players.slice().sort(function(a,b){ return (a.bike-b.bike); });
  var hasKim=(_RACE && _RACE.kimari && _RACE.kimari.exists);
  var html='';
  if(!hasKim){
    html+='<div class="ora-warn">このレースは決まり手遷移データがありません。予想できません。</div>';
  }
  // 軸(複数可・軸ながし)
  html+='<div class="ora-sec"><div class="ora-lbl">軸（1着に置く・複数可＝軸ながし）</div><div class="ora-chips">';
  for(var i=0;i<ps.length;i++){
    var bk=String(ps[i].bike);
    var on=(_ORA_AXIS.indexOf(bk)>=0)?" on":"";
    html+='<button class="ora-chip'+on+'" onclick="oraSetAxis(\''+bk+'\')">'+__oraChipInner(ps[i])+'</button>';
  }
  html+='</div></div>';
  // 対抗(2着固定・任意)
  html+='<div class="ora-sec"><div class="ora-lbl">対抗（2着固定・任意／空=おまかせ）</div><div class="ora-chips">';
  for(var j=0;j<ps.length;j++){
    var bk2=String(ps[j].bike);
    var dis=(_ORA_AXIS.indexOf(bk2)>=0);
    var on2=(_ORA_RIVAL.indexOf(bk2)>=0)?" on":"";
    var cls="ora-chip"+on2+(dis?" ora-dis":"");
    html+='<button class="'+cls+'" '+(dis?'disabled':'onclick="oraToggleRival(\''+bk2+'\')"')+'>'+__oraChipInner(ps[j])+'</button>';
  }
  html+='</div></div>';
  // 点数上限(ドロップリスト)
  html+='<div class="ora-sec ora-row"><div class="ora-lbl">点数上限</div>'
     + '<select class="ora-sel" id="oraNSel" onchange="oraPickN(this.value)">'+__oraNSelHtml()+'</select>'
     + '<button class="ora-gen" onclick="oraGenerate()">御告を仰ぐ</button></div>';
  // 託宣行: 左に軸人数ドロップリスト + 右に託宣ボタン
  html+='<div class="ora-sec ora-row ora-row-alt">'
     + '<div class="ora-axsel"><span class="ora-axsel-lbl">軸人数</span>'
     + '<select class="ora-sel" id="oraAxSel" onchange="oraPickAx(this.value)">'+__oraAxSelHtml()+'</select></div>'
     + '<button class="ora-gen ora-gen-alt" onclick="oraOmakase()">託宣を仰ぐ</button></div>';
  html+='<div id="ora-result" class="ora-result"></div>';
  html+='<div class="ora-foot">託宣は3柱合議で軸選定。'
     + '柱A=決まり手遷移×複合力、柱B=score別着順実績（絶対1着率×揺らぎ×母数補正）、柱C=頻出買い目1着車。'
     + '3柱を等重みで合成し上位3軸まで選出。買い目は決まり手遷移シナリオで生成し着順揺らぎ補正。</div>';
  return html;
}
function oraSetAxis(bike){
  var idx=_ORA_AXIS.indexOf(bike);
  if(idx>=0){ _ORA_AXIS.splice(idx,1); }
  else {
    _ORA_AXIS.push(bike);
    var ri=_ORA_RIVAL.indexOf(bike);
    if(ri>=0) _ORA_RIVAL.splice(ri,1);   // 軸にした車は対抗から外す
  }
  __oraRerenderForm();
}
function oraToggleRival(bike){
  if(_ORA_AXIS.indexOf(bike)>=0) return;   // 軸の車は対抗に選べない
  var idx=_ORA_RIVAL.indexOf(bike);
  if(idx>=0) _ORA_RIVAL.splice(idx,1); else _ORA_RIVAL.push(bike);
  __oraRerenderForm();
}
function oraSetN(v){ var n=parseInt(v,10); if(isNaN(n)||n<1)n=1; if(n>50)n=50; _ORA_N=n; }
// 点数選択(1-20 連続のドロップリスト)
function __oraNSelHtml(){
  var h='';
  for(var v=1; v<=20; v++){
    var sel=(v===_ORA_N)?" selected":"";
    h+='<option value="'+v+'"'+sel+'>'+v+'点</option>';
  }
  return h;
}
function oraPickN(v){
  var n=parseInt(v,10); if(isNaN(n)||n<1)n=1; if(n>20)n=20;
  _ORA_N=n;
}
// 託宣の軸人数(自動/1/2/3 のドロップリスト)
function __oraAxSelHtml(){
  var opts=[{v:0,t:"自動"},{v:1,t:"1人"},{v:2,t:"2人"},{v:3,t:"3人"}];
  var h='';
  for(var i=0;i<opts.length;i++){
    var sel=(opts[i].v===_ORA_OMK_AXIS)?" selected":"";
    h+='<option value="'+opts[i].v+'"'+sel+'>'+opts[i].t+'</option>';
  }
  return h;
}
function oraPickAx(v){
  var n=parseInt(v,10); if(isNaN(n)||n<0)n=0; if(n>3)n=3;
  _ORA_OMK_AXIS=n;
}
function __oraRerenderForm(){
  __oraEnsureData(function(d){
    var box=document.getElementById("orabox");
    if(box && d && d.players) box.innerHTML=__oraFormHtml(d.players);
  });
}

// 御告/託宣の買い目に、表示時点のオッズを反映する。
//   .ora-score[data-combo] セルを探し、combo→odds で書き換える。
//   オッズ未取得なら /api/odds を引いてから適用(キャッシュ流用)。
function __oraApplyOdds(){
  if(!_CUR_KEY) return;
  function paint(map){
    var cells=document.querySelectorAll('.ora-score[data-combo]');
    for(var i=0;i<cells.length;i++){
      var cb=cells[i].getAttribute('data-combo');
      var it=map[cb];
      if(it && it.odds!=null){
        cells[i].innerHTML=__oddsFmt(it.odds)+'<span class="odds-bai">倍</span>';
        cells[i].className='ora-score od-odds '+__oddsClass(it.odds);
      }else{
        cells[i].innerHTML='<span class="od-na">―</span>';
        cells[i].className='ora-score od-odds';
      }
    }
  }
  if(_ODDS_CACHE && _ODDS_CACHE.key===_CUR_KEY && _ODDS_CACHE.map){
    paint(_ODDS_CACHE.map); return;
  }
  var url="/api/odds?date="+DATE+"&key="+encodeURIComponent(_CUR_KEY);
  fetch(url).then(function(r){return r.json()}).then(function(j){
    var map={}; var items=j.items||[];
    for(var i=0;i<items.length;i++){ map[items[i].combo]=items[i]; }
    _ODDS_CACHE={key:_CUR_KEY, ok:j.ok, items:items, map:map,
                 count:j.count||0, ts:Date.now(), diag:j.diag||{}};
    paint(map);
  }).catch(function(){ /* オッズ不可なら数値非表示のまま */ });
}

function oraGenerate(){
  __oraEnsureData(function(d){
    var res=document.getElementById("ora-result");
    if(!res) return;
    if(!d || !d.players){ res.innerHTML='<div class="nodata">データなし</div>'; return; }
    var pred=__oraPredict(d);
    if(pred.error){ res.innerHTML='<div class="ora-warn">'+esc(pred.error)+'</div>'; return; }
    var topN=pred.combos.slice(0, _ORA_N);
    _ORA_LAST=[];
    for(var t=0;t<topN.length;t++){ _ORA_LAST.push(topN[t].b.join('-')); }
    var html='<div class="ora-rhead">御告の買い目（3連単・上位'+topN.length+'点）</div>';
    html+='<div class="ora-list">';
    var hitCombo=__oraHitCombo();
    for(var r=0;r<topN.length;r++){
      var c=topN[r];
      var cb=c.b.join('-');
      var hitCls='';
      if(hitCombo && c.b && c.b.length>=3
          && String(c.b[0])===hitCombo[0]
          && String(c.b[1])===hitCombo[1]
          && String(c.b[2])===hitCombo[2]){
        hitCls=' ora-item-hit';
      }
      html+='<div class="ora-item'+hitCls+'">'
        + '<span class="ora-rank">'+(r+1)+'</span>'
        + '<span class="ora-bikes">'
        +   '<span class="ora-bk bcol'+esc(c.b[0])+'">'+esc(c.b[0])+'</span>'
        +   '<span class="ora-arr">&#8594;</span>'
        +   '<span class="ora-bk bcol'+esc(c.b[1])+'">'+esc(c.b[1])+'</span>'
        +   '<span class="ora-arr">&#8594;</span>'
        +   '<span class="ora-bk bcol'+esc(c.b[2])+'">'+esc(c.b[2])+'</span>'
        + '</span>'
        + '<span class="ora-score od-odds" data-combo="'+esc(cb)+'"><span class="od-loading">…</span></span>'
        + '</div>';
    }
    html+='</div>';
    html+='<div class="ora-foot">数値は表示時点の3連単オッズ（倍）。</div>';
    res.innerHTML=html;
    __oraApplyOdds();
  });
}

// ============ 託宣(おまかせ生成) モード: バランス ============
// 点数指定だけで軸も含め自動生成。本線50% / 抑え30% / 穴目20%。
//   本線: 複合力×1着遷移が高い軸の堅い決着。勝負弱が頭なら減点。
//   抑え: 本線軸の2着3着違い等、中位の保険。穴サイドを弱く加点。
//   穴目: 揺らぎ大の組を基本に、穴サイド(hit/den実績)を強く加点、離脱明けは減点。
function oraOmakase(){
  __oraEnsureData(function(d){
    var box=document.getElementById("orabox");
    var res=document.getElementById("ora-result");
    if(!d || !d.players){ if(res) res.innerHTML='<div class="nodata">データなし</div>'; return; }
    var pred=__oraOmakasePredict(d, "balance", _ORA_N);
    if(pred.error){
      if(res) res.innerHTML='<div class="ora-warn">'+esc(pred.error)+'</div>';
      else if(box) box.innerHTML='<div class="ora-warn">'+esc(pred.error)+'</div>';
      return;
    }
    // 結果欄が無ければフォーム描画してから
    if(!res){
      if(box) box.innerHTML=__oraFormHtml(d.players);
      res=document.getElementById("ora-result");
      if(!res) return;
    }
    res.innerHTML=__oraOmakaseHtml(pred);
    _ORA_LAST=[];
    var L=pred.layers||{};
    var lyrs=[L.honsen||[], L.osae||[], L.ana||[]];
    for(var li=0;li<lyrs.length;li++){
      for(var k=0;k<lyrs[li].length;k++){ _ORA_LAST.push(lyrs[li][k].b.join('-')); }
    }
    __oraApplyOdds();
  });
}

// ============================================================
//  実績バックテスト
//   期間内のキャッシュ済みレースに対し託宣を再現し、
//   1点〜20点買いの的中率・回収率を集計する。
// ============================================================
var _BT_MAXN=20;            // 集計する最大点数
var _BT_AXIS=0;             // 実績集計の軸人数(0=自動 / 1,2,3)
var _BT_LAST_SUMMARY=null;  // 直近の集計結果(明細の点数切替で再利用)
var _BT_LAST_BUNDLE=null;   // 8パターン集計束
var _BT_TAB_MAIN="omk";     // 大タブ: omk(託宣) / ora(御告) / vfa(検証A) / vfb(検証B)
var _BT_TAB_SUB="0";        // 子タブ: 託宣 0/1/2/3, 御告 honmei/taikou/tanana/renka
var _BT_BODY_ID="btResult_body";  // 集計本体の描画先ID
var _BT_DETAIL_N=1;         // 明細表示中の点数
var _BT_RUNNING=false;
var _BT_CACHE={};           // key "from_to" -> 集計結果(再表示用)

// 託宣の買い目を「順位順の combo 配列」に展開する。
//   honsen → osae → ana の順。各 combo は "a-b-c"。
// ※v322で検証タブは和集合方式に変更したため、以下2関数は現在未使用。
//   ただし過去に保存した期待値方式のデータ(evFiltered付き)を
//   明細表示するために、判定ロジックは __btRenderDetail に残してある。
//   関数自体も将来の再利用に備えて残す。
// v315: 期待値評価。全候補でスコアを正規化して確率とし(=B案)、
//   確定オッズと掛けて買い目1点ごとのEVを出す。
//   上位n点のうち EV>=1.0 のものだけ買った場合を集計する。
function __btEvalRaceEV(racePayload, trifecta, refund, oddsMap){
  var saved=_RACE;
  var savedAx=_ORA_OMK_AXIS;
  var res={ok:false, combos:[], evs:[], odds:[], probs:[], hitIndex:-1, refund:0};
  try{
    _RACE=racePayload;
    _ORA_OMK_AXIS=_BT_AXIS;
    if(!racePayload || !racePayload.header || !racePayload.header.players) return res;
    if(!oddsMap) return res;
    var d={players:racePayload.header.players};
    // 全候補を得るため点数上限を大きくする(7車立ての全順列=210)
    var pred=__oraOmakasePredict(d, "balance", 210);
    if(pred.error) return res;
    var L=pred.layers||{};
    var lyrs=[L.honsen||[], L.osae||[], L.ana||[]];
    var seen={}, combos=[], scores=[], tot=0;
    for(var li=0; li<lyrs.length; li++){
      for(var k=0; k<lyrs[li].length; k++){
        var it=lyrs[li][k];
        var c=it.b.join('-');
        if(seen[c]) continue;
        seen[c]=1;
        var sc=(typeof it.sc==="number" && isFinite(it.sc) && it.sc>0)?it.sc:0;
        combos.push(c); scores.push(sc); tot+=sc;
      }
    }
    if(!combos.length || tot<=0) return res;
    res.odds=[]; res.probs=[];
    for(var i2=0;i2<combos.length;i2++){
      var p=scores[i2]/tot;                 // 全候補で正規化した絶対確率
      var od=oddsMap[combos[i2]];
      var odv=(typeof od==="number" && od>0)?od:0;
      var ev=odv>0?(p*odv):0;
      res.evs.push(ev);
      res.odds.push(odv);
      res.probs.push(p);
    }
    res.combos=combos;
    res.ok=true;
    if(trifecta){
      for(var h=0;h<combos.length;h++){
        if(combos[h]===trifecta){ res.hitIndex=h; res.refund=refund||0; break; }
      }
    }
  }catch(e){ res.ok=false; }
  finally{ _RACE=saved; _ORA_OMK_AXIS=savedAx; }
  return res;
}

// EV>=1.0 の買い目だけ買った場合を集計する。aggの形は従来と同一。
function __btAccumEV(agg, ev, meta){
  agg.races++;
  var navail=ev.combos.length;
  // v317: 候補上位n点のうち「実際に買った点数」を n ごとに記録する。
  //   EVフィルタは買い目を間引くので、全レースにn点賭けた前提で
  //   明細や脚注を計算すると数字が食い違う(v316までの不具合)。
  var boughtN=[], hitAtN=[];
  for(var n=1;n<=_BT_MAXN;n++){
    var lim=(n<=navail)?n:navail;
    var bo=0, hi=false;
    for(var q=0;q<lim;q++){
      if(ev.evs[q]>=1.0){ bo++; if(ev.hitIndex===q) hi=true; }
    }
    boughtN.push(bo); hitAtN.push(hi?1:0);
  }
  // v318: 検証用に買い目の明細を残す。
  //   どれが託宣の候補で、どれをEVで残したかを画面で確認できるようにする。
  //   上位20点までに限定(保存サイズを抑えるため)。
  var picks=[];
  var plim=(navail<20)?navail:20;
  for(var pi=0; pi<plim; pi++){
    picks.push({
      c:ev.combos[pi],
      ev:Math.round((ev.evs[pi]||0)*1000)/1000,
      od:Math.round((ev.odds?(ev.odds[pi]||0):0)*10)/10,
      p:Math.round((ev.probs?(ev.probs[pi]||0):0)*100000)/100000
    });
  }
  agg.detail.push({
    date:meta.date, key:meta.key, trifecta:meta.trifecta, venue:meta.venue||"",
    post:meta.post||"", raceNo:meta.raceNo||"",
    hitIndex:ev.hitIndex, refund:ev.refund, navail:navail,
    boughtN:boughtN, hitAtN:hitAtN, evFiltered:1, picks:picks,
    axisMark:meta.axisMark, axisBike:meta.axisBike,
    labAna:meta.labAna, labWeak:meta.labWeak, labLayoff:meta.labLayoff
  });
  for(var n=1;n<=_BT_MAXN;n++){
    var lim=(n<=navail)?n:navail;
    if(lim<=0) continue;
    var bought=0, hit=false;
    for(var i=0;i<lim;i++){
      if(ev.evs[i]>=1.0){
        bought++;
        if(ev.hitIndex===i) hit=true;
      }
    }
    if(bought<=0) continue;
    agg.betN[n-1]+=bought*100;
    if(hit){ agg.hitN[n-1]++; agg.retN[n-1]+=ev.refund; }
  }
}

// v322: 複数系列の買い目を重ねる(重複は1点にまとめる)。
//   系列ごとに上位k点を取り、同じ買い目は1点として数える。
//   4系列が同じ買い目を出す = 自信がある → 点数が少なくなる
//   意見が割れる → 点数が増える
//   つまり「合議度」そのものが点数に反映される。
// ============================================================
// v329: 集計の明細に「どんなレースだったか」を持たせる。
//   後から「賭けなくていいレース」を分類するための材料。
//   payload から作れるものだけを使う (追加の通信はしない)。
//
//   q        拮抗度 Q1-5
//   lineCls  分戦 (二分戦/三分戦/…)
//   grade    F1 / G3 など
//   cls      チャレンジ / A級 / S級 / ガールズ
//   kind     予選 / 準決勝 / 決勝 / 特選 …
//   line     ライン構成            例 123-45-67
//   rsOrder  ライン構成順のrawscore順位  例 214-35-76
//   stOrder  ライン構成順の戦術         例 逃差差-逃差-捲マ
//   sOrder   ライン構成順のS回数        例 213-60-42
//   bOrder   ライン構成順のB回数        例 710-30-21
// ============================================================
function __btRaceMeta(payload){
  var out={q:"",lineCls:"",grade:"",cls:"",kind:"",line:"",
           rsOrder:"",stOrder:"",sOrder:"",bOrder:"",
           kimari1:"",kimari2:"",lap:""};
  try{
    var hdr=(payload&&payload.header)?payload.header:{};
    var ps=hdr.players||[];
    var lineStr=String(hdr.line_display||hdr.line||"");
    out.line=lineStr;

    // 拮抗度Q (競走得点と直近着順から出した raw_score を使う)
    var vals=[];
    for(var i=0;i<ps.length;i++){
      var rv=Number(ps[i].raw_score);
      if(!isNaN(rv)) vals.push(rv);
    }
    if(vals.length>=2){
      var mx=Math.max.apply(null,vals);
      if(mx>0){
        var t=0;
        for(var j=0;j<vals.length;j++) t+=vals[j]/mx*10;
        var a=t/vals.length;
        var bd=[9.3637,9.1749,8.9724,8.6605];
        out.q="Q5";
        for(var k=0;k<bd.length;k++){ if(a>=bd[k]){ out.q="Q"+(k+1); break; } }
      }
    }

    // 分戦
    var chunks=[];
    var parts=lineStr.replace(/[ー−―]/g,"-").split("-");
    for(var p2=0;p2<parts.length;p2++){
      var digs=String(parts[p2]).replace(/[^0-9]/g,"");
      if(digs) chunks.push(digs);
    }
    var nL=chunks.length;
    out.lineCls = (nL<=1)?"一本棒":(nL===2)?"二分戦":(nL===3)?"三分戦"
                 :(nL===4)?"四分戦":"細切戦";

    // グレード / 級班 / 種別
    out.grade=String(hdr.grade||"");
    var rk=String(hdr.race_kind||"");
    if(rk.indexOf("ガールズ")>=0) out.cls="ガールズ";
    else if(rk.indexOf("チャレンジ")>=0) out.cls="チャレンジ";
    else if(rk.indexOf("Ｓ級")>=0||rk.indexOf("S級")>=0) out.cls="S級";
    else if(rk.indexOf("Ａ級")>=0||rk.indexOf("A級")>=0) out.cls="A級";
    var kinds=["準決勝","決勝","特選","選抜","優秀","予選","一般"];
    for(var ki=0;ki<kinds.length;ki++){
      if(rk.indexOf(kinds[ki])>=0){ out.kind=kinds[ki]; break; }
    }

    // 車番 -> 各値
    var byBike={};
    for(var m=0;m<ps.length;m++){ byBike[String(ps[m].bike)]=ps[m]; }
    // rawscore順位 (高い順に1位)
    var srt=ps.slice().sort(function(x,y){
      return (Number(y.raw_score)||0)-(Number(x.raw_score)||0); });
    var rank={};
    for(var r2=0;r2<srt.length;r2++){ rank[String(srt[r2].bike)]=r2+1; }

    function mapLine(fn){
      var segs=[];
      for(var c=0;c<chunks.length;c++){
        var seg="";
        for(var d2=0;d2<chunks[c].length;d2++){
          seg+=fn(byBike[chunks[c][d2]], chunks[c][d2]);
        }
        segs.push(seg);
      }
      return segs.join("-");
    }
    out.rsOrder=mapLine(function(p3,bk){
      var v=rank[bk]; return (v==null)?"?":String(v); });
    // 値が無いときは "-" にする。0 と混同すると
    // 「Sを取っていない」のか「データが無い」のか分からなくなる。
    // S/B は選手ごとに . で区切る。
    //   区切らないと 1400-50-133 のようになり、2桁が混ざると
    //   どこまでが1人分か分からなくなる。
    function mapLineSep(fn){
      var segs=[];
      for(var c2=0;c2<chunks.length;c2++){
        var one=[];
        for(var d3=0;d3<chunks[c2].length;d3++){
          one.push(fn(byBike[chunks[c2][d3]], chunks[c2][d3]));
        }
        segs.push(one.join("."));
      }
      return segs.join("-");
    }
    out.sOrder=mapLineSep(function(p3){
      if(!p3) return "-";
      var v=p3.s_cnt; return (v==null)?"-":String(v); });
    out.bOrder=mapLineSep(function(p3){
      if(!p3) return "-";
      var v=p3.b_cnt; return (v==null)?"-":String(v); });
    // 戦術は1文字なので区切らない
    out.stOrder=mapLine(function(p3){
      if(!p3) return "?";
      return String(p3.style||"?"); });

    // 決まり手 (1着・2着)
    var rr=(payload&&payload.race_result)?payload.race_result:null;
    var rlist=(rr&&rr.result)?rr.result:[];
    var k1="", k2="";
    for(var q2=0;q2<rlist.length;q2++){
      if(rlist[q2].rank===1) k1=String(rlist[q2].finish||"");
      else if(rlist[q2].rank===2) k2=String(rlist[q2].finish||"");
    }
    out.kimari1=k1; out.kimari2=k2;

    // 周回中の並び (DBのlap)。
    //   lap は {周回中, 赤板, 打鐘, ホーム, バック} の辞書で、
    //   各局面は [{bike, x, y}] の配列 (x=前後の位置)。
    //   周回中の順に見て、ラインごとにまとめて - で区切る。
    //   例 周回順 2,5,4,6,3,1,7 / ライン 631-254-7 -> 254-631-7
    function lapByLine(arr){
      if(!arr || Object.prototype.toString.call(arr)!=="[object Array]") return "";
      var ok=arr.slice().sort(function(a,b){
        var ax=Number(a.x)||0, bx=Number(b.x)||0;
        if(ax!==bx) return ax-bx;
        return (Number(a.y)||0)-(Number(b.y)||0);
      });
      // 車番 -> 所属ライン番号
      var lineOf={};
      for(var c3=0;c3<chunks.length;c3++){
        for(var d4=0;d4<chunks[c3].length;d4++){ lineOf[chunks[c3][d4]]=c3; }
      }
      var seen=[], bucket={};
      for(var i3=0;i3<ok.length;i3++){
        var bk3=String(ok[i3].bike);
        var li=lineOf[bk3];
        if(li==null) li="x"+bk3;   // ラインに含まれない車番はそれ単独で扱う
        if(bucket[li]==null){ bucket[li]=[]; seen.push(li); }
        bucket[li].push(bk3);
      }
      var segs=[];
      for(var j3=0;j3<seen.length;j3++){ segs.push(bucket[seen[j3]].join("")); }
      return segs.join("-");
    }
    var lp=hdr.lap;
    if(lp && typeof lp==="object"){
      out.lap=lapByLine(lp["周回中"]);
    } else {
      out.lap="";
    }
  }catch(e){}
  return out;
}

function __btUnionRace(racePayload, trifecta, refund, axisList){
  var res = {ok:false, byN:[], detail:[]};
  var saved = _BT_AXIS;
  var savedOmk = _ORA_OMK_AXIS;
  try{
    // 系列ごとの買い目リストを作る
    var lists = [];
    var ai = 0;
    while(ai < axisList.length){
      var ax = axisList[ai];
      ai = ai + 1;
      _BT_AXIS = ax;
      _ORA_OMK_AXIS = ax;
      var ev = __btEvalRace(racePayload, trifecta, refund);
      if(ev.ok && ev.combos && ev.combos.length){
        lists.push({ax:ax, combos:ev.combos});
      } else {
        lists.push({ax:ax, combos:[]});
      }
    }
    if(!lists.length) return res;

    // k=1..MAXN それぞれで和集合を作る
    var n = 1;
    while(n <= _BT_MAXN){
      var seen = {};
      var uni = [];
      var per = [];
      var li = 0;
      while(li < lists.length){
        var L = lists[li];
        li = li + 1;
        var lim = (n <= L.combos.length) ? n : L.combos.length;
        per.push({ax:L.ax, n:lim});
        var q = 0;
        while(q < lim){
          var cb = L.combos[q];
          q = q + 1;
          if(!seen[cb]){
            seen[cb] = 1;
            uni.push(cb);
          }
        }
      }
      var hitAt = -1;
      var qq = 0;
      while(qq < uni.length){
        if(uni[qq] === trifecta){ hitAt = qq; break; }
        qq = qq + 1;
      }
      res.byN.push({pts:uni.length, hit:(hitAt >= 0), per:per,
                    combos:uni});
      n = n + 1;
    }
    res.ok = true;
  }catch(e){ res.ok = false; }
  finally{ _BT_AXIS = saved; _ORA_OMK_AXIS = savedOmk; }
  return res;
}

// 和集合の結果を集計器に入れる。
// v329: レースの素性を明細行にも載せる。
//   detail は項目を固定で並べているので、ここで足さないと捨てられる。
function __btCopyMeta(row, meta){
  if(!row || !meta) return row;
  row.q=meta.q||""; row.lineCls=meta.lineCls||"";
  row.grade=meta.grade||""; row.cls=meta.cls||""; row.kind=meta.kind||"";
  row.line=meta.line||""; row.rsOrder=meta.rsOrder||"";
  row.stOrder=meta.stOrder||""; row.sOrder=meta.sOrder||"";
  row.bOrder=meta.bOrder||"";
  row.kimari1=meta.kimari1||""; row.kimari2=meta.kimari2||"";
  row.lap=meta.lap||"";
  return row;
}

function __btAccumUnion(agg, ur, meta){
  agg.races++;
  var pts = [];
  var hits = [];
  var i = 0;
  while(i < ur.byN.length){
    pts.push(ur.byN[i].pts);
    hits.push(ur.byN[i].hit ? 1 : 0);
    i = i + 1;
  }
  // ★v323修正: v322は k=6 の買い目リストだけを保存し、表示時に
  //   選んだN点ぶんを先頭から切り出していた。そのためNが6以外だと
  //   **実際に買った買い目と表示が一致しなかった**
  //   (的中しているのに一覧に的中目が出ないケースが発生)。
  //   N別のリストと系列内訳をそれぞれ保存する。
  var combosByN = [];
  var perByN = [];
  var z = 0;
  while(z < ur.byN.length){
    combosByN.push(ur.byN[z].combos.slice(0, 30));
    perByN.push(ur.byN[z].per);
    z = z + 1;
  }
  agg.detail.push(__btCopyMeta({
    date:meta.date, key:meta.key, trifecta:meta.trifecta,
    venue:meta.venue||"", post:meta.post||"", raceNo:meta.raceNo||"",
    refund:meta.refundAll||0, unionPts:pts, unionHit:hits,
    unionPerN:perByN,
    unionCombosN:combosByN,
    unionFiltered:1,
    axisMark:meta.axisMark, axisBike:meta.axisBike,
    labAna:meta.labAna, labWeak:meta.labWeak, labLayoff:meta.labLayoff
  }, meta));
  i = 0;
  while(i < ur.byN.length && i < _BT_MAXN){
    var b = ur.byN[i];
    if(b.pts > 0){
      agg.betN[i] += b.pts * 100;
      if(b.hit){
        agg.hitN[i]++;
        agg.retN[i] += (meta.refundAll || 0);
      }
    }
    i = i + 1;
  }
}

// v324: 検証C = 検証Bのレース選別と軸配分をそのまま使い、
//   2着3着だけを酒場フォーメーション順に置き換える。
//
//   例) 検証B(1点)の買い目が
//         1-2-5, 1-5-2, 2-1-5, 2-5-1, 6-4-1
//       → 軸ごとの点数は 1軸=2点 / 2軸=2点 / 6軸=1点
//
//       酒場フォーメーションが
//         1軸: 1-5-2376, 1-2-53
//         2軸: 2-1-5237, 2-1-56
//         6軸: 6-1-5, 6-5-123, 6-7-12
//       なら、各軸の上から必要点数だけ取って
//         1-5-23 (2点), 2-1-52 (2点), 6-1-5 (1点)
//       合計5点 = 検証Bと同じ点数・同じ軸配分。
//
//   つまり「どのレースを買うか」「どの車を軸にするか」「何点買うか」は
//   検証Bのまま。**2着3着の選び方だけを酒場に差し替える。**

// フォーメーション文字列 "1-5-23" を ["1-5-2","1-5-3"] へ展開する。
function __tavExpand(form){
  var out=[];
  if(!form || typeof form!=="string") return out;
  var parts=form.split("-");
  if(parts.length!==3) return out;
  var a=parts[0], b=parts[1], cs=parts[2];
  var i=0;
  while(i<cs.length){
    out.push(a+"-"+b+"-"+cs.charAt(i));
    i=i+1;
  }
  return out;
}

// 酒場の全フォーメーションを軸(1着車)ごとに展開順で並べる。
//   返り値: {軸車番: [買い目, ...]}  上から並んだ順
function __tavByAxis(racePayload){
  var out={};
  if(!racePayload) return out;
  var pats=racePayload.patterns||[];
  var i=0;
  while(i<pats.length){
    var p=pats[i];
    i=i+1;
    if(!p || !p.formations || !p.formations.length) continue;
    var wb=String(p.winner_bike||"");
    if(!wb) continue;
    var lst=out[wb];
    if(!lst){ lst=[]; out[wb]=lst; }
    var k=0;
    while(k<p.formations.length){
      var ex=__tavExpand(p.formations[k]);
      k=k+1;
      var q=0;
      while(q<ex.length){
        // 同じ買い目の重複は入れない
        if(lst.indexOf(ex[q])<0) lst.push(ex[q]);
        q=q+1;
      }
    }
  }
  return out;
}

// 検証Bの買い目リストから「軸ごとの点数」を数える(出現順を保つ)
function __axisCounts(combos){
  var order=[];
  var cnt={};
  var i=0;
  while(i<combos.length){
    var c=combos[i];
    i=i+1;
    var ax=String(c).split("-")[0];
    if(!ax) continue;
    if(cnt[ax]===undefined){ cnt[ax]=0; order.push(ax); }
    cnt[ax]=cnt[ax]+1;
  }
  return {order:order, cnt:cnt};
}

// 検証C: 検証Bの和集合から軸配分を取り、酒場順で買い目を作り直す。
function __btTavernRace(racePayload, trifecta, refund, axisList){
  var res={ok:false, byN:[]};
  var ur=__btUnionRace(racePayload, trifecta, refund, axisList);
  if(!ur.ok) return res;
  var tav=__tavByAxis(racePayload);
  var hasTav=false;
  for(var kk in tav){ if(tav.hasOwnProperty(kk)){ hasTav=true; break; } }
  if(!hasTav) return res;   // 酒場が無いレースは対象外

  var n=0;
  while(n<ur.byN.length){
    var b=ur.byN[n];
    n=n+1;
    var ac=__axisCounts(b.combos);
    var picked=[];
    var seen={};
    var oi=0;
    while(oi<ac.order.length){
      var ax=ac.order[oi];
      oi=oi+1;
      var need=ac.cnt[ax];
      var src=tav[ax];
      if(!src){
        // 酒場にその軸が無い場合は検証Bの買い目をそのまま使う
        var z=0;
        var got=0;
        while(z<b.combos.length && got<need){
          var cb=b.combos[z];
          z=z+1;
          if(String(cb).split("-")[0]!==ax) continue;
          if(!seen[cb]){ seen[cb]=1; picked.push(cb); got=got+1; }
        }
        continue;
      }
      var t=0;
      var take=0;
      while(t<src.length && take<need){
        var cc=src[t];
        t=t+1;
        if(!seen[cc]){ seen[cc]=1; picked.push(cc); take=take+1; }
      }
    }
    var hitAt=-1;
    var q2=0;
    while(q2<picked.length){
      if(picked[q2]===trifecta){ hitAt=q2; break; }
      q2=q2+1;
    }
    res.byN.push({pts:picked.length, hit:(hitAt>=0), combos:picked,
                  per:b.per, srcPts:b.pts});
  }
  res.ok=true;
  return res;
}

function __btOrderedCombos(pred){
  var out=[];
  var seen={};
  var L=pred.layers||{};
  var lyrs=[L.honsen||[], L.osae||[], L.ana||[]];
  for(var li=0; li<lyrs.length; li++){
    for(var k=0; k<lyrs[li].length; k++){
      var c=lyrs[li][k].b.join('-');
      if(seen[c]) continue;
      seen[c]=1;
      out.push(c);
    }
  }
  return out;
}

// 1レースを評価。_RACE を一時差し替えして託宣を再現。
//   返り値: {ok, combos:[...], hitIndex:(0始まり,的中した順位/なければ-1), refund:int}
function __btEvalRace(racePayload, trifecta, refund){
  var saved=_RACE;
  var savedAx=_ORA_OMK_AXIS;
  var res={ok:false, combos:[], hitIndex:-1, refund:0, axisMark:"", axisBike:"", axisLabel:"", labAna:false, labWeak:false, labLayoff:false};
  try{
    _RACE=racePayload;
    _ORA_OMK_AXIS=_BT_AXIS;
    if(!racePayload || !racePayload.header || !racePayload.header.players){ return res; }
    var players=racePayload.header.players;
    // レース内に存在するラベル種別(穴/弱/離)を収集
    for(var lp=0; lp<players.length; lp++){
      var lk=players[lp].label_kind;
      if(lk==="ana") res.labAna=true;
      else if(lk==="weak") res.labWeak=true;
      if(players[lp].layoff_kind==="layoff") res.labLayoff=true;
    }
    var d={players:players};
    var pred=__oraOmakasePredict(d, "balance", _BT_MAXN);
    if(pred.error){ res.error=pred.error; return res; }
    var combos=__btOrderedCombos(pred);
    res.ok=true;
    res.combos=combos;
    // 確定3連単の1着車の予想印(keihai_mark)とラベル(穴/弱/離)を取得
    if(trifecta){
      var firstBike=String(trifecta).split("-")[0];
      res.axisBike=firstBike;
      for(var p=0;p<players.length;p++){
        if(String(players[p].bike)===firstBike){
          res.axisMark=players[p].keihai_mark||"";
          // ラベル: 離脱明 > 穴 > 弱 の優先で1つ
          var pl=players[p];
          if(pl.layoff_kind==="layoff") res.axisLabel="離";
          else if(pl.label_kind==="ana") res.axisLabel="穴";
          else if(pl.label_kind==="weak") res.axisLabel="弱";
          else res.axisLabel="";
          break;
        }
      }
      for(var i=0;i<combos.length;i++){
        if(combos[i]===trifecta){ res.hitIndex=i; res.refund=refund||0; break; }
      }
    }
  }catch(e){
    res.error=String(e);
  }finally{
    _RACE=saved;
    _ORA_OMK_AXIS=savedAx;
  }
  return res;
}

// ============================================================
// エラー診断ログ
//   logError(category, detail) でエラーを記録。
//   未読数を赤バッジ表示。コピペ用整形・全既読対応。
// ============================================================
var _ERRLOG=[];        // {id, ts, cat, detail, read}
var _ERRLOG_SEQ=0;

function logError(cat, detail){
  try{
    _ERRLOG_SEQ++;
    var d=detail;
    if(typeof d!=='string'){ try{ d=JSON.stringify(d,null,2); }catch(e){ d=String(d); } }
    _ERRLOG.unshift({id:_ERRLOG_SEQ, ts:new Date(), cat:String(cat||'error'),
                     detail:String(d||''), read:false});
    if(_ERRLOG.length>200) _ERRLOG.length=200;  // 上限
    errlogUpdateBadges();
  }catch(e){}
}

function errlogUnreadCount(){
  var n=0;
  for(var i=0;i<_ERRLOG.length;i++){ if(!_ERRLOG[i].read) n++; }
  return n;
}
function errlogUpdateBadges(){
  var n=errlogUnreadCount();
  var txt=(n>99?'99+':String(n));
  var b1=document.getElementById('menuBtnBadge');
  var b2=document.getElementById('errlogMenuBadge');
  if(b1){ b1.textContent=(n>0?txt:''); b1.className='err-badge'+(n>0?' on':''); }
  if(b2){ b2.textContent=(n>0?txt:''); b2.className='err-badge'+(n>0?' on':''); }
}

function __errlogFmtTs(d){
  function z(x){ return (x<10?'0':'')+x; }
  return d.getFullYear()+'-'+z(d.getMonth()+1)+'-'+z(d.getDate())
       + ' '+z(d.getHours())+':'+z(d.getMinutes())+':'+z(d.getSeconds());
}
// 1件をコピペ用テキストに整形
function __errlogPlain(e){
  return '【'+e.cat+'】 '+__errlogFmtTs(e.ts)+'\n'+e.detail;
}

function errlogOpen(){
  closeMenu();
  errlogRender();
  document.getElementById('errlogOverlay').style.display='block';
}
function errlogClose(){
  document.getElementById('errlogOverlay').style.display='none';
}

function errlogRender(){
  var body=document.getElementById('errlogBody');
  if(!body) return;
  if(!_ERRLOG.length){
    body.innerHTML='<div class="errlog-empty">エラーlog はありません</div>';
    return;
  }
  var h='';
  for(var i=0;i<_ERRLOG.length;i++){
    var e=_ERRLOG[i];
    var cls='errlog-item '+(e.read?'read':'unread');
    h+='<div class="'+cls+'">'
     + '<div class="errlog-itop">'
     +   '<span class="errlog-cat"><span class="dot"></span>'+esc(e.cat)+'</span>'
     +   '<span class="errlog-time">'+esc(__errlogFmtTs(e.ts))+'</span>'
     +   '<button class="errlog-copy" onclick="errlogCopyOne('+e.id+')">コピー</button>'
     + '</div>'
     + '<div class="errlog-detail">'+esc(e.detail)+'</div>'
     + '</div>';
  }
  body.innerHTML=h;
}

function __errlogFind(id){
  for(var i=0;i<_ERRLOG.length;i++){ if(_ERRLOG[i].id===id) return _ERRLOG[i]; }
  return null;
}

function errlogCopyOne(id){
  var e=__errlogFind(id);
  if(!e) return;
  __errlogCopyText(__errlogPlain(e));
  e.read=true;
  errlogUpdateBadges();
  errlogRender();
}
function errlogCopyAll(){
  if(!_ERRLOG.length){ __errlogCopyText('(エラーlogなし)'); return; }
  var parts=[];
  for(var i=_ERRLOG.length-1;i>=0;i--){ parts.push(__errlogPlain(_ERRLOG[i])); }
  __errlogCopyText(parts.join('\n\n----------------------------------------\n\n'));
}
function errlogMarkAllRead(){
  for(var i=0;i<_ERRLOG.length;i++){ _ERRLOG[i].read=true; }
  errlogUpdateBadges();
  errlogRender();
}

// クリップボードコピー(失敗時フォールバック付き)
function __errlogCopyText(txt){
  function done(){ toast('コピーしました'); }
  function fail(){
    try{
      var ta=document.createElement('textarea');
      ta.value=txt; ta.style.position='fixed'; ta.style.left='-9999px';
      document.body.appendChild(ta); ta.focus(); ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done();
    }catch(e){ toast('コピー失敗。手動で選択してください'); }
  }
  try{
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(txt).then(done, fail);
    }else{ fail(); }
  }catch(e){ fail(); }
}

// 軽量トースト
function toast(msg){
  var t=document.getElementById('__toast');
  if(!t){
    t=document.createElement('div'); t.id='__toast';
    t.style.cssText='position:fixed;left:50%;bottom:64px;transform:translateX(-50%);'
      +'background:rgba(0,0,0,.85);color:#fff;padding:9px 16px;border-radius:8px;'
      +'font-size:13px;z-index:120;opacity:0;transition:opacity .2s;pointer-events:none';
    document.body.appendChild(t);
  }
  t.textContent=msg; t.style.opacity='1';
  clearTimeout(t.__tmr);
  t.__tmr=setTimeout(function(){ t.style.opacity='0'; }, 1600);
}

// メニュー (ハンバーガー)
function toggleMenu(){
  var ov=document.getElementById('menuOverlay');
  if(ov.style.display==='block'){closeMenu();return;}
  ov.style.display='block';
  loadDbStatus();
}
function closeMenu(){document.getElementById('menuOverlay').style.display='none';}
// v341: 結果分析の入口は廃止 (menuOpenBacktest を削除)
function loadDbStatus(){
  var el=document.getElementById('dbStatusBody');
  el.textContent='確認中...';
  fetch('/api/db_status').then(function(r){return r.json();}).then(function(j){
    if(!j.exists){el.textContent='DBが見つかりません';return;}
    var d=j.db_date?(j.db_date.slice(0,4)+'/'+j.db_date.slice(4,6)+'/'+j.db_date.slice(6,8)):'不明';
    el.innerHTML='<div class="mn-row"><span>DB最新日</span><b>'+d+'</b></div>'
      +'<div class="mn-row"><span>サイズ</span><b>'+j.size_mb+' MB</b></div>'
      +'<div class="mn-row"><span>場所</span><b>'+j.location+'</b></div>';
  }).catch(function(){el.textContent='取得失敗';});
}

// 実績画面を開く
function openBacktest(){
  var ov=document.getElementById("btOverlay");
  if(!ov){
    ov=document.createElement("div");
    ov.id="btOverlay";
    ov.className="bt-overlay";
    document.body.appendChild(ov);
  }
  ov.style.display="block";
  ov.innerHTML=__btFormHtml();
}
function closeBacktest(){
  var ov=document.getElementById("btOverlay");
  if(ov) ov.style.display="none";
}

function __btFormHtml(){
  // 既定: 直近30日
  var today=new Date();
  var to=__btYmd(today);
  var fromD=new Date(today.getTime()-29*86400000);
  var from=__btYmd(fromD);
  var h='';
  h+='<div class="bt-panel">';
  h+='<div class="bt-head"><span class="bt-ttl">実績集計</span>';
  h+='<button class="bt-close" onclick="closeBacktest()">×</button></div>';
  h+='<div class="bt-body">';
  h+='<div class="bt-row"><label>開始</label>';
  h+='<input type="date" id="btFrom" value="'+__btYmdDash(from)+'" onchange="btSyncTo()"></div>';
  h+='<div class="bt-row"><label>終了</label>';
  h+='<input type="date" id="btTo" value="'+__btYmdDash(to)+'"></div>';
  h+='<div class="bt-note">期間内のレースで託宣（合議/軸1〜3人）と御告（◎○▲△を軸）を再現し、上位1〜'+_BT_MAXN+'点買いの的中率・回収率を集計します。結果は託宣/御告タブで切替表示。出走表が未取得の日は自動でスクレイピング取得します（時間がかかります）。</div>';
  h+='<div class="bt-force"><label><input type="checkbox" id="btForce"> 集計済みも上書きで再集計する</label></div>';
  // v320: 再集計する範囲を選べる。辞書を替えて検証するとき、
  //   全系列を回すと時間がかかるので必要な系列だけにする。
  h+='<div class="bt-scope">再集計する対象'
   + '<select id="btScope" class="bt-scope-sel">'
   + '<option value="all">すべて</option>'
   + '<option value="omk">託宣のみ</option>'
   + '<option value="ora">御告のみ</option>'
   + '<option value="vfa">検証Aのみ</option>'
   + '<option value="vfb">検証Bのみ</option>'
   + '<option value="vfc">検証Cのみ</option>'
   + '<option value="vf2">検証A＋B＋Cまとめて</option>'
   + '</select>'
   + '<div class="bt-scope-note">「託宣のみ」等を選ぶと、その系列だけ計算して'
   + '保存します。他の系列は前回の値がそのまま残ります。</div></div>';
  h+='<div class="bt-actions">';
  h+='<button class="bt-run" onclick="runBacktest()">集計開始</button>';
  h+='</div>';
  h+='<div class="bt-actions">';
  h+='<button class="bt-run bt-past" onclick="btTogglePast()">過去の集計</button>';
  h+='</div>';
  h+='<div id="btPastList" class="bt-pastlist"></div>';
  h+='<div id="btProgress" class="bt-progress"></div>';
  h+='<div id="btResult" class="bt-result"></div>';
  h+='</div></div>';
  return h;
}

// 開始日変更時、終了日を開始日に合わせる(デフォルト同期)
function btSyncTo(){
  var f=document.getElementById("btFrom");
  var t=document.getElementById("btTo");
  if(!f||!t) return;
  t.value=f.value;
}

// 過去集計を開き、指定日(YYYYMMDD)を展開表示する
function btOpenPastAt(date){
  var box=document.getElementById("btPastList");
  if(!box) return;
  box.setAttribute("data-open","1");
  box.innerHTML='<div class="bt-past-load">読み込み中...</div>';
  fetch("/api/bt_list").then(function(r){return r.json();}).then(function(j){
    if(!j.ok || !j.years || !j.years.length){
      box.innerHTML='<div class="bt-past-load">保存された集計がありません。</div>'; return;
    }
    _BT_YEARS=j.years;
    _BT_OPEN_YEAR=date.slice(0,4);
    _BT_OPEN_DATE=date;
    __btRenderPast();
  }).catch(function(){
    box.innerHTML='<div class="bt-past-load">読み込み失敗。</div>';
  });
}

// 過去集計リストの開閉(年→日付の2階層)
var _BT_YEARS=null;        // bt_listの結果キャッシュ
var _BT_OPEN_YEAR=null;    // 展開中の年
var _BT_OPEN_DATE=null;    // 展開中の日付(直下に集計表示)

function btTogglePast(){
  var box=document.getElementById("btPastList");
  if(!box) return;
  if(box.getAttribute("data-open")==="1"){
    box.innerHTML=""; box.setAttribute("data-open","0");
    _BT_OPEN_YEAR=null; _BT_OPEN_DATE=null; return;
  }
  box.setAttribute("data-open","1");
  box.innerHTML='<div class="bt-past-load">読み込み中...</div>';
  fetch("/api/bt_list").then(function(r){return r.json();}).then(function(j){
    if(!j.ok || !j.years || !j.years.length){
      box.innerHTML='<div class="bt-past-load">保存された集計がありません。</div>'; return;
    }
    _BT_YEARS=j.years;
    _BT_OPEN_YEAR=null; _BT_OPEN_DATE=null;
    __btRenderPast();
  }).catch(function(){
    box.innerHTML='<div class="bt-past-load">読み込み失敗。</div>';
  });
}

// 過去リストを現在の展開状態で描画 (年→月→日 の3階層)
var _BT_OPEN_MONTH=null;   // "YYYYMM" の月キー
function __btRenderPast(){
  var box=document.getElementById("btPastList");
  if(!box || !_BT_YEARS) return;
  var h='';
  for(var y=0;y<_BT_YEARS.length;y++){
    var yr=_BT_YEARS[y];
    var yOpen=(_BT_OPEN_YEAR===yr.year);
    h+='<button class="bt-year-bar" onclick="btToggleYear(\''+yr.year+'\')">'
     + '<span>'+yr.year+'年</span><span class="bt-year-cnt">'+yr.days.length+'日</span></button>';
    if(yOpen){
      // 月ごとに集約
      var byMonth={};
      var monthOrder=[];
      for(var d=0;d<yr.days.length;d++){
        var dd=yr.days[d];
        var mk=dd.date.slice(0,6);
        if(!byMonth[mk]){ byMonth[mk]=[]; monthOrder.push(mk); }
        byMonth[mk].push(dd);
      }
      for(var mi=0;mi<monthOrder.length;mi++){
        var mk2=monthOrder[mi];
        var mLabel=parseInt(mk2.slice(4,6),10)+'月';
        var mDays=byMonth[mk2];
        var mRaces=0;
        for(var k=0;k<mDays.length;k++){ mRaces+=mDays[k].races; }
        var mOpen=(_BT_OPEN_MONTH===mk2);
        h+='<button class="bt-month-bar" onclick="btToggleMonth(\''+mk2+'\')">'
         + '<span class="bt-month-label">'+mLabel+'</span>'
         + '<span class="bt-month-cnt">'+mDays.length+'日・'+mRaces+'R</span></button>';
        if(mOpen){
          // 月行の直下に「月別集計」ボタン+表示エリア
          h+='<button class="bt-day-bar bt-month-sum" onclick="btLoadMonth(\''+mk2+'\')">'
           + '<span>月別集計</span><span class="bt-day-cnt">合計'+mRaces+'R</span></button>';
          if(_BT_OPEN_DATE==="MONTH:"+mk2){
            h+='<div id="btDayResult" class="bt-day-result-full"></div>';
          }
          // 日リスト
          for(var di=0;di<mDays.length;di++){
            var dd2=mDays[di];
            var md=dd2.date.slice(4,6)+'/'+dd2.date.slice(6,8);
            h+='<button class="bt-day-bar" onclick="btToggleDay(\''+dd2.date+'\')">'
             + '<span>'+md+'</span><span class="bt-day-cnt">'+dd2.races+'R</span></button>';
            if(_BT_OPEN_DATE===dd2.date){
              h+='<div id="btDayResult" class="bt-day-result-full"></div>';
            }
          }
        }
      }
    }
  }
  box.innerHTML=h;
  if(_BT_OPEN_DATE){
    if(_BT_OPEN_DATE.indexOf("MONTH:")===0){
      btLoadMonth(_BT_OPEN_DATE.slice(6), true);
    } else {
      btLoadDay(_BT_OPEN_DATE);
    }
  }
}

function btToggleYear(year){
  if(_BT_OPEN_YEAR===year){ _BT_OPEN_YEAR=null; _BT_OPEN_MONTH=null; _BT_OPEN_DATE=null; }
  else { _BT_OPEN_YEAR=year; _BT_OPEN_MONTH=null; _BT_OPEN_DATE=null; }
  __btRenderPast();
}
function btToggleMonth(mkey){
  if(_BT_OPEN_MONTH===mkey){ _BT_OPEN_MONTH=null; _BT_OPEN_DATE=null; }
  else { _BT_OPEN_MONTH=mkey; _BT_OPEN_DATE=null; }
  __btRenderPast();
}
function btToggleDay(date){
  if(_BT_OPEN_DATE===date){ _BT_OPEN_DATE=null; }
  else { _BT_OPEN_DATE=date; }
  __btRenderPast();
}

// 月別集計: その月の全日を期間として読み込み btDayResult に表示
function btLoadMonth(mkey, fromRender){
  if(!fromRender){
    if(_BT_OPEN_DATE==="MONTH:"+mkey){ _BT_OPEN_DATE=null; }
    else { _BT_OPEN_DATE="MONTH:"+mkey; }
    __btRenderPast();
    return;
  }
  // 月初〜月末
  var y=parseInt(mkey.slice(0,4),10);
  var m=parseInt(mkey.slice(4,6),10);
  var from=mkey+"01";
  var lastDay=new Date(y, m, 0).getDate();
  var ld=lastDay<10?("0"+lastDay):(""+lastDay);
  var to=mkey+ld;
  fetch("/api/bt_get?from="+from+"&to="+to)
    .then(function(r){return r.json();}).then(function(j){
      if(!j.ok || !j.raw){
        var box=document.getElementById("btDayResult");
        if(box) box.innerHTML='<div class="bt-meta">この月の保存集計がありません。</div>';
        return;
      }
      _BT_LAST_RAW=j.raw; _BT_VENUE_FILTER="";
      var bundle=__btRawToBundle(j.raw);
      __btRenderBundle(bundle, from, to, "btDayResult");
    }).catch(function(){});
}

// 単日の集計を直下コンテナに表示
function btLoadDay(date){
  fetch("/api/bt_get?date="+date)
    .then(function(r){return r.json();}).then(function(j){
      if(!j.ok || !j.raw){ return; }
      _BT_LAST_RAW=j.raw; _BT_VENUE_FILTER="";
      var bundle=__btRawToBundle(j.raw);
      __btRenderBundle(bundle, date, date, "btDayResult");
    }).catch(function(){});
}

// 生raw(aggの集合)を summary bundle に変換
function __btRawToBundle(raw){
  var b={omk:{}, ora:{}, vfa:{}, vfb:{}, vfc:{}};
  var ok=raw.omk||{}, og=raw.ora||{};
  var va=raw.vfa||raw.evw||{}, vb=raw.vfb||{}, vc=raw.vfc||{};
  var omkKeys=["0","1","2","3"];
  for(var i=0;i<omkKeys.length;i++){
    b.omk[omkKeys[i]]=__btSummarize(ok[omkKeys[i]]||__btNewAgg());
  }
  b.vfa["0"]=__btSummarize(va["0"]||__btNewAgg());
  b.vfb["0"]=__btSummarize(vb["0"]||__btNewAgg());
  b.vfc["0"]=__btSummarize(vc["0"]||__btNewAgg());
  var oraKeys=["honmei","taikou","tanana","renka"];
  for(var j=0;j<oraKeys.length;j++){
    b.ora[oraKeys[j]]=__btSummarize(og[oraKeys[j]]||__btNewAgg());
  }
  return b;
}

// rawの全aggのdetailを走査し、出現する会場名一覧(重複なし)を返す
function __btVenuesInRaw(raw){
  if(!raw) return [];
  var seen={}, out=[];
  var groups=[raw.omk||{}, raw.ora||{}];
  for(var gi=0; gi<groups.length; gi++){
    var g=groups[gi];
    for(var k in g){
      if(!g.hasOwnProperty(k)) continue;
      var det=(g[k] && g[k].detail) ? g[k].detail : [];
      for(var di=0; di<det.length; di++){
        var v=det[di] ? (det[di].venue||"") : "";
        if(v && !seen[v]){ seen[v]=true; out.push(v); }
      }
    }
  }
  out.sort();
  return out;
}

// detail配列をpredで絞り、betN/retN/hitNを__btAccumと同じ式で再集計したaggを返す
function __btAggFromDetail(detail, pred){
  var a=__btNewAgg();
  var det=detail||[];
  for(var di=0; di<det.length; di++){
    var dd=det[di];
    if(pred && !pred(dd)) continue;
    a.races++;
    a.detail.push(dd);
    // v322: detail の種類ごとに買い方が違う。取り違えると
    //   会場フィルタ時だけ数字が狂うので、明示的に分岐する。
    if(dd.unionFiltered && dd.unionPts){
      // 和集合: 点数は unionPts[n-1]、的中は unionHit[n-1]
      for(var n=1;n<=_BT_MAXN;n++){
        var up=dd.unionPts[n-1]||0;
        if(up<=0) continue;
        a.betN[n-1]+=up*100;
        if(dd.unionHit && dd.unionHit[n-1]){
          a.hitN[n-1]++; a.retN[n-1]+=dd.refund;
        }
      }
    } else if(dd.evFiltered && dd.boughtN){
      // 期待値フィルタ: 実際に買った点数
      for(var n=1;n<=_BT_MAXN;n++){
        var bn=dd.boughtN[n-1]||0;
        if(bn<=0) continue;
        a.betN[n-1]+=bn*100;
        if(dd.hitAtN && dd.hitAtN[n-1]){
          a.hitN[n-1]++; a.retN[n-1]+=dd.refund;
        }
      }
    } else {
      var navail=(typeof dd.navail==="number")?dd.navail:0;
      for(var n=1;n<=_BT_MAXN;n++){
        var buyN=(n<=navail)?n:navail;
        if(buyN<=0) continue;
        a.betN[n-1]+=buyN*100;
        if(dd.hitIndex>=0 && dd.hitIndex<buyN){
          a.hitN[n-1]++; a.retN[n-1]+=dd.refund;
        }
      }
    }
  }
  return a;
}

// 会場フィルター付きでbundleを生成(全aggをpredで絞って再集計→Summarize)
function __btRawToBundleFiltered(raw, pred){
  var b={omk:{}, ora:{}, vfa:{}, vfb:{}, vfc:{}};
  var ok=raw.omk||{}, og=raw.ora||{};
  var va=raw.vfa||raw.evw||{}, vb=raw.vfb||{}, vc=raw.vfc||{};
  var omkKeys=["0","1","2","3"];
  for(var i=0;i<omkKeys.length;i++){
    var srcO=ok[omkKeys[i]]||__btNewAgg();
    b.omk[omkKeys[i]]=__btSummarize(__btAggFromDetail(srcO.detail, pred));
  }
  b.vfa["0"]=__btSummarize(__btAggFromDetail((va["0"]||__btNewAgg()).detail, pred));
  b.vfb["0"]=__btSummarize(__btAggFromDetail((vb["0"]||__btNewAgg()).detail, pred));
  b.vfc["0"]=__btSummarize(__btAggFromDetail((vc["0"]||__btNewAgg()).detail, pred));
  var oraKeys=["honmei","taikou","tanana","renka"];
  for(var j=0;j<oraKeys.length;j++){
    var srcR=og[oraKeys[j]]||__btNewAgg();
    b.ora[oraKeys[j]]=__btSummarize(__btAggFromDetail(srcR.detail, pred));
  }
  return b;
}


// 期間指定で過去集計を読み込んで表示(複数日合算)
function btLoadPastRange(from,to){
  var prog=document.getElementById("btProgress");
  if(prog) prog.textContent="過去の集計を読み込み中...";
  fetch("/api/bt_get?from="+from+"&to="+to)
    .then(function(r){return r.json();}).then(function(j){
      if(!j.ok || !j.raw){
        if(prog) prog.textContent="その期間の保存集計がありません。"; return;
      }
      if(prog) prog.textContent="";
      _BT_LAST_RAW=j.raw; _BT_VENUE_FILTER="";
      var bundle=__btRawToBundle(j.raw);
      __btRenderBundle(bundle, from, to);
    }).catch(function(){
      if(prog) prog.textContent="読み込み失敗。";
    });
}

function __btYmd(dt){
  var y=dt.getFullYear();
  var m=dt.getMonth()+1; if(m<10) m="0"+m;
  var d=dt.getDate(); if(d<10) d="0"+d;
  return ""+y+m+d;
}
function __btYmdDash(ymd){
  return ymd.slice(0,4)+"-"+ymd.slice(4,6)+"-"+ymd.slice(6,8);
}
function __btDashToYmd(s){ return s.replace(/-/g,""); }

// 集計実行
var _BT_FORCE=false;
var _BT_SCOPE="all";   // v324: all / omk / ora / vfa / vfb / vfc / vf2(A+B+C)
// v322: 検証A/Bで重ねる系列。0=合議 1=軸1人 2=軸2人 3=軸3人
var _VF_AXES_A=[0,1,2];      // 検証A: 100%超だった3系列
var _VF_AXES_B=[0,1,2,3];    // 検証B: 全4系列
function runBacktest(){
  if(_BT_RUNNING) return;
  var fEl=document.getElementById("btFrom");
  var tEl=document.getElementById("btTo");
  if(!fEl||!tEl) return;
  var from=__btDashToYmd(fEl.value||"");
  var to=__btDashToYmd(tEl.value||"");
  if(!from||!to){ alert("期間を指定してください"); return; }
  var fc=document.getElementById("btForce");
  _BT_FORCE=(fc && fc.checked)?true:false;
  var sc=document.getElementById("btScope");
  _BT_SCOPE=(sc && sc.value)?sc.value:"all";
  // 対象を絞ったときは既存集計をスキップしては意味がないので強制再集計にする
  if(_BT_SCOPE!=="all"){ _BT_FORCE=true; }
  var prog=document.getElementById("btProgress");
  var rEl=document.getElementById("btResult");
  if(rEl) rEl.innerHTML="";
  _BT_RUNNING=true;
  if(prog) prog.textContent="レース一覧を取得中...";
  fetch("/api/period_races?from="+from+"&to="+to+"&scrape=1")
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(!j.ok || !j.days || !j.total){
        if(prog) prog.textContent="対象レースがありません。";
        _BT_RUNNING=false;
        return;
      }
      // 日別に処理する
      __btProcessDays(j.days, 0, from, to);
    })
    .catch(function(e){
      if(prog) prog.textContent="取得エラー: "+e;
      _BT_RUNNING=false;
    });
}

// 日別に順次処理。既存日はスキップ。全日完了後に期間合算を表示。
function __btProcessDays(days, dayIdx, from, to){
  var prog=document.getElementById("btProgress");
  if(dayIdx>=days.length){
    _BT_RUNNING=false;
    if(prog) prog.textContent="";
    // 過去集計を開き、開始日(from)を展開表示する
    btOpenPastAt(from);
    return;
  }
  var day=days[dayIdx];
  // 既存チェック (強制再集計時はスキップしない)
  if(_BT_FORCE){
    var tasks=[];
    for(var ki=0; ki<day.keys.length; ki++){
      tasks.push({date:day.date, key:day.keys[ki]});
    }
    if(prog) prog.textContent="再集計中 "+day.date+" "+(dayIdx+1)+"/"+days.length;
    __btProcessOneDay(tasks, day.date, function(){
      __btProcessDays(days, dayIdx+1, from, to);
    });
    return;
  }
  fetch("/api/bt_exists?date="+day.date)
    .then(function(r){return r.json();}).then(function(j){
      if(j.exists){
        if(prog) prog.textContent=day.date+" は集計済み(スキップ) "+(dayIdx+1)+"/"+days.length;
        __btProcessDays(days, dayIdx+1, from, to);
        return;
      }
      // この日のタスクを作って集計
      var tasks=[];
      for(var ki=0; ki<day.keys.length; ki++){
        tasks.push({date:day.date, key:day.keys[ki]});
      }
      __btProcessOneDay(tasks, day.date, function(){
        __btProcessDays(days, dayIdx+1, from, to);
      });
    }).catch(function(){
      __btProcessDays(days, dayIdx+1, from, to);
    });
}

// 御告(手動軸)を指定車番1点を軸に再現する。
//   返り値: {ok, combos:[...], hitIndex, refund}
function __btEvalOra(racePayload, axisBike, trifecta, refund){
  var saved=_RACE;
  var savedAxis=_ORA_AXIS;
  var savedRival=_ORA_RIVAL;
  var res={ok:false, combos:[], hitIndex:-1, refund:0};
  try{
    _RACE=racePayload;
    _ORA_AXIS=[String(axisBike)];
    _ORA_RIVAL=[];
    if(!racePayload || !racePayload.header || !racePayload.header.players){ return res; }
    var d={players:racePayload.header.players};
    var pred=__oraPredict(d);
    if(pred.error || !pred.combos){ return res; }
    var combos=[];
    var seen={};
    for(var k=0;k<pred.combos.length;k++){
      var c=pred.combos[k].b.join('-');
      if(seen[c]) continue; seen[c]=1; combos.push(c);
    }
    res.ok=true; res.combos=combos;
    if(trifecta){
      for(var i=0;i<combos.length;i++){
        if(combos[i]===trifecta){ res.hitIndex=i; res.refund=refund||0; break; }
      }
    }
  }catch(e){
    res.error=String(e);
  }finally{
    _RACE=saved; _ORA_AXIS=savedAxis; _ORA_RIVAL=savedRival;
  }
  return res;
}

// 空の集計器を作る
function __btNewAgg(){
  var a={races:0, skipped:0, noResult:0, hitN:[], retN:[], betN:[], detail:[]};
  for(var i=0;i<_BT_MAXN;i++){ a.hitN.push(0); a.retN.push(0); a.betN.push(0); }
  return a;
}
// 1パターンの結果(ev相当: combos,hitIndex,refund)を集計器に反映
function __btAccum(agg, ev, meta){
  agg.races++;
  var navail=ev.combos.length;
  agg.detail.push(__btCopyMeta({
    date:meta.date, key:meta.key, trifecta:meta.trifecta, venue:meta.venue||"",
    post:meta.post||"", raceNo:meta.raceNo||"",
    hitIndex:ev.hitIndex, refund:ev.refund, navail:navail,
    axisMark:meta.axisMark, axisBike:meta.axisBike,
    labAna:meta.labAna, labWeak:meta.labWeak, labLayoff:meta.labLayoff
  }, meta));
  for(var n=1;n<=_BT_MAXN;n++){
    var buyN=(n<=navail)?n:navail;
    if(buyN<=0) continue;
    agg.betN[n-1]+=buyN*100;
    if(ev.hitIndex>=0 && ev.hitIndex<buyN){
      agg.hitN[n-1]++; agg.retN[n-1]+=ev.refund;
    }
  }
}

// 1日分のタスクを順次処理し、生aggを日別保存。完了でdoneCb()。
function __btProcessOneDay(tasks, date, doneCb){
  var prog=document.getElementById("btProgress");
  var aggs={
    omk0:__btNewAgg(), omk1:__btNewAgg(), omk2:__btNewAgg(), omk3:__btNewAgg(),
    oraA:__btNewAgg(), oraB:__btNewAgg(), oraC:__btNewAgg(), oraD:__btNewAgg()
  };
  var oraMarks=[["oraA","◎"],["oraB","◯"],["oraC","▲"],["oraD","△"]];
  var _omkFail={};
  // v322: 検証A/Bの集計器(和集合)。
  // v322: 検証A/Bは和集合方式に変更(下の __btUnionRace)。
  //   Aは現行辞書、Bは比較したい辞書構成で集計する想定。
  //   同一レース・同一集計器なので横並びで比較できる。
  // v322: 検証A/Bは和集合。軸別ではないので1つずつ。
  aggs.vfa0=__btNewAgg();
  aggs.vfb0=__btNewAgg();
  // v324: 検証C = 検証Bの軸配分 + 酒場順の2着3着
  aggs.vfc0=__btNewAgg();

  // 1レース分(payload + result)を集計器へ反映。データ取得方法に依存しない純ロジック。
  function accumRace(payload, rs, oddsMap, raceKey){
    if(!payload || payload.status!=="ok") return;
    if(!rs || !rs.ok || !rs.trifecta) return;
    var refund=rs.refund_3t||0;
    var tri=rs.trifecta;
    var players=(payload.header&&payload.header.players)?payload.header.players:[];
    var venueName=(payload.header&&payload.header.venue)?payload.header.venue:"";
    // v322: key が空文字で保存されていたため明細に「レース」が出なかった。
    //   サーバは races[i].key(会場_R番号)を返しているので、それを使う。
    var hdr=(payload && payload.header)?payload.header:{};
    var meta={date:date, key:raceKey||("" + (hdr.venue||"") + "_" + (hdr.race_no||"")),
              post:hdr.post_time||"", raceNo:hdr.race_no||"",
              trifecta:tri, venue:venueName,
              axisMark:"", axisBike:String(tri).split("-")[0],
              labAna:false, labWeak:false, labLayoff:false};
    for(var lp=0; lp<players.length; lp++){
      var lk=players[lp].label_kind;
      if(lk==="ana") meta.labAna=true; else if(lk==="weak") meta.labWeak=true;
      if(players[lp].layoff_kind==="layoff") meta.labLayoff=true;
      if(String(players[lp].bike)===meta.axisBike) meta.axisMark=players[lp].keihai_mark||"";
    }
    // v322: 検証A / 検証B = **複数系列の買い目を重ねる**(重複は1点)。
    //   A = 合議 + 軸1人 + 軸2人 の3系列
    //       6ヶ月の集計で、除外ルールを変えてもこの3系列が
    //       安定して100%前後以上だったため(軸3人は常に100%未満)。
    //       ※これは標本内の選択である点に注意。
    //   B = 4系列すべて
    //   同じ買い目を複数系列が出せば1点にまとまるので、
    //   **合議度が高いレースほど点数が減る**。
    meta.refundAll = refund;
    // v329: レースの素性を明細に持たせる (分析用)
    try{
      var _rm=__btRaceMeta(payload);
      meta.q=_rm.q; meta.lineCls=_rm.lineCls; meta.grade=_rm.grade;
      meta.cls=_rm.cls; meta.kind=_rm.kind; meta.line=_rm.line;
      meta.rsOrder=_rm.rsOrder; meta.stOrder=_rm.stOrder;
      meta.sOrder=_rm.sOrder; meta.bOrder=_rm.bOrder;
      meta.kimari1=_rm.kimari1; meta.kimari2=_rm.kimari2; meta.lap=_rm.lap;
    }catch(e){}
    if(_BT_SCOPE==="all" || _BT_SCOPE==="vfa" || _BT_SCOPE==="vf2"){
      var urA=__btUnionRace(payload, tri, refund, _VF_AXES_A);
      if(urA.ok){ __btAccumUnion(aggs.vfa0, urA, meta); }
    }
    if(_BT_SCOPE==="all" || _BT_SCOPE==="vfb" || _BT_SCOPE==="vf2"){
      var urB=__btUnionRace(payload, tri, refund, _VF_AXES_B);
      if(urB.ok){ __btAccumUnion(aggs.vfb0, urB, meta); }
    }
    // v324: 検証C。検証Bと同じ軸配分で、2着3着を酒場順に置き換える。
    if(_BT_SCOPE==="all" || _BT_SCOPE==="vfc" || _BT_SCOPE==="vf2"){
      var urC=__btTavernRace(payload, tri, refund, _VF_AXES_B);
      if(urC.ok){ __btAccumUnion(aggs.vfc0, urC, meta); }
    }
    // 託宣 4パターン
    if(_BT_SCOPE==="all" || _BT_SCOPE==="omk"){
    var savedAx=_ORA_OMK_AXIS;
    var omkKeys=["omk0","omk1","omk2","omk3"];
    for(var oi=0; oi<4; oi++){
      _BT_AXIS=oi;
      var evk=__btEvalRace(payload, tri, refund);
      if(evk.ok){ __btAccum(aggs[omkKeys[oi]], evk, meta); }
      else if(oi===0){
        // v301: 託宣が0Rになる原因を掴むため失敗理由を数える
        var _rz=String(evk.error||"(理由なし)").substring(0,200);
        _omkFail[_rz]=(_omkFail[_rz]||0)+1;
      }
    }
    _ORA_OMK_AXIS=savedAx;
    }
    // 御告 4パターン
    if(_BT_SCOPE!=="all" && _BT_SCOPE!=="ora"){ return; }
    for(var mi=0; mi<oraMarks.length; mi++){
      var mk=oraMarks[mi][1];
      var aggKey=oraMarks[mi][0];
      var axisB=null;
      for(var pp=0; pp<players.length; pp++){
        var km=players[pp].keihai_mark;
        if(km===mk || (mk==="◯" && km==="○") || (mk==="○" && km==="◯")){
          axisB=String(players[pp].bike); break;
        }
      }
      if(axisB==null) continue;
      var evo=__btEvalOra(payload, axisB, tri, refund);
      if(evo.ok){
        var metaO={date:date, key:meta.key, trifecta:tri, venue:meta.venue,
                   post:meta.post, raceNo:meta.raceNo,
                   axisMark:mk, axisBike:axisB,
                   labAna:meta.labAna, labWeak:meta.labWeak, labLayoff:meta.labLayoff,
                   q:meta.q, lineCls:meta.lineCls, grade:meta.grade,
                   cls:meta.cls, kind:meta.kind, line:meta.line,
                   rsOrder:meta.rsOrder, stOrder:meta.stOrder,
                   sOrder:meta.sOrder, bOrder:meta.bOrder,
                   kimari1:meta.kimari1, kimari2:meta.kimari2, lap:meta.lap,
                   };
        __btAccum(aggs[aggKey], evo, metaO);
      }
    }
  }

  function saveAndDone(){
    // v320: scope で計算した系列だけ送る。サーバ側は既存値とマージする。
    var raw={};
    if(_BT_SCOPE==="all" || _BT_SCOPE==="omk"){
      raw.omk={ "0":aggs.omk0, "1":aggs.omk1, "2":aggs.omk2, "3":aggs.omk3 };
    }
    if(_BT_SCOPE==="all" || _BT_SCOPE==="ora"){
      raw.ora={ "honmei":aggs.oraA, "taikou":aggs.oraB, "tanana":aggs.oraC, "renka":aggs.oraD };
    }
    // v322: 検証A/Bは和集合なので軸別の区分は無い。"0" のみ使う。
    if(_BT_SCOPE==="all" || _BT_SCOPE==="vfa" || _BT_SCOPE==="vf2"){
      raw.vfa={ "0":aggs.vfa0 };
    }
    if(_BT_SCOPE==="all" || _BT_SCOPE==="vfb" || _BT_SCOPE==="vf2"){
      raw.vfb={ "0":aggs.vfb0 };
    }
    if(_BT_SCOPE==="all" || _BT_SCOPE==="vfc" || _BT_SCOPE==="vf2"){
      raw.vfc={ "0":aggs.vfc0 };
    }
    try{
      fetch("/api/bt_save",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({date:date, raw:raw, diag:_omkFail, scope:_BT_SCOPE})})
        .then(function(){ if(doneCb) doneCb(); })
        .catch(function(){ if(doneCb) doneCb(); });
    }catch(e){ if(doneCb) doneCb(); }
  }

  // 1日分の payload+result をまとめて1回で取得(HTTP往復を 2N → 1 に削減)。
  // サーバ側が並列構築するため、端末側の直列待ちが消えて大幅に高速化する。
  if(prog) prog.textContent="集計中 "+date+" (一括取得)";
  fetch("/api/bt_day?date="+encodeURIComponent(date)+"&scrape=1")
    .then(function(r){ return r.json(); })
    .then(function(j){
      if(!j || !j.ok || !j.races){ saveAndDone(); return; }
      var races=j.races;
      // 集計はJSなので一気に回せるが、UIブロックを避けるため少量ずつ分割して進捗表示。
      var i=0;
      function chunk(){
        var end=Math.min(i+8, races.length);
        for(; i<end; i++){
          accumRace(races[i].payload, races[i].result, races[i].odds,
                    races[i].key);
        }
        if(prog) prog.textContent="集計中 "+date+" "+i+"/"+races.length;
        if(i<races.length){ setTimeout(chunk,0); }
        else { saveAndDone(); }
      }
      chunk();
    })
    .catch(function(){ saveAndDone(); });
}

// 8パターン束をタブUIで描画。targetId未指定時は btResult。
function __btRenderBundle(bundle, from, to, targetId){
  _BT_LAST_BUNDLE=bundle;
  var rId=targetId||"btResult";
  var rEl=document.getElementById(rId);
  if(!rEl) return;
  // デフォルトタブ
  _BT_TAB_MAIN="omk"; _BT_TAB_SUB="0";
  _BT_BODY_ID=rId+"_body";
  var h='';
  // 大タブ
  h+='<div class="bt-tabs-main">';
  h+='<button class="bt-tabm" data-m="omk" onclick="btTab(\'omk\')">託宣</button>';
  h+='<button class="bt-tabm" data-m="ora" onclick="btTab(\'ora\')">御告</button>';
  h+='<button class="bt-tabm" data-m="vfa" onclick="btTab(\'vfa\')">検証A</button>';
  h+='<button class="bt-tabm" data-m="vfb" onclick="btTab(\'vfb\')">検証B</button>';
  h+='<button class="bt-tabm" data-m="vfc" onclick="btTab(\'vfc\')">検証C</button>';
  h+='</div>';
  // 子タブ(中身は切替時に差し替え)
  h+='<div id="btSubTabs" class="bt-tabs-sub"></div>';
  h+='<div id="'+_BT_BODY_ID+'" class="bt-result-body"></div>';
  rEl.innerHTML=h;
  btTab("omk");
}

// 大タブ切替
function btTab(main){
  _BT_TAB_MAIN=main;
  // 大タブのactive表示(描画先を問わずdocument全体から探す)
  var ms=document.querySelectorAll(".bt-tabm");
  for(var i=0;i<ms.length;i++){
    if(ms[i].getAttribute("data-m")===main) ms[i].classList.add("on");
    else ms[i].classList.remove("on");
  }
  // 子タブを構築
  var sub=document.getElementById("btSubTabs");
  var subs, defSub;
  if(main==="vfa" || main==="vfb" || main==="vfc"){
    // v322: 和集合なので軸別の区分は無い
    var lbl="全4系列";
    if(main==="vfa") lbl="合議+軸1+軸2";
    else if(main==="vfc") lbl="検証B軸×酒場順";
    subs=[["0", lbl]];
    defSub="0";
  } else if(main==="omk"){
    subs=[["0","合議"],["1","軸1人"],["2","軸2人"],["3","軸3人"]];
    defSub="0";
  } else {
    subs=[["honmei","◎"],["taikou","◯"],["tanana","▲"],["renka","△"]];
    defSub="honmei";
  }
  var sh='';
  for(var s=0;s<subs.length;s++){
    sh+='<button class="bt-tabs" data-s="'+subs[s][0]+'" onclick="btSubTab(\''+subs[s][0]+'\')">'+subs[s][1]+'</button>';
  }
  if(sub) sub.innerHTML=sh;
  btSubTab(defSub);
}

// 子タブ切替
function btSubTab(subKey){
  _BT_TAB_SUB=subKey;
  var sub=document.getElementById("btSubTabs");
  if(sub){
    var ss=sub.querySelectorAll(".bt-tabs");
    for(var i=0;i<ss.length;i++){
      if(ss[i].getAttribute("data-s")===subKey) ss[i].classList.add("on");
      else ss[i].classList.remove("on");
    }
  }
  var b=_BT_LAST_BUNDLE;
  if(!b) return;
  var summary=null;
  if(_BT_TAB_MAIN==="omk") summary=(b.omk||{})[subKey];
  else if(_BT_TAB_MAIN==="vfa") summary=(b.vfa||{})[subKey];
  else if(_BT_TAB_MAIN==="vfb") summary=(b.vfb||{})[subKey];
  else if(_BT_TAB_MAIN==="vfc") summary=(b.vfc||{})[subKey];
  else summary=(b.ora||{})[subKey];
  if(summary) __btRenderResult(summary);
  else {
    var body=document.getElementById(_BT_BODY_ID);
    if(body) body.innerHTML='<div class="bt-meta">該当データがありません。</div>';
  }
}

function __btSummarize(agg){
  var rows=[];
  for(var n=1;n<=_BT_MAXN;n++){
    var bet=agg.betN[n-1];
    var ret=agg.retN[n-1];
    var hit=agg.hitN[n-1];
    var hitRate=(agg.races>0)?(hit/agg.races*100):0;
    var roi=(bet>0)?(ret/bet*100):0;
    rows.push({n:n, hit:hit, hitRate:hitRate, bet:bet, ret:ret, roi:roi});
  }
  return {races:agg.races, skipped:agg.skipped||0, noResult:agg.noResult||0,
          rows:rows, detail:agg.detail||[]};
}

// 会場別表示の現在状態 (空文字="全会場")
var _BT_VENUE_FILTER="";
// 現在の生raw (会場フィルター用) を保持
var _BT_LAST_RAW=null;

function __btRenderResult(s, from, to){
  var rEl=document.getElementById(_BT_BODY_ID);
  if(!rEl) rEl=document.getElementById("btResultBody");
  if(!rEl) rEl=document.getElementById("btResult");
  if(!rEl) return;
  _BT_LAST_SUMMARY=s;   // 明細の点数切替で再利用
  var h='';
  // 会場フィルター (失敗しても表は描画する)
  try {
    if(_BT_LAST_RAW){
      var venues=__btVenuesInRaw(_BT_LAST_RAW);
      if(venues.length>1){
        h+='<div class="bt-venue-h">会場別 ';
        h+='<select class="bt-detail-sel" onchange="btChangeVenue(this.value)">';
        var allSel=(_BT_VENUE_FILTER==="")?" selected":"";
        h+='<option value=""'+allSel+'>全会場</option>';
        for(var vi=0;vi<venues.length;vi++){
          var vv=venues[vi];
          var vs=(_BT_VENUE_FILTER===vv)?" selected":"";
          h+='<option value="'+vv+'"'+vs+'>'+vv+'</option>';
        }
        h+='</select></div>';
      }
    }
  } catch(e) { h+='<div class="bt-meta">[venue err] '+e+'</div>'; }
  h+='<div class="bt-meta">対象 '+s.races+'レース／結果なし '+s.noResult+'／不成立 '+s.skipped+'</div>';
  h+='<table class="bt-table"><thead><tr>';
  h+='<th>点数</th><th>的中</th><th>的中率</th><th>回収率</th></tr></thead><tbody>';
  var bestRoi=-1, bestN=0;
  for(var i=0;i<s.rows.length;i++){ if(s.rows[i].roi>bestRoi){ bestRoi=s.rows[i].roi; bestN=s.rows[i].n; } }
  for(var i=0;i<s.rows.length;i++){
    var r=s.rows[i];
    var cls=(r.n===bestN)?' class="bt-best"':'';
    var roiCls=(r.roi>=100)?'bt-pos':'bt-neg';
    h+='<tr'+cls+'>';
    h+='<td>'+r.n+'点</td>';
    h+='<td>'+r.hit+'</td>';
    h+='<td>'+r.hitRate.toFixed(1)+'%</td>';
    h+='<td class="'+roiCls+'">'+r.roi.toFixed(1)+'%</td>';
    h+='</tr>';
  }
  h+='</tbody></table>';
  h+='<div class="bt-best-note">最高回収率: '+bestN+'点買い ('+bestRoi.toFixed(1)+'%)</div>';

  // ---- レース明細: 点数をドロップリストで選んで切替 ----
  var det=s.detail||[];
  if(det.length){
    _BT_DETAIL_N=bestN;   // 既定は最高回収率の点数
    h+='<div class="bt-detail-h">レース明細';
    h+='<select id="btDetailN" class="bt-detail-sel" onchange="btChangeDetailN(this.value)">';
    for(var n=1;n<=_BT_MAXN;n++){
      var sel2=(n===bestN)?" selected":"";
      h+='<option value="'+n+'"'+sel2+'>'+n+'点買い</option>';
    }
    h+='</select></div>';
    h+='<div id="btDetailBody"></div>';
  }
  rEl.innerHTML=h;
  if(det.length) __btRenderDetail(_BT_DETAIL_N);
}

// 会場フィルター変更時: rawから絞り直して再描画
function btChangeVenue(v){
  _BT_VENUE_FILTER=v||"";
  if(!_BT_LAST_RAW) return;
  var bundle;
  if(_BT_VENUE_FILTER===""){
    bundle=__btRawToBundle(_BT_LAST_RAW);
  } else {
    var vf=_BT_VENUE_FILTER;
    bundle=__btRawToBundleFiltered(_BT_LAST_RAW, function(d){ return d.venue===vf; });
  }
  _BT_LAST_BUNDLE=bundle;
  var summary;
  if(_BT_TAB_MAIN==="omk") summary=(bundle.omk||{})[_BT_TAB_SUB];
  else summary=(bundle.ora||{})[_BT_TAB_SUB];
  if(summary) __btRenderResult(summary);
}

// 明細本体を指定点数Nで描画
function btTogglePicks(i){
  var el=document.getElementById("btPick"+i);
  if(!el) return;
  el.style.display=(el.style.display==="none")?"":"none";
}

// v329: 明細に「どんなレースだったか」を出す。
//   賭けなくていいレースを分類するための材料をここに並べる。
//   古い集計データには入っていないので、その場合は何も出さない。
function __btMetaHtml(dd){
  if(!dd) return "";
  var has=(dd.q||dd.lineCls||dd.grade||dd.cls||dd.kind||dd.line);
  if(!has) return "";
  var h='<div class="bt-meta">';
  h+='<div class="bt-meta-tags">';
  if(dd.q) h+='<span class="bt-mt on">'+esc(dd.q)+'</span>';
  if(dd.lineCls) h+='<span class="bt-mt on">'+esc(dd.lineCls)+'</span>';
  if(dd.grade) h+='<span class="bt-mt">'+esc(dd.grade)+'</span>';
  if(dd.cls) h+='<span class="bt-mt">'+esc(dd.cls)+'</span>';
  if(dd.kind) h+='<span class="bt-mt">'+esc(dd.kind)+'</span>';
  h+='</div>';
  function row(lab,val){
    if(!val) return "";
    return '<div class="bt-meta-row"><span class="bt-ml">'+lab+'</span>'
         + '<span class="bt-mv">'+esc(val)+'</span></div>';
  }
  if(dd.kimari1 || dd.kimari2){
    var kk="1着 "+(dd.kimari1||"―");
    if(dd.kimari2) kk+="　2着 "+dd.kimari2;
    h+='<div class="bt-meta-kim">'+esc(kk)+'</div>';
  }
  h+=row("ライン構成", dd.line);
  h+=row("rawscore順位", dd.rsOrder);
  h+=row("戦術", dd.stOrder);
  function hasVal(v){
    if(!v) return false;
    return String(v).replace(/[-]/g,"").length>0;
  }
  if(hasVal(dd.sOrder)) h+=row("S回数", dd.sOrder);
  if(hasVal(dd.bOrder)) h+=row("B回数", dd.bOrder);
  h+=row("周回並び", dd.lap);
  h+='</div>';
  return h;
}

function __btRenderDetail(N){
  var s=_BT_LAST_SUMMARY;
  if(!s) return;
  var body=document.getElementById("btDetailBody");
  if(!body) return;
  var det=s.detail||[];
  var h='';
  var hitCnt=0, retSum=0;
  h+='<table class="bt-dtable"><thead><tr>';
  h+='<th class="bt-c-date">日付</th><th class="bt-c-race">レース</th>'
   + '<th class="bt-c-lab"></th><th class="bt-c-am">軸</th><th class="bt-c-tri">結果</th>'
   + '<th class="bt-c-ref">払戻</th><th class="bt-c-hit"></th></tr></thead><tbody>';
  var betSum=0, nBet=0, nSkip=0, boughtSum=0;
  for(var di=0; di<det.length; di++){
    var dd=det[di];
    var hitInN, nb;
    if(dd.unionFiltered && dd.unionPts){
      // v322: 和集合。重複をまとめた後の点数。
      nb=dd.unionPts[N-1]||0;
      hitInN=!!(dd.unionHit && dd.unionHit[N-1]);
    } else if(dd.evFiltered && dd.boughtN){
      // v317: EVフィルタ時。実際に買った点数で判定する。
      nb=dd.boughtN[N-1]||0;
      hitInN=!!(dd.hitAtN && dd.hitAtN[N-1]);
    } else {
      nb=(N<=dd.navail)?N:dd.navail;
      hitInN=(dd.hitIndex>=0 && dd.hitIndex<N);
    }
    boughtSum+=nb;
    betSum+=nb*100;
    if(nb>0) nBet++; else nSkip++;
    var refTxt;
    if(hitInN){
      refTxt=(dd.refund>0)?(dd.refund.toLocaleString()+"円"):"―";
      hitCnt++; retSum+=(dd.refund||0);
    } else {
      refTxt="―";
    }
    var amTxt=(dd.axisMark)?dd.axisMark:"―";
    // ラベルバー列: レース内に弱/穴/離のラベルを持つ選手がいれば各色のバーを縦に積む
    var barH='';
    if(dd.labWeak) barH+='<span class="bt-bar bt-bar-weak"></span>';
    if(dd.labAna) barH+='<span class="bt-bar bt-bar-ana"></span>';
    if(dd.labLayoff) barH+='<span class="bt-bar bt-bar-layoff"></span>';
    var labCell=barH?('<span class="bt-lab-stack">'+barH+'</span>'):'';
    var hitBadge=hitInN?'<span class="bt-hit-badge">的</span>':'';
    var md=dd.date.slice(4,6)+"/"+dd.date.slice(6,8);
    var canExp=((dd.evFiltered && dd.picks && dd.picks.length) ||
                (dd.unionFiltered && (dd.unionCombosN || dd.unionCombos)))
               ?true:false;
    if(canExp){
      h+='<tr class="bt-dt-row bt-dt-exp" onclick="btTogglePicks('+di+')">';
    } else {
      h+='<tr>';
    }
    h+='<td class="bt-c-date">'+md+(canExp?' <span class="bt-caret">▶</span>':'')+'</td>';
    var raceTxt=esc(dd.key||"");
    if(dd.post){ raceTxt=raceTxt+'<br><span class="bt-post">'+esc(dd.post)+'</span>'; }
    h+='<td class="bt-c-race">'+raceTxt+'</td>';
    h+='<td class="bt-c-lab">'+labCell+'</td>';
    h+='<td class="bt-c-am bt-am">'+amTxt+'</td>';
    h+='<td class="bt-c-tri bt-dt-tri">'+esc(dd.trifecta)+'</td>';
    h+='<td class="bt-c-ref bt-dt-ref">'+refTxt+'</td>';
    h+='<td class="bt-c-hit">'+hitBadge+'</td>';
    h+='</tr>';
    if(canExp){
      // v318: 買い目の内訳。託宣が選んだ候補と、EVで残した買い目を明示する。
      h+='<tr id="btPick'+di+'" class="bt-pick-row" style="display:none">';
      h+='<td colspan="7"><div class="bt-picks">';
      if(dd.unionFiltered){
        // v322: 和集合の内訳
        // 買った中に的中があった場合だけ払戻を受け取る
        var pay=(hitInN && dd.refund>0)?dd.refund:0;
        // v323: 結果の横に3連単の払戻を出す(的中の有無に関わらず)
        var payAll=(dd.refund>0)?(dd.refund.toLocaleString()+'円'):'―';
        h+='<div class="bt-picks-h">'
          + esc(dd.venue||"") + ' ' + esc(dd.raceNo?(dd.raceNo+"R"):"")
          + (dd.post?('　'+esc(dd.post)):'')
          + '　結果 ' + esc(dd.trifecta)
          + '　<span class="bt-pk-pay">3連単 ' + payAll + '</span>'
          + '</div>';
        h+=__btMetaHtml(dd);
        h+='<div class="bt-picks-h">各系列'+N+'点 → 重複をまとめて '
          + nb + '点購入　投資 ' + (nb*100).toLocaleString() + '円'
          + '　払戻 ' + pay.toLocaleString() + '円'
          + '　収支 ' + (pay-nb*100).toLocaleString() + '円</div>';
        var perNow=null;
        if(dd.unionPerN && dd.unionPerN[N-1]){ perNow=dd.unionPerN[N-1]; }
        else if(dd.unionPer){ perNow=dd.unionPer; }
        if(perNow && perNow.length){
          var axn=["合議","軸1人","軸2人","軸3人"];
          var pl2=[];
          var pz=0;
          while(pz<perNow.length){
            var pe=perNow[pz];
            pz=pz+1;
            pl2.push((axn[pe.ax]||("軸"+pe.ax))+' '+pe.n+'点');
          }
          h+='<div class="bt-picks-h">内訳: '+pl2.join('　')+'</div>';
        }
      } else {
      h+='<div class="bt-picks-h">'+esc(dd.key)+'　候補'+N+'点中 '
        +nb+'点を投票（結果 '+esc(dd.trifecta)+'）</div>';
      }
      if(dd.unionFiltered){
        // v322: 和集合の買い目を並べる
        var uc=[];
        if(dd.unionCombosN && dd.unionCombosN[N-1]){
          uc=dd.unionCombosN[N-1];
        } else if(dd.unionCombos){
          // v322で保存した古いデータ(k=6固定)。件数が合わない場合がある。
          uc=dd.unionCombos;
        }
        var uz=0;
        while(uz<uc.length && uz<nb){
          var isH=(uc[uz]===dd.trifecta);
          h+='<div class="bt-pick bt-pick-on'+(isH?' bt-pick-hit':'')+'">';
          h+='<span class="bt-pk-no">'+(uz+1)+'</span>';
          h+='<span class="bt-pk-c">'+esc(uc[uz])+'</span>';
          if(isH){
            h+='<span class="bt-pk-hit">的中 '
              +(dd.refund||0).toLocaleString()+'円</span>';
          }
          h+='</div>';
          uz=uz+1;
        }
        h+='</div></td></tr>';
        continue;
      }
      var pl=(N<dd.picks.length)?N:dd.picks.length;
      for(var pj=0; pj<pl; pj++){
        var pk=dd.picks[pj];
        var voted=(pk.ev>=1.0);
        var isHit=(dd.hitIndex===pj);
        var cls='bt-pick'+(voted?' bt-pick-on':' bt-pick-off')+(isHit?' bt-pick-hit':'');
        h+='<div class="'+cls+'">';
        h+='<span class="bt-pk-no">'+(pj+1)+'</span>';
        h+='<span class="bt-pk-c">'+esc(pk.c)+'</span>';
        h+='<span class="bt-pk-p">確率'+(pk.p*100).toFixed(2)+'%</span>';
        h+='<span class="bt-pk-o">'+(pk.od>0?(pk.od.toFixed(1)+'倍'):'オッズ無')+'</span>';
        h+='<span class="bt-pk-ev">EV'+pk.ev.toFixed(2)+'</span>';
        h+='<span class="bt-pk-v">'+(voted?'投票':'見送り')+'</span>';
        if(isHit) h+='<span class="bt-pk-hit">的中'+(dd.refund?(' '+dd.refund.toLocaleString()+'円'):'')+'</span>';
        h+='</div>';
      }
      h+='</div></td></tr>';
    }
  }
  h+='</tbody></table>';
  var roi=(betSum>0)?(retSum/betSum*100):0;
  var isEV=(det.length>0 && det[0].evFiltered)?true:false;
  var isUni=(det.length>0 && det[0].unionFiltered)?true:false;
  var denom=(isEV||isUni)?nBet:det.length;
  var hr=(denom>0)?(hitCnt/denom*100):0;
  h+='<div class="bt-detail-note">';
  if(isUni){
    // v322: 和集合。表と同じ betSum/retSum から計算しているので一致する。
    var avgU=(nBet>0)?(boughtSum/nBet):0;
    h+='各系列'+N+'点を重ね、重複をまとめて購入: '
     + '参加 '+nBet+'/'+det.length+'R'
     + (nSkip>0?('（見送り '+nSkip+'R）'):'')
     + '／平均 '+avgU.toFixed(2)+'点'
     + '／的中 '+hitCnt+'/'+nBet+'（'+hr.toFixed(1)+'%）'
     + '／投資 '+betSum.toLocaleString()+'円'
     + '／払戻 '+retSum.toLocaleString()+'円'
     + '／収支 '+(retSum-betSum).toLocaleString()+'円'
     + '／回収率 '+roi.toFixed(1)+'%。'
     + '<br>重複した買い目は1点として数える。'
     + '同じ買い目を複数系列が出すほど点数が減る。'
     + '<br>行をタップすると系列ごとの内訳と買い目が出る。';
  } else if(isEV){
    // 過去に期待値方式で保存したデータを表示する場合のみ通る
    var avgB=(nBet>0)?(boughtSum/nBet):0;
    h+='[旧方式] 候補'+N+'点のうち期待値1.0以上のみ購入: '
     + '参加 '+nBet+'/'+det.length+'R（見送り '+nSkip+'R）'
     + '／平均 '+avgB.toFixed(2)+'点'
     + '／的中 '+hitCnt+'/'+nBet+'（'+hr.toFixed(1)+'%）'
     + '／賭金 '+betSum.toLocaleString()+'円'
     + '／払戻 '+retSum.toLocaleString()+'円／回収率 '+roi.toFixed(1)+'%。'
     + '<br>上の表の的中率は全'+det.length+'Rを母数にした値のため、'
     + 'ここの参加ベース的中率とは異なる。';
  } else {
    h+=N+'点買い: 的中 '+hitCnt+'/'+det.length
     + '（'+hr.toFixed(1)+'%）'
     + '／払戻計 '+retSum.toLocaleString()+'円／回収率 '+roi.toFixed(1)+'%。';
  }
  if(!isUni){
    h+='<br>軸=確定3連単1着車の予想印。「的中」='+N
     +'点買いに含まれたレース。払戻は3連単。';
  }
  h+='</div>';
  body.innerHTML=h;
}

function btChangeDetailN(v){
  var n=parseInt(v,10); if(isNaN(n)||n<1)n=1; if(n>_BT_MAXN)n=_BT_MAXN;
  _BT_DETAIL_N=n;
  __btRenderDetail(n);
}

// 託宣の予想本体。layers:{honsen:[],osae:[],ana:[]} を返す。
// ---- 軸×特定決まり手シナリオ単位で2着3着のcomboを生成 ----
//   その決まり手(kim)の遷移だけを使う(確率混合しない)。
//   戻り値: [{b:[b1,b2,b3], sc, fluct}]
var _ORA_DBG={call:0,noLink:0,items:0,badLabel2:0,noBikes2:0,noThird:0,badLabel3:0,noBikes3:0,out:0,lab:"",cell:""};
function __oraDbgReset(){
  _ORA_DBG={call:0,noLink:0,items:0,badLabel2:0,noBikes2:0,noThird:0,badLabel3:0,noBikes3:0,out:0,lab:"",cell:""};
}
function __oraDbgText(){
  var d=_ORA_DBG;
  return 'cell='+d.cell+' 実ラベル="'+d.lab+'" '
    +'call='+d.call+' link無='+d.noLink+' items='+d.items
    +' ラベル2不正='+d.badLabel2+' 車番2空='+d.noBikes2
    +' 3着無='+d.noThird+' ラベル3不正='+d.badLabel3
    +' 車番3空='+d.noBikes3+' 生成='+d.out;
}
function __oraScenarioCombos(axis, kim, kimP, lineInfo, base, allBikes){
  var out=[];
  _ORA_DBG.call++;
  var link=__oraLink(kimP, kim);
  if(!link || !link.items){ _ORA_DBG.noLink++; return out; }
  var linkN=link.n||0;
  _ORA_DBG.items+=link.items.length;
  for(var s=0;s<link.items.length;s++){
    var it2=link.items[s];
    var p2=__oraParseLabel(it2.label);
    if(!p2){
      _ORA_DBG.badLabel2++;
      if(!_ORA_DBG.lab){ _ORA_DBG.lab=String(it2.label); }
      continue;
    }
    var bikes2=__oraLabelToBikes(p2.side, p2.pos, axis, lineInfo, allBikes);
    if(!bikes2.length){ _ORA_DBG.noBikes2++; continue; }
    var rate2=(it2.rate!=null)?it2.rate/100.0:0;
    var base2=(it2.base_rate!=null)?it2.base_rate/100.0:null;
    var n2=Math.round(rate2*linkN);
    var fl2=__oraFluct(rate2, base2, n2);
    var cand2=__oraRankCandidates(bikes2, base, p2.kim);
    for(var c2=0;c2<cand2.length;c2++){
      var b2=cand2[c2].bike;
      if(b2===axis) continue;
      var sc2=rate2*fl2*cand2[c2].apt;
      var thirds=it2.third||[];
      if(!thirds.length){ _ORA_DBG.noThird++; continue; }
      var thirdN=it2.third_n||0;
      for(var t=0;t<thirds.length;t++){
        var it3=thirds[t];
        var p3=__oraParseLabel(it3.label);
        if(!p3){ _ORA_DBG.badLabel3++; continue; }
        var bikes3=__oraLabelToBikes(p3.side, p3.pos, axis, lineInfo, allBikes);
        if(!bikes3.length){ _ORA_DBG.noBikes3++; continue; }
        var rate3=(it3.rate!=null)?it3.rate/100.0:0;
        var base3=(it3.base_rate!=null)?it3.base_rate/100.0:null;
        var n3=Math.round(rate3*thirdN);
        var fl3=__oraFluct(rate3, base3, n3);
        var cand3=__oraRankCandidates(bikes3, base, '');
        for(var c3=0;c3<cand3.length;c3++){
          var b3=cand3[c3].bike;
          if(b3===axis || b3===b2) continue;
          var sc3=sc2*rate3*fl3*cand3[c3].apt;
          out.push({b:[axis,b2,b3], sc:sc3, fluct:fl2*fl3});
          _ORA_DBG.out++;
        }
      }
    }
  }
  return out;
}

// ---- 軸の決まり手を順位づけ(その選手の決まり手% × 1着遷移揺らぎ) ----
//   戻り値: [{kim, w, playerRate}] 降順。
function __oraAxisKimRank(axisBike, kimP, base){
  var o=base[axisBike];
  var a1=__oraAxis1st(kimP);
  var arr=[];
  for(var x=0;x<a1.length;x++){
    var kk=a1[x].kim;
    var pr=__oraKimRate(o?o.kimari:null, kk);
    var prv=(pr!=null)?pr:0.0;
    var w=prv * a1[x].rate * a1[x].fluct;
    arr.push({kim:kk, w:w, playerRate:prv});
  }
  arr.sort(function(a,b){ return b.w-a.w; });
  return arr;
}

function __oraOmakasePredict(d, mode, nPts){
  var players=d.players;
  var kimP=(_RACE && _RACE.kimari && _RACE.kimari.exists)? _RACE.kimari : null;
  if(!kimP){ return {error:'このレースは決まり手遷移データがなく託宣できません。'}; }
  var lineDisp=(_RACE && _RACE.header)? (_RACE.header.line_display||'') : '';
  var lineInfo=__oraParseLines(lineDisp);
  var base=__oraPlayerBase(players);
  var allBikes=[]; for(var i=0;i<players.length;i++){ allBikes.push(String(players[i].bike)); }

  // ラベル参照(穴/勝負弱/離脱明け)
  var labelOf={};
  for(var li=0; li<players.length; li++){
    var p=players[li];
    labelOf[String(p.bike)]={
      kind:p.label_kind||null, hit:p.label_hit||0, den:p.label_den||0,
      layoff:(p.layoff_kind==='layoff'), gap:p.layoff_gap||0
    };
  }

  // 1. 各車の「1着力」を算出(複合力 × 最良1着決まり手の揺らぎ × その決まり手率)
  var a1=__oraAxis1st(kimP);
  var firstForce={};
  for(var b=0;b<allBikes.length;b++){
    var bk=allBikes[b]; var o=base[bk]; if(!o) continue;
    var force=0.45*o.sN + 0.30*o.mN + 0.10*o.rN + 0.15;
    // 軸が最も得意な決まり手×その型の遷移揺らぎ
    var bestW=0;
    for(var x=0;x<a1.length;x++){
      var rr=__oraKimRate(o.kimari, a1[x].kim);
      var rv=(rr!=null)?rr:0.25;
      var w=a1[x].rate*a1[x].fluct*rv;
      if(w>bestW) bestW=w;
    }
    var ff=force*(0.4+0.6*bestW*3);  // bestWは小さめなので増幅
    // 勝負弱は1着力を減点(頭固定の過信を防ぐ)
    if(labelOf[bk] && labelOf[bk].kind==='weak') ff*=0.6;
    // 離脱明けは減点(離脱日数で強める: 31日=軽, 180日+=重)
    if(labelOf[bk] && labelOf[bk].layoff){
      var gp=labelOf[bk].gap||31;
      var pen=Math.min(0.5, (gp-31)/300.0 + 0.1); // 0.1〜0.5の減点率
      ff*=(1.0-pen);
    }
    firstForce[bk]=ff;
  }

  // === 3柱合議による軸選定 ===
  // 柱A: firstForce(決まり手遷移×複合力)
  // 柱B: rsrank axisScore(絶対1着率×conf×揺らぎ)
  // 柱C: 頻出買い目(top_trifectas)の1着車集計スコア
  // 3柱を各々正規化(最大1)→単純平均→ランキング→上位3軸まで。

  // 柱A正規化
  var _maxA=0;
  for(var fa=0;fa<allBikes.length;fa++){ var _va=firstForce[allBikes[fa]]||0; if(_va>_maxA)_maxA=_va; }
  if(_maxA<=0)_maxA=1;

  // 柱B: rsrank axisScore
  var _rsrMap=__oraRsrMap(players);
  var _axisB={}; var _maxB=0;
  for(var rb=0;rb<allBikes.length;rb++){
    var _bk=allBikes[rb];
    var _rf=__oraRsrAxisScore(_rsrMap[_bk]);
    _axisB[_bk]=_rf; if(_rf>_maxB)_maxB=_rf;
  }
  if(_maxB<=0)_maxB=1;

  // 柱C: top_trifectas の1着車集計
  // _RACE.patterns[0].formations から1着車を集計しスコア化
  var _axisC={}; var _maxC=0;
  (function(){
    var pats=(_RACE && _RACE.patterns)? _RACE.patterns : [];
    for(var pi=0;pi<pats.length;pi++){
      var fms=pats[pi].formations||[];
      for(var fi=0;fi<fms.length;fi++){
        var fm=fms[fi];
        if(!fm || !fm.bikes || fm.bikes.length<1) continue;
        var b1=String(fm.bikes[0]);
        var w=fm.rate||0;
        _axisC[b1]=(_axisC[b1]||0)+w;
        if(_axisC[b1]>_maxC)_maxC=_axisC[b1];
      }
    }
  })();
  if(_maxC<=0)_maxC=1;

  // 3柱等重み合成 → axisTotal スコア
  var axisTotal={};
  for(var ab=0;ab<allBikes.length;ab++){
    var _b=allBikes[ab];
    var aN=(firstForce[_b]||0)/_maxA;
    var bN=(_axisB[_b]||0)/_maxB;
    var cN=(_axisC[_b]||0)/_maxC;
    axisTotal[_b]=(aN+bN+cN)/3.0;
  }

  // ランキング → 上位3軸まで(比0.60/0.75の閾値は維持)
  var rankBikes=allBikes.slice().sort(function(a,b){ return (axisTotal[b]||0)-(axisTotal[a]||0); });
  if(rankBikes.length<3){ return {error:'出走車が少なく託宣できません。'}; }

  var f0=axisTotal[rankBikes[0]]||0.0001;
  var axes=[rankBikes[0]];
  if(_ORA_OMK_AXIS>=1){
    // 軸人数を強制指定: 上位から指定数だけ採用(出走数で頭打ち)
    var want=_ORA_OMK_AXIS;
    if(want>rankBikes.length) want=rankBikes.length;
    axes=[];
    for(var ax=0; ax<want; ax++){ axes.push(rankBikes[ax]); }
  } else {
    // 自動: 比0.60/0.75の閾値で上位3軸まで
    if((axisTotal[rankBikes[1]]||0)/f0 >= 0.60) axes.push(rankBikes[1]);
    if(rankBikes.length>2 && axes.length>=2 && (axisTotal[rankBikes[2]]||0)/f0 >= 0.75) axes.push(rankBikes[2]);
  }
  var axisDominant=(axes.length===1);

  // ラベル補正(層別)
  function comboLabelBonus(parts, layer){
    var bonus=1.0;
    for(var pi=0; pi<parts.length; pi++){
      var lb=labelOf[parts[pi]]; if(!lb) continue;
      if(lb.kind==='ana'){
        var hr=(lb.den>0)? (lb.hit/lb.den) : 0.25;
        if(layer==='ana') bonus*=(1.0+0.8*(0.3+hr));
        else if(layer==='osae') bonus*=(1.0+0.25*(0.3+hr));
      }
      if(lb.layoff){
        var gp=lb.gap||31; var pen=Math.min(0.5,(gp-31)/300.0+0.1);
        bonus*=(1.0-pen*0.7);
      }
      if(lb.kind==='weak' && pi===0 && layer==='honsen') bonus*=0.6;
    }
    return bonus;
  }

  // 3. 各軸の決まり手を順位づけ → 決まり手順位で層を決める
  //    最有力=本線 / 2番目=抑え / 3番目以下=穴目。
  //    シナリオ妥当性(その手で勝つ力)が極端に低いものは足切り。
  __oraDbgReset();
  _ORA_DBG.cell=String((kimP&&kimP.cell_key)||'?');
  var _dbgKrank=0, _dbgPrateNG=0;
  var KIM_CUT=0.02;   // 決まり手シナリオの足切り(w=選手率×rate×揺らぎ)
  var PRATE_CUT=0.08; // 選手のその決まり手率がこれ未満なら非現実(打たない手)
  var honAcc={}, osaAcc={}, anaAcc={};   // key -> {sc,fluct,parts}
  function accum(dst, combos, layer){
    for(var i=0;i<combos.length;i++){
      var c=combos[i]; var key=c.b.join('-');
      var add=c.sc*comboLabelBonus(c.b, layer);
      if(layer==='ana') add=c.sc*c.fluct*comboLabelBonus(c.b,'ana'); // 穴は揺らぎ強調
      if(!dst[key]) dst[key]={sc:0, fluct:0, parts:c.b};
      dst[key].sc+=add;
      dst[key].fluct=Math.max(dst[key].fluct, c.fluct);
    }
  }
  for(var ax=0; ax<axes.length; ax++){
    var axis=axes[ax];
    var krank=__oraAxisKimRank(axis, kimP, base);
    // 軸の1着力(firstForce)を重みに掛けて軸間のバランスを取る
    var axW=(firstForce[axis]||0)/f0;
    for(var ri=0; ri<krank.length; ri++){
      var kr=krank[ri];
      _dbgKrank++;
      if(kr.playerRate < PRATE_CUT){ _dbgPrateNG++; continue; }   // 打たない手は除外
      if(kr.w < KIM_CUT && ri>0) continue;       // 2番目以降で妥当性低すぎは除外
      var combos=__oraScenarioCombos(axis, kr.kim, kimP, lineInfo, base, allBikes);
      if(!combos.length) continue;
      // 軸力×決まり手妥当性 でこのシナリオ全体を重み付け
      var sw=axW*(0.3+0.7*Math.min(1,kr.w*8));
      for(var ci=0; ci<combos.length; ci++){ combos[ci].sc*=sw; }
      // 決まり手順位で層に割り当て
      if(ri===0) accum(honAcc, combos, 'honsen');
      else if(ri===1) accum(osaAcc, combos, 'osae');
      else accum(anaAcc, combos, 'ana');
    }
  }

  function toList(acc, layer){
    var arr=[];
    for(var k in acc){ if(acc.hasOwnProperty(k)){
      arr.push({b:acc[k].parts, sc:acc[k].sc, fluct:acc[k].fluct, _l:layer});
    }}
    return arr;
  }
  var honList=toList(honAcc,'honsen');
  var osaList=toList(osaAcc,'osae');
  var anaList=toList(anaAcc,'ana');
  if(!honList.length && !osaList.length && !anaList.length){
    return {error:'決まり手シナリオから買い目を構成できませんでした。 [軸='
      +axes.length+' 決手候補='+_dbgKrank+' 率不足='+_dbgPrateNG+' '
      +__oraDbgText()+']'};
  }

  // 4. 点数配分(本線50/抑え30/穴目20)。各層内スコア降順。重複排除。
  var nH=Math.round(nPts*0.5), nO=Math.round(nPts*0.3), nA=nPts-nH-nO;
  if(nA<0) nA=0;
  honList.sort(function(a,b){ return b.sc-a.sc; });
  osaList.sort(function(a,b){ return b.sc-a.sc; });
  anaList.sort(function(a,b){ return b.sc-a.sc; });

  var used={}; var honsen=[], osae=[], ana=[];
  function take(list, n, dst){
    for(var i=0;i<list.length && dst.length<n;i++){
      var kk=list[i].b.join('-');
      if(used[kk]) continue;
      used[kk]=1; dst.push(list[i]);
    }
  }
  take(honList, nH, honsen);
  take(anaList, nA, ana);
  take(osaList, nO, osae);
  // 不足分は全層の未使用候補を統合し、スコア降順で埋める(点数を使い切る)。
  // 層内プールだけ見る旧方式だと、薄い層の枠が埋まらず総点数が指定に届かなかった。
  var got=honsen.length+osae.length+ana.length;
  if(got<nPts){
    var rest=[];
    function pushRest(list,dst){
      for(var i=0;i<list.length;i++){
        var kk=list[i].b.join('-');
        if(used[kk]) continue;
        rest.push({item:list[i], dst:dst});
      }
    }
    pushRest(honList, honsen);
    pushRest(osaList, osae);
    pushRest(anaList, ana);
    rest.sort(function(a,b){ return b.item.sc-a.item.sc; });
    for(var ri2=0; ri2<rest.length && got<nPts; ri2++){
      var kk2=rest[ri2].item.b.join('-');
      if(used[kk2]) continue;
      used[kk2]=1;
      rest[ri2].dst.push(rest[ri2].item);
      got++;
    }
  }
  // 最終フォールバック: 決まり手シナリオの組合せ自体が乏しく依然不足する場合、
  // 軸×rsrank着順揺らぎの総当たりで穴目層を補完し、指定点数を必ず満たす。
  if(got<nPts){
    var fb=[];
    for(var fa=0; fa<axes.length; fa++){
      var fax=axes[fa];
      for(var i2=0;i2<allBikes.length;i2++){
        var bb2=allBikes[i2]; if(bb2===fax) continue;
        for(var i3=0;i3<allBikes.length;i3++){
          var bb3=allBikes[i3]; if(bb3===fax||bb3===bb2) continue;
          var kkf=[fax,bb2,bb3].join('-');
          if(used[kkf]) continue;
          var rs=__oraRsrScore(_rsrMap, fax, bb2, bb3);  // 揺らぎ積(データ無=既定値)
          fb.push({b:[fax,bb2,bb3], sc:rs, fluct:rs, _l:'ana', key:kkf});
        }
      }
    }
    fb.sort(function(a,b){ return b.sc-a.sc; });
    for(var fi2=0; fi2<fb.length && got<nPts; fi2++){
      if(used[fb[fi2].key]) continue;
      used[fb[fi2].key]=1;
      ana.push({b:fb[fi2].b, sc:fb[fi2].sc, fluct:fb[fi2].fluct, _l:'ana'});
      got++;
    }
  }

  function norm(list){
    var mx=0; for(var i=0;i<list.length;i++){ if(list[i].sc>mx)mx=list[i].sc; }
    if(mx<=0)mx=1;
    for(var i=0;i<list.length;i++){ list[i].score=Math.round(list[i].sc/mx*1000)/10; }
  }
  norm(honsen); norm(osae); norm(ana);

  // 各軸の決まり手シナリオ要約(表示用)
  var axisInfo=[];
  for(var ax2=0; ax2<axes.length; ax2++){
    var kr2=__oraAxisKimRank(axes[ax2], kimP, base);
    var top=null;
    for(var z=0;z<kr2.length;z++){ if(kr2[z].playerRate>=PRATE_CUT){ top=kr2[z]; break; } }
    axisInfo.push({bike:axes[ax2], kim: top?top.kim:'-'});
  }

  return {layers:{honsen:honsen, osae:osae, ana:ana},
          axes:axes, axisInfo:axisInfo, axisDominant:axisDominant,
          base:base, labelOf:labelOf, mode:mode};
}

function __oraOmakaseHtml(pred){
  var L=pred.layers;
  var axhtml=(pred.axisInfo||[]).map(function(ai){
    return '<span class="ora-axkim"><span class="ora-bk bcol'+esc(ai.bike)+'">'+esc(ai.bike)+'</span>'
         + '<span class="ora-kim">'+esc(ai.kim)+'</span></span>';
  }).join('');
  var html='<div class="ora-rhead">託宣（バランス）— 軸ながし '+axhtml+'</div>';
  html+=__oraLayerBlock('本線', 'honsen', L.honsen, pred.labelOf);
  html+=__oraLayerBlock('抑え', 'osae', L.osae, pred.labelOf);
  html+=__oraLayerBlock('穴目', 'ana', L.ana, pred.labelOf);
  var total=L.honsen.length+L.osae.length+L.ana.length;
  html+='<div class="ora-foot">計'+total+'点 / 本線50%・抑え30%・穴目20%。'
    + '穴目は揺らぎ×穴サイド実績で選定、勝負弱は本線頭で減点、離脱明けは離脱日数で減点。'
    + '<br>右端の数値は表示時点の3連単オッズ（倍）。</div>';
  return html;
}

// 結果が出たレースの的中3連単(例 "4-1-2")を取得。無ければ null。
function __oraHitCombo(){
  if(!_RACE || !_RACE.race_result) return null;
  var t=_RACE.race_result.trifecta;
  if(!t) return null;
  var parts=String(t).split('-');
  if(parts.length<3) return null;
  return parts;
}
// 買い目 b(配列)が的中3連単と完全一致(着順込み)なら的中バッジHTMLを返す。
function __oraHitBadge(b){
  var hit=__oraHitCombo();
  if(!hit || !b || b.length<3) return '';
  if(String(b[0])===hit[0] && String(b[1])===hit[1] && String(b[2])===hit[2]){
    return '<span class="ora-hit">的中</span>';
  }
  return '';
}

function __oraLayerBlock(title, cls, list, labelOf){
  if(!list || !list.length) return '';
  var html='<div class="ora-layer ora-'+esc(cls)+'">'
    + '<div class="ora-lh">'+esc(title)+'<span class="ora-lc">'+list.length+'点</span></div>';
  html+='<div class="ora-list">';
  var hitCombo=__oraHitCombo();
  for(var r=0;r<list.length;r++){
    var c=list[r];
    // 買い目行のラベルは全廃(的中/弱/穴/離)。ただし的中「枠」(緑)は復活。
    // 的中3連単と着順込みで完全一致した行に ora-item-hit を付与する。
    var hitCls='';
    if(hitCombo && c.b && c.b.length>=3
        && String(c.b[0])===hitCombo[0]
        && String(c.b[1])===hitCombo[1]
        && String(c.b[2])===hitCombo[2]){
      hitCls=' ora-item-hit';
    }
    html+='<div class="ora-item'+hitCls+'">'
      + '<span class="ora-rank">'+(r+1)+'</span>'
      + '<span class="ora-bikes">'
      +   '<span class="ora-bk bcol'+esc(c.b[0])+'">'+esc(c.b[0])+'</span>'
      +   '<span class="ora-arr">&#8594;</span>'
      +   '<span class="ora-bk bcol'+esc(c.b[1])+'">'+esc(c.b[1])+'</span>'
      +   '<span class="ora-arr">&#8594;</span>'
      +   '<span class="ora-bk bcol'+esc(c.b[2])+'">'+esc(c.b[2])+'</span>'
      + '</span>'
      + '<span class="ora-score od-odds" data-combo="'+esc(c.b.join('-'))+'"><span class="od-loading">…</span></span>'
      + '</div>';
  }
  html+='</div></div>';
  return html;
}




function rsrankHtml(d){
  if(!d || !d.available) return '<div class="nodata">score順位データがありません（player_rsrank_finish_7car_FINAL.jsonl 未配置）</div>';
  var rows=d.rows||[];
  if(!rows.length) return '<div class="nodata">対象データがありません</div>';
  var labels=["1着","2着","3着","4着","5着","6着","7着"];
  var html='';
  if(d.scope==="cond"){
    html+='<div class="nodata" style="padding:2px 0 8px">区分: '+esc(d.cond_label)+'</div>';
  }
  for(var i=0;i<rows.length;i++){
    var r=rows[i];
    html+='<div class="grp-title"><span class="gbike bcol'+esc(r.bike)+'">'+esc(r.bike)+'</span>'
        + esc(r.name)+' <span style="color:var(--txt-dim);font-weight:600">（score'+esc(r.rs_rank)+'位）</span>'
        + (r.label_kind?'<span class="plabel '+r.label_kind+'">'+esc(r.label_text)+'</span>':'')
        + (r.layoff_kind?'<span class="plabel layoff">'+esc(r.layoff_text)+'</span>':'');
    var nlab = r.self&&r.self.n!=null ? 'n='+r.self.n : '';
    html+='<span class="gn">'+nlab+'</span></div>';
    if(!r.self){
      html+='<div class="nodata">この選手の該当データなし</div>';
      continue;
    }
    // 正規化max = self/baseline の全着順最大
    var mx=0;
    for(var k=0;k<7;k++){
      if(r.self.pct[k]>mx) mx=r.self.pct[k];
      if(r.baseline&&r.baseline.pct&&r.baseline.pct[k]>mx) mx=r.baseline.pct[k];
    }
    if(mx<=0) mx=100;
    for(var k=0;k<7;k++){
      var bv=(r.baseline&&r.baseline.pct)? r.baseline.pct[k] : null;
      html+=barRow(labels[k], r.self.pct[k], bv, mx, false);
    }
  }
  return html;
}

// 生成済みHTMLの先頭タグに anim クラスを差し込む
// slide=true なら左スライドイン(animL)、falseならフェードアップ(anim)
function injectAnim(htmlStr, n, slide){
  var cls=(slide?"animL":"anim")+" d"+n;
  if(/^\s*<div class="/.test(htmlStr)){
    return htmlStr.replace(/^(\s*<div class=")/, '$1'+cls+' ');
  }
  return htmlStr.replace(/^(\s*<div)/, '$1 class="'+cls+'"');
}

function resultCard(rr){
  var tri='';
  var marksLine='';
  if(rr.trifecta){
    var parts=rr.trifecta.split("-");
    tri='';
    for(var i=0;i<parts.length;i++){
      if(i>0) tri+='<span class="tri-sep">-</span>';
      var bk=(parts[i]||"").trim();
      tri+='<span class="tri-bk bcol'+esc(bk)+'">'+esc(bk)+'</span>';
    }
    // 気配値印の行 (◎-◯-✕)
    var marks=rr.trifecta_marks||[];
    if(marks.length){
      var ms=[];
      for(var i=0;i<marks.length;i++){
        var mk=marks[i]||"–";
        var c=(mk==="◎")?"mkgold":"mktxt";
        ms.push('<span class="'+c+'">'+mk+'</span>');
      }
      marksLine='<div class="res-marks">'+ms.join('<span class="mksep">-</span>')+'</div>';
    }
  }
  var refund = rr.refund_3t ? ('3連単 '+Number(rr.refund_3t).toLocaleString()+'円') : '';
  var order='';
  if(rr.result && rr.result.length){
    order='<div class="res-order">';
    for(var i=0;i<rr.result.length;i++){
      var r=rr.result[i];
      if(r.rank>3) continue;
      order+='<span class="res-od">'+r.rank+'着 <b>'+esc(r.bike)+'</b>'
        + (r.finish&&r.finish!="--"?' '+esc(r.finish):'')+'</span>';
    }
    order+='</div>';
  }
  return '<div class="resultcard fade"><div class="rc-h"><span class="ttl">3連単</span>'
    + '<span class="res-tri">'+tri+'</span></div><div class="rc-b">'
    + marksLine
    + (refund?'<div class="res-refund">'+refund+'</div>':'')
    + order + '</div></div>';
}

function colorLine(lineStr, top3){
  // ライン文字列の数字のうち、3着以内の車番を緑にする
  var t3 = {};
  for(var i=0;i<top3.length;i++){ t3[String(top3[i])] = true; }
  var out = '';
  for(var i=0;i<lineStr.length;i++){
    var ch = lineStr[i];
    if(ch>='0' && ch<='9' && t3[ch]){
      out += '<span class="line-top3">'+ch+'</span>';
    }else{
      out += esc(ch);
    }
  }
  return out;
}

function headerCard(h){
  var meta='';
  if(h.bank) meta+='<span class="tag">'+esc(h.bank)+'</span>';
  if(h.wind_arrow) meta+='<span class="tag wind">風 '+esc(h.wind_arrow)+'</span>';
  if(h.wind_judge) meta+='<span class="tag judge">'+esc(h.wind_judge)+'</span>';
  // レース内に穴/勝負弱の選手がいればヘッダーにも小ラベル (判定タグとは別段)
  var hasAna=false, hasWeak=false, hasLayoff=false;
  if(h.players){
    for(var pj=0;pj<h.players.length;pj++){
      var lk=h.players[pj].label_kind;
      if(lk==="ana") hasAna=true;
      if(lk==="weak") hasWeak=true;
      if(h.players[pj].layoff_kind) hasLayoff=true;
    }
  }
  var sideLabels='';
  if(hasAna) sideLabels+='<span class="plabel ana hdlabel">穴サイド</span>';
  if(hasWeak) sideLabels+='<span class="plabel weak hdlabel">勝負弱</span>';
  if(hasLayoff) sideLabels+='<span class="plabel layoff hdlabel">離脱明</span>';
  var weather = h.weather_raw? '<span class="tag">'+esc(h.weather_raw)+'</span>':'';

  var lines='';
  if(h.line_display){
    var top3 = h.top3_bikes || [];
    lines = '<div class="line-block">'
      + '<div class="line-main"><div><span class="lab">ライン</span><span class="val">'+colorLine(h.line_display, top3)+'</span></div>'
      + '<div style="margin-top:4px"><span class="lab">score順位</span><span class="val rank">'+esc(h.rank_display)+'</span></div></div>'
      + '<button class="line-ora-btn" onclick="gotoCathedral()">予想<span class="loa-sub">を見る</span></button>'
      + '</div>';
  }

  // 全車ラベル一覧 (穴/勝負弱がある選手のみ強調)
  return '<div class="card rhead fade"><div class="card-b">'
    + '<div class="rhead-top"><span class="venue">'+esc(h.venue)+'</span>'
    + '<span class="rno">'+esc(h.race_no)+'R</span>'
    + (h.race_url? '<a class="wt-btn" href="'+esc(h.race_url)+'" target="_blank" rel="noopener" aria-label="WINTICKETで見る">w</a>':'')
    + '<span class="post">発走 '+esc(h.post_time)+'</span></div>'
    + '<div class="meta-row">'+weather+meta+'</div>'
    + (sideLabels?'<div class="meta-row side-row">'+sideLabels+'</div>':'')
    + lines
    + '</div></div>';
}

// 小ラベル化: 勝負弱→弱 / 穴サイド→穴 / 離脱明→離。それ以外は先頭1文字。
function shortLabel(txt){
  if(!txt) return '';
  if(txt.indexOf('勝負弱')>=0) return '弱';
  if(txt.indexOf('穴')>=0) return '穴';
  if(txt.indexOf('離脱')>=0) return '離';
  return txt.charAt(0);
}
// 決まり手1種のセル(率を大きく+下に分数)。rank=列内率順位で色分け(1琥珀/2緑/3赤)。
function kimariCell(it, rank){
  if(!it || !it.den){ return '<td class="km-c"><span class="km-na">-</span></td>'; }
  var pct = Math.round(it.rate*100);
  var rcls='km-r';
  if(rank===1) rcls='km-r kr1';
  else if(rank===2) rcls='km-r kr2';
  else if(rank===3) rcls='km-r kr3';
  return '<td class="km-c"><span class="'+rcls+'">'+pct+'%</span>'
       + '<span class="km-f">'+it.hit+'/'+it.den+'</span></td>';
}
// 数値1種のセル(上段メイン+下段サブ)。rank=列内順位で上段を色分け(1琥珀/2緑/3赤)。
// extraCls: td に付与する追加クラス(任意)。S/B列の幅調整などに使用。
function statCell(topTxt, subTxt, rank, extraCls){
  var tdc='km-c'+(extraCls?(' '+extraCls):'');
  if(topTxt===null||topTxt===undefined||topTxt===''){ return '<td class="'+tdc+'"><span class="km-na">-</span></td>'; }
  var rcls='km-r';
  if(rank===1) rcls='km-r kr1';
  else if(rank===2) rcls='km-r kr2';
  else if(rank===3) rcls='km-r kr3';
  return '<td class="'+tdc+'"><span class="'+rcls+'">'+topTxt+'</span>'
       + '<span class="km-f">'+(subTxt!==null&&subTxt!==undefined?subTxt:'')+'</span></td>';
}
// 出走表(roster)のHTMLを返す。列順: 車/選手/役割/脚/印/ラベル/逃捲差マ
// 車・選手を左固定、それより右を横スクロール。決まり手は列ごと最上位%を赤字。
function rosterHtml(h){
  if(!(h.players && h.players.length)) return '<div class="nodata">出走表データなし</div>';
  var order=['逃','捲','差','マ'];
  // 各選手の決まり手をマップ化 + 列ごとに率順位(選手index→1/2/3..)を算出
  var pls=h.players;
  var maps=[];           // pls[i] に対応する {逃:it,...}
  for(var i=0;i<pls.length;i++){
    var byk={};
    // 決まり手の参照元を KIMARI_MODE で切替: '今回'=役割別kimari / '通算'=kimari_total
    var ksrc = (KIMARI_MODE==='通算') ? pls[i].kimari_total : pls[i].kimari;
    if(ksrc && ksrc.items){
      for(var ki=0;ki<ksrc.items.length;ki++){
        var it=ksrc.items[ki]; byk[it.k]=it;
      }
    }else if(KIMARI_MODE!=='通算' && pls[i].kimari_diag){
      // 「今回」表示で役割データが無い選手を診断ログに記録 (原因特定用)
      try{
        logError('決まり手データ欠落',
          '日付='+DATE+' '+(h.venue||'')+(h.race_no||'')+'R\n'
          +'車'+pls[i].bike+' '+(pls[i].name||'')
          +'\nキー='+(pls[i].kimari_key||'(空)')
          +' 役割='+(pls[i].role||'(不明)')
          +'\n理由: '+pls[i].kimari_diag);
      }catch(e){}
    }
    maps.push(byk);
  }
  // 列ごと: den>0 の選手を率降順に並べ、選手indexに順位(1,2,3..)を付与
  var kRank={'逃':{},'捲':{},'差':{},'マ':{}};
  var korder=['逃','捲','差','マ'];
  for(var ci=0;ci<korder.length;ci++){
    var kk=korder[ci];
    var arr=[];
    for(var pj=0;pj<pls.length;pj++){
      var itc=maps[pj][kk];
      if(itc && itc.den){ arr.push({idx:pj, rate:itc.rate}); }
    }
    arr.sort(function(a,b){ return b.rate-a.rate; });
    for(var ai=0;ai<arr.length;ai++){ kRank[kk][arr[ai].idx]=ai+1; }
  }
  // 啓示点: 気配値/S/適合/SR/逃/捲/差/マ の各順位を 1位3点,2位2点,3位1点 で合算
  function rankPt(r){ if(r===1)return 3; if(r===2)return 2; if(r===3)return 1; return 0; }
  var revPt=[];   // 選手index→啓示合計点
  for(var ri=0;ri<pls.length;ri++){
    var p=pls[ri];
    var pt=0;
    pt+=rankPt(p.keihai_rank);
    pt+=rankPt(p.rs_rank);      // S
    pt+=rankPt(p.match_rank);   // 適合
    pt+=rankPt(p.rs_rank);      // SR(順位はscore順位と同じ)
    pt+=rankPt(kRank['逃'][ri]);
    pt+=rankPt(kRank['捲'][ri]);
    pt+=rankPt(kRank['差'][ri]);
    pt+=rankPt(kRank['マ'][ri]);
    revPt.push(pt);
  }
  // 啓示点の高い順に 1/2/3位を付与(同点は先着順)
  var revRank={};
  var revArr=[];
  for(var rj=0;rj<pls.length;rj++){ revArr.push({idx:rj, pt:revPt[rj]}); }
  revArr.sort(function(a,b){ return b.pt-a.pt; });
  for(var rk=0;rk<revArr.length;rk++){ revRank[revArr[rk].idx]=rk+1; }
  // 決まり手 今回/通算 トグル (出走表カード右上)
  var tgGuard = (KIMARI_MODE==='通算');
  var html='<div class="kimari-mode-bar">'
    + '<span class="kmm-lab">決まり手</span>'
    + '<span class="kmm-seg">'
    +   '<button class="kmm-btn'+(tgGuard?'':' on')+'" onclick="__setKimariMode(\'今回\')">今回</button>'
    +   '<button class="kmm-btn'+(tgGuard?' on':'')+'" onclick="__setKimariMode(\'通算\')">通算</button>'
    + '</span></div>';
  html+='<div class="roster-wrap"><table class="rost-tbl">';
  html+='<thead><tr>'
      + '<th class="c-id fix fix1">車・選手</th>'
      + '<th class="c-lb">ラベル</th>'
      + '<th class="c-rl">役割</th>'
      + '<th class="c-st">脚</th>'
      + '<th class="c-mk">印</th>'
      + '<th class="c-km">啓示</th>'
      + '<th class="c-km">S</th>'
      + '<th class="c-km">適合</th>'
      + '<th class="c-km">SR</th>'
      + '<th class="c-km c-sb">S</th>'
      + '<th class="c-km c-sb">B</th>'
      + '<th class="c-km">逃</th>'
      + '<th class="c-km">捲</th>'
      + '<th class="c-km">差</th>'
      + '<th class="c-km">マ</th>'
      + '</tr></thead><tbody>';
  for(var pi=0;pi<pls.length;pi++){
    var pl=pls[pi];
    var lab='';
    if(pl.label_kind){
      lab+='<span class="plabel '+pl.label_kind+'">'+esc(shortLabel(pl.label_text))+'</span>';
    }
    if(pl.layoff_kind){
      lab+='<span class="plabel layoff">'+esc(shortLabel(pl.layoff_text))+'</span>';
    }
    var mk = pl.keihai_mark ? '<span class="kmark r'+(pl.keihai_rank||9)+'">'+pl.keihai_mark+'</span>':'';
    var byk=maps[pi];
    var kc='';
    for(var oi=0;oi<order.length;oi++){
      var k=order[oi];
      var it=byk[k];
      kc+=kimariCell(it, kRank[k][pi]);
    }
    html+='<tr>'
      + '<td class="c-id fix fix1">'
        + '<span class="rb bcol'+esc(pl.bike)+'">'+esc(pl.bike)+'</span>'
        + '<span class="rn">'+esc(pl.name)+'</span>'
      + '</td>'
      + '<td class="c-lb">'+lab+'</td>'
      + '<td class="c-rl">'+(pl.role?'<span class="rrole">'+esc(pl.role)+'</span>':'')+'</td>'
      + '<td class="c-st">'+(pl.style?'<span class="rs">'+esc(pl.style)+'</span>':'')+'</td>'
      + '<td class="c-mk">'+mk+'</td>'
      + statCell(revPt[pi], '', revRank[pi])
      + statCell(pl.rs_rank!=null?pl.rs_rank+'位':null, pl.raw_score!=null?'('+(Math.round(pl.raw_score*10)/10)+')':'', pl.rs_rank)
      + statCell(pl.match_rank!=null?pl.match_rank+'位':null, pl.match_score!=null?pl.match_score+'%':'該当なし', pl.match_rank)
      + statCell(pl.rr!=null?pl.rr.toFixed(2):null, pl.rs_rank!=null?'('+pl.rs_rank+'位)':'', pl.rs_rank)
      + statCell((pl.s_cnt!=null&&pl.s_cnt!==0)?String(pl.s_cnt):null, '', null, 'c-sb')
      + statCell((pl.b_cnt!=null&&pl.b_cnt!==0)?String(pl.b_cnt):null, '', null, 'c-sb')
      + kc
      + '</tr>';
  }
  html+='</tbody></table></div>';
  html+=rosterChartHtml(pls, h.line_display||'');
  // v330: 「競走得点 / S 推移」は廃止した。
  //   html+=scoreTrendHtml(h, pls);
  return html;
}

// 横向きチャート: 縦軸=色付き車番マーク, S=横向き面グラフ(半透明青), 適合=折れ線
// 並び順トグル: S順(score高い順) / L順(ライン並び順)
var __rchartStore = (window.__rchartStore = window.__rchartStore || {});
var __rchartSeq = 0;
var BIKE_FILL = {1:'#f5f5f5',2:'#1c1c1c',3:'#e23b3b',4:'#2f7be2',5:'#f1c40f',6:'#2bb24d',7:'#e8772e',8:'#e261b0',9:'#8a5cd1'};
var BIKE_TXT  = {1:'#1a1a1a',2:'#ffffff',3:'#ffffff',4:'#ffffff',5:'#1a1a1a',6:'#ffffff',7:'#ffffff',8:'#ffffff',9:'#ffffff'};
var BIKE_BD   = {1:'#cfcfcf',2:'#000000',3:'#cc2222',4:'#1f5fc0',5:'#caa400',6:'#1f9440',7:'#c75f1c',8:'#c54897',9:'#6f45b3'};

function rosterChartHtml(pls, lineDisplay){
  var pts=[];
  for(var i=0;i<pls.length;i++){
    var p=pls[i];
    if(p.rs_rank!=null && p.raw_score!=null){
      pts.push({rank:p.rs_rank, bike:String(p.bike), name:p.name,
                score:p.raw_score,
                match:(p.match_score!=null? p.match_score : null)});
    }
  }
  if(pts.length<2) return '';
  // ライン並び順(line_display)から 車番→全体順序index と 車番→ライングループID を作る
  var lineOrder={}, lineGroup={};
  var groups=(lineDisplay||'').split('-');
  var gidx=0, seq=0;
  for(var gi=0; gi<groups.length; gi++){
    var grp=groups[gi].replace(/[^0-9]/g,'');
    if(!grp) continue;
    for(var ci=0; ci<grp.length; ci++){
      var ch=grp.charAt(ci);
      if(lineOrder[ch]===undefined){ lineOrder[ch]=seq; lineGroup[ch]=gidx; seq++; }
    }
    gidx++;
  }

  var cid='rchart_'+(++__rchartSeq);
  __rchartStore[cid]={pts:pts, lineOrder:lineOrder, lineGroup:lineGroup};
  // 初期はL順で描画(最も使用頻度が高いため)
  var svg=__buildRChartSvg(pts, lineOrder, 'L', lineGroup);
  var btns=''
   + '<div class="rchart-btns">'
   + '<button type="button" class="rchart-btn active" data-mode="L" '
   +   'onclick="__rchartSort(\''+cid+'\',\'L\',this)">L順</button>'
   + '<button type="button" class="rchart-btn" data-mode="S" '
   +   'onclick="__rchartSort(\''+cid+'\',\'S\',this)">S順</button>'
   + '</div>';
  var legend=''
   + '<div class="rchart-legend">'
   + '<span class="rcl-item"><span class="rcl-swatch sw-score"></span>S(score)</span>'
   + '<span class="rcl-item"><span class="rcl-swatch sw-match"></span>適合</span>'
   + '</div>';
  return '<div class="rchart-wrap" id="'+cid+'">'
       + '<div class="rchart-head">'
       +   '<div class="rchart-title">S / 適合</div>'
       +   btns
       + '</div>'
       + legend
       + '<div class="rchart-svg">'+svg+'</div>'
       + '</div>';
}

// 並び替えハンドラ(グローバル)
function __setKimariMode(mode){
  if(mode!=='今回' && mode!=='通算') return;
  if(KIMARI_MODE===mode) return;
  // 切り替え前の横スクロール位置を保存 (比較しやすいよう位置を維持)
  var sx=0, sy=0;
  var oldWrap=document.querySelector('.roster-wrap');
  if(oldWrap){ sx=oldWrap.scrollLeft; }
  var pane=document.getElementById('tabpane');
  if(pane){ sy=pane.scrollTop; }
  KIMARI_MODE=mode;
  // 出走表タブを再描画 (再通信なし。kimari/kimari_total は既に手元にある)
  if(typeof renderTab==='function'){ renderTab(); }
  // 再描画後にスクロール位置を復元
  var newWrap=document.querySelector('.roster-wrap');
  if(newWrap){ newWrap.scrollLeft=sx; }
  if(pane){ pane.scrollTop=sy; }
  // フェード等でレイアウト確定が遅れる場合に備え次フレームでも復元
  if(typeof requestAnimationFrame==='function'){
    requestAnimationFrame(function(){
      var w2=document.querySelector('.roster-wrap');
      if(w2){ w2.scrollLeft=sx; }
      var p2=document.getElementById('tabpane');
      if(p2){ p2.scrollTop=sy; }
    });
  }
}

function __rchartSort(cid, mode, btn){
  var st=__rchartStore[cid]; if(!st) return;
  var wrap=document.getElementById(cid); if(!wrap) return;
  var holder=wrap.querySelector('.rchart-svg');
  if(holder){ holder.innerHTML=__buildRChartSvg(st.pts, st.lineOrder, mode, st.lineGroup); }
  var bs=wrap.querySelectorAll('.rchart-btn');
  for(var i=0;i<bs.length;i++){ bs[i].className='rchart-btn'+(bs[i].getAttribute('data-mode')===mode?' active':''); }
}

// 横向きSVGを組み立てて返す
function __buildRChartSvg(ptsIn, lineOrder, mode, lineGroup){
  lineGroup = lineGroup || {};
  // 並び替え(配列コピー)
  var pts=ptsIn.slice();
  if(mode==='L'){
    pts.sort(function(a,b){
      var oa=(lineOrder[a.bike]!==undefined)?lineOrder[a.bike]:999;
      var ob=(lineOrder[b.bike]!==undefined)?lineOrder[b.bike]:999;
      if(oa!==ob) return oa-ob;
      return a.rank-b.rank;
    });
  } else {
    pts.sort(function(a,b){ return b.score-a.score; });  // S順: score高い順
  }
  var n=pts.length;

  // 縦向きレイアウト: X=下軸の車番, Y=値
  var W=680, H=300;
  var padL=46, padR=46, padT=26, padB=46;
  var iW=W-padL-padR, iH=H-padT-padB;

  // S範囲
  var sMin=Infinity,sMax=-Infinity;
  for(var a=0;a<n;a++){ if(pts[a].score<sMin)sMin=pts[a].score; if(pts[a].score>sMax)sMax=pts[a].score; }
  var sPad=(sMax-sMin)*0.12; if(sPad<1)sPad=1;
  var sLo=sMin-sPad, sHi=sMax+sPad;
  var mLo=0, mHi=100;
  var hasMatch=false;
  for(var b=0;b<n;b++){ if(pts[b].match!=null){ hasMatch=true; break; } }

  function xAt(i){ return padL + (n<=1?iW/2:(iW*i/(n-1))); }
  function yScore(v){ return padT + iH*(1-(v-sLo)/(sHi-sLo)); }
  function yMatch(v){ return padT + iH*(1-(v-mLo)/(mHi-mLo)); }

  // 横グリッド(S目盛3本) + 左ラベル
  var grid='';
  for(var g=0;g<=2;g++){
    var gv=sLo+(sHi-sLo)*g/2;
    var gy=yScore(gv).toFixed(1);
    grid += '<line x1="'+padL+'" y1="'+gy+'" x2="'+(W-padR)+'" y2="'+gy+'" '
          + 'stroke="rgba(160,160,150,.14)" stroke-width="1"/>';
    grid += '<text x="'+(padL-7)+'" y="'+(parseFloat(gy)+3.5).toFixed(1)+'" '
          + 'text-anchor="end" font-size="10" style="fill:#6fa8d8">'+Math.round(gv)+'</text>';
  }
  // 右の適合目盛(0/50/100)
  var mAxis='';
  if(hasMatch){
    var mv=[0,50,100];
    for(var f=0;f<mv.length;f++){
      var myy=yMatch(mv[f]).toFixed(1);
      mAxis += '<text x="'+(W-padR+7)+'" y="'+(parseFloat(myy)+3.5).toFixed(1)+'" '
             + 'text-anchor="start" font-size="10" style="fill:#e08a4d">'+mv[f]+'%</text>';
    }
  }

  // L順のとき: ライン境界に縦点線を引く(隣り合う車番のライングループが変わる位置)
  var lineSep='';
  if(mode==='L' && n>1){
    for(var s=0;s<n-1;s++){
      var gCur=(lineGroup[pts[s].bike]!==undefined)?lineGroup[pts[s].bike]:-1;
      var gNext=(lineGroup[pts[s+1].bike]!==undefined)?lineGroup[pts[s+1].bike]:-2;
      if(gNext!==gCur){
        var sepx=((xAt(s)+xAt(s+1))/2).toFixed(1);
        lineSep += '<line x1="'+sepx+'" y1="'+(padT-4)+'" x2="'+sepx+'" y2="'+(H-padB+22)+'" '
                 + 'stroke="rgba(220,200,140,.55)" stroke-width="1.3" stroke-dasharray="4 4"/>';
      }
    }
  }

  // S 面グラフ(縦) + 線 + 点, 下軸の車番マーク
  var areaTop='', sLine='', sDots='', badges='';
  for(var c=0;c<n;c++){
    var cx=xAt(c), cy=yScore(pts[c].score);
    areaTop += (c===0?'M':'L')+cx.toFixed(1)+' '+cy.toFixed(1)+' ';
    sLine   += (c===0?'M':'L')+cx.toFixed(1)+' '+cy.toFixed(1)+' ';
    sDots   += '<circle cx="'+cx.toFixed(1)+'" cy="'+cy.toFixed(1)+'" r="3.5" '
             + 'fill="#5b9bd5" stroke="#0c1a2b" stroke-width="1.5"/>';
    // 下軸の色付き車番マーク
    var bn=parseInt(pts[c].bike,10);
    var fill=BIKE_FILL[bn]||'#888', tcol=BIKE_TXT[bn]||'#fff', bd=BIKE_BD[bn]||'#555';
    var bw=24, bh=22, byy=H-padB+8;
    badges += '<rect x="'+(cx-bw/2).toFixed(1)+'" y="'+byy+'" width="'+bw+'" height="'+bh+'" rx="3" '
            + 'fill="'+fill+'" stroke="'+bd+'" stroke-width="1.5"/>'
            + '<text x="'+cx.toFixed(1)+'" y="'+(byy+bh/2+4).toFixed(1)+'" text-anchor="middle" '
            + 'font-size="13" font-weight="800" fill="'+tcol+'">'+esc(pts[c].bike)+'</text>';
  }
  var baseY=(padT+iH).toFixed(1);
  var areaPath = areaTop + 'L'+xAt(n-1).toFixed(1)+' '+baseY+' L'+xAt(0).toFixed(1)+' '+baseY+' Z';

  // 適合 折れ線(縦)
  var mLine='', mDots='';
  if(hasMatch){
    var started=false;
    for(var d=0;d<n;d++){
      if(pts[d].match==null) continue;
      var mx=xAt(d), my=yMatch(pts[d].match);
      mLine += (started?'L':'M')+mx.toFixed(1)+' '+my.toFixed(1)+' ';
      started=true;
      mDots += '<circle cx="'+mx.toFixed(1)+'" cy="'+my.toFixed(1)+'" r="4.5" '
             + 'fill="#fff" stroke="#ff8a3d" stroke-width="2.5"/>';
    }
  }

  return ''
   + '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet" '
   + 'style="width:100%;height:auto;display:block">'
   + '<defs>'
   + '<linearGradient id="scoreFill" x1="0" y1="0" x2="0" y2="1">'
   + '<stop offset="0%" stop-color="#5b9bd5" stop-opacity="0.45"/>'
   + '<stop offset="100%" stop-color="#5b9bd5" stop-opacity="0.06"/>'
   + '</linearGradient>'
   + '</defs>'
   + grid
   + lineSep
   + '<path d="'+areaPath+'" fill="url(#scoreFill)" stroke="none"/>'
   + '<path d="'+sLine+'" fill="none" stroke="#5b9bd5" stroke-width="2.5" '
   + 'stroke-linejoin="round" stroke-linecap="round"/>'
   + sDots
   + (hasMatch? '<path d="'+mLine+'" fill="none" stroke="#ff8a3d" stroke-width="2.5" '
       + 'stroke-linejoin="round" stroke-linecap="round"/>':'')
   + mDots
   + badges
   + mAxis
   + '</svg>';
}

// ===== score推移グラフ (過去5ヶ月: raw=面 / 競走得点=波線, 車番トグル, 複数選択) =====
var __strendStore = (window.__strendStore = window.__strendStore || {});
var __strendSeq = 0;

function scoreTrendHtml(h, pls){
  var st = (h && h.score_trend) ? h.score_trend : null;
  if(!st || !st.series || st.series.length===0) return '';
  // bike -> series 引き表
  var byBike = {};
  for(var i=0;i<st.series.length;i++){ byBike[String(st.series[i].bike)] = st.series[i]; }
  // ライン並び順 + ライングループ (line_display から)
  var lineOrder=[], lineGroup={};
  var groups=((h.line_display)||'').split('-');
  var gidx=0;
  for(var gi=0; gi<groups.length; gi++){
    var grp=groups[gi].replace(/[^0-9]/g,'');
    if(!grp) continue;
    var inThis=[];
    for(var ci=0; ci<grp.length; ci++){
      var ch=grp.charAt(ci);
      if(byBike[ch] && lineGroup[ch]===undefined){ inThis.push(ch); lineGroup[ch]=gidx; }
    }
    if(inThis.length){ lineOrder.push(inThis); gidx++; }
  }
  // line_display に出てこない車番(単騎漏れ等)を末尾に
  var leftover=[];
  for(var k in byBike){ if(lineGroup[k]===undefined){ leftover.push(k); } }
  if(leftover.length){ leftover.sort(function(a,b){return a-b;}); lineOrder.push(leftover); }
  if(lineOrder.length===0) return '';

  // 初期選択: 結果が出ているレース=1〜3着車番 / 出ていない=選択なし
  var initSel = [];
  if(st.has_result && st.top3_bikes && st.top3_bikes.length){
    for(var ti=0; ti<st.top3_bikes.length; ti++){
      var tbk=String(st.top3_bikes[ti]);
      if(byBike[tbk]) initSel.push(tbk);
    }
  }

  var cid='strend_'+(++__strendSeq);
  __strendStore[cid]={ byBike:byBike, lineOrder:lineOrder, lineGroup:lineGroup,
                       sel:{}, months:(st.months||5) };
  for(var s2=0;s2<initSel.length;s2++){ __strendStore[cid].sel[initSel[s2]]=true; }

  // ボタン列
  var btns='<div class="strend-btns">';
  for(var g=0; g<lineOrder.length; g++){
    for(var b=0; b<lineOrder[g].length; b++){
      var bn=lineOrder[g][b];
      var on=__strendStore[cid].sel[bn]?' on':'';
      btns += '<div class="strend-bk bcol'+bn+on+'" data-bk="'+bn+'" '
            + 'onclick="__strendToggle(\''+cid+'\',\''+bn+'\',this)">'+bn+'</div>';
    }
    if(g<lineOrder.length-1){ btns += '<div class="strend-sep"></div>'; }
  }
  btns += '</div>';

  var legend=''
   + '<div class="strend-legend">'
   + '<span class="stl-clear" onclick="__strendClear(\''+cid+'\')">消</span>'
   + '<span class="stl-item"><span class="stl-area"></span>S(raw)</span>'
   + '<span class="stl-item"><span class="stl-line"></span>競走得点</span>'
   + '</div>';

  var svg=__buildStrendSvg(cid);
  return '<div class="strend-wrap" id="'+cid+'">'
       + '<div class="strend-head"><div class="strend-title">競走得点 / S 推移</div>'+legend+'</div>'
       + btns
       + '<div class="strend-svg">'+svg+'</div>'
       + '</div>';
}

function __strendToggle(cid, bn, el){
  var st=__strendStore[cid]; if(!st) return;
  if(st.sel[bn]){ delete st.sel[bn]; if(el) el.className=el.className.replace(/ on\b/,''); }
  else { st.sel[bn]=true; if(el && el.className.indexOf(' on')<0) el.className+=' on'; }
  var wrap=document.getElementById(cid); if(!wrap) return;
  var holder=wrap.querySelector('.strend-svg');
  if(holder){ holder.innerHTML=__buildStrendSvg(cid); }
}

// 全選択解除
function __strendClear(cid){
  var st=__strendStore[cid]; if(!st) return;
  st.sel={};
  var wrap=document.getElementById(cid); if(!wrap) return;
  var bks=wrap.querySelectorAll('.strend-bk');
  for(var i=0;i<bks.length;i++){ bks[i].className=bks[i].className.replace(/ on\b/,''); }
  var holder=wrap.querySelector('.strend-svg');
  if(holder){ holder.innerHTML=__buildStrendSvg(cid); }
}

// Catmull-Rom -> ベジェ で滑らかな曲線パス
function __strendSpline(pts){
  if(pts.length<2) return '';
  var d='M '+pts[0].x.toFixed(1)+' '+pts[0].y.toFixed(1);
  for(var i=0;i<pts.length-1;i++){
    var p0=pts[i-1]||pts[i], p1=pts[i], p2=pts[i+1], p3=pts[i+2]||pts[i+1];
    var c1x=p1.x+(p2.x-p0.x)/6, c1y=p1.y+(p2.y-p0.y)/6;
    var c2x=p2.x-(p3.x-p1.x)/6, c2y=p2.y-(p3.y-p1.y)/6;
    d+=' C '+c1x.toFixed(1)+' '+c1y.toFixed(1)+', '+c2x.toFixed(1)+' '+c2y.toFixed(1)
      +', '+p2.x.toFixed(1)+' '+p2.y.toFixed(1);
  }
  return d;
}

function __buildStrendSvg(cid){
  var st=__strendStore[cid]; if(!st) return '';
  var sel=[]; for(var k in st.sel){ if(st.sel[k]) sel.push(k); }
  var W=520, H=300;
  var padL=34, padR=38, padT=16, padB=34;
  var iW=W-padL-padR, iH=H-padT-padB;
  var months=st.months||5;

  // 値域 (選択車のみ)
  var rmin=Infinity,rmax=-Infinity,tmin=Infinity,tmax=-Infinity;
  for(var i=0;i<sel.length;i++){
    var ser=st.byBike[sel[i]]; if(!ser) continue;
    for(var a=0;a<ser.raw.length;a++){ var rv=ser.raw[a].v; if(rv<rmin)rmin=rv; if(rv>rmax)rmax=rv; }
    for(var b=0;b<ser.ten.length;b++){ var tv=ser.ten[b].v; if(tv<tmin)tmin=tv; if(tv>tmax)tmax=tv; }
  }
  if(!isFinite(rmin)){ rmin=-20; rmax=20; }
  if(!isFinite(tmin)){ tmin=40; tmax=70; }
  var rpad=(rmax-rmin)*0.12; if(rpad<2)rpad=2; rmin-=rpad; rmax+=rpad;
  var tpad=(tmax-tmin)*0.12; if(tpad<2)tpad=2; tmin-=tpad; tmax+=tpad;

  function X(t){ return padL + t*iW; }                       // 0(5M)->1(現)  左->右
  function Yraw(v){ return padT + iH*(1-(v-rmin)/(rmax-rmin)); }
  function Yten(v){ return padT + iH*(1-(v-tmin)/(tmax-tmin)); }

  var h='';
  // 横グリッド + 左右ラベル
  for(var gi2=0; gi2<=4; gi2++){
    var rv2=rmin+(rmax-rmin)*gi2/4;
    var y=Yraw(rv2);
    h+='<line x1="'+padL+'" y1="'+y.toFixed(1)+'" x2="'+(W-padR)+'" y2="'+y.toFixed(1)
      +'" stroke="rgba(160,160,150,.10)" stroke-width="1"/>';
    h+='<text x="'+(padL-5)+'" y="'+(y+3).toFixed(1)+'" text-anchor="end" font-size="10" '
      +'style="fill:#8f8f8f">'+Math.round(rv2)+'</text>';
    var tv2=tmin+(tmax-tmin)*gi2/4;
    h+='<text x="'+(W-padR+5)+'" y="'+(y+3).toFixed(1)+'" text-anchor="start" font-size="10" '
      +'style="fill:#8f8f8f">'+Math.round(tv2)+'</text>';
  }
  // 縦の月グリッド (5M..1M) + 現
  var mt=[0,0.2,0.4,0.6,0.8];
  var mlab=[months+'M',(months-1)+'M',(months-2)+'M',(months-3)+'M',(months-4)+'M'];
  for(var mi=0; mi<mt.length; mi++){
    var mx=X(mt[mi]);
    h+='<line x1="'+mx.toFixed(1)+'" y1="'+padT+'" x2="'+mx.toFixed(1)+'" y2="'+(padT+iH)
      +'" stroke="rgba(200,200,180,.12)" stroke-width="1" stroke-dasharray="3 4"/>';
    h+='<text x="'+mx.toFixed(1)+'" y="'+(H-12)+'" text-anchor="middle" font-size="10" '
      +'style="fill:#8f8f8f">'+mlab[mi]+'</text>';
  }
  // 現在ライン
  var cx0=X(1);
  h+='<line x1="'+cx0.toFixed(1)+'" y1="'+padT+'" x2="'+cx0.toFixed(1)+'" y2="'+(padT+iH)
    +'" stroke="rgba(202,49,37,.5)" stroke-width="1" stroke-dasharray="3 4"/>';
  h+='<text x="'+cx0.toFixed(1)+'" y="'+(H-12)+'" text-anchor="middle" font-size="10" '
    +'style="fill:#ca3125">現</text>';
  // 軸名
  h+='<text x="'+(padL-5)+'" y="'+(padT-3)+'" text-anchor="end" font-size="9" style="fill:#8f8f8f">raw</text>';
  h+='<text x="'+(W-padR+5)+'" y="'+(padT-3)+'" text-anchor="start" font-size="9" style="fill:#8f8f8f">点</text>';

  // 面(raw) 後ろ
  for(var s1=0;s1<sel.length;s1++){
    var ser1=st.byBike[sel[s1]]; if(!ser1) continue;
    if(ser1.raw.length<2) continue;
    var bn1=parseInt(sel[s1],10);
    var col=(bn1===2)?'#9a9a9a':(BIKE_FILL[bn1]||'#888'); // 黒(2)は背景に溶けるため明るめ
    var aop=(bn1===2)?'0.22':'0.16';
    var pts1=[]; for(var p1=0;p1<ser1.raw.length;p1++){ pts1.push({x:X(ser1.raw[p1].t), y:Yraw(ser1.raw[p1].v)}); }
    var top=__strendSpline(pts1);
    var baseY=(padT+iH).toFixed(1);
    var area=top+' L '+pts1[pts1.length-1].x.toFixed(1)+' '+baseY
            +' L '+pts1[0].x.toFixed(1)+' '+baseY+' Z';
    h+='<path d="'+area+'" fill="'+col+'" opacity="'+aop+'"/>';
    h+='<path d="'+top+'" fill="none" stroke="'+col+'" stroke-width="1" opacity="0.40"/>';
  }
  // 波線(競走得点) 前
  for(var s3=0;s3<sel.length;s3++){
    var ser3=st.byBike[sel[s3]]; if(!ser3) continue;
    if(ser3.ten.length<2) continue;
    var bn3=parseInt(sel[s3],10);
    var lc=(bn3===2)?'#9a9a9a':(BIKE_FILL[bn3]||'#888'); // 黒(2)は線が見えないため明るめ
    var pts3=[]; for(var p3=0;p3<ser3.ten.length;p3++){ pts3.push({x:X(ser3.ten[p3].t), y:Yten(ser3.ten[p3].v)}); }
    h+='<path d="'+__strendSpline(pts3)+'" fill="none" stroke="'+lc+'" stroke-width="2.4" '
      +'stroke-linecap="round" stroke-linejoin="round" opacity="0.95"/>';
  }
  if(sel.length===0){
    h+='<text x="'+(W/2)+'" y="'+(H/2)+'" text-anchor="middle" font-size="12" style="fill:#8f8f8f">車番を選択してください</text>';
  }

  return '<svg viewBox="0 0 '+W+' '+H+'" preserveAspectRatio="xMidYMid meet" '
       + 'style="width:100%;height:auto;display:block">'+h+'</svg>';
}

function patternCard(p, isTop, j){
  var styleTag = p.winner_style? '<span class="pc-style">'+esc(p.winner_style)+'</span>':'';
  var labTag = p.winner_label_kind ? '<span class="plabel '+p.winner_label_kind+'">'+esc(p.winner_label_text)+'</span>':'';
  if(p.winner_layoff_kind) labTag += '<span class="plabel layoff">'+esc(p.winner_layoff_text)+'</span>';
  var matchN = p.has_cell ? (p.match_rank+'位 '+p.match_score+'% (n='+p.cell_n+')') : '該当なし';
  var hitCls = p.hit ? ' hit' : '';
  var html = '<div class="pcard'+(isTop?' top':'')+hitCls+'">';
  var hitMark = p.hit ? '<span class="plabel" style="color:var(--green);border:1px solid var(--green);background:rgba(90,208,138,.14)">的中</span>' : '';
  html += '<div class="pc-h">'
    + '<div class="maru bcol'+esc(p.winner_bike)+'">'+esc(p.winner_bike)+'</div>'
    + '<div><div class="pc-name">'+esc(p.winner_name)+styleTag+labTag+hitMark+'</div></div>'
    + '<div class="kh"><div class="lab">気配値</div><div class="v">'+p.keihaichi.toFixed(2)+'</div></div>'
    + '</div>';
  html += '<div class="pc-stats">'
    + '<div class="stat"><div class="l">過去</div><div class="n">'+p.past_rank+'位</div><div class="sub">'+p.occurrence_rate+'%</div></div>'
    + '<div class="stat"><div class="l">適合</div><div class="n">'+(p.has_cell?p.match_rank+'位':'-')+'</div><div class="sub">'+(p.has_cell?p.match_score+'%':'該当なし')+'</div></div>'
    + '<div class="stat"><div class="l">rawr</div><div class="n">'+p.rr.toFixed(2)+'</div><div class="sub">'+p.winner_rsrank+'位</div></div>'
    + '</div>';
  // 的中した3連単 (展開照合用)
  var tri = (j && j.race_result && j.race_result.trifecta) ? j.race_result.trifecta.replace(/ /g,"") : "";
  if(p.has_cell && p.formations.length){
    html += '<div class="forms"><div class="fl">買い目フォーメーション</div>';
    for(var k=0;k<p.formations.length;k++){
      var f=p.formations[k];
      var parts=f.split("-");
      var formHit = tri && expandForm(f).indexOf(tri)>=0;
      var fcls = formHit ? 'form hitform' : 'form';
      var fmk = formHit ? '<span class="hitmark">的中</span>' : '';
      html += '<div class="'+fcls+'"><span class="b1">'+esc(parts[0])+'</span>-'+esc(parts[1])+'-'+esc(parts[2])+fmk+'</div>';
    }
    html += '</div>';
  }else if(!p.has_cell){
    html += '<div class="forms"><div class="fl" style="color:var(--txt-dim)">該当データなし</div></div>';
  }
  html += '</div>';
  return html;
}

function expandForm(form){
  var parts=form.split("-");
  if(parts.length!==3) return [];
  var out=[];
  var a=parts[0].split(""), b=parts[1].split(""), c=parts[2].split("");
  for(var i=0;i<a.length;i++)for(var j2=0;j2<b.length;j2++)for(var k=0;k<c.length;k++){
    if(a[i]!==b[j2]&&b[j2]!==c[k]&&a[i]!==c[k]) out.push(a[i]+"-"+b[j2]+"-"+c[k]);
  }
  return out;
}

function bar(rate, maxv){
  var w = maxv>0 ? Math.round(rate/maxv*100) : 0;
  if(w>100) w=100; if(w<0) w=0;
  return '<div class="bar-track"><div class="bar-main"><div class="bar-fill" style="width:0%" data-w="'+w+'"></div></div></div>';
}

// 共通バー行: メインバー(pct) + 基準ラインバー(basePct)。
// 幅は % の絶対値 (100%=満タン)。値は縦並び(上=今回%, 下=(基準%))。
function barRow(label, pct, basePct, maxv, bold, sel, pvBtn){
  var w = pct; if(w>100) w=100; if(w<0) w=0;
  var bw = (basePct!=null) ? basePct : 0;
  if(bw>100) bw=100; if(bw<0) bw=0;
  // 選択中ラベルは行に sel-row クラス (左縦ライン+薄ハイライト背景)。位置はズラさない
  var lb = bold? '<b>'+esc(label)+'</b>' : esc(label);
  var baseTrack = (basePct!=null)
    ? '<div class="bar-base"><div class="bar-base-fill" style="width:0%" data-w="'+bw+'"></div></div>'
    : '';
  var bv = (basePct!=null) ? '<span class="bvsub">('+basePct.toFixed(1)+'%)</span>' : '';
  var rowcls = sel ? 'bar-row sel-row' : 'bar-row';
  // pvBtn: {onclick, open} があれば %表示部分を細枠ボタンにする
  var pvCls = 'pvwrap';
  var pvAttr = '';
  if(pvBtn){
    pvCls += ' pvbtn' + (pvBtn.open ? ' pvopen' : '');
    pvAttr = ' onclick="'+pvBtn.onclick+'"';
  }
  return '<div class="'+rowcls+'"><span class="lb">'+lb+'</span>'
    + '<div class="bar-track"><div class="bar-main"><div class="bar-fill" style="width:0%" data-w="'+w+'"></div></div>'
    + baseTrack + '</div>'
    + '<span class="'+pvCls+'"'+pvAttr+'><span class="pv">'+pct.toFixed(1)+'%</span>'+bv+'</span></div>';
}

// コンテナ内の全バーを 0 から実値へニュッと伸ばす
function animateBars(container){
  if(!container) return;
  var fills=container.querySelectorAll(".bar-fill,.bar-base-fill");
  // 次フレームで幅をdata-wにセット→CSS transitionで伸びる
  requestAnimationFrame(function(){
    requestAnimationFrame(function(){
      for(var i=0;i<fills.length;i++){
        var w=fills[i].getAttribute("data-w");
        if(w!=null) fills[i].style.width=w+"%";
      }
    });
  });
}

function kimariCard(k){
  return '<div class="card fade"><div class="card-h"><span class="ttl">決まり手 ('+esc(k.cell_key)+' n='+k.cell_n+')</span></div><div class="card-b">'
    + kimariBody(k) + '</div></div>';
}
var _KIMARI_SEL=0;  // 選択中の1着決まり手グループ index
function kimariBody(k){
  // 選択indexが範囲外なら0に
  var links=k.kimari_link||[];
  if(_KIMARI_SEL>=links.length) _KIMARI_SEL=0;
  var html='';
  // === 1着決まり手比率 (見出し + 逃/捲/差ボタン) ===
  var maxv=0;
  for(var i=0;i<k.kimari_1st.length;i++){
    if(k.kimari_1st[i].rate>maxv) maxv=k.kimari_1st[i].rate;
    if(k.kimari_1st[i].base_rate!=null && k.kimari_1st[i].base_rate>maxv) maxv=k.kimari_1st[i].base_rate;
  }
  html+='<div class="kimari-head"><span class="kh-ttl">1着決まり手比率</span>';
  // ボタン (kimari_link の各グループ = 逃/捲/差)
  html+='<span class="kimari-btns">';
  for(var g=0;g<links.length;g++){
    var on=(g===_KIMARI_SEL)?" on":"";
    html+='<button class="kbtn'+on+'" onclick="selectKimari('+g+')">'+esc(links[g].kimari)+'</button>';
  }
  html+='</span></div>';
  // 選択中グループの決まり手名
  var selKimari = links.length ? links[_KIMARI_SEL].kimari : null;
  for(var i=0;i<k.kimari_1st.length;i++){
    var it=k.kimari_1st[i];
    var sel = (selKimari!=null && it.label===selKimari);
    html+=barRow(it.label, it.rate, (it.base_rate!=null?it.base_rate:null), maxv, true, sel);
  }
  // === 2着決まり手比率 (選択中の1着決まり手グループのみ) ===
  if(links.length){
    var grp=links[_KIMARI_SEL];
    html+='<div class="link-grp"><div style="font-size:12px;color:var(--txt-dim);margin-bottom:6px">2着決まり手比率</div>';
    html+='<div class="gh">1着='+esc(grp.kimari)+' (n='+grp.n+')</div>';
    var gmax=0;
    for(var i=0;i<grp.items.length;i++){
      if(grp.items[i].rate>gmax) gmax=grp.items[i].rate;
      if(grp.items[i].base_rate!=null && grp.items[i].base_rate>gmax) gmax=grp.items[i].base_rate;
    }
    for(var i=0;i<grp.items.length;i++){
      var it=grp.items[i];
      var hasThird = it.third && it.third.length;
      var open = (_KIMARI3_SEL===i);
      // 3着データがある場合、%表示部分を細枠ボタンにする
      if(hasThird){
        var pvBtn = {onclick:'toggleKimari3('+i+')', open:open};
        html+=barRow(it.label, it.rate, (it.base_rate!=null?it.base_rate:null), gmax, false, false, pvBtn);
        if(open){
          // 3着グラフを展開
          html+='<div class="third-grp"><div class="th-h">3着決まり手 (n='+(it.third_n||0)+')</div>';
          var tmax=0;
          for(var t=0;t<it.third.length;t++){
            if(it.third[t].rate>tmax) tmax=it.third[t].rate;
            if(it.third[t].base_rate!=null && it.third[t].base_rate>tmax) tmax=it.third[t].base_rate;
          }
          for(var t=0;t<it.third.length;t++){
            var tt=it.third[t];
            html+=barRow(tt.label, tt.rate, (tt.base_rate!=null?tt.base_rate:null), tmax, false);
          }
          html+='</div>';
        }
      } else {
        html+=barRow(it.label, it.rate, (it.base_rate!=null?it.base_rate:null), gmax, false);
      }
    }
    html+='</div>';
  }
  return html;
}
// 3着グラフのアコーディオン展開 (単一展開: 別を開くと他は閉じる)
var _KIMARI3_SEL=-1;
function toggleKimari3(i){
  _KIMARI3_SEL = (_KIMARI3_SEL===i) ? -1 : i;
  renderAsub();
}
function selectKimari(g){
  _KIMARI_SEL=g;
  _KIMARI3_SEL=-1; // 1着決まり手を変えたら3着展開を閉じる
  renderAsub(); // 決まり手サブタブを再描画
}

// 起動
(function(){
  // 暗(託宣)モードをデフォルト適用。起動初期画面(intro)を表示する
  document.body.className = "intro";
  document.documentElement.className = "";
  var di=document.getElementById("dateInput");
  di.value = ymdToIso(ymdToday());
  di.max = ymdToIso(ymdOffset(1));    // 明日まで
  di.min = ymdToIso(ymdOffset(-400)); // 過去約13ヶ月
  // intro: KEIRINの横幅と中央位置を託宣に合わせる(描画後に実測してscaleX+marginLeft適用)
  function fitIntroKeirin(){
    var jp=document.querySelector(".g-oracle-jp");
    var krBox=document.getElementById("introKeirin");
    var kr=krBox ? krBox.querySelector("span") : null;
    if(!jp || !kr) return;
    // 一旦リセットして素の幅と中央を測る
    kr.style.transform="scaleX(1)";
    kr.style.marginLeft="0px";
    void kr.offsetWidth;
    var rJp=jp.getBoundingClientRect();
    var rKr=kr.getBoundingClientRect();
    var wJp=rJp.width, wKr=rKr.width;
    if(wJp>0 && wKr>0){
      var s=wJp/wKr;
      kr.style.transform="scaleX("+s.toFixed(4)+")";
      // scaleX 適用後の中央を託宣の中央に揃え、
      // さらに託宣の letter-spacing 末尾ぶら下がり分(0.09em相当)だけ左へ補正
      void kr.offsetWidth;
      var rKr2=kr.getBoundingClientRect();
      var cJp=rJp.left + rJp.width/2;
      var cKr=rKr2.left + rKr2.width/2;
      // 託宣のフォントサイズから 0.09em を算出して左補正
      var jpFs=parseFloat(getComputedStyle(jp).fontSize)||0;
      var visualOffset=jpFs*0.40;
      var dx=(cJp - cKr) - visualOffset;
      kr.style.marginLeft=dx.toFixed(2)+"px";
      kr.setAttribute("data-scale", s.toFixed(4)+" dx="+dx.toFixed(1)+" off="+visualOffset.toFixed(1));
    }
  }
  setTimeout(fitIntroKeirin, 0);
  setTimeout(fitIntroKeirin, 60);
  setTimeout(fitIntroKeirin, 250);
  setTimeout(fitIntroKeirin, 800);
  if(document.fonts && document.fonts.ready && document.fonts.ready.then){
    document.fonts.ready.then(fitIntroKeirin);
  }
  window.addEventListener("load", fitIntroKeirin);
  window.addEventListener("resize", fitIntroKeirin);
  if(window.ResizeObserver){
    try{
      var ro=new ResizeObserver(fitIntroKeirin);
      var jpEl=document.querySelector(".g-oracle-jp");
      if(jpEl) ro.observe(jpEl);
    }catch(e){}
  }
})();
updateThemeIcon();
updateDateDisp();
// 起動時は会場読み込みをしない。託宣ボタン押下時のみ loadVenues() を実行する。
try{ errlogUpdateBadges(); }catch(e){}

</script>
</body>
</html>
"""

# ============================================================
# v329: /race/conditions と /race/oracle_core.js を配信する。
#   予想タブがこの2つを読む。無くてもアプリ本体は普通に動く。
# ============================================================
try:
    import race_page as _race_page
    _race_page.register(app, load_races, get_dicts, build_race_payload,
                        helpers={
                            "get_picks": get_picks,
                            "quick_label_kinds": _quick_label_kinds,
                            "quick_displayable": _quick_displayable,
                            "result_and_hit": _result_and_hit,
                            "is_post_passed": _is_post_passed,
                            "race_key": race_key,
                            "get_picks": get_picks,
                        })
    print("[race] 予想タブ用の配信を登録しました")
except Exception as _e:
    print("[race] 予想タブは無効です: " + str(_e)[:150])
    print("       race_page.py を app と同じ場所に置いてください")


APP_VERSION = "v329"
print("")
print("==================================================")
print("  託宣KEIRIN  " + APP_VERSION + "  起動")
print("  (この版数が想定と違うなら古いファイルを実行している)")
print("==================================================")

if __name__ == "__main__":
    # Pydroid3 でも PC でも同じ。host=0.0.0.0 にすると同一LANの他端末からも見える
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)

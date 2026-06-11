# -*- coding: utf-8 -*-
"""
fetch_keirin_data_v19.py
v19 からの変更点（休止の短縮 + 保険実行の休止無視）:
  - 休止時間を短縮: 1回目 30分 / 2回目以降 2時間 (旧: 1時間/6時間)
  - KEIRIN_IGNORE_PAUSE=1 で休止状態を無視して実行
    (朝5:30の保険実行用。メンテ明け回収を休止が妨げないように)

v18 からの変更点（連続失敗検知 + 自動休止 + LINE通知）:
  - 自動実行モード時、無限リトライを廃止（リトライ3回で1失敗カウント）
  - 連続10回失敗 → 取得中断、30分休止（2回連続なら2時間休止）
  - 休止状態は fetch_state.json で管理、次回起動時に自動チェック
  - 中断/復旧時に LINE Messaging API でプッシュ通知
    (環境変数 LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID)
  - Pydroid3 の対話実行は従来通り無限リトライ（変更なし）

v17 からの変更点（キャッチアップモード）:
  - KEIRIN_CATCHUP=1 で進捗ファイル(fetch_progress.txt)の続きから自動取得
    - KEIRIN_INIT_START: 初回開始日 (デフォルト 20260101)
    - KEIRIN_CHUNK_DAYS: 1回あたりの取得日数 (デフォルト 5)
    - 昨日分まで追いついたら以降は差分のみ自動取得
  - 完了時に進捗ファイルを更新

v16 からの変更点（GitHub Actions 対応）:
  - 環境変数 KEIRIN_AUTO=1 で対話入力なしの自動実行モード
    - KEIRIN_START / KEIRIN_END で日付指定（省略時は昨日1日分）
  - Pydroid3 では従来通り対話実行（変更なし）

v13 からの変更点（高速化）:
  - 1レース内の4つのHTTP (chariloto/yen-joy/gamboo/Open-Meteo) を並列化
    → 1レースあたり5-10秒 → 2-3秒に高速化
  - 各レース後の time.sleep(0.5) を削除
  - 補完フェーズの【レース詳細】表示を1行ログに簡素化
  - Open-Meteo 風向データを「会場+日付」キャッシュ化（60倍高速化）

v13 までの仕様:
  - 日付指定を対話入力化
  - 通信エラー(403/5xx/タイムアウト) → 無限リトライ
  - 出力先: keirin_data_scored_v2.jsonl (本番DB)
  - スコア計算ロジック統合
    - raw_score = 競走得点 - (履歴平均×3) + h2グレード加点
    - 9車立て(G1以上) 8着以下 → 7着補正
  - 補足取得時 5秒/10秒リトライ
  - 補足取得フェーズ (その日の全レース取得後 自動穴埋め)
  - SPA(Angular) JSON抽出

出力: keirin_data_scored_v2.jsonl (本番DB、スコア付き)
"""
import pandas as pd
import requests
import re
import os
import json
import calendar
from io import StringIO
import time
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# ============================================================
# スコア計算ロジック (v11新規統合)
# ============================================================
# 仕様:
#   raw_score = 競走得点 - (履歴平均×3) + h2グレード加点
#     グレード加点: GP/G1/G2/G3 → +5, F1 → +3, F2 → +1
#     9車立て(G1以上)のレース履歴 → 8,9着を7着に補正
#     h1=なし の場合は h2,h3 のみで平均
#   score = ((raw - min) / range) × 9 + 1  (レース内正規化)
#   raw_score は小数第2位まで（小数第3位以下を四捨五入）
# ============================================================
SCORE_GRADE_BONUS = {
    'ＧＰ': 5, 'Ｇ１': 5, 'Ｇ２': 5, 'Ｇ３': 5,
    'Ｆ１': 3, 'Ｆ２': 1,
    'GP': 5, 'G1': 5, 'G2': 5, 'G3': 5,
    'F1': 3, 'F2': 1
}
SCORE_HIGH_GRADES = set(['ＧＰ','Ｇ１','Ｇ２','Ｇ３','GP','G1','G2','G3'])
SCORE_GRADE_PATTERN = re.compile(r'(ＧＰ|Ｇ[１２３]|Ｆ[１２]|GP|G[123]|F[12])')
SCORE_RANK_TAIL_PATTERN = re.compile(r'(\d+(?:・\d+)*)\s*$')

def _score_parse_kyousou_ten(full_info):
    if not full_info or '/' not in full_info:
        return None
    parts = full_info.split('/')
    if len(parts) < 5:
        return None
    pt_str = parts[4].replace('点','').strip()
    try:
        return float(pt_str)
    except ValueError:
        return None

def _score_extract_grade(history_text):
    if not history_text or history_text == 'なし':
        return None
    m = SCORE_GRADE_PATTERN.search(history_text)
    if m:
        return m.group(1)
    return None

def _score_extract_ranks(history_text):
    if not history_text or history_text == 'なし':
        return []
    m = SCORE_RANK_TAIL_PATTERN.search(history_text)
    if not m:
        return []
    ranks = []
    for n in m.group(1).split('・'):
        try:
            ranks.append(int(n))
        except ValueError:
            pass
    return ranks

def _score_apply_high_grade_correction(ranks, grade):
    if grade in SCORE_HIGH_GRADES:
        corrected = []
        for r in ranks:
            if r >= 8:
                corrected.append(7)
            else:
                corrected.append(r)
        return corrected
    return ranks

def _score_calc_history_avg(h1, h2, h3):
    all_ranks = []
    if h1 and h1 != 'なし':
        ranks = _score_extract_ranks(h1)
        grade = _score_extract_grade(h1)
        all_ranks.extend(_score_apply_high_grade_correction(ranks, grade))
    if h2 and h2 != 'なし':
        ranks = _score_extract_ranks(h2)
        grade = _score_extract_grade(h2)
        all_ranks.extend(_score_apply_high_grade_correction(ranks, grade))
    if h3 and h3 != 'なし':
        ranks = _score_extract_ranks(h3)
        grade = _score_extract_grade(h3)
        all_ranks.extend(_score_apply_high_grade_correction(ranks, grade))
    if not all_ranks:
        return None
    return sum(all_ranks) / len(all_ranks)

def _score_get_h2_grade_bonus(h2):
    if not h2 or h2 == 'なし':
        return 0
    grade = _score_extract_grade(h2)
    if grade is None:
        return 0
    return SCORE_GRADE_BONUS.get(grade, 0)

def _score_calc_raw(full_info, h1, h2, h3):
    kyousou = _score_parse_kyousou_ten(full_info)
    if kyousou is None:
        return None
    avg = _score_calc_history_avg(h1, h2, h3)
    if avg is None:
        return None
    bonus = _score_get_h2_grade_bonus(h2)
    raw = kyousou - avg * 3 + bonus
    return round(raw, 2)

def normalize_scores_in_race(players_dict):
    """
    レース内で raw_score をもとに score (1-10) と raw_range を計算
    
    Args:
      players_dict: {車番(str): {full_info, h1, h2, h3, ...}}
    
    Returns:
      players_dict (同オブジェクト): 各選手に score, raw_score, raw_range を追加
    """
    raw_scores = {}
    for bn, p in players_dict.items():
        if not isinstance(p, dict):
            continue
        raw = _score_calc_raw(
            p.get('full_info',''),
            p.get('h1','なし'),
            p.get('h2','なし'),
            p.get('h3','なし')
        )
        raw_scores[bn] = raw
    
    valid_raws = [v for v in raw_scores.values() if v is not None]
    if not valid_raws:
        for bn, p in players_dict.items():
            if not isinstance(p, dict):
                continue
            p['raw_score'] = None
            p['raw_range'] = None
            p['score'] = None
        return players_dict
    
    min_raw = min(valid_raws)
    max_raw = max(valid_raws)
    raw_range = max_raw - min_raw
    
    for bn, p in players_dict.items():
        if not isinstance(p, dict):
            continue
        raw = raw_scores.get(bn)
        if raw is None:
            p['raw_score'] = None
            if raw_range > 0:
                p['raw_range'] = round(raw_range, 4)
            else:
                p['raw_range'] = None
            p['score'] = None
        else:
            p['raw_score'] = round(raw, 2)
            p['raw_range'] = round(raw_range, 4)
            if raw_range < 0.001:
                p['score'] = 10.0
            else:
                sc = ((raw - min_raw) / raw_range) * 9 + 1
                p['score'] = round(sc, 4)
    return players_dict

# =========================
# 設定
# =========================
# START_DATE / END_DATE は実行時に対話入力（main内で input()）
SAVE_DIR = "/storage/emulated/0/Download"
JSONL_PATH = os.path.join(SAVE_DIR, "keirin_data_scored_v2.jsonl")  # 本番DB

# 【DEBUG】 取得データを全部コンソール表示するか
# (今回限定の確認用。普段は False)
DEBUG_DUMP = False

KEIRIN_CODE_MAP = {
    "11": "函館","12": "青森","13": "いわき平",
    "21": "弥彦","22": "前橋","23": "取手","24": "宇都宮","25": "大宮","26": "西武園","27": "京王閣","28": "立川",
    "31": "松戸","34": "川崎","35": "平塚","36": "小田原","37": "伊東","38": "静岡",
    "42": "名古屋","43": "岐阜","44": "大垣","45": "豊橋","46": "富山","47": "松阪","48": "四日市",
    "51": "福井","53": "奈良","54": "向日町","55": "和歌山","56": "岸和田",
    "61": "玉野","62": "広島","63": "防府",
    "71": "高松","73": "小松島","74": "高知","75": "松山",
    "81": "小倉","83": "久留米","84": "武雄","85": "佐世保","86": "別府","87": "熊本"
}

KEIRIN_LOCATION_MAP = {
    "11": (41.7686, 140.7290),"12": (40.8244, 140.7400),"13": (37.0500, 140.8833),
    "21": (37.5667, 138.9333),"22": (36.3833, 139.0667),"23": (35.9167, 140.2000),
    "24": (36.5500, 139.8833),"25": (35.9000, 139.6167),"26": (35.7333, 139.4333),
    "27": (35.6500, 139.4500),"28": (35.7167, 139.4167),"31": (35.8000, 139.9000),
    "34": (35.5167, 139.7000),"35": (35.3333, 139.3500),"36": (35.2667, 139.1333),
    "37": (34.9667, 139.1000),"38": (34.9500, 138.4000),"42": (35.1667, 136.9000),
    "43": (35.4167, 136.7167),"44": (35.3667, 136.6167),"45": (34.7667, 137.3833),
    "46": (36.6833, 137.2000),"47": (34.5833, 136.5333),"48": (34.9667, 136.6167),
    "51": (36.0667, 136.2167),"53": (34.6833, 135.8333),"54": (34.9333, 135.6833),
    "55": (34.2167, 135.1667),"56": (34.4667, 135.3667),"61": (34.5000, 133.9333),
    "62": (34.4000, 132.4500),"63": (34.0500, 131.5667),"71": (34.3333, 134.0500),
    "73": (33.9333, 134.5500),"74": (33.5500, 133.5500),"75": (33.8333, 132.7667),
    "81": (33.8500, 130.8667),"83": (33.3167, 130.5167),"84": (33.2000, 130.0167),
    "85": (33.1667, 129.7167),"86": (33.2667, 131.5000),"87": (32.8000, 130.7000),
}

PREFECTURES = [
    "北海道","青森","岩手","宮城","秋田","山形","福島",
    "茨城","栃木","群馬","埼玉","千葉","東京","神奈川",
    "新潟","富山","石川","福井","山梨","長野","岐阜","静岡","愛知",
    "三重","滋賀","京都","大阪","兵庫","奈良","和歌山",
    "鳥取","島根","岡山","広島","山口",
    "徳島","香川","愛媛","高知",
    "福岡","佐賀","長崎","熊本","大分","宮崎","鹿児島","沖縄"
]

if not os.path.exists(SAVE_DIR):
    SAVE_DIR = os.getcwd()
    JSONL_PATH = os.path.join(SAVE_DIR, "keirin_data_scored_v2.jsonl")

# ============================================================
# 強化版 HTTPヘッダ (yen-joy.net の弾きを回避)
# ============================================================
BROWSER_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer': 'https://www.yen-joy.net/',
}

# リトライ設定 (v6: 段階的待機)
RETRY_WAITS = [15, 30, 60]   # 初期3回のリトライ待機
RETRY_WAIT_DEFAULT = 60      # 4回目以降の固定待機（v12: 無限リトライ用）
SUPPLEMENT_RETRY_WAITS = [5, 10]   # 補足取得用 (短い): 5秒 → 10秒 で2回リトライ

def fetch_with_retry(url, headers=None, timeout=15, max_retries=None):
    """
    URL を取得 (段階的待機リトライ付き)
    返り値: (status_code, text) - 失敗時 (-1, "")
    
    リトライ対象:
      - 403 (レート制限)
      - 5xx (サーバーエラー)
      - タイムアウト・接続エラー
    
    max_retries=None (デフォルト): 【v12】無限リトライ（取得できるまで諦めない）
    max_retries=N: N回までリトライしてスキップ（開催発見の軽い探り用）
    """
    if headers is None: headers = BROWSER_HEADERS
    
    # 待機シーケンス決定
    if max_retries is None:
        # 無限リトライ用ジェネレータ的に振る舞う（実際は大きい数で実現）
        infinite_mode = True
        waits = list(RETRY_WAITS)  # 初期は [15,30,60]
    else:
        infinite_mode = False
        waits = RETRY_WAITS[:max_retries]
    
    attempt = 0
    while True:
        # 【v19】中断フラグが立っていたら即座に失敗を返す（残りスレッドの早期終了用）
        if _abort_event.is_set():
            return -1, ""
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            resp.encoding = resp.apparent_encoding
            status = resp.status_code
            # 200 OK は即返す
            if status == 200:
                _record_fetch_success()   # 【v19】連続失敗カウンタをリセット
                if attempt > 0:
                    with _print_lock:
                        print("    [retry成功] " + str(attempt) + "回目で取得 (" + url[:80] + ")")
                return status, resp.text
            # 403 (レート制限) や 5xx はリトライ対象
            is_retryable = (status == 403) or (500 <= status < 600)
            if not is_retryable:
                # 404 などはリトライしても無駄
                return status, resp.text
            # 【v19】自動実行モード: 無限リトライ廃止、waits使い切ったら失敗カウントして打ち切り
            if AUTO_FETCH_MODE and attempt >= len(waits):
                _record_fetch_fail(url)
                return status, ""
            # リトライ判定
            if infinite_mode:
                # 無限リトライ: 初期waits使い切ったら60秒固定
                wait_sec = waits[attempt] if attempt < len(waits) else RETRY_WAIT_DEFAULT
                with _print_lock:
                    if attempt < len(waits):
                        print("    [retry] status=" + str(status) + " → " + str(wait_sec) + "秒待機 [" + str(attempt+1) + "回目] (" + url[:80] + ")")
                    else:
                        # 10回ごとに警告（沈黙しない）
                        if (attempt - len(waits) + 1) % 10 == 0:
                            print("    [長時間retry] status=" + str(status) + " → " + str(wait_sec) + "秒待機 [" + str(attempt+1) + "回目] (" + url[:80] + ")")
                time.sleep(wait_sec)
                attempt += 1
                continue
            else:
                # 有限モード（補足探り用）
                if attempt < len(waits):
                    wait_sec = waits[attempt]
                    with _print_lock:
                        print("    [retry] status=" + str(status) + " → " + str(wait_sec) + "秒待機 (" + url[:80] + ")")
                    time.sleep(wait_sec)
                    attempt += 1
                    continue
                # リトライ回数尽きた
                return status, resp.text
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as e:
            # 【v19】自動実行モード: waits使い切ったら失敗カウントして打ち切り
            if AUTO_FETCH_MODE and attempt >= len(waits):
                _record_fetch_fail(url)
                return -1, ""
            if infinite_mode:
                wait_sec = waits[attempt] if attempt < len(waits) else RETRY_WAIT_DEFAULT
                with _print_lock:
                    if attempt < len(waits):
                        print("    [retry] timeout → " + str(wait_sec) + "秒待機 [" + str(attempt+1) + "回目] (" + url[:80] + ")")
                    else:
                        if (attempt - len(waits) + 1) % 10 == 0:
                            print("    [長時間retry] timeout → " + str(wait_sec) + "秒待機 [" + str(attempt+1) + "回目] (" + url[:80] + ")")
                time.sleep(wait_sec)
                attempt += 1
                continue
            else:
                if attempt < len(waits):
                    wait_sec = waits[attempt]
                    with _print_lock:
                        print("    [retry] timeout → " + str(wait_sec) + "秒待機 (" + url[:80] + ")")
                    time.sleep(wait_sec)
                    attempt += 1
                    continue
                return -1, ""
        except Exception as e:
            # 予期しない例外: 無限モードでも数回試したらスキップ
            if infinite_mode and attempt < 5:
                wait_sec = waits[attempt] if attempt < len(waits) else RETRY_WAIT_DEFAULT
                with _print_lock:
                    print("    [retry] 例外 " + str(e)[:50] + " → " + str(wait_sec) + "秒待機 [" + str(attempt+1) + "回目]")
                time.sleep(wait_sec)
                attempt += 1
                continue
            return -1, ""

_jsonl_lock = threading.Lock()
_print_lock = threading.Lock()

# ============================================================
# 【v19】連続失敗検知 + 休止状態管理 + LINE通知
# ============================================================
#   自動実行モード(KEIRIN_AUTO=1)時のみ有効:
#   - 無限リトライを廃止し、リトライ3回で「1失敗」とカウント
#   - 連続 FAIL_LIMIT 回失敗 → 全取得を中断 (abort)
#   - 中断1回目 → 30分休止 / 2回連続 → 2時間休止
#   - 休止状態は fetch_state.json に保存し、次回起動時にチェック
#   - 中断時に LINE Messaging API でプッシュ通知
# ============================================================
AUTO_FETCH_MODE = os.environ.get("KEIRIN_AUTO", "") == "1"
FAIL_LIMIT = 10
STATE_FILE_NAME = "fetch_state.json"
_consec_fail_lock = threading.Lock()
_consec_fail_count = [0]   # リストで包んでスレッド間共有
_abort_event = threading.Event()

def _record_fetch_fail(url):
    """取得失敗を1カウント。連続 FAIL_LIMIT 回で abort フラグを立てる"""
    with _consec_fail_lock:
        _consec_fail_count[0] += 1
        n = _consec_fail_count[0]
    with _print_lock:
        print("    [連続失敗 " + str(n) + "/" + str(FAIL_LIMIT) + "] " + url[:80])
    if n >= FAIL_LIMIT and not _abort_event.is_set():
        _abort_event.set()
        with _print_lock:
            print("\n" + "!" * 60)
            print("[中断] 連続" + str(FAIL_LIMIT) + "回の取得失敗を検知 → 全取得を中断します")
            print("!" * 60)

def _record_fetch_success():
    """取得成功で連続失敗カウンタをリセット"""
    with _consec_fail_lock:
        _consec_fail_count[0] = 0

def _state_file_path():
    return os.path.join(SAVE_DIR, STATE_FILE_NAME)

def load_fetch_state():
    """休止状態の読み込み: {streak: 中断連続回数, pause_until: 再開可能epoch秒}"""
    path = _state_file_path()
    if not os.path.exists(path):
        return {"streak": 0, "pause_until": 0}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            st = json.load(f)
        if not isinstance(st, dict):
            return {"streak": 0, "pause_until": 0}
        return {"streak": int(st.get("streak", 0)),
                "pause_until": float(st.get("pause_until", 0))}
    except Exception:
        return {"streak": 0, "pause_until": 0}

def save_fetch_state(streak, pause_until):
    with open(_state_file_path(), 'w', encoding='utf-8') as f:
        json.dump({"streak": streak, "pause_until": pause_until,
                   "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False)

def send_line_message(text):
    """LINE Messaging API でプッシュ通知 (要環境変数 LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID)"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    user_id = os.environ.get("LINE_USER_ID", "").strip()
    if not token or not user_id:
        print("[LINE] トークン未設定のため通知スキップ")
        return False
    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json"},
            json={"to": user_id,
                  "messages": [{"type": "text", "text": text}]},
            timeout=15)
        print("[LINE] 通知送信 status=" + str(resp.status_code))
        return resp.status_code == 200
    except Exception as e:
        print("[LINE] 通知失敗: " + str(e)[:100])
        return False
_existing_ids = set()

# 【v14】Open-Meteo風向キャッシュ（会場+日付ごとに1日分の風向データを保持）
# キー: place_code + date_str
# 値: dict({hour_int: "風向名"}) または "ERROR"
_wind_cache = {}
_wind_cache_lock = threading.Lock()


def load_existing_ids():
    global _existing_ids
    if not os.path.exists(JSONL_PATH):
        return
    with open(JSONL_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                obj = json.loads(line)
                rid = obj.get('race_id')
                if rid: _existing_ids.add(rid)
            except json.JSONDecodeError:
                continue
    print("[INFO] 既存レース数: " + str(len(_existing_ids)) + " 件")


def save_to_jsonl(race_info, upsert=False):
    """
    upsert=False (デフォルト): 既存IDなら何もしない (新規追加のみ)
    upsert=True             : 既存IDがあれば上書き (補足再取得用)
    """
    race_id = race_info.get('race_id')
    with _jsonl_lock:
        if race_id in _existing_ids and not upsert:
            return
        if upsert and race_id in _existing_ids:
            # 上書き: ファイル全体を読み直して該当行を置換
            tmp_path = JSONL_PATH + ".tmp"
            with open(JSONL_PATH, 'r', encoding='utf-8') as fin, \
                 open(tmp_path, 'w', encoding='utf-8') as fout:
                for line in fin:
                    line_strip = line.strip()
                    if not line_strip:
                        fout.write(line)
                        continue
                    try:
                        obj = json.loads(line_strip)
                        if obj.get('race_id') == race_id:
                            # 置換
                            fout.write(json.dumps(race_info, ensure_ascii=False) + '\n')
                        else:
                            fout.write(line if line.endswith('\n') else line + '\n')
                    except json.JSONDecodeError:
                        fout.write(line)
            os.replace(tmp_path, JSONL_PATH)
            return
        # 新規
        with open(JSONL_PATH, 'a', encoding='utf-8') as f:
            f.write(json.dumps(race_info, ensure_ascii=False) + '\n')
        _existing_ids.add(race_id)


def degrees_to_direction(deg):
    directions = ["北","北北東","北東","東北東","東","東南東","南東","南南東",
                  "南","南南西","南西","西南西","西","西北西","北西","北北西"]
    idx = int((deg + 11.25) / 22.5) % 16
    return directions[idx]


def get_wind_direction(place_code, date_str, post_time_str):
    """
    【v14】会場+日付でキャッシュし、Open-Meteoへのアクセスを1日1回に削減
    キャッシュは1日分（24時間）の風向データ。post_time_str に応じた時刻のデータを返す
    """
    if place_code not in KEIRIN_LOCATION_MAP:
        return "不明"
    
    target_hour = 0
    if post_time_str and post_time_str != "--:--":
        try:
            target_hour = int(post_time_str.split(":")[0])
        except (ValueError, IndexError):
            target_hour = 0
    
    # キャッシュキー = 会場+日付
    cache_key = place_code + "_" + date_str
    
    # キャッシュチェック
    with _wind_cache_lock:
        cached = _wind_cache.get(cache_key)
    
    if cached is not None:
        if cached == "ERROR":
            return "不明"
        # 該当時刻に最も近い値を返す
        if isinstance(cached, dict):
            best_hour = 0
            best_diff = 999
            for h in cached.keys():
                diff = abs(target_hour - h)
                if diff < best_diff:
                    best_diff = diff
                    best_hour = h
            return cached.get(best_hour, "不明")
        return "不明"
    
    # 未キャッシュ: Open-Meteo から取得
    lat, lon = KEIRIN_LOCATION_MAP[place_code]
    date_fmt = date_str[:4] + "-" + date_str[4:6] + "-" + date_str[6:8]
    try:
        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {"latitude": lat, "longitude": lon, "start_date": date_fmt,
                  "end_date": date_fmt, "hourly": "wind_direction_10m", "timezone": "Asia/Tokyo"}
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            with _wind_cache_lock:
                _wind_cache[cache_key] = "ERROR"
            return "不明"
        data = resp.json()
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        dirs = hourly.get("wind_direction_10m", [])
        if not times or not dirs:
            with _wind_cache_lock:
                _wind_cache[cache_key] = "ERROR"
            return "不明"
        
        # 1日分の風向を時刻別に dict 化してキャッシュ
        hour_to_dir = {}
        for i, t in enumerate(times):
            try:
                h = int(t.split("T")[1].split(":")[0])
                deg = dirs[i]
                if deg is None:
                    continue
                hour_to_dir[h] = degrees_to_direction(float(deg))
            except (IndexError, ValueError):
                continue
        
        with _wind_cache_lock:
            _wind_cache[cache_key] = hour_to_dir
        
        # 該当時刻に最も近い値を返す
        if not hour_to_dir:
            return "不明"
        best_hour = 0
        best_diff = 999
        for h in hour_to_dir.keys():
            diff = abs(target_hour - h)
            if diff < best_diff:
                best_diff = diff
                best_hour = h
        return hour_to_dir.get(best_hour, "不明")
    except Exception:
        with _wind_cache_lock:
            _wind_cache[cache_key] = "ERROR"
        return "不明"


# ================================================================
# 【新規】 グレード / シリーズ名 / 日目 / レース種別 を抽出
# ================================================================
def extract_race_meta(html):
    """
    gamboo HTML から以下を抽出:
      grade        : F1/F2/G1/G2/G3/GP (全角→半角)
      series_name  : 第44回下野新聞社杯 など
      day_label    : 初日/2日目/3日目/最終日 など
      race_kind    : Ａ級予選/Ａ級決勝/Ｓ級準決勝/チャレンジ予選 など
    """
    out = {"grade": "", "series_name": "", "day_label": "", "race_kind": ""}
    if not html: return out

    # 全角→半角の変換マップ
    grade_map = {
        "Ｆ１": "F1", "Ｆ２": "F2",
        "Ｇ１": "G1", "Ｇ２": "G2", "Ｇ３": "G3",
        "ＧⅠ": "G1", "ＧⅡ": "G2", "ＧⅢ": "G3",
        "GＰ": "GP", "ＧＰ": "GP",
    }

    # ① <title> タグから抽出
    # 例: 「レース出走表・結果・払戻金詳細 | 宇都宮競輪 Ｆ２ 第４４回下野新聞社杯 初日（2024年01月01日） 1レース | ...」
    soup = BeautifulSoup(html, 'html.parser')
    title = soup.find('title')
    if title:
        ttext = title.get_text(strip=True)
        # グレード抽出
        for fw, hw in grade_map.items():
            if fw in ttext:
                out["grade"] = hw; break
        # シリーズ名抽出: 「{場名}競輪 {Ｆ#} (.+?) (?:初日|N日目|最終日)」
        m = re.search(r'競輪\s*[ＦＧ][\d\u2160-\u2163ⅠⅡⅢ]\w?\s*(.+?)\s*(?:初日|[０-９0-9０２-９2-9]日目|最終日)', ttext)
        if m:
            out["series_name"] = m.group(1).strip()
        # 日目抽出: 「初日」「N日目」「最終日」
        m = re.search(r'(初日|[０-９0-9]+日目|最終日)', ttext)
        if m:
            day = m.group(1)
            # 全角数字を半角に
            day = day.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
            out["day_label"] = day

    # ② レース種別 (「情報提供 {業者} {キャッチコピー} {レース種別} 発走予定」のパターン)
    # 例:
    #   情報提供 アオケイ 首位争い Ａ級決勝 発走予定 ...
    #   情報提供 中部競輪 新人平野 チャレンジ予選 発走予定 ...
    #   情報提供 競輪毎日 北陸両者 チャレンジ一般 発走予定 ...
    #   情報提供 ひかり 波乱含み Ａ級一般 発走予定 ...
    text = soup.get_text(separator=' ')
    text_norm = re.sub(r'\s+', ' ', text)
    # 「発走予定」の直前にあるトークン (空白区切り) がレース種別
    m = re.search(r'情報提供\s+\S+\s+\S+\s+(\S{2,12}?)\s+発走予定', text_norm)
    if m:
        kind = m.group(1).strip()
        # ノイズ除外: 「投票締切」「予想」などが入った場合は除外
        if kind not in ("投票締切", "予想", "勝ち上がり"):
            out["race_kind"] = kind
    if not out["race_kind"]:
        # フォールバック1: 「予想担当記者」直後を解析するシンプル版
        m2 = re.search(r'(チャレンジ[\u4e00-\u9fff]+|Ａ級\S{1,8}|Ｓ級\S{1,8}|Ｌ級\S{1,8}|ガールズ\S{1,8})\s+発走予定', text_norm)
        if m2:
            out["race_kind"] = m2.group(1).strip()

    # ③ <h3> タグから日目を補完 (titleで取れなかった場合)
    if not out["day_label"]:
        for h3 in soup.find_all('h3'):
            ht = h3.get_text(strip=True)
            m = re.search(r'(初日|[０-９0-9]+日目|最終日)', ht)
            if m:
                day = m.group(1)
                day = day.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
                out["day_label"] = day
                break

    # ④ <h3> から race_kind 補完 (アオケイで取れなかった場合)
    # 例: <h3> 12R 出走表詳細（01月03日 最終日） </h3>
    # → race_kind は別経路 (既に取得試みた)

    return out


def _build_racer_no_to_cycle_no_map(html):
    """
    HTMLから racer_no → cycle_no マップを構築 (numbering.r1〜r9 から)
    lap データの cycle_no が空のときのフォールバック用
    """
    mp = {}
    for r_idx in range(1, 10):
        block = _extract_rN_block(html, r_idx)
        if not block: continue
        rn = re.search(r'"racer_no"\s*:\s*"(\d+)"', block)
        cn = re.search(r'"cycle_no"\s*:\s*"(\d+)"', block)
        if rn and cn:
            try:
                bike = int(cn.group(1))
                if 1 <= bike <= 9:
                    mp[rn.group(1)] = bike
            except ValueError:
                pass
    return mp


def _extract_lap_entries_from_json(lap_array, racer_no_map=None):
    """
    JSON の lap配列 (first_lap / aka_ban / jyan / final_hs / final_bs) を
    既存形式 [{"bike": int, "x": int, "y": int}, ...] に変換 + 正規化
    bike は cycle_no を使う。空の場合は racer_no_map で引く (フォールバック)
    """
    entries = []
    for item in lap_array:
        if not isinstance(item, dict): continue
        cycle_no = str(item.get('cycle_no', '')).strip()
        bike = None
        # ① cycle_no が数字なら使う
        if cycle_no:
            try:
                bike = int(cycle_no)
            except ValueError:
                bike = None
        # ② フォールバック: racer_no_map から引く
        if bike is None and racer_no_map:
            racer_no = str(item.get('racer_no', '')).strip()
            if racer_no in racer_no_map:
                bike = racer_no_map[racer_no]
        # ③ どちらもダメならスキップ
        if bike is None or bike == 0:
            continue
        try:
            x = int(item.get('point_x', 0))
            y = int(item.get('point_y', 0))
        except (ValueError, TypeError):
            continue
        entries.append({"bike": bike, "x": x, "y": y})
    if not entries:
        return []
    # 既存ロジック踏襲: x昇順ソート + x rank化 + y正規化
    entries.sort(key=lambda d: (d["x"], d["y"]))
    x_vals = sorted(set(e["x"] for e in entries))
    x_rank_map = {v: i + 1 for i, v in enumerate(x_vals)}
    y_vals = sorted(set(e["y"] for e in entries))
    def _norm_y(y):
        if y == y_vals[0]: return 1
        elif len(y_vals) > 1 and y == y_vals[1]: return 2
        else: return 3
    for e in entries:
        e["x"] = x_rank_map[e["x"]]
        e["y"] = _norm_y(e["y"])
    return entries


def _extract_b_hyo_arrays(html):
    """
    HTML中のJSONから b_hyo配下の周回データを抽出
    返り値: dict { "first_lap": [...], "aka_ban": [...], ... }
    """
    out = {"first_lap": [], "aka_ban": [], "jyan": [], "final_hs": [], "final_bs": []}
    for key in out.keys():
        # ブラケットマッチで対応する ] を探す
        idx = html.find('"' + key + '"')
        if idx < 0: continue
        s = html.find('[', idx)
        if s < 0: continue
        depth = 0
        i = s
        end = -1
        while i < len(html):
            c = html[i]
            if c == '[': depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
            i += 1
        if end < 0: continue
        arr_str = html[s:end]
        # \u002F のような JSON エスケープを保持したまま json.loads
        try:
            arr = json.loads(arr_str)
            if isinstance(arr, list):
                out[key] = arr
        except (json.JSONDecodeError, ValueError):
            pass
    return out


def _extract_weather_from_json(html):
    """
    HTML中のJSONから 天気・風速 を抽出
    "aka_race_rslt":{"weather_nm":"晴","wind":"1",...}
    """
    weather = "不明"
    wind = "--"
    m_w = re.search(r'"weather_nm"\s*:\s*"([^"]+)"', html)
    if m_w:
        wv = m_w.group(1).strip()
        if wv:
            weather = wv
    m_f = re.search(r'"wind"\s*:\s*"([^"]*)"', html)
    if m_f:
        wv = m_f.group(1).strip()
        if wv and wv not in ("0", ""):
            wind = wv + "m"
    return weather, wind


def _extract_rN_block(html, n):
    """
    HTML中の "rN":[{...}] の配列ブロックを厳密ブラケットマッチで抽出
    """
    key = '"r' + str(n) + '":'
    idx = html.find(key)
    if idx < 0: return None
    s = html.find('[', idx)
    if s < 0: return None
    depth = 0
    i = s
    while i < len(html):
        c = html[i]
        if c == '[': depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                return html[s:i+1]
        i += 1
    return None


def _extract_result_from_json(html):
    """
    HTML中のJSONから 着順 (result) を抽出
    numbering.r1〜r9 を着順順に並べて、各 racer_dtl + race_rslt_racer_dtl から:
      rank, bike (cycle_no), kimar_nm (決まり手), chaks_cd (着差) を取得
    """
    DIFF_ORDER = {"ハナ":1,"1/4車輪":2,"1/2車輪":3,"3/4車輪":4,"1車輪":5,
                  "1/2車身":6,"3/4車身":7,"1車身":8,"1車身1/2":9,"2車身":10,"3車身":11,"大差":12}
    # JSON の chaks_cd は英記号 → 日本語表記に変換
    DIFF_NORMALIZE = {
        "T": "ハナ",
        "1/8W": "1/4車輪", "1/4W": "1/4車輪", "1/2W": "1/2車輪", "3/4W": "3/4車輪", "1W": "1車輪",
        "1/2B": "1/2車身", "3/4B": "3/4車身", "1B": "1車身",
        "1B1/2": "1車身1/2", "2B": "2車身", "3B": "3車身",
        "4B": "大差", "5B": "大差", "S": "大差",
    }

    result_data = []
    seen_ranks = set()
    for r_idx in range(1, 10):
        block = _extract_rN_block(html, r_idx)
        if not block: continue
        # \u002F → / に変換 (chaks_cd 用)
        block_decoded = block.replace("\\u002F", "/")

        rk_m = re.search(r'"rank"\s*:\s*"(\d+)"', block_decoded)
        cn_m = re.search(r'"cycle_no"\s*:\s*"(\d+)"', block_decoded)
        km_m = re.search(r'"kimar_nm"\s*:\s*"([^"]*)"', block_decoded)
        cd_m = re.search(r'"chaks_cd"\s*:\s*"([^"]*)"', block_decoded)
        if not (rk_m and cn_m): continue
        try:
            rank = int(rk_m.group(1))
            bike = int(cn_m.group(1))
        except ValueError:
            continue
        if rank == 0 or bike == 0: continue
        if rank in seen_ranks: continue
        seen_ranks.add(rank)

        finish = (km_m.group(1) if km_m else "--").strip() or "--"
        # 「逃残り」→「逃残」、「捲残り」→「捲残」
        if finish == "逃残り": finish = "逃残"
        elif finish == "捲残り": finish = "捲残"

        diff_raw = (cd_m.group(1) if cd_m else "").strip()
        if diff_raw and diff_raw in DIFF_NORMALIZE:
            diff_text = DIFF_NORMALIZE[diff_raw]
        elif diff_raw:
            # NB (N=数字) の場合、N≥4 なら 大差扱い (4B/5B/...11B など全部)
            m_nb = re.match(r'^(\d+)B$', diff_raw)
            if m_nb:
                try:
                    n = int(m_nb.group(1))
                    if n >= 4:
                        diff_text = "大差"
                    else:
                        diff_text = diff_raw   # 1B,2B,3B はマップに既定値あり、ここに来ないはず
                except ValueError:
                    diff_text = diff_raw
            else:
                diff_text = diff_raw
        else:
            diff_text = "--"

        result_data.append({
            "rank": rank, "bike": bike,
            "diff": diff_text,
            "diff_order": DIFF_ORDER.get(diff_text, 0),
            "finish": finish,
        })
    result_data.sort(key=lambda r: r["rank"])
    return result_data


def extract_lap_positions_enjoy(url):
    """
    yen-joy.net の レース詳細ページから JSON を抽出して
    lap, weather, result を取得 (新サイト=Angular SPA + 埋め込みJSON対応)
    """
    status, html_content = fetch_with_retry(url)
    if status != 200 or not html_content:
        return {}, "未検出", "天気:不明 風速:--", []
    try:
        # ① lap 配列を抽出
        b_hyo = _extract_b_hyo_arrays(html_content)
        # ② cycle_no 空のとき用のフォールバックマップを構築
        racer_no_map = _build_racer_no_to_cycle_no_map(html_content)
        # ③ ラベル名対応
        label_map = [
            ("first_lap", "周回中"),
            ("aka_ban", "赤板"),
            ("jyan", "打鐘"),
            ("final_hs", "ホーム"),
            ("final_bs", "バック"),
        ]
        lap_data = {}
        res_list = []
        for json_key, jp_label in label_map:
            arr = b_hyo.get(json_key, [])
            if not arr: continue
            entries = _extract_lap_entries_from_json(arr, racer_no_map=racer_no_map)
            if not entries: continue
            lap_data[jp_label] = entries
            display = []
            for e in entries:
                if e["y"] == 1: display.append(str(e["bike"]))
                elif e["y"] == 2: display.append("(" + str(e["bike"]) + ")")
                else: display.append("((" + str(e["bike"]) + "))")
            res_list.append(jp_label + ": " + "-".join(display))
        lap_positions = " / ".join(res_list) if res_list else "未検出"

        # ③ 天気・風速
        weather, wind = _extract_weather_from_json(html_content)
        weather_info = "天気:" + weather + " 風速:" + wind

        # ④ 着順
        result_data = _extract_result_from_json(html_content)

        return lap_data, lap_positions, weather_info, result_data
    except Exception:
        return {}, "エラー", "天気:不明 風速:--", []


def clean_history(text):
    if pd.isna(text) or text == "": return "なし"
    text = text.replace("[映像]", "").replace("\n", " ").strip()
    m_place = re.search(r'^([^\d]+ \w\d)', text)
    m_date = re.search(r'(\d{1,2}/\d{1,2})', text)
    results = re.findall(r'(\d)着', text)
    place = m_place.group(1) if m_place else ""
    date = m_date.group(1) if m_date else ""
    res_str = "・".join(results) if results else ""
    return (place + " " + date + " " + res_str).strip() if res_str else "なし"


def get_line(place_code, actual_date_str, race_no):
    date_fmt = actual_date_str[:4] + "-" + actual_date_str[4:6] + "-" + actual_date_str[6:8]
    url = "https://www.chariloto.com/keirin/results/" + place_code + "/" + date_fmt
    try:
        status, html = fetch_with_retry(url)
        if status != 200 or not html:
            return ""
        tables = pd.read_html(StringIO(html))
        idx = (int(race_no) - 1) * 7 + 3
        df = tables[idx]
        groups = []
        current = []
        for v in df.iloc[0]:
            if pd.isna(v):
                if current:
                    groups.append("".join(map(str, current))); current = []
            else:
                try: current.append(int(v))
                except Exception: pass
        if current: groups.append("".join(map(str, current)))
        return "-".join(groups)
    except Exception:
        return ""


def get_race_data(place_code, p_name, base_date, day, actual_dt, race_no, upsert=False):
    actual_date_str = actual_dt.strftime("%Y%m%d")
    base_date_str = base_date.strftime("%Y%m%d")
    race_id = place_code + actual_date_str + str(race_no).zfill(2)
    # upsert=True なら既存スキップしない (補足再取得用)
    if race_id in _existing_ids and not upsert:
        return True
    _, last_day = calendar.monthrange(base_date.year, base_date.month)
    if base_date.day == last_day:
        if base_date.month == 12:
            url_ym_str = str(base_date.year + 1) + "01"
        else:
            url_ym_str = str(base_date.year) + str(base_date.month + 1).zfill(2)
    else:
        url_ym_str = base_date.strftime("%Y%m")
    gamboo_url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/result/"
        + place_code + base_date_str + "/"
        + place_code + base_date_str + str(day).zfill(2) + "00/"
        + str(race_no).zfill(2) + "/")
    enjoy_url = ("https://www.yen-joy.net/kaisai/race/result/detail/"
        + url_ym_str + "/" + place_code + "/" + base_date_str + "/" + actual_date_str + "/" + str(race_no))
    
    # 【v16】 HTTP並列化を取り消して逐次に戻す
    # 理由: 並列度が高すぎてサーバー側でレート制限がかかり、lap=空応答が頻発
    # 逐次なら新規取得時の lap 取得成功率が大幅向上
    line_formation = get_line(place_code, actual_date_str, race_no)
    lap_data, lap_positions, weather_info, result_data = extract_lap_positions_enjoy(enjoy_url)
    status, html = fetch_with_retry(gamboo_url)
    
    players = {i: {"full_info": "未取得", "h1": "なし", "h2": "なし", "h3": "なし", "style": "--"} for i in range(1, 10)}
    try:
        if status != 200 or not html:
            return False

        # 【新規】グレード等を抽出
        meta = extract_race_meta(html)

        soup_g = BeautifulSoup(html, 'html.parser')
        post_time = "--:--"
        dt_tag = soup_g.find('dt', string=re.compile("発走予定"))
        if dt_tag:
            dd_tag = dt_tag.find_next_sibling('dd')
            if dd_tag:
                post_time = dd_tag.get_text(strip=True)
        if "風速:--" in weather_info:
            wind_dir = "--"
        else:
            wind_dir = get_wind_direction(place_code, actual_date_str, post_time)
        weather_info = weather_info + " 風向:" + wind_dir
        tables = pd.read_html(StringIO(html))
        if len(tables) < 3:
            return False
        df_style = tables[0]
        for _, row in df_style.iterrows():
            vals = [str(v).strip() for v in row.values]
            try:
                c_idx = int(vals[4])
                style_match = re.search(r'[SAL]\d\s+([\u9003\u6378\u5dee\u8ffd\u4e21])', " ".join(vals))
                if style_match:
                    players[c_idx]["style"] = style_match.group(1)
            except Exception:
                pass
        df_hist = tables[2]
        for _, row in df_hist.iterrows():
            vals = [str(v).strip() for v in row.values]
            c_idx_match = re.search(r'(\d)', vals[1])
            if not c_idx_match: continue
            c_idx = int(c_idx_match.group(1))
            raw_info = " ".join(vals[2].split())
            parts = raw_info.split('/')
            name_pref_raw = parts[0].strip()
            found_pref = "不明"
            found_name = name_pref_raw
            search_target = name_pref_raw.replace(" ", "").replace("\u3000", "")
            for pref in sorted(PREFECTURES, key=len, reverse=True):
                if search_target.endswith(pref):
                    found_pref = pref
                    temp_name = name_pref_raw
                    for char in reversed(list(pref)):
                        temp_name = re.sub(str(char) + r"\s*$", "", temp_name).strip()
                    found_name = temp_name
                    break
            age = parts[1].strip() if len(parts) > 1 else "--"
            ki = parts[2].strip() if len(parts) > 2 else "--"
            score = vals[3]
            players[c_idx]["full_info"] = found_name + "/" + found_pref + "/" + age + "歳/" + ki + "期/" + score + "点"
            players[c_idx]["h1"] = clean_history(vals[4])
            players[c_idx]["h2"] = clean_history(vals[5])
            players[c_idx]["h3"] = clean_history(vals[6])
        m2 = re.search(r'2車(?:連)?単.*?(\d+-\d+).*?([\d,]+)円', html, re.S)
        r2, p2 = (m2.group(1), m2.group(2)) if m2 else ("-", "0")
        m3 = re.search(r'3連(?:勝)?単.*?(\d+-\d+-\d+).*?([\d,]+)円', html, re.S)
        r3, p3 = (m3.group(1), m3.group(2)) if m3 else ("-", "0")
        # 【v11】 スコア計算: 取得済み players からスコアを付与
        players_for_save = {str(i): players[i] for i in range(1, 10) if players[i]["full_info"] != "未取得"}
        normalize_scores_in_race(players_for_save)
        race_dict = {
            'race_id': race_id,
            'date': actual_date_str,
            'race_no': int(race_no),
            'place': p_name,
            'post_time': post_time,
            'weather': weather_info,
            # 【新規フィールド】
            'grade': meta["grade"],
            'series_name': meta["series_name"],
            'day_label': meta["day_label"],
            'race_kind': meta["race_kind"],
            # 既存フィールド
            'line': line_formation,
            'lap': lap_data,
            'result': result_data,
            'refund_2t': r2 + "(" + p2 + "円)",
            'refund_3t': r3 + "(" + p3 + "円)",
            'players': players_for_save
        }
        save_to_jsonl(race_dict, upsert=upsert)
        with _print_lock:
            if upsert:
                # 【v14】補完時は1行ログ（高速化、画面出力軽減）
                lap_ok = "lap=OK" if lap_data and any(lap_data.values()) else "lap=NG"
                res_ok = "res=OK" if result_data else "res=NG"
                print("  [補完OK] " + p_name + " R" + str(race_no) + " (" + race_id + ") " + lap_ok + " " + res_ok)
            else:
                # 新規取得時は従来の詳細表示
                print("\n============================================================")
                print("【レース詳細】 " + p_name + " " + str(race_no) + "R (race_id: " + race_id + ")")
                print("発走時間  : " + post_time)
                print("グレード  : " + (meta["grade"] or "(不明)"))
                print("シリーズ  : " + (meta["series_name"] or "(不明)"))
                print("日目      : " + (meta["day_label"] or "(不明)"))
                print("レース種別: " + (meta["race_kind"] or "(不明)"))
                print("気象条件  : " + weather_info)
                print("並び(Line) : " + line_formation)
                print("周回(Lap)  : " + lap_positions)
                print("------------------------------------------------------------")
                print("出場選手と近況成績:")
                for i in range(1, 10):
                    p = players[i]
                    if p["full_info"] != "未取得":
                        print("  " + str(i) + "番車: " + p['full_info'] + " [" + p['style'] + "]")
                print("------------------------------------------------------------")
                # 【DEBUG】 保存される全データを表示 (確認用、不要なら DEBUG_DUMP=False に)
                if DEBUG_DUMP:
                    print("--- RAW DUMP (このレースの保存データ全体) ---")
                    # 1) result (着順 + 決まり手 + 着差)
                    print("  [result]")
                    for r in race_dict.get('result', []):
                        print("    " + json.dumps(r, ensure_ascii=False))
                    # 2) lap (5ラップ全部)
                    print("  [lap]")
                    for label, entries in race_dict.get('lap', {}).items():
                        print("    " + label + ":")
                        for e in entries:
                            print("      " + json.dumps(e, ensure_ascii=False))
                    # 3) refund
                    print("  [refund_2t] " + str(race_dict.get('refund_2t', '')))
                    print("  [refund_3t] " + str(race_dict.get('refund_3t', '')))
                    # 4) players (raw)
                    print("  [players]")
                    for bs, p in race_dict.get('players', {}).items():
                        print("    " + bs + "番: " + json.dumps(p, ensure_ascii=False))
                    # 5) その他フィールド
                    print("  [date]        " + str(race_dict.get('date', '')))
                    print("  [post_time]   " + str(race_dict.get('post_time', '')))
                    print("  [weather]     " + str(race_dict.get('weather', '')))
                    print("  [grade]       " + str(race_dict.get('grade', '')))
                    print("  [series_name] " + str(race_dict.get('series_name', '')))
                    print("  [day_label]   " + str(race_dict.get('day_label', '')))
                    print("  [race_kind]   " + str(race_dict.get('race_kind', '')))
                    print("  [line]        " + str(race_dict.get('line', '')))
                print("============================================================\n")
        return True
    except Exception as e:
        with _print_lock:
            print("[エラー] " + p_name + " R" + str(race_no) + ": " + str(e))
        return False


def process_place(p_code, p_name, current_dt):
    count = 0
    for d in range(1, 5):
        potential_base_date = current_dt - timedelta(days=d - 1)
        base_date_str = potential_base_date.strftime("%Y%m%d")
        test_url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/result/"
            + p_code + base_date_str + "/"
            + p_code + base_date_str + str(d).zfill(2) + "00/01/")
        try:
            # 開催発見の探りは5回までリトライ（v12: 通信エラー耐性強化）
            # 15→30→60→60→60秒 = 最大約4分粘る
            status, body = fetch_with_retry(test_url, timeout=5, max_retries=5)
            if status == 200 and body and "レース結果がありません" not in body and '<table' in body:
                with _print_lock:
                    print("--- 開催発見: " + p_name + " (" + base_date_str + "開始の第" + str(d) + "日目) ---")
                for r in range(1, 13):
                    if get_race_data(p_code, p_name, potential_base_date, d, current_dt, r):
                        count += 1
                        # 【v14】sleep削除（高速化）
                    else:
                        break
        except Exception:
            pass
    return count


def find_holes_in_db(target_date_str):
    """
    DB から target_date_str に該当する穴ありレコードを抽出
    戻り値: [(race_id, place_code, place_name, base_date_str, day, race_no), ...]
    """
    if not os.path.exists(JSONL_PATH):
        return []

    # 場所名 → コード逆引き
    NAME_TO_CODE = {v: k for k, v in KEIRIN_CODE_MAP.items()}

    holes = []
    with open(JSONL_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict): continue
            date = rec.get('date', '')
            if date != target_date_str: continue

            # 穴判定
            lap = rec.get('lap')
            weather = rec.get('weather', '')
            lap_hole = False
            if lap is None: lap_hole = True
            elif isinstance(lap, str): lap_hole = True   # "未検出" / "エラー" 等
            elif isinstance(lap, dict) and not lap: lap_hole = True
            weather_hole = False
            if not weather: weather_hole = True
            elif isinstance(weather, str):
                if "天気:不明" in weather: weather_hole = True
                elif "天気:エラー" in weather: weather_hole = True
                elif "天気:未検出" in weather: weather_hole = True

            if not (lap_hole or weather_hole):
                continue

            # race_id から place_code, race_no を抽出
            race_id = rec.get('race_id', '')
            if len(race_id) != 12: continue
            place_code = race_id[:2]
            race_no = int(race_id[10:12])
            place_name = rec.get('place', NAME_TO_CODE.get(place_code, ""))

            # base_date を逆算: 既存ロジックでは day を含む URL 構築が必要
            # 既存DBの場合 base_date 情報が無いので、actual_date と同じ初日として扱う
            # 失敗ケースは複数 day を試す
            holes.append({
                'race_id': race_id,
                'place_code': place_code,
                'place_name': place_name,
                'actual_date': date,
                'race_no': race_no,
                'why': ('lap' if lap_hole else '') + ('+weather' if weather_hole else ''),
            })
    return holes


def _is_record_still_hole(race_id):
    """JSONL から race_id のレコードを引いて、まだ穴があるかチェック"""
    if not os.path.exists(JSONL_PATH):
        return True
    with open(JSONL_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try: rec = json.loads(line)
            except json.JSONDecodeError: continue
            if rec.get('race_id') != race_id: continue
            # 穴判定
            lap = rec.get('lap')
            weather = rec.get('weather', '')
            lap_hole = False
            if lap is None: lap_hole = True
            elif isinstance(lap, str): lap_hole = True
            elif isinstance(lap, dict) and not lap: lap_hole = True
            weather_hole = False
            if not weather: weather_hole = True
            elif isinstance(weather, str):
                if "天気:不明" in weather or "天気:エラー" in weather or "天気:未検出" in weather:
                    weather_hole = True
            return lap_hole or weather_hole
    return True


def _supplement_one_race(h):
    """
    1レコード分の補足取得を実行。
    base_date 候補 0〜3日前を順に試行 + 失敗時は 5秒/10秒待機リトライ
    返り値: True (補完成功) / False (失敗)
    """
    race_id = h['race_id']
    place_code = h['place_code']
    place_name = h['place_name']
    actual_date_str = h['actual_date']
    race_no = h['race_no']
    actual_dt = datetime.strptime(actual_date_str, "%Y%m%d")

    # 試行ループ: 1回目=即時、2回目=5秒待機、3回目=10秒待機
    for attempt, wait_before in enumerate([0] + SUPPLEMENT_RETRY_WAITS):
        if wait_before > 0:
            with _print_lock:
                print("    [補足リトライ] " + str(wait_before) + "秒待機後 再試行 (" + race_id + ")")
            time.sleep(wait_before)

        # base_date 候補 0〜3日前を試す
        for diff_days in range(0, 4):
            base_dt = actual_dt - timedelta(days=diff_days)
            base_date_str = base_dt.strftime("%Y%m%d")
            day = diff_days + 1
            test_url = ("https://keirin.kdreams.jp/gamboo/keirin-kaisai/race-card/result/"
                + place_code + base_date_str + "/"
                + place_code + base_date_str + str(day).zfill(2) + "00/01/")
            status, body = fetch_with_retry(test_url, timeout=5, max_retries=1)
            if status != 200 or not body or "レース結果がありません" in body or '<table' not in body:
                continue
            with _print_lock:
                print("  [再取得] " + place_name + " R" + str(race_no)
                      + " (" + race_id + ") base=" + base_date_str + " day" + str(day)
                      + " (試行" + str(attempt + 1) + ") 理由=" + h['why'])
            # 再取得実行
            ok = get_race_data(place_code, place_name, base_dt, day, actual_dt, race_no, upsert=True)
            # 【v14】sleep削除（高速化）
            if ok:
                # 取得後に「まだ穴か」チェック
                if not _is_record_still_hole(race_id):
                    return True   # 補完成功
                # まだ穴なら次の試行へ (lap=未検出でDB上書きされた状態)
            break  # base_date は1個成功したらこの試行は完了

    return False


def supplement_holes_for_date(target_date_str):
    """
    補足取得フェーズ:
    target_date_str の DB レコードのうち、穴のあるレースを再取得して上書き
    1レコード単位で 5秒待機 / 10秒待機 のリトライを行う
    """
    holes = find_holes_in_db(target_date_str)
    if not holes:
        print("\n[補足] " + target_date_str + " に穴なし、スキップ")
        return 0

    print("\n" + "=" * 60)
    print("[補足取得フェーズ] " + target_date_str + ": " + str(len(holes)) + " 件の穴を再取得")
    print("=" * 60)

    success = 0
    for h in holes:
        # 【v19】中断フラグチェック
        if _abort_event.is_set():
            print("[補足] 中断フラグ検知 → 補足取得を打ち切り")
            break
        if _supplement_one_race(h):
            success += 1
        else:
            with _print_lock:
                print("  [再取得失敗] " + h['place_name'] + " R" + str(h['race_no'])
                      + " (" + h['race_id'] + ") → スキップ")

    print("[補足] 完了: " + str(success) + " / " + str(len(holes)) + " 件 補完成功")
    return success


def _prompt_date(prompt_text, default=None):
    """日付入力プロンプト。YYYYMMDD 形式で入力。空Enterならdefault使用。"""
    while True:
        if default:
            user_input = input(prompt_text + " [" + default + "]: ").strip()
        else:
            user_input = input(prompt_text + ": ").strip()
        if not user_input and default:
            user_input = default
        if len(user_input) != 8 or not user_input.isdigit():
            print("  [エラー] YYYYMMDD 形式の8桁数字で入力してください (例: 20260515)")
            continue
        try:
            datetime.strptime(user_input, "%Y%m%d")
            return user_input
        except ValueError:
            print("  [エラー] 無効な日付です。再入力してください")


if __name__ == "__main__":
    # ===== 日付入力 =====
    print("="*60)
    print("競輪データ取得 v20 (休止短縮 30分/2時間 + 保険実行の休止無視)")
    print("="*60)
    print()
    today_str = datetime.now().strftime("%Y%m%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")

    # 【v17】自動実行モード (GitHub Actions 用)
    #   KEIRIN_AUTO=1     → 対話入力・確認をスキップ
    #   KEIRIN_START      → 取得開始日 (省略時: 昨日)
    #   KEIRIN_END        → 取得終了日 (省略時: 開始日と同じ)
    # 【v18】キャッチアップモード (進捗ファイルで続きから自動取得)
    #   KEIRIN_CATCHUP=1      → 進捗ファイルの続きから KEIRIN_CHUNK_DAYS 日分取得
    #   KEIRIN_INIT_START     → 初回開始日 (省略時: 20260101)
    #   KEIRIN_CHUNK_DAYS     → 1回の実行で取得する日数 (省略時: 5)
    #   ※ KEIRIN_START が指定されている場合は v17 AUTO モードを優先
    AUTO_MODE = os.environ.get("KEIRIN_AUTO", "") == "1"
    CATCHUP_MODE = (os.environ.get("KEIRIN_CATCHUP", "") == "1"
                    and not os.environ.get("KEIRIN_START", "").strip())
    PROGRESS_FILE = os.path.join(SAVE_DIR, "fetch_progress.txt")

    if CATCHUP_MODE:
        import sys
        # 【v19】休止状態チェック (連続失敗による休止中なら何もせず終了)
        # 【v20】KEIRIN_IGNORE_PAUSE=1 (朝5:30の保険実行) は休止を無視して実行
        _ignore_pause = os.environ.get("KEIRIN_IGNORE_PAUSE", "").strip() == "1"
        _st = load_fetch_state()
        _now_epoch = time.time()
        if _st["pause_until"] > _now_epoch and not _ignore_pause:
            _remain_min = int((_st["pause_until"] - _now_epoch) / 60) + 1
            print("[休止中] 連続取得失敗による休止期間です (中断" + str(_st["streak"])
                  + "回目)。再開まで約 " + str(_remain_min) + " 分。今回は終了")
            sys.exit(0)
        if _st["pause_until"] > _now_epoch and _ignore_pause:
            print("[保険実行] 休止期間中ですが KEIRIN_IGNORE_PAUSE=1 のため実行します")
        INIT_START = os.environ.get("KEIRIN_INIT_START", "20260101").strip()
        try:
            CHUNK_DAYS = int(os.environ.get("KEIRIN_CHUNK_DAYS", "5"))
        except ValueError:
            CHUNK_DAYS = 5
        if CHUNK_DAYS < 1:
            CHUNK_DAYS = 1
        # 進捗ファイル読み込み (最後に取得完了した日付 YYYYMMDD)
        last_done = ""
        if os.path.exists(PROGRESS_FILE):
            with open(PROGRESS_FILE, 'r', encoding='utf-8') as pf:
                last_done = pf.read().strip()
        if len(last_done) == 8 and last_done.isdigit():
            chunk_start_dt = datetime.strptime(last_done, "%Y%m%d") + timedelta(days=1)
            print("[キャッチアップ] 進捗: " + last_done + " まで取得済み")
        else:
            chunk_start_dt = datetime.strptime(INIT_START, "%Y%m%d")
            print("[キャッチアップ] 進捗ファイルなし → 初回開始日 " + INIT_START + " から開始")
        yesterday_dt = datetime.now() - timedelta(days=1)
        if chunk_start_dt.strftime("%Y%m%d") > yesterday_dt.strftime("%Y%m%d"):
            print("[キャッチアップ] 昨日分まで取得済み。今回は何もせず終了")
            sys.exit(0)
        chunk_end_dt = chunk_start_dt + timedelta(days=CHUNK_DAYS - 1)
        if chunk_end_dt > yesterday_dt:
            chunk_end_dt = yesterday_dt
        START_DATE = chunk_start_dt.strftime("%Y%m%d")
        END_DATE = chunk_end_dt.strftime("%Y%m%d")
        AUTO_MODE = True
        print("[キャッチアップ] 今回の取得範囲: " + START_DATE + " 〜 " + END_DATE)
    elif AUTO_MODE:
        START_DATE = os.environ.get("KEIRIN_START", "").strip() or yesterday_str
        END_DATE = os.environ.get("KEIRIN_END", "").strip() or START_DATE
        if len(START_DATE) != 8 or not START_DATE.isdigit() or len(END_DATE) != 8 or not END_DATE.isdigit():
            print("[エラー] KEIRIN_START / KEIRIN_END は YYYYMMDD 形式で指定してください")
            import sys
            sys.exit(1)
        print("[自動実行モード] 対話入力をスキップ")
    else:
        START_DATE = _prompt_date("取得開始日 (YYYYMMDD)", default=yesterday_str)
        END_DATE = _prompt_date("取得終了日 (YYYYMMDD)", default=START_DATE)
    
    # 範囲チェック
    if START_DATE > END_DATE:
        print("[エラー] 開始日が終了日より後ろです。終了します。")
        import sys
        sys.exit(1)
    
    # 確認
    days_count = (datetime.strptime(END_DATE, "%Y%m%d") - datetime.strptime(START_DATE, "%Y%m%d")).days + 1
    print()
    print("取得期間: " + START_DATE + " 〜 " + END_DATE + " (" + str(days_count) + "日間)")
    print("保存先  : " + JSONL_PATH)
    print()
    if not AUTO_MODE:
        confirm = input("実行しますか? [y/N]: ").strip().lower()
        if confirm not in ('y','yes'):
            print("キャンセルしました")
            import sys
            sys.exit(0)
    
    # ===== 本処理 =====
    load_existing_ids()
    start_dt = datetime.strptime(START_DATE, "%Y%m%d")
    end_dt = datetime.strptime(END_DATE, "%Y%m%d")
    total_race_count = 0
    total_supplemented = 0
    print("\n[開始] " + START_DATE + " から " + END_DATE + " (v13: 補足取得+スコア統合+無限リトライ)")
    print("保存先JSONL: " + JSONL_PATH + "\n")
    current_dt = start_dt
    while current_dt <= end_dt:
        # 【v19】中断フラグチェック
        if _abort_event.is_set():
            break
        date_str = current_dt.strftime("%Y%m%d")
        print("\n>>> 日付: " + current_dt.strftime("%Y/%m/%d") + " をスキャン中...")
        place_items = sorted(KEIRIN_CODE_MAP.items())
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(process_place, p_code, p_name, current_dt): p_name for p_code, p_name in place_items}
            for future in as_completed(futures):
                try:
                    total_race_count += future.result()
                except Exception as e:
                    with _print_lock:
                        print("[エラー] " + futures[future] + ": " + str(e))

        # === 補足取得フェーズ (その日が終わった直後) ===
        if not _abort_event.is_set():
            total_supplemented += supplement_holes_for_date(date_str)

        current_dt += timedelta(days=1)

    # 【v19】中断処理: 休止状態を保存して LINE 通知
    if _abort_event.is_set() and AUTO_FETCH_MODE:
        import sys
        _st = load_fetch_state()
        _streak = _st["streak"] + 1
        if _streak >= 2:
            _pause_minutes = 120
        else:
            _pause_minutes = 30
        _pause_until = time.time() + _pause_minutes * 60
        save_fetch_state(_streak, _pause_until)
        _resume_at = datetime.fromtimestamp(_pause_until).strftime("%m/%d %H:%M")
        _msg = ("【競輪DB取得 中断】\n"
                + "連続" + str(FAIL_LIMIT) + "回の取得失敗を検知しました。\n"
                + "サイトメンテナンス等の可能性があります。\n"
                + "中断連続回数: " + str(_streak) + "回目\n"
                + "休止: " + str(_pause_minutes) + "分 (再開予定 " + _resume_at + " ころ)\n"
                + "取得済範囲: " + START_DATE + "〜 (中断日: " + date_str + ")\n"
                + "新規取得: " + str(total_race_count) + "件まで処理済")
        print("\n" + "=" * 60)
        print(_msg)
        print("=" * 60)
        send_line_message(_msg)
        # 進捗ファイルは更新しない → 次回は同じ日から再開 (upsertなので重複しない)
        sys.exit(0)

    print("\n[完了] 新規取得: " + str(total_race_count) + " 件")
    print("        補足取得: " + str(total_supplemented) + " 件")
    print("        ※ スコア計算済み（v19）")
    print("保存先: " + JSONL_PATH)

    # 【v19】正常完了: 中断連続回数をリセット
    if AUTO_FETCH_MODE:
        _st = load_fetch_state()
        if _st["streak"] > 0 or _st["pause_until"] > 0:
            save_fetch_state(0, 0)
            print("[復旧] 取得成功 → 中断カウンタをリセット")
            send_line_message("【競輪DB取得 復旧】\n取得が正常に再開されました。\n"
                              + "取得範囲: " + START_DATE + "〜" + END_DATE)

    # 【v18】キャッチアップモード: 進捗ファイル更新
    if CATCHUP_MODE:
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as pf:
            pf.write(END_DATE)
        print("[キャッチアップ] 進捗更新: " + END_DATE + " まで完了 → " + PROGRESS_FILE)

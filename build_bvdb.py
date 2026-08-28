"""BVDB.json を作る。

ウィンチケットのAPIと、手元の統合DBを突き合わせて、
分析しやすい形の1レース1行のデータベースを作る。

  ウィンチケット (race_detail)
      レース諸元 / 選手プロフィール / 競走得点 / S・H・B /
      決まり手内訳 / 着別度数 / 直近成績 / EXデータ全種 /
      対戦成績 / 全券種の的中目・人気・払戻金 / 並び予想 / バンク要目
  統合DB (keirin_data_scored_v2.jsonl)
      着順 / 決まり手 / 着差 / 周回中の隊列(lap)

置き場所は決め打ちにしない。engine も統合DBも自分で探す。

  再生ボタンを押すだけで動く。
  GitHub Actions でも同じスクリプトがそのまま動く。

出力:
  bvdb/BVDB_YYYYMMDD.json    日ごと。既にあれば飛ばすので再開できる。
  bvdb/venues.json           会場マスタ (バンク要目)。初回だけ作る。
  bvdb/BVDB.json             全日をまとめたもの (--merge のとき)

環境変数 (GitHub Actions 用。無ければ既定値):
  BVDB_FROM   開始日 YYYYMMDD  (既定 20260415 = Hが入った日)
  BVDB_TO     終了日 YYYYMMDD  (既定 統合DBの最終日)
  BVDB_MERGE  1 なら日別ファイルを BVDB.json にまとめる
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    import requests
    HAS_REQUESTS = True
except Exception:
    HAS_REQUESTS = False


# ============================================================
# 設定
# ============================================================
# H (ホーム回数) がAPIに入った日。これより前は H が全員 0 になる。
DEFAULT_FROM = "20260415"

OUT_DIR = "bvdb"
API_ROOT = "https://api.winticket.jp/v1/keirin"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android) keirin-oracle/1.0",
    "Accept": "application/json",
    "Referer": "https://www.winticket.jp/",
}
SLEEP = 0.15          # 1リクエストの間隔
RETRY = 3             # 取得の再試行
MAX_LOOKBACK = 10     # cupId 解決で遡る日数

MAX_WALK_DEPTH = 6
SKIP_DIRS = ("Android", "node_modules", ".git", ".cache", "cache",
             "DCIM", "Pictures", "Movies", "Music", "WhatsApp", "bvdb")

DB_NAMES = ["keirin_data_scored_v2.jsonl"]
DB_ROOTS = [
    "/storage/emulated/0/Download/takusen/data",
    "/storage/emulated/0/Download/takusen",
    "/storage/emulated/0/Download",
    ".", "..",
    "takusen/data",
    "/sdcard/Download",
    "/storage/emulated/0",
    os.path.expanduser("~"),
]
ENGINE_NAME = "predict_v14_wind_unified.py"
ENGINE_ROOTS = [
    "/storage/emulated/0/Download/takusen/code",
    "/storage/emulated/0/Download/takusen",
    "/storage/emulated/0/Download",
    ".", "..",
    "takusen/code",
    "/sdcard/Download",
    "/storage/emulated/0",
    os.path.expanduser("~"),
]

# 既存スクリプトと同じ対応表を使う (勝手に作らない)
GRADE_NUM = {1: "F2", 2: "F1", 3: "G3"}
CLASS_NUM = {1: "S", 2: "A", 3: "L"}

# cups[].labels と時間区分。
#   実データ22開催で発走時刻と突き合わせて確かめたもの。
#     5    08:30〜08:40  モーニング
#     なし 10:45〜10:56  デイ
#     1    15:15〜16:09  ナイター
#     2    20:40〜20:50  ミッドナイト
#   3 と 4 は全時間帯に散らばっており、時間区分ではない。
#   併用されるだけなので無視する ([5,3]=モーニング, [2,3]=ミッドナイト)。
#   サマータイムに当たる数値は見つからなかったので判定しない。
LABEL_ZONE = {5: "モーニング", 1: "ナイター", 2: "ミッドナイト"}

# 選手ごとの成績で使われている時間区分 (キー名 -> 呼び名)
HOUR_KEYS = {
    "hourTypeNormal": "デイ",
    "hourTypeMorning": "モーニング",
    "hourTypeNight": "ナイター",
    "hourTypeMidnight": "ミッドナイト",
    "hourTypeSummertime": "サマータイム",
}

# EXデータ 戦法別
EX_KEYS = {
    "exSpurt": "かまし",
    "exThrust": "つっぱり",
    "exLeftBehind": "ちぎられ",
    "exSplitLine": "ちぎり",
    "exSnatch": "飛びつき",
    "exCompete": "競り",
}

# EXデータ 位置別
POS_KEYS = {
    "linePositionFirst": "ライン先頭",
    "linePositionSecond": "番手",
    "linePositionThird": "3番手以降",
    "lineSingleHorseman": "単騎",
    "lineCompete": "競り",
}

# EXデータ レース種別別
RTYPE_KEYS = {
    "raceTypeQualifyingRound": "予選",
    "raceTypeSemifinal": "準決勝",
    "raceTypeFinal": "決勝",
    "raceTypeLoserRound": "敗者戦",
    "raceTypeSpecial": "特選",
}

# EXデータ 周長別・天候別
TRACK_KEYS = {"trackDistance333": "333", "trackDistance400": "400",
              "trackDistance500": "500"}
WEATHER_KEYS = {"weatherSunny": "晴", "weatherCloudy": "曇",
                "weatherRainy": "雨", "weatherSnowy": "雪"}

# 券種。枠単・枠複は成立しないので取らない (的中目も払戻も空)。
BET_KINDS = [
    ("trifecta", "3連単", "trifectaWinningOddsIds"),
    ("trio", "3連複", "trioWinningOddsIds"),
    ("exacta", "2車単", "exactaWinningOddsIds"),
    ("quinella", "2車複", "quinellaWinningOddsIds"),
    ("quinellaPlace", "ワイド", "quinellaPlaceWinningOddsIds"),
]


# ============================================================
# 探索
# ============================================================
_ENGINE = None
_ENGINE_TRIED = False


def _walk_find(roots, want_file):
    for root in roots:
        try:
            root = os.path.abspath(root)
        except Exception:
            continue
        if not os.path.isdir(root):
            continue
        base = root.rstrip(os.sep).count(os.sep)
        try:
            for dp, dns, fns in os.walk(root):
                if dp.count(os.sep) - base >= MAX_WALK_DEPTH:
                    dns[:] = []
                    continue
                dns[:] = [d for d in dns
                          if d not in SKIP_DIRS and not d.startswith(".")]
                if want_file in fns:
                    return os.path.join(dp, want_file)
        except Exception:
            pass
    return None


def load_engine():
    global _ENGINE, _ENGINE_TRIED
    if _ENGINE_TRIED:
        return _ENGINE
    _ENGINE_TRIED = True
    try:
        import predict_v14_wind_unified as eng
        _ENGINE = eng
        return _ENGINE
    except Exception:
        pass
    fp = _walk_find(ENGINE_ROOTS, ENGINE_NAME)
    if not fp:
        print("[find] engine が見つかりません")
        return None
    d = os.path.dirname(fp)
    print("[find] engine: " + d)
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import predict_v14_wind_unified as eng
        _ENGINE = eng
    except Exception as e:
        # pandas など重い依存で落ちることがある。
        # 会場コードは API からも作れるので、ここでは止めない。
        print("[find] engine を読み込めません (" + str(e)[:50]
              + ") → API から会場コードを作ります")
        _ENGINE = None
    return _ENGINE


def find_db():
    # 素直に手元にあるならそれでよい (Actions は直下に置く)
    for nm in DB_NAMES:
        if os.path.exists(nm):
            return os.path.abspath(nm)
    eng = load_engine()
    if eng is not None:
        pdb = getattr(eng, "PATH_DB", None)
        if pdb and os.path.exists(str(pdb)):
            return str(pdb)
        for attr in ("DATA_DIR", "SAVE_DIR"):
            v = getattr(eng, attr, None)
            if v:
                for nm in DB_NAMES:
                    fp = os.path.join(str(v), nm)
                    if os.path.exists(fp):
                        return fp
    for nm in DB_NAMES:
        fp = _walk_find(DB_ROOTS, nm)
        if fp:
            return fp
    return None


VENUE_MAP_CACHE = os.path.join(OUT_DIR, "venue_codes.json")


def venue_map_from_api(need_names=None):
    """会場名 -> 会場コード を API から作る。

    engine は pandas/bs4 などを要求するため、環境によっては読み込めない。
    必要なのは会場名とコードの対応だけで、それは venues[] に入っている。

    ただし venues[] はその応答に含まれる範囲しか返さないことがあり、
    1レース引いただけでは全43会場が揃わない。
    実際 31件しか集まらず、11会場が丸ごと欠けた。
    そこで日を変えながら足し合わせ、必要な会場が揃うまで続ける。
    一度作ったら保存して、次回からは通信しない。
    """
    m = {}
    if os.path.exists(VENUE_MAP_CACHE):
        try:
            f = open(VENUE_MAP_CACHE, "r", encoding="utf-8")
            m = json.load(f) or {}
            f.close()
        except Exception:
            m = {}
    if m:
        lack = [nm for nm in (need_names or []) if nm not in m]
        if not lack:
            print("[find] 会場コード: 保存済みを使用 (%d件)" % len(m))
            return m
        print("[find] 保存済みに足りない会場 %d件 → 追加で集めます" % len(lack))

    print("[find] 会場コードを API から集めます…")
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y%m%d")
    n_hit = 0
    back = 0
    # 開催は日によって偏るので、日を散らして集める。
    while back <= 60:
        d = date_minus(today, back)
        got_today = False
        for i in range(1, 100):
            code = "%02d" % i
            if code in m.values():
                continue          # そのコードは既に分かっている
            data = http_json(race_url(d + code, 1, 1))
            if not (isinstance(data, dict) and data.get("venues")):
                continue
            got_today = True
            for v in data.get("venues"):
                nm = str(v.get("name", "") or "")
                vid = str(v.get("id", "") or "")
                if nm and vid and nm not in m:
                    m[nm] = vid
                    n_hit = n_hit + 1
            break             # 1日1回当たれば venues[] は同じ
        if got_today:
            time.sleep(SLEEP)
        lack = [nm for nm in (need_names or []) if nm not in m]
        if need_names and not lack:
            break
        back = back + 3       # 日を散らす (連続日は同じ開催になりやすい)

    if m:
        try:
            if not os.path.isdir(OUT_DIR):
                os.makedirs(OUT_DIR)
            f = open(VENUE_MAP_CACHE, "w", encoding="utf-8")
            json.dump(m, f, ensure_ascii=False, indent=1)
            f.close()
        except Exception:
            pass
        print("[find] 会場コード: %d件 (新規 %d)" % (len(m), n_hit))
    return m or None


def venue_code_map(need_names=None):
    """会場名 -> 会場コード。

    engine が読めればそれを使い、駄目なら API から作る。
    engine は pandas を要求することがあり、Actions では落ちる。

    さらに、engine が読めても CODES が足りないことがある。
    その場合に黙って会場を飛ばすと、その会場のレースが丸ごと欠ける。
    必要な会場名(need_names)を渡してもらい、
    1つでも欠けていれば API 側で補う。
    """
    m = {}
    eng = load_engine()
    if eng is not None:
        try:
            for pc in eng.CODES:
                m[eng.CODES[pc]] = pc
            if m:
                print("[find] 会場コード: engine から %d件" % len(m))
        except Exception:
            m = {}

    missing = []
    if need_names:
        for nm in need_names:
            if nm not in m:
                missing.append(nm)

    if (not m) or missing:
        if missing:
            print("[find] engine に無い会場: " + ", ".join(missing[:8]))
        api = venue_map_from_api(need_names)
        if api:
            for nm in api:
                if nm not in m:
                    m[nm] = api[nm]
            print("[find] 会場コード: 合計 %d件" % len(m))

    return m or None


# ============================================================
# 通信
# ============================================================
def http_json(url):
    for _ in range(RETRY):
        if HAS_REQUESTS:
            try:
                r = requests.get(url, headers=HEADERS, timeout=25)
                if r.status_code == 200:
                    try:
                        return r.json()
                    except Exception:
                        return None
                if r.status_code in (403, 404):
                    return None
            except Exception:
                pass
        else:
            try:
                from urllib.request import Request, urlopen
            except Exception:
                from urllib2 import Request, urlopen
            try:
                f = urlopen(Request(url, headers=HEADERS), timeout=25)
                raw = f.read()
                f.close()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                return json.loads(raw)
            except Exception:
                pass
        time.sleep(SLEEP * 3)
    return None


def date_minus(d, n):
    return (datetime.strptime(d, "%Y%m%d") - timedelta(days=n)).strftime("%Y%m%d")


def date_plus(d, n):
    return date_minus(d, -n)


def race_url(cup_id, day, rno):
    return (API_ROOT + "/cups/" + cup_id + "/schedules/" + str(day)
            + "/races/" + str(int(rno)) + "?pf=web")


def resolve_cup_day(venue_code, date_str, sample_rno):
    """開催初日を遡って cupId と day を突き止める"""
    back = 0
    while back <= MAX_LOOKBACK:
        cup_id = date_minus(date_str, back) + venue_code
        data = http_json(race_url(cup_id, back + 1, sample_rno))
        if isinstance(data, dict) and data.get("entries"):
            return (cup_id, back + 1, data)
        time.sleep(SLEEP)
        back = back + 1
    return (None, None, None)


# ============================================================
# 取り出し
# ============================================================
def grade_of(num):
    try:
        return GRADE_NUM.get(int(num), "")
    except Exception:
        return ""


def player_class_of(cls, grp):
    try:
        ch = CLASS_NUM.get(int(cls), "")
    except Exception:
        return ""
    if not ch:
        return ""
    try:
        g = int(grp)
    except Exception:
        return ch
    return ch + str(g)


# 発走時刻は日本時間で出す。
#   fromtimestamp は実行環境のタイムゾーンに従うので、
#   GitHub Actions (UTC) で回すと9時間ずれる。
#   ミッドナイト開催が昼に見えてしまうため、JSTに固定する。
JST = timezone(timedelta(hours=9))


def day_label_of(day, duration):
    """何日目か。初日 / N日目 / 最終日。統合DBは空のことがあるのでAPIから作る。"""
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


def build_race_kind(cls, rtype):
    """「Ａ級一般」のような表記。級が raceType に含まれていれば足さない。"""
    c = str(cls or "").strip()
    t = str(rtype or "").strip()
    if not t:
        return c
    for w in ("チャレンジ", "ガールズ", "Ｓ級", "S級", "Ａ級", "A級", "Ｌ級", "L級"):
        if w in t:
            return t
    return (c + t) if c else t


def hhmm(ts):
    try:
        return datetime.fromtimestamp(int(ts), JST).strftime("%H:%M")
    except Exception:
        return ""


def zone_of(labels, start_at):
    """時間区分。labels で決め、付かなければ発走時刻で補う。"""
    for v in (labels or []):
        try:
            z = LABEL_ZONE.get(int(v))
        except Exception:
            z = None
        if z:
            return z
    if labels is not None and len(labels) == 0:
        return "デイ"
    # labels に 3/4 しか無い場合など。発走時刻で決める。
    t = hhmm(start_at)
    if not t:
        return ""
    h = int(t[:2])
    if h < 11:
        return "モーニング"
    if h < 16:
        return "デイ"
    if h < 20:
        return "ナイター"
    return "ミッドナイト"


def rate_block(src):
    """EXデータの {first,second,third,others,total,*Percentage} をそのまま"""
    if not isinstance(src, dict):
        return None
    out = {}
    for k in ("first", "second", "third", "others", "total",
              "firstPercentage", "secondPercentage",
              "thirdPercentage", "othersPercentage"):
        if k in src:
            out[k] = src.get(k)
    return out if out else None


def ex_block(src):
    if not isinstance(src, dict):
        return None
    return {"total": src.get("total"),
            "succeeded": src.get("succeeded"),
            "percentage": src.get("percentage")}


def build_payouts(data):
    """全券種の的中目・人気・払戻金。
    的中目は *WinningOddsIds、金額と人気はオッズ配列から引く。"""
    out = {}
    for key, name, widkey in BET_KINDS:
        wids = data.get(widkey) or []
        arr = data.get(key) or []
        byid = {}
        for o in arr:
            byid[str(o.get("id"))] = o
        hits = []
        for wid in wids:
            o = byid.get(str(wid))
            if not o:
                continue
            hits.append({
                "combo": "-".join(str(x) for x in (o.get("key") or [])),
                "popularity": o.get("popularityOrder"),
                "payout": o.get("payoffUnitPrice"),
                "odds": o.get("oddsStr"),
            })
        out[name] = hits
    # 枠単・枠複は成立しないので「−」を明示しておく
    out["枠単"] = None
    out["枠複"] = None
    return out


def build_players(data):
    """車番をキーにした選手情報"""
    players = {}
    pl_by_id = {}
    for p in (data.get("players") or []):
        pl_by_id[str(p.get("id"))] = p

    entries = data.get("entries") or []
    records = data.get("records") or []
    rec_by_pid = {}
    for r in records:
        rec_by_pid[str(r.get("playerId"))] = r

    # 対戦成績: 自分 -> 相手 -> 勝敗
    comp = {}
    for c in (data.get("competitionRecords") or []):
        a = str(c.get("playerId"))
        b = str(c.get("opponentId"))
        comp.setdefault(a, {})[b] = {"wins": c.get("wins"),
                                     "losses": c.get("losses")}

    pid_to_bike = {}
    for e in entries:
        bike = e.get("number")
        if bike is None:
            bike = e.get("bracketNumber")
        if bike is not None:
            pid_to_bike[str(e.get("playerId"))] = int(bike)

    for e in entries:
        bike = e.get("number")
        if bike is None:
            bike = e.get("bracketNumber")
        if bike is None:
            continue
        pid = str(e.get("playerId"))
        p = pl_by_id.get(pid) or {}
        r = rec_by_pid.get(pid) or {}

        # H はデータがある選手だけ数値。無ければ None (画面の「−」)
        home = r.get("home") if r.get("hasHome") is True else None

        item = {
            "bike": int(bike),
            "player_id": pid,
            "name": p.get("name"),
            "yomi": p.get("yomi"),
            "prefecture": p.get("prefecture"),   # 所属 = 府県
            "term": p.get("term"),               # 期
            "age": p.get("age"),
            "birthday": p.get("birthday"),
            "gender": p.get("gender"),
            "class_group": player_class_of(e.get("playerCurrentTermClass"),
                                           e.get("playerCurrentTermGroup")),
            "class_group_prev": player_class_of(
                e.get("playerPreviousTermClass"),
                e.get("playerPreviousTermGroup")),
            "absent": e.get("absent"),

            "race_point": r.get("racePoint"),
            "gear_ratio": r.get("gearRatio"),
            "style": r.get("style"),             # 脚質
            "comment": r.get("comment"),         # 前検コメント

            "s": r.get("standing"),
            "h": home,
            "b": r.get("back"),

            # 決まり手内訳
            "kimarite": {
                "逃": r.get("frontRunner"),
                "捲": r.get("stalker"),
                "差": r.get("deepCloser"),
                "マ": r.get("marker"),
            },
            # 着別度数
            "chakubetsu": {
                "1着": r.get("first"), "2着": r.get("second"),
                "3着": r.get("third"), "着外": r.get("others"),
            },
            "win_rate": r.get("firstRate"),
            "place2_rate": r.get("secondRate"),
            "place3_rate": r.get("thirdRate"),

            # 直近・当場所
            "latest_cup_results": r.get("latestCupResults"),
            "latest_venue_results": r.get("latestVenueResults"),
            "current_cup_results": r.get("currentCupResults"),
            "previous_cup_results": r.get("previousCupResults"),
            "previous_cup_id": r.get("previousCupId"),
        }

        # EXデータ
        ex = {}
        for k, nm in EX_KEYS.items():
            b = ex_block(r.get(k))
            if b:
                ex[nm] = b
        item["ex_senpou"] = ex or None

        pos = {}
        for k, nm in POS_KEYS.items():
            b = rate_block(r.get(k))
            if b:
                pos[nm] = b
        item["ex_ichi"] = pos or None

        rt = {}
        for k, nm in RTYPE_KEYS.items():
            b = rate_block(r.get(k))
            if b:
                rt[nm] = b
        item["ex_shubetsu"] = rt or None

        tr = {}
        for k, nm in TRACK_KEYS.items():
            b = rate_block(r.get(k))
            if b:
                tr[nm] = b
        item["ex_shuchou"] = tr or None

        we = {}
        for k, nm in WEATHER_KEYS.items():
            b = rate_block(r.get(k))
            if b:
                we[nm] = b
        item["ex_tenkou"] = we or None

        hr = {}
        for k, nm in HOUR_KEYS.items():
            b = rate_block(r.get(k))
            if b:
                hr[nm] = b
        item["ex_jikantai"] = hr or None

        # 対戦成績 (相手を車番で持つ)
        vs = {}
        for opid, wl in (comp.get(pid) or {}).items():
            ob = pid_to_bike.get(opid)
            if ob is not None:
                vs[str(ob)] = wl
        item["vs"] = vs or None

        players[str(int(bike))] = item
    return players


def build_race(data, db_rec, place, date_str):
    """1レース分をまとめる"""
    race = data.get("race") or {}
    sch = data.get("schedule") or {}
    cid = str(sch.get("cupId", "") or "")
    cup = None
    for c in (data.get("cups") or []):
        if str(c.get("id", "")) == cid:
            cup = c
            break
    cup = cup or {}
    labels = cup.get("labels")

    lp = data.get("linePrediction") or {}
    lines = []
    for ln in (lp.get("lines") or []):
        grp = []
        for ent in (ln.get("entries") or []):
            for n in (ent.get("numbers") or []):
                grp.append(n)
        if grp:
            lines.append(grp)

    out = {
        "race_id": race.get("id"),
        "date": date_str,
        "venue": place,
        "race_no": race.get("number"),
        "cup_id": cid,
        "cup_name": cup.get("name"),
        "day": sch.get("day"),
        "day_label": day_label_of(sch.get("day"), cup.get("duration")),
        "grade": grade_of(cup.get("grade")),
        "cup_labels": labels,          # 生の数値も残す (3/4の意味が後で分かるように)
        "time_zone": zone_of(labels, race.get("startAt")),
        "post_time": hhmm(race.get("startAt")),
        "close_time": hhmm(race.get("closeAt")),
        "distance": race.get("distance"),
        "lap_count": race.get("lap"),
        "entries_number": race.get("entriesNumber"),
        "race_class": race.get("class"),
        "race_type": race.get("raceType"),
        "race_kind": build_race_kind(race.get("class"), race.get("raceType")),
        "advancement": race.get("advancementConditionText"),
        "weather": race.get("weather"),
        "wind_speed": race.get("windSpeed"),
        "status": race.get("status"),
        "cancel": race.get("cancel"),

        "line_prediction": {"type": lp.get("lineType"), "lines": lines},
        "line_db": (db_rec or {}).get("line"),

        "payouts": build_payouts(data),

        # 統合DBから: 着順・決まり手・着差・隊列
        "result": (db_rec or {}).get("result"),
        "lap": (db_rec or {}).get("lap"),

        "players": build_players(data),
    }
    return out


def build_venues(data):
    """会場マスタ。毎レース同じものが付いてくるので初回だけ残す。"""
    out = {}
    for v in (data.get("venues") or []):
        out[str(v.get("name"))] = {
            "id": v.get("id"),
            "name": v.get("name"),
            "address": v.get("address"),
            "track_distance": v.get("trackDistance"),
            "straight_distance": v.get("trackStraightDistance"),
            "angle_center": v.get("trackAngleCenter"),
            "angle_straight": v.get("trackAngleStraight"),
            "home_width": v.get("homeWidth"),
            "back_width": v.get("backWidth"),
            "center_width": v.get("centerWidth"),
            "bank_feature": v.get("bankFeature"),
            "factors": v.get("factors"),
            "best_record": v.get("bestRecord"),
        }
    return out


# ============================================================
# 本体
# ============================================================
def load_db(path):
    """日付 -> 会場 -> レース番号 -> レコード と、会場名 -> 会場コード。

    会場コードは統合DBの race_id の先頭2桁に入っている。
    それがウィンチケットの venues[].id と完全に一致することを、
    34会場ぶん突き合わせて確かめた (不一致ゼロ)。
    したがって engine も通信も要らない。
    """
    idx = {}
    codes = {}
    n = 0
    f = open(path, "r", encoding="utf-8")
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        n = n + 1
        d = str(r.get("date", "") or "")
        if len(d) != 8:
            continue
        p = str(r.get("place", "") or "")
        try:
            rno = int(r.get("race_no"))
        except Exception:
            continue
        idx.setdefault(d, {}).setdefault(p, {})[rno] = r
        if p and p not in codes:
            rid = str(r.get("race_id", "") or "")
            if len(rid) >= 10 and rid[:2].isdigit():
                codes[p] = rid[:2]
    f.close()
    return (idx, n, codes)


def main():
    print("=" * 56)
    print("BVDB 構築")
    print("=" * 56)

    dbp = find_db()
    if not dbp:
        print("統合DBが見つかりません。")
        return 1
    print("[find] 統合DB: " + dbp)

    idx, nrow, n2c = load_db(dbp)
    days = sorted(idx)
    print("[find] 会場コード: 統合DBの race_id から %d件" % len(n2c))

    need = set()
    for d in idx:
        for p in idx[d]:
            need.add(p)
    still = [nm for nm in sorted(need) if nm not in n2c]
    if still:
        # DBに race_id の無いレコードがある場合の保険。
        print("[find] DBから引けない会場 %d件 → APIで補います: %s"
              % (len(still), ", ".join(still[:8])))
        api = venue_map_from_api(still)
        for nm in (api or {}):
            if nm not in n2c:
                n2c[nm] = api[nm]
        still = [nm for nm in sorted(need) if nm not in n2c]
        if still:
            print("[warn] 最後まで解決できない会場: " + ", ".join(still))
    if not n2c:
        print("会場コードが得られません。")
        return 1
    print("[db] %d行 / %d日ぶん (%s 〜 %s)"
          % (nrow, len(days), days[0], days[-1]))

    d_from = os.environ.get("BVDB_FROM", "").strip() or DEFAULT_FROM
    d_to = os.environ.get("BVDB_TO", "").strip() or days[-1]
    targets = [d for d in days if d_from <= d <= d_to]
    if not targets:
        print("対象日がありません (%s 〜 %s)" % (d_from, d_to))
        return 1
    print("[range] %s 〜 %s  %d日" % (targets[0], targets[-1], len(targets)))

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)

    venues_path = os.path.join(OUT_DIR, "venues.json")
    have_venues = os.path.exists(venues_path)

    t0 = time.time()
    n_req = 0
    n_race = 0
    n_day = 0

    for d in targets:
        out_path = os.path.join(OUT_DIR, "BVDB_" + d + ".json")
        if os.path.exists(out_path):
            print("[skip] %s (取得済み)" % d)
            continue

        races = []
        for place in sorted(idx[d]):
            code = n2c.get(place)
            if not code:
                print("  [warn] %s 会場コード不明: %s" % (d, place))
                continue
            rnos = sorted(idx[d][place])
            if not rnos:
                continue

            cup_id, day, first = resolve_cup_day(code, d, rnos[0])
            n_req = n_req + 1
            if not cup_id:
                print("  [warn] %s %s cupId解決できず" % (d, place))
                continue

            if not have_venues:
                vs = build_venues(first)
                if vs:
                    f = open(venues_path, "w", encoding="utf-8")
                    json.dump(vs, f, ensure_ascii=False, indent=1)
                    f.close()
                    have_venues = True
                    print("[venues] %d会場ぶん保存" % len(vs))

            got = 0
            for rno in rnos:
                if rno == rnos[0]:
                    data = first
                else:
                    data = http_json(race_url(cup_id, day, rno))
                    n_req = n_req + 1
                    time.sleep(SLEEP)
                if not isinstance(data, dict) or not data.get("entries"):
                    continue
                try:
                    races.append(build_race(data, idx[d][place].get(rno),
                                            place, d))
                    got = got + 1
                except Exception as e:
                    print("  [warn] %s %s %dR 組み立て失敗: %s"
                          % (d, place, rno, str(e)[:50]))
            print("  %s %-6s %2d/%2dR" % (d, place, got, len(rnos)))
            n_race = n_race + got

        body = {"date": d, "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "n_races": len(races), "races": races}
        f = open(out_path, "w", encoding="utf-8")
        json.dump(body, f, ensure_ascii=False)
        f.close()
        n_day = n_day + 1
        el = time.time() - t0
        print("[done] %s  %dR  (通信 %d / 経過 %.0f分)"
              % (d, len(races), n_req, el / 60.0))

    print("")
    print("=" * 56)
    print("おわり: %d日 / %dレース / 通信 %d回 / %.1f分"
          % (n_day, n_race, n_req, (time.time() - t0) / 60.0))

    if os.environ.get("BVDB_MERGE", "").strip() == "1":
        merge()
    print("=" * 56)
    return 0


def merge():
    """日別ファイルを1つにまとめる"""
    import glob
    files = sorted(glob.glob(os.path.join(OUT_DIR, "BVDB_*.json")))
    allr = []
    for fp in files:
        try:
            f = open(fp, "r", encoding="utf-8")
            b = json.load(f)
            f.close()
        except Exception:
            continue
        allr.extend(b.get("races") or [])
    out = os.path.join(OUT_DIR, "BVDB.json")
    f = open(out, "w", encoding="utf-8")
    json.dump({"generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "n_races": len(allr), "races": allr},
              f, ensure_ascii=False)
    f.close()
    print("[merge] %d日 %dレース -> %s" % (len(files), len(allr), out))


if __name__ == "__main__":
    sys.exit(main())

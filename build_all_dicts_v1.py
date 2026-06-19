"""
build_all_dicts_v1.py  (5辞書 統合生成・自動配置)

予想に必要な5辞書を、正しい生成元スクリプトで作り、アプリが読む正しい名前・
場所に自動配置する。環境変数の手打ちもリネームも不要。▶を押すだけ。

5辞書と生成元:
  profiles      → dicts_build_FINAL.py            (--only profiles)
  line_lead     → dicts_build_FINAL.py            (--only line_lead)
  rawscore      → build_rawscore_pattern_stats_v6_cutoff.py
  kimari        → build_kimari_stats_v3_cutoff.py
  rsrank        → build_player_rsrank_finish_v3_cutoff.py

アプリが読む配置 (dicts/ 直下、FINAL命名):
  player_profiles_FINAL/                     (フォルダ)
  player_profile_index_FINAL.json
  player_line_lead_rate_FINAL.json
  rawscore_pattern_stats_FINAL.json
  kimari_stats_FINAL.json
  player_rsrank_finish_7car_FINAL.jsonl
  player_rsrank_finish_9car_FINAL.jsonl      (9車用。あれば)

2つのモード:
  python build_all_dicts_v1.py
      → 通常用。全期間で dicts/ に生成 (今日以降の予想用)。引数なし。

  python build_all_dicts_v1.py --monthly 202601,202602,...
      → 月別walk-forward用。各月の前月末cutoffで
         dicts_monthly/<cutoff>/ に5辞書一式を生成。
      → 引数なしの既定は 202601〜202606。

  python build_all_dicts_v1.py --monthly-default
      → 既定の6ヶ月(202601-202606)を月別生成。

依存: 同じフォルダ(takusen/code)に下記5本が必要
  dicts_build_FINAL.py
  build_rawscore_pattern_stats_v6_cutoff.py
  build_kimari_stats_v3_cutoff.py
  build_player_rsrank_finish_v3_cutoff.py

Pydroid3制約: f-string禁止 / for-else禁止 / 完全ファイル提供
"""

import os
import sys
import time
import subprocess

DL = "/storage/emulated/0/Download"

# 生成元スクリプトが実際に在るフォルダを探す。
# Pydroid3では __file__ が一時パスになることがあるため、複数候補から
# 「dicts_build_FINAL.py が存在する」フォルダを CODE_DIR とする。
def _find_code_dir():
    cands = []
    try:
        cands.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    cands.append(os.path.join(DL, "takusen", "code"))
    cands.append(os.path.join(os.getcwd(), "takusen", "code"))
    cands.append(os.getcwd())
    for c in cands:
        if c and os.path.exists(os.path.join(c, "dicts_build_FINAL.py")):
            return c
    # 見つからなければ最初の候補(従来動作)
    return cands[0] if cands else os.getcwd()

CODE_DIR = _find_code_dir()
DATA_DIR = os.path.join(DL, "takusen", "data")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = os.path.join(os.getcwd(), "takusen", "data")
DICTS_DIR = os.path.join(DATA_DIR, "dicts")
MONTHLY_ROOT = os.path.join(DATA_DIR, "dicts_monthly")
STATIC_DIR = os.path.join(DATA_DIR, "static")
DB_PATH = os.path.join(DATA_DIR, "keirin_data_scored_v2.jsonl")

PY = sys.executable if sys.executable else "python"

# 生成元スクリプト
S_FINAL = os.path.join(CODE_DIR, "dicts_build_FINAL.py")
S_RAWSCORE = os.path.join(CODE_DIR, "build_rawscore_pattern_stats_v6_cutoff.py")
S_KIMARI = os.path.join(CODE_DIR, "build_kimari_stats_v3_cutoff.py")
S_RSRANK = os.path.join(CODE_DIR, "build_player_rsrank_finish_v3_cutoff.py")

DEFAULT_MONTHS = ["202601", "202602", "202603", "202604", "202605", "202606"]


def prev_month_end(yyyymm):
    y = int(yyyymm[:4])
    m = int(yyyymm[4:6])
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


def run_script(path, env_extra, args=None):
    """生成スクリプトを環境変数付きで実行。成功でTrue"""
    env = dict(os.environ)
    for k in env_extra:
        env[k] = env_extra[k]
    cmd = [PY, path]
    if args:
        for a in args:
            cmd.append(a)
    try:
        r = subprocess.run(cmd, env=env, cwd=CODE_DIR,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        ok = (r.returncode == 0)
        if not ok:
            sys.stdout.write("  [警告] " + os.path.basename(path)
                             + " 終了コード " + str(r.returncode) + "\n")
            tail = r.stdout.decode("utf-8", "ignore")[-400:] if r.stdout else ""
            if tail:
                sys.stdout.write("  --- 末尾ログ ---\n  " + tail.replace("\n", "\n  ")
                                 + "\n")
        return ok
    except Exception as e:
        sys.stdout.write("  [エラー] " + os.path.basename(path) + ": "
                         + str(e)[:120] + "\n")
        return False


def build_one_set(out_dir, cutoff, label):
    """指定フォルダに5辞書一式を生成。cutoff='' なら全期間"""
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    t0 = time.time()
    print("------------------------------------------------")
    print(" 生成セット: " + label)
    print(" 出力: " + out_dir)
    print(" cutoff: " + (cutoff if cutoff else "(全期間)"))
    print("------------------------------------------------")

    cut_env = {}
    if cutoff:
        cut_env["KEIRIN_CUTOFF"] = cutoff

    # 1. profiles + line_lead (dicts_build_FINAL)
    print(" [1/5,2/5] profiles + line_lead ...")
    args = []
    if cutoff:
        args = ["--cutoff", cutoff, "--out", out_dir]
    else:
        args = ["--out", out_dir]
    run_script(S_FINAL, {}, args + ["--only", "profiles"])
    run_script(S_FINAL, {}, args + ["--only", "line_lead"])

    # 2. rawscore
    print(" [3/5] rawscore_pattern_stats ...")
    env = dict(cut_env)
    env["KEIRIN_OUT"] = os.path.join(out_dir, "rawscore_pattern_stats_FINAL.json")
    run_script(S_RAWSCORE, env)

    # 3. kimari
    print(" [4/5] kimari_stats ...")
    env = dict(cut_env)
    env["KEIRIN_OUT"] = os.path.join(out_dir, "kimari_stats_FINAL.json")
    run_script(S_KIMARI, env)

    # 4. rsrank (7car/9car) → FINAL名にリネーム配置
    print(" [5/5] rsrank_finish ...")
    env = dict(cut_env)
    env["KEIRIN_OUT_DIR"] = out_dir
    run_script(S_RSRANK, env)
    # FINALなし → FINALありにリネーム
    _rename(os.path.join(out_dir, "player_rsrank_finish_7car.jsonl"),
            os.path.join(out_dir, "player_rsrank_finish_7car_FINAL.jsonl"))
    _rename(os.path.join(out_dir, "player_rsrank_finish_9car.jsonl"),
            os.path.join(out_dir, "player_rsrank_finish_9car_FINAL.jsonl"))

    print(" セット完了 (" + str(int(time.time() - t0)) + "秒)")
    _verify_set(out_dir)


def _rename(src, dst):
    if os.path.exists(src):
        try:
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(src, dst)
        except Exception as e:
            sys.stdout.write("  [警告] リネーム失敗 "
                             + os.path.basename(src) + ": " + str(e)[:80] + "\n")


def _verify_set(out_dir):
    """5辞書が揃ったか確認"""
    checks = [
        ("player_profiles_FINAL", True),
        ("player_profile_index_FINAL.json", False),
        ("player_line_lead_rate_FINAL.json", False),
        ("rawscore_pattern_stats_FINAL.json", False),
        ("kimari_stats_FINAL.json", False),
        ("player_rsrank_finish_7car_FINAL.jsonl", False),
    ]
    missing = []
    for name, is_dir in checks:
        p = os.path.join(out_dir, name)
        ok = os.path.isdir(p) if is_dir else os.path.exists(p)
        if not ok:
            missing.append(name)
    if missing:
        print("  [!] 不足: " + ", ".join(missing))
    else:
        print("  [OK] 5辞書すべて揃いました")


def parse_args(argv):
    opt = {"mode": "normal", "months": None}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--monthly" and i + 1 < len(argv):
            opt["mode"] = "monthly"
            opt["months"] = [x.strip() for x in argv[i + 1].split(",")
                             if x.strip()]
            i = i + 2
            continue
        if a == "--monthly-default":
            opt["mode"] = "monthly"
            opt["months"] = list(DEFAULT_MONTHS)
            i = i + 1
            continue
        i = i + 1
    return opt


def main():
    opt = parse_args(sys.argv[1:])
    print("================================================")
    print(" build_all_dicts_v1  (5辞書 統合生成)")
    print(" DB    : " + DB_PATH)
    print(" dicts : " + DICTS_DIR)
    print("================================================")

    # 生成元スクリプトの存在チェック
    need = [S_FINAL, S_RAWSCORE, S_KIMARI, S_RSRANK]
    miss = []
    for s in need:
        if not os.path.exists(s):
            miss.append(os.path.basename(s))
    if miss:
        print("[エラー] 生成元スクリプトが不足: " + ", ".join(miss))
        print("  探したフォルダ(CODE_DIR): " + CODE_DIR)
        print("  このフォルダに下記4本を置いてください:")
        print("    dicts_build_FINAL.py")
        print("    build_rawscore_pattern_stats_v6_cutoff.py")
        print("    build_kimari_stats_v3_cutoff.py")
        print("    build_player_rsrank_finish_v3_cutoff.py")
        # 参考: CODE_DIRに実在する.pyを列挙
        try:
            here = [x for x in os.listdir(CODE_DIR) if x.endswith(".py")]
            print("  CODE_DIR内の.pyファイル: " + ", ".join(here[:20]))
        except Exception:
            pass
        return
    if not os.path.exists(DB_PATH):
        print("[エラー] DBが見つかりません: " + DB_PATH)
        return

    if opt["mode"] == "normal":
        # 通常用: 全期間で dicts/ に生成
        print("モード: 通常 (全期間 → dicts/、今日以降の予想用)")
        build_one_set(DICTS_DIR, "", "通常dicts (全期間)")
        print("")
        print("完了。アプリを再起動すると新しい辞書で予想します。")
    else:
        months = opt["months"]
        print("モード: 月別walk-forward")
        print("対象月: " + ", ".join(months))
        if not os.path.isdir(MONTHLY_ROOT):
            os.makedirs(MONTHLY_ROOT)
        done = {}
        for ym in months:
            cutoff = prev_month_end(ym)
            if cutoff in done:
                print(ym + " → cutoff " + cutoff + " (生成済み、共用)")
                continue
            out_dir = os.path.join(MONTHLY_ROOT, cutoff)
            build_one_set(out_dir, cutoff, ym + " 用 (cutoff " + cutoff + ")")
            done[cutoff] = out_dir
        print("")
        print("================================================")
        print(" 月別生成 完了。cutoffフォルダ:")
        for c in sorted(done.keys()):
            print("   " + MONTHLY_ROOT + "/" + c)
        print("================================================")


if __name__ == "__main__":
    main()

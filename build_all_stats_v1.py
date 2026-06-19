# -*- coding: utf-8 -*-
"""
build_all_stats_v1.py  統計一括生成ランナー

dicts/ に必要な全統計を1コマンドで最新化する。内部で以下を順に実行:
  1. build_all_dicts_v1.py        (系統1: 予測用5辞書 profiles/line_lead/rawscore/kimari_stats/rsrank)
  2. build_kimari_player_stats_v3.py (系統2: 決まり手 kimari_player_role_FINAL)
  3. build_ana_score_v2.py        (系統2: 穴スコア ana_score_venue/global_FINAL)

各スクリプトはサブプロセスで実行 (変数衝突を避ける)。
環境変数 (KEIRIN_DB 等) は子プロセスに引き継がれる。

使い方 (Pydroid3で ▶ / Actionsで python build_all_stats_v1.py):
  python build_all_stats_v1.py
  python build_all_stats_v1.py --monthly-default   (系統1の月別辞書も生成。月初向け)
  KEIRIN_SCRIPT_DIR=/path/to/scripts python build_all_stats_v1.py  (スクリプト置き場を明示)

スクリプト探索順: KEIRIN_SCRIPT_DIR → このファイルと同じフォルダ → cwd → takusen/code

Pydroid3制約: f-string不使用 / for-else不使用
"""
import os
import sys
import time
import subprocess

DOWNLOAD_DIR = "/storage/emulated/0/Download"

# このランナー自身の場所
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))

# 子スクリプトの探索ディレクトリ候補
_SCRIPT_DIRS = []
_env_sdir = os.environ.get("KEIRIN_SCRIPT_DIR", "").strip()
if _env_sdir:
    _SCRIPT_DIRS.append(_env_sdir)
_SCRIPT_DIRS.append(_SELF_DIR)
_SCRIPT_DIRS.append(os.getcwd())
# 実際のスクリプト置き場 (ユーザー環境)
_SCRIPT_DIRS.append(os.path.join(DOWNLOAD_DIR, "takusen", "code", "FINAL"))
_SCRIPT_DIRS.append(os.path.join(DOWNLOAD_DIR, "takusen", "code"))


def find_script(name):
    """子スクリプトのフルパスを探す。見つからなければ None。"""
    di = 0
    while di < len(_SCRIPT_DIRS):
        d = _SCRIPT_DIRS[di]
        di = di + 1
        if not d:
            continue
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


# 実行する子スクリプト (順番が重要: 系統1 → 決まり手 → 穴スコア)
# 各要素: (表示名, ファイル名, 追加引数リスト)
PASS_MONTHLY = "--monthly-default" in sys.argv[1:]

STEPS = [
    ("系統1: 予測用5辞書", "build_all_dicts_v1.py",
     (["--monthly-default"] if PASS_MONTHLY else [])),
    ("系統2: 決まり手", "build_kimari_player_stats_v3.py", []),
    ("系統2: 穴スコア", "build_ana_score_v2.py", []),
]


def run_step(label, script_name, extra_args):
    path = find_script(script_name)
    print("")
    print("=" * 64)
    print("  [STEP] " + label + "  (" + script_name + ")")
    print("=" * 64)
    if path is None:
        print("  [SKIP] スクリプトが見つかりません: " + script_name)
        print("    探索した場所:")
        di = 0
        while di < len(_SCRIPT_DIRS):
            print("      - " + str(_SCRIPT_DIRS[di]))
            di = di + 1
        return ("skip", 0.0)
    cmd = [sys.executable, path]
    ai = 0
    while ai < len(extra_args):
        cmd.append(extra_args[ai])
        ai = ai + 1
    t0 = time.time()
    try:
        # 子プロセスの出力をキャプチャしつつ親にも表示
        proc = subprocess.Popen(cmd, env=os.environ.copy(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        captured = []
        while True:
            ln = proc.stdout.readline()
            if not ln:
                break
            try:
                txt = ln.decode("utf-8", "replace")
            except Exception:
                txt = str(ln)
            sys.stdout.write(txt)
            captured.append(txt)
        proc.wait()
        ret = proc.returncode
    except Exception as e:
        print("  [ERROR] 実行例外: " + str(e)[:120])
        return ("error", time.time() - t0)
    dt = time.time() - t0
    # 終了コードが0でも、出力にエラー兆候があれば失敗扱い
    joined = "".join(captured)
    bad_signs = ["[エラー]", "[ERROR]", "生成元スクリプトが不足",
                 "not found", "Traceback", "見つかりません"]
    has_bad = False
    bi = 0
    while bi < len(bad_signs):
        if bad_signs[bi] in joined:
            has_bad = True
        bi = bi + 1
    if ret == 0 and not has_bad:
        return ("ok", dt)
    if ret == 0 and has_bad:
        return ("warn(出力にエラー兆候)", dt)
    return ("fail(rc=" + str(ret) + ")", dt)


def main():
    print("#" * 64)
    print("#  build_all_stats_v1.py  統計一括生成")
    print("#  月別辞書も生成: " + ("はい" if PASS_MONTHLY else "いいえ"))
    print("#" * 64)

    results = []
    t_all = time.time()
    si = 0
    while si < len(STEPS):
        label, script_name, extra_args = STEPS[si]
        si = si + 1
        status, dt = run_step(label, script_name, extra_args)
        results.append((label, status, dt))

    total_dt = time.time() - t_all
    print("")
    print("#" * 64)
    print("#  完了サマリ")
    print("#" * 64)
    ok_count = 0
    ri = 0
    while ri < len(results):
        label, status, dt = results[ri]
        ri = ri + 1
        mark = "OK " if status == "ok" else "!! "
        if status == "ok":
            ok_count = ok_count + 1
        print("  " + mark + label + " : " + status
              + " (" + str(round(dt, 1)) + "秒)")
    print("")
    print("  成功: " + str(ok_count) + "/" + str(len(results))
          + " / 総時間: " + str(round(total_dt, 1)) + "秒")
    # 全成功でなければ非ゼロで終了 (Actionsで失敗検知できるように)
    if ok_count < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()

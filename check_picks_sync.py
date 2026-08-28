"""出走表と買い目の食い違いを検める。

ミッドナイトはラインの掲載が遅い。朝に作った買い目は
「ライン情報なし」で飛ばした状態で残るため、あとから
出走表にラインが入っても、買い目を作り直さないかぎり
アプリの画面には「ライン情報なし」が出続ける。

そうなると取得側の不具合に見えてしまい、原因を取り違える。
実際に 8/28 の玉野で起きた。出走表にはラインが12レース分
入っていたのに、買い目は 09:14 のまま作り直されておらず、
画面だけが古い状態を映していた。

このスクリプトは、その食い違いを実行のたびに声に出す。
    ラインがあるのに買い目が飛ばしている  → error
    買い目そのものが無い                  → warning

使い方: python3 check_picks_sync.py YYYYMMDD
"""

import json
import os
import sys


def load_json(path):
    f = open(path, "r", encoding="utf-8")
    try:
        return json.load(f)
    finally:
        f.close()


def main():
    if len(sys.argv) < 2:
        print("使い方: python3 check_picks_sync.py YYYYMMDD")
        return 0

    date_str = sys.argv[1].strip()
    races_path = os.path.join("today_cache", "races_" + date_str + ".json")
    picks_path = os.path.join("picks", date_str + ".json")

    if not os.path.exists(races_path):
        print("[chk] " + date_str + " 出走表なし")
        return 0
    if not os.path.exists(picks_path):
        print("::warning::[chk] " + date_str + " 買い目が作られていない")
        return 0

    try:
        races = load_json(races_path)
        body = load_json(picks_path)
    except Exception as e:
        print("::warning::[chk] " + date_str + " 読み取り失敗: " + str(e)[:80])
        return 0

    # レースごとに、ラインと発走時刻が入っているか
    has_line = {}
    n_line = 0
    n_post = 0
    for r in races:
        key = str(r.get("place", "")) + "_" + str(r.get("race_no", ""))
        line = str(r.get("line", "") or "").strip()
        has_line[key] = bool(line)
        if line:
            n_line = n_line + 1
        post = str(r.get("post_time", "") or "").strip()
        if post and "-" not in post:
            n_post = n_post + 1

    n_races = len(body.get("races") or [])
    print("[chk] " + date_str
          + " 出走表 " + str(len(races)) + "R"
          + " (ライン " + str(n_line) + " / 発走 " + str(n_post) + ")"
          + "  買い目 " + str(n_races) + "R"
          + "  作成 " + str(body.get("generated", "?")))

    # ラインがあるのに「ライン情報なし」で飛ばされているもの
    bad = []
    for sk in (body.get("skips") or []):
        if sk.get("reason") != "ライン情報なし":
            continue
        key = str(sk.get("key", ""))
        if has_line.get(key):
            bad.append(key)

    if bad:
        print("::error::[chk] " + date_str
              + " 出走表にはラインがあるのに買い目が飛ばしている: "
              + str(len(bad)) + "件  " + ", ".join(bad[:12]))
        print("::error::買い目の作り直しが走っていない。"
              "この日の picks は古いまま。")
        return 1

    print("[chk] " + date_str + " 食い違いなし")
    return 0


if __name__ == "__main__":
    sys.exit(main())

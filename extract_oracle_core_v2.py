# -*- coding: utf-8 -*-
"""
extract_oracle_core_v2.py -- app本体の埋め込みJSから「集計コア」を抜き出す (v2)

【v2の変更】
v1のSEEDSは v306 時点のもので、v322以降に入った検証A/B/C・EVの
入口関数(__btUnionRace / __btAccumUnion / __btTavernRace /
__btEvalRaceEV / __btAccumEV)を辿れず、静かに欠落していた。
SEEDSに追加し、同種の取りこぼしを検出する点検を足した。

【なぜ要るか】
過去集計の数字(託宣/御告)を作っているのは、app_singlevNNN.py に埋め込まれた
JavaScript である。Python側は出走表と結果を渡すだけで、買い目を計算していない。
そのため「DBも辞書もGitHubにあるのに Actions で集計できない」状態になっていた。

調べたところ、集計に必要な33関数は **DOMに一切触れていない純粋関数** だった。
つまりブラウザは不要で、Node.js があれば動く。GitHub Actions は Node を
標準で使えるので、JSをPythonへ全面移植しなくても自動化できる。

本スクリプトは app本体から必要な関数だけを依存ごと抜き出し、
Nodeから require できる oracle_core.js を生成する。

【使い方】
    python3 extract_oracle_core_v2.py app_singlev327.py oracle_core.js
    node --check oracle_core.js

【重要】
oracle_core.js は自動生成物。手で編集しないこと。
app本体のロジックを変えたら必ず作り直し、node --check を通すこと。
そうしないと「端末の集計」と「Actionsの集計」が静かに食い違う。

制約: f-string 禁止 / for-else 禁止
"""
import os
import re
import sys

# 集計の入口となる関数。ここから依存を辿る。
SEEDS = ["__btEvalRace", "__btEvalOra", "__btAccum", "__btNewAgg",
         "__btOrderedCombos",
         # v322以降の検証A/B/C・EV。旧SEEDSでは辿れず欠落していた。
         "__btUnionRace", "__btAccumUnion", "__btTavernRace",
         "__btEvalRaceEV", "__btAccumEV"]


# 関数の外で定義されている必要な変数
GLOBAL_VARS = ["_BT_AXIS", "_BT_MAXN", "_ORA_AXIS", "_ORA_CONF_N", "_ORA_DBG",
               "_ORA_OMK_AXIS", "_ORA_RATIO_CAP", "_ORA_RIVAL",
               "_ORA_RSR_CONFN", "_RACE"]


def extract_script(src):
    """app本体から <script> ブロックを取り出す。"""
    blocks = re.findall(r'<script>(.*?)</script>', src, re.S)
    if not blocks:
        return ""
    best = ""
    i = 0
    while i < len(blocks):
        if len(blocks[i]) > len(best):
            best = blocks[i]
        i = i + 1
    return best


def index_functions(js):
    """function名 -> 本体テキスト の辞書と、出現順のリストを返す。"""
    funcs = {}
    order = []
    for m in re.finditer(r'function\s+([A-Za-z_$][\w$]*)\s*\(', js):
        name = m.group(1)
        start = m.start()
        i = js.index("{", m.end() - 1)
        depth = 0
        j = i
        while j < len(js):
            ch = js[j]
            if ch == "{":
                depth = depth + 1
            elif ch == "}":
                depth = depth - 1
                if depth == 0:
                    break
            j = j + 1
        funcs[name] = js[start:j + 1]
        order.append(name)
    return (funcs, order)


def closure(funcs, seeds):
    """seedsから呼ばれる関数を推移的に集める。"""
    need = set()
    stack = []
    i = 0
    while i < len(seeds):
        stack.append(seeds[i])
        i = i + 1
    while stack:
        name = stack.pop()
        if name in need:
            continue
        if name not in funcs:
            continue
        need.add(name)
        body = funcs[name]
        for ident in set(re.findall(r'\b([A-Za-z_$][\w$]*)\s*\(', body)):
            if ident in funcs and ident not in need:
                stack.append(ident)
    return need


def collect_globals(js, funcs, need):
    """抽出した関数が実際に参照している「関数の外のvar」を自動で集める。

    手書きのGLOBAL_VARSは v306 時点のもので、v322以降に入った
    _BT_SCOPE / _VF_AXES_A / _VF_AXES_B が抜けていた。
    抜けたまま動かすと未定義参照で落ちるか、最悪は黙って別の値になる。
    人間が名簿を保守するのをやめ、原文から機械的に拾う。
    """
    # 関数の中身を全部消して、残った部分の var 宣言 = トップレベル宣言。
    outer = js
    names = sorted(funcs.keys(), key=lambda n: -len(funcs[n]))
    i = 0
    while i < len(names):
        outer = outer.replace(funcs[names[i]], " ")
        i = i + 1
    decl = {}
    for m in re.finditer(r'\bvar[ \t]+([A-Za-z_$][\w$]*)[ \t]*=[ \t]*([^;\n]+);',
                         outer):
        if m.group(1) not in decl:
            decl[m.group(1)] = m.group(2).strip()

    # 抽出対象の関数が参照している識別子
    body = ""
    for n in sorted(need):
        body = body + funcs[n] + "\n"
    used = set(re.findall(r'[A-Za-z_$][\w$]*', body))

    out = []
    hit = []
    for v in sorted(decl.keys()):
        if v in used and v not in funcs:
            out.append("var " + v + " = " + decl[v] + ";")
            hit.append(v)

    # 手書き名簿にあるのに拾えなかったものは警告する(取りこぼしの最後の砦)
    miss = []
    j = 0
    while j < len(GLOBAL_VARS):
        if GLOBAL_VARS[j] not in hit:
            miss.append(GLOBAL_VARS[j])
        j = j + 1
    return (out, hit, miss)


def find_decls(js):
    """必要なグローバル変数の宣言行を原文から拾う。"""
    out = []
    miss = []
    i = 0
    while i < len(GLOBAL_VARS):
        v = GLOBAL_VARS[i]
        i = i + 1
        # 行末コメント付き / 空白ゆれ に耐えるよう、宣言の値部分だけを拾う。
        m = re.search(r'\bvar[ \t]+' + re.escape(v) + r'[ \t]*=[ \t]*([^;\n]+);',
                      js)
        if m:
            out.append("var " + v + " = " + m.group(1).strip() + ";")
        else:
            out.append("var " + v + " = null;  // 宣言が見つからず暫定")
            miss.append(v)
    return (out, miss)


def main():
    if len(sys.argv) < 3:
        print("使い方: python3 extract_oracle_core.py <app本体.py> <出力.js>")
        return 1
    app_path = sys.argv[1]
    out_path = sys.argv[2]
    if not os.path.exists(app_path):
        print("[error] app本体が見つかりません: " + app_path)
        return 1

    f = open(app_path, "r", encoding="utf-8")
    try:
        src = f.read()
    finally:
        f.close()

    js = extract_script(src)
    if not js:
        print("[error] <script> ブロックが見つかりません")
        return 1

    funcs, order = index_functions(js)
    print("JS内の関数総数: " + str(len(funcs)))

    lost = []
    i = 0
    while i < len(SEEDS):
        if SEEDS[i] not in funcs:
            lost.append(SEEDS[i])
        i = i + 1
    if lost:
        print("[error] 入口の関数が見つかりません: " + ", ".join(lost))
        print("  app側で関数名が変わった可能性がある。SEEDS を直すこと。")
        return 1

    need = closure(funcs, SEEDS)
    print("抽出する関数: " + str(len(need)))

    # DOM依存が混ざっていないか点検 (混ざるとNodeで落ちる)
    dom = {}
    for name in need:
        hits = re.findall(
            r'\b(document|window|getElementById|querySelector|innerHTML'
            r'|alert|localStorage)\b', funcs[name])
        if hits:
            dom[name] = sorted(set(hits))
    if dom:
        print("[warn] DOM依存が見つかりました。Nodeで動きません:")
        for name in dom:
            print("   " + name + " : " + ", ".join(dom[name]))
    else:
        print("DOM依存: なし (Nodeで動く)")

    # 取りこぼし点検。
    # v306用のSEEDSのまま v327 に掛けたとき、検証A/B/C(__btUnionRace 等)が
    # 静かに欠落していた。同じ事故を繰り返さないため、
    # 「1レースを評価する形の関数」で未収録のものを必ず名指しする。
    orphan = []
    i = 0
    while i < len(order):
        name = order[i]
        i = i + 1
        if name in need:
            continue
        if not re.match(r'^__(bt|ora|tav|axis)', name):
            continue
        if not re.search(r'(Race|Accum|Combos|Predict|Expand|Counts)$', name):
            continue
        body = funcs[name]
        if re.search(r'\b(document|window|getElementById|querySelector'
                     r'|innerHTML|alert|localStorage|fetch)\b', body):
            continue
        orphan.append(name)
    if orphan:
        print("")
        print("[warn] ★未収録の評価系関数があります: " + ", ".join(orphan))
        print("  DOM非依存なのにSEEDSから辿れていない。")
        print("  集計に使う関数なら SEEDS に足すこと。")
        print("  放置すると端末とActionsの数字が静かに食い違う。")

    decls, hit, miss = collect_globals(js, funcs, need)
    print("グローバル変数: " + str(len(hit)) + "個 (自動検出)")
    print("  " + ", ".join(hit))
    if miss:
        print("[warn] 名簿にあるが今回は不要/未検出: " + ", ".join(miss))

    lines = []
    lines.append("// oracle_core.js -- app本体から自動抽出した集計コア")
    lines.append("// 生成元: " + os.path.basename(app_path))
    lines.append("// 手で編集しないこと。extract_oracle_core.py で作り直す。")
    lines.append("")
    i = 0
    while i < len(decls):
        lines.append(decls[i])
        i = i + 1
    lines.append("")
    i = 0
    while i < len(order):
        name = order[i]
        i = i + 1
        if name in need:
            lines.append(funcs[name])
            lines.append("")
    names = sorted(need)
    lines.append("module.exports = {" + ", ".join(names)
                 + ", setRace: function(r){ _RACE = r; }"
                 + ", setAxis: function(a){ _BT_AXIS = a; _ORA_OMK_AXIS = a; }"
                 + "};")

    g = open(out_path, "w", encoding="utf-8")
    try:
        g.write("\n".join(lines))
    finally:
        g.close()
    print("")
    print("出力: " + out_path)
    print("次にやること: node --check " + out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

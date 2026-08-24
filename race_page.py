# -*- coding: utf-8 -*-
"""
race_page.py -- 軽量レースページ (v1)

既存の託宣画面には一切手を触れない。新しいURLを足すだけ。
    /race    軽量ページ

【設計の要点: 上位ボタンが下位の読み込みをしない】
  起動        通信ゼロ
  日付を選ぶ  /api/venues 1回だけ (会場もRの一覧もここに入っている)
  会場を押す  通信ゼロ。取得済みデータからRバーを描くだけ
  Rを押す     /api/race でそのレース1件だけ生成 (唯一の重い処理)
  帯メニュー  出走表/分析 = 取得済み / 予想 = その場で計算
              結果 = 開いたときだけ /api/result

既存画面が重いのは、託宣ボタンが build_cache(同期+スクレイピング)を
必ず走らせ、さらに会場を自動選択して refetch_lines まで実行するため。
このページはそれらを一切しない。同期は明示ボタンに分けた。

【予想タブ】
payload から 拮抗度Q と 分戦(ライン本数) を判定し、
conditions.json の稼働条件に一致すれば買い目を出す。
買い目の生成は oracle_core.js の __btUnionRace を使う。
端末の集計と同じ関数なので、表示と検証結果がずれない。

【組み込み方】
app_singlev327.py の末尾 (app.run の手前) に2行:

    import race_page
    race_page.register(app, load_races, get_dicts, build_race_payload)

制約: f-string 禁止 / for-else 禁止
"""
import os
import json
import datetime as _dt

from flask import Response, jsonify, request

# oracle_core.js と conditions.json の探索先
_DIRS = [
    "/data/data/com.termux/files/home/bt",
    "/storage/emulated/0/Download/takusen/data",
    "/storage/emulated/0/Download/takusen",
    "/storage/emulated/0/Download",
    os.getcwd(),
]


def _find(name):
    i = 0
    while i < len(_DIRS):
        p = os.path.join(_DIRS[i], name)
        i = i + 1
        if os.path.exists(p):
            return p
    return ""



# ============================================================
# 予想の該当判定 (軽い処理だけで済ませる)
#
# 拮抗度Q は 出走表の「競走得点」と「直近の着順」だけで計算できる。
#   raw = 競走得点 - 平均着順*5 + グレード補正
# 選別(sieve/verify)で使った式と同じ。
# 予想計算(build_race_payload)を呼ぶ必要がないので、
# 1会場ぶんまとめて判定しても待たされない。
# ============================================================
import re as _re

_FL_BOUNDS = [9.3637, 9.1749, 8.9724, 8.6605]


def _pts(full_info):
    if not full_info or full_info == "未取得":
        return None
    m = _re.search(r'([\d.]+)点$', full_info)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _finishes(h):
    if not h or h == "なし" or not isinstance(h, str):
        return []
    t = h.strip().split()
    if not t:
        return []
    out = []
    for x in _re.split(r'[・.]', t[-1]):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(int(x))
        except Exception:
            pass
    return out


def _grade(h):
    if not h or h == "なし" or not isinstance(h, str):
        return ""
    m = _re.search(r'(GP|G1|G2|G3|F1|F2)', h)
    if m:
        return m.group(1)
    s2 = h
    for k, v in (("Ｇ", "G"), ("Ｆ", "F"), ("１", "1"), ("２", "2"), ("３", "3")):
        s2 = s2.replace(k, v)
    m = _re.search(r'(GP|G1|G2|G3|F1|F2)', s2)
    if m:
        return m.group(1)
    return ""


def _q_of(race):
    players = race.get("players") or {}
    vals = []
    for bk in players:
        p = players[bk]
        if not isinstance(p, dict):
            return ""
        pt = _pts(p.get("full_info", ""))
        if pt is None or pt == 0.0:
            return ""
        ranks = []
        for key in ("h1", "h2", "h3"):
            for r in _finishes(p.get(key, "")):
                rr = 7 if r >= 8 else r
                if rr >= 1:
                    ranks.append(rr)
        if not ranks:
            return ""
        avg = float(sum(ranks)) / float(len(ranks))
        g = _grade(p.get("h2", ""))
        gb = 0
        if g in ("GP", "G1", "G2", "G3"):
            gb = 5
        elif g == "F1":
            gb = 3
        elif g == "F2":
            gb = 1
        vals.append(pt - avg * 5.0 + gb)
    if len(vals) < 3:
        return ""
    mx = max(vals)
    if mx <= 0:
        return ""
    t = 0.0
    for v in vals:
        t = t + v / mx * 10.0
    a = t / len(vals)
    q = 1
    for b in _FL_BOUNDS:
        if a >= b:
            return "Q" + str(q)
        q = q + 1
    return "Q5"


def _line_class(race):
    ln = race.get("line", "")
    if not ln or not isinstance(ln, str):
        return ""
    s2 = ln.replace("ー", "-").replace("−", "-").replace("―", "-")
    if "-" not in s2 and (" " in s2 or "\u3000" in s2):
        s2 = "-".join(s2.replace("\u3000", " ").split())
    n = 0
    for part in s2.split("-"):
        has = False
        for ch in part:
            if ch.isdigit():
                has = True
        if has:
            n = n + 1
    if n <= 1:
        return "一本棒"
    if n == 2:
        return "二分戦"
    if n == 3:
        return "三分戦"
    if n == 4:
        return "四分戦"
    return "細切戦"


def _load_conditions():
    p = _find("conditions.json")
    if not p:
        return None
    f = open(p, "r", encoding="utf-8")
    try:
        return json.load(f)
    finally:
        f.close()


PAGE = """<!doctype html><html lang="ja"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>託宣KEIRIN / race</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#12100c;color:#e8e0cf;
 font-family:-apple-system,"Hiragino Kaku Gothic ProN",sans-serif;
 font-size:15px;-webkit-tap-highlight-color:transparent}
.bar{display:flex;gap:6px;align-items:center;padding:8px 10px;
 background:#1a1710;border-bottom:1px solid #3a3226;position:sticky;top:0;z-index:5}
input[type=date]{background:#241f16;color:#e8e0cf;border:1px solid #4a4030;
 border-radius:6px;padding:5px 7px;font-size:14px}
button{background:#2a2418;color:#d9c9a3;border:1px solid #5a4c33;
 border-radius:6px;padding:6px 11px;font-size:14px}
button.on{background:#5a4620;color:#ffe6a8;border-color:#a8862f}
.scroll{display:flex;gap:6px;overflow-x:auto;padding:8px 10px;
 -webkit-overflow-scrolling:touch;scrollbar-width:none}
.scroll::-webkit-scrollbar{display:none}
.scroll button{flex:0 0 auto}
.rbtn{min-width:52px;text-align:center;line-height:1.15}
.rbtn small{display:block;font-size:10px;color:#9a8c70}
#tabs{display:flex;gap:0;border-bottom:1px solid #3a3226;background:#1a1710;
 position:sticky;top:45px;z-index:4}
#tabs button{flex:1;border:0;border-radius:0;border-bottom:2px solid transparent;
 background:transparent;padding:10px 0}
#tabs button.on{border-bottom-color:#c9a227;color:#ffe6a8;background:#221c12}
#body{padding:10px 10px 60px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{border-bottom:1px solid #332c20;padding:5px 4px;text-align:left}
th{color:#9a8c70;font-weight:normal;font-size:11px}
.msg{color:#9a8c70;padding:14px 4px;line-height:1.7}
.tag{display:inline-block;background:#2a2418;border:1px solid #5a4c33;
 border-radius:4px;padding:1px 7px;margin-right:5px;font-size:12px}
.hit{background:#3a2e12;border-color:#a8862f;color:#ffe6a8}
.buy{background:#1b1811;border:1px solid #3a3226;border-radius:8px;
 padding:9px 11px;margin-bottom:9px}
.buy h4{margin:0 0 6px;font-size:14px;color:#ffe6a8;font-weight:normal}
.combo{display:inline-block;background:#2f2a1c;border:1px solid #6a5a38;
 border-radius:5px;padding:3px 9px;margin:3px 4px 0 0;font-size:15px;
 letter-spacing:1px}
.small{font-size:11px;color:#9a8c70;margin-top:5px}
.warn{color:#e0a05a}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #5a4c33;
 border-top-color:#c9a227;border-radius:50%;animation:sp .8s linear infinite;
 vertical-align:-1px;margin-right:6px}
@keyframes sp{to{transform:rotate(360deg)}}
</style></head><body>

<div class="bar">
  <input type="date" id="d">
  <button id="go" onclick="loadVenues()">会場</button>
  <span id="st" class="msg" style="padding:0;font-size:12px"></span>
</div>
<div class="scroll" id="vs"></div>
<div class="scroll" id="rs"></div>
<div id="tabs"></div>
<div id="body"><div class="msg">日付を選び「会場」を押してください。<br>
会場を押すとRが出ます。Rを押すとそのレースだけ計算します。</div></div>

<script src="/race/oracle_core.js"></script>
<script>
var DATE="", VENUES=[], VI=-1, RKEY="", PAY=null, TAB="entry", COND=null, RES=null;

function $(i){return document.getElementById(i)}
function st(s){ $("st").innerHTML=s||""; }
function ymd(iso){ return (iso||"").replace(/-/g,""); }
function iso(y){ return y? y.slice(0,4)+"-"+y.slice(4,6)+"-"+y.slice(6,8):""; }

(function(){
  var t=new Date();
  var s=t.getFullYear()+"-"+("0"+(t.getMonth()+1)).slice(-2)+"-"+("0"+t.getDate()).slice(-2);
  $("d").value=s;
  fetch("/race/conditions").then(function(r){return r.json()})
    .then(function(j){ COND=j; }).catch(function(){});
})();

function loadVenues(){
  DATE=ymd($("d").value);
  if(!DATE){ st("日付を選んでください"); return; }
  VENUES=[]; VI=-1; RKEY=""; PAY=null;
  $("vs").innerHTML=""; $("rs").innerHTML=""; $("tabs").innerHTML="";
  $("body").innerHTML="";
  st('<span class="spin"></span>会場を読んでいます');
  fetch("/api/venues?date="+DATE).then(function(r){return r.json()})
    .then(function(j){
      VENUES=j.venues||[];
      if(!VENUES.length){ st(""); $("body").innerHTML='<div class="msg">'
        +(j.message||"開催なし")+'</div>'; return; }
      st("");
      var h="";
      for(var i=0;i<VENUES.length;i++){
        h+='<button onclick="pickVenue('+i+')" id="v'+i+'">'+VENUES[i].name
          +'</button>';
      }
      $("vs").innerHTML=h;
      $("body").innerHTML='<div class="msg">会場を選んでください。</div>';
    })
    .catch(function(e){ st("読み込み失敗: "+e); });
}

// 会場を押しても通信しない。取得済みの一覧からRを描くだけ。
function pickVenue(i){
  VI=i;
  for(var k=0;k<VENUES.length;k++){
    var b=$("v"+k); if(b) b.className=(k===i?"on":"");
  }
  var rl=VENUES[i].races||[];
  var h="";
  for(var j=0;j<rl.length;j++){
    h+='<button class="rbtn" id="r'+j+'" onclick="pickRace('+j+')">'
      +rl[j].race_no+'R<small>'+(rl[j].post_time||"")+'</small></button>';
  }
  $("rs").innerHTML=h;
  $("tabs").innerHTML=""; $("body").innerHTML=
    '<div class="msg">Rを選んでください。</div>';
  RKEY=""; PAY=null; RES=null;
}

// ここで初めて重い処理を1レースぶんだけ走らせる。
function pickRace(j){
  var rl=VENUES[VI].races||[];
  RKEY=rl[j].key; PAY=null; RES=null;
  for(var k=0;k<rl.length;k++){
    var b=$("r"+k); if(b) b.className="rbtn"+(k===j?" on":"");
  }
  drawTabs();
  $("body").innerHTML='<div class="msg"><span class="spin"></span>'
    +'このレースを計算しています…</div>';
  fetch("/api/race?date="+DATE+"&key="+encodeURIComponent(RKEY))
    .then(function(r){return r.json()})
    .then(function(j2){ PAY=j2; draw(); })
    .catch(function(e){ $("body").innerHTML='<div class="msg">失敗: '+e+'</div>'; });
}

function drawTabs(){
  var t=[["entry","出走表"],["ana","分析"],["pred","予想"],["res","結果"]];
  var h="";
  for(var i=0;i<t.length;i++){
    h+='<button class="'+(TAB===t[i][0]?"on":"")+'" onclick="setTab(\\''
      +t[i][0]+'\\')">'+t[i][1]+'</button>';
  }
  $("tabs").innerHTML=h;
}
function setTab(t){ TAB=t; drawTabs(); draw(); }

function draw(){
  if(!PAY){ return; }
  if(PAY.status!=="ok"){
    $("body").innerHTML='<div class="msg warn">このレースは対象外です。<br>'
      +(PAY.reason||PAY.error||"")+'</div>';
    return;
  }
  if(TAB==="entry"){ drawEntry(); }
  else if(TAB==="ana"){ drawAna(); }
  else if(TAB==="pred"){ drawPred(); }
  else { drawRes(); }
}

function drawEntry(){
  var h=PAY.header||{}, ps=h.players||[];
  var s='<div style="margin-bottom:8px">'
    +'<span class="tag">'+(h.venue||"")+' '+(h.race_no||"")+'R</span>'
    +'<span class="tag">'+(h.post_time||"")+'</span>'
    +'<span class="tag">'+lineStr()+'</span></div>';
  s+='<table><tr><th>車</th><th>選手</th><th>脚</th><th>S</th>'
    +'<th>rawscore</th><th>印</th></tr>';
  for(var i=0;i<ps.length;i++){
    var p=ps[i];
    s+='<tr><td>'+p.bike+'</td><td>'+(p.name||"")+'</td><td>'
      +(p.style||"")+'</td><td>'+(p.s!=null?p.s:"")+'</td><td>'
      +(p.raw_score!=null?Number(p.raw_score).toFixed(2):"")+'</td><td>'
      +(p.keihai_mark||"")+'</td></tr>';
  }
  s+='</table>';
  $("body").innerHTML=s;
}

function drawAna(){
  var h=PAY.header||{};
  var q=calcQ(), lc=lineClass();
  var s='<div style="margin-bottom:10px">'
    +'<span class="tag hit">'+q+'</span><span class="tag hit">'+lc+'</span>'
    +'<span class="tag">'+(PAY.wind_pat||"")+'</span>'
    +'<span class="tag">'+(PAY.speed_cls||"")+'</span></div>';
  s+='<div class="small">拮抗度Qは全車のrawscoreのばらつき。'
    +'Q1が拮抗、Q5が格差。分戦はラインの本数。</div>';
  var km=PAY.kimari||{};
  if(km.kimari_1st){
    s+='<div style="margin-top:12px"><b style="font-weight:normal;color:#9a8c70">'
      +'決まり手 ('+(km.cell_key||"")+' n='+(km.cell_n||0)+')</b><table>';
    for(var i=0;i<km.kimari_1st.length;i++){
      var k=km.kimari_1st[i];
      s+='<tr><td>'+k.label+'</td><td>'+Number(k.rate).toFixed(1)+'%</td></tr>';
    }
    s+='</table></div>';
  }
  $("body").innerHTML=s;
}

function calcQ(){
  var ps=(PAY.header||{}).players||[];
  var v=[];
  for(var i=0;i<ps.length;i++){
    var r=Number(ps[i].raw_score);
    if(!isNaN(r)) v.push(r);
  }
  if(v.length<2) return "Q?";
  var mx=Math.max.apply(null,v);
  if(mx<=0) return "Q?";
  var s=0;
  for(var j=0;j<v.length;j++) s+=v[j]/mx*10;
  var a=s/v.length;
  var b=[9.3637,9.1749,8.9724,8.6605];
  for(var k=0;k<b.length;k++){ if(a>=b[k]) return "Q"+(k+1); }
  return "Q5";
}

function lineStr(){
  var h=PAY.header||{};
  return String(h.line_display||h.line||"");
}
function lineClass(){
  var ln=lineStr();
  var parts=ln.replace(/[ー−―]/g,"-").split("-");
  var n=0;
  for(var i=0;i<parts.length;i++){ if(/\\d/.test(parts[i])) n++; }
  if(n<=1) return "一本棒";
  if(n===2) return "二分戦";
  if(n===3) return "三分戦";
  if(n===4) return "四分戦";
  return "細切戦";
}

function drawPred(){
  if(typeof __btUnionRace!=="function"){
    $("body").innerHTML='<div class="msg warn">oracle_core.js が読めていません。'
      +'<br>~/bt か takusen/data に置いてください。</div>';
    return;
  }
  if(!COND){ $("body").innerHTML='<div class="msg">条件表を読んでいます…</div>';
    return; }
  var q=calcQ(), lc=lineClass();
  var ven=(PAY.header||{}).venue||"";
  var s='<div style="margin-bottom:10px">'
    +'<span class="tag hit">'+q+'</span><span class="tag hit">'+lc+'</span>'
    +'<span class="tag">'+ven+'</span></div>';

  var cache={};
  var nBuy=0;
  var strat=COND.strategies||[];
  for(var i=0;i<strat.length;i++){
    var S=strat[i];
    var cs=S.conds||[];
    var lines="";
    for(var j=0;j<cs.length;j++){
      var c=cs[j];
      if(c.q!==q) continue;
      if(c.line!==lc) continue;
      if(c.venue && c.venue!==ven) continue;
      var key=S.series;
      if(!cache[key]){
        try{ cache[key]=__btUnionRace(PAY,"0-0-0",0,S.axes); }
        catch(e){ cache[key]={ok:false}; }
      }
      var u=cache[key];
      if(!u||!u.ok||!u.byN||!u.byN[c.n-1]) continue;
      var cb=u.byN[c.n-1].combos||[];
      var tags="";
      for(var k=0;k<cb.length;k++){ tags+='<span class="combo">'+cb[k]+'</span>'; }
      lines+='<div style="margin-bottom:7px">'
        +'<div class="small">'+c.n+'点 (前月回収 '+c.roi_prev+'%)</div>'
        +tags+'</div>';
      nBuy++;
    }
    if(lines){
      s+='<div class="buy"><h4>'+S.name+'</h4>'+lines+'</div>';
    }
  }
  if(!nBuy){
    s+='<div class="msg">該当する条件がありません。<b>見送り</b>です。</div>';
  }
  s+='<div class="small" style="margin-top:12px">'
    +'条件は '+(COND.updated||"")+' 時点のもの。'
    +'重複は排除していないので、同じレースに複数出ることがあります。<br>'
    +'<span class="warn">この条件は月あたりのレース数が少ない。'
    +'数字が良く見えても、母数を必ず確認すること。</span></div>';
  $("body").innerHTML=s;
}

function drawRes(){
  if(RES){ showRes(); return; }
  $("body").innerHTML='<div class="msg"><span class="spin"></span>結果を確認中…</div>';
  var rr=(PAY.race_result)||null;
  if(rr && rr.result && rr.result.length){ RES=rr; showRes(); return; }
  fetch("/api/result?date="+DATE+"&key="+encodeURIComponent(RKEY))
    .then(function(r){return r.json()})
    .then(function(j){ RES=j; showRes(); })
    .catch(function(){ $("body").innerHTML='<div class="msg">結果なし</div>'; });
}

function showRes(){
  var r=RES||{};
  var lst=r.result||[];
  if(!lst.length){ $("body").innerHTML='<div class="msg">まだ結果がありません。</div>';
    return; }
  var s='<table><tr><th>着</th><th>車</th><th>決まり手</th><th>差</th></tr>';
  for(var i=0;i<lst.length;i++){
    s+='<tr><td>'+lst[i].rank+'</td><td>'+lst[i].bike+'</td><td>'
      +(lst[i].finish||"")+'</td><td>'+(lst[i].diff||"")+'</td></tr>';
  }
  s+='</table>';
  if(r.refund_3t_raw){ s+='<div style="margin-top:10px"><span class="tag hit">3連単 '
    +r.refund_3t_raw+'</span></div>'; }
  $("body").innerHTML=s;
}
</script></body></html>"""


def register(app, load_races, get_dicts, build_race_payload, helpers=None):
    """既存の app に新しいURLを足す。既存のルートには触らない。"""

    @app.route("/race")
    def race_page():
        return Response(PAGE, mimetype="text/html")

    @app.route("/race/oracle_core.js")
    def race_core_js():
        p = _find("oracle_core.js")
        if not p:
            return Response("/* oracle_core.js が見つかりません */",
                            mimetype="application/javascript")
        f = open(p, "r", encoding="utf-8")
        try:
            js = f.read()
        finally:
            f.close()
        # Node用の module.exports がブラウザで落ちないようにする
        shim = "var module={exports:{}};var exports=module.exports;\n"
        return Response(shim + js, mimetype="application/javascript")

    @app.route("/race/conditions")
    def race_conditions():
        p = _find("conditions.json")
        if not p:
            return jsonify({"updated": "", "strategies": [],
                            "error": "conditions.json が見つかりません"})
        f = open(p, "r", encoding="utf-8")
        try:
            return Response(f.read(), mimetype="application/json")
        finally:
            f.close()

    H = helpers or {}
    GET_PICKS = H.get("get_picks")

    @app.route("/race/picks")
    def race_picks():
        """その日の買い目をそのまま返す。
        GitHub Actions が作ったものを読むだけで、判定はしない。"""
        date_str = request.args.get("date", "").strip()
        if not date_str:
            return jsonify({"error": "date が必要"}), 400
        if not GET_PICKS:
            return jsonify({"picks": {}, "error": "未対応"})
        try:
            pk = GET_PICKS(date_str)
        except Exception as e:
            return jsonify({"picks": {}, "error": str(e)[:120]})
        meta = pk.get("__meta__") or {}
        out = {}
        for k in pk:
            if k == "__meta__":
                continue
            out[k] = pk[k]
        return jsonify({"picks": out, "meta": meta, "n": len(out)})


    def _picks(date_str):
        """GitHubで作られた買い目を引く。端末では判定しない。"""
        fn = H.get("get_picks")
        if not fn:
            return {}
        try:
            return fn(date_str) or {}
        except Exception:
            return {}

    @app.route("/race/picks")
    def race_picks():
        """その日の買い目 (GitHub Actions が作ったもの) をそのまま返す。
        アプリ側は★の計算も条件判定もしない。受け取って表示するだけ。"""
        date_str = request.args.get("date", "").strip()
        if not date_str:
            return jsonify({"error": "date が必要"}), 400
        f = H.get("get_picks")
        if not f:
            return jsonify({"races": {}, "ok": False,
                            "reason": "get_picks 未登録"})
        try:
            picks = f(date_str)
        except Exception as e:
            return jsonify({"races": {}, "ok": False,
                            "reason": str(e)[:120]})
        return jsonify({"races": picks or {}, "ok": True,
                        "n": len(picks or {})})


    @app.route("/race/vflags")
    def race_vflags():
        """会場ぶんの表示情報を、重い処理を避けて返す。

        /api/venue_flags は全レースで予想計算(1件2.5秒)を走らせるため、
        会場を押してから数秒待たされていた。
        だが画面に要るもののうち、予想計算が必要なのは
        「計算可能か(displayable)」だけである。

          印(穴/弱/離) … 出走表を見るだけ。軽い。
          終了/発走前   … 時刻を見るだけ。軽い。
          結果・配当    … DBを引くだけ。通信しない。
          計算可能か    … 予想計算が要る。重い。

        そこで最後の1つを「稼働条件に該当したレースだけ」に絞る。
        1会場につき0〜2件なので、待ち時間はほぼ消える。
        """
        date_str = request.args.get("date", "").strip()
        venue = request.args.get("venue", "").strip()
        if not date_str or not venue:
            return jsonify({"error": "date と venue が必要"}), 400
        try:
            races, _m = load_races(date_str)
        except Exception as e:
            return jsonify({"error": str(e)[:120], "flags": {}})

        cond = _load_conditions()
        strat = (cond or {}).get("strategies") or []
        d = None
        try:
            d = get_dicts()
        except Exception:
            d = None

        f_labels = H.get("quick_label_kinds")
        f_disp = H.get("quick_displayable")
        f_result = H.get("result_and_hit")
        f_passed = H.get("is_post_passed")
        f_key = H.get("race_key")

        now = _dt.datetime.now()
        out = {}
        heavy = []
        i = 0
        while i < len(races):
            r = races[i]
            i = i + 1
            if r.get("place", "") != venue:
                continue
            key = (f_key(r) if f_key
                   else (str(r.get("place", "")) + "_" + str(r.get("race_no", ""))))

            q = _q_of(r)
            lc = _line_class(r)
            pl = r.get("players") or {}
            ok_basic = (len(pl) == 7 and lc != "" and lc != "一本棒")

            hits = []
            if ok_basic and q and lc:
                j = 0
                while j < len(strat):
                    S = strat[j]
                    j = j + 1
                    cs = S.get("conds") or []
                    k = 0
                    while k < len(cs):
                        c = cs[k]
                        k = k + 1
                        if c.get("q") != q or c.get("line") != lc:
                            continue
                        cv = c.get("venue")
                        if cv and cv != venue:
                            continue
                        hits.append({"name": S.get("name", ""),
                                     "series": S.get("series", ""),
                                     "axes": S.get("axes", []),
                                     "n": c.get("n"),
                                     "roi_prev": c.get("roi_prev")})
            pick = None
            m = 0
            while m < len(hits):
                h = hits[m]
                m = m + 1
                if pick is None or h.get("n", 99) < pick.get("n", 99):
                    pick = h
                elif h.get("n", 99) == pick.get("n", 99):
                    if (h.get("roi_prev") or 0) > (pick.get("roi_prev") or 0):
                        pick = h

            # --- 軽い情報 ---
            labels = {"ana": False, "weak": False, "layoff": False}
            if f_labels:
                try:
                    labels = f_labels(r)
                except Exception:
                    pass
            finished = False
            if f_passed:
                try:
                    finished = bool(f_passed(r, date_str, now))
                except Exception:
                    finished = False
            res = {"has_result": False, "hit": False,
                   "trifecta": "", "refund_3t": 0}
            if finished and f_result and d:
                try:
                    # 通信しない(allow_scrape=False)。DBにあるものだけ使う。
                    res = f_result(r, d["venue_home_dir"], d["bank_data"],
                                   allow_scrape=False)
                except Exception:
                    pass

            # 重い判定(計算可能か)は後でまとめて並列に行う。
            disp = None
            if pick is not None:
                heavy.append((key, r))

            out[key] = {
                "q": q, "line": lc, "hits": hits, "pick": pick,
                "displayable": disp,          # None = 判定していない
                "labels": labels,
                "finished": finished,
                "trifecta": res.get("trifecta", ""),
                "refund_3t": res.get("refund_3t", 0),
                "has_result": res.get("has_result", False),
            }
        # 買う候補のレースだけ「計算可能か」を判定する。
        #   1件2.5秒かかるので、複数あるときは並列にする。
        if heavy and f_disp and d:
            try:
                from concurrent.futures import ThreadPoolExecutor

                def _one(item):
                    k2, r2 = item
                    try:
                        return (k2, bool(f_disp(r2, d["venue_home_dir"],
                                                d["bank_data"])))
                    except Exception:
                        return (k2, False)

                ex = ThreadPoolExecutor(max_workers=4)
                try:
                    for k2, v2 in ex.map(_one, heavy):
                        if k2 in out:
                            out[k2]["displayable"] = v2
                finally:
                    ex.shutdown(wait=True)
            except Exception:
                for k2, r2 in heavy:
                    try:
                        out[k2]["displayable"] = bool(
                            f_disp(r2, d["venue_home_dir"], d["bank_data"]))
                    except Exception:
                        out[k2]["displayable"] = False

        return jsonify({"venue": venue, "flags": out,
                        "updated": (cond or {}).get("updated", ""),
                        "heavy": len(heavy)})

    @app.route("/race/venue_marks")
    def race_venue_marks():
        """全会場ぶん、予想があるかどうかだけ返す。
        予想計算は呼ばないので、会場ボタンの色付けに使える。"""
        date_str = request.args.get("date", "").strip()
        if not date_str:
            return jsonify({"error": "date が必要"}), 400
        try:
            races, _m = load_races(date_str)
        except Exception as e:
            return jsonify({"error": str(e)[:120], "venues": {}})
        cond = _load_conditions()
        strat = (cond or {}).get("strategies") or []
        out = {}
        cand = []
        i = 0
        while i < len(races):
            r = races[i]
            i = i + 1
            ven = r.get("place", "")
            if ven not in out:
                out[ven] = 0
            q = _q_of(r)
            lc = _line_class(r)
            pl = r.get("players") or {}
            if len(pl) != 7 or lc == "" or lc == "一本棒" or not q:
                continue
            found = False
            j = 0
            while j < len(strat) and not found:
                S = strat[j]
                j = j + 1
                cs = S.get("conds") or []
                k = 0
                while k < len(cs):
                    c = cs[k]
                    k = k + 1
                    if c.get("q") != q or c.get("line") != lc:
                        continue
                    cv = c.get("venue")
                    if cv and cv != ven:
                        continue
                    found = True
                    break
            if found:
                cand.append((ven, r))

        # v329: 条件に該当しても、計算できないレース(9車立て・風判定不可・
        #   該当セル無し)は買い目が作れない。ここを見ないと、
        #   予想が無い会場まで託宣カラーになってしまう。
        #   候補レースは1日で数件なので、並列で確かめる。
        f_disp = H.get("quick_displayable")
        d = None
        try:
            d = get_dicts()
        except Exception:
            d = None
        if cand and f_disp and d:
            try:
                from concurrent.futures import ThreadPoolExecutor

                def _chk(item):
                    v2, r2 = item
                    try:
                        return (v2, bool(f_disp(r2, d["venue_home_dir"],
                                                d["bank_data"])))
                    except Exception:
                        return (v2, False)

                ex = ThreadPoolExecutor(max_workers=4)
                try:
                    for v2, ok2 in ex.map(_chk, cand):
                        if ok2:
                            out[v2] = out[v2] + 1
                finally:
                    ex.shutdown(wait=True)
            except Exception:
                for v2, r2 in cand:
                    try:
                        if f_disp(r2, d["venue_home_dir"], d["bank_data"]):
                            out[v2] = out[v2] + 1
                    except Exception:
                        pass
        return jsonify({"venues": out, "checked": len(cand)})

    @app.route("/race/qflags")
    def race_qflags():
        """会場ぶんの 拮抗度Q / 分戦 / 予想の該当 をまとめて返す。
        予想計算は呼ばないので速い。Rボタンの色付けに使う。"""
        date_str = request.args.get("date", "").strip()
        venue = request.args.get("venue", "").strip()
        if not date_str or not venue:
            return jsonify({"error": "date と venue が必要"}), 400
        try:
            races, _m = load_races(date_str)
        except Exception as e:
            return jsonify({"error": str(e)[:120], "flags": {}})
        cond = _load_conditions()
        strat = (cond or {}).get("strategies") or []

        out = {}
        i = 0
        while i < len(races):
            r = races[i]
            i = i + 1
            if r.get("place", "") != venue:
                continue
            q = _q_of(r)
            lc = _line_class(r)
            hits = []
            # 7車立てでない / 個人戦 は買い目が作れない。
            # ここで落としておかないと「予想あり」と出たのに
            # 開くと計算対象外、という食い違いが起きる。
            ok_basic = True
            pl = r.get("players") or {}
            if len(pl) != 7:
                ok_basic = False
            if lc == "" or lc == "一本棒":
                ok_basic = False
            if ok_basic and q and lc:
                j = 0
                while j < len(strat):
                    S = strat[j]
                    j = j + 1
                    cs = S.get("conds") or []
                    k = 0
                    while k < len(cs):
                        c = cs[k]
                        k = k + 1
                        if c.get("q") != q:
                            continue
                        if c.get("line") != lc:
                            continue
                        cv = c.get("venue")
                        if cv and cv != venue:
                            continue
                        hits.append({"name": S.get("name", ""),
                                     "series": S.get("series", ""),
                                     "axes": S.get("axes", []),
                                     "n": c.get("n"),
                                     "roi_prev": c.get("roi_prev")})
            key = ""
            try:
                key = str(r.get("place", "")) + "_" + str(r.get("race_no", ""))
            except Exception:
                key = ""
            # 買うのは1つだけ。点数の少ないものを選ぶ (同点なら前月回収率が高い方)。
            pick = None
            m = 0
            while m < len(hits):
                h = hits[m]
                m = m + 1
                if pick is None:
                    pick = h
                    continue
                if h.get("n", 99) < pick.get("n", 99):
                    pick = h
                elif h.get("n", 99) == pick.get("n", 99):
                    if (h.get("roi_prev") or 0) > (pick.get("roi_prev") or 0):
                        pick = h
            out[key] = {"q": q, "line": lc, "hits": hits, "pick": pick}
        return jsonify({"venue": venue, "flags": out,
                        "updated": (cond or {}).get("updated", "")})

    return app

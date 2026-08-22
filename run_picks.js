// run_picks.js -- 買い目を確定させる (第2段/Node)
//
// 使い方: node run_picks.js [YYYYMMDD]
//
// build_picks.py が作った _picks_stage1.json を読み、
// oracle_core.js で各系列の買い目を作る。
// 条件が複数該当したら買い目を重ね、同じ目は1点にまとめる。
// どの条件から出たかは label に残す。
//
// 系列と関数の対応 (実データで確認済み):
//   検証A vfa = __btUnionRace(payload, tri, refund, [0,1,2])
//   検証B vfb = __btUnionRace(payload, tri, refund, [0,1,2,3])
//   検証C vfc = __btTavernRace(payload, tri, refund, [0,1,2,3])   関数が違う
// 買い目は byN[N-1].combos。
// 和集合なので「1点」でも実際の買い目は2目前後になる。
// trifecta と refund は買い目を作るだけなら使わないので 0-0-0 / 0 を渡す。

var fs = require("fs");
var path = require("path");
var C = null;
(function () {
  var cand = [
    path.join(__dirname, "oracle_core.js"),
    path.join(process.cwd(), "oracle_core.js"),
    "/storage/emulated/0/Download/oracle_core.js",
    "/storage/emulated/0/Download/takusen/data/oracle_core.js",
    "/storage/emulated/0/Download/takusen/code/oracle_core.js"
  ];
  for (var i = 0; i < cand.length; i++) {
    try {
      if (fs.existsSync(cand[i])) { C = require(cand[i]); return; }
    } catch (e) {}
  }
  console.error("[error] oracle_core.js が見つかりません。");
  for (var j = 0; j < cand.length; j++) console.error("  " + cand[j]);
  process.exit(1);
})();

var AXES_A = [0, 1, 2];
var AXES_B = [0, 1, 2, 3];

var HERE = __dirname;

// 置き場所を選ばないように候補を順に探す。
// Actions ではリポジトリ直下に並ぶので HERE だけで足りるが、
// 手元での試しではフォルダが分かれていることがある。
var SEARCH_DIRS = [
  HERE,
  process.cwd(),
  "/sdcard/Download",
  "/storage/emulated/0/Download",
  "/sdcard/Download/takusen",
  "/storage/emulated/0/Download/takusen",
  "/sdcard/Download/takusen/code",
  "/storage/emulated/0/Download/takusen/code",
  "/sdcard/Download/takusen/data",
  "/storage/emulated/0/Download/takusen/data",
  "/data/user/0/ru.iiec.pydroid3/files",
  "/data/data/ru.iiec.pydroid3/files"
];

function findNear(name) {
  for (var i = 0; i < SEARCH_DIRS.length; i++) {
    if (!SEARCH_DIRS[i]) continue;
    var p = path.join(SEARCH_DIRS[i], name);
    try { if (fs.existsSync(p)) return p; } catch (e) {}
  }
  return "";
}

// 第1引数がファイルパスならそれを使う。日付でも可。
var argPath = "";
var argDate = "";
if (process.argv[2]) {
  var a2 = String(process.argv[2]);
  if (a2.indexOf("/") >= 0 || a2.indexOf(".json") >= 0) argPath = a2;
  else argDate = a2;
}
var stagePath = argPath;
if (stagePath && !fs.existsSync(stagePath)) {
  console.error("[error] 指定されたファイルがありません: " + stagePath);
  process.exit(1);
}
if (!stagePath) stagePath = findNear("_picks_stage1.json");
if (!stagePath) {
  console.error("[error] _picks_stage1.json がありません。");
  console.error("        先に python3 build_picks.py を実行してください。");
  for (var si = 0; si < SEARCH_DIRS.length; si++) {
    console.error("  探した場所: " + SEARCH_DIRS[si]);
  }
  process.exit(1);
}
console.log("入力: " + stagePath);
var stage = JSON.parse(fs.readFileSync(stagePath, "utf8"));
var date = argDate || stage.date;

function combosOf(payload, series, pts) {
  var u = null;
  try {
    if (series === "vfa") u = C.__btUnionRace(payload, "0-0-0", 0, AXES_A);
    else if (series === "vfb") u = C.__btUnionRace(payload, "0-0-0", 0, AXES_B);
    else if (series === "vfc") u = C.__btTavernRace(payload, "0-0-0", 0, AXES_B);
    else return null;
  } catch (e) { return null; }
  if (!u || !u.ok || !u.byN) return null;
  if (pts - 1 >= u.byN.length) return null;
  var b = u.byN[pts - 1];
  if (!b || !b.combos) return null;
  return b.combos;
}

// 買い目をフォーメーションにまとめる。
//   1着-2着が同じものを束ね、3着を並べる。
//     5-2-7 / 5-2-1 / 5-2-4  ->  5-2-1,4,7
//   まとまらない目はそのまま残す。全部を1つの形にすると
//   買っていない目が混入して点数が変わるので、それはしない。
//   まとめた後に展開し直し、元の集合と完全一致するかを必ず照合する。
//   一致しなければフォーメーションを使わず個別表示に戻す。
function toForm(combos) {
  var by = {};
  var order = [];
  for (var i = 0; i < combos.length; i++) {
    var p = String(combos[i]).split("-");
    if (p.length !== 3) return null;
    var k = p[0] + "-" + p[1];
    if (!by[k]) { by[k] = []; order.push(k); }
    by[k].push(p[2]);
  }
  var out = [];
  for (var j = 0; j < order.length; j++) {
    var k2 = order[j];
    var th = by[k2].slice().sort(function (a, b) {
      return Number(a) - Number(b);
    });
    out.push({ head: k2, third: th, n: th.length });
  }
  return out;
}

function expandForm(forms) {
  var out = [];
  for (var i = 0; i < forms.length; i++) {
    for (var j = 0; j < forms[i].third.length; j++) {
      out.push(forms[i].head + "-" + forms[i].third[j]);
    }
  }
  return out;
}

function formText(forms) {
  var s = [];
  for (var i = 0; i < forms.length; i++) {
    s.push(forms[i].head + "-" + forms[i].third.join(","));
  }
  return s.join(" / ");
}

function sortedKey(arr) {
  return arr.slice().sort().join("|");
}

var races = stage.races || [];
var out = [];
var nFail = 0;
var nFormFail = 0;

for (var i = 0; i < races.length; i++) {
  var r = races[i];
  var seen = {};
  var order = [];
  var from = [];
  var labels = [];
  var plan = r.plan || [];
  for (var p = 0; p < plan.length; p++) {
    var st = plan[p];
    var cb = combosOf(r.payload, st.series, st.points);
    if (!cb || !cb.length) continue;
    var mine = [];
    for (var c = 0; c < cb.length; c++) {
      var t = String(cb[c]);
      mine.push(t);
      if (!seen[t]) { seen[t] = []; order.push(t); }
      seen[t].push(st.label);
    }
    from.push({ id: st.id, label: st.label, mark: st.mark,
                series: st.series, points: st.points, combos: mine });
    labels.push(String(st.label) + String(st.mark || "")
                + String(st.points));
  }
  if (!order.length) { nFail++; continue; }

  // 買い目ごとに、どの条件から出たかを残す
  var combos = [];
  for (var k = 0; k < order.length; k++) {
    combos.push({ t: order[k], from: seen[order[k]] });
  }

  // フォーメーション。点数が変わっていないかを必ず確かめる。
  var forms = toForm(order);
  var formOk = false;
  var formStr = "";
  if (forms) {
    var ex = expandForm(forms);
    if (ex.length === order.length && sortedKey(ex) === sortedKey(order)) {
      formOk = true;
      formStr = formText(forms);
    } else {
      nFormFail++;
    }
  }

  out.push({
    key: r.key, venue: r.venue, race_no: r.race_no,
    post_time: r.post_time,
    star: r.star, pred_pay: r.pred_pay,
    q: r.q, line_config: r.line_config, race_type: r.race_type,
    label: labels.join(" \u00b7 "),
    points: order.length,
    formation: formOk ? formStr : "",
    formation_ok: formOk,
    combos: combos,
    from: from
  });
}

var outDir = path.join(HERE, "picks");
try {
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });
} catch (e) {
  outDir = "/storage/emulated/0/Download/picks";
  try { if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true }); }
  catch (e2) { outDir = path.dirname(stagePath); }
}
var outPath = path.join(outDir, date + ".json");
var body = {
  date: date,
  generated: new Date().toISOString().replace("T", " ").substring(0, 19),
  star_model: stage.star_model,
  conditions_updated: stage.conditions_updated,
  races: out
};
fs.writeFileSync(outPath, JSON.stringify(body, null, 1), "utf8");

var tot = 0;
for (var m = 0; m < out.length; m++) tot += out[m].points;
console.log("買い目のあるレース " + out.length + "R  合計 " + tot + "点");
if (nFail) console.log("  買い目を作れなかったレース " + nFail + "R");
if (nFormFail) console.log("  [警告] フォーメーションが一致しなかった " + nFormFail
  + "R (個別表示に戻した)");
for (var n = 0; n < out.length && n < 8; n++) {
  var o = out[n];
  var ts = [];
  for (var q = 0; q < o.combos.length && q < 5; q++) ts.push(o.combos[q].t);
  console.log("  " + o.venue + o.race_no + "R  "
    + "\u2605".repeat(o.star) + "  " + o.label
    + "  計" + o.points + "点");
  console.log("      " + (o.formation_ok ? o.formation : ts.join(" ")));
}
if (out.length > 8) console.log("  ... 他 " + (out.length - 8) + "R");
console.log("");
console.log("出力: " + outPath);

// run_day.js -- __btProcessOneDay の accumRace ループを Node で再現する
// 使い方: node run_day.js <in.json> <date> <out.json>
// in.json は /api/bt_day の返り値そのもの。
var fs = require("fs");
var C = require("./oracle_core.js");

var _BT_SCOPE = "all";
var _VF_AXES_A = [0, 1, 2];
var _VF_AXES_B = [0, 1, 2, 3];

var inPath = process.argv[2];
var date = process.argv[3];
var outPath = process.argv[4];

var j = JSON.parse(fs.readFileSync(inPath, "utf8"));
var races = j.races || [];

var aggs = {
  omk0: C.__btNewAgg(), omk1: C.__btNewAgg(),
  omk2: C.__btNewAgg(), omk3: C.__btNewAgg(),
  oraA: C.__btNewAgg(), oraB: C.__btNewAgg(),
  oraC: C.__btNewAgg(), oraD: C.__btNewAgg(),
  vfa0: C.__btNewAgg(), vfb0: C.__btNewAgg(), vfc0: C.__btNewAgg()
};
var oraMarks = [["oraA", "\u25ce"], ["oraB", "\u25ef"],
                ["oraC", "\u25b2"], ["oraD", "\u25b3"]];
var _omkFail = {};

function accumRace(payload, rs, oddsMap, raceKey) {
  if (!payload || payload.status !== "ok") return;
  if (!rs || !rs.ok || !rs.trifecta) return;
  var refund = rs.refund_3t || 0;
  var tri = rs.trifecta;
  var players = (payload.header && payload.header.players)
    ? payload.header.players : [];
  var venueName = (payload.header && payload.header.venue)
    ? payload.header.venue : "";
  var hdr = (payload && payload.header) ? payload.header : {};
  var meta = {
    date: date,
    key: raceKey || ("" + (hdr.venue || "") + "_" + (hdr.race_no || "")),
    post: hdr.post_time || "", raceNo: hdr.race_no || "",
    trifecta: tri, venue: venueName,
    axisMark: "", axisBike: String(tri).split("-")[0],
    labAna: false, labWeak: false, labLayoff: false
  };
  for (var lp = 0; lp < players.length; lp++) {
    var lk = players[lp].label_kind;
    if (lk === "ana") meta.labAna = true;
    else if (lk === "weak") meta.labWeak = true;
    if (players[lp].layoff_kind === "layoff") meta.labLayoff = true;
    if (String(players[lp].bike) === meta.axisBike) {
      meta.axisMark = players[lp].keihai_mark || "";
    }
  }
  meta.refundAll = refund;

  var urA = C.__btUnionRace(payload, tri, refund, _VF_AXES_A);
  if (urA.ok) { C.__btAccumUnion(aggs.vfa0, urA, meta); }
  var urB = C.__btUnionRace(payload, tri, refund, _VF_AXES_B);
  if (urB.ok) { C.__btAccumUnion(aggs.vfb0, urB, meta); }
  var urC = C.__btTavernRace(payload, tri, refund, _VF_AXES_B);
  if (urC.ok) { C.__btAccumUnion(aggs.vfc0, urC, meta); }


  var omkKeys = ["omk0", "omk1", "omk2", "omk3"];
  for (var oi = 0; oi < 4; oi++) {
    C.setAxis(oi);
    var evk = C.__btEvalRace(payload, tri, refund);
    if (evk.ok) { C.__btAccum(aggs[omkKeys[oi]], evk, meta); }
    else if (oi === 0) {
      var _rz = String(evk.error || "(\u7406\u7531\u306a\u3057)").substring(0, 200);
      _omkFail[_rz] = (_omkFail[_rz] || 0) + 1;
    }
  }
  // __btEvalRace が内部で _ORA_OMK_AXIS を退避・復元するため復元は不要

  for (var mi = 0; mi < oraMarks.length; mi++) {
    var mk = oraMarks[mi][1];
    var aggKey = oraMarks[mi][0];
    var axisB = null;
    for (var pp = 0; pp < players.length; pp++) {
      var km = players[pp].keihai_mark;
      if (km === mk || (mk === "\u25ef" && km === "\u25cb")
          || (mk === "\u25cb" && km === "\u25ef")) {
        axisB = String(players[pp].bike); break;
      }
    }
    if (axisB == null) continue;
    var evo = C.__btEvalOra(payload, axisB, tri, refund);
    if (evo.ok) {
      var metaO = {
        date: date, key: "", trifecta: tri, venue: meta.venue,
        axisMark: mk, axisBike: axisB,
        labAna: meta.labAna, labWeak: meta.labWeak, labLayoff: meta.labLayoff
      };
      C.__btAccum(aggs[aggKey], evo, metaO);
    }
  }
}

for (var i = 0; i < races.length; i++) {
  accumRace(races[i].payload, races[i].result, races[i].odds, races[i].key);
}

var raw = {
  omk: { "0": aggs.omk0, "1": aggs.omk1, "2": aggs.omk2, "3": aggs.omk3 },
  ora: { honmei: aggs.oraA, taikou: aggs.oraB,
         tanana: aggs.oraC, renka: aggs.oraD },
  vfa: { "0": aggs.vfa0 },
  vfb: { "0": aggs.vfb0 },
  vfc: { "0": aggs.vfc0 }
};
fs.writeFileSync(outPath, JSON.stringify({ date: date, raw: raw }), "utf8");
console.log("races=" + races.length + " -> " + outPath);
console.log("omkFail=" + JSON.stringify(_omkFail).substring(0, 200));

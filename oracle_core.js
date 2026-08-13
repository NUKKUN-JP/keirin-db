// oracle_core.js -- app本体から自動抽出した集計コア
// 生成元: app_singlev327.py
// 手で編集しないこと。extract_oracle_core.py で作り直す。

var _BT_AXIS = 0;
var _BT_MAXN = 20;
var _ORA_AXIS = [];
var _ORA_CONF_N = 20;
var _ORA_DBG = {call:0,noLink:0,items:0,badLabel2:0,noBikes2:0,noThird:0,badLabel3:0,noBikes3:0,out:0,lab:"",cell:""};
var _ORA_OMK_AXIS = 0;
var _ORA_RATIO_CAP = 3.0;
var _ORA_RIVAL = [];
var _ORA_RSR_CONFN = 10;
var _RACE = null;

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

function nz(v,lo,hi){ if(v==null||hi<=lo) return 0.5; return (v-lo)/(hi-lo); }

function __oraKimRate(kimari, kk){
  if(!kimari || !kimari.items || !kk) return null;
  for(var i=0;i<kimari.items.length;i++){
    if(kimari.items[i].k===kk) return kimari.items[i].rate;
  }
  return 0;
}

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

function __oraLink(kimariPayload, axisKim){
  if(!kimariPayload || !kimariPayload.kimari_link) return null;
  for(var i=0;i<kimariPayload.kimari_link.length;i++){
    if(kimariPayload.kimari_link[i].kimari===axisKim) return kimariPayload.kimari_link[i];
  }
  return null;
}

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

function __oraRsrMap(players){
  var m={};
  for(var i=0;i<players.length;i++){
    m[String(players[i].bike)] = players[i].rsr || null;
  }
  return m;
}

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

function __oraRsrScore(rsrMap, axis, b2, b3){
  var fa=__oraRsrFluct(rsrMap[axis], 1);
  var f2=__oraRsrFluct(rsrMap[b2], 2);
  var f3=__oraRsrFluct(rsrMap[b3], 3);
  return fa*f2*f3;
}

function __oraRsrSelfPct(rsr, rank){
  if(!rsr || !rsr.self) return null;
  var idx=rank-1;
  if(idx<0 || idx>6) return null;
  var sp=rsr.self.pct;
  if(!sp || sp[idx]==null) return null;
  return sp[idx];
}

function __oraRsrAxisScore(rsr){
  var sp=__oraRsrSelfPct(rsr, 1);
  if(sp==null) return 0;
  var n=(rsr && rsr.self && rsr.self.n!=null)?rsr.self.n:0;
  var conf=(n>0)?Math.min(1, n/_ORA_RSR_CONFN):0;
  var fl=__oraRsrFluct(rsr, 1);
  return sp*conf*fl;
}

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
  agg.detail.push({
    date:meta.date, key:meta.key, trifecta:meta.trifecta,
    venue:meta.venue||"", post:meta.post||"", raceNo:meta.raceNo||"",
    refund:meta.refundAll||0, unionPts:pts, unionHit:hits,
    unionPerN:perByN,
    unionCombosN:combosByN,
    unionFiltered:1,
    axisMark:meta.axisMark, axisBike:meta.axisBike,
    labAna:meta.labAna, labWeak:meta.labWeak, labLayoff:meta.labLayoff
  });
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

function __btNewAgg(){
  var a={races:0, skipped:0, noResult:0, hitN:[], retN:[], betN:[], detail:[]};
  for(var i=0;i<_BT_MAXN;i++){ a.hitN.push(0); a.retN.push(0); a.betN.push(0); }
  return a;
}

function __btAccum(agg, ev, meta){
  agg.races++;
  var navail=ev.combos.length;
  agg.detail.push({
    date:meta.date, key:meta.key, trifecta:meta.trifecta, venue:meta.venue||"",
    post:meta.post||"", raceNo:meta.raceNo||"",
    hitIndex:ev.hitIndex, refund:ev.refund, navail:navail,
    axisMark:meta.axisMark, axisBike:meta.axisBike,
    labAna:meta.labAna, labWeak:meta.labWeak, labLayoff:meta.labLayoff
  });
  for(var n=1;n<=_BT_MAXN;n++){
    var buyN=(n<=navail)?n:navail;
    if(buyN<=0) continue;
    agg.betN[n-1]+=buyN*100;
    if(ev.hitIndex>=0 && ev.hitIndex<buyN){
      agg.hitN[n-1]++; agg.retN[n-1]+=ev.refund;
    }
  }
}

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

function toList(acc, layer){
    var arr=[];
    for(var k in acc){ if(acc.hasOwnProperty(k)){
      arr.push({b:acc[k].parts, sc:acc[k].sc, fluct:acc[k].fluct, _l:layer});
    }}
    return arr;
  }

function take(list, n, dst){
    for(var i=0;i<list.length && dst.length<n;i++){
      var kk=list[i].b.join('-');
      if(used[kk]) continue;
      used[kk]=1; dst.push(list[i]);
    }
  }

function pushRest(list,dst){
      for(var i=0;i<list.length;i++){
        var kk=list[i].b.join('-');
        if(used[kk]) continue;
        rest.push({item:list[i], dst:dst});
      }
    }

function norm(list){
    var mx=0; for(var i=0;i<list.length;i++){ if(list[i].sc>mx)mx=list[i].sc; }
    if(mx<=0)mx=1;
    for(var i=0;i<list.length;i++){ list[i].score=Math.round(list[i].sc/mx*1000)/10; }
  }

module.exports = {__axisCounts, __btAccum, __btAccumEV, __btAccumUnion, __btEvalOra, __btEvalRace, __btEvalRaceEV, __btNewAgg, __btOrderedCombos, __btTavernRace, __btUnionRace, __oraAccumAxis, __oraAxis1st, __oraAxisKimRank, __oraDbgReset, __oraDbgText, __oraFluct, __oraKimRate, __oraLabelToBikes, __oraLink, __oraOmakasePredict, __oraParseLabel, __oraParseLines, __oraPlayerBase, __oraPredict, __oraRankCandidates, __oraRsrAxisScore, __oraRsrFluct, __oraRsrMap, __oraRsrScore, __oraRsrSelfPct, __oraScenarioCombos, __tavByAxis, __tavExpand, accum, comboLabelBonus, norm, nz, pushRest, take, toList, setRace: function(r){ _RACE = r; }, setAxis: function(a){ _BT_AXIS = a; _ORA_OMK_AXIS = a; }};
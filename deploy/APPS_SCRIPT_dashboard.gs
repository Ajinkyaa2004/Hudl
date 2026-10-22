// PuckTrace search log + dashboard. Paste over everything in Extensions > Apps Script, Save,
// then Deploy > Manage deployments > edit (pencil) > Version: New version > Deploy (same URL).
// Once: choose the function "setup" in the toolbar and press Run (it asks for permission once).
var KEY = 'SEE data/sheet_log.json (key)';
var HEAD = ['Time (IST)', 'Name', 'Header', 'Found', 'Report link', 'Server',
            'Search time (s)', 'Result', 'Found on', 'Competition', 'Match ID'];
var ACC_HEAD = ['Match ID', 'Header', 'PuckTrace answer', 'PuckTrace link', 'Analyst link', 'Verdict', 'Checked on'];

function logSheet_(ss) {
  var all = ss.getSheets();
  for (var i = 0; i < all.length; i++) {
    if (String(all[i].getRange(1, 1).getValue()) === HEAD[0]) return all[i];
  }
  return all[0];
}

function doPost(e) {
  var d = JSON.parse(e.postData.contents);
  if (d.key !== KEY) return ContentService.createTextOutput('no');
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (d.type === 'accuracy') {                       // PuckTrace's answers checked against the RTT sheet
    var a = ss.getSheetByName('Accuracy') || ss.insertSheet('Accuracy');
    a.clear();
    a.getRange(1, 1, 1, ACC_HEAD.length).setValues([ACC_HEAD]).setFontWeight('bold');
    if (d.rows && d.rows.length) a.getRange(2, 1, d.rows.length, ACC_HEAD.length).setValues(d.rows);
    a.setFrozenRows(1);
    buildDashboard();
    return ContentService.createTextOutput('ok ' + (d.rows || []).length);
  }
  var s = logSheet_(ss);
  s.getRange(1, 1, 1, HEAD.length).setValues([HEAD]);
  s.appendRow([d.time, d.name, d.header, d.found, d.link, d.server,
               d.seconds, d.result, d.found_on, d.competition, d.match_id]);
  return ContentService.createTextOutput('ok');
}

function onOpen() {
  SpreadsheetApp.getUi().createMenu('PuckTrace').addItem('Refresh dashboard', 'buildDashboard').addToUi();
}

function setup() {                                    // once: hourly refresh + first build
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'buildDashboard') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('buildDashboard').timeBased().everyHours(1).create();
  buildDashboard();
}

// ---------------------------------------------------------------- dashboard
var T = {navy: '#14213d', ink: '#1f2a44', sub: '#6b7280', line: '#e5e7eb', bg: '#f4f6fb', card: '#ffffff', orange: '#f26a1b',
         found: '#22a06b', pick: '#f5a524', none: '#e5484d', wrong: '#9f1239', blue: '#3b82f6', teal: '#0ea5a4',
         purple: '#7c3aed', grey: '#9ca3af', sky: '#7aa6d9', font: 'Roboto'};

function when_(v) {
  if (v instanceof Date) return v;
  var m = String(v).match(/(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
}
function fmt_(t, f) { return Utilities.formatDate(t, 'Asia/Kolkata', f); }
function res_(r) {
  var x = String(r[7] || '').trim();
  if (x === 'Found' || (!x && String(r[3]).trim() === 'Yes')) return 'Found';
  if (x === 'Shortlist') return 'Pick one';
  return 'Not found';
}

function buildDashboard() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var log = logSheet_(ss);
  var rows = log.getDataRange().getValues().slice(1).filter(function (r) { return r[2]; }).map(function (r) {
    return {t: when_(r[0]), name: String(r[1] || '').trim() || '(no name)', header: String(r[2]), res: res_(r),
            secs: parseFloat(r[6]), server: String(r[5] || '') || 'unknown', site: String(r[8] || '')};
  }).filter(function (r) { return r.t; });

  // ---- numbers
  var n = rows.length, cnt = {'Found': 0, 'Pick one': 0, 'Not found': 0};
  rows.forEach(function (r) { cnt[r.res]++; });
  var timed = rows.filter(function (r) { return !isNaN(r.secs); });
  var secs = timed.map(function (r) { return r.secs; }).sort(function (a, b) { return a - b; });
  var med = secs.length ? secs[Math.floor(secs.length / 2)] : 0;
  var today = fmt_(new Date(), 'yyyy-MM-dd');
  var todayRows = rows.filter(function (r) { return fmt_(r.t, 'yyyy-MM-dd') === today; });
  var todayFound = todayRows.filter(function (r) { return r.res === 'Found'; }).length;
  var ppl = {};
  rows.forEach(function (r) {
    var k = r.name.toLowerCase();
    var p = ppl[k] || (ppl[k] = {name: r.name, n: 0, 'Found': 0, 'Pick one': 0, 'Not found': 0, s: 0, sn: 0, last: r.t});
    p.n++; p[r.res]++; if (!isNaN(r.secs)) { p.s += r.secs; p.sn++; } if (r.t > p.last) p.last = r.t;
  });
  var plist = Object.keys(ppl).map(function (k) { return ppl[k]; }).sort(function (a, b) { return b.n - a.n; });
  var ACC = ['Correct (same game as the analyst)', 'Wrong (different game from the analyst)', 'Missed (a report existed, PuckTrace did not find it)',
             'Found on a different website from the analyst (not compared)', 'Cannot check yet (analyst used a screenshot or has no link yet)',
             'No report anywhere (the analyst found none either)'];
  var ACC_SHORT = ['Correct', 'Wrong', 'Missed (report existed)', 'Found on another website', 'Cannot check yet', 'No report anywhere'];
  var ac = ACC.map(function () { return 0; });
  var accSheet = ss.getSheetByName('Accuracy');
  if (accSheet) accSheet.getDataRange().getValues().slice(1).forEach(function (r) { var i = ACC.indexOf(r[5]); if (i >= 0) ac[i]++; });
  var checked = ac[0] + ac[1];

  // ---- hidden data tab (the charts read from it, the dashboard stays clean)
  var dd = ss.getSheetByName('Dashboard data') || ss.insertSheet('Dashboard data');
  dd.clear();
  var dRow = 1;
  function put(head, body) {
    dd.getRange(dRow, 1, 1, head.length).setValues([head]);
    if (body.length) dd.getRange(dRow + 1, 1, body.length, head.length).setValues(body);
    var r = dd.getRange(dRow, 1, Math.max(body.length, 1) + 1, head.length);
    dRow += body.length + 3;
    return r;
  }
  var dRes = put(['Result', 'Searches'], [['Found', cnt['Found']], ['Pick one', cnt['Pick one']], ['Not found', cnt['Not found']]]);
  var dAcc = put(['Verdict', 'Games'], ACC_SHORT.map(function (k, i) { return [k, ac[i]]; }));
  var days = {}, dorder = [];
  rows.forEach(function (r) {
    var k = fmt_(r.t, 'yyyy-MM-dd');
    if (!days[k]) { days[k] = {'Found': 0, 'Pick one': 0, 'Not found': 0, n: 0}; dorder.push(k); }
    days[k][r.res]++; days[k].n++;
  });
  dorder.sort();
  var last = dorder.slice(-21);
  var dDay = put(['Day', 'Found', 'Pick one', 'Not found'], last.map(function (k) {
    return [fmt_(new Date(k + 'T12:00:00'), 'dd MMM'), days[k]['Found'], days[k]['Pick one'], days[k]['Not found']]; }));
  var dRate = put(['Day', 'Found automatically (%)'], last.map(function (k) {
    return [fmt_(new Date(k + 'T12:00:00'), 'dd MMM'), Math.round(100 * days[k]['Found'] / days[k].n)]; }));
  var top = plist.slice(0, 12);
  var dPpl = put(['Person', 'Found', 'Pick one', 'Not found'], top.map(function (p) { return [p.name, p['Found'], p['Pick one'], p['Not found']]; }));
  var sites = {};
  rows.forEach(function (r) { if (r.res === 'Found' && r.site) sites[r.site] = (sites[r.site] || 0) + 1; });
  var slist = Object.keys(sites).map(function (k) { return [k, sites[k]]; }).sort(function (a, b) { return b[1] - a[1]; }).slice(0, 10);
  var dSite = put(['Website', 'Reports found'], slist.length ? slist : [['none yet', 0]]);
  var B = [['< 5 s', 0, 5], ['5–15 s', 5, 15], ['15–30 s', 15, 30], ['30–60 s', 30, 60], ['> 60 s', 60, 1e9]];
  var dSpeed = put(['Search time', 'Searches'], B.map(function (b) { return [b[0], secs.filter(function (x) { return x >= b[1] && x < b[2]; }).length]; }));
  var hours = [];
  for (var h = 0; h < 24; h++) hours.push([('0' + h).slice(-2) + ':00', 0]);
  rows.forEach(function (r) { hours[+fmt_(r.t, 'H')][1]++; });
  var dHour = put(['Hour (IST)', 'Searches'], hours);
  dd.hideSheet();

  // ---- the dashboard tab
  var sh = ss.getSheetByName('Dashboard');
  if (!sh) sh = ss.insertSheet('Dashboard', 1);
  sh.getCharts().forEach(function (c) { sh.removeChart(c); });
  if (sh.getMaxColumns() < 12) sh.insertColumnsAfter(sh.getMaxColumns(), 12 - sh.getMaxColumns());
  if (sh.getMaxRows() < 160) sh.insertRowsAfter(sh.getMaxRows(), 160 - sh.getMaxRows());
  var all = sh.getRange(1, 1, sh.getMaxRows(), sh.getMaxColumns());
  all.breakApart(); sh.clear(); sh.clearConditionalFormatRules();
  sh.setHiddenGridlines(true);
  sh.setColumnWidths(1, 12, 100);
  sh.setRowHeights(1, sh.getMaxRows(), 21);
  all.setBackground(T.bg).setFontFamily(T.font).setFontColor(T.ink).setVerticalAlignment('middle');

  // banner
  sh.setRowHeights(1, 2, 30);
  sh.getRange(1, 1, 2, 12).merge().setValue('   PuckTrace   ·   Search dashboard').setBackground(T.navy)
    .setFontColor('#ffffff').setFontSize(22).setFontWeight('bold');
  sh.getRange(3, 1, 1, 12).merge().setValue('   Updated ' + fmt_(new Date(), 'dd MMM yyyy, HH:mm') + ' IST   ·   refreshes every hour   ·   menu PuckTrace › Refresh dashboard')
    .setBackground(T.orange).setFontColor('#ffffff').setFontSize(10);

  // KPI cards
  function card(col, label, value, sub, color) {
    sh.getRange(5, col, 1, 2).merge().setValue(label.toUpperCase()).setFontSize(9).setFontWeight('bold').setFontColor(T.sub);
    sh.getRange(6, col, 2, 2).merge().setValue(value).setFontSize(26).setFontWeight('bold').setFontColor(color).setHorizontalAlignment('left');
    sh.getRange(8, col, 1, 2).merge().setValue(sub).setFontSize(9).setFontColor(T.sub);
    var box = sh.getRange(5, col, 4, 2).setBackground(T.card);
    box.setBorder(true, true, true, true, null, null, T.line, SpreadsheetApp.BorderStyle.SOLID);
    sh.getRange(5, col, 1, 2).setBorder(true, null, null, null, null, null, color, SpreadsheetApp.BorderStyle.SOLID_THICK);
    sh.getRange(5, col, 4, 2).setHorizontalAlignment('left');
  }
  sh.setRowHeight(4, 12); sh.setRowHeight(5, 24); sh.setRowHeights(6, 2, 24); sh.setRowHeight(8, 22);
  card(1, 'Searches', n, 'since the log started', T.navy);
  card(3, 'Found automatically', n ? Math.round(100 * cnt['Found'] / n) + '%' : '–', cnt['Found'] + ' of ' + n + ' searches', T.found);
  card(5, 'Answers correct', checked ? (100 * ac[0] / checked).toFixed(1) + '%' : '–', checked ? ac[0] + ' of ' + checked + ' checked games' : 'after the next RTT sheet', T.teal);
  card(7, 'People using it', plist.length, plist.length ? 'most active: ' + plist[0].name : '', T.purple);
  card(9, 'Typical search', med.toFixed(1) + ' s', '9 in 10 under ' + (secs.length ? secs[Math.floor(secs.length * 0.9)].toFixed(0) : 0) + ' s', T.blue);
  card(11, 'Today', todayRows.length, todayRows.length ? todayFound + ' found (' + Math.round(100 * todayFound / todayRows.length) + '%)' : 'no searches yet', T.orange);

  // sections and charts
  function section(row, text, note) {
    sh.getRange(row, 1, 1, 12).merge().setValue(text).setFontSize(14).setFontWeight('bold').setFontColor(T.navy)
      .setBorder(null, null, true, null, null, null, T.orange, SpreadsheetApp.BorderStyle.SOLID_MEDIUM);
    sh.setRowHeight(row, 30);
    if (note) sh.getRange(row + 1, 1, 1, 12).merge().setValue(note).setFontSize(10).setFontColor(T.sub).setFontStyle('italic');
  }
  function chart(type, range, row, col, opts, height) {
    var b = sh.newChart().setChartType(type).addRange(range).setPosition(row, col, 6, 4)
      .setHiddenDimensionStrategy(Charts.ChartHiddenDimensionStrategy.SHOW_BOTH)
      .setOption('width', 588).setOption('height', height || 330).setOption('backgroundColor', T.card)
      .setOption('fontName', T.font).setOption('titleTextStyle', {fontSize: 14, bold: true, color: T.ink})
      .setOption('legend', {position: 'bottom', textStyle: {color: T.sub, fontSize: 11}})
      .setOption('hAxis', {textStyle: {color: T.sub}, gridlines: {color: '#eef0f4'}})
      .setOption('vAxis', {textStyle: {color: T.sub}, gridlines: {color: '#eef0f4'}});
    for (var k in opts) b = b.setOption(k, opts[k]);
    sh.insertChart(b.build());
  }
  var PIE = {pieHole: 0.55, pieSliceText: 'percentage', pieSliceTextStyle: {color: '#ffffff', fontSize: 12}, legend: {position: 'right', textStyle: {color: T.ink, fontSize: 12}}};
  function merge_(a, b) { var o = {}; for (var k in a) o[k] = a[k]; for (var j in b) o[j] = b[j]; return o; }

  section(10, 'Overview', 'Found = PuckTrace gave the report itself · Pick one = it showed possible games · Not found = nothing. Right-hand chart: answers compared with the report link in the RTT sheet.');
  chart(Charts.ChartType.PIE, dRes, 12, 1, merge_(PIE, {title: 'What PuckTrace answered', colors: [T.found, T.pick, T.none]}));
  chart(Charts.ChartType.PIE, dAcc, 12, 7, merge_(PIE, {title: checked ? 'Were the answers right?  ' + (100 * ac[0] / checked).toFixed(1) + '% correct' : 'Were the answers right?',
        colors: [T.found, T.wrong, T.none, T.sky, '#cbd5e1', T.grey]}));

  section(29, 'Trend', 'Searches each day by result, and the share PuckTrace found by itself.');
  chart(Charts.ChartType.COLUMN, dDay, 31, 1, {title: 'Searches per day', isStacked: true, colors: [T.found, T.pick, T.none]});
  chart(Charts.ChartType.LINE, dRate, 31, 7, {title: 'Found automatically, % per day', colors: [T.found], lineWidth: 3, pointSize: 6, curveType: 'function',
        vAxis: {minValue: 0, maxValue: 100, textStyle: {color: T.sub}, gridlines: {color: '#eef0f4'}}, legend: {position: 'none'}});

  section(48, 'People and websites', 'Who searches most (split by result), and the websites PuckTrace took the reports from.');
  var hP = Math.max(330, 30 * top.length + 90), hS = Math.max(330, 30 * slist.length + 90), hRow = Math.ceil(Math.max(hP, hS) / 21) + 2;
  chart(Charts.ChartType.BAR, dPpl, 50, 1, {title: 'Who uses PuckTrace most', isStacked: true, colors: [T.found, T.pick, T.none]}, hP);
  chart(Charts.ChartType.BAR, dSite, 50, 7, {title: 'Where the reports were found', colors: [T.blue], legend: {position: 'none'}}, hS);

  var r = 50 + hRow;
  section(r, 'Speed and busy hours', 'Most answers come from PuckTrace\'s own game list in seconds; games looked up live on league sites take longer.');
  chart(Charts.ChartType.COLUMN, dSpeed, r + 2, 1, {title: 'How long searches take', colors: [T.teal], legend: {position: 'none'}});
  chart(Charts.ChartType.COLUMN, dHour, r + 2, 7, {title: 'Busiest hours (IST)', colors: [T.purple], legend: {position: 'none'}});
  r += 19;

  // tables
  function table(row, title, cols, body) {
    section(row, title);
    row += 1;
    var c = 1;
    cols.forEach(function (col) {
      sh.getRange(row, c, 1, col.w).merge().setValue(col.h).setBackground(T.navy).setFontColor('#ffffff').setFontWeight('bold').setFontSize(10);
      c += col.w;
    });
    body.forEach(function (vals, i) {
      var cc = 1, rr = row + 1 + i;
      cols.forEach(function (col, j) {
        var cell = sh.getRange(rr, cc, 1, col.w).merge().setBackground(i % 2 ? '#f8fafc' : T.card).setFontSize(10);
        var v = vals[j];
        if (typeof v === 'string' && v.charAt(0) === '=') cell.setFormula(v); else cell.setValue(v);
        if (col.color) cell.setFontColor(col.color(v)).setFontWeight('bold');
        cc += col.w;
      });
    });
    sh.getRange(row, 1, body.length + 1, 12).setBorder(true, true, true, true, null, true, T.line, SpreadsheetApp.BorderStyle.SOLID);
    return row + body.length + 2;
  }
  function bar(v, color) { return '=SPARKLINE(' + v + ',{"charttype","bar";"max",1;"color1","' + color + '"})'; }
  var resColor = function (v) { return v === 'Found' ? T.found : v === 'Pick one' ? T.pick : T.none; };

  r = table(r, 'Leaderboard', [{h: '#', w: 1}, {h: 'Person', w: 3}, {h: 'Searches', w: 1}, {h: 'Found', w: 1}, {h: 'Found rate', w: 3},
            {h: 'Avg time', w: 1}, {h: 'Last search', w: 2}],
            plist.slice(0, 15).map(function (p, i) {
              var rate = p.n ? p['Found'] / p.n : 0;
              return [i + 1, p.name, p.n, p['Found'], bar(rate.toFixed(3), rate >= 0.7 ? T.found : rate >= 0.5 ? T.pick : T.none),
                      p.sn ? (p.s / p.sn).toFixed(1) + ' s' : '', fmt_(p.last, 'dd MMM HH:mm')];
            }));
  var hdr = function (x) { return x.length > 95 ? x.slice(0, 92) + '…' : x; };
  r = table(r + 1, 'Slowest searches', [{h: 'Header', w: 7}, {h: 'Seconds', w: 1}, {h: 'Result', w: 1, color: resColor}, {h: 'Who', w: 2}, {h: 'When', w: 1}],
            timed.slice().sort(function (a, b) { return b.secs - a.secs; }).slice(0, 10)
              .map(function (x) { return [hdr(x.header), x.secs, x.res, x.name, fmt_(x.t, 'dd MMM HH:mm')]; }));
  r = table(r + 1, 'Quickest reports found', [{h: 'Header', w: 7}, {h: 'Seconds', w: 1}, {h: 'Result', w: 1, color: resColor}, {h: 'Who', w: 2}, {h: 'When', w: 1}],
            timed.filter(function (x) { return x.res === 'Found'; }).sort(function (a, b) { return a.secs - b.secs; }).slice(0, 10)
              .map(function (x) { return [hdr(x.header), x.secs, x.res, x.name, fmt_(x.t, 'dd MMM HH:mm')]; }));
  var srv = {};
  rows.forEach(function (x) { srv[x.server] = (srv[x.server] || 0) + 1; });
  sh.getRange(r + 1, 1, 1, 12).merge().setValue('Servers: ' + Object.keys(srv).map(function (k) { return (k === 'nas' ? 'main (NAS) ' : k === 'render' ? 'backup (Render) ' : k + ' ') + Math.round(100 * srv[k] / n) + '%'; }).join('  ·  '))
    .setFontSize(10).setFontColor(T.sub);
  sh.setFrozenRows(3);
  ss.setActiveSheet(sh);
}

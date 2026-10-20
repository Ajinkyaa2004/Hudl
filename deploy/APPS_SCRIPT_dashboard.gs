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
var C = {found: '#2e9e5b', pick: '#f0a020', none: '#d9534f', blue: '#3b6fd4', grey: '#9aa3ad', wrong: '#8e2c2c'};

function when_(v) {
  if (v instanceof Date) return v;
  var m = String(v).match(/(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})/);
  return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
}
function day_(t) { return Utilities.formatDate(t, 'Asia/Kolkata', 'dd MMM'); }
function res_(r) {
  var x = String(r[7] || '').trim();
  if (x === 'Found' || (!x && String(r[3]).trim() === 'Yes')) return 'Found';
  if (x === 'Shortlist') return 'Pick one';
  return 'Not found';
}

function buildDashboard() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var log = logSheet_(ss);
  var data = log.getDataRange().getValues().slice(1).filter(function (r) { return r[2]; });
  var sh = ss.getSheetByName('Dashboard');
  if (!sh) sh = ss.insertSheet('Dashboard', 1);
  sh.getCharts().forEach(function (c) { sh.removeChart(c); });
  sh.getRange(1, 1, sh.getMaxRows(), sh.getMaxColumns()).breakApart();
  sh.clear();
  sh.setHiddenGridlines(true);
  sh.setColumnWidth(1, 300); sh.setColumnWidths(2, 6, 95);

  var rows = data.map(function (r) {
    return {t: when_(r[0]), name: String(r[1] || '').trim() || '(no name)', header: String(r[2]), res: res_(r),
            secs: parseFloat(r[6]), server: String(r[5] || ''), site: String(r[8] || ''), id: String(r[10] || '')};
  }).filter(function (r) { return r.t; });
  var n = rows.length, cnt = {'Found': 0, 'Pick one': 0, 'Not found': 0};
  rows.forEach(function (r) { cnt[r.res]++; });
  var timed = rows.filter(function (r) { return !isNaN(r.secs); }).map(function (r) { return r.secs; }).sort(function (a, b) { return a - b; });
  var avg = timed.length ? timed.reduce(function (a, b) { return a + b; }, 0) / timed.length : 0;
  var med = timed.length ? timed[Math.floor(timed.length / 2)] : 0;
  var people = {}; rows.forEach(function (r) { people[r.name.toLowerCase()] = 1; });

  var row = 1;
  function title(text, sub) {
    sh.getRange(row, 1).setValue(text).setFontSize(13).setFontWeight('bold').setFontColor('#1f3864');
    if (sub) sh.getRange(row + 1, 1, 1, 7).merge().setValue(sub).setFontColor('#666666').setFontStyle('italic').setWrap(true);
    row += sub ? 2 : 1;
  }
  function table(head, body, colors) {
    sh.getRange(row, 1, 1, head.length).setValues([head]).setFontWeight('bold').setBackground('#1f3864').setFontColor('#ffffff');
    if (body.length) sh.getRange(row + 1, 1, body.length, head.length).setValues(body);
    if (colors) colors.forEach(function (c, i) { if (c) sh.getRange(row + 1 + i, 1).setBackground(c).setFontColor('#ffffff').setFontWeight('bold'); });
    var r = sh.getRange(row, 1, body.length + 1, head.length);
    row += body.length + 2;
    return r;
  }
  function chart(type, range, opts, height) {
    var b = sh.newChart().setChartType(type).addRange(range).setPosition(range.getRow(), 9, 0, 0)
      .setOption('width', 620).setOption('height', height || 300).setOption('legend', {position: 'right'});
    for (var k in opts) b = b.setOption(k, opts[k]);
    sh.insertChart(b.build());
  }
  function pad(h) { row = Math.max(row, h); }

  // ---- headline
  sh.getRange(row, 1).setValue('PuckTrace: how searches went').setFontSize(18).setFontWeight('bold').setFontColor('#c45a00');
  sh.getRange(row + 1, 1).setValue('Updated ' + Utilities.formatDate(new Date(), 'Asia/Kolkata', 'dd MMM yyyy, HH:mm') + ' IST · refreshes every hour · menu PuckTrace > Refresh dashboard').setFontColor('#666666');
  row += 3;
  var kpi = [['Searches', n], ['Found automatically', n ? Math.round(100 * cnt['Found'] / n) + '%' : '-'],
             ['People using it', Object.keys(people).length], ['Average search time', avg.toFixed(1) + ' s'], ['Typical search time (median)', med.toFixed(1) + ' s']];
  sh.getRange(row, 1, kpi.length, 2).setValues(kpi);
  sh.getRange(row, 1, kpi.length, 1).setFontWeight('bold');
  sh.getRange(row, 2, kpi.length, 1).setFontSize(14).setFontColor('#1f3864').setHorizontalAlignment('left');
  row += kpi.length + 2;

  // ---- 1. results
  var top = row;
  title('1. What PuckTrace answered', 'Found = it gave the report itself. Pick one = it showed possible games to choose from. Not found = nothing found.');
  var r1 = table(['Result', 'Searches'], [['Found', cnt['Found']], ['Pick one', cnt['Pick one']], ['Not found', cnt['Not found']]], [C.found, C.pick, C.none]);
  chart(Charts.ChartType.PIE, r1, {title: 'What PuckTrace answered', colors: [C.found, C.pick, C.none], pieHole: 0.45});
  pad(top + 17);

  // ---- 2. accuracy
  top = row;
  title('2. Were the answers right?', 'Each game PuckTrace answered, compared with the report link the analyst put in the RTT sheet (updated when a new RTT sheet is processed).');
  var acc = ss.getSheetByName('Accuracy');
  var order = ['Correct (same game as the analyst)', 'Wrong (different game from the analyst)', 'Missed (a report existed, PuckTrace did not find it)',
               'Found on a different website from the analyst (not compared)',
               'Cannot check yet (analyst used a screenshot or has no link yet)', 'No report anywhere (the analyst found none either)'];
  var acol = [C.found, C.wrong, C.none, '#7aa6d9', C.grey, C.blue];
  var ac = {}; order.forEach(function (k) { ac[k] = 0; });
  if (acc) acc.getDataRange().getValues().slice(1).forEach(function (r) { if (ac[r[5]] !== undefined) ac[r[5]]++; });
  var r2 = table(['Verdict', 'Games'], order.map(function (k) { return [k, ac[k]]; }), acol);
  var checked = ac[order[0]] + ac[order[1]];
  sh.getRange(row - 1, 1).setValue(checked ? 'Accuracy where it can be checked: ' + (100 * ac[order[0]] / checked).toFixed(1) + '% (' + ac[order[0]] + ' of ' + checked + ')' : 'No accuracy data yet').setFontWeight('bold').setFontColor(C.found);
  row += 1;
  chart(Charts.ChartType.BAR, r2, {title: 'Answers checked against the analysts', colors: [C.blue], legend: {position: 'none'}});
  pad(top + 17);

  // ---- 3. per day
  top = row;
  title('3. Searches per day', 'Green = found, orange = pick one, red = not found.');
  var days = {}, dorder = [];
  rows.forEach(function (r) { var k = day_(r.t); if (!days[k]) { days[k] = {'Found': 0, 'Pick one': 0, 'Not found': 0}; dorder.push(k); } days[k][r.res]++; });
  var r3 = table(['Day', 'Found', 'Pick one', 'Not found'], dorder.slice(-21).map(function (k) { return [k, days[k]['Found'], days[k]['Pick one'], days[k]['Not found']]; }));
  chart(Charts.ChartType.COLUMN, r3, {title: 'Searches per day', isStacked: true, colors: [C.found, C.pick, C.none]});
  pad(top + 17);

  // ---- 4. people
  top = row;
  title('4. Who uses PuckTrace most', 'Searches per person, split by result. Avg time = average seconds per search for that person.');
  var ppl = {};
  rows.forEach(function (r) {
    var k = r.name.toLowerCase();
    var p = ppl[k] || (ppl[k] = {name: r.name, n: 0, 'Found': 0, 'Pick one': 0, 'Not found': 0, s: 0, sn: 0});
    p.n++; p[r.res]++; if (!isNaN(r.secs)) { p.s += r.secs; p.sn++; }
  });
  var plist = Object.keys(ppl).map(function (k) { return ppl[k]; }).sort(function (a, b) { return b.n - a.n; }).slice(0, 20);
  var r4 = table(['Person', 'Found', 'Pick one', 'Not found', 'Searches', 'Avg time (s)'],
                 plist.map(function (p) { return [p.name, p['Found'], p['Pick one'], p['Not found'], p.n, p.sn ? +(p.s / p.sn).toFixed(1) : '']; }));
  chart(Charts.ChartType.BAR, sh.getRange(r4.getRow(), 1, r4.getNumRows(), 4), {title: 'Searches per person', isStacked: true, colors: [C.found, C.pick, C.none]}, Math.max(300, 26 * plist.length + 80));
  pad(top + Math.max(17, plist.length + 6));

  // ---- 5. speed
  top = row;
  title('5. How long searches take', 'Most games come from PuckTrace\'s own game list in seconds; games it has to look up live on league sites take longer.');
  var buckets = [['Under 5 s', 0, 5], ['5 to 15 s', 5, 15], ['15 to 30 s', 15, 30], ['30 to 60 s', 30, 60], ['Over 60 s', 60, 1e9]];
  var r5 = table(['Search time', 'Searches'], buckets.map(function (b) { return [b[0], timed.filter(function (x) { return x >= b[1] && x < b[2]; }).length]; }));
  chart(Charts.ChartType.COLUMN, r5, {title: 'How long searches take', colors: [C.blue], legend: {position: 'none'}});
  pad(top + 17);

  // ---- 6. sites
  top = row;
  title('6. Where the reports were found', 'The websites PuckTrace found reports on (top 12).');
  var sites = {};
  rows.forEach(function (r) { if (r.res === 'Found' && r.site) sites[r.site] = (sites[r.site] || 0) + 1; });
  var slist = Object.keys(sites).map(function (k) { return [k, sites[k]]; }).sort(function (a, b) { return b[1] - a[1]; }).slice(0, 12);
  var r6 = table(['Website', 'Reports found'], slist);
  if (slist.length) chart(Charts.ChartType.BAR, r6, {title: 'Where the reports were found', colors: [C.found], legend: {position: 'none'}}, Math.max(300, 26 * slist.length + 80));
  pad(top + 19);

  // ---- 7. slowest / fastest
  title('7. Slowest searches', 'The 10 searches that took longest (usually games only on slow league sites).');
  var withT = rows.filter(function (r) { return !isNaN(r.secs); });
  var slow = withT.slice().sort(function (a, b) { return b.secs - a.secs; }).slice(0, 10);
  table(['Header', 'Seconds', 'Result', 'Who', 'When'], slow.map(function (r) { return [r.header, r.secs, r.res, r.name, Utilities.formatDate(r.t, 'Asia/Kolkata', 'dd MMM HH:mm')]; }));
  title('8. Quickest reports found', 'The 10 fastest searches that found the report (games already in PuckTrace\'s game list).');
  var fast = withT.filter(function (r) { return r.res === 'Found'; }).sort(function (a, b) { return a.secs - b.secs; }).slice(0, 10);
  table(['Header', 'Seconds', 'Result', 'Who', 'When'], fast.map(function (r) { return [r.header, r.secs, r.res, r.name, Utilities.formatDate(r.t, 'Asia/Kolkata', 'dd MMM HH:mm')]; }));

  // ---- 9. server
  top = row;
  title('9. Which server answered', 'nas = the main server at home; render = the backup (used when the main one is off or busy).');
  var srv = {};
  rows.forEach(function (r) { var k = r.server || 'unknown'; srv[k] = (srv[k] || 0) + 1; });
  var r9 = table(['Server', 'Searches'], Object.keys(srv).map(function (k) { return [k, srv[k]]; }));
  chart(Charts.ChartType.PIE, r9, {title: 'Which server answered', colors: [C.blue, C.pick, C.grey], pieHole: 0.45});
  pad(top + 17);
  sh.getRange(1, 1, row, 6).setVerticalAlignment('middle');
  sh.getRange(1, 1, row, 1).setWrap(true);
}

/**
 * 看護業務量調査ツール — Google スプレッドシート連携
 *
 * サーバの集計API（/api/summary）と明細CSV（/api/export.csv）を取り込んで
 * シートに書き出す。開けば最新、時間トリガーで自動更新もできる。
 *
 * 導入手順は google/README.md を参照。APIキーはこのファイルに書かず、
 * スクリプトプロパティ NWT_API_KEY に入れること（コードを共有しても漏れない）。
 */

/** サーバのベースURL。末尾のスラッシュは付けない。 */
const NWT_BASE = 'https://ik1-106-59985.vs.sakura.ne.jp/nwt';

/** 自動更新の間隔（時間）。setupTrigger() で使う。 */
const NWT_REFRESH_HOURS = 6;

const SH_CONF = '設定';
const SH_SUMMARY = 'サマリ';
const SH_GROUP = '集計_大分類';
const SH_CAT = '集計_中分類';
const SH_SUB = '集計_小分類';
const SH_AI = 'AIベースライン';
const SH_DETAIL = '明細';


// ---------------------------------------------------------------------------
// メニュー
// ---------------------------------------------------------------------------
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('業務量調査')
    .addItem('今すぐ更新', 'refreshAll')
    .addSeparator()
    .addItem('初期セットアップ（シート作成）', 'setupSheets')
    .addItem('APIキーを登録', 'promptApiKey')
    .addItem('自動更新をONにする', 'setupTrigger')
    .addItem('自動更新をOFFにする', 'removeTrigger')
    .addToUi();
}


// ---------------------------------------------------------------------------
// 通信
// ---------------------------------------------------------------------------
function _apiKey_() {
  const k = PropertiesService.getScriptProperties().getProperty('NWT_API_KEY');
  if (!k) {
    throw new Error('APIキーが未登録です。メニュー［業務量調査 → APIキーを登録］から入れてください。');
  }
  return k;
}

/** 設定シートの条件を読む。戻り値は {query: APIに渡す文字列, label: 人が読む文字列}。 */
function _conditions_() {
  const sh = SpreadsheetApp.getActive().getSheetByName(SH_CONF);
  if (!sh) return { query: '', label: '（全期間・全病棟）' };
  const tz = SpreadsheetApp.getActive().getSpreadsheetTimeZone();
  const val = function (row) {
    const v = sh.getRange(row, 2).getValue();
    if (v === '' || v === null) return '';
    // 日付セルは Date になるので、APIが受け取る YYYY-MM-DD に直す
    if (Object.prototype.toString.call(v) === '[object Date]') {
      return Utilities.formatDate(v, tz, 'yyyy-MM-dd');
    }
    return String(v).trim();
  };
  const q = [], label = [];
  [[2, 'from', '開始'], [3, 'to', '終了'], [4, 'ward', '病棟'], [5, 'shift', '勤務帯']]
    .forEach(function (c) {
      const v = val(c[0]);
      if (!v) return;
      q.push(c[1] + '=' + encodeURIComponent(v));
      label.push(c[2] + ' ' + v);          // 表示用はエンコードしない生の値
    });
  return {
    query: q.length ? '?' + q.join('&') : '',
    label: label.length ? label.join(' / ') : '（全期間・全病棟）',
  };
}

function _fetch_(path, query) {
  const res = UrlFetchApp.fetch(NWT_BASE + path + (query || ''), {
    method: 'get',
    headers: { 'X-API-Key': _apiKey_() },
    muteHttpExceptions: true,
    followRedirects: false,
  });
  const code = res.getResponseCode();
  if (code === 401 || code === 403) {
    throw new Error('サーバに拒否されました（' + code + '）。APIキーが正しいか確認してください。');
  }
  if (code >= 300) {
    throw new Error('取得に失敗しました（HTTP ' + code + '）: ' + path);
  }
  return res.getContentText();
}


// ---------------------------------------------------------------------------
// 書き込み
// ---------------------------------------------------------------------------
function _sheet_(name) {
  const ss = SpreadsheetApp.getActive();
  return ss.getSheetByName(name) || ss.insertSheet(name);
}

/** ヘッダ＋データを一括で書き込む。前回より行が減っても残骸が残らないよう毎回クリアする。 */
function _write_(name, header, rows) {
  const sh = _sheet_(name);
  sh.clear();
  sh.getRange(1, 1, 1, header.length).setValues([header])
    .setFontWeight('bold').setBackground('#e8f0ee');
  if (rows.length) {
    sh.getRange(2, 1, rows.length, header.length).setValues(rows);
  }
  sh.setFrozenRows(1);
  sh.autoResizeColumns(1, header.length);
  return sh;
}


// ---------------------------------------------------------------------------
// メイン
// ---------------------------------------------------------------------------
function refreshAll() {
  const cond = _conditions_();
  const q = cond.query;
  const s = JSON.parse(_fetch_('/api/summary', q));

  // --- サマリ ---
  _write_(SH_SUMMARY,
    ['項目', '値'],
    [
      ['抽出条件', cond.label],
      ['総記録時間（分）', s.total_minutes],
      ['　勤務時間内（分）', s.total_in_minutes],
      ['　勤務時間外（分）', s.total_ot_minutes],
      ['総記録時間（時間）', Math.round(s.total_minutes / 6) / 10],
      ['延べ勤務数', s.n_sessions],
      ['記録区間数', s.n_intervals],
      ['1勤務あたり（分）', s.n_sessions ? Math.round(s.total_minutes / s.n_sessions * 10) / 10 : 0],
    ]);

  // --- 大分類 ---
  _write_(SH_GROUP,
    ['大分類', '合計(分)', '勤務時間内(分)', '勤務時間外(分)', '割合(%)'],
    (s.groups || []).map(function (g) {
      return [g.group, g.minutes, g.in_minutes, g.ot_minutes, g.pct];
    }));

  // --- 中分類 ---
  _write_(SH_CAT,
    ['大分類', '中分類', '合計(分)', '勤務時間内(分)', '勤務時間外(分)', '割合(%)'],
    (s.categories || []).map(function (c) {
      return [c.group, c.cat_label, c.minutes, c.in_minutes, c.ot_minutes, c.pct];
    }));

  // --- 小分類 ---
  _write_(SH_SUB,
    ['大分類', '中分類', '小分類', 'AIツール', '合計(分)', '勤務時間内(分)',
     '勤務時間外(分)', '割合(%)', '1勤務あたり(分)', '回数'],
    (s.subcategories || []).map(function (x) {
      return [x.group, x.cat_label, x.sub_label, x.ai_tool, x.minutes, x.in_minutes,
              x.ot_minutes, x.pct, x.min_per_session, x.count];
    }));

  // --- AI導入効果のベースライン ---
  _write_(SH_AI,
    ['AIツール', '中分類', '小分類', '合計(分)', '1勤務あたり(分)', '回数', '割合(%)'],
    (s.ai_baseline || []).map(function (x) {
      return [x.ai_tool, x.cat_label, x.sub_label, x.minutes, x.min_per_session, x.count, x.pct];
    }));

  // --- 明細（CSV） ---
  let csv = _fetch_('/api/export.csv', q);
  if (csv.charCodeAt(0) === 0xFEFF) csv = csv.slice(1);   // Excel向けBOMを除去
  const rows = Utilities.parseCsv(csv);
  if (rows.length) {
    _write_(SH_DETAIL, rows[0], rows.slice(1));
  }

  // --- 最終更新時刻 ---
  const conf = SpreadsheetApp.getActive().getSheetByName(SH_CONF);
  if (conf) {
    conf.getRange('B7').setValue(new Date());
    conf.getRange('B8').setValue(rows.length ? rows.length - 1 : 0);
  }
  SpreadsheetApp.getActive().toast('更新しました（明細 ' + Math.max(rows.length - 1, 0) + ' 行）', '業務量調査', 5);
}


// ---------------------------------------------------------------------------
// セットアップ
// ---------------------------------------------------------------------------
function setupSheets() {
  const sh = _sheet_(SH_CONF);
  sh.clear();
  sh.getRange('A1:B8').setValues([
    ['設定', '値（空欄なら絞り込みなし）'],
    ['開始日', ''],
    ['終了日', ''],
    ['病棟', ''],
    ['勤務帯', ''],
    ['接続先', NWT_BASE],
    ['最終更新', ''],
    ['明細の行数', ''],
  ]);
  sh.getRange('A1:B1').setFontWeight('bold').setBackground('#e8f0ee');
  sh.getRange('A6:B6').setFontColor('#777777');
  sh.getRange('B2:B3').setNumberFormat('yyyy-mm-dd');
  sh.getRange('B7').setNumberFormat('yyyy-mm-dd hh:mm');
  sh.setColumnWidth(1, 140);
  sh.setColumnWidth(2, 320);
  SpreadsheetApp.getUi().alert(
    '設定シートを作りました。\n\n' +
    '次に［APIキーを登録］を実行してから［今すぐ更新］を押してください。\n' +
    '開始日・終了日・病棟・勤務帯は空欄なら全件です。');
}

function promptApiKey() {
  const ui = SpreadsheetApp.getUi();
  const r = ui.prompt('APIキーの登録',
    'サーバのAPIキーを貼り付けてください。\n' +
    '（Excelの入力シート N7 に入れているものと同じ値です）',
    ui.ButtonSet.OK_CANCEL);
  if (r.getSelectedButton() !== ui.Button.OK) return;
  const key = r.getResponseText().trim();
  if (!key) { ui.alert('空でした。登録していません。'); return; }
  PropertiesService.getScriptProperties().setProperty('NWT_API_KEY', key);
  // 入力したキーがシート上に残らないよう、確認はサーバ応答だけで行う
  try {
    _fetch_('/api/summary', '');
    ui.alert('登録しました。サーバへの接続も確認できました。');
  } catch (e) {
    ui.alert('登録しましたが、接続確認に失敗しました:\n' + e.message);
  }
}

function setupTrigger() {
  removeTrigger();
  ScriptApp.newTrigger('refreshAll').timeBased().everyHours(NWT_REFRESH_HOURS).create();
  SpreadsheetApp.getUi().alert(NWT_REFRESH_HOURS + '時間ごとの自動更新をONにしました。');
}

function removeTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'refreshAll') ScriptApp.deleteTrigger(t);
  });
}

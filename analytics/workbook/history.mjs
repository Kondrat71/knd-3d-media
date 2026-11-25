import { mergeLedger } from './merge-ledger.mjs';

function iso(value) {
  if (typeof value === 'number') return new Date((value-25569)*86400000).toISOString().slice(0,10);
  if (value instanceof Date) return value.toISOString().slice(0,10);
  return String(value ?? '').slice(0,10);
}

function clean(value) { return value === '' || value === undefined ? null : value; }

export function readPriorOperations(sheet) {
  const used = sheet.getUsedRange();
  const last = Math.max(8, (used?.rowIndex ?? 0) + (used?.rowCount ?? 0));
  const saved = sheet.getRange(`A8:AA${last}`).values;
  const rows = [];
  const revisions = [];
  for (const data of saved) {
    if (data[10]) rows.push({
      date: iso(data[0]), post: data[1] || '', offer: clean(data[2]), qty: clean(data[3]),
      kind: data[4], amt: Math.round((Number(data[5])-Number(data[6]))*100)/100,
      id: String(data[9]), key: String(data[10]), type: String(data[11] ?? ''), sale: clean(data[12]),
      localCost: clean(data[13]), tax: clean(data[14]), labor: clean(data[15]),
      wear: clean(data[16]), other: clean(data[17]), asof: data[18], note: data[19],
      verified: !String(data[19]).includes('не распределён'), withdrawn: data[20] === 'снят',
      sourceDay: iso(data[21]) || iso(data[0]),
    });
    if (data[22]) revisions.push(data.slice(22,27));
  }
  return { rows, revisions };
}

// Call before building the summary: coverage is derived from successful current
// source replies; historical dates are retained only in the settlement ledger.
export function withHistory(ledger, priorSheet, live, confirmedRemoved = []) {
  const old = readPriorOperations(priorSheet);
  const byId = new Map();
  for (const [day, payload] of Object.entries(live.finance?.by_day ?? {})) {
    const accruals = Array.isArray(payload) ? payload : payload?.accruals;
    if (Array.isArray(accruals)) for (const accrual of accruals) byId.set(String(accrual.accrual_id), day);
  }
  const incoming = ledger.out.map(r => ({ ...r, sourceDay: byId.get(r.id) ?? r.date }));
  const merged = mergeLedger(old.rows, incoming, { asof: live.asof, coveredDays: ledger.covered, confirmedRemoved });
  const revisions = [...old.revisions, ...merged.revisions.map(r => [
    r.asof, r.key, r.reason, JSON.stringify(r.before), JSON.stringify({ ...r.after, ...r.historicalCosts }),
  ])];
  return { ...ledger, out: merged.rows, revisions, missing: merged.missing };
}

// Call after writing the operations sheet; all historical amounts N:R are
// written by the standard builder from the records supplied by withHistory.
export function finishOperations(sheet, ledger, live) {
  sheet.getRange('U7:AA7').values = [[
    'Статус компонента','Дата выборки Ozon','Дата изменения','Ключ исправленной строки',
    'Событие','Прежняя запись','Новая запись и зафиксированные расходы',
  ]];
  sheet.getRange('U:V').format.columnWidth = 19;
  sheet.getRange('W:AA').format.columnWidth = 32;
  const missing = new Set(ledger.missing ?? []);
  if (ledger.out.length) sheet.getRange(`S8:V${7+ledger.out.length}`).values = ledger.out.map(r => [
    r.asof ?? live.asof, r.note || (r.verified ? 'факт Ozon' : 'факт Ozon · состав не распределён'),
    r.withdrawn ? 'снят' : missing.has(r.key) ? 'сверить с отчётом' : 'активен', r.sourceDay ?? r.date,
  ]);
  if (ledger.revisions?.length) sheet.getRange(`W8:AA${7+ledger.revisions.length}`).values = ledger.revisions;
}

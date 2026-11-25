// Forecast rates, manual counts, printer availability and test notes belong to
// the owner. Match them by business key, even when catalog order changes.
function put(sheet, row, col, v) {
  sheet.getCell(row, col).values = [[v ?? null]];
}

function byKey(sheet, firstRow, lastRow, keyColumn, scopeWidth) {
  const data = sheet.getRangeByIndexes(firstRow - 1, keyColumn, lastRow - firstRow + 1, scopeWidth).values;
  const found = new Map();
  for (const [i, values] of data.entries()) {
    if (values[0] == null || values[0] === '') continue;
    const key = String(values[0]);
    if (!found.has(key)) found.set(key, []);
    found.get(key).push({ row: firstRow + i - 1, values });
  }
  return found;
}

function preserveRates(before, after) {
  const prior = byKey(before, 6, 34, 35, 5);
  const fresh = byKey(after, 6, 34, 35, 5);
  for (const [key, [old]] of prior) {
    const current = fresh.get(key)?.[0];
    if (!current) throw Error(`Input key disappeared: ${key}`);
    // The workbook is the source of record for user-supplied rates and their
    // effective dates. A change requires an explicit edit to that workbook.
    for (const offset of [1,3,4]) put(after, current.row, 35+offset, old.values[offset]);
  }
}

function preserveKeyedInputs(before, after, first, last, fields, width, destinationLast=last) {
  const prior = byKey(before, first, last, 0, width);
  const fresh = byKey(after, first, destinationLast, 0, width);
  for (const [key, [old]] of prior) {
    const current = fresh.get(key)?.[0];
    const present = fields.filter(col=>old.values[col]!==null && old.values[col]!==undefined && old.values[col]!=='');
    if (!current && present.length) throw Error(`Retain input for changed or retired key: ${key}`);
    if (!current) continue;
    for (const n of fields) {
      const source = before.getCell(old.row,n), target = after.getCell(current.row,n);
      if (!source.formulas[0][0] && !target.formulas[0][0]) put(after,current.row,n,old.values[n]);
    }
  }
}

function preserveProductInputs(before, after) {
  preserveKeyedInputs(before,after,7,13,[18,19,20],21);
  preserveKeyedInputs(before,after,47,50,[2,3],4);
  preserveKeyedInputs(before,after,54,58,[1],2);
  preserveKeyedInputs(before,after,66,68,[1],2);
  const end = sheet=>Math.max(77,sheet.getUsedRange().rowIndex+sheet.getUsedRange().rowCount);
  preserveKeyedInputs(before,after,77,end(before),[18,19,20],21,end(after));
}

function copyManualSheet(before, after) {
  const range = before.getUsedRange();
  const target = after.getRangeByIndexes(range.rowIndex, range.columnIndex, range.rowCount, range.columnCount);
  target.copyFrom(range, 'all');
}

export function preserveEdits(prior, next) {
  const get = (book, name) => book.worksheets.getItem(name);
  const products = 'Товары и себестоимость';
  preserveRates(get(prior, products), get(next, products));
  preserveProductInputs(get(prior, products), get(next, products));
  const oldPrinters=get(prior,'Принтеры и загрузка'), newPrinters=get(next,'Принтеры и загрузка');
  preserveKeyedInputs(oldPrinters,newPrinters,7,10,[1,2,3,4,5,6,7,11],12);
  // Daily finance refreshes must not replace the current research, sources or
  // decisions with the initial research seed. Weekly analysis updates these
  // two live sheets after the daily pass, matching ideas by name and tests by SKU.
  for (const name of ['Идеи','Тесты']) copyManualSheet(get(prior,name),get(next,name));
}

// The importer supplies complete, de-paginated Ozon daily results. The ledger
// keeps one active row per settlement/component key and a separate revision list.
const sourceFields = ['date', 'post', 'offer', 'qty', 'kind', 'amt', 'sale', 'type', 'id', 'key'];
const frozenFields = ['localCost', 'tax', 'labor', 'wear', 'other'];
const selected = (row, fields) => Object.fromEntries(fields.map(k => [k, row[k] ?? null]));
const canonical = row => JSON.stringify(selected(row, sourceFields));

function indexRows(rows, name) {
  const indexed = new Map();
  for (const row of rows) {
    if (!row.key || !row.id || !/^\d{4}-\d{2}-\d{2}$/.test(row.date) || !Number.isFinite(row.amt)) {
      throw Error(`Incomplete ${name} settlement component`);
    }
    const previous = indexed.get(row.key);
    if (previous && (name === 'existing' || canonical(previous) !== canonical(row))) {
      throw Error(`Conflicting ${name} settlement key: ${row.key}`);
    }
    indexed.set(row.key, row);
  }
  return indexed;
}

export function mergeLedger(existing, incoming, { asof, coveredDays, confirmedRemoved = [] }) {
  if (!asof) throw Error('Missing as-of timestamp');
  const old = indexRows(existing, 'existing');
  const fresh = indexRows(incoming, 'incoming');
  const completeDays = new Set(coveredDays);
  const confirmed = new Set(confirmedRemoved);
  const parentIds = new Set(incoming.map(r => r.id));
  const revisions = [];
  const missing = [];
  const rows = [];
  const recordChange = (before, after, reason) => revisions.push({
    asof, key: before.key, reason, before: selected(before, sourceFields),
    after: selected(after, sourceFields), historicalCosts: selected(before, frozenFields),
  });

  for (const before of old.values()) {
    const next = fresh.get(before.key);
    if (!next) {
      // A missing day, a failed query and a row outside the rolling window
      // never prove deletion. The day must be known complete and the parent
      // revised, or a separate authoritative report must identify the removal.
      const completeParent = parentIds.has(before.id) && completeDays.has(before.sourceDay ?? before.date);
      if (before.withdrawn || !(completeParent || confirmed.has(before.key))) {
        if (!before.withdrawn && completeDays.has(before.sourceDay ?? before.date)) missing.push(before.key);
        rows.push(before);
        continue;
      }
      const removed = { ...before, amt: 0, qty: null, sale: null, asof, withdrawn: true };
      recordChange(before, removed, 'Компонент снят источником');
      rows.push(removed);
      continue;
    }
    fresh.delete(before.key);
    if (!before.withdrawn && canonical(before) === canonical(next)) {
      rows.push(before);
      continue;
    }
    // Product labels can fail to resolve in a partial catalog pull. The caller
    // supplies the last known mapping or rejects the dependent feed first.
    const updated = { ...before, ...next, ...selected(before, frozenFields), asof, withdrawn: false };
    if (before.note !== undefined) updated.note = before.note;
    recordChange(before, updated, before.withdrawn ? 'Восстановлен источником' : 'Корректировка Ozon');
    rows.push(updated);
  }
  for (const next of fresh.values()) rows.push({ ...next, asof });
  return { rows, revisions, missing };
}

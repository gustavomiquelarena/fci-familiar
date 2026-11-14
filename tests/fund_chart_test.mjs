import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
const source = await readFile(new URL('../docs/fund-chart.js', import.meta.url), 'utf8');
const { filterHistory, periodChange, renderChart } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const points = [
  { date: '2026-11-14', unit_value: '1.5' },
  { date: '2024-11-13', unit_value: '0.9' },
  { date: '2024-11-14', unit_value: '1' },
  { date: '2025-11-13', unit_value: '1.1' },
  { date: '2025-11-14', unit_value: '1.2' },
  { date: '2025-12-31', unit_value: '1.25' },
  { date: '2026-01-01', unit_value: '1.3' },
  { date: '2026-08-13', unit_value: '1.35' },
  { date: '2026-08-14', unit_value: '1.4' },
];
const original = JSON.stringify(points);
for (const [range, first] of [['3m', '2026-08-14'], ['1y', '2025-11-14'], ['ytd', '2026-01-01'], ['2y', '2024-11-14'], ['all', '2024-11-13']]) {
  const filtered = filterHistory(points, range);
  assert.equal(filtered[0].date, first, range);
  assert.equal(filtered.at(-1).date, '2026-11-14');
}
assert.equal(JSON.stringify(points), original, 'Filtering leaves original report intact');
assert.deepEqual(filterHistory(points, '90'), filterHistory(points, '3m'));
assert.deepEqual(filterHistory(points, '365'), filterHistory(points, '1y'));
assert.deepEqual(filterHistory(points, '730'), filterHistory(points, '2y'));
assert.equal(filterHistory([{ date: '2025-02-28' }, { date: '2025-05-31' }], '3m')[0].date, '2025-02-28');
assert.equal(filterHistory([{ date: '2023-02-28' }, { date: '2024-02-29' }], '1y')[0].date, '2023-02-28');
assert.deepEqual(filterHistory([{ date: 'invalid' }, { date: '2026-02-30' }]), []);
assert.equal(periodChange([]), null);
assert.equal(periodChange([{ date: '2026-11-14', unit_value: '1' }]), null);
assert.equal(periodChange([{ date: '2026-01-01', unit_value: '1' }, { date: '2026-11-14', unit_value: '1.5' }]), 50);
assert.equal(periodChange([{ date: '2026-01-01', unit_value: '0' }, { date: '2026-11-14', unit_value: '1.5' }]), null);
assert.match(renderChart(filterHistory(points, '3m'), 'unit_value', true), /Valor de cuotaparte desde 14/);
assert.doesNotMatch(renderChart([{ date: '2026-11-14', unit_value: null }], 'unit_value', true), /<svg/);
assert.doesNotMatch(renderChart([{ date: '2026-11-14', unit_value: '1.5' }], 'unit_value', true), /NaN|Infinity/);
console.log('Gráfico: filtros calendario, año en curso, fin de mes, bisiesto, variación y datos vacíos verificados.');

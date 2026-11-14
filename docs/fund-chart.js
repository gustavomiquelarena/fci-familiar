/** Date filtering and SVG charts shared by the family view and private admin. */
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

function parseDate(value) {
  const key = String(value ?? '').slice(0, 10);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return null;
  const date = new Date(`${key}T12:00:00Z`);
  return Number.isNaN(date.valueOf()) || date.toISOString().slice(0, 10) !== key ? null : date;
}

function dateLabel(value, options = { day: '2-digit', month: 'short', year: 'numeric' }) {
  const date = parseDate(value);
  return date ? date.toLocaleDateString('es-AR', { ...options, timeZone: 'UTC' }) : 'Sin fecha';
}

function monthsBefore(end, months) {
  const result = new Date(end);
  const day = result.getUTCDate();
  result.setUTCDate(1);
  result.setUTCMonth(result.getUTCMonth() - months);
  const lastDay = new Date(Date.UTC(result.getUTCFullYear(), result.getUTCMonth() + 1, 0)).getUTCDate();
  result.setUTCDate(Math.min(day, lastDay));
  return result;
}

/** Calendar ranges end at the latest available business date, not the PC clock. */
export function filterHistory(points, range = 'all') {
  const series = [...points].filter(point => parseDate(point.date))
    .sort((a, b) => String(a.date).localeCompare(String(b.date)));
  if (!series.length) return series;
  const selected = ({ '90': '3m', '365': '1y', '730': '2y' })[range] || range;
  const end = parseDate(series.at(-1).date);
  let start;
  if (selected === 'ytd') start = new Date(Date.UTC(end.getUTCFullYear(), 0, 1, 12));
  else if (selected === '3m') start = monthsBefore(end, 3);
  else if (selected === '1y') start = monthsBefore(end, 12);
  else if (selected === '2y') start = monthsBefore(end, 24);
  else return series;
  return series.filter(point => parseDate(point.date).valueOf() >= start.valueOf());
}

export function periodChange(points, field = 'unit_value') {
  const series = filterHistory(points).filter(point => Number.isFinite(Number(point[field])) && Number(point[field]) > 0);
  return series.length > 1 ? (Number(series.at(-1)[field]) / Number(series[0][field]) - 1) * 100 : null;
}

function chart(points, field, id, small = false) {
  const series = filterHistory(points).filter(point => point[field] !== null && point[field] !== '' && Number.isFinite(Number(point[field])));
  if (!series.length) return '<div class="chart-empty">La evolución se mostrará cuando haya cierres disponibles.</div>';
  const width = 620; const height = small ? 185 : 245;
  const left = 57; const right = 20; const top = 17; const bottom = 38;
  const values = series.map(point => Number(point[field]));
  const minValue = Math.min(...values); const maxValue = Math.max(...values);
  const padding = Math.max((maxValue - minValue) * .18, Math.abs(maxValue) * .025, .01);
  const min = Math.max(0, minValue - padding); const max = maxValue + padding;
  const stamps = series.map(point => parseDate(point.date).valueOf());
  const start = Math.min(...stamps); const end = Math.max(...stamps);
  const x = index => start === end ? (left + width - right) / 2 : left + (stamps[index] - start) / (end - start) * (width - left - right);
  const y = value => height - bottom - (value - min) / Math.max(max - min, .000001) * (height - top - bottom);
  const coords = values.map((value, index) => `${x(index).toFixed(2)},${y(value).toFixed(2)}`);
  const line = `M${coords.join(' L')}`;
  const area = `${line} L${x(values.length - 1)},${height - bottom} L${x(0)},${height - bottom} Z`;
  const labels = Array.from({ length: 4 }, (_, index) => {
    const value = min + (max - min) * index / 3;
    const label = small ? new Intl.NumberFormat('es-AR', { maximumFractionDigits: 3 }).format(value) : new Intl.NumberFormat('es-AR', { notation: Math.abs(value) >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 0 }).format(value);
    return `<line x1="${left}" y1="${y(value)}" x2="${width - right}" y2="${y(value)}" stroke="#dfe7df" stroke-dasharray="3 5"/><text x="${left - 11}" y="${y(value) + 3}" text-anchor="end" class="chart-text">${escapeHtml(label)}</text>`;
  }).join('');
  const circle = `<circle cx="${x(values.length - 1)}" cy="${y(values.at(-1))}" r="4.5" fill="#15776d" stroke="#fff" stroke-width="2"/>`;
  const firstDate = dateLabel(series[0].date, { day: '2-digit', month: 'short', year: '2-digit' });
  const lastDate = dateLabel(series.at(-1).date, { day: '2-digit', month: 'short', year: '2-digit' });
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${small ? 'Valor de cuotaparte' : 'Saldo en USD MEP'} desde ${escapeHtml(firstDate)} hasta ${escapeHtml(lastDate)}"><defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#8ab6a2" stop-opacity=".28"/><stop offset="100%" stop-color="#8ab6a2" stop-opacity=".02"/></linearGradient></defs>${labels}<path d="${area}" fill="url(#${id})"/><path d="${line}" stroke="#15776d" stroke-width="2.5" fill="none" stroke-linejoin="round" stroke-linecap="round"/>${circle}<text x="${left}" y="${height - 10}" class="chart-text">${escapeHtml(firstDate)}</text>${series.length > 1 ? `<text x="${width - right}" y="${height - 10}" text-anchor="end" class="chart-text">${escapeHtml(lastDate)}</text>` : ''}</svg>`;
}

export function renderChart(points, field = 'balance_usd', small = false) {
  return chart(points, field, small ? 'unit-gradient' : 'balance-gradient', small);
}


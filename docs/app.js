const numberFormat = new Intl.NumberFormat('es-AR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const unitFormat = new Intl.NumberFormat('es-AR', { minimumFractionDigits: 4, maximumFractionDigits: 6 });
const percentFormat = new Intl.NumberFormat('es-AR', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const tones = ['#15776d', '#659286', '#9ab6a0', '#c7aa74', '#607f91', '#b5c6bd', '#8caaac'];
const numeric = value => Number.isFinite(Number(value)) ? Number(value) : 0;
const money = value => numberFormat.format(numeric(value));
const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

function parseDate(value) {
  if (!value) return null;
  const text = String(value);
  const date = /^\d{4}-\d{2}-\d{2}$/.test(text) ? new Date(`${text}T12:00:00`) : new Date(text);
  return Number.isNaN(date.valueOf()) ? null : date;
}

function dateLabel(value, options = { day: '2-digit', month: 'short', year: 'numeric' }) {
  const date = parseDate(value);
  return date ? date.toLocaleDateString('es-AR', options) : 'Sin fecha';
}

function fromBase64(value) {
  if (typeof value !== 'string') throw new Error('Archivo inválido');
  return Uint8Array.from(atob(value), character => character.charCodeAt(0));
}

/** Same envelope as Python: AES-GCM ciphertext ends with its authentication tag. */
export async function decryptReport(envelope, password) {
  if (!globalThis.crypto?.subtle || envelope?.version !== 1 || envelope?.kdf !== 'PBKDF2-SHA256') throw new Error('Archivo no compatible');
  const iterations = Number(envelope.iterations);
  if (!Number.isInteger(iterations) || iterations < 100000 || iterations > 2000000) throw new Error('Parámetros no compatibles');
  const salt = fromBase64(envelope.salt);
  const iv = fromBase64(envelope.iv);
  const ciphertext = fromBase64(envelope.ciphertext);
  if (salt.byteLength < 16 || iv.byteLength !== 12 || ciphertext.byteLength < 16) throw new Error('Archivo inválido');
  const material = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveKey']);
  const key = await crypto.subtle.deriveKey({ name: 'PBKDF2', hash: 'SHA-256', salt, iterations }, material, { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
  const plaintext = await crypto.subtle.decrypt({ name: 'AES-GCM', iv, tagLength: 128 }, key, ciphertext);
  try {
    const report = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(plaintext));
    if (!report?.participant?.id || !Array.isArray(report.history) || !Array.isArray(report.movements) || !Array.isArray(report.allocation)) throw new Error('Reporte inválido');
    return report;
  } finally {
    new Uint8Array(plaintext).fill(0);
  }
}

function chart(points, field, id, small = false) {
  const series = points.filter(point => parseDate(point.date) && Number.isFinite(Number(point[field])));
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

if (typeof document !== 'undefined') {
  const $ = id => document.getElementById(id);
  let manifest = null;
  let currentReport = null;
  let working = false;
  let currentRange = 'all';

  function message(text, error = false) {
    $('login-message').textContent = text;
    $('login-message').classList.toggle('error', error);
  }

  function filteredHistory() {
    const history = [...(currentReport?.history || [])].sort((a, b) => String(a.date).localeCompare(String(b.date)));
    if (currentRange === 'all' || !history.length) return history;
    const end = parseDate(history.at(-1).date)?.valueOf();
    return history.filter(point => parseDate(point.date)?.valueOf() >= end - Number(currentRange) * 86400000);
  }

  function renderHistory() {
    const history = filteredHistory();
    $('balance-chart').innerHTML = chart(history, 'balance_usd', 'balance-gradient');
    $('unit-chart').innerHTML = chart(history, 'unit_value', 'unit-gradient', true);
    const valid = history.filter(point => numeric(point.unit_value) > 0);
    const change = valid.length > 1 ? (numeric(valid.at(-1).unit_value) / numeric(valid[0].unit_value) - 1) * 100 : null;
    $('unit-return').textContent = change === null ? 'Se necesita más de un cierre para comparar la evolución.' : `${change >= 0 ? '+' : ''}${percentFormat.format(change)}% de variación del valor de cuotaparte en el período mostrado.`;
    document.querySelectorAll('[data-range]').forEach(button => {
      const active = button.dataset.range === currentRange;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  function renderAllocation() {
    const positions = [...currentReport.allocation].sort((a, b) => numeric(b.value_usd) - numeric(a.value_usd));
    const total = positions.reduce((sum, position) => sum + numeric(position.value_usd), 0);
    if (!positions.length || total <= 0) {
      $('allocation').innerHTML = '<p class="empty-state">El detalle de la cartera estará disponible después de la próxima descarga de posiciones.</p>';
      return;
    }
    $('allocation').innerHTML = positions.map((position, index) => {
      const weight = Math.max(0, Math.min(100, numeric(position.value_usd) / total * 100));
      const tone = tones[index % tones.length];
      return `<div class="allocation-item"><div class="allocation-label"><div class="allocation-name"><i class="allocation-dot" style="background:${tone}" aria-hidden="true"></i><strong title="${escapeHtml(position.name || position.symbol)}">${escapeHtml(position.symbol || position.name)}</strong><small>${escapeHtml(position.category || '')}</small></div><span>${percentFormat.format(weight)}%</span></div><div class="allocation-track"><div class="allocation-fill" style="width:${weight.toFixed(3)}%;background:${tone}"></div></div></div>`;
    }).join('');
  }

  function renderMovements() {
    const movements = [...currentReport.movements].sort((a, b) => String(b.date).localeCompare(String(a.date)));
    $('movement-count').textContent = `${movements.length} ${movements.length === 1 ? 'movimiento' : 'movimientos'}`;
    $('movements').innerHTML = movements.map(movement => {
      const redemption = ['redemption', 'withdrawal', 'rescate'].includes(String(movement.type).toLowerCase()) || numeric(movement.amount_usd) < 0;
      const label = movement.type === 'reversal' ? 'Reversión' : redemption ? 'Retiro' : 'Aporte';
      return `<tr><td>${escapeHtml(dateLabel(movement.date))}</td><td><span class="movement-badge${redemption ? ' withdrawal' : ''}">${label}</span></td><td class="movement-note">${escapeHtml(movement.note || '—')}</td><td class="numeric ${redemption ? 'negative' : 'positive'}">${redemption ? '−' : '+'}${money(Math.abs(numeric(movement.amount_usd)))}</td><td class="numeric">${unitFormat.format(Math.abs(numeric(movement.units)))}</td></tr>`;
    }).join('');
    $('no-movements').hidden = movements.length !== 0;
  }

  function renderReport(report) {
    currentReport = report;
    currentRange = 'all';
    $('dashboard-title').textContent = `Hola, ${report.participant.name}.`;
    $('as-of').textContent = dateLabel(report.as_of);
    $('quote-as-of').textContent = report.quote_as_of ? `Cierre MEP: ${dateLabel(report.quote_as_of)}` : '';
    $('quote-as-of').hidden = !report.quote_as_of;
    $('balance').textContent = money(report.balance_usd);
    $('net-contributions').textContent = money(numeric(report.contributions_usd) - numeric(report.withdrawals_usd));
    $('profit').textContent = `${numeric(report.profit_usd) < 0 ? '−' : ''}${money(Math.abs(numeric(report.profit_usd)))}`;
    $('profit-container').classList.toggle('negative', numeric(report.profit_usd) < 0);
    $('profit-container').classList.toggle('positive', numeric(report.profit_usd) >= 0);
    $('contribution-caption').textContent = `Aportes ${money(report.contributions_usd)} · Retiros ${money(report.withdrawals_usd)}`;
    $('unit-value').textContent = `USD ${unitFormat.format(numeric(report.unit_value))}`;
    $('unit-count').textContent = `Tenés ${unitFormat.format(numeric(report.units))} cuotapartes`;
    renderHistory(); renderAllocation(); renderMovements();
    $('login-view').hidden = true;
    $('dashboard').hidden = false;
    $('logout').hidden = false;
    $('header-note').hidden = true;
    window.scrollTo({ top: 0, behavior: 'instant' });
    $('dashboard-title').setAttribute('tabindex', '-1');
    $('dashboard-title').focus({ preventScroll: true });
  }

  function logout() {
    currentReport = null;
    $('password').value = '';
    $('password').type = 'password';
    $('show-password').textContent = 'Ver';
    $('show-password').setAttribute('aria-pressed', 'false');
    $('show-password').setAttribute('aria-label', 'Mostrar contraseña');
    ['movements', 'allocation', 'balance-chart', 'unit-chart'].forEach(id => { $(id).replaceChildren(); });
    ['dashboard-title', 'as-of', 'quote-as-of', 'balance', 'net-contributions', 'profit', 'unit-value', 'unit-count', 'unit-return', 'contribution-caption', 'movement-count'].forEach(id => { $(id).textContent = ''; });
    $('dashboard').hidden = true; $('login-view').hidden = false; $('logout').hidden = true; $('header-note').hidden = false;
    message('Elegí tu nombre para volver a entrar.');
    $('participant').focus();
  }

  $('show-password').addEventListener('click', () => {
    const show = $('password').type === 'password';
    $('password').type = show ? 'text' : 'password';
    $('show-password').textContent = show ? 'Ocultar' : 'Ver';
    $('show-password').setAttribute('aria-label', show ? 'Ocultar contraseña' : 'Mostrar contraseña');
    $('show-password').setAttribute('aria-pressed', String(show));
  });
  $('logout').addEventListener('click', logout);
  $('participant').addEventListener('change', () => { $('password').value = ''; message(''); });
  document.querySelectorAll('[data-range]').forEach(button => button.addEventListener('click', () => { currentRange = button.dataset.range; renderHistory(); }));
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (working || !manifest) return;
    const participant = manifest.participants.find(item => String(item.id) === $('participant').value);
    if (!participant) { message('Elegí tu nombre.', true); return; }
    let password = $('password').value;
    if (!password) return;
    $('password').value = '';
    working = true; $('login-submit').disabled = true; message('Abriendo tu reporte…');
    try {
      const url = new URL(participant.file, window.location.href);
      if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) throw new Error('Archivo no compatible');
      const response = await fetch(url, { cache: 'no-store', credentials: 'omit' });
      if (!response.ok) throw new Error('No disponible');
      const report = await decryptReport(await response.json(), password);
      if (String(report.participant.id) !== String(participant.id)) throw new Error('Reporte no compatible');
      renderReport(report);
    } catch {
      message('No pudimos abrir el reporte. Revisá tu contraseña e intentá de nuevo. Si sigue pasando, consultá al administrador.', true);
      $('password').focus();
    } finally {
      password = ''; working = false; $('login-submit').disabled = false;
    }
  });

  (async function loadManifest() {
    try {
      if (!crypto?.subtle) throw new Error('Este navegador necesita una conexión HTTPS para abrir los reportes.');
      const response = await fetch('data/manifest.json', { cache: 'no-store', credentials: 'omit' });
      if (!response.ok) throw new Error('Aún no hay reportes publicados. Volvé a visitar este espacio después de la primera actualización.');
      const data = await response.json();
      if (data.schema_version !== 1 || !Array.isArray(data.participants) || !data.participants.length || data.participants.some(item => !item.id || !item.name || !item.file)) throw new Error('Aún no hay reportes publicados.');
      manifest = data;
      $('participant').replaceChildren(new Option('Elegí tu nombre', ''), ...data.participants.map(item => new Option(item.name, String(item.id))));
      $('participant').disabled = false; $('password').disabled = false; $('login-submit').disabled = false;
      message('');
    } catch (error) {
      $('participant').replaceChildren(new Option('Sin reportes disponibles', ''));
      message(error.message || 'No se pudo cargar el reporte. Intentá nuevamente más tarde.', true);
    }
  })();
}

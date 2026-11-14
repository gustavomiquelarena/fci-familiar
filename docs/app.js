import { renderChart, filterHistory, periodChange } from './fund-chart.js?v=1';
export { renderChart };

const numberFormat = new Intl.NumberFormat('es-AR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const unitFormat = new Intl.NumberFormat('es-AR', { minimumFractionDigits: 4, maximumFractionDigits: 6 });
const exchangeFormat = new Intl.NumberFormat('es-AR', { minimumFractionDigits: 2, maximumFractionDigits: 6 });
const percentFormat = new Intl.NumberFormat('es-AR', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
const tones = ['#15776d', '#659286', '#9ab6a0', '#c7aa74', '#607f91', '#b5c6bd', '#8caaac'];
const categoryColors = {
  'Cash ARS': '#4b8690', 'Cash USD': '#86ad9c', Efectivo: '#86ad9c', 'Títulos públicos': '#be832e',
  'Obligaciones negociables': '#a85771', 'Acciones argentinas': '#526db1',
  CEDEAR: '#15776d', 'Fondos comunes': '#816b9c', 'Bonos y letras': '#8d7556',
};
const cashCategories = new Set(['Cash ARS', 'Cash USD', 'Efectivo']);
const numeric = value => Number.isFinite(Number(value)) ? Number(value) : 0;
const money = value => numberFormat.format(numeric(value));
const signedMoney = value => `${value < 0 ? '−' : value > 0 ? '+' : ''}${money(Math.abs(value))}`;
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

if (typeof document !== 'undefined') {
  const $ = id => document.getElementById(id);
  let manifest = null;
  let currentReport = null;
  let working = false;
  let currentRange = 'all';
  let unitRange = 'all';

  function message(text, error = false) {
    $('login-message').textContent = text;
    $('login-message').classList.toggle('error', error);
  }

  function renderHistory() {
    const history = filterHistory(currentReport?.history || [], currentRange);
    const units = filterHistory(currentReport?.history || [], unitRange);
    $('balance-chart').innerHTML = renderChart(history, 'balance_usd');
    $('unit-chart').innerHTML = renderChart(units, 'unit_value', true);
    const change = periodChange(units);
    $('unit-return').textContent = change === null ? 'Se necesita más de un cierre para comparar la evolución.' : `${change >= 0 ? '+' : ''}${percentFormat.format(change)}% de variación del valor de cuotaparte en el período mostrado.`;
    document.querySelectorAll('[data-range]').forEach(button => {
      const active = button.dataset.range === currentRange;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('[data-unit-range]').forEach(button => {
      const active = button.dataset.unitRange === unitRange;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  function renderAllocation() {
    const positions = [...currentReport.allocation].sort((a, b) => numeric(b.value_usd) - numeric(a.value_usd));
    renderAllocationChart(positions);
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

  function renderAllocationChart(positions) {
    const amounts = new Map();
    for (const position of positions) {
      const category = position.category || 'Sin clasificar';
      amounts.set(category, (amounts.get(category) || 0) + numeric(position.value_usd));
    }
    const groups = [...amounts].filter(([, value]) => value !== 0).sort((a, b) => b[1] - a[1]);
    if (!groups.length) {
      $('allocation-chart').innerHTML = '<p class="empty-state">Todavía no hay posiciones con saldo para mostrar.</p>';
      return;
    }
    const total = groups.reduce((sum, [, value]) => sum + value, 0);
    const cash = groups.reduce((sum, [category, value]) => sum + (cashCategories.has(category) ? value : 0), 0);
    const positiveTotal = groups.reduce((sum, [, value]) => sum + Math.max(value, 0), 0);
    const color = category => Object.hasOwn(categoryColors, category) ? categoryColors[category] : '#7b8b85';
    let start = 0;
    const slices = groups.filter(([, value]) => value > 0).map(([category, value]) => {
      const end = start + 100 * value / positiveTotal;
      const slice = `${color(category)} ${start}% ${end}%`;
      start = end;
      return slice;
    });
    const legend = groups.map(([category, value]) => `
      <li><i style="background-color:${color(category)}" aria-hidden="true"></i><span>${escapeHtml(category)}</span>
        <strong>${money(value)}</strong><span class="portfolio-share">${value > 0 ? `${percentFormat.format(100 * value / positiveTotal)}%` : '—'}</span></li>`).join('');
    $('allocation-chart').innerHTML = `
      <div class="portfolio-chart">
        <div class="portfolio-donut" style="${slices.length ? `background-image:conic-gradient(${slices.join(',')})` : ''}">
          <div><span>Total USD MEP</span><strong>${money(total)}</strong></div>
        </div>
        <div class="portfolio-detail">
          <dl class="portfolio-totals"><div><dt>Efectivo</dt><dd>${money(cash)}</dd></div>
            <div><dt>Inversiones</dt><dd>${money(total - cash)}</dd></div></dl>
          <ul class="portfolio-legend" aria-label="Distribución en USD MEP por clase">${legend}</ul>
        </div>
      </div>
      ${groups.some(([, value]) => value < 0) ? '<p class="chart-note">La torta muestra los saldos positivos. El total incluye los saldos negativos.</p>' : ''}`;
  }

  function renderMovements() {
    const movements = [...currentReport.movements].sort((a, b) => String(b.date).localeCompare(String(a.date)));
    $('movement-count').textContent = `${movements.length} ${movements.length === 1 ? 'movimiento' : 'movimientos'}`;
    $('movements').innerHTML = movements.map(movement => {
      const redemption = ['redemption', 'withdrawal', 'rescate'].includes(String(movement.type).toLowerCase()) || numeric(movement.amount_usd) < 0;
      const label = movement.type === 'reversal' ? 'Reversión' : redemption ? 'Retiro' : 'Aporte';
      const usd = numeric(movement.amount_usd);
      let originalCurrency = '';
      if (movement.currency === 'ARS') {
        const ars = movement.original_amount == null || movement.original_amount === ''
          ? '—' : signedMoney(Math.sign(usd) * Math.abs(numeric(movement.original_amount)));
        const rate = numeric(movement.fx) > 0 ? exchangeFormat.format(numeric(movement.fx)) : '—';
        originalCurrency = `<small>ARS ${ars}</small><small title="Tipo de cambio MEP en ARS por USD">TC MEP ${rate}</small>`;
      }
      return `<tr><td>${escapeHtml(dateLabel(movement.date))}</td><td><span class="movement-badge${redemption ? ' withdrawal' : ''}">${label}</span></td><td class="movement-note">${escapeHtml(movement.note || '—')}</td><td class="numeric movement-amount"><span class="${usd < 0 ? 'negative' : 'positive'}">USD ${signedMoney(usd)}</span>${originalCurrency}</td><td class="numeric">${unitFormat.format(Math.abs(numeric(movement.units)))}</td></tr>`;
    }).join('');
    $('no-movements').hidden = movements.length !== 0;
  }

  function renderReport(report) {
    currentReport = report;
    currentRange = 'all';
    unitRange = 'all';
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
    ['movements', 'allocation', 'allocation-chart', 'balance-chart', 'unit-chart'].forEach(id => { $(id).replaceChildren(); });
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
  document.querySelectorAll('[data-unit-range]').forEach(button => button.addEventListener('click', () => { unitRange = button.dataset.unitRange; renderHistory(); }));
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

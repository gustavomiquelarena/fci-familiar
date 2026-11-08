const $ = id => document.getElementById(id);
const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
const moneyFormat = new Intl.NumberFormat('es-AR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const unitFormat = new Intl.NumberFormat('es-AR', { minimumFractionDigits: 4, maximumFractionDigits: 6 });
const money = value => moneyFormat.format(number(value));
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const dateText = value => {
  if (!value) return '—';
  const text = String(value); const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(text) ? `${text}T12:00:00` : text);
  return Number.isNaN(d.valueOf()) ? text : d.toLocaleDateString('es-AR', { day: '2-digit', month: 'short', year: 'numeric' });
};
const titles = { resumen: 'Resumen', movimientos: 'Movimientos', usuarios: 'Usuarios y contraseñas', configuracion: 'Configuración', actividad: 'Actividad y respaldos' };
let state = null;
let busy = false;
let activeView = 'resumen';
let pendingSubmission = null;

function notify(message, kind = 'success') {
  $('notification').textContent = message;
  $('notification').className = `notification${kind === 'error' ? ' error' : kind === 'busy' ? ' busy' : ''}`;
  $('notification').hidden = false;
}

function setBusy(value, label = 'Procesando…') {
  busy = value;
  document.querySelectorAll('button').forEach(button => { button.disabled = value; });
  if (value) notify(label, 'busy');
  $('sync-button').innerHTML = value ? 'Procesando <i class="busy-indicator" aria-hidden="true"></i>' : 'Actualizar desde IOL <span aria-hidden="true">↻</span>';
}

async function api(path, payload, method = 'POST') {
  const options = { method, credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } };
  if (method !== 'GET') {
    options.headers['Content-Type'] = 'application/json';
    options.headers['X-CSRF-Token'] = state?.csrf_token || '';
    options.body = JSON.stringify(payload || {});
  }
  const response = await fetch(path, options);
  let body;
  try { body = await response.json(); } catch { throw new Error('El servidor no devolvió una respuesta válida. Revisá el registro de la aplicación.'); }
  if (!response.ok || body.ok === false) {
    const detail = body.error || body.detail || `No se pudo completar la acción (${response.status}).`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body.ok === true && Object.hasOwn(body, 'result') ? body.result : body;
}

function readableResult(result, fallback) {
  if (typeof result === 'string') return result;
  if (result?.message) return result.message;
  const lines = [];
  const labels = { operations_inserted: 'Operaciones nuevas', operations_updated: 'Operaciones corregidas', quotes_inserted: 'Cotizaciones nuevas', snapshots_inserted: 'Cierres incorporados', movements_inserted: 'Asignaciones importadas', new_operations: 'Operaciones nuevas', inserted: 'Registros nuevos', updated: 'Registros corregidos', unchanged: 'Sin cambios', pending: 'Pendientes' };
  for (const [key, label] of Object.entries(labels)) if (['string', 'number'].includes(typeof result?.[key])) lines.push(`${label}: ${result[key]}`);
  return [fallback, ...lines].filter(Boolean).join('\n');
}

async function runAction(path, data, label, success) {
  if (busy) return false;
  setBusy(true, label);
  try {
    const result = await api(path, data);
    await refreshState();
    notify(readableResult(result, success));
    if (activeView === 'actividad') await loadLogs();
    return true;
  } catch (error) {
    notify(error.message || 'No se pudo completar la acción.', 'error');
    return false;
  } finally { setBusy(false); }
}

function participantName(id) {
  return (state?.participants || []).find(participant => String(participant.id) === String(id))?.name || id || 'Sin asignar';
}

function movementDate(movement) { return movement.date || movement.effective_at; }
function isRedemption(movement) { return ['redemption', 'withdrawal', 'rescate'].includes(String(movement.type).toLowerCase()) || number(movement.amount_usd) < 0; }
function statusLabel(status) {
  return ({ pending: 'Pendiente', posted: 'Acreditado', published: 'Publicado', pushed: 'Esperando confirmación de Pages', preparing: 'Preparando', prepared: 'Preparado', approved: 'Aprobado', success: 'Correcto', completed: 'Completado', failed: 'Error', error: 'Error', reversed: 'Revertido', staged: 'Preparado', pending_publish: 'Por publicar', imported: 'Histórico', ready: 'Listo' })[status] || status || '—';
}
function statusBadge(status) {
  const cls = ['failed', 'error'].includes(status) ? ' error' : ['pending', 'pending_publish', 'staged', 'pushed', 'prepared', 'preparing'].includes(status) ? ' pending' : ['reversed', 'imported'].includes(status) ? ' neutral' : '';
  return `<span class="status${cls}">${esc(statusLabel(status))}</span>`;
}

function renderOverview() {
  const summary = state.summary || {};
  $('total-usd').textContent = summary.total_usd == null ? '—' : money(summary.total_usd);
  $('unit-value').textContent = summary.unit_value == null ? '—' : unitFormat.format(number(summary.unit_value));
  $('total-units').textContent = `${unitFormat.format(number(summary.total_units ?? summary.units))} cuotapartes`;
  $('cutoff').textContent = summary.as_of ? `Cierre: ${dateText(summary.as_of)}${summary.quote_as_of ? ` · MEP: ${dateText(summary.quote_as_of)}` : ''}` : 'Sin cierres todavía';
  const pending = Array.isArray(state.pending) ? state.pending : [];
  $('pending-count').textContent = pending.length;
  $('participant-cards').innerHTML = (state.participants || []).map(participant => {
    const report = participant.report || participant;
    return `<div class="participant-row"><div class="avatar" aria-hidden="true">${esc(participant.name?.slice(0, 1) || '?')}</div><div class="participant-name"><strong>${esc(participant.name)}</strong><small>${unitFormat.format(number(report.units))} cuotapartes</small></div><div class="participant-balance"><small>USD</small> ${report.balance_usd == null ? '—' : money(report.balance_usd)}</div></div>`;
  }).join('') || '<p class="empty">Importá el histórico para incorporar a los participantes.</p>';
  const publications = state.publications || [];
  const latest = publications[0];
  $('publication-status').innerHTML = latest ? `<p class="publication-message"><strong>${esc(statusLabel(latest.status || latest.state || (latest.published_at ? 'published' : 'staged')))}</strong>${esc(latest.message || latest.error || `Última versión: ${latest.version || latest.id || '—'}`)}<br>${esc(dateText(latest.published_at || latest.created_at || latest.as_of))}</p>` : '<p class="publication-message"><strong>Primera publicación pendiente</strong>Configurá las contraseñas y el repositorio para compartir el último cierre.</p>';
  const issues = Array.isArray(state.issues) ? state.issues : [];
  $('issues-panel').hidden = !issues.length;
  $('issues').innerHTML = issues.map(issue => `<li>${esc(typeof issue === 'string' ? issue : issue.message || issue.description || JSON.stringify(issue))}</li>`).join('');
  $('pending-list').innerHTML = pending.length ? pending.map(movement => `<div class="pending-item"><div><strong>${esc(movement.participant_name || participantName(movement.participant_id))} · ${isRedemption(movement) ? 'Retiro' : 'Aporte'}</strong><small>${esc(dateText(movementDate(movement)))} · ${esc(movement.note || movement.reason || 'Sin concepto')}</small></div><div class="numeric">USD ${money(Math.abs(number(movement.amount_usd)))}<small>${esc(statusLabel(movement.status || 'pending'))}</small>${movement.status === 'pending' && movement.id ? `<button type="button" class="button button-quiet button-small" data-post="${esc(movement.id)}">Marcar acreditado</button>` : ''}</div></div>`).join('') : '<p class="empty">No hay movimientos pendientes.</p>';
  document.querySelectorAll('[data-post]').forEach(button => button.addEventListener('click', () => runAction(`/api/movements/${encodeURIComponent(button.dataset.post)}/post`, {}, 'Confirmando la acreditación del movimiento…', 'Movimiento marcado como acreditado. Se incorporará al próximo cierre.')));
  const pagesUrl = state.config?.pages_url;
  if (pagesUrl && /^https?:\/\//i.test(pagesUrl)) { $('family-site').href = pagesUrl; $('family-site').hidden = false; } else { $('family-site').hidden = true; }
}

function renderMovements() {
  const participants = state.participants || [];
  const selected = $('movement-participant').value;
  $('movement-participant').replaceChildren(new Option('Elegir participante', ''), ...participants.map(participant => new Option(participant.name, String(participant.id))), new Option('Distribuir entre varios', '__split__'));
  if ([...$('movement-participant').options].some(option => option.value === selected)) $('movement-participant').value = selected;
  const splitValues = Object.fromEntries([...document.querySelectorAll('[data-allocation-id]')].map(input => [input.dataset.allocationId, input.value]));
  $('split-fields').innerHTML = participants.map(participant => `<div class="split-row"><label for="allocation-${esc(participant.id)}">${esc(participant.name)}</label><input id="allocation-${esc(participant.id)}" data-allocation-id="${esc(participant.id)}" type="number" min="0" step="any" inputmode="decimal" value="${esc(splitValues[String(participant.id)] || '')}" placeholder="0,00"></div>`).join('');
  document.querySelectorAll('[data-allocation-id]').forEach(input => input.addEventListener('input', showSplitTotal));
  const movements = [...(state.movements || [])].sort((a, b) => String(movementDate(b)).localeCompare(String(movementDate(a))));
  $('movement-rows').innerHTML = movements.map(movement => {
    const redemption = isRedemption(movement);
    const label = movement.type === 'reversal' ? 'Reversión' : movement.type === 'initial' ? 'Inicio' : redemption ? 'Rescate' : 'Suscripción';
    return `<tr><td>${esc(dateText(movementDate(movement)))}<small>${esc(movement.participant_name || participantName(movement.participant_id))}</small></td><td class="movement-concept">${label}<small>${esc(movement.note || '—')}</small></td><td class="numeric ${redemption ? 'negative' : 'positive'}">${redemption ? '−' : '+'}${money(Math.abs(number(movement.amount_usd)))}</td><td>${statusBadge(movement.status)}</td><td>${movement.status === 'pending' ? `<button type="button" class="button button-quiet button-small" data-post-row="${esc(movement.id)}">Acreditado</button> ` : ''}${movement.status === 'reversed' || movement.reverses_id ? '' : `<button type="button" class="button button-quiet button-small" data-reverse="${esc(movement.id)}">Revertir</button>`}</td></tr>`;
  }).join('');
  $('movement-empty').hidden = movements.length !== 0;
  document.querySelectorAll('[data-reverse]').forEach(button => button.addEventListener('click', () => {
    $('reverse-id').value = button.dataset.reverse; $('reverse-reason').value = ''; $('reverse-dialog').showModal(); $('reverse-reason').focus();
  }));
  document.querySelectorAll('[data-post-row]').forEach(button => button.addEventListener('click', () => runAction(`/api/movements/${encodeURIComponent(button.dataset.postRow)}/post`, {}, 'Confirmando la acreditación del movimiento…', 'Movimiento marcado como acreditado. Se incorporará al próximo cierre.')));
}

function renderUsers() {
  $('user-cards').innerHTML = (state.participants || []).map(participant => `<article class="user-card"><div class="user-heading"><div class="avatar" aria-hidden="true">${esc(participant.name?.slice(0, 1) || '?')}</div><div><h2>${esc(participant.name)}</h2><span class="status${participant.password_pending ? ' pending' : participant.password_configured ? '' : ' neutral'}">${participant.password_pending ? 'Cambio por publicar' : participant.password_configured ? 'Contraseña configurada' : 'Sin contraseña'}</span></div></div><form data-password-form="${esc(participant.id)}"><div><label for="password-${esc(participant.id)}">Nueva contraseña</label><div class="password-input"><input id="password-${esc(participant.id)}" name="password" type="password" autocomplete="new-password" required><button type="button" data-toggle-password="password-${esc(participant.id)}" aria-label="Mostrar contraseña" aria-pressed="false">Ver</button></div></div><div><label for="confirm-${esc(participant.id)}">Repetir contraseña</label><input id="confirm-${esc(participant.id)}" name="confirm" type="password" autocomplete="new-password" required></div><p class="field-help">Puede ser sencilla y fácil de recordar. Se aplica al último reporte disponible.</p><button type="submit" class="button button-primary">Guardar y publicar contraseña</button></form></article>`).join('') || '<p class="empty">Importá el histórico para configurar los usuarios.</p>';
  document.querySelectorAll('[data-toggle-password]').forEach(button => button.addEventListener('click', () => {
    const input = $(button.dataset.togglePassword); const show = input.type === 'password'; input.type = show ? 'text' : 'password'; button.textContent = show ? 'Ocultar' : 'Ver'; button.setAttribute('aria-pressed', String(show)); button.setAttribute('aria-label', show ? 'Ocultar contraseña' : 'Mostrar contraseña');
  }));
  document.querySelectorAll('[data-password-form]').forEach(form => form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy) return;
    let password = form.elements.password.value; let confirm = form.elements.confirm.value;
    if (password !== confirm) { notify('Las contraseñas no coinciden.', 'error'); form.elements.confirm.focus(); return; }
    if (!password) return;
    const participantId = form.dataset.passwordForm;
    form.reset();
    await runAction(`/api/users/${encodeURIComponent(participantId)}/password`, { password, confirm, publish: true }, 'Guardando la contraseña y publicando el último reporte…', 'Contraseña guardada. Revisá el estado de publicación.');
    password = ''; confirm = '';
  }));
}

function renderSettings() {
  const config = state.config || {};
  const settings = { 'source-workbook': 'source_workbook', 'encrypted-config': 'encrypted_config_path', 'iol-username': 'iol_username', 'repository-url': 'repository_url', 'pages-url': 'pages_url' };
  for (const [id, key] of Object.entries(settings)) if (document.activeElement !== $(id)) $(id).value = config[key] || '';
  $('schedule-description').textContent = typeof config.schedule === 'string' && config.schedule ? config.schedule : config.schedule?.description || 'Domingos a las 18:00, hora de Argentina, bajo tu usuario de Windows.';
}

function renderPublications() {
  const publications = state.publications || [];
  $('publication-list').innerHTML = publications.length ? publications.map(item => `<div class="publication-item"><div>Cierre ${esc(dateText(item.as_of))}<small>Publicado: ${esc(dateText(item.published_at || item.created_at))}${item.message || item.error ? ` · ${esc(item.message || item.error)}` : ''}</small></div>${statusBadge(item.status || item.state || 'published')}${item.state === 'published' ? `<button type="button" class="button button-quiet button-small" data-restore-publication="${esc(item.id)}">Republicar este cierre</button>` : ''}</div>`).join('') : '<p class="empty">Las publicaciones aparecerán después del primer envío al sitio.</p>';
  document.querySelectorAll('[data-restore-publication]').forEach(button => button.addEventListener('click', () => runAction(`/api/publications/${encodeURIComponent(button.dataset.restorePublication)}/restore`, {}, 'Recuperando el cierre con las contraseñas actuales…', 'Cierre republicado.')));
}

async function refreshState() {
  state = await api('/api/state', null, 'GET');
  $('loading').hidden = true;
  renderOverview(); renderMovements(); renderUsers(); renderSettings(); renderPublications();
  if (busy) document.querySelectorAll('button').forEach(button => { button.disabled = true; });
}

async function loadLogs() {
  try {
    const result = await api('/api/logs', null, 'GET');
    const entries = Array.isArray(result) ? result : [...(result.logs || result.runs || result.entries || []), ...(result.audit || []).map(item => ({ ...item, created_at: item.at, kind: item.action, message: `${item.entity || ''} ${item.entity_key || ''}`.trim() }))];
    entries.sort((a, b) => String(b.created_at || b.started_at || '').localeCompare(String(a.created_at || a.started_at || '')));
    $('logs').innerHTML = entries.length ? entries.map(entry => `<div class="activity-item"><span class="activity-date">${esc(dateText(entry.created_at || entry.started_at || entry.timestamp || entry.date))}</span><div><strong>${esc(entry.action || entry.kind || entry.name || entry.type || 'Ejecución')}</strong><small>${esc(entry.message || entry.summary || entry.error || entry.result_json || (entry.details && typeof entry.details === 'object' ? JSON.stringify(entry.details) : entry.details) || '')}</small></div>${statusBadge(entry.status || 'success')}</div>`).join('') : '<p class="empty">Todavía no hay actividad para mostrar.</p>';
  } catch (error) { $('logs').innerHTML = `<p class="empty">${esc(error.message)}</p>`; }
}

function navigate() {
  const view = location.hash.slice(1) || 'resumen';
  activeView = Object.hasOwn(titles, view) ? view : 'resumen';
  document.querySelectorAll('[data-section]').forEach(section => { section.hidden = section.dataset.section !== activeView; });
  document.querySelectorAll('[data-view]').forEach(link => { const active = link.dataset.view === activeView; link.classList.toggle('active', active); if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current'); });
  $('page-title').textContent = titles[activeView];
  if (activeView === 'actividad' && state) loadLogs();
}

function showSplitTotal() {
  const allocated = [...document.querySelectorAll('[data-allocation-id]')].reduce((sum, input) => sum + number(input.value), 0);
  const total = number($('movement-amount').value);
  $('split-total').textContent = `Asignado: ${money(allocated)} / ${money(total)} ${$('movement-currency').value}`;
  $('split-total').classList.toggle('negative', Math.abs(allocated - total) > .00000001);
}

$('movement-participant').addEventListener('change', () => { $('split-allocations').hidden = $('movement-participant').value !== '__split__'; showSplitTotal(); });
$('movement-amount').addEventListener('input', showSplitTotal);
$('movement-currency').addEventListener('change', showSplitTotal);
$('movement-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  const selected = $('movement-participant').value;
  const amount = $('movement-amount').value;
  let allocations;
  if (selected === '__split__') {
    allocations = [...document.querySelectorAll('[data-allocation-id]')].filter(input => number(input.value) > 0).map(input => ({ participant_id: input.dataset.allocationId, amount: input.value }));
    if (Math.abs(allocations.reduce((sum, allocation) => sum + number(allocation.amount), 0) - number(amount)) > .00000001) { notify('La suma de las asignaciones debe coincidir con el importe total.', 'error'); return; }
  } else allocations = [{ participant_id: selected, amount }];
  const payload = { date: $('movement-date').value, type: $('movement-type').value, amount, currency: $('movement-currency').value, allocations, note: $('movement-note').value, status: $('movement-status').value };
  const serialized = JSON.stringify(payload);
  if (!pendingSubmission || pendingSubmission.payload !== serialized) pendingSubmission = { payload: serialized, id: crypto.randomUUID() };
  payload.request_id = pendingSubmission.id;
  if (await runAction('/api/movements', payload, 'Registrando el movimiento…', 'Movimiento registrado. Se incorporará al próximo cierre semanal.')) {
    pendingSubmission = null;
    $('movement-amount').value = ''; $('movement-note').value = ''; document.querySelectorAll('[data-allocation-id]').forEach(input => { input.value = ''; }); showSplitTotal();
  }
});
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  await runAction('/api/settings', { source_workbook: $('source-workbook').value, encrypted_config_path: $('encrypted-config').value, iol_username: $('iol-username').value, repository_url: $('repository-url').value, pages_url: $('pages-url').value }, 'Guardando la configuración…', 'Configuración guardada.');
});
$('sync-button').addEventListener('click', () => runAction('/api/sync', {}, 'Descargando IOL y conciliando el cierre. Esta operación puede demorar…', 'Actualización completada.'));
$('publish-button').addEventListener('click', () => runAction('/api/publish', {}, 'Preparando los reportes y publicando en GitHub…', 'Publicación procesada.'));
for (const id of ['backup-button', 'activity-backup']) $(id).addEventListener('click', () => runAction('/api/backup', {}, 'Creando un respaldo consistente de la base…', 'Respaldo creado.'));
$('import-button').addEventListener('click', () => runAction('/api/import', {}, 'Importando el Excel y verificando el histórico…', 'Importación completada.'));
$('schedule-button').addEventListener('click', () => runAction('/api/schedule', {}, 'Configurando la tarea semanal de Windows…', 'Tarea semanal configurada.'));
$('refresh-logs').addEventListener('click', loadLogs);
$('reverse-cancel').addEventListener('click', () => $('reverse-dialog').close());
$('reverse-form').addEventListener('submit', async event => {
  event.preventDefault();
  const id = $('reverse-id').value; const reason = $('reverse-reason').value;
  if (!reason.trim()) return;
  $('reverse-dialog').close();
  await runAction(`/api/movements/${encodeURIComponent(id)}/reverse`, { reason }, 'Registrando la reversión…', 'Reversión registrada con trazabilidad.');
});
window.addEventListener('hashchange', navigate);
const today = new Date();
$('movement-date').value = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
navigate();
refreshState().catch(error => { $('loading').hidden = true; notify(`No se pudo cargar la base. ${error.message}`, 'error'); });

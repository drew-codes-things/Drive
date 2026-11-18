'use strict';

const SYNC_POLL_MS = 2000;
const SHOWN_ERRORS = 3;
const DIRECTION_TEXT = {
  both: 'Both ways',
  upload: 'Upload only (Drive to cloud)',
  download: 'Download only (cloud to Drive)',
};
const DIRECTION_ARROW = { both: '<->', upload: '->', download: '<-' };
const INTERVAL_TEXT = {
  0: 'Only when I press Sync now',
  15: 'Every 15 minutes',
  60: 'Every hour',
  360: 'Every 6 hours',
  1440: 'Every day',
};
const ACTION_TEXT = {
  upload: 'Copy to the cloud',
  download: 'Copy to Drive',
  replace_remote: 'Update the cloud copy',
  replace_local: 'Update the Drive copy',
  remote_trash: 'Move to the cloud bin',
  local_trash: 'Move to the Drive bin',
  remote_move: 'Rename or move in the cloud',
  local_move: 'Rename or move in Drive',
  remote_folder: 'Make the folder in the cloud',
  local_folder: 'Make the folder in Drive',
  compare: 'Compare; keep both if they differ',
  record: 'Already on both sides',
  forget: 'Gone from both sides',
  skip: 'Skip',
};
const PREVIEW_COUNT_TEXT = {
  upload: 'to copy to the cloud',
  download: 'to copy to Drive',
  replace_remote: 'to update in the cloud',
  replace_local: 'to update in Drive',
  remote_trash: 'to move to the cloud bin',
  local_trash: 'to move to the Drive bin',
  remote_move: 'to rename or move in the cloud',
  local_move: 'to rename or move in Drive',
  remote_folder: 'folders to make in the cloud',
  local_folder: 'folders to make in Drive',
  compare: 'to compare, keeping both if they differ',
};
const COUNT_TEXT = {
  uploaded: 'sent',
  downloaded: 'fetched',
  binnedInCloud: 'to the cloud bin',
  binnedInDrive: 'to the Drive bin',
  moved: 'moved',
  foldersMade: 'folders made',
  alreadyTheSame: 'already the same',
  conflicts: 'conflicts kept as copies',
  errors: 'errors',
  skipped: 'skipped',
  binsHeld: 'held back from a bin',
};
const BIN_PAGE = 500;
const BIN_SIDE_TEXT = { drive: 'Drive bin', cloud: 'Cloud bin' };
const STATUS_LABEL = { ok: 'Synced', errors: 'Synced with errors', failed: 'Stopped', held: 'Waiting for a yes' };
const syncState = { connections: [], googleReady: false, timer: 0, waiting: new Map(), fetchedBins: new Map() };

function isSyncView() {
  return state.view === 'sync';
}

async function loadSync() {
  const answer = await api('GET', '/api/sync');
  syncState.connections = answer.connections;
  syncState.googleReady = answer.googleReady;
  scheduleSyncPoll();
}

function scheduleSyncPoll() {
  clearTimeout(syncState.timer);
  const busy = syncState.connections.some((connection) => connection.activity);
  if (busy && isSyncView()) syncState.timer = setTimeout(pollSync, SYNC_POLL_MS);
}

async function pollSync() {
  if (!isSyncView()) return;
  try {
    await loadSync();
  } catch (error) {
    reportError(error);
    return;
  }
  render();
  for (const [id, after] of syncState.waiting) {
    const connection = syncState.connections.find((entry) => entry.id === id);
    if (connection && !connection.activity) {
      syncState.waiting.delete(id);
      after(connection);
    }
  }
}

function whenText(seconds) {
  return seconds ? timeFormat.format(new Date(seconds * 1000)) : 'never';
}

function countsText(counts) {
  const parts = Object.entries(counts || {})
    .filter(([name, amount]) => amount && COUNT_TEXT[name])
    .map(([name, amount]) => `${amount} ${COUNT_TEXT[name]}`);
  return parts.join(', ') || 'nothing to do';
}

function statusText(connection) {
  if (connection.activity === 'preview') return 'Looking at both sides for the preview...';
  if (connection.activity === 'run') return 'Syncing now...';
  if (connection.needsSignIn) return 'Sign in with Google to start.';
  if (connection.folderMissing) return 'The Drive folder is gone (or in the bin); delete this connection.';
  if (connection.lastStatus === 'never') {
    return connection.previewedAt ? 'Preview seen; ready for the first sync.' : 'Look at the preview before the first sync.';
  }
  if (connection.lastStatus === 'held' && !heldBinsOf(connection)) {
    return `Held back ${whenText(connection.lastFinishedAt)}; that list of files to bin is no longer current, `
      + 'so Sync now looks again and asks again only if it is still needed.';
  }
  const label = STATUS_LABEL[connection.lastStatus];
  return `${label} ${whenText(connection.lastFinishedAt)}: ${countsText(connection.lastCounts)}.`;
}

function scheduleText(connection) {
  if (connection.paused) return 'Paused';
  const text = INTERVAL_TEXT[connection.intervalMinutes] || '';
  if (!connection.intervalMinutes || !connection.nextRunAt) return text;
  return `${text}, next ${whenText(connection.nextRunAt)}`;
}

function remoteText(connection) {
  const where = connection.provider === 'google' ? connection.account || 'not signed in' : connection.serverUrl;
  return `${connection.providerLabel} (${where}): /${connection.remotePath}`;
}

function syncButton(label, onClick, className = 'btn ghost') {
  const button = element('button', className, label);
  button.type = 'button';
  button.addEventListener('click', onClick);
  return button;
}

function syncActions(connection) {
  const actions = element('div', 'sync-actions');
  const busy = Boolean(connection.activity);
  if (connection.provider === 'google' && syncState.googleReady) {
    const label = connection.needsSignIn ? 'Sign in with Google' : 'Sign in again';
    actions.append(syncButton(label, () => signInWithGoogle(connection), connection.needsSignIn ? 'btn send' : 'btn ghost'));
  }
  const firstRun = connection.lastSuccessAt === null;
  const preview = syncButton(firstRun ? 'Preview first sync' : 'Preview', () => previewSync(connection));
  const run = syncButton('Sync now', () => runSync(connection), firstRun ? 'btn ghost' : 'btn send');
  const blocked = busy || connection.needsSignIn || connection.folderMissing;
  preview.disabled = blocked;
  run.disabled = blocked || (firstRun && !connection.previewedAt);
  actions.append(preview, run);
  const held = heldBinsOf(connection);
  if (held) {
    const agree = syncButton(binButtonText(held.bins), () => runAllowingBins(connection, held), 'btn danger');
    agree.disabled = blocked;
    actions.append(agree);
  }
  actions.append(syncButton(connection.paused ? 'Resume' : 'Pause', () => changeSync(connection, { paused: !connection.paused })));
  if (connection.provider !== 'google') actions.append(syncButton('New app password', () => replacePassword(connection)));
  const remove = syncButton('Delete', () => deleteSync(connection));
  remove.disabled = busy;
  actions.append(remove);
  return actions;
}

function scheduleChoice(connection) {
  const label = element('label', 'sync-schedule');
  label.append(element('span', 'visually-hidden', `How often ${connection.name} syncs`));
  const select = element('select', 'select');
  Object.entries(INTERVAL_TEXT).forEach(([minutes, text]) => {
    const option = element('option', '', text);
    option.value = minutes;
    option.selected = Number(minutes) === connection.intervalMinutes;
    select.append(option);
  });
  select.addEventListener('change', () => changeSync(connection, { intervalMinutes: Number(select.value) }));
  label.append(select);
  return label;
}

function problemList(title, lines) {
  const details = element('details', 'sync-problems');
  details.append(element('summary', '', `${title} (${lines.length})`));
  const list = element('ul', 'sync-lines');
  lines.forEach((line) => list.append(element('li', '', line)));
  details.append(list);
  return details;
}

function heldBinsOf(connection) {
  return connection.held && connection.held.approval ? connection.held : null;
}

function fetchedBins(connectionId, held) {
  const fetched = syncState.fetchedBins.get(connectionId);
  return fetched && fetched.approval === held.approval ? fetched.files : [];
}

function keepFetchedBins(connectionId, held, start, files) {
  const kept = fetchedBins(connectionId, held);
  if (held.files.length + kept.length !== start) return;
  syncState.fetchedBins.set(connectionId, { approval: held.approval, files: kept.concat(files) });
}

function binLine(line) {
  const row = element('li');
  const side = element('span', 'sync-action', `${BIN_SIDE_TEXT[line.side] || line.side}: `);
  row.append(side, document.createTextNode(line.path));
  return row;
}

function moreBinsText(more) {
  return `Show the next ${Math.min(more, BIN_PAGE)} of ${more} more`;
}

async function showMoreBins(connectionId, held, list, button) {
  button.disabled = true;
  const start = list.children.length;
  let page;
  try {
    page = await api('GET', `/api/sync/connections/${connectionId}/held?from=${start}`);
  } catch (error) {
    button.disabled = false;
    reportError(error);
    return;
  }
  if (page.approval !== held.approval) {
    showToast('The files to bin have changed since this list; look at the new list.', true);
    refreshSync();
    return;
  }
  keepFetchedBins(connectionId, held, start, page.files);
  page.files.forEach((line) => list.append(binLine(line)));
  const more = held.bins - list.children.length;
  if (more > 0 && page.files.length) {
    button.textContent = moreBinsText(more);
    button.disabled = false;
  } else {
    button.remove();
  }
}

function binList(held, connectionId) {
  const box = element('div', 'sync-bins');
  box.append(element('p', 'sync-bins-title', `Files that would go to a bin (${held.bins}):`));
  const list = element('ul', 'sync-lines sync-bin-lines');
  const shown = held.files.concat(fetchedBins(connectionId, held));
  shown.forEach((line) => list.append(binLine(line)));
  box.append(list);
  const more = held.bins - shown.length;
  if (more > 0) {
    const button = syncButton(moreBinsText(more), () => showMoreBins(connectionId, held, list, button),
      'btn ghost sync-bins-more');
    box.append(button);
  }
  return box;
}

function renderSyncConnection(connection) {
  const card = element('div', 'item sync-item');
  card.setAttribute('role', 'listitem');
  const glyph = element('span', 'item-icon folder');
  glyph.append(icon('folder'));
  const title = element('div', 'sync-title');
  title.append(element('span', 'item-name', connection.name), element('span', 'link-meta', scheduleText(connection)));
  card.append(glyph, title);
  const route = element('p', 'sync-route');
  route.textContent = `${connection.folderPath || 'Missing folder'} ${DIRECTION_ARROW[connection.direction]} ${remoteText(connection)}`;
  const status = element('p', `sync-status ${connection.lastStatus}`, statusText(connection));
  card.append(route, status);
  const errors = connection.lastErrors || [];
  if (errors.length) {
    const shown = element('ul', 'sync-lines sync-errors');
    errors.slice(0, SHOWN_ERRORS).forEach((line) => shown.append(element('li', '', sentence(line))));
    card.append(shown);
    if (errors.length > SHOWN_ERRORS) card.append(problemList('All errors', errors.map(sentence)));
  }
  const held = heldBinsOf(connection);
  if (held) card.append(binList(held, connection.id));
  if ((connection.lastSkipped || []).length) card.append(problemList('Skipped names', connection.lastSkipped));
  const footer = element('div', 'sync-footer');
  footer.append(scheduleChoice(connection), syncActions(connection));
  card.append(footer);
  return card;
}

function renderSync(list) {
  list.className = 'items list sync-list';
  syncState.connections.forEach((connection) => list.append(renderSyncConnection(connection)));
}

async function refreshSync() {
  try {
    await loadSync();
  } catch (error) {
    reportError(error);
  }
  if (isSyncView()) render();
}

async function changeSync(connection, change) {
  try {
    await api('PATCH', `/api/sync/connections/${connection.id}`, change);
  } catch (error) {
    reportError(error);
  }
  refreshSync();
}

async function deleteSync(connection) {
  const question = `Delete the connection "${connection.name}"? Files stay where they are on both sides; only the link between them goes.`;
  if (!window.confirm(question)) return;
  try {
    await api('DELETE', `/api/sync/connections/${connection.id}`);
    showToast('Connection deleted. No files were removed.');
  } catch (error) {
    reportError(error);
  }
  refreshSync();
}

async function signInWithGoogle(connection) {
  try {
    const answer = await api('POST', `/api/sync/connections/${connection.id}/google`);
    location.assign(answer.url);
  } catch (error) {
    reportError(error);
  }
}

async function startSync(connection, mode, after, body) {
  try {
    await api('POST', `/api/sync/connections/${connection.id}/${mode}`, body);
  } catch (error) {
    reportError(error);
    refreshSync();
    return;
  }
  syncState.waiting.set(connection.id, after);
  refreshSync();
}

function previewSync(connection) {
  startSync(connection, 'preview', (fresh) => openPreview(fresh));
}

function runSync(connection, body) {
  startSync(connection, 'run', (fresh) => {
    showToast(statusText(fresh), fresh.lastStatus === 'failed' || fresh.lastStatus === 'held');
    if (fresh.lastStatus !== 'failed') loadStorage();
  }, body);
}

function binButtonText(bins) {
  return `Bin ${bins} ${bins === 1 ? 'file' : 'files'} and sync`;
}

function runAllowingBins(connection, held) {
  const question = `This sync moves the ${held.bins} ${held.bins === 1 ? 'file' : 'files'} in the list to a bin, `
    + 'not away for good: Drive\'s bin or the cloud\'s bin keeps them. If the list has changed by the time it '
    + 'runs, nothing is binned and Drive asks again. Go ahead?';
  if (!window.confirm(question)) return;
  runSync(connection, { approveBins: held.approval });
}

function previewLine(line) {
  const row = element('li', 'share-row');
  const text = line.target ? `${line.path} to ${line.target}` : line.path;
  const what = element('span', 'sync-action', ACTION_TEXT[line.action] || line.action);
  const where = element('span', 'share-url', line.kind === 'folder' ? `${text}/` : text);
  row.append(what, where);
  if (line.size) row.append(element('span', 'link-meta', formatSize(line.size)));
  return row;
}

function previewSummary(result) {
  const counts = Object.entries(result.counts || {})
    .filter(([name, amount]) => amount && PREVIEW_COUNT_TEXT[name])
    .map(([name, amount]) => `${amount} ${PREVIEW_COUNT_TEXT[name]}`);
  return counts.length ? `${counts.join(', ')}.` : 'Both sides already match; nothing would change.';
}

async function openPreview(connection) {
  let result;
  try {
    result = await api('GET', `/api/sync/connections/${connection.id}/preview`);
  } catch (error) {
    reportError(error);
    return;
  }
  const dialog = byId('sync-preview-dialog');
  byId('sync-preview-title').textContent = `Preview of "${connection.name}"`;
  const firstRun = connection.lastSuccessAt === null;
  const notes = [...(result.notes || [])];
  if (firstRun) notes.unshift('This is the first sync, so nothing is deleted on either side.');
  byId('sync-preview-note').textContent = result.stopped
    ? `The preview stopped: ${sentence(result.stopped)}`
    : [previewSummary(result), ...notes.map(sentence)].join(' ');
  const lines = byId('sync-preview-list');
  lines.replaceChildren(...(result.actions || []).map(previewLine));
  lines.hidden = !lines.children.length;
  const skipped = byId('sync-preview-skipped');
  skipped.replaceChildren();
  if ((result.skipped || []).length) skipped.append(problemList('Skipped names', result.skipped));
  const held = result.binApproval
    ? { bins: result.binsHeld, approval: result.binApproval, files: result.binFiles || [] }
    : null;
  byId('sync-preview-bins').replaceChildren(...(held ? [binList(held, connection.id)] : []));
  const run = byId('sync-preview-run');
  run.disabled = Boolean(result.stopped);
  run.textContent = held ? binButtonText(held.bins) : 'Sync now';
  run.className = held ? 'btn danger' : 'btn send';
  run.onclick = () => {
    dialog.close();
    if (held) runAllowingBins(connection, held);
    else runSync(connection);
  };
  dialog.querySelectorAll('[data-close]').forEach((button) => {
    button.onclick = () => dialog.close();
  });
  dialog.showModal();
}

function askPassword(connection) {
  return new Promise((resolve) => {
    const dialog = byId('sync-password-dialog');
    byId('sync-password-title').textContent = `New app password for "${connection.name}"`;
    const input = byId('sync-password-input');
    input.value = '';
    closeOnCancel(dialog, resolve);
    byId('sync-password-form').onsubmit = (event) => {
      event.preventDefault();
      const value = input.value;
      input.value = '';
      dialog.onclose = null;
      dialog.close();
      resolve(value || null);
    };
    dialog.showModal();
  });
}

async function replacePassword(connection) {
  const password = await askPassword(connection);
  if (!password) return;
  await changeSync(connection, { password });
  showToast('App password replaced.');
}

function showProviderFields(provider) {
  const isGoogle = provider === 'google';
  byId('sync-webdav-fields').hidden = isGoogle;
  byId('sync-google-note').hidden = !isGoogle;
  byId('sync-google-note').textContent = syncState.googleReady
    ? 'After saving, press Sign in with Google on the connection. Upload-only connections ask Google for access to the files this app makes; the others ask for your whole Google Drive.'
    : 'Google sign-in is not set up on this server yet (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET). The README has the steps.';
  byId('sync-url-label').firstChild.textContent = provider === 'nextcloud' ? 'Nextcloud address' : 'WebDAV address';
  byId('sync-url').placeholder = provider === 'nextcloud' ? 'https://cloud.example.com' : 'https://dav.example.com/files/';
  ['sync-url', 'sync-username', 'sync-password'].forEach((id) => {
    byId(id).required = !isGoogle;
  });
}

function connectionBody(folderId) {
  const provider = byId('sync-provider').value;
  const body = {
    name: byId('sync-name').value.trim(),
    provider,
    folderId,
    remotePath: byId('sync-remote').value.trim(),
    direction: byId('sync-direction').value,
    intervalMinutes: Number(byId('sync-interval').value),
  };
  if (provider !== 'google') {
    body.serverUrl = byId('sync-url').value.trim();
    body.username = byId('sync-username').value.trim();
    body.password = byId('sync-password').value;
  }
  return body;
}

function fillChoices(select, texts) {
  select.replaceChildren(...Object.entries(texts).map(([value, text]) => {
    const option = element('option', '', text);
    option.value = value;
    return option;
  }));
}

function openAddConnection() {
  const dialog = byId('sync-dialog');
  const form = byId('sync-form');
  form.reset();
  fillChoices(byId('sync-direction'), DIRECTION_TEXT);
  fillChoices(byId('sync-interval'), INTERVAL_TEXT);
  let folderId = null;
  byId('sync-folder-path').textContent = 'No folder chosen yet';
  showProviderFields(byId('sync-provider').value);
  byId('sync-provider').onchange = () => showProviderFields(byId('sync-provider').value);
  byId('sync-folder').onclick = async () => {
    const picked = await pickFolder('Sync this Drive folder', 'Use this folder', []);
    if (picked === null) return;
    folderId = picked;
    const listing = await api('GET', `/api/folders/${picked}`);
    byId('sync-folder-path').textContent = ['My Drive', ...listing.path.map((step) => step.name)].join(' / ');
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (folderId === null) {
      showToast('Choose the Drive folder to sync first.', true);
      return;
    }
    try {
      await api('POST', '/api/sync/connections', connectionBody(folderId));
    } catch (error) {
      reportError(error);
      return;
    }
    byId('sync-password').value = '';
    dialog.close();
    showToast('Connection saved. Look at the preview before the first sync.');
    refreshSync();
  };
  dialog.querySelectorAll('[data-close]').forEach((button) => {
    button.onclick = () => dialog.close();
  });
  dialog.onclose = () => {
    byId('sync-password').value = '';
  };
  dialog.showModal();
}

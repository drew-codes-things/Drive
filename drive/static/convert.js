'use strict';

const CONVERSION_POLL_MS = 1500;
const CONVERSION_KEEP_TEXT = 'The converted copy is saved next to this file. The original stays as it is.';
const conversionJobs = new Map();
let conversionTimer = 0;

function sentence(text) {
  return text.charAt(0).toUpperCase() + text.slice(1) + (text.endsWith('.') ? '' : '.');
}

function conversionNote(options) {
  if (!options.kind || !options.targets.length) return 'This kind of file cannot be converted here.';
  if (!options.ready) return sentence(options.reason);
  return `${CONVERSION_KEEP_TEXT} Files up to ${formatSize(options.maxInputBytes)} can be converted.`;
}

function conversionChoice(item, target, dialog) {
  const button = element('button', '', target.label);
  button.type = 'button';
  button.setAttribute('role', 'listitem');
  button.onclick = () => {
    dialog.close();
    startConversion(item, target);
  };
  return button;
}

async function openConvertDialog(item) {
  let options;
  try {
    options = await api('GET', `/api/files/${item.id}/conversions`);
  } catch (error) {
    reportError(error);
    return;
  }
  const dialog = byId('convert-dialog');
  byId('convert-title').textContent = `Convert "${item.name}"`;
  byId('convert-note').textContent = conversionNote(options);
  const choices = byId('convert-choices');
  choices.replaceChildren();
  if (options.ready) options.targets.forEach((target) => choices.append(conversionChoice(item, target, dialog)));
  choices.hidden = !choices.children.length;
  dialog.querySelectorAll('[data-close]').forEach((button) => {
    button.onclick = () => dialog.close();
  });
  dialog.showModal();
}

async function startConversion(item, target) {
  try {
    trackConversion(await api('POST', `/api/files/${item.id}/conversions`, { target: target.key }));
  } catch (error) {
    reportError(error);
  }
}

function trackConversion(job) {
  conversionJobs.set(job.id, job);
  drawConversions();
  scheduleConversionPoll();
}

function conversionStateText(job) {
  if (job.status === 'done') return `Saved as ${job.result.name}`;
  if (job.status === 'failed') return sentence(job.error || 'the conversion failed');
  return `Converting to ${job.target.toUpperCase()}...`;
}

function drawConversionRow(job) {
  const row = element('li', `upload-row ${job.status}`);
  const name = element('span', 'upload-name', `${job.sourceName} to ${job.target.toUpperCase()}`);
  name.title = name.textContent;
  row.append(name, element('span', 'upload-state', conversionStateText(job)));
  if (job.status === 'done') {
    const show = element('button', 'link-btn', 'Show');
    show.type = 'button';
    show.onclick = () => goTo('drive', job.result.parentId);
    row.append(show);
  }
  return row;
}

function drawConversions() {
  const jobs = [...conversionJobs.values()];
  const running = jobs.filter((job) => job.status === 'running').length;
  byId('conversions').hidden = !jobs.length;
  byId('conversions-title').textContent = running
    ? `Converting ${running} ${running === 1 ? 'file' : 'files'}`
    : 'Conversions finished';
  byId('conversion-list').replaceChildren(...jobs.map(drawConversionRow));
}

function scheduleConversionPoll() {
  clearTimeout(conversionTimer);
  if ([...conversionJobs.values()].some((job) => job.status === 'running')) {
    conversionTimer = setTimeout(pollConversions, CONVERSION_POLL_MS);
  }
}

async function pollConversions() {
  let finishedOne = false;
  for (const job of [...conversionJobs.values()].filter((entry) => entry.status === 'running')) {
    try {
      const fresh = await api('GET', `/api/conversions/${job.id}`);
      conversionJobs.set(job.id, fresh);
      finishedOne = finishedOne || fresh.status === 'done';
    } catch (error) {
      conversionJobs.set(job.id, { ...job, status: 'failed', error: error.message });
    }
  }
  drawConversions();
  if (finishedOne) {
    load();
    loadStorage();
  }
  scheduleConversionPoll();
}

function clearFinishedConversions() {
  for (const [id, job] of conversionJobs) {
    if (job.status !== 'running') conversionJobs.delete(id);
  }
  drawConversions();
}

async function loadConversions() {
  try {
    const answer = await api('GET', '/api/conversions');
    answer.jobs.filter((job) => job.status === 'running').forEach(trackConversion);
  } catch (error) {
    if (error.status !== 404) reportError(error);
  }
}

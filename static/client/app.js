'use strict';

const sealedMessage = 'Your answers are safe and will upload automatically.';

const problemMessages = {
  coordinator_unavailable: 'The assessment server is temporarily unavailable. Your saved work is safe.',
  start_window_closed: 'The 10-minute start window has closed. Ask Faculty for help.',
  content_hash_mismatch: 'The assessment download failed verification and will be downloaded again.',
  device_inactive: 'This lab computer is not registered. Ask Faculty or IT for help.',
  corrupt_local_attempt: 'Saved assessment data could not be verified. Do not close the application; ask Faculty for help.',
  faculty_intervention_required: 'The sealed submission needs Faculty attention. Your answers remain saved on this computer.',
};

function problemMessage(code, diagnosticReference) {
  if (Object.prototype.hasOwnProperty.call(problemMessages, code)) {
    return problemMessages[code];
  }
  const suffix = diagnosticReference ? ` Reference: ${diagnosticReference}` : '';
  return `The requested action could not be completed.${suffix}`;
}

function canEdit(state) {
  return state === 'in_progress';
}

function optimisticSelection(previous, selected) {
  return { selected, status: 'saving' };
}

function restoreSelection(_current, previous) {
  return { selected: previous, status: 'error' };
}

function setSafeText(element, value) {
  element.textContent = value == null ? '' : String(value);
}

function assetUrl(attemptId, reference) {
  if (typeof attemptId !== 'string'
      || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(attemptId)
      || typeof reference !== 'string') {
    return null;
  }
  const match = /^assets\/([0-9a-f]{64}\.(?:png|jpe?g|webp|svg))$/.exec(reference);
  return match ? `/api/attempts/${encodeURIComponent(attemptId)}/assets/${match[1]}` : null;
}

const exported = {
  assetUrl,
  canEdit,
  optimisticSelection,
  problemMessage,
  problemMessages,
  restoreSelection,
  sealedMessage,
  setSafeText,
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = exported;
}

if (typeof document !== 'undefined') {
  const csrf = document.querySelector('meta[name="ksat-csrf"]').content;
  const elements = {
    statusPanel: document.getElementById('status-panel'),
    statusTitle: document.getElementById('status-title'),
    statusCopy: document.getElementById('status-copy'),
    actionArea: document.getElementById('action-area'),
    assessmentPanel: document.getElementById('assessment-panel'),
    questionHeading: document.getElementById('question-heading'),
    questionText: document.getElementById('question-text'),
    questionMedia: document.getElementById('question-media'),
    progress: document.getElementById('progress-label'),
    saveStatus: document.getElementById('save-status'),
    options: document.getElementById('options'),
    previous: document.getElementById('previous-question'),
    next: document.getElementById('next-question'),
    submit: document.getElementById('submit-attempt'),
    timer: document.getElementById('timer'),
    gate: document.getElementById('fullscreen-gate'),
    enterFullscreen: document.getElementById('enter-fullscreen'),
    announcer: document.getElementById('announcer'),
    errorAnnouncer: document.getElementById('error-announcer'),
  };

  const ui = {
    state: null,
    attempt: null,
    questionIndex: 0,
    fullscreenReady: false,
    pollHandle: null,
    saving: false,
  };

  async function request(path, options = {}) {
    const init = { credentials: 'same-origin', cache: 'no-store', ...options };
    if (init.method && init.method !== 'GET') {
      init.headers = {
        'Content-Type': 'application/json',
        'X-KSAT-CSRF': csrf,
        ...(init.headers || {}),
      };
    }
    const response = await fetch(path, init);
    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = { problem: { code: 'invalid_local_response' } };
    }
    if (!response.ok && response.status !== 202) {
      const failure = new Error('local request failed');
      failure.problem = payload.problem || { code: 'local_action_failed' };
      throw failure;
    }
    return payload;
  }

  function announce(message, error = false) {
    setSafeText(error ? elements.errorAnnouncer : elements.announcer, message);
  }

  function button(label, handler, className = '') {
    const item = document.createElement('button');
    item.type = 'button';
    setSafeText(item, label);
    if (className) item.className = className;
    item.addEventListener('click', handler);
    return item;
  }

  function input(labelText, type, autocomplete) {
    const label = document.createElement('label');
    label.className = 'field';
    const caption = document.createElement('span');
    setSafeText(caption, labelText);
    const control = document.createElement('input');
    control.type = type;
    control.required = true;
    control.autocomplete = autocomplete;
    label.append(caption, control);
    return { label, control };
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function createPublicImage(attemptId, media) {
    if (!media || typeof media !== 'object') return null;
    const source = assetUrl(attemptId, media.url);
    if (!source) return null;
    const image = document.createElement('img');
    image.className = 'public-media';
    image.src = source;
    image.alt = typeof media.alt_text === 'string' ? media.alt_text : '';
    image.draggable = false;
    image.decoding = 'async';
    image.loading = 'eager';
    if (Number.isInteger(media.width) && media.width > 0 && media.width <= 10000) {
      image.width = media.width;
    }
    if (Number.isInteger(media.height) && media.height > 0 && media.height <= 10000) {
      image.height = media.height;
    }
    return image;
  }

  function showStatus(title, copy) {
    elements.assessmentPanel.hidden = true;
    elements.statusPanel.hidden = false;
    setSafeText(elements.statusTitle, title);
    setSafeText(elements.statusCopy, copy);
    clear(elements.actionArea);
  }

  function renderDeviceSetup() {
    showStatus('Set up this lab computer', 'Enter the one-time details provided by Faculty or IT.');
    const form = document.createElement('form');
    const label = input('Computer label', 'text', 'off');
    const code = input('Enrollment code', 'password', 'off');
    const submit = button('Register computer', () => {});
    submit.type = 'submit';
    form.append(label.label, code.label, submit);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        await request('/api/device/enroll', {
          method: 'POST',
          body: JSON.stringify({ label: label.control.value, enrollment_code: code.control.value }),
        });
        await refreshState();
      } catch (error) {
        showProblem(error.problem);
        submit.disabled = false;
      } finally {
        code.control.value = '';
      }
    });
    elements.actionArea.append(form);
    label.control.focus();
  }

  function renderLogin() {
    showStatus('Student sign in', 'Sign in while connected to the assessment network.');
    const form = document.createElement('form');
    const student = input('Student ID', 'text', 'username');
    const password = input('Password', 'password', 'current-password');
    const submit = button('Sign in', () => {});
    submit.type = 'submit';
    form.append(student.label, password.label, submit);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        await request('/api/login', {
          method: 'POST',
          body: JSON.stringify({ student_id: student.control.value, password: password.control.value }),
        });
        await loadAssessments();
      } catch (error) {
        showProblem(error.problem);
        submit.disabled = false;
      } finally {
        password.control.value = '';
      }
    });
    elements.actionArea.append(form);
    student.control.focus();
  }

  async function loadAssessments() {
    showStatus('Available assessments', 'Checking for an assessment launched by Faculty.');
    try {
      const payload = await request('/api/assessments');
      clear(elements.actionArea);
      if (!payload.assessments.length) {
        setSafeText(elements.statusCopy, 'No assessment is ready yet. This page will check again.');
        window.setTimeout(loadAssessments, 3000);
        return;
      }
      setSafeText(elements.statusCopy, 'Choose the launched assessment when Faculty asks you to begin.');
      payload.assessments.forEach((assessment) => {
        const row = document.createElement('div');
        row.className = 'assessment-row';
        const name = document.createElement('strong');
        setSafeText(name, assessment.test_name);
        const action = button(assessment.content_ready ? 'Start' : 'Download', async () => {
          action.disabled = true;
          try {
            if (!assessment.content_ready) {
              await request('/api/content/prefetch', {
                method: 'POST', body: JSON.stringify({ release_id: assessment.release_id }),
              });
            }
            const attempt = await request(`/api/assessments/${encodeURIComponent(assessment.release_id)}/start`, {
              method: 'POST', body: JSON.stringify({ confirmed: true }),
            });
            ui.fullscreenReady = false;
            await enterFullscreen();
            renderAttempt(attempt);
          } catch (error) {
            showProblem(error.problem);
            action.disabled = false;
          }
        });
        row.append(name, action);
        elements.actionArea.append(row);
      });
    } catch (error) {
      showProblem(error.problem);
    }
  }

  function formatTime(seconds) {
    const safe = Number.isInteger(seconds) && seconds >= 0 ? seconds : 0;
    return `${String(Math.floor(safe / 60)).padStart(2, '0')}:${String(safe % 60).padStart(2, '0')}`;
  }

  function disableExamControls() {
    elements.options.querySelectorAll('button[data-answer]').forEach((item) => { item.disabled = true; });
    elements.previous.disabled = true;
    elements.next.disabled = true;
    elements.submit.disabled = true;
  }

  function renderAttempt(attempt) {
    ui.state = attempt.state;
    ui.attempt = attempt;
    setSafeText(elements.timer, formatTime(attempt.remaining_seconds));
    if (!canEdit(attempt.state)) {
      disableExamControls();
      if (attempt.state === 'acknowledged_result') {
        renderAcknowledged(attempt.result);
      } else if (attempt.state === 'faculty_intervention_required') {
        renderIntervention(attempt);
      } else {
        renderSealed(attempt);
      }
      return;
    }
    if (!ui.fullscreenReady || !document.fullscreenElement) {
      elements.gate.hidden = false;
      elements.assessmentPanel.hidden = true;
      return;
    }
    elements.gate.hidden = true;
    elements.statusPanel.hidden = true;
    elements.assessmentPanel.hidden = false;
    ui.questionIndex = Math.min(ui.questionIndex, attempt.questions.length - 1);
    const question = attempt.questions[ui.questionIndex];
    setSafeText(elements.progress, `Question ${ui.questionIndex + 1} of ${attempt.questions.length}`);
    setSafeText(elements.questionHeading, `Question ${ui.questionIndex + 1}`);
    setSafeText(elements.questionText, question.question_text);
    clear(elements.questionMedia);
    if (question.stimulus && question.stimulus.type === 'image') {
      const stimulusImage = createPublicImage(attempt.attempt_id, question.stimulus);
      if (stimulusImage) elements.questionMedia.append(stimulusImage);
    }
    const displayMedia = question.display_media || {};
    const questionImage = createPublicImage(attempt.attempt_id, displayMedia.question);
    if (questionImage) elements.questionMedia.append(questionImage);
    clear(elements.options);
    const selected = attempt.responses[String(question.question_id)];
    Object.entries(question.options).forEach(([key, value]) => {
      const option = document.createElement('button');
      option.type = 'button';
      option.dataset.answer = key;
      option.setAttribute('aria-pressed', selected === key ? 'true' : 'false');
      option.className = selected === key ? 'answer selected' : 'answer';
      const keyLabel = document.createElement('strong');
      setSafeText(keyLabel, `${key}. `);
      const copy = document.createElement('span');
      setSafeText(copy, value);
      option.append(keyLabel, copy);
      const optionImage = createPublicImage(
        attempt.attempt_id,
        displayMedia.options && displayMedia.options[key],
      );
      if (optionImage) option.append(optionImage);
      option.addEventListener('click', () => saveAnswer(question.question_id, key));
      elements.options.append(option);
    });
    elements.previous.disabled = ui.questionIndex === 0;
    elements.next.disabled = ui.questionIndex === attempt.questions.length - 1;
    elements.submit.disabled = false;
    setSafeText(elements.saveStatus, 'Saved locally');
    startPolling();
  }

  async function saveAnswer(questionId, selected) {
    if (!canEdit(ui.state) || ui.saving) return;
    const previous = ui.attempt.responses[String(questionId)] || null;
    const optimistic = optimisticSelection(previous, selected);
    ui.attempt.responses[String(questionId)] = optimistic.selected;
    ui.saving = true;
    setSafeText(elements.saveStatus, 'Saving locally…');
    renderAttempt(ui.attempt);
    let saveFailed = false;
    try {
      const saved = await request(`/api/attempts/${encodeURIComponent(ui.attempt.attempt_id)}/responses/${questionId}`, {
        method: 'PUT', body: JSON.stringify({ answer: selected }),
      });
      ui.attempt.responses[String(questionId)] = saved.selected_answer;
      setSafeText(elements.saveStatus, 'Saved locally');
      announce('Answer saved locally.');
    } catch (error) {
      saveFailed = true;
      const restored = restoreSelection(optimistic, previous);
      ui.attempt.responses[String(questionId)] = restored.selected;
      showProblem(error.problem);
    } finally {
      ui.saving = false;
      renderAttempt(ui.attempt);
      setSafeText(
        elements.saveStatus,
        saveFailed ? 'Local save failed; selection restored' : 'Saved locally',
      );
    }
  }

  function renderSealed(attempt) {
    showStatus('Assessment submitted', sealedMessage);
    const queue = attempt.queue || {};
    const retry = document.createElement('p');
    setSafeText(retry, `Upload retries: ${queue.retry_count || 0}. Next attempt: ${queue.next_attempt_at || 'automatically'}. ${queue.last_error || ''}`);
    elements.actionArea.append(retry);
    startPolling();
  }

  function renderIntervention(attempt) {
    showStatus('Faculty assistance required', problemMessages.faculty_intervention_required);
    const detail = document.createElement('p');
    setSafeText(detail, attempt.message || problemMessages.faculty_intervention_required);
    elements.actionArea.append(detail);
    stopPolling();
  }

  function renderAcknowledged(result) {
    showStatus('Result received', 'The coordinator accepted and scored your submission.');
    const score = document.createElement('p');
    score.className = 'result-score';
    setSafeText(score, `${result.score} / ${result.total_questions} (${result.percentage}%)`);
    elements.actionArea.append(score);
    stopPolling();
  }

  function showProblem(problem = {}) {
    const message = problemMessage(problem.code, problem.diagnostic_reference);
    setSafeText(elements.statusCopy, message);
    announce(message, true);
  }

  async function pollAttempt() {
    if (!ui.attempt) return;
    try {
      const attempt = await request(`/api/attempts/${encodeURIComponent(ui.attempt.attempt_id)}`);
      if (attempt.state === 'acknowledged_result') {
        const result = await request(`/api/attempts/${encodeURIComponent(ui.attempt.attempt_id)}/result`);
        attempt.result = result.result;
      }
      renderAttempt(attempt);
    } catch (error) {
      showProblem(error.problem);
    }
  }

  function startPolling() {
    if (ui.pollHandle !== null) return;
    ui.pollHandle = window.setInterval(pollAttempt, 1000);
  }

  function stopPolling() {
    if (ui.pollHandle !== null) window.clearInterval(ui.pollHandle);
    ui.pollHandle = null;
  }

  async function enterFullscreen() {
    try {
      await document.documentElement.requestFullscreen();
      ui.fullscreenReady = true;
      elements.gate.hidden = true;
    } catch (_error) {
      announce('Full screen is required to continue the assessment.', true);
    }
  }

  async function refreshState() {
    try {
      const state = await request('/api/state');
      ui.state = state.state;
      if (state.state === 'device_setup') return renderDeviceSetup();
      if (state.state === 'login') return renderLogin();
      if (state.state === 'waiting_or_ready') return loadAssessments();
      if (state.problem) {
        showStatus('Faculty assistance required', problemMessage(state.problem.code, state.problem.diagnostic_reference));
        return;
      }
      if (state.attempt) {
        const attempt = await request(`/api/attempts/${encodeURIComponent(state.attempt.attempt_id)}`);
        ui.fullscreenReady = false;
        renderAttempt(attempt);
      }
    } catch (error) {
      showStatus('Client unavailable', problemMessage(error.problem && error.problem.code, error.problem && error.problem.diagnostic_reference));
    }
  }

  async function recordViolation(eventType) {
    if (!ui.attempt || !canEdit(ui.state)) return;
    try {
      await request(`/api/attempts/${encodeURIComponent(ui.attempt.attempt_id)}/violations`, {
        method: 'POST', body: JSON.stringify({ event_type: eventType }),
      });
    } catch (_error) {
      announce('The integrity event remains subject to local retry.', true);
    }
  }

  function blockAndRecord(event, eventType) {
    event.preventDefault();
    recordViolation(eventType);
  }

  elements.previous.addEventListener('click', () => {
    if (ui.questionIndex > 0) {
      ui.questionIndex -= 1;
      renderAttempt(ui.attempt);
      elements.questionHeading.focus();
    }
  });
  elements.next.addEventListener('click', () => {
    if (ui.attempt && ui.questionIndex < ui.attempt.questions.length - 1) {
      ui.questionIndex += 1;
      renderAttempt(ui.attempt);
      elements.questionHeading.focus();
    }
  });
  elements.submit.addEventListener('click', async () => {
    if (!ui.attempt || !canEdit(ui.state)) return;
    disableExamControls();
    ui.state = 'sealed_pending';
    try {
      const sealed = await request(`/api/attempts/${encodeURIComponent(ui.attempt.attempt_id)}/submit`, {
        method: 'POST', body: JSON.stringify({ confirmed: true }),
      });
      renderAttempt(sealed);
    } catch (error) {
      showProblem(error.problem);
      startPolling();
    }
  });
  elements.enterFullscreen.addEventListener('click', async () => {
    await enterFullscreen();
    if (ui.attempt) renderAttempt(ui.attempt);
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) recordViolation('visibility_hidden');
  });
  window.addEventListener('blur', () => recordViolation('focus_lost'));
  ['copy', 'cut', 'paste', 'contextmenu', 'dragstart', 'drop'].forEach((name) => {
    document.addEventListener(name, (event) => blockAndRecord(event, name));
  });
  window.addEventListener('beforeprint', (event) => blockAndRecord(event, 'print_attempt'));
  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && ['c', 'x', 'v', 'p', 's'].includes(event.key.toLowerCase())) {
      blockAndRecord(event, `shortcut_${event.key.toLowerCase()}`);
    }
  });
  document.addEventListener('fullscreenchange', () => {
    if (!document.fullscreenElement && ui.attempt && canEdit(ui.state)) {
      recordViolation('fullscreen_exited');
    }
  });

  refreshState();
}

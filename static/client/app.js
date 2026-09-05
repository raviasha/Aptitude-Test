'use strict';

const sealedMessage = 'Your answers are safe and will upload automatically.';
const reviewPollDelay = 5000;

const problemMessages = {
  coordinator_unavailable: 'The assessment server is temporarily unavailable. Your saved work is safe.',
  start_window_closed: 'The 10-minute start window has closed. Ask Faculty for help.',
  content_hash_mismatch: 'The assessment download failed verification and will be downloaded again.',
  device_inactive: 'This lab computer is not registered. Ask Faculty or IT for help.',
  corrupt_local_attempt: 'Saved assessment data could not be verified. Do not close the application; ask Faculty for help.',
  faculty_intervention_required: 'The sealed submission needs Faculty attention. Your answers remain saved on this computer.',
  review_not_released: 'Review will be available after Faculty closes the assessment.',
  review_unavailable: 'Detailed review is unavailable for this older assessment.',
};

function reviewChoiceState(selected, correct) {
  return {
    state: selected === null ? 'unanswered' : selected === correct ? 'correct' : 'incorrect',
    selected,
    correct,
  };
}

function reviewFailureAction(problem = {}) {
  if (problem.code === 'invalid_client_session' || problem.code === 'client_session_required') {
    return 'signin';
  }
  if (problem.code === 'device_inactive' || problem.code === 'device_not_enrolled') {
    return 'device';
  }
  return problem.retryable === false ? 'stop' : 'retry';
}

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

function saveStatusMessage(state) {
  if (state === 'saving') return 'Saving locally…';
  if (state === 'error') return 'Local save failed; selection restored';
  return 'Saved locally';
}

async function persistOptimisticAnswer(attempt, questionId, selected, write, onState) {
  const key = String(questionId);
  const previous = attempt.responses[key] == null ? null : attempt.responses[key];
  const optimistic = optimisticSelection(previous, selected);
  attempt.responses[key] = optimistic.selected;
  onState('saving');
  try {
    const saved = await write();
    attempt.responses[key] = saved.selected_answer;
    onState('saved');
    return saved;
  } catch (error) {
    const restored = restoreSelection(optimistic, previous);
    attempt.responses[key] = restored.selected;
    onState('error');
    throw error;
  }
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

function reviewAssetUrl(attemptId, reference) {
  const source = assetUrl(attemptId, reference);
  return source ? source.replace('/api/attempts/', '/api/reviews/') : null;
}

const exported = {
  assetUrl,
  canEdit,
  optimisticSelection,
  persistOptimisticAnswer,
  problemMessage,
  problemMessages,
  reviewChoiceState,
  reviewFailureAction,
  reviewAssetUrl,
  reviewPollDelay,
  restoreSelection,
  saveStatusMessage,
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
    saveState: 'saved',
    positionSaving: false,
    reviewPollHandle: null,
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

  function createPublicImage(attemptId, media, review = false) {
    if (!media || typeof media !== 'object') return null;
    const source = review ? reviewAssetUrl(attemptId, media.url) : assetUrl(attemptId, media.url);
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
      const [payload, completed] = await Promise.all([
        request('/api/assessments'), request('/api/reviews'),
      ]);
      clear(elements.actionArea);
      setSafeText(
        elements.statusCopy,
        payload.assessments.length
          ? 'Choose the launched assessment when Faculty asks you to begin.'
          : 'No assessment is ready to start.',
      );
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
            ui.saveState = 'saved';
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
      if (completed.reviews.length) {
        const heading = document.createElement('h3');
        setSafeText(heading, 'Completed assessments');
        elements.actionArea.append(heading);
      }
      let waiting = false;
      completed.reviews.forEach((assessment) => {
        const row = document.createElement('div');
        row.className = 'assessment-row review-summary';
        const details = document.createElement('div');
        const name = document.createElement('strong');
        const score = document.createElement('p');
        setSafeText(name, assessment.test_name);
        setSafeText(score, `${assessment.score} / ${assessment.total_questions} (${assessment.percentage}%)`);
        details.append(name, score);
        let action;
        if (assessment.review_state === 'available') {
          action = button('Review answers', async () => {
            action.disabled = true;
            try {
              renderReview(await request(`/api/reviews/${encodeURIComponent(assessment.attempt_id)}`));
            } catch (error) {
              showProblem(error.problem);
              action.disabled = false;
            }
          });
        } else if (assessment.review_state === 'waiting') {
          waiting = true;
          action = button('Waiting for Faculty to close', loadAssessments);
        } else {
          action = button('Detailed review unavailable', () => {});
          action.disabled = true;
        }
        row.append(details, action);
        elements.actionArea.append(row);
      });
      if (!payload.assessments.length && !completed.reviews.length) {
        setSafeText(elements.statusCopy, 'No assessment is ready yet. This page will check again.');
      }
      if (ui.reviewPollHandle !== null) window.clearTimeout(ui.reviewPollHandle);
      ui.reviewPollHandle = (waiting || (!payload.assessments.length && !completed.reviews.length))
        ? window.setTimeout(loadAssessments, reviewPollDelay)
        : null;
    } catch (error) {
      showProblem(error.problem);
    }
  }

  function renderReview(review) {
    if (ui.reviewPollHandle !== null) window.clearTimeout(ui.reviewPollHandle);
    ui.reviewPollHandle = null;
    showStatus(review.test_name, 'Review of your submitted answers.');
    const list = document.createElement('div');
    list.className = 'review-list';
    review.questions.forEach((item, index) => {
      const card = document.createElement('section');
      card.className = 'review-question';
      const heading = document.createElement('h3');
      const text = document.createElement('p');
      setSafeText(heading, `Question ${index + 1}`);
      setSafeText(text, item.question.question_text);
      card.append(heading, text);
      if (item.question.stimulus && item.question.stimulus.type === 'image') {
        const image = createPublicImage(review.attempt_id, item.question.stimulus, true);
        if (image) card.append(image);
      }
      const displayMedia = item.question.display_media || {};
      const questionImage = createPublicImage(review.attempt_id, displayMedia.question, true);
      if (questionImage) card.append(questionImage);
      const choice = reviewChoiceState(item.selected_answer, item.correct_answer);
      Object.entries(item.question.options).forEach(([key, value]) => {
        const option = document.createElement('div');
        option.className = 'review-option';
        if (key === choice.selected) option.classList.add('student-choice');
        if (key === choice.correct) option.classList.add('correct-choice');
        const copy = document.createElement('span');
        setSafeText(copy, `${key}. ${value}`);
        option.append(copy);
        const optionImage = createPublicImage(
          review.attempt_id, displayMedia.options && displayMedia.options[key], true,
        );
        if (optionImage) option.append(optionImage);
        if (key === choice.selected) {
          const badge = document.createElement('strong');
          setSafeText(badge, 'Your choice');
          option.append(badge);
        }
        if (key === choice.correct) {
          const badge = document.createElement('strong');
          setSafeText(badge, 'Correct choice');
          option.append(badge);
        }
        card.append(option);
      });
      if (choice.state === 'unanswered') {
        const unanswered = document.createElement('p');
        unanswered.className = 'review-state-unanswered';
        setSafeText(unanswered, 'Your choice: Unanswered');
        card.append(unanswered);
      }
      const solutionHeading = document.createElement('h4');
      const steps = document.createElement('ol');
      steps.className = 'solution-steps';
      setSafeText(solutionHeading, 'Solution steps');
      item.solution_steps.forEach((step) => {
        const line = document.createElement('li');
        setSafeText(line, step);
        steps.append(line);
      });
      card.append(solutionHeading, steps);
      list.append(card);
    });
    elements.actionArea.append(list, button('Back to completed assessments', loadAssessments));
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
    const storedIndex = attempt.questions.findIndex(
      question => question.question_id === attempt.current_question_id,
    );
    ui.questionIndex = storedIndex >= 0
      ? storedIndex
      : Math.min(ui.questionIndex, attempt.questions.length - 1);
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
    setSafeText(elements.saveStatus, saveStatusMessage(ui.saveState));
    startPolling();
  }

  async function saveAnswer(questionId, selected) {
    if (!canEdit(ui.state) || ui.saving) return;
    ui.saving = true;
    try {
      await persistOptimisticAnswer(
        ui.attempt,
        questionId,
        selected,
        () => request(`/api/attempts/${encodeURIComponent(ui.attempt.attempt_id)}/responses/${questionId}`, {
          method: 'PUT', body: JSON.stringify({ answer: selected }),
        }),
        state => {
          ui.saveState = state;
          renderAttempt(ui.attempt);
        },
      );
      announce('Answer saved locally.');
    } catch (error) {
      showProblem(error.problem);
      announce(saveStatusMessage('error'), true);
    } finally {
      ui.saving = false;
      renderAttempt(ui.attempt);
    }
  }

  async function persistPosition(index) {
    if (
      !ui.attempt
      || !canEdit(ui.state)
      || ui.positionSaving
      || index < 0
      || index >= ui.attempt.questions.length
    ) return;
    const previousId = ui.attempt.current_question_id;
    const target = ui.attempt.questions[index];
    ui.positionSaving = true;
    ui.attempt.current_question_id = target.question_id;
    ui.questionIndex = index;
    renderAttempt(ui.attempt);
    try {
      const saved = await request(
        `/api/attempts/${encodeURIComponent(ui.attempt.attempt_id)}/position`,
        {
          method: 'PUT',
          body: JSON.stringify({ question_id: target.question_id }),
        },
      );
      ui.attempt.current_question_id = saved.current_question_id;
    } catch (error) {
      ui.attempt.current_question_id = previousId;
      showProblem(error.problem);
    } finally {
      ui.positionSaving = false;
      renderAttempt(ui.attempt);
      elements.questionHeading.focus();
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

  function scheduleAcknowledgedReview(result) {
    if (ui.reviewPollHandle !== null) window.clearTimeout(ui.reviewPollHandle);
    ui.reviewPollHandle = window.setTimeout(
      () => checkAcknowledgedReview(result), reviewPollDelay,
    );
  }

  async function checkAcknowledgedReview(result) {
    try {
      const completed = await request('/api/reviews');
      const summary = completed.reviews.find((item) => item.attempt_id === result.attempt_id);
      renderAcknowledged(result, summary ? summary.review_state : 'waiting');
    } catch (error) {
      renderAcknowledged(
        result,
        reviewFailureAction(error.problem),
        error.problem,
      );
    }
  }

  function renderAcknowledged(result, reviewState = 'waiting', problem = {}) {
    showStatus('Result received', 'The coordinator accepted and scored your submission.');
    const score = document.createElement('p');
    score.className = 'result-score';
    setSafeText(score, `${result.score} / ${result.total_questions} (${result.percentage}%)`);
    elements.actionArea.append(score);
    const reviewStatus = document.createElement('p');
    if (reviewState === 'available') {
      setSafeText(reviewStatus, 'Faculty has closed the assessment. Your review is ready.');
      const reviewButton = button('Review answers', async () => {
        reviewButton.disabled = true;
        try {
          renderReview(await request(`/api/reviews/${encodeURIComponent(result.attempt_id)}`));
        } catch (error) {
          showProblem(error.problem);
          reviewButton.disabled = false;
        }
      });
      elements.actionArea.append(reviewStatus, reviewButton);
    } else if (reviewState === 'unavailable') {
      setSafeText(reviewStatus, 'Detailed review is unavailable for this older assessment.');
      const unavailable = button('Detailed review unavailable', () => {});
      unavailable.disabled = true;
      elements.actionArea.append(reviewStatus, unavailable);
    } else if (reviewState === 'signin') {
      setSafeText(reviewStatus, 'Your sign-in has expired. Sign in again to check review availability.');
      elements.actionArea.append(reviewStatus, button('Sign in again', renderLogin));
    } else if (reviewState === 'device') {
      setSafeText(reviewStatus, problemMessages.device_inactive);
      elements.actionArea.append(
        reviewStatus,
        button('Check registration', () => checkAcknowledgedReview(result)),
      );
    } else if (reviewState === 'stop') {
      setSafeText(reviewStatus, problemMessage(problem.code, problem.diagnostic_reference));
      elements.actionArea.append(
        reviewStatus,
        button('Check again', () => checkAcknowledgedReview(result)),
      );
    } else {
      setSafeText(
        reviewStatus,
        reviewState === 'retry'
          ? 'Could not check yet. Your result remains available; review will be checked again.'
          : 'Answers and solutions will be available after Faculty closes the assessment.',
      );
      elements.actionArea.append(
        reviewStatus,
        button('Check review availability', () => checkAcknowledgedReview(result)),
      );
    }
    stopPolling();
    if (reviewState === 'waiting' || reviewState === 'retry') {
      scheduleAcknowledgedReview(result);
    } else {
      if (ui.reviewPollHandle !== null) window.clearTimeout(ui.reviewPollHandle);
      ui.reviewPollHandle = null;
    }
  }

  function showProblem(problem = {}) {
    const message = problemMessage(problem.code, problem.diagnostic_reference);
    setSafeText(elements.statusCopy, message);
    announce(message, true);
  }

  async function pollAttempt() {
    if (!ui.attempt || ui.saving || ui.positionSaving) return;
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
      persistPosition(ui.questionIndex - 1);
    }
  });
  elements.next.addEventListener('click', () => {
    if (ui.attempt && ui.questionIndex < ui.attempt.questions.length - 1) {
      persistPosition(ui.questionIndex + 1);
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

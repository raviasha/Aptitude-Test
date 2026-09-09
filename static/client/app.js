'use strict';

const sealedMessage = 'Your answers are safe and will upload automatically.';
const reviewPollDelay = 5000;
const browserSessionKey = 'ksat-browser-session-active';

const problemMessages = {
  coordinator_unavailable: 'The assessment server is temporarily unavailable. Your saved work is safe.',
  start_window_closed: 'The 10-minute start window has closed. Ask Faculty for help.',
  content_hash_mismatch: 'The assessment download failed verification and will be downloaded again.',
  device_inactive: 'This lab computer is not registered. Ask Faculty or IT for help.',
  invalid_registration: 'Check the student details and use a password of at least 6 characters.',
  student_id_exists: 'That Student ID is already registered.',
  registration_rate_limited: 'Too many accounts are being created. Wait one minute and try again.',
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

function acknowledgedReviewStatus(reviewState, problem = {}) {
  if (reviewState === 'available') {
    return {
      message: 'Faculty has closed the assessment. Your review is ready.',
      action: 'review', label: 'Review answers', autoRetry: false,
    };
  }
  if (reviewState === 'unavailable') {
    return {
      message: 'Detailed review is unavailable for this older assessment.',
      action: 'unavailable', label: 'Detailed review unavailable', autoRetry: false,
    };
  }
  if (reviewState === 'signin') {
    return {
      message: 'Your sign-in has expired. Sign in again to check review availability.',
      action: 'signin', label: 'Sign in again', autoRetry: false,
    };
  }
  if (reviewState === 'device') {
    return {
      message: problemMessages.device_inactive,
      action: 'check', label: 'Check registration', autoRetry: false,
    };
  }
  if (reviewState === 'stop') {
    return {
      message: problemMessage(problem.code, problem.diagnostic_reference),
      action: 'check', label: 'Check again', autoRetry: false,
    };
  }
  return {
    message: reviewState === 'retry'
      ? 'Could not check yet. Your result remains available; review will be checked again.'
      : 'Answers and solutions will be available after Faculty closes the assessment.',
    action: 'check', label: 'Check review availability', autoRetry: true,
  };
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

function registrationPayload(values) {
  const password = String(values.password || '');
  if (password.length < 6) {
    throw new Error('Password must be at least 6 characters.');
  }
  if (values.password !== values.confirm_password) {
    throw new Error('Passwords do not match.');
  }
  return {
    student_id: String(values.student_id || '').trim(),
    name: String(values.name || '').trim(),
    student_class: String(values.student_class || '').trim(),
    section: String(values.section || '').trim(),
    password,
  };
}

function beginBrowserSession(storage) {
  try {
    const fresh = storage.getItem(browserSessionKey) !== 'true';
    storage.setItem(browserSessionKey, 'true');
    return fresh;
  } catch (_error) {
    return false;
  }
}

function shouldAutoLogout(freshBrowserSession, state) {
  return freshBrowserSession
    && (state === 'waiting_or_ready' || state === 'acknowledged_result');
}

async function logoutStudent(requester) {
  return requester('/api/logout', {
    method: 'POST',
    body: JSON.stringify({ confirmed: true }),
  });
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
  acknowledgedReviewStatus,
  assetUrl,
  beginBrowserSession,
  canEdit,
  logoutStudent,
  optimisticSelection,
  persistOptimisticAnswer,
  problemMessage,
  problemMessages,
  reviewChoiceState,
  reviewFailureAction,
  reviewAssetUrl,
  reviewPollDelay,
  registrationPayload,
  restoreSelection,
  saveStatusMessage,
  sealedMessage,
  setSafeText,
  shouldAutoLogout,
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = exported;
}

if (typeof document !== 'undefined') {
  const csrf = document.querySelector('meta[name="ksat-csrf"]').content;
  let freshBrowserSession = beginBrowserSession(window.sessionStorage);
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
    view: null,
    viewGeneration: 0,
    viewRequests: new Set(),
    acknowledgedCheck: null,
    attemptPoll: null,
    pendingReview: null,
    localReview: null,
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

  function beginView(view) {
    stopPolling();
    if (ui.reviewPollHandle !== null) window.clearTimeout(ui.reviewPollHandle);
    ui.reviewPollHandle = null;
    ui.viewRequests.forEach(controller => controller.abort());
    ui.viewRequests.clear();
    ui.view = view;
    ui.viewGeneration += 1;
    if (view === 'starting_attempt' || view === 'signing_out') {
      elements.actionArea.querySelectorAll('button').forEach(item => { item.disabled = true; });
    }
    return ui.viewGeneration;
  }

  function ownsView(view, generation) {
    return ui.view === view && ui.viewGeneration === generation;
  }

  async function viewRequest(path) {
    const controller = new AbortController();
    ui.viewRequests.add(controller);
    try {
      return await request(path, { signal: controller.signal });
    } finally {
      ui.viewRequests.delete(controller);
    }
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

  function signOutButton() {
    const action = button('Sign out', async () => {
      action.disabled = true;
      const generation = beginView('signing_out');
      try {
        await logoutStudent(request);
        if (!ownsView('signing_out', generation)) return;
        ui.state = 'login';
        ui.attempt = null;
        ui.pendingReview = null;
        ui.localReview = null;
        ui.questionIndex = 0;
        setSafeText(elements.timer, '--:--');
        if (document.fullscreenElement && typeof document.exitFullscreen === 'function') {
          try {
            await document.exitFullscreen();
          } catch (_error) {
            // The session is already signed out even if the browser refuses this cosmetic step.
          }
        }
        renderLogin();
      } catch (error) {
        if (!ownsView('signing_out', generation)) return;
        showProblem(error.problem);
        await refreshState();
      }
    }, 'secondary');
    return action;
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

  async function renderDeviceSetup() {
    showStatus('Connecting this lab computer', 'Completing automatic setup with the assessment server.');
    try {
      await request('/api/device/enroll', {
        method: 'POST',
        body: JSON.stringify({}),
      });
      await refreshState();
    } catch (error) {
      showProblem(error.problem);
      elements.actionArea.append(button('Try again', renderDeviceSetup, 'secondary'));
    }
  }

  function renderLogin() {
    beginView('login');
    ui.state = 'login';
    ui.pendingReview = null;
    ui.localReview = null;
    showStatus('Student sign in', 'Sign in while connected to the assessment network.');
    const form = document.createElement('form');
    const student = input('Student ID', 'text', 'username');
    const password = input('Password', 'password', 'current-password');
    const submit = button('Sign in', () => {});
    submit.type = 'submit';
    const register = button('New student? Create account', renderRegistration, 'secondary');
    form.append(student.label, password.label, submit, register);
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

  function renderRegistration() {
    beginView('registration');
    showStatus('Create student account', 'Use your official student details.');
    const form = document.createElement('form');
    const name = input('Student name', 'text', 'name');
    const student = input('Student ID / USN', 'text', 'username');
    const studentClass = input('Class', 'text', 'organization');
    const section = input('Section', 'text', 'off');
    const password = input('Password', 'password', 'new-password');
    const confirmation = input('Confirm password', 'password', 'new-password');
    password.control.minLength = 6;
    confirmation.control.minLength = 6;
    studentClass.control.value = 'AIML';
    section.control.value = 'A';
    const submit = button('Create account', () => {});
    submit.type = 'submit';
    const back = button('Back to sign in', renderLogin, 'secondary');
    form.append(
      name.label, student.label, studentClass.label, section.label,
      password.label, confirmation.label, submit, back,
    );
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        const payload = registrationPayload({
          student_id: student.control.value,
          name: name.control.value,
          student_class: studentClass.control.value,
          section: section.control.value,
          password: password.control.value,
          confirm_password: confirmation.control.value,
        });
        await request('/api/register', {
          method: 'POST',
          body: JSON.stringify(payload),
        });
        announce('Account created. You can now sign in.');
        renderLogin();
      } catch (error) {
        const message = error.problem
          ? problemMessage(error.problem.code, error.problem.diagnostic_reference)
          : error.message;
        setSafeText(elements.statusCopy, message);
        announce(message, true);
        submit.disabled = false;
      } finally {
        password.control.value = '';
        confirmation.control.value = '';
      }
    });
    elements.actionArea.append(form);
    name.control.focus();
  }

  async function loadAssessments() {
    const generation = beginView('assessments');
    ui.state = 'waiting_or_ready';
    showStatus('Available assessments', 'Checking for an assessment launched by Faculty.');
    const view = {
      generation,
      available: document.createElement('div'),
      completed: document.createElement('div'),
      assessmentBusy: false,
      reviewBusy: false,
    };
    view.refresh = button('Refresh', () => refreshAssessmentView(view), 'secondary');
    elements.actionArea.append(view.available, view.completed, view.refresh, signOutButton());
    await refreshAssessmentView(view);
  }

  function refreshAssessmentView(view) {
    const available = refreshAvailableAssessments(view);
    refreshCompletedAssessments(view);
    return available;
  }

  async function refreshAvailableAssessments(view) {
    if (!ownsView('assessments', view.generation) || view.assessmentBusy) return;
    view.assessmentBusy = true;
    view.refresh.disabled = true;
    if (ui.reviewPollHandle !== null) window.clearTimeout(ui.reviewPollHandle);
    ui.reviewPollHandle = null;
    try {
      const payload = await viewRequest('/api/assessments');
      if (!ownsView('assessments', view.generation)) return;
      clear(view.available);
      setSafeText(
        elements.statusCopy,
        payload.assessments.length
          ? 'Choose the launched assessment when Faculty asks you to begin.'
          : 'No assessment is ready to start. This page will check again.',
      );
      payload.assessments.forEach((assessment) => {
        const row = document.createElement('div');
        row.className = 'assessment-row';
        const name = document.createElement('strong');
        setSafeText(name, assessment.test_name);
        const action = button(assessment.content_ready ? 'Start' : 'Download', async () => {
          if (!ownsView('assessments', view.generation)) return;
          action.disabled = true;
          const generation = beginView('starting_attempt');
          try {
            if (!assessment.content_ready) {
              await request('/api/content/prefetch', {
                method: 'POST', body: JSON.stringify({ release_id: assessment.release_id }),
              });
              if (!ownsView('starting_attempt', generation)) return;
            }
            const attempt = await request(`/api/assessments/${encodeURIComponent(assessment.release_id)}/start`, {
              method: 'POST', body: JSON.stringify({ confirmed: true }),
            });
            if (!ownsView('starting_attempt', generation)) return;
            ui.fullscreenReady = false;
            ui.saveState = 'saved';
            await enterFullscreen();
            if (!ownsView('starting_attempt', generation)) return;
            renderAttempt(attempt);
          } catch (error) {
            if (!ownsView('starting_attempt', generation)) return;
            showProblem(error.problem);
            await refreshState();
          }
        });
        row.append(name, action);
        view.available.append(row);
      });
    } catch (error) {
      if (ownsView('assessments', view.generation)) showProblem(error.problem);
    } finally {
      view.assessmentBusy = false;
      if (ownsView('assessments', view.generation)) {
        view.refresh.disabled = false;
        ui.reviewPollHandle = window.setTimeout(
          () => refreshAssessmentView(view), reviewPollDelay,
        );
      }
    }
  }

  async function refreshCompletedAssessments(view) {
    if (!ownsView('assessments', view.generation) || view.reviewBusy) return;
    view.reviewBusy = true;
    try {
      const completed = await viewRequest('/api/reviews');
      if (!ownsView('assessments', view.generation)) return;
      clear(view.completed);
      if (completed.reviews.length) {
        const heading = document.createElement('h3');
        setSafeText(heading, 'Completed assessments');
        view.completed.append(heading);
      }
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
            if (!ownsView('assessments', view.generation)) return;
            action.disabled = true;
            await openReview(assessment);
          });
        } else if (assessment.review_state === 'waiting') {
          action = button('Waiting for Faculty to close', () => refreshAssessmentView(view));
        } else {
          action = button('Detailed review unavailable', () => {});
          action.disabled = true;
        }
        row.append(details, action);
        view.completed.append(row);
      });
    } catch (error) {
      if (ownsView('assessments', view.generation)) {
        announce(problemMessage(error.problem && error.problem.code), true);
      }
    } finally {
      view.reviewBusy = false;
    }
  }

  async function openReview(result) {
    const generation = beginView('loading_review');
    try {
      const review = await viewRequest(`/api/reviews/${encodeURIComponent(result.attempt_id)}`);
      if (ownsView('loading_review', generation)) renderReview(review);
    } catch (error) {
      if (ownsView('loading_review', generation)) renderAcknowledgedFailure(result, error.problem);
    }
  }

  function renderReview(review) {
    const generation = beginView(review.local_result ? 'local_review' : 'review');
    showStatus(review.test_name, 'Review of your submitted answers.');
    let uploadView;
    if (review.local_result) {
      uploadView = {
        generation, review, confirmed: null,
        score: document.createElement('p'),
        status: document.createElement('p'),
        actions: document.createElement('div'),
      };
      uploadView.score.className = 'result-score';
      ui.localReview = uploadView;
      elements.actionArea.append(uploadView.score, uploadView.status);
    }
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
    if (uploadView) {
      elements.actionArea.append(list, uploadView.actions);
      updateLocalReviewUpload(uploadView, review.result
        ? { ...ui.attempt, state: 'acknowledged_result', result: review.result }
        : ui.attempt);
    } else {
      elements.actionArea.append(
        list,
        button('Back to assessments', loadAssessments),
        signOutButton(),
      );
    }
  }

  function updateLocalReviewUpload(view, attempt) {
    if (!ownsView('local_review', view.generation)) return;
    ui.attempt = attempt;
    ui.state = attempt.state;
    const receipt = attempt.state === 'acknowledged_result' && attempt.result;
    const score = receipt || view.review.local_result;
    setSafeText(view.score,
      `${receipt ? 'Confirmed score' : 'Locally calculated score'}: ${score.score} / ${score.total_questions} (${score.percentage}%)`);
    setSafeText(view.status, receipt
      ? 'Upload confirmed. The coordinator accepted your submission.'
      : attempt.state === 'faculty_intervention_required'
        ? 'Upload needs Faculty attention. Your answers remain saved on this computer.'
        : 'Upload pending. Your answers are saved and will upload in the background.');
    if (view.confirmed !== Boolean(receipt)) {
      view.confirmed = Boolean(receipt);
      clear(view.actions);
      if (receipt) {
        view.actions.append(button('Back to assessments', loadAssessments), signOutButton());
      } else {
        view.actions.append(button('Back to submission', () => {
          if (ownsView('local_review', view.generation)) renderAttempt(ui.attempt);
        }));
      }
    }
    if (receipt) stopPolling();
    else startPolling();
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
    if (ui.view !== 'attempt') beginView('attempt');
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
    showStatus('Assessment submitted', `${sealedMessage} Uploading continues in the background.`);
    const queue = attempt.queue || {};
    const retry = document.createElement('p');
    setSafeText(retry, `Upload retries: ${queue.retry_count || 0}. Next attempt: ${queue.next_attempt_at || 'automatically'}. ${queue.last_error || ''}`);
    elements.actionArea.append(retry);
    renderPendingReviewControls(attempt);
    startPolling();
  }

  function renderIntervention(attempt) {
    showStatus('Faculty assistance required', problemMessages.faculty_intervention_required);
    const detail = document.createElement('p');
    setSafeText(detail, attempt.message || problemMessages.faculty_intervention_required);
    elements.actionArea.append(detail);
    renderPendingReviewControls(attempt);
    stopPolling();
  }

  function renderPendingReviewControls(attempt) {
    if (!ui.pendingReview || ui.pendingReview.generation !== ui.viewGeneration
        || ui.pendingReview.attemptId !== attempt.attempt_id) {
      const previous = ui.pendingReview;
      ui.pendingReview = {
        generation: ui.viewGeneration, attemptId: attempt.attempt_id,
        busy: false, stopped: false,
        message: 'Answers, solutions, and a locally calculated score are available after Faculty closes the assessment.',
        review: previous && previous.attemptId === attempt.attempt_id ? previous.review : null,
      };
    }
    const pending = ui.pendingReview;
    const message = document.createElement('p');
    setSafeText(message, pending.review
      ? 'Faculty has closed the assessment. You can review your saved answers.'
      : pending.message);
    const action = button(pending.review ? 'Review answers' : 'Check review availability', () => {
      if (!ownsView('attempt', pending.generation)) return;
      if (pending.review) renderReview(pending.review);
      else checkPendingReview(pending);
    });
    action.disabled = pending.busy;
    elements.actionArea.append(message, action);
    if (!pending.review && !pending.busy && !pending.stopped && ui.reviewPollHandle === null) {
      ui.reviewPollHandle = window.setTimeout(() => checkPendingReview(pending), reviewPollDelay);
    }
  }

  async function checkPendingReview(pending) {
    if (!ownsView('attempt', pending.generation) || pending.busy) return;
    pending.busy = true;
    if (ui.reviewPollHandle !== null) window.clearTimeout(ui.reviewPollHandle);
    ui.reviewPollHandle = null;
    renderAttempt(ui.attempt);
    try {
      const review = await viewRequest(`/api/attempts/${encodeURIComponent(pending.attemptId)}/review`);
      if (!ownsView('attempt', pending.generation)) return;
      pending.review = review;
      renderReview(review);
    } catch (error) {
      if (!ownsView('attempt', pending.generation)) return;
      const problem = error.problem || {};
      pending.stopped = problem.code !== 'review_not_released'
        && reviewFailureAction(problem) !== 'retry';
      pending.message = problem.code === 'review_not_released'
        ? problemMessages.review_not_released
        : `${problemMessage(problem.code, problem.diagnostic_reference)} Your upload status is checked separately.`;
    } finally {
      pending.busy = false;
      if (ownsView('attempt', pending.generation)) renderAttempt(ui.attempt);
    }
  }

  function scheduleAcknowledgedReview(result) {
    if (ui.reviewPollHandle !== null) window.clearTimeout(ui.reviewPollHandle);
    const generation = ui.viewGeneration;
    ui.reviewPollHandle = window.setTimeout(
      () => {
        if (ownsView('acknowledged', generation)) checkAcknowledgedReview(result);
      }, reviewPollDelay,
    );
  }

  async function checkAcknowledgedReview(result) {
    const generation = ui.viewGeneration;
    if (ui.view !== 'acknowledged' || ui.acknowledgedCheck === generation) return;
    ui.acknowledgedCheck = generation;
    if (ui.reviewPollHandle !== null) window.clearTimeout(ui.reviewPollHandle);
    ui.reviewPollHandle = null;
    try {
      const completed = await viewRequest('/api/reviews');
      if (!ownsView('acknowledged', generation)) return;
      const summary = completed.reviews.find((item) => item.attempt_id === result.attempt_id);
      renderAcknowledged(result, summary ? summary.review_state : 'waiting');
    } catch (error) {
      if (!ownsView('acknowledged', generation)) return;
      renderAcknowledged(
        result,
        reviewFailureAction(error.problem),
        error.problem,
      );
    } finally {
      if (ui.acknowledgedCheck === generation) ui.acknowledgedCheck = null;
    }
  }

  function renderAcknowledgedFailure(result, problem = {}) {
    renderAcknowledged(result, reviewFailureAction(problem), problem);
  }

  function renderAcknowledged(result, reviewState = 'waiting', problem = {}) {
    if (ui.view !== 'acknowledged') beginView('acknowledged');
    ui.state = 'acknowledged_result';
    showStatus('Result received', 'The coordinator accepted and scored your submission.');
    const score = document.createElement('p');
    score.className = 'result-score';
    setSafeText(score, `${result.score} / ${result.total_questions} (${result.percentage}%)`);
    elements.actionArea.append(score);
    const reviewStatus = document.createElement('p');
    const status = acknowledgedReviewStatus(reviewState, problem);
    setSafeText(reviewStatus, status.message);
    if (status.action === 'review') {
      const reviewButton = button(status.label, async () => {
        reviewButton.disabled = true;
        await openReview(result);
      });
      elements.actionArea.append(reviewStatus, reviewButton);
    } else if (status.action === 'unavailable') {
      const unavailable = button(status.label, () => {});
      unavailable.disabled = true;
      elements.actionArea.append(reviewStatus, unavailable);
    } else if (status.action === 'signin') {
      elements.actionArea.append(reviewStatus, button(status.label, renderLogin));
    } else {
      elements.actionArea.append(
        reviewStatus,
        button(status.label, () => checkAcknowledgedReview(result)),
      );
    }
    elements.actionArea.append(button('Back to assessments', loadAssessments), signOutButton());
    stopPolling();
    if (status.autoRetry) {
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
    const view = ui.view;
    if (!['attempt', 'local_review'].includes(view) || !ui.attempt || ui.saving || ui.positionSaving) return;
    const generation = ui.viewGeneration;
    if (ui.attemptPoll === generation) return;
    ui.attemptPoll = generation;
    const attemptId = ui.attempt.attempt_id;
    const state = ui.state;
    try {
      const attempt = await viewRequest(`/api/attempts/${encodeURIComponent(attemptId)}`);
      if (!ownsView(view, generation)) return;
      if (attempt.state === 'acknowledged_result' && !attempt.result) {
        const result = await viewRequest(`/api/attempts/${encodeURIComponent(attemptId)}/result`);
        attempt.result = result.result;
      }
      if (!ownsView(view, generation)
          || ui.state !== state || ui.saving || ui.positionSaving) return;
      if (view === 'local_review') updateLocalReviewUpload(ui.localReview, attempt);
      else renderAttempt(attempt);
    } catch (error) {
      if (ownsView(view, generation)) {
        if (view === 'local_review') {
          setSafeText(ui.localReview.status, 'Could not check upload status. Your answers remain saved; checking again.');
        } else showProblem(error.problem);
      }
    } finally {
      if (ui.attemptPoll === generation) ui.attemptPoll = null;
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
      if (shouldAutoLogout(freshBrowserSession, state.state)) {
        freshBrowserSession = false;
        await logoutStudent(request);
        ui.state = 'login';
        ui.attempt = null;
        setSafeText(elements.timer, '--:--');
        return renderLogin();
      }
      freshBrowserSession = false;
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

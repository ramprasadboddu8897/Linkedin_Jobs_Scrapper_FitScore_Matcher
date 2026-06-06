/* ============================================================
   LinkedIn Job Matcher — Application Logic
   Vanilla JavaScript • No dependencies
   ============================================================ */

// ─── Global Application State ───────────────────────────────
const state = {
  jobs: [],          // Raw scraped jobs (no scores)
  matchedJobs: [],   // Jobs with match scores attached
  skills: [],        // User's skill list
  coreSkills: [],    // User's starred core skill list
  resumeText: '',    // Extracted resume text (server-side)
  isLoading: false,
  isScraping: false,
  isMatching: false,
  currentPage: 1,    // Current page of results
  pageSize: 10,      // Number of jobs per page (responsive pagination)
};

// ─── DOM References ─────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const dom = {
  // Search
  keywords:        $('#keywords'),
  location:        $('#location'),
  datePosted:      $('#datePosted'),
  jobType:         $('#jobType'),
  experienceLevel: $('#experienceLevel'),
  workplaceType:   $('#workplaceType'),
  maxItems:        $('#maxItems'),
  scrapeBtn:       $('#scrape-btn'),
  scrapeStatus:    $('#scrape-status'),

  // Resume
  uploadZone:      $('#upload-zone'),
  resumeFile:      $('#resume-file'),
  skillsTags:      $('#skills-tags'),
  newSkill:        $('#new-skill'),
  addSkillBtn:     $('#add-skill-btn'),
  matchBtn:        $('#match-btn'),

  // Results
  resultsHeader:       $('#results-header'),
  resultsCount:        $('#results-count'),
  reloadCacheBtn:      $('#reload-cache-btn'),
  sortBy:              $('#sort-by'),
  jobsGrid:            $('#jobs-grid'),
  skeletonGrid:        $('#skeleton-grid'),
  paginationContainer: $('#pagination-container'),

  // Modal
  modal:           $('#job-modal'),
  modalClose:      $('#modal-close'),
  modalBody:       $('#modal-body'),

  // Toast
  toastContainer:  $('#toast-container'),
};


// ═══════════════════════════════════════════════════════════════
//  UTILITY HELPERS
// ═══════════════════════════════════════════════════════════════

/**
 * Safe fetch wrapper — returns parsed JSON or throws with a
 * user-friendly message.
 */
async function apiFetch(url, options = {}) {
  try {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      throw new Error(data?.message || `Server error (${res.status})`);
    }
    return data;
  } catch (err) {
    if (err.name === 'TypeError') {
      throw new Error('Network error — is the backend running?');
    }
    throw err;
  }
}

/** Return the CSS class suffix for a score value */
function scoreClass(score) {
  if (score > 70) return 'high';
  if (score >= 40) return 'mid';
  return 'low';
}

/** Return the hex color for a score value */
function scoreColor(score) {
  if (score > 70) return '#10b981';
  if (score >= 40) return '#f59e0b';
  return '#f43f5e';
}

/** Escape HTML to prevent XSS when inserting user data */
function esc(str) {
  const d = document.createElement('div');
  d.textContent = str ?? '';
  return d.innerHTML;
}


// ═══════════════════════════════════════════════════════════════
//  TOAST NOTIFICATIONS
// ═══════════════════════════════════════════════════════════════

/**
 * Show a toast notification in the bottom-right corner.
 * @param {string} message
 * @param {'success'|'error'|'info'} type
 */
function showToast(message, type = 'success') {
  const icons = { success: '✅', error: '❌', info: 'ℹ️' };

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <span class="toast-icon">${icons[type] || icons.info}</span>
    <span>${esc(message)}</span>
  `;

  dom.toastContainer.appendChild(toast);

  // Auto-dismiss after 4 seconds
  setTimeout(() => {
    toast.classList.add('toast-exit');
    toast.addEventListener('animationend', () => toast.remove());
  }, 4000);
}


// ═══════════════════════════════════════════════════════════════
//  SKELETON LOADERS
// ═══════════════════════════════════════════════════════════════

function showSkeletons() {
  dom.jobsGrid.hidden = true;
  dom.skeletonGrid.hidden = false;
}

function hideSkeletons() {
  dom.skeletonGrid.hidden = true;
  dom.jobsGrid.hidden = false;
}


// ═══════════════════════════════════════════════════════════════
//  SVG SCORE CIRCLE BUILDER
// ═══════════════════════════════════════════════════════════════

/**
 * Build an SVG circular progress indicator.
 * @param {number} score   - 0–100
 * @param {number} size    - px diameter (56 for cards, 80 for modal)
 * @param {number} stroke  - stroke width
 * @returns {string} HTML string
 */
function buildScoreCircle(score, size = 56, stroke = 4, extraClass = '') {
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (score / 100) * circumference;
  const cls = scoreClass(score);
  const color = scoreColor(score);

  return `
    <div class="match-score-circle score-${cls} ${extraClass}" style="width:${size}px;height:${size}px">
      <svg width="${size}" height="${size}">
        <circle class="score-bg" cx="${size / 2}" cy="${size / 2}" r="${radius}" stroke-width="${stroke}" />
        <circle class="score-fg"
          cx="${size / 2}" cy="${size / 2}" r="${radius}"
          stroke="${color}"
          stroke-width="${stroke}"
          stroke-dasharray="${circumference}"
          stroke-dashoffset="${offset}"
          style="--circumference:${circumference}"
        />
      </svg>
      <span class="score-text">${Math.round(score)}%</span>
    </div>
  `;
}


// ═══════════════════════════════════════════════════════════════
//  RENDER: SKILL TAGS
// ═══════════════════════════════════════════════════════════════

function renderSkillTags() {
  dom.skillsTags.innerHTML = state.skills
    .map(
      (skill) => {
        const isCore = state.coreSkills.some((s) => s.toLowerCase() === skill.toLowerCase());
        return `
        <span class="skill-tag${isCore ? ' core' : ''}">
          <span class="toggle-core-star" onclick="toggleCoreSkill('${esc(skill.replace(/'/g, "\\'"))}')" title="${isCore ? 'Core Skill (Starred)' : 'Make Core Skill'}">${isCore ? '★' : '☆'}</span>
          <span class="skill-name-text">${esc(skill)}</span>
          <button class="remove-skill" onclick="removeSkill('${esc(skill.replace(/'/g, "\\'"))}')" aria-label="Remove ${esc(skill)}">×</button>
        </span>`;
      }
    )
    .join('');

  // Enable match button only when we have jobs AND skills
  dom.matchBtn.disabled = !(state.jobs.length > 0 && state.skills.length > 0);
}

async function toggleCoreSkill(skillName) {
  const isCore = state.coreSkills.some((s) => s.toLowerCase() === skillName.toLowerCase());
  if (isCore) {
    state.coreSkills = state.coreSkills.filter((s) => s.toLowerCase() !== skillName.toLowerCase());
  } else {
    state.coreSkills.push(skillName);
  }
  renderSkillTags();

  try {
    await apiFetch('/api/resume/skills', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skills: state.skills, core_skills: state.coreSkills }),
    });
  } catch (err) {
    showToast(`Saved locally — ${err.message}`, 'info');
  }
}
window.toggleCoreSkill = toggleCoreSkill;


// ═══════════════════════════════════════════════════════════════
//  RENDER: JOB CARDS
// ═══════════════════════════════════════════════════════════════

function renderJobCards(jobs) {
  if (!jobs || jobs.length === 0) {
    dom.jobsGrid.innerHTML = '';
    if (dom.paginationContainer) dom.paginationContainer.hidden = true;
    return;
  }

  // Slicing for pagination
  const totalItems = jobs.length;
  const totalPages = Math.ceil(totalItems / state.pageSize);
  
  // Boundary check
  if (state.currentPage > totalPages) {
    state.currentPage = totalPages;
  }
  if (state.currentPage < 1) {
    state.currentPage = 1;
  }

  const startIndex = (state.currentPage - 1) * state.pageSize;
  const endIndex = Math.min(startIndex + state.pageSize, totalItems);
  const paginatedJobs = jobs.slice(startIndex, endIndex);

  dom.jobsGrid.innerHTML = paginatedJobs
    .map((job, index) => {
      const globalIndex = startIndex + index;
      const hasScore = job.match_score != null;
      const score = job.match_score ?? 0;

      // Badge helper — add contextual color classes
      const badgeHtml = (label, type) => {
        if (!label) return '';
        let extraClass = '';
        if (type === 'workplace' && label.toLowerCase().includes('remote')) extraClass = ' badge-remote';
        if (type === 'jobType' && label.toLowerCase().includes('full')) extraClass = ' badge-fulltime';
        if (type === 'target') extraClass = ' badge-target-company';
        return `<span class="badge${extraClass}">${esc(label)}</span>`;
      };

      // Matched / unmatched / partial skill chips
      let skillsHtml = '';
      if (hasScore && state.skills.length > 0) {
        const matched = (job.matched_skills || []).map((s) => s.toLowerCase());
        const partial = (job.partial_skills || []).map((s) => s.toLowerCase());

        skillsHtml = `
          <div class="job-skills">
            ${state.skills
              .map((s) => {
                const s_low = s.toLowerCase();
                const isCore = state.coreSkills.some((cs) => cs.toLowerCase() === s_low);
                const star = isCore ? '★ ' : '';
                const coreCls = isCore ? ' core' : '';

                if (matched.includes(s_low)) {
                  const analysis = job.skills_analysis?.[s];
                  let title = 'Fully matched';
                  if (analysis && analysis.reason === 'concept_match') {
                    const ctx = analysis.matched_context_terms || [];
                    title = `Semantic match via [${analysis.concept_group}]: matched related terms (${ctx.join(', ')})`;
                    return `<span class="skill-semantic${coreCls}" title="${esc(title)}">${star}${esc(s)}</span>`;
                  }
                  return `<span class="skill-matched${coreCls}" title="Fully matched">${star}${esc(s)}</span>`;
                } else if (partial.includes(s_low)) {
                  const analysis = job.skills_analysis?.[s];
                  const missing = (analysis?.tokens || []).filter((t) => !t.matched).map((t) => t.token);
                  return `<span class="skill-partial${coreCls}" title="Missing: ${esc(missing.join(', '))}">${star}${esc(s)} <small style="opacity:0.75;font-size:0.65rem">(${esc(missing.join(', '))})</small></span>`;
                } else {
                  let title = 'Not found';
                  const analysis = job.skills_analysis?.[s];
                  if (analysis && analysis.tokens && analysis.tokens.length > 0) {
                    const missing = analysis.tokens.filter((t) => !t.matched).map((t) => t.token);
                    title = `Missing: ${missing.join(', ')}`;
                  }
                  return `<span class="skill-unmatched" title="${esc(title)}">${esc(s)}</span>`;
                }
              })
              .join('')}
          </div>
        `;
      }

      const isTarget = job.is_partner_company === true;
      const targetClass = isTarget ? ' target-company' : '';

      return `
        <div class="job-card${targetClass}" style="animation-delay:${index * 0.05}s">
          <div class="card-header">
            ${hasScore ? buildScoreCircle(score) : ''}
            <div class="job-info">
              <h3 class="job-title">${esc(job.title || '')}</h3>
              <p class="job-company">${esc(job.companyName || job.company || '')}</p>
            </div>
          </div>
          <p class="job-location">📍 ${esc(job.location || 'Location not specified')}</p>
          <p class="job-date">🕐 ${esc(job.postedAt || job.postedDate || 'Date not available')}</p>
          ${job.salary ? `<p class="job-salary">💰 ${esc(job.salary)}</p>` : ''}
          <div class="job-badges">
            ${isTarget ? badgeHtml('🎯 Recruiter Network', 'target') : ''}
            ${badgeHtml(job.employmentType || job.jobType, 'jobType')}
            ${badgeHtml(job.seniorityLevel || job.experienceLevel, 'experience')}
            ${badgeHtml(job.applicantsCount ? job.applicantsCount + ' applicants' : '', 'workplace')}
          </div>
          ${skillsHtml}
          <div class="card-actions">
            <button class="btn-details" onclick="showJobDetail(${globalIndex})">View Details</button>
            ${
              (job.applyUrl || job.link)
                ? `<a class="btn-apply" href="${esc(job.applyUrl || job.link)}" target="_blank" rel="noopener">Apply →</a>`
                : ''
            }
          </div>
        </div>
      `;
    })
    .join('');

  renderPagination(totalItems);
}

// ═══════════════════════════════════════════════════════════════
//  RENDER: PAGINATION CONTROLS
// ═══════════════════════════════════════════════════════════════

function renderPagination(totalItems) {
  if (!dom.paginationContainer) return;

  const totalPages = Math.ceil(totalItems / state.pageSize);
  if (totalPages <= 1) {
    dom.paginationContainer.hidden = true;
    return;
  }

  dom.paginationContainer.hidden = false;

  const isMobile = window.innerWidth <= 768;
  const prevDisabled = state.currentPage === 1 ? 'disabled' : '';
  const nextDisabled = state.currentPage === totalPages ? 'disabled' : '';

  let html = '';

  // Previous Page Button
  html += `
    <button class="pagination-btn" onclick="changePage(${state.currentPage - 1})" ${prevDisabled} aria-label="Previous Page">
      <span>◀</span> ${isMobile ? '' : 'Prev'}
    </button>
  `;

  if (isMobile) {
    // Compact mobile pagination info
    html += `<span class="pagination-info">Page ${state.currentPage} of ${totalPages}</span>`;
  } else {
    // Full page buttons for desktop viewports
    html += `<div class="pagination-pages">`;
    
    const pages = [];
    if (totalPages <= 7) {
      for (let i = 1; i <= totalPages; i++) {
        pages.push(i);
      }
    } else {
      if (state.currentPage <= 4) {
        pages.push(1, 2, 3, 4, 5, '...', totalPages);
      } else if (state.currentPage >= totalPages - 3) {
        pages.push(1, '...', totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages);
      } else {
        pages.push(1, '...', state.currentPage - 1, state.currentPage, state.currentPage + 1, '...', totalPages);
      }
    }

    pages.forEach((p) => {
      if (p === '...') {
        html += `<span class="pagination-info">...</span>`;
      } else {
        const activeClass = p === state.currentPage ? ' active' : '';
        html += `<button class="pagination-page-btn${activeClass}" onclick="changePage(${p})">${p}</button>`;
      }
    });

    html += `</div>`;
  }

  // Next Page Button
  html += `
    <button class="pagination-btn" onclick="changePage(${state.currentPage + 1})" ${nextDisabled} aria-label="Next Page">
      ${isMobile ? '' : 'Next'} <span>▶</span>
    </button>
  `;

  dom.paginationContainer.innerHTML = html;
}

function changePage(pageNum) {
  const jobs = state.matchedJobs.length > 0 ? state.matchedJobs : state.jobs;
  const totalPages = Math.ceil(jobs.length / state.pageSize);
  
  if (pageNum < 1) pageNum = 1;
  if (pageNum > totalPages) pageNum = totalPages;
  
  state.currentPage = pageNum;
  renderJobCards(jobs);
  
  // Smooth scroll to results header
  if (dom.resultsHeader) {
    dom.resultsHeader.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

// Expose globally
window.changePage = changePage;


// ═══════════════════════════════════════════════════════════════
//  RENDER: RESULTS HEADER
// ═══════════════════════════════════════════════════════════════

function updateResultsHeader(jobs) {
  if (!jobs || jobs.length === 0) {
    dom.resultsHeader.hidden = true;
    return;
  }
  dom.resultsHeader.hidden = false;
  const hasScores = jobs.some((j) => j.match_score != null);
  dom.resultsCount.innerHTML = hasScores
    ? `Showing <strong>${jobs.length}</strong> matched jobs`
    : `Showing <strong>${jobs.length}</strong> scraped jobs`;
}


// ═══════════════════════════════════════════════════════════════
//  SCRAPE JOBS
// ═══════════════════════════════════════════════════════════════

async function scrapeJobs() {
  if (state.isScraping) return;

  // Validate at least keywords
  const keywords = dom.keywords.value.trim();
  if (!keywords) {
    showToast('Please enter job keywords to search', 'error');
    dom.keywords.focus();
    return;
  }

  state.isScraping = true;
  dom.scrapeBtn.disabled = true;
  dom.scrapeBtn.classList.add('btn-loading');

  // Status progress messages
  const statusMessages = [
    '🔄 Starting Apify actor…',
    '🌐 Scraping LinkedIn…',
    '⚙️ Processing results…',
  ];

  let msgIndex = 0;
  dom.scrapeStatus.innerHTML = `<span class="status-spinner"></span> ${statusMessages[0]}`;
  const statusInterval = setInterval(() => {
    msgIndex = Math.min(msgIndex + 1, statusMessages.length - 1);
    dom.scrapeStatus.innerHTML = `<span class="status-spinner"></span> ${statusMessages[msgIndex]}`;
  }, 3000);

  showSkeletons();
  state.currentPage = 1;

  try {
    const payload = {
      keywords,
      location: dom.location.value.trim(),
      datePosted: dom.datePosted.value,
      jobType: dom.jobType.value,
      experienceLevel: dom.experienceLevel.value,
      workplaceType: dom.workplaceType.value,
      maxItems: parseInt(dom.maxItems.value, 10) || 25,
    };

    const data = await apiFetch('/api/scrape', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    // Backend returns raw array or {error: ...}
    state.jobs = Array.isArray(data) ? data : (data.jobs || []);
    state.matchedJobs = []; // reset matched results on new scrape

    hideSkeletons();
    renderJobCards(state.jobs);
    updateResultsHeader(state.jobs);

    // Enable match button if we also have skills
    dom.matchBtn.disabled = !(state.skills.length > 0);

    showToast(`Found ${state.jobs.length} jobs`, 'success');
    dom.scrapeStatus.textContent = `✅ ${state.jobs.length} jobs scraped successfully`;
  } catch (err) {
    hideSkeletons();
    showToast(err.message, 'error');
    dom.scrapeStatus.textContent = `❌ ${err.message}`;
  } finally {
    clearInterval(statusInterval);
    state.isScraping = false;
    dom.scrapeBtn.disabled = false;
    dom.scrapeBtn.classList.remove('btn-loading');
  }
}


// ═══════════════════════════════════════════════════════════════
//  RESUME UPLOAD
// ═══════════════════════════════════════════════════════════════

async function uploadResume(file) {
  if (!file) return;

  // Validate file type
  if (!file.name.toLowerCase().endsWith('.docx')) {
    showToast('Please upload a .docx file', 'error');
    return;
  }

  const formData = new FormData();
  formData.append('resume', file);

  // Visual feedback
  dom.uploadZone.classList.add('uploaded');
  dom.uploadZone.querySelector('.upload-label').textContent = `Uploading ${file.name}…`;

  try {
    const data = await apiFetch('/api/resume/upload', {
      method: 'POST',
      body: formData,
    });

    state.skills = data.skills || [];
    state.resumeText = data.full_text || data.resumeText || '';
    renderSkillTags();

    dom.uploadZone.querySelector('.upload-label').textContent = `✅ ${file.name}`;
    dom.uploadZone.querySelector('.upload-hint').textContent = 'Resume parsed successfully';

    showToast('Resume uploaded & parsed', 'success');

    // Auto-trigger matching if we already have jobs loaded
    if (state.jobs.length > 0) {
      matchJobs();
    }
  } catch (err) {
    dom.uploadZone.classList.remove('uploaded');
    dom.uploadZone.querySelector('.upload-label').textContent =
      'Drag & drop your DOCX resume or click to upload';
    showToast(err.message, 'error');
  }
}


// ═══════════════════════════════════════════════════════════════
//  SKILL MANAGEMENT
// ═══════════════════════════════════════════════════════════════

async function addSkill(skillName) {
  const name = (skillName || dom.newSkill.value).trim();
  if (!name) return;

  // Prevent duplicates (case-insensitive)
  if (state.skills.some((s) => s.toLowerCase() === name.toLowerCase())) {
    showToast('Skill already exists', 'info');
    return;
  }

  state.skills.push(name);
  dom.newSkill.value = '';
  renderSkillTags();

  try {
    await apiFetch('/api/resume/skills', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skills: state.skills, core_skills: state.coreSkills }),
    });
  } catch (err) {
    // Skill saved locally even if API fails
    showToast(`Skill added locally — ${err.message}`, 'info');
  }
}

async function removeSkill(skillName) {
  state.skills = state.skills.filter(
    (s) => s.toLowerCase() !== skillName.toLowerCase()
  );
  state.coreSkills = state.coreSkills.filter(
    (s) => s.toLowerCase() !== skillName.toLowerCase()
  );
  renderSkillTags();

  try {
    await apiFetch('/api/resume/skills', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skills: state.skills, core_skills: state.coreSkills }),
    });
  } catch (err) {
    showToast(`Skill removed locally — ${err.message}`, 'info');
  }
}

// Expose to inline onclick handlers
window.removeSkill = removeSkill;


// ═══════════════════════════════════════════════════════════════
//  MATCH JOBS
// ═══════════════════════════════════════════════════════════════

async function matchJobs() {
  if (state.isMatching || state.jobs.length === 0 || state.skills.length === 0) return;

  state.isMatching = true;
  dom.matchBtn.disabled = true;
  dom.matchBtn.classList.add('btn-loading');
  showSkeletons();
  state.currentPage = 1;

  try {
    const data = await apiFetch('/api/match', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        jobs: state.jobs,
        skills: state.skills,
        core_skills: state.coreSkills,
      }),
    });

    // Backend returns raw array of scored jobs
    state.matchedJobs = Array.isArray(data) ? data : (data.matchedJobs || []);

    // Sort by score descending by default
    state.matchedJobs.sort((a, b) => (b.match_score ?? 0) - (a.match_score ?? 0));
    dom.sortBy.value = 'score';

    hideSkeletons();
    renderJobCards(state.matchedJobs);
    updateResultsHeader(state.matchedJobs);

    showToast(`Matched ${state.matchedJobs.length} jobs to your resume`, 'success');
  } catch (err) {
    hideSkeletons();
    renderJobCards(state.jobs); // Fallback to unscored
    showToast(err.message, 'error');
  } finally {
    state.isMatching = false;
    dom.matchBtn.disabled = false;
    dom.matchBtn.classList.remove('btn-loading');
  }
}

async function reloadCache() {
  if (state.isLoading) return;
  state.isLoading = true;
  state.currentPage = 1;

  if (dom.reloadCacheBtn) {
    dom.reloadCacheBtn.disabled = true;
    dom.reloadCacheBtn.classList.add('btn-loading');
  }
  showSkeletons();

  try {
    const jobsData = await apiFetch('/api/jobs');
    const cachedJobs = Array.isArray(jobsData) ? jobsData : (jobsData?.jobs || []);
    if (cachedJobs.length > 0) {
      state.jobs = cachedJobs;
      state.matchedJobs = []; // clear previous matched jobs

      // If we already have skills, automatically run matching!
      if (state.skills.length > 0) {
        hideSkeletons();
        await matchJobs();
      } else {
        hideSkeletons();
        renderJobCards(state.jobs);
        updateResultsHeader(state.jobs);
        showToast(`Loaded ${state.jobs.length} jobs from cache`, 'success');
      }
    } else {
      hideSkeletons();
      showToast('No cached jobs found. Please run a new scrape first.', 'info');
    }
  } catch (err) {
    hideSkeletons();
    showToast(`Failed to load cache: ${err.message}`, 'error');
  } finally {
    state.isLoading = false;
    if (dom.reloadCacheBtn) {
      dom.reloadCacheBtn.disabled = false;
      dom.reloadCacheBtn.classList.remove('btn-loading');
    }
  }
}


// ═══════════════════════════════════════════════════════════════
//  SORT JOBS
// ═══════════════════════════════════════════════════════════════

function sortJobs(criteria) {
  const jobs = state.matchedJobs.length > 0 ? state.matchedJobs : state.jobs;
  if (jobs.length === 0) return;
  state.currentPage = 1;

  switch (criteria) {
    case 'score':
      jobs.sort((a, b) => (b.match_score ?? 0) - (a.match_score ?? 0));
      break;
    case 'date':
      jobs.sort((a, b) => {
        // Try ISO date, fallback to string compare
        const da = new Date(a.postedDate || 0);
        const db = new Date(b.postedDate || 0);
        return db - da;
      });
      break;
    case 'company':
      jobs.sort((a, b) => (a.companyName || a.company || '').localeCompare(b.companyName || b.company || ''));
      break;
    case 'location':
      jobs.sort((a, b) => (a.location || '').localeCompare(b.location || ''));
      break;
  }

  renderJobCards(jobs);
}


// ═══════════════════════════════════════════════════════════════
//  JOB DETAIL MODAL
// ═══════════════════════════════════════════════════════════════

function showJobDetail(index) {
  const jobs = state.matchedJobs.length > 0 ? state.matchedJobs : state.jobs;
  const job = jobs[index];
  if (!job) return;

  const hasScore = job.match_score != null;
  const score = job.match_score ?? 0;

  // Score breakdown section
  let scoreHtml = '';
  if (hasScore) {
    const keywordScore = job.keyword_score ?? score;
    const tfidfScore = job.tfidf_score ?? score;

    scoreHtml = `
      <div class="modal-score-display">
        ${buildScoreCircle(score, 80, 5, 'modal-score-circle')}
        <div class="modal-score-label">
          <strong>Overall Match Score</strong>
          Based on keyword overlap and TF-IDF similarity
        </div>
      </div>
      <div class="score-breakdown">
        <h4>Score Breakdown</h4>
        <div class="score-bar-group">
          <div class="score-bar-label">
            <span>Keyword Match</span>
            <span>${Math.round(keywordScore)}%</span>
          </div>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:${keywordScore}%"></div>
          </div>
        </div>
        <div class="score-bar-group">
          <div class="score-bar-label">
            <span>TF-IDF Similarity</span>
            <span>${Math.round(tfidfScore)}%</span>
          </div>
          <div class="score-bar-track">
            <div class="score-bar-fill" style="width:${tfidfScore}%"></div>
          </div>
        </div>
      </div>
    `;
  }

  // Skills section
  let skillsHtml = '';
  if (hasScore && state.skills.length > 0) {
    const matched = (job.matched_skills || []).map((s) => s.toLowerCase());
    const partial = (job.partial_skills || []).map((s) => s.toLowerCase());

    skillsHtml = `
      <div class="modal-skills-section">
        <h4>Skills Analysis</h4>
        <div class="modal-skills-list">
          ${state.skills
            .map((s) => {
              const s_low = s.toLowerCase();
              const isCore = state.coreSkills.some(cs => cs.toLowerCase() === s_low);
              const star = isCore ? '★ ' : '';
              const coreCls = isCore ? ' core-match' : '';
              const analysis = job.skills_analysis?.[s];

              if (matched.includes(s_low)) {
                if (analysis && analysis.semantic) {
                  const title = `Matched semantically via concept group: ${analysis.concept_group}. Terms found: ${analysis.matched_context_terms.join(', ')}`;
                  return `<span class="skill-semantic${coreCls}" title="${esc(title)}">${star}${esc(s)}</span>`;
                }
                return `<span class="skill-matched${coreCls}" title="Fully matched">${star}${esc(s)}</span>`;
              } else if (partial.includes(s_low)) {
                const missing = (analysis?.tokens || []).filter((t) => !t.matched).map((t) => t.token);
                return `<span class="skill-partial${coreCls}" title="Missing: ${esc(missing.join(', '))}">${star}${esc(s)} <small style="opacity:0.75;font-size:0.65rem">(${esc(missing.join(', '))})</small></span>`;
              } else {
                let title = 'Not found';
                if (analysis && analysis.tokens && analysis.tokens.length > 0) {
                  const missing = analysis.tokens.filter((t) => !t.matched).map((t) => t.token);
                  title = `Missing: ${missing.join(', ')}`;
                }
                return `<span class="skill-unmatched" title="${esc(title)}">${esc(s)}</span>`;
              }
            })
            .join('')}
        </div>
      </div>
    `;
  }

  // Description — prefer HTML version, fallback to text
  const descriptionContent = job.descriptionHtml || job.descriptionText || job.description || 'No description available.';
  const descriptionHtml = descriptionContent.includes('<')
    ? descriptionContent
    : `<p>${esc(descriptionContent)}</p>`;

  dom.modalBody.innerHTML = `
    <h2 class="modal-job-title">${esc(job.title)}</h2>
    <p class="modal-company">${esc(job.companyName || job.company || '')}</p>
    <div class="modal-meta">
      <span>📍 ${esc(job.location || 'N/A')}</span>
      <span>🕐 ${esc(job.postedAt || job.postedDate || 'N/A')}</span>
      ${job.employmentType ? `<span>💼 ${esc(job.employmentType)}</span>` : ''}
      ${job.seniorityLevel ? `<span>📊 ${esc(job.seniorityLevel)}</span>` : ''}
      ${job.salary ? `<span>💰 ${esc(job.salary)}</span>` : ''}
      ${job.applicantsCount ? `<span>👥 ${esc(String(job.applicantsCount))} applicants</span>` : ''}
    </div>
    ${job.is_partner_company ? `
      <div style="margin: -0.5rem 0 1rem; display: inline-flex; align-items: center; gap: 0.5rem; background: rgba(6, 182, 212, 0.1); border: 1px solid rgba(6, 182, 212, 0.25); padding: 0.5rem 1rem; border-radius: var(--radius-md)">
        <span style="font-size:1.1rem">🎯</span>
        <span style="font-size:0.85rem; font-weight:600; color:var(--accent-cyan)">Recruiter Network Target Company</span>
        <a href="${esc(job.partner_company_info.url)}" target="_blank" rel="noopener" style="font-size:0.8rem; color:#fff; background:var(--accent-cyan); border-radius:var(--radius-sm); padding:0.25rem 0.6rem; text-decoration:none; margin-left:0.5rem">Careers Link ↗</a>
      </div>
    ` : ''}
    ${scoreHtml}
    ${skillsHtml}
    <div class="modal-description">
      ${descriptionHtml}
    </div>
    ${
      (job.applyUrl || job.link)
        ? `<a class="modal-apply-btn" href="${esc(job.applyUrl || job.link)}" target="_blank" rel="noopener">Apply for this position →</a>`
        : ''
    }
  `;

  dom.modal.hidden = false;
  document.body.style.overflow = 'hidden';
}

// Expose to inline onclick handlers
window.showJobDetail = showJobDetail;

function closeModal() {
  dom.modal.hidden = true;
  document.body.style.overflow = '';
}


// ═══════════════════════════════════════════════════════════════
//  INITIAL DATA LOAD
// ═══════════════════════════════════════════════════════════════

/**
 * On page load, attempt to fetch any cached jobs and skills
 * from the backend so the user sees previous session data.
 */
async function loadCachedData() {
  state.currentPage = 1;
  // Fetch cached jobs
  try {
    const jobsData = await apiFetch('/api/jobs');
    // Backend returns raw array
    const cachedJobs = Array.isArray(jobsData) ? jobsData : (jobsData?.jobs || []);
    if (cachedJobs.length) {
      state.jobs = cachedJobs;
      renderJobCards(state.jobs);
      updateResultsHeader(state.jobs);
    }
  } catch {
    // No cached jobs — that's fine
  }

  // Fetch cached skills
  try {
    const skillsData = await apiFetch('/api/resume/skills');
    if (skillsData?.skills?.length) {
      state.skills = skillsData.skills;
      state.coreSkills = skillsData.core_skills || [];
      renderSkillTags();
    }
  } catch {
    // No cached skills — that's fine
  }
}


// ═══════════════════════════════════════════════════════════════
//  EVENT LISTENERS
// ═══════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
  // ── Scrape Button ──
  dom.scrapeBtn.addEventListener('click', scrapeJobs);

  // ── Resume File Input (click on upload zone) ──
  dom.uploadZone.addEventListener('click', () => dom.resumeFile.click());

  dom.resumeFile.addEventListener('change', (e) => {
    if (e.target.files[0]) uploadResume(e.target.files[0]);
  });

  // ── Drag & Drop ──
  dom.uploadZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dom.uploadZone.classList.add('drag-over');
  });

  dom.uploadZone.addEventListener('dragleave', () => {
    dom.uploadZone.classList.remove('drag-over');
  });

  dom.uploadZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dom.uploadZone.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file) uploadResume(file);
  });

  // ── Add Skill ──
  dom.addSkillBtn.addEventListener('click', () => addSkill());

  dom.newSkill.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addSkill();
    }
  });

  // ── Match Button ──
  dom.matchBtn.addEventListener('click', matchJobs);

  // ── Reload Cache Button ──
  if (dom.reloadCacheBtn) {
    dom.reloadCacheBtn.addEventListener('click', reloadCache);
  }

  // ── Sort ──
  dom.sortBy.addEventListener('change', (e) => sortJobs(e.target.value));

  // ── Modal Close ──
  dom.modalClose.addEventListener('click', closeModal);

  // Close modal on overlay click (outside content)
  dom.modal.addEventListener('click', (e) => {
    if (e.target === dom.modal) closeModal();
  });

  // Close modal on Escape key
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !dom.modal.hidden) closeModal();
  });

  // ── City Quick-Pick Chips (Multi-Select) ──
  const cityChips = $$('.city-chip');
  cityChips.forEach((chip) => {
    chip.addEventListener('click', () => {
      // Toggle active status on click
      chip.classList.toggle('active');

      // Collect all active locations
      const activeCities = [];
      cityChips.forEach((c) => {
        if (c.classList.contains('active')) {
          activeCities.push(c.getAttribute('data-city'));
        }
      });

      // Update location input field using semicolon delimiter
      dom.location.value = activeCities.join('; ');
    });
  });

  // Keep chips in sync with manual input
  dom.location.addEventListener('input', () => {
    const val = dom.location.value.toLowerCase();
    cityChips.forEach((chip) => {
      const city = chip.getAttribute('data-city').toLowerCase();
      // If the input value contains this city, make the chip active
      if (val.includes(city)) {
        chip.classList.add('active');
      } else {
        chip.classList.remove('active');
      }
    });
  });

  // ── Window Resize Event for Responsive Pagination ──
  window.addEventListener('resize', () => {
    if (!dom.paginationContainer || dom.paginationContainer.hidden) return;
    const jobs = state.matchedJobs.length > 0 ? state.matchedJobs : state.jobs;
    if (jobs.length > 0) {
      renderPagination(jobs.length);
    }
  });

  // ── Load cached data ──
  loadCachedData();
});

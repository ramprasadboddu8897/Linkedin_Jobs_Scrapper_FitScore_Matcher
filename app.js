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
  connections: [],   // LinkedIn connections list
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
  resumeRemoveBtn: $('#resume-remove-btn'),
  skillsTags:      $('#skills-tags'),
  newSkill:        $('#new-skill'),
  addSkillBtn:     $('#add-skill-btn'),
  matchBtn:        $('#match-btn'),
  connectionsUploadZone: $('#connections-upload-zone'),
  connectionsFile:       $('#connections-file'),
  connectionsCountText:  $('#connections-count-text'),
  clearConnectionsBtn:   $('#clear-connections-btn'),

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

/** Clean company name by removing punctuation and corporate suffixes */
function cleanCompanyName(name) {
  if (!name) return "";
  name = name.toLowerCase();
  name = name.replace(/[^\w\s]/g, "");
  const suffixes = new Set(["inc", "llc", "ltd", "co", "corp", "corporation", "pvt", "private", "limited", "solutions", "services", "technologies", "technology"]);
  const words = name.split(/\s+/).filter(w => w && !suffixes.has(w));
  return words.join(" ");
}

/** Correctly parses a single CSV line handling quoted fields containing commas */
function parseCSVLine(line) {
  const result = [];
  let current = "";
  let inQuotes = false;
  
  for (let i = 0; i < line.length; i++) {
    const char = line[i];
    if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === ',' && !inQuotes) {
      result.push(current.trim());
      current = "";
    } else {
      current += char;
    }
  }
  result.push(current.trim());
  return result;
}

/** Parses the LinkedIn exported Connections CSV text into a structured array */
function parseConnectionsCSV(csvText) {
  const lines = csvText.split(/\r?\n/);
  let headerIndex = -1;
  
  // LinkedIn CSV has empty lines or a "Connections" title line at the top.
  // Find the line containing the main headers.
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].includes("First Name") && lines[i].includes("Company")) {
      headerIndex = i;
      break;
    }
  }
  
  if (headerIndex === -1) {
    headerIndex = 0;
  }
  
  const headersLine = lines[headerIndex];
  if (!headersLine) return [];
  
  const headers = parseCSVLine(headersLine);
  const firstNameIdx = headers.indexOf("First Name");
  const lastNameIdx = headers.indexOf("Last Name");
  const companyIdx = headers.indexOf("Company");
  const positionIdx = headers.indexOf("Position");
  const urlIdx = headers.indexOf("URL");
  
  if (firstNameIdx === -1 || companyIdx === -1) {
    throw new Error("Invalid CSV format. Please upload a valid LinkedIn Connections export.");
  }
  
  const connections = [];
  for (let i = headerIndex + 1; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) continue;
    
    const row = parseCSVLine(line);
    if (row.length < headers.length) continue;
    
    const firstName = row[firstNameIdx] || "";
    const lastName = row[lastNameIdx] || "";
    const company = row[companyIdx] || "";
    const position = row[positionIdx] || "";
    const url = row[urlIdx] || "";
    
    if (firstName && company) {
      connections.push({
        name: `${firstName} ${lastName}`.trim(),
        company: company.trim(),
        position: position.trim(),
        url: url.trim()
      });
    }
  }
  
  return connections;
}

/** Cross-references jobs with the user's connection list and attaches matching results */
function matchConnectionsToJobs(jobs, connections) {
  if (!jobs || jobs.length === 0) return jobs;
  
  if (!connections || connections.length === 0) {
    jobs.forEach(job => {
      job.network_connections = [];
    });
    return jobs;
  }
  
  const connMap = new Map();
  connections.forEach(c => {
    const cleaned = cleanCompanyName(c.company);
    if (cleaned) {
      if (!connMap.has(cleaned)) {
        connMap.set(cleaned, []);
      }
      connMap.get(cleaned).push(c);
    }
  });
  
  jobs.forEach(job => {
    const companyName = job.companyName || job.company || "";
    const cleanedJob = cleanCompanyName(companyName);
    const matches = [];
    
    if (cleanedJob) {
      // Direct match
      if (connMap.has(cleanedJob)) {
        matches.push(...connMap.get(cleanedJob));
      } else {
        // Substring match for matching words
        for (const [cleanedC, conns] of connMap.entries()) {
          if (cleanedC.length >= 4 && cleanedJob.length >= 4) {
            if (cleanedC.includes(cleanedJob) || cleanedJob.includes(cleanedC)) {
              matches.push(...conns);
            }
          }
        }
      }
    }
    
    // Deduplicate matches
    const seen = new Set();
    const uniqueMatches = [];
    matches.forEach(m => {
      if (!seen.has(m.name)) {
        seen.add(m.name);
        uniqueMatches.push(m);
      }
    });
    
    job.network_connections = uniqueMatches;
  });
  
  return jobs;
}

/**
 * Safe fetch wrapper — returns parsed JSON or throws with a
 * user-friendly message.
 */
async function apiFetch(url, options = {}) {
  try {
    const res = await fetch(url, options);
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      throw new Error(data?.error || data?.message || `Server error (${res.status})`);
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
//  RECRUITER OUTREACH & TELEMETRY
// ═══════════════════════════════════════════════════════════════

/** Generate mailto: link for recruiter direct outreach */
function buildRecruiterMailto(recruiterEmail, recruiterName, companyName, jobTitle) {
  const subject = encodeURIComponent(
    `Application: ServiceNow Developer — ${companyName || 'Hiring Team'}`
  );
  const body = encodeURIComponent(
    `Hi ${recruiterName || 'Hiring Team'},\r\n\r\n` +
    `I noticed the ${jobTitle || 'ServiceNow Developer'} position at ${companyName || 'your organization'} and wanted to reach out directly.\r\n\r\n` +
    `With 3 years of hands-on experience in enterprise application workflows, scoped applications, platform policies, and integrations, I am confident I can contribute effectively to your development team.\r\n\r\n` +
    `I have attached my resume for your review and would welcome the opportunity to connect.\r\n\r\n` +
    `Best regards,\r\nRam`
  );
  return `mailto:${recruiterEmail || ''}?subject=${subject}&body=${body}`;
}


// ═══════════════════════════════════════════════════════════════
//  RENDER: JOB CARDS
// ═══════════════════════════════════════════════════════════════

function renderJobCards(jobs) {
  if (!jobs || jobs.length === 0) {
    dom.jobsGrid.innerHTML = '';
    if (dom.paginationContainer) dom.paginationContainer.hidden = true;
    return;
  }

  // Cross-reference with connections network dynamically
  matchConnectionsToJobs(jobs, state.connections);

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
        if (type === 'network') extraClass = ' badge-network';
        if (type === 'active-recruiter') extraClass = ' badge-active-recruiter';
        if (type === 'coverage') extraClass = ' badge-coverage';
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
                  if (analysis && (analysis.reason === 'semantic_match' || analysis.reason === 'concept_match' || analysis.semantic)) {
                    const scorePct = analysis.score ? Math.round(analysis.score * 100) : 100;
                    title = `Semantic vector match (${scorePct}% similarity with job description context)`;
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

      let networkSnippetHtml = '';
      if (job.network_connections && job.network_connections.length > 0) {
        const first = job.network_connections[0];
        const extraText = job.network_connections.length > 1 ? ` & ${job.network_connections.length - 1} other${job.network_connections.length - 1 > 1 ? 's' : ''}` : '';
        networkSnippetHtml = `
          <div style="font-size: 0.75rem; color: var(--accent-emerald); display: flex; align-items: center; gap: 0.35rem; margin-top: 0.25rem;">
            <span>👥</span>
            <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
              Referral: <strong>${esc(first.name)}</strong> (${esc(first.position)})${esc(extraText)}
            </span>
          </div>
        `;
      }

      // Career Portal Button
      const careerPortalHtml = job.career_site_url
        ? `<a class="btn-portal" href="${esc(job.career_site_url)}" target="_blank" rel="noopener">🌐 View on Portal</a>`
        : '';

      // Active Recruiters Snippet
      let recruitersSnippetHtml = '';
      if (job.active_recruiters && job.active_recruiters.length > 0) {
        recruitersSnippetHtml = `
          <div class="job-recruiters-section">
            <div class="recruiters-header">
              <span>📬 Active Hiring Team</span>
            </div>
            <div class="recruiters-list">
              ${job.active_recruiters.map(r => {
                const mailtoLink = buildRecruiterMailto(r.email, r.name, job.companyName || job.company, job.title);
                const views = r.profilesViewed ? ` · ${r.profilesViewed} views` : '';
                const status = r.status || 'Hiring Team Contact';
                const badgeClass = status.toLowerCase().includes('active') ? 'active-now' : 'contact';
                return `
                  <div class="recruiter-chip">
                    <div class="recruiter-meta">
                      <strong>${esc(r.name)}</strong>
                      <span class="recruiter-badge ${badgeClass}">
                        ${esc(status)}${esc(views)}
                      </span>
                    </div>
                    <a class="btn-email-recruiter" href="${mailtoLink}">✉️ Email</a>
                  </div>
                `;
              }).join('')}
            </div>
          </div>
        `;
      }

      return `
        <div class="job-card${targetClass}" style="animation-delay:${index * 0.05}s">
          <div class="card-header">
            ${hasScore ? buildScoreCircle(score) : ''}
            <div class="job-info">
              <h3 class="job-title">${esc(job.title || '')}</h3>
              <p class="job-company">${esc(job.companyName || job.company || '')}</p>
            </div>
          </div>
          <div class="job-meta">
            <p class="job-location">📍 ${esc(job.location || 'Location not specified')}</p>
            <p class="job-date">🕐 ${esc(job.postedAt || job.postedDate || 'Date not available')}</p>
            ${job.salary ? `<p class="job-salary">💰 ${esc(job.salary)}</p>` : ''}
          </div>
          <div class="job-badges">
            ${isTarget ? badgeHtml('🎯 Recruiter Network', 'target') : ''}
            ${(job.network_connections && job.network_connections.length > 0) ? badgeHtml(`👥 ${job.network_connections.length} Referral${job.network_connections.length > 1 ? 's' : ''}`, 'network') : ''}
            ${job.has_active_recruiters ? badgeHtml(`📬 ${job.active_recruiters.length} Active Recruiter${job.active_recruiters.length > 1 ? 's' : ''}`, 'active-recruiter') : ''}
            ${badgeHtml(job.employmentType || job.jobType, 'jobType')}
            ${badgeHtml(job.seniorityLevel || job.experienceLevel, 'experience')}
            ${job.applicantsCount ? badgeHtml(job.applicantsCount + ' Applicants', 'applicants') : ''}
            ${(job.total_requirements > 0) ? badgeHtml(`📊 ${job.requirements_met_score}% Coverage (${(job.jd_matched_skills || []).length}/${job.total_requirements})`, 'coverage') : ''}
          </div>
          ${skillsHtml}
          ${(job.missing_skills && job.missing_skills.length > 0) ? `
            <div class="job-skills" style="margin-top:0.25rem">
              ${job.missing_skills.slice(0, 4).map(s => `<span class="skill-gap">🔴 ${esc(s)}</span>`).join('')}
              ${job.missing_skills.length > 4 ? `<span style="font-size:0.7rem;color:var(--text-muted)"> +${job.missing_skills.length - 4} more gaps</span>` : ''}
            </div>
          ` : ''}
          ${networkSnippetHtml}
          <div class="card-actions">
            <button class="btn-details" onclick="showJobDetail(${globalIndex})">View Details</button>
            ${careerPortalHtml}
            ${
              (job.applyUrl || job.link)
                ? `<a class="btn-apply" href="${esc(job.applyUrl || job.link)}" target="_blank" rel="noopener">Apply →</a>`
                : ''
            }
          </div>
          ${recruitersSnippetHtml}
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
  dom.scrapeBtn.classList.add('btn-loading', 'scraping-active');

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
    dom.scrapeBtn.classList.remove('btn-loading', 'scraping-active');
    updateQuotas();
  }
}


// ═══════════════════════════════════════════════════════════════
//  RESUME UPLOAD
// ═══════════════════════════════════════════════════════════════

async function uploadResume(file) {
  if (!file) return;

  // Validate file type
  if (!file.name.toLowerCase().match(/\.(docx|pdf)$/)) {
    showToast('Please upload a .pdf or .docx file', 'error');
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
    if (dom.resumeRemoveBtn) dom.resumeRemoveBtn.style.display = 'inline-flex';

    showToast('Resume uploaded & parsed', 'success');

    // Auto-trigger matching if we already have jobs loaded
    if (state.jobs.length > 0) {
      matchJobs();
    }
  } catch (err) {
    resetResumeUI();
    showToast(err.message, 'error');
  }
}


function resetResumeUI() {
  dom.uploadZone.classList.remove('uploaded');
  dom.uploadZone.querySelector('.upload-label').textContent =
    'Drag & drop your resume or click to upload';
  dom.uploadZone.querySelector('.upload-hint').textContent =
    'Supports .pdf and .docx files up to 5 MB';
  if (dom.resumeRemoveBtn) dom.resumeRemoveBtn.style.display = 'none';
  if (dom.resumeFile) dom.resumeFile.value = '';
}


async function removeResume() {
  try {
    await apiFetch('/api/resume', { method: 'DELETE' });
    state.skills = [];
    state.coreSkills = [];
    state.resumeText = '';
    renderSkillTags();
    resetResumeUI();
    showToast('Resume attachment removed & cache cleared', 'info');

    // Re-render job cards without scores if present
    if (state.jobs && state.jobs.length > 0) {
      state.jobs.forEach((j) => {
        delete j.match_score;
        delete j.keyword_score;
        delete j.tfidf_score;
        delete j.matched_skills;
        delete j.partial_skills;
        delete j.skills_analysis;
      });
      renderJobCards(state.jobs);
    }
  } catch (err) {
    showToast('Failed to remove resume: ' + err.message, 'error');
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
        mode: 'semantic',
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
    updateQuotas();
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
    const params = new URLSearchParams();
    params.set('limit', '25');
    if (dom.keywords?.value?.trim()) params.set('keywords', dom.keywords.value.trim());
    if (dom.location?.value?.trim()) params.set('location', dom.location.value.trim());

    const jobsData = await apiFetch(`/api/jobs?${params.toString()}`);
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
    case 'recruiter':
      jobs.sort((a, b) => {
        const aTarget = a.is_partner_company === true ? 1 : 0;
        const bTarget = b.is_partner_company === true ? 1 : 0;
        
        // Sort target companies first
        if (bTarget !== aTarget) {
          return bTarget - aTarget;
        }
        
        // Secondary sort: by match score descending
        const aScore = a.match_score ?? 0;
        const bScore = b.match_score ?? 0;
        if (aScore !== bScore) {
          return bScore - aScore;
        }
        
        // Tertiary sort: alphabetically by company name
        return (a.companyName || a.company || '').localeCompare(b.companyName || b.company || '');
      });
      break;
  }

  renderJobCards(jobs);
}


// ═══════════════════════════════════════════════════════════════
//  JOB DETAIL MODAL
// ═══════════════════════════════════════════════════════════════

function showJobDetail(index) {
  try {
    const jobs = state.matchedJobs.length > 0 ? state.matchedJobs : state.jobs;
    const job = jobs[index];
    if (!job) return;

    // 1. Immediately show modal with subtle loading skeleton for instant tactile feedback
    dom.modal.hidden = false;
    document.body.style.overflow = 'hidden';
    dom.modalBody.innerHTML = `
      <div class="modal-loading-state">
        <div class="status-spinner" style="width: 28px; height: 28px; border-width: 3px;"></div>
        <p style="margin-top: 0.75rem; font-size: 0.85rem; color: var(--text-secondary);">Loading job details…</p>
      </div>
    `;

    const hasScore = job.match_score != null;
    const score = job.match_score ?? 0;

    // Score breakdown section
    let scoreHtml = '';
    if (hasScore) {
      const keywordScore = job.keyword_score ?? score;
      const tfidfScore = job.tfidf_score ?? score;
      const methodLabel = job.match_method || 'Semantic Similarity';

      scoreHtml = `
        <div class="modal-score-display">
          ${buildScoreCircle(score, 80, 5, 'modal-score-circle')}
          <div class="modal-score-label">
            <strong>Overall Match Score</strong>
            Based on keyword overlap and ${methodLabel.toLowerCase()}
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
              <span>${methodLabel}</span>
              <span>${Math.round(tfidfScore)}%</span>
            </div>
            <div class="score-bar-track">
              <div class="score-bar-fill" style="width:${tfidfScore}%"></div>
            </div>
          </div>
        </div>
      `;
    }

    // Skills section — FIXED: Safe semantic title without undefined .join()
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
                  // V2 Safe Semantic Match Rendering
                  if (analysis && (analysis.reason === 'semantic_match' || analysis.semantic)) {
                    const scorePct = analysis.score ? Math.round(analysis.score * 100) : 100;
                    const title = `Semantic vector match (${scorePct}% similarity with job description context)`;
                    return `<span class="skill-semantic${coreCls}" title="${esc(title)}">${star}${esc(s)}</span>`;
                  }
                  return `<span class="skill-matched${coreCls}" title="Fully matched">${star}${esc(s)}</span>`;
                } else if (partial.includes(s_low)) {
                  const missing = (analysis?.tokens || []).filter((t) => !t.matched).map((t) => t.token);
                  return `<span class="skill-partial${coreCls}" title="Missing: ${esc(missing.join(', '))}">
                    ${star}${esc(s)} <small style="opacity:0.75;font-size:0.65rem">(${esc(missing.join(', '))})</small>
                  </span>`;
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

    // Network referrals section
    let referralsHtml = '';
    if (job.network_connections && job.network_connections.length > 0) {
      const listHtml = job.network_connections
        .map(c => {
          const initials = (c.name || 'EM').split(' ').filter(Boolean).map(n => n[0]).join('').substring(0, 2).toUpperCase();
          return `
            <div class="referrals-card">
              <div class="referrals-card-avatar">${esc(initials)}</div>
              <div class="referrals-card-details">
                <div class="referrals-card-name">${esc(c.name)}</div>
                <div class="referrals-card-pos">${esc(c.position || 'Employee')} at ${esc(c.company)}</div>
              </div>
            </div>
          `;
        })
        .join('');

      const firstConn = job.network_connections[0];
      const emailBody = `Hi ${firstConn.name},\n\nI noticed a ${job.title} role open at ${firstConn.company} and saw that you work there.\n\nWould you be open to connecting or sharing an internal referral?\n\nJob Link: ${job.applyUrl || job.link || ''}\n\nBest regards,\nRam`;

      referralsHtml = `
        <div class="referrals-modal-section">
          <h4>👥 Network Referral Connections (${job.network_connections.length})</h4>
          <div class="referrals-list">${listHtml}</div>
          <div class="referral-outreach-container">
            <div style="font-size: 0.8rem; color: var(--text-secondary); font-weight: 500; text-align: left;">
              Outreach Template (for ${esc(firstConn.name)}):
            </div>
            <textarea class="referral-outreach-box" id="referral-outreach-text" readonly>${esc(emailBody)}</textarea>
            <button class="btn-copy-referral" onclick="copyReferralText()">📋 Copy Message Template</button>
          </div>
        </div>
      `;
    }

    // Active Recruiters Section (V2 Intelligence)
    let recruitersHtml = '';
    if (job.active_recruiters && job.active_recruiters.length > 0) {
      recruitersHtml = `
        <div class="job-recruiters-section" style="margin: 1rem 0;">
          <div class="recruiters-header">
            <span>📬 Active Hiring Team (${job.active_recruiters.length})</span>
          </div>
          <div class="recruiters-list">
            ${job.active_recruiters.map(r => {
              const mailto = buildRecruiterMailto(r.email, r.name, job.companyName || job.company, job.title);
              const views = r.profilesViewed ? ` · ${r.profilesViewed} views` : '';
              const status = r.status || 'Hiring Team Contact';
              const badgeClass = status.toLowerCase().includes('active') ? 'active-now' : 'contact';
              return `
                <div class="recruiter-chip">
                  <div class="recruiter-meta">
                    <strong>${esc(r.name)}</strong>
                    <span style="font-size:0.75rem; color:var(--text-secondary)">(${esc(r.email)})</span>
                    <span class="recruiter-badge ${badgeClass}">
                      ${esc(status)}${esc(views)}
                    </span>
                  </div>
                  <a href="${mailto}" class="btn-email-recruiter">✉️ Email Recruiter</a>
                </div>
              `;
            }).join('')}
          </div>
        </div>
      `;
    }

    // Career portal button
    const portalButtonHtml = job.career_site_url
      ? `<a class="btn-portal" href="${esc(job.career_site_url)}" target="_blank" rel="noopener" style="margin-right:0.5rem">🌐 View on Official Career Portal ↗</a>`
      : '';

    // Description text / html
    const descriptionContent = job.descriptionHtml || job.descriptionText || job.description || 'No description available.';
    const descriptionHtml = descriptionContent.includes('<')
      ? descriptionContent
      : `<p>${esc(descriptionContent)}</p>`;

    // Render complete payload into modal body
    dom.modalBody.innerHTML = `
      <h2 class="modal-job-title">${esc(job.title)}</h2>
      <p class="modal-company">${esc(job.companyName || job.company || '')}</p>
      <div class="modal-meta">
        <span>📍 ${esc(job.location || 'N/A')}</span>
        <span>🕐 ${esc(job.postedAt || job.postedDate || 'N/A')}</span>
        ${job.employmentType ? `<span>💼 ${esc(job.employmentType)}</span>` : ''}
        ${job.seniorityLevel ? `<span>📊 ${esc(job.seniorityLevel)}</span>` : ''}
        ${job.applicantsCount ? `<span>👥 ${esc(String(job.applicantsCount))} applicants</span>` : ''}
      </div>
      ${job.is_partner_company ? `
        <div style="margin: 0.5rem 0; display: inline-flex; align-items: center; gap: 0.5rem; background: rgba(6, 182, 212, 0.1); border: 1px solid rgba(6, 182, 212, 0.25); padding: 0.5rem 1rem; border-radius: var(--radius-md)">
          <span style="font-size:1.1rem">🎯</span>
          <span style="font-size:0.85rem; font-weight:600; color:var(--accent-cyan)">Recruiter Network Target Company</span>
          <a href="${esc(job.partner_company_info.url)}" target="_blank" rel="noopener" style="font-size:0.8rem; color:#fff; background:var(--accent-cyan); border-radius:var(--radius-sm); padding:0.25rem 0.6rem; text-decoration:none; margin-left:0.5rem">Careers Link ↗</a>
        </div>
      ` : ''}
      ${portalButtonHtml ? `<div style="margin: 0.5rem 0 1rem;">${portalButtonHtml}</div>` : ''}
      ${scoreHtml}
      ${skillsHtml}
      ${referralsHtml}
      ${recruitersHtml}
      ${(job.total_requirements > 0) ? `
        <div style="margin: 1.5rem 0;">
          <h4 style="font-size:0.85rem; font-weight:600; color:var(--text-secondary); text-transform:uppercase; letter-spacing:0.04em; margin-bottom:0.75rem;">📊 JD Requirements Coverage — ${job.requirements_met_score}% (${(job.jd_matched_skills || []).length}/${job.total_requirements})</h4>
          <div class="jd-requirements-grid">
            <div>
              <p style="font-size:0.75rem; font-weight:600; color:var(--accent-emerald); margin-bottom:0.4rem;">🟢 Verified on Resume (${(job.jd_matched_skills || []).length})</p>
              <div class="modal-skills-list">
                ${(job.jd_matched_skills || []).map(s => `<span class="skill-matched">${esc(s)}</span>`).join('')}
              </div>
            </div>
            <div>
              <p style="font-size:0.75rem; font-weight:600; color:#f43f5e; margin-bottom:0.4rem;">🔴 Missing Gaps — Required by Employer (${(job.missing_skills || []).length})</p>
              <div class="modal-skills-list">
                ${(job.missing_skills || []).map(s => `<span class="skill-gap">${esc(s)}</span>`).join('')}
              </div>
            </div>
          </div>
        </div>
        ${(job.missing_skills && job.missing_skills.length > 0) ? `
          <button class="btn-tailor-primary" id="btn-trigger-tailor" onclick="generateTailoredKit('${esc((job.title || '').replace(/'/g, ''))}', '${esc((job.companyName || job.company || '').replace(/'/g, ''))}', ${JSON.stringify(job.missing_skills || []).replace(/'/g, '&apos;')})">
            ✨ Tailor Resume & Prep Interview for this Role
          </button>
          <div id="tailor-result-container" hidden style="margin-top:1rem;"></div>
        ` : ''}
      ` : ''}
      <div class="modal-description">${descriptionHtml}</div>
      ${(job.applyUrl || job.link)
        ? `<a class="modal-apply-btn" href="${esc(job.applyUrl || job.link)}" target="_blank" rel="noopener">Apply for this position →</a>`
        : ''}
    `;

  } catch (err) {
    console.error("Error opening job details modal:", err);
    showToast("Failed to open job details: " + err.message, "error");
    closeModal();
  }
}

// Expose to inline onclick handlers
window.showJobDetail = showJobDetail;

function closeModal() {
  dom.modal.hidden = true;
  document.body.style.overflow = '';
}

// ═══════════════════════════════════════════════════════════════
//  V3: DYNAMIC RESUME TAILORING & INTERVIEW PREP
// ═══════════════════════════════════════════════════════════════

async function generateTailoredKit(jobTitle, company, missingSkills) {
  const container = document.getElementById('tailor-result-container');
  const btn = document.getElementById('btn-trigger-tailor');
  if (!container || !btn) return;

  btn.disabled = true;
  btn.innerHTML = `<span class="status-spinner" style="width:16px;height:16px;border-width:2px"></span> Analyzing JD & Generating Kit…`;
  container.hidden = false;
  container.innerHTML = `<p style="font-size:0.85rem; color:var(--text-secondary)">Synthesizing tailored experience bullets & interview questions…</p>`;

  try {
    const res = await apiFetch('/api/tailor', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job_title: jobTitle,
        company: company,
        missing_skills: missingSkills,
        current_skills: state.skills
      })
    });

    const summaryEscaped = esc(res.tailored_summary || '');
    const bulletsHtml = (res.tailored_bullets || []).map(b => {
      const bulletEscaped = esc(b);
      return `
        <li style="margin-bottom:0.5rem">
          ${bulletEscaped}
          <button class="btn-copy-sm" style="margin-left:0.5rem" onclick="navigator.clipboard.writeText(this.previousSibling.textContent.trim()); showToast('Bullet copied!', 'success')">📋</button>
        </li>
      `;
    }).join('');

    const questionsHtml = (res.interview_questions || []).map(q => `
      <div class="interview-qa-block">
        <strong style="color:var(--text-primary); font-size:0.85rem">Q: ${esc(q.question || '')}</strong>
        <p style="color:var(--text-secondary); font-size:0.8rem; margin-top:0.25rem">💡 <em>Suggested Talking Point:</em> ${esc(q.key_talking_point || '')}</p>
      </div>
    `).join('');

    container.innerHTML = `
      <div class="tailor-card">
        <h4 style="color:var(--accent-cyan); margin-bottom:0.4rem;">🎯 Targeted Professional Summary</h4>
        <div class="tailor-text">${summaryEscaped}</div>
        <button class="btn-copy-sm" style="margin-top:0.5rem" onclick="navigator.clipboard.writeText(document.querySelector('.tailor-text').textContent); showToast('Summary copied!', 'success')">📋 Copy Summary</button>

        <h4 style="color:var(--accent-cyan); margin: 1rem 0 0.4rem;">💼 Recommended Experience Bullets (Bridging Gaps)</h4>
        <ul style="padding-left:1.2rem; font-size:0.85rem; line-height:1.5;">
          ${bulletsHtml}
        </ul>

        <h4 style="color:var(--accent-cyan); margin: 1rem 0 0.4rem;">🎙️ Top Interview Questions to Expect</h4>
        <div style="display:flex; flex-direction:column; gap:0.5rem;">
          ${questionsHtml}
        </div>
      </div>
    `;
  } catch (err) {
    container.innerHTML = `<p style="color:var(--accent-rose); font-size:0.85rem">Failed to generate tailor kit: ${esc(err.message)}</p>`;
  } finally {
    btn.disabled = false;
    btn.innerHTML = `✨ Regenerate Tailored Kit`;
  }
}
window.generateTailoredKit = generateTailoredKit;


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
    const params = new URLSearchParams();
    params.set('limit', '25');
    if (dom.keywords?.value?.trim()) params.set('keywords', dom.keywords.value.trim());
    if (dom.location?.value?.trim()) params.set('location', dom.location.value.trim());

    const jobsData = await apiFetch(`/api/jobs?${params.toString()}`);
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
      dom.uploadZone.classList.add('uploaded');
      dom.uploadZone.querySelector('.upload-label').textContent = '✅ Resume loaded from cache';
      dom.uploadZone.querySelector('.upload-hint').textContent = `${skillsData.skills.length} skills loaded`;
      if (dom.resumeRemoveBtn) dom.resumeRemoveBtn.style.display = 'inline-flex';
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
  dom.uploadZone.addEventListener('click', (e) => {
    // Don't trigger file chooser if clicking the remove button
    if (e.target.closest('#resume-remove-btn')) return;
    dom.resumeFile.click();
  });

  if (dom.resumeRemoveBtn) {
    dom.resumeRemoveBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      removeResume();
    });
  }

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

  // ── Connections File Upload ──
  if (dom.connectionsUploadZone) {
    dom.connectionsUploadZone.addEventListener('click', () => dom.connectionsFile.click());
  }
  if (dom.connectionsFile) {
    dom.connectionsFile.addEventListener('change', (e) => {
      if (e.target.files[0]) handleConnectionsUpload(e.target.files[0]);
    });
  }
  
  // Drag & Drop for Connections
  if (dom.connectionsUploadZone) {
    dom.connectionsUploadZone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dom.connectionsUploadZone.classList.add('drag-over');
    });
    dom.connectionsUploadZone.addEventListener('dragleave', () => {
      dom.connectionsUploadZone.classList.remove('drag-over');
    });
    dom.connectionsUploadZone.addEventListener('drop', (e) => {
      e.preventDefault();
      dom.connectionsUploadZone.classList.remove('drag-over');
      const file = e.dataTransfer.files[0];
      if (file) handleConnectionsUpload(file);
    });
  }

  // Clear Connections
  if (dom.clearConnectionsBtn) {
    dom.clearConnectionsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      clearConnections();
    });
  }

  // Load stored connections
  loadConnectionsState();

  // ── Load cached data ──
  loadCachedData();

  // ── Live Quota Tracking ──
  updateQuotas();
  setInterval(updateQuotas, 60000);
});

async function updateQuotas() {
  try {
    const res = await fetch('/api/limits');
    if (!res.ok) return;
    const data = await res.json();

    // Update Hugging Face live status
    const hfText = document.getElementById('hf-status-text');
    if (hfText && data.huggingface) {
      if (data.huggingface.connected) {
        const remaining = data.huggingface.remaining_requests;
        const limit = data.huggingface.limit_requests;
        hfText.textContent = `HF: ${remaining.toLocaleString()} / ${limit.toLocaleString()} reqs left today`;
      } else {
        hfText.textContent = 'HF: Offline / Token Missing';
      }
    }

    // Update Apify live status
    const apifyText = document.getElementById('apify-quota-text');
    const apifyPill = document.getElementById('apify-quota-pill');
    if (apifyText && data.apify) {
      if (data.apify.connected) {
        const remUsd = typeof data.apify.remaining_usd === 'number' ? data.apify.remaining_usd.toFixed(2) : data.apify.remaining_usd;
        const totalUsd = typeof data.apify.total_usd === 'number' ? data.apify.total_usd.toFixed(2) : data.apify.total_usd;
        const usedUsd = typeof data.apify.used_usd === 'number' ? data.apify.used_usd.toFixed(2) : '0.00';
        apifyText.textContent = `Apify: $${remUsd} left (~${data.apify.scrapes_left} searches)`;
        if (apifyPill) {
          apifyPill.title = `Monthly Usage: $${usedUsd} / $${totalUsd} (${data.apify.percent_used}% used) • ~${data.apify.scrapes_left} searches remaining`;
        }
      } else {
        apifyText.textContent = 'Apify: Offline / Limit Reached';
      }
    }
  } catch (e) {
    console.debug('Quota fetch skipped:', e);
  }
}

// ═══════════════════════════════════════════════════════════════
//  CONNECTIONS MANAGEMENT FUNCTIONS
// ═══════════════════════════════════════════════════════════════

function loadConnectionsState() {
  try {
    const stored = localStorage.getItem('network_connections');
    if (stored) {
      state.connections = JSON.parse(stored);
      updateConnectionsUI();
    }
  } catch (err) {
    console.error('Failed to load connections state:', err);
  }
}

function updateConnectionsUI() {
  if (!dom.connectionsCountText) return;
  
  const count = state.connections.length;
  if (count > 0) {
    dom.connectionsCountText.innerHTML = `👥 <strong>${count}</strong> connections loaded`;
    if (dom.clearConnectionsBtn) dom.clearConnectionsBtn.style.display = 'inline-flex';
    if (dom.connectionsUploadZone) {
      dom.connectionsUploadZone.classList.add('uploaded');
      dom.connectionsUploadZone.querySelector('.upload-label').textContent = 'Connections list active';
    }
  } else {
    dom.connectionsCountText.textContent = 'No connections loaded';
    if (dom.clearConnectionsBtn) dom.clearConnectionsBtn.style.display = 'none';
    if (dom.connectionsUploadZone) {
      dom.connectionsUploadZone.classList.remove('uploaded');
      dom.connectionsUploadZone.querySelector('.upload-label').textContent = 'Drag & drop Connections.csv or click to upload';
    }
  }
}

async function handleConnectionsUpload(file) {
  if (!file) return;
  
  if (!file.name.toLowerCase().endsWith('.csv')) {
    showToast('Please upload a .csv file', 'error');
    return;
  }
  
  const reader = new FileReader();
  reader.onload = function(e) {
    const text = e.target.result;
    try {
      const parsed = parseConnectionsCSV(text);
      if (parsed.length === 0) {
        showToast('No valid connections found in the file.', 'error');
        return;
      }
      
      state.connections = parsed;
      localStorage.setItem('network_connections', JSON.stringify(parsed));
      updateConnectionsUI();
      showToast(`Loaded ${parsed.length} connections successfully!`, 'success');
      
      // Auto-refresh the current job list to show referral badges immediately!
      const activeJobs = state.matchedJobs.length > 0 ? state.matchedJobs : state.jobs;
      if (activeJobs.length > 0) {
        renderJobCards(activeJobs);
      }
    } catch (err) {
      showToast(err.message, 'error');
    }
  };
  
  reader.onerror = function() {
    showToast('Failed to read CSV file.', 'error');
  };
  
  reader.readAsText(file);
}

function clearConnections() {
  state.connections = [];
  localStorage.removeItem('network_connections');
  updateConnectionsUI();
  showToast('Connections network cleared.', 'info');
  
  // Refresh job list
  const activeJobs = state.matchedJobs.length > 0 ? state.matchedJobs : state.jobs;
  if (activeJobs.length > 0) {
    renderJobCards(activeJobs);
  }
}
window.clearConnections = clearConnections;

function copyReferralText() {
  const textBox = document.getElementById('referral-outreach-text');
  if (!textBox) return;
  
  textBox.select();
  textBox.setSelectionRange(0, 99999); // For mobile devices
  
  navigator.clipboard.writeText(textBox.value)
    .then(() => {
      showToast('Referral request template copied!', 'success');
    })
    .catch(() => {
      showToast('Failed to copy to clipboard', 'error');
    });
}
window.copyReferralText = copyReferralText;

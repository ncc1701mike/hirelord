"""
Hire Lord — Database Schema v2
================================
Full job description + requirements stored as first-class fields.
Complete audit trail. Full automation pipeline support.
"""

CREATE_TABLES = """
-- ── Jobs ──────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jobs (
    id                      TEXT PRIMARY KEY,
    title                   TEXT NOT NULL,
    company                 TEXT NOT NULL,
    location                TEXT DEFAULT '',
    url                     TEXT DEFAULT '',
    source                  TEXT DEFAULT 'linkedin',

    -- Full job content (critical for interview prep)
    description_full        TEXT DEFAULT '',   -- Complete original JD, verbatim
    description_summary     TEXT DEFAULT '',   -- AI-generated 3-5 sentence summary
    requirements_raw        TEXT DEFAULT '',   -- Raw requirements section, verbatim
    requirements_parsed     TEXT DEFAULT '[]', -- JSON array of parsed requirement strings
    nice_to_have            TEXT DEFAULT '[]', -- JSON array of "nice to have" items
    responsibilities        TEXT DEFAULT '[]', -- JSON array of responsibilities
    tech_stack              TEXT DEFAULT '[]', -- JSON array of technologies mentioned
    seniority_level         TEXT DEFAULT '',   -- entry | mid | senior | staff | principal | lead
    employment_type         TEXT DEFAULT '',   -- full_time | part_time | contract | freelance
    salary_range_text       TEXT DEFAULT '',   -- Raw salary text from JD if present
    salary_low              INTEGER,
    salary_high             INTEGER,
    remote_type             TEXT DEFAULT '',   -- remote | hybrid | onsite

    -- Company info
    company_website         TEXT DEFAULT '',
    company_linkedin        TEXT DEFAULT '',
    company_size            TEXT DEFAULT '',
    company_industry        TEXT DEFAULT '',
    company_context         TEXT DEFAULT '',   -- AI-researched company summary
    hiring_manager          TEXT DEFAULT '',
    recruiter_name          TEXT DEFAULT '',
    recruiter_linkedin      TEXT DEFAULT '',

    -- Screening results
    match_score             INTEGER DEFAULT 0,
    match_tier              TEXT DEFAULT 'unscreened',
    matching_skills         TEXT DEFAULT '[]',
    missing_skills          TEXT DEFAULT '[]',
    keyword_matches         TEXT DEFAULT '[]', -- exact JD keywords found in resume
    recommendation          TEXT DEFAULT '',
    priority                INTEGER DEFAULT 3,

    -- Pipeline status
    -- new → screened → tailoring → ready → applied → interviewing → offered → rejected | withdrawn
    status                  TEXT DEFAULT 'new',

    -- Timestamps
    discovered_at           TEXT DEFAULT (datetime('now')),
    posted_at               TEXT DEFAULT '',   -- when job was originally posted
    expires_at              TEXT DEFAULT '',   -- application deadline if known
    status_updated_at       TEXT DEFAULT (datetime('now')),
    notes                   TEXT DEFAULT '',

    UNIQUE(url) ON CONFLICT IGNORE
);

-- ── Applications ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS applications (
    id                      TEXT PRIMARY KEY,
    job_id                  TEXT NOT NULL REFERENCES jobs(id),

    -- Documents
    resume_path             TEXT DEFAULT '',
    cover_letter_path       TEXT DEFAULT '',
    resume_text             TEXT DEFAULT '',
    cover_letter_text       TEXT DEFAULT '',
    tailoring_notes         TEXT DEFAULT '',   -- AI notes on tailoring decisions

    -- Submission
    application_url         TEXT DEFAULT '',
    applied_via             TEXT DEFAULT 'linkedin',
    ats_system              TEXT DEFAULT '',   -- Workday | Greenhouse | Lever | etc.
    confirmation_number     TEXT DEFAULT '',
    confirmation_received   INTEGER DEFAULT 0,

    -- Contacts
    contact_name            TEXT DEFAULT '',
    contact_email           TEXT DEFAULT '',
    recruiter_name          TEXT DEFAULT '',
    recruiter_email         TEXT DEFAULT '',
    recruiter_linkedin      TEXT DEFAULT '',
    hiring_manager_name     TEXT DEFAULT '',

    -- Pipeline status
    -- submitted → viewed → phone_screen → interview_scheduled →
    -- interviewing → final_round → offer → rejected | withdrawn
    status                  TEXT DEFAULT 'submitted',

    -- Offer details
    offer_amount            INTEGER,
    offer_equity            TEXT DEFAULT '',
    offer_bonus             TEXT DEFAULT '',
    offer_benefits          TEXT DEFAULT '',
    offer_start_date        TEXT DEFAULT '',
    offer_deadline          TEXT DEFAULT '',
    offer_notes             TEXT DEFAULT '',

    -- Follow-up tracking
    last_followup_at        TEXT,
    next_followup_at        TEXT,
    followup_count          INTEGER DEFAULT 0,

    -- Timestamps
    applied_at              TEXT DEFAULT (datetime('now')),
    status_updated_at       TEXT DEFAULT (datetime('now')),
    last_activity_at        TEXT DEFAULT (datetime('now')),
    notes                   TEXT DEFAULT ''
);

-- ── Interview Prep ────────────────────────────────────────────────────────────
-- AI-generated prep material keyed to the specific JD + candidate background
CREATE TABLE IF NOT EXISTS interview_prep (
    id                      TEXT PRIMARY KEY,
    job_id                  TEXT NOT NULL REFERENCES jobs(id),
    application_id          TEXT REFERENCES applications(id),
    generated_at            TEXT DEFAULT (datetime('now')),

    -- AI-generated prep content
    company_research        TEXT DEFAULT '',   -- Company background, recent news, culture
    role_analysis           TEXT DEFAULT '',   -- Deep analysis of role vs candidate background
    likely_questions        TEXT DEFAULT '[]', -- JSON array of predicted interview questions
    suggested_answers       TEXT DEFAULT '[]', -- JSON array of STAR-format suggested answers
    questions_to_ask        TEXT DEFAULT '[]', -- JSON array of questions Mike should ask
    technical_prep          TEXT DEFAULT '',   -- Technical topics to review based on JD
    red_flags               TEXT DEFAULT '',   -- Potential concerns to address proactively
    talking_points          TEXT DEFAULT '[]', -- Key narrative points to emphasize
    salary_strategy         TEXT DEFAULT '',   -- Negotiation strategy based on market data

    notes                   TEXT DEFAULT ''
);

-- ── Status History ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS status_history (
    id              TEXT PRIMARY KEY,
    entity_type     TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT NOT NULL,
    changed_at      TEXT DEFAULT (datetime('now')),
    changed_by      TEXT DEFAULT 'system',
    note            TEXT DEFAULT ''
);

-- ── Follow-ups ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS followups (
    id                  TEXT PRIMARY KEY,
    application_id      TEXT NOT NULL REFERENCES applications(id),
    followup_type       TEXT NOT NULL,
    message_subject     TEXT DEFAULT '',
    message_text        TEXT NOT NULL,
    sent_to             TEXT DEFAULT '',
    sent_at             TEXT DEFAULT (datetime('now')),
    scheduled_for       TEXT,
    status              TEXT DEFAULT 'sent',
    response_received   INTEGER DEFAULT 0,
    response_at         TEXT,
    response_text       TEXT DEFAULT '',
    notes               TEXT DEFAULT ''
);

-- ── Interviews ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS interviews (
    id                  TEXT PRIMARY KEY,
    application_id      TEXT NOT NULL REFERENCES applications(id),
    round_number        INTEGER DEFAULT 1,
    interview_type      TEXT DEFAULT 'phone',
    scheduled_at        TEXT,
    duration_minutes    INTEGER DEFAULT 60,
    timezone            TEXT DEFAULT 'America/Denver',
    interviewers        TEXT DEFAULT '[]',
    platform            TEXT DEFAULT '',
    meeting_link        TEXT DEFAULT '',
    status              TEXT DEFAULT 'scheduled',
    outcome             TEXT DEFAULT '',
    feedback_received   TEXT DEFAULT '',
    thank_you_sent      INTEGER DEFAULT 0,
    thank_you_sent_at   TEXT,
    notes               TEXT DEFAULT ''
);

-- ── Indexes ───────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_jobs_status      ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_tier        ON jobs(match_tier);
CREATE INDEX IF NOT EXISTS idx_jobs_priority    ON jobs(priority, match_score);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered  ON jobs(discovered_at);
CREATE INDEX IF NOT EXISTS idx_apps_status      ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_followup    ON applications(next_followup_at);
CREATE INDEX IF NOT EXISTS idx_apps_activity    ON applications(last_activity_at);
CREATE INDEX IF NOT EXISTS idx_history_entity   ON status_history(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_history_changed  ON status_history(changed_at);
CREATE INDEX IF NOT EXISTS idx_followups_app    ON followups(application_id);
CREATE INDEX IF NOT EXISTS idx_interviews_app   ON interviews(application_id);
CREATE INDEX IF NOT EXISTS idx_prep_job         ON interview_prep(job_id);
"""

# ── Status definitions ────────────────────────────────────────────────────────

JOB_STATUSES = {
    "new":          ("NEW",          "white",        "Discovered, not yet screened"),
    "screened":     ("SCREENED",     "cyan",         "AI screened, awaiting tailoring"),
    "tailoring":    ("TAILORING",    "yellow",       "Documents being tailored"),
    "ready":        ("READY",        "green",        "Documents ready to submit"),
    "applied":      ("APPLIED",      "blue",         "Application submitted"),
    "interviewing": ("INTERVIEWING", "magenta",      "In interview process"),
    "offered":      ("OFFERED",      "bold green",   "Offer received"),
    "rejected":     ("REJECTED",     "red",          "Application rejected"),
    "withdrawn":    ("WITHDRAWN",    "dim",          "Application withdrawn"),
    "skip":         ("SKIP",         "dim",          "Skipped - poor match"),
}

APPLICATION_STATUSES = {
    "submitted":            ("SUBMITTED",           "blue",         "Application submitted"),
    "viewed":               ("VIEWED",              "cyan",         "Viewed by employer"),
    "phone_screen":         ("PHONE SCREEN",        "yellow",       "Phone screen scheduled"),
    "interview_scheduled":  ("INTERVIEW SCHEDULED", "yellow",       "Interview scheduled"),
    "interviewing":         ("INTERVIEWING",        "magenta",      "Active interview process"),
    "final_round":          ("FINAL ROUND",         "bold yellow",  "In final round"),
    "offer":                ("OFFER",               "bold green",   "Offer received"),
    "rejected":             ("REJECTED",            "red",          "Rejected"),
    "withdrawn":            ("WITHDRAWN",           "dim",          "Withdrawn"),
}

# ── Automation pipeline stages ────────────────────────────────────────────────
# Maps each status to what the automation agent does next

AUTOMATION_PIPELINE = {
    "new":          "screen_job",
    "screened":     "tailor_resume",       # if tier is strong/good
    "tailoring":    "human_review",        # HITL gate
    "ready":        "submit_application",  # automated via LinkedIn/ATS
    "applied":      "schedule_followup",   # auto-schedule day 7 followup
    "interviewing": "generate_prep",       # auto-generate interview prep
    "offered":      "human_review",        # HITL gate — offer decision
}

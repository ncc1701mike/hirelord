"""
Hire Lord — Database Store v2
==============================
Full CRUD with audit trail. Stores complete JD + requirements.
"""

import json
import uuid
import aiosqlite
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .models import CREATE_TABLES

DB_PATH = Path(__file__).parent.parent.parent / "hirelord.db"


from contextlib import asynccontextmanager

@asynccontextmanager
async def get_db():
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.executescript(CREATE_TABLES)
    await db.commit()
    try:
        yield db
    finally:
        await db.close()


# ── Audit trail ───────────────────────────────────────────────────────────────

async def _record_status_change(
    db, entity_type, entity_id, from_status, to_status,
    changed_by="system", note=""
):
    await db.execute(
        """INSERT INTO status_history
           (id, entity_type, entity_id, from_status, to_status, changed_by, note)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), entity_type, entity_id,
         from_status, to_status, changed_by, note),
    )


# ── Jobs ──────────────────────────────────────────────────────────────────────

async def upsert_job(
    title: str,
    company: str,
    description_full: str,
    url: str = "",
    location: str = "",
    source: str = "linkedin",
    job_id: Optional[str] = None,
    # Optional enriched fields
    requirements_raw: str = "",
    requirements_parsed: list = None,
    nice_to_have: list = None,
    responsibilities: list = None,
    tech_stack: list = None,
    seniority_level: str = "",
    employment_type: str = "full_time",
    salary_range_text: str = "",
    salary_low: Optional[int] = None,
    salary_high: Optional[int] = None,
    remote_type: str = "",
    company_website: str = "",
    company_linkedin: str = "",
    company_size: str = "",
    company_industry: str = "",
    posted_at: str = "",
    expires_at: str = "",
) -> str:
    async with get_db() as db:
        if url:
            cursor = await db.execute(
                "SELECT id FROM jobs WHERE url = ?", (url,)
            )
            row = await cursor.fetchone()
            if row:
                return row["id"]
        if job_id is None:
            job_id = str(uuid.uuid4())

        await db.execute(
            """INSERT OR IGNORE INTO jobs (
                id, title, company, location, url, source,
                description_full, requirements_raw,
                requirements_parsed, nice_to_have, responsibilities, tech_stack,
                seniority_level, employment_type, salary_range_text,
                salary_low, salary_high, remote_type,
                company_website, company_linkedin, company_size, company_industry,
                posted_at, expires_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?
            )""",
            (
                job_id, title, company, location, url, source,
                description_full, requirements_raw,
                json.dumps(requirements_parsed or []),
                json.dumps(nice_to_have or []),
                json.dumps(responsibilities or []),
                json.dumps(tech_stack or []),
                seniority_level, employment_type, salary_range_text,
                salary_low, salary_high, remote_type,
                company_website, company_linkedin, company_size, company_industry,
                posted_at, expires_at,
            ),
        )
        await _record_status_change(
            db, "job", job_id, None, "new", "agent",
            f"Discovered via {source}: {title} @ {company}"
        )
        await db.commit()
    return job_id


async def update_job_screening(
    job_id: str,
    match_score: int,
    match_tier: str,
    matching_skills: list,
    missing_skills: list,
    recommendation: str,
    priority: int,
    keyword_matches: list = None,
    description_summary: str = "",
) -> None:
    async with get_db() as db:
        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        old_status = row["status"] if row else "new"
        await db.execute(
            """UPDATE jobs SET
               match_score = ?, match_tier = ?,
               matching_skills = ?, missing_skills = ?,
               keyword_matches = ?,
               recommendation = ?, priority = ?,
               description_summary = ?,
               status = 'screened',
               status_updated_at = datetime('now')
               WHERE id = ?""",
            (
                match_score, match_tier,
                json.dumps(matching_skills), json.dumps(missing_skills),
                json.dumps(keyword_matches or []),
                recommendation, priority,
                description_summary,
                job_id,
            ),
        )
        new_status = "screened" if match_tier not in ("weak", "skip") else "skip"
        await _record_status_change(
            db, "job", job_id, old_status, new_status, "agent",
            f"Score: {match_score}/100 | Tier: {match_tier} | Priority: {priority}"
        )
        await db.commit()


async def update_job_company_context(job_id: str, context: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE jobs SET company_context = ? WHERE id = ?",
            (context, job_id)
        )
        await db.commit()


async def update_job_status(
    job_id: str, status: str, notes: str = "", changed_by: str = "system"
) -> None:
    async with get_db() as db:
        cursor = await db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        old_status = row["status"] if row else None
        await db.execute(
            """UPDATE jobs SET status = ?, notes = ?,
               status_updated_at = datetime('now') WHERE id = ?""",
            (status, notes, job_id),
        )
        await _record_status_change(
            db, "job", job_id, old_status, status, changed_by, notes
        )
        await db.commit()


async def get_job(job_id: str) -> Optional[dict]:
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_jobs_by_status(status: str) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY priority, match_score DESC",
            (status,),
        )
        return [dict(r) for r in await cursor.fetchall()]


async def get_strong_matches() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM jobs
               WHERE match_tier IN ('strong','good') AND status = 'screened'
               ORDER BY priority, match_score DESC""",
        )
        return [dict(r) for r in await cursor.fetchall()]


# ── Applications ──────────────────────────────────────────────────────────────

async def create_application(
    job_id: str,
    resume_text: str,
    cover_letter_text: str,
    tailoring_notes: str = "",
    resume_path: str = "",
    cover_letter_path: str = "",
    application_url: str = "",
    applied_via: str = "linkedin",
    ats_system: str = "",
) -> str:
    app_id = str(uuid.uuid4())
    next_followup = (datetime.now() + timedelta(days=7)).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO applications (
                id, job_id, resume_text, cover_letter_text, tailoring_notes,
                resume_path, cover_letter_path, application_url,
                applied_via, ats_system, next_followup_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                app_id, job_id, resume_text, cover_letter_text, tailoring_notes,
                resume_path, cover_letter_path, application_url,
                applied_via, ats_system, next_followup,
            ),
        )
        await _record_status_change(
            db, "application", app_id, None, "submitted", "human",
            f"Applied via {applied_via} | ATS: {ats_system or 'unknown'}"
        )
        await db.execute(
            "UPDATE jobs SET status='applied', status_updated_at=datetime('now') WHERE id=?",
            (job_id,)
        )
        await _record_status_change(
            db, "job", job_id, "ready", "applied", "human",
            f"Application {app_id} submitted"
        )
        await db.commit()
    return app_id


async def update_application_status(
    app_id: str, status: str, notes: str = "", changed_by: str = "human"
) -> None:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT status, job_id FROM applications WHERE id = ?", (app_id,)
        )
        row = await cursor.fetchone()
        old_status = row["status"] if row else None
        job_id = row["job_id"] if row else None

        await db.execute(
            """UPDATE applications SET status=?, notes=?,
               status_updated_at=datetime('now'),
               last_activity_at=datetime('now') WHERE id=?""",
            (status, notes, app_id),
        )
        await _record_status_change(
            db, "application", app_id, old_status, status, changed_by, notes
        )

        # Mirror to job status
        mirror_map = {
            "offer": "offered", "rejected": "rejected",
            "withdrawn": "withdrawn",
            "phone_screen": "interviewing",
            "interview_scheduled": "interviewing",
            "interviewing": "interviewing",
            "final_round": "interviewing",
        }
        if job_id and status in mirror_map:
            await db.execute(
                "UPDATE jobs SET status=?, status_updated_at=datetime('now') WHERE id=?",
                (mirror_map[status], job_id)
            )
        await db.commit()


async def get_applications_due_followup() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT a.*, j.title, j.company, j.location,
                      j.description_full, j.requirements_raw
               FROM applications a JOIN jobs j ON a.job_id = j.id
               WHERE a.next_followup_at <= datetime('now')
               AND a.status NOT IN ('offer','rejected','withdrawn')
               AND a.followup_count < 3
               ORDER BY a.next_followup_at""",
        )
        return [dict(r) for r in await cursor.fetchall()]


async def record_followup(
    application_id: str,
    followup_type: str,
    message_text: str,
    message_subject: str = "",
    sent_to: str = "",
) -> str:
    followup_id = str(uuid.uuid4())
    next_followup = (datetime.now() + timedelta(days=7)).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO followups
               (id, application_id, followup_type, message_subject,
                message_text, sent_to, status)
               VALUES (?, ?, ?, ?, ?, ?, 'sent')""",
            (followup_id, application_id, followup_type,
             message_subject, message_text, sent_to),
        )
        await db.execute(
            """UPDATE applications SET
               last_followup_at = datetime('now'),
               next_followup_at = ?,
               followup_count = followup_count + 1,
               last_activity_at = datetime('now')
               WHERE id = ?""",
            (next_followup, application_id),
        )
        await db.commit()
    return followup_id


# ── Interview prep ────────────────────────────────────────────────────────────

async def save_interview_prep(
    job_id: str,
    application_id: Optional[str],
    company_research: str = "",
    role_analysis: str = "",
    likely_questions: list = None,
    suggested_answers: list = None,
    questions_to_ask: list = None,
    technical_prep: str = "",
    red_flags: str = "",
    talking_points: list = None,
    salary_strategy: str = "",
) -> str:
    prep_id = str(uuid.uuid4())
    async with get_db() as db:
        await db.execute(
            """INSERT OR REPLACE INTO interview_prep (
                id, job_id, application_id,
                company_research, role_analysis,
                likely_questions, suggested_answers, questions_to_ask,
                technical_prep, red_flags, talking_points, salary_strategy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                prep_id, job_id, application_id,
                company_research, role_analysis,
                json.dumps(likely_questions or []),
                json.dumps(suggested_answers or []),
                json.dumps(questions_to_ask or []),
                technical_prep, red_flags,
                json.dumps(talking_points or []),
                salary_strategy,
            ),
        )
        await db.commit()
    return prep_id


async def get_interview_prep(job_id: str) -> Optional[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM interview_prep WHERE job_id = ? ORDER BY generated_at DESC LIMIT 1",
            (job_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


# ── Pipeline analytics ────────────────────────────────────────────────────────

async def get_pipeline_summary() -> dict:
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT status, COUNT(*) as count FROM jobs GROUP BY status"
        )
        job_counts = {r["status"]: r["count"] for r in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT status, COUNT(*) as count FROM applications GROUP BY status"
        )
        app_counts = {r["status"]: r["count"] for r in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT COUNT(*) as count FROM applications WHERE next_followup_at <= datetime('now') "
            "AND status NOT IN ('offer','rejected','withdrawn')"
        )
        followups_due = (await cursor.fetchone())["count"]

        cursor = await db.execute(
            "SELECT COUNT(*) as count FROM interviews WHERE status='scheduled'"
        )
        interviews_upcoming = (await cursor.fetchone())["count"]

        cursor = await db.execute(
            "SELECT AVG(match_score) as avg FROM jobs WHERE match_score > 0"
        )
        row = await cursor.fetchone()
        avg_score = round(row["avg"] or 0, 1)

    return {
        "jobs": job_counts,
        "applications": app_counts,
        "followups_due": followups_due,
        "interviews_upcoming": interviews_upcoming,
        "avg_match_score": avg_score,
    }


async def get_full_pipeline() -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT
                j.id, j.title, j.company, j.location, j.url,
                j.match_score, j.match_tier, j.priority,
                j.status as job_status,
                j.seniority_level, j.remote_type, j.employment_type,
                j.salary_range_text, j.salary_low, j.salary_high,
                j.description_summary, j.recommendation,
                j.matching_skills, j.missing_skills, j.keyword_matches,
                j.tech_stack, j.requirements_parsed,
                j.company_context, j.company_size, j.company_industry,
                j.discovered_at, j.posted_at, j.expires_at,
                j.status_updated_at, j.notes as job_notes,
                a.id as app_id,
                a.status as app_status,
                a.applied_at, a.applied_via, a.ats_system,
                a.followup_count, a.next_followup_at,
                a.last_activity_at, a.tailoring_notes,
                a.offer_amount, a.offer_deadline,
                a.recruiter_name, a.hiring_manager_name,
                a.notes as app_notes,
                (SELECT COUNT(*) FROM interviews i
                 WHERE i.application_id = a.id
                 AND i.status = 'scheduled') as upcoming_interviews,
                (SELECT COUNT(*) FROM followups f
                 WHERE f.application_id = a.id) as total_followups
               FROM jobs j
               LEFT JOIN applications a ON a.job_id = j.id
               WHERE j.status NOT IN ('skip','new')
               ORDER BY j.priority, j.match_score DESC""",
        )
        return [dict(r) for r in await cursor.fetchall()]


async def get_job_history(job_id: str) -> list[dict]:
    async with get_db() as db:
        cursor = await db.execute(
            """SELECT * FROM status_history
               WHERE entity_id = ? OR entity_id IN (
                   SELECT id FROM applications WHERE job_id = ?
               )
               ORDER BY changed_at ASC""",
            (job_id, job_id),
        )
        return [dict(r) for r in await cursor.fetchall()]

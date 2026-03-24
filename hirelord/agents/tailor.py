"""
Hire Lord — Resume Tailoring Agent
====================================
LangGraph pipeline:

  load_resume
      │
  screen_job          ← classify match quality (Haiku, fast + cheap)
      │
  [skip if weak] ─────────────────────────────────────────► END
      │
  tailor_resume       ← full resume rewrite (Sonnet, high quality)
      │
  write_cover_letter  ← targeted cover letter (Sonnet)
      │
  human_review        ← HITL: approve / edit / reject
      │
  save_outputs        ← write DOCX files + update DB
      │
      END
"""

import json
import uuid
from pathlib import Path
from typing import Optional, Literal
from typing_extensions import TypedDict

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command

from ..prompts.tailor import (
    RESUME_TAILOR_SYSTEM,
    RESUME_TAILOR_HUMAN,
    COVER_LETTER_SYSTEM,
    COVER_LETTER_HUMAN,
    JOB_MATCH_SYSTEM,
    JOB_MATCH_HUMAN,
)
from ..db.store import (
    upsert_job,
    update_job_screening,
    update_job_status,
    create_application,
)

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent.parent
RESUME_PATH = ROOT / "data" / "MIKE_DORAN_UnityResume_Current_2024.docx"
OUTPUT_DIR = ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
DB_PATH = ROOT / "hirelord.db"

# ── Models ────────────────────────────────────────────────────────────────────

# Fast + cheap for screening
haiku = ChatAnthropic(model="claude-haiku-4-5-20251001", temperature=0)

# High quality for tailoring
sonnet = ChatAnthropic(model="claude-sonnet-4-6", temperature=0.3)


# ── State ─────────────────────────────────────────────────────────────────────

class TailoringState(TypedDict):
    # Inputs
    job_id: str
    job_title: str
    company_name: str
    location: str
    job_url: str
    job_description: str
    company_context: str          # Optional extra company info

    # Resume
    base_resume: str

    # Screening
    match_score: int
    match_tier: str               # strong | good | weak | skip
    matching_skills: list[str]
    missing_skills: list[str]
    recommendation: str
    priority: int

    # Outputs
    tailored_resume: str
    cover_letter: str
    tailoring_notes: str

    # HITL
    human_decision: str           # approve | edit | reject
    human_feedback: str

    # Tracking
    application_id: str
    resume_output_path: str
    cover_letter_output_path: str
    error: Optional[str]


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_resume_text() -> str:
    """Load base resume from DOCX."""
    try:
        from docx import Document
        doc = Document(RESUME_PATH)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        # Fallback: return hardcoded text if file not found
        return """MIKE DORAN
Holladay, Utah • 84124 • 801.613.2057 • michaelbryandoran@gmail.com
UNITY DEVELOPER
Highly skilled Unity Developer with extensive experience in XR/VR/AR/MR experiences.
[Full resume in data/MIKE_DORAN_UnityResume_Current_2024.docx]"""


def save_markdown_output(content: str, filename: str) -> Path:
    """Save tailored content as markdown file."""
    path = OUTPUT_DIR / filename
    path.write_text(content, encoding="utf-8")
    return path


# ── Nodes ─────────────────────────────────────────────────────────────────────

def load_resume(state: TailoringState) -> dict:
    """Load the base resume from disk."""
    print("📄 Loading base resume...")
    base_resume = load_resume_text()
    return {"base_resume": base_resume}


def screen_job(state: TailoringState) -> dict:
    """
    Use Haiku to quickly screen the job for match quality.
    Fast and cheap — runs for every job discovered.
    """
    print(f"🔍 Screening: {state['job_title']} @ {state['company_name']}...")

    prompt = JOB_MATCH_HUMAN.format(
        job_title=state["job_title"],
        company_name=state["company_name"],
        location=state.get("location", ""),
        job_description=state["job_description"],
    )

    response = haiku.invoke([
        SystemMessage(content=JOB_MATCH_SYSTEM),
        HumanMessage(content=prompt),
    ])

    # Parse JSON response
    try:
        raw = response.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
    except json.JSONDecodeError:
        # Fallback if model doesn't return clean JSON
        result = {
            "match_score": 50,
            "match_tier": "good",
            "matching_skills": [],
            "missing_skills": [],
            "recommendation": "Manual review needed — screening parse failed.",
            "priority": 3,
        }

    print(f"   Score: {result['match_score']}/100 | Tier: {result['match_tier']}")
    print(f"   {result['recommendation']}")

    return {
        "match_score": result.get("match_score", 50),
        "match_tier": result.get("match_tier", "good"),
        "matching_skills": result.get("matching_skills", []),
        "missing_skills": result.get("missing_skills", []),
        "recommendation": result.get("recommendation", ""),
        "priority": result.get("priority", 3),
    }


def should_proceed(state: TailoringState) -> Literal["tailor_resume", "skip_job"]:
    """Route: only tailor if match tier is strong or good."""
    if state["match_tier"] in ("strong", "good"):
        return "tailor_resume"
    print(f"   ⏭  Skipping — tier: {state['match_tier']}")
    return "skip_job"


def skip_job(state: TailoringState) -> dict:
    """Mark job as skipped in DB and exit."""
    return {"error": f"Job skipped — match tier: {state['match_tier']}"}


def tailor_resume(state: TailoringState) -> dict:
    """
    Use Sonnet to produce a fully tailored resume.
    This is the high-value, high-cost node.
    """
    print(f"✍️  Tailoring resume for {state['job_title']} @ {state['company_name']}...")

    prompt = RESUME_TAILOR_HUMAN.format(
        base_resume=state["base_resume"],
        job_description=state["job_description"],
        company_name=state["company_name"],
        job_title=state["job_title"],
    )

    response = sonnet.invoke([
        SystemMessage(content=RESUME_TAILOR_SYSTEM),
        HumanMessage(content=prompt),
    ])

    full_output = response.content

    # Split resume from tailoring notes
    if "## TAILORING NOTES" in full_output:
        parts = full_output.split("## TAILORING NOTES", 1)
        tailored_resume = parts[0].strip()
        tailoring_notes = "## TAILORING NOTES\n" + parts[1].strip()
    else:
        tailored_resume = full_output
        tailoring_notes = ""

    print("   ✅ Resume tailored.")
    return {
        "tailored_resume": tailored_resume,
        "tailoring_notes": tailoring_notes,
    }


def write_cover_letter(state: TailoringState) -> dict:
    """Generate a targeted cover letter using Sonnet."""
    print(f"📝 Writing cover letter for {state['company_name']}...")

    prompt = COVER_LETTER_HUMAN.format(
        tailored_resume=state["tailored_resume"],
        job_description=state["job_description"],
        company_name=state["company_name"],
        job_title=state["job_title"],
        company_context=state.get("company_context", "No additional context provided."),
    )

    response = sonnet.invoke([
        SystemMessage(content=COVER_LETTER_SYSTEM),
        HumanMessage(content=prompt),
    ])

    print("   ✅ Cover letter written.")
    return {"cover_letter": response.content}


def human_review(state: TailoringState) -> Command:
    """
    HITL checkpoint — show outputs to Mike and get approval.
    Pauses graph execution until resumed with a decision.
    """
    print("\n" + "═" * 60)
    print("👑 HIRE LORD — HUMAN REVIEW REQUIRED")
    print("═" * 60)
    print(f"\nJob:     {state['job_title']} @ {state['company_name']}")
    print(f"Match:   {state['match_score']}/100 ({state['match_tier']})")
    print(f"\n{state['tailoring_notes']}")
    print("\n" + "─" * 60)

    decision = interrupt({
        "message": "Review the tailored resume and cover letter. Approve, edit, or reject?",
        "job_title": state["job_title"],
        "company_name": state["company_name"],
        "match_score": state["match_score"],
        "tailored_resume": state["tailored_resume"],
        "cover_letter": state["cover_letter"],
        "tailoring_notes": state["tailoring_notes"],
        "options": {
            "approve": "Save outputs and mark ready to apply",
            "edit":    "Provide feedback and regenerate",
            "reject":  "Skip this job entirely",
        },
    })

    # decision is dict: {"action": "approve"|"edit"|"reject", "feedback": "..."}
    action = decision.get("action", "approve") if isinstance(decision, dict) else decision

    if action == "reject":
        return Command(
            goto="skip_job",
            update={"human_decision": "reject", "human_feedback": decision.get("feedback", "")},
        )
    elif action == "edit":
        return Command(
            goto="tailor_resume",
            update={
                "human_decision": "edit",
                "human_feedback": decision.get("feedback", ""),
                # Inject feedback into job description for re-tailoring
                "job_description": state["job_description"] + f"\n\n## HUMAN FEEDBACK FOR REVISION\n{decision.get('feedback', '')}",
            },
        )
    else:
        return Command(
            goto="save_outputs",
            update={"human_decision": "approve"},
        )


def save_outputs(state: TailoringState) -> dict:
    """Save tailored resume + cover letter as markdown files and update DB."""
    print("💾 Saving outputs...")

    safe_company = "".join(c if c.isalnum() or c in "-_" else "_" for c in state["company_name"])
    safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in state["job_title"])
    base_name = f"{safe_company}_{safe_title}"

    resume_path = save_markdown_output(
        state["tailored_resume"],
        f"{base_name}_resume.md",
    )
    cover_path = save_markdown_output(
        state["cover_letter"],
        f"{base_name}_cover_letter.md",
    )

    print(f"   📄 Resume:       {resume_path}")
    print(f"   📄 Cover letter: {cover_path}")
    print("\n👑 Hire Lord has spoken. Go get that job, Mike.\n")

    return {
        "resume_output_path": str(resume_path),
        "cover_letter_output_path": str(cover_path),
    }


# ── Graph ─────────────────────────────────────────────────────────────────────

def build_tailor_graph() -> tuple:
    """Build and compile the tailoring graph with SQLite checkpointer."""

    builder = StateGraph(TailoringState)

    # Nodes
    builder.add_node("load_resume",        load_resume)
    builder.add_node("screen_job",         screen_job)
    builder.add_node("tailor_resume",      tailor_resume)
    builder.add_node("write_cover_letter", write_cover_letter)
    builder.add_node("human_review",       human_review)
    builder.add_node("save_outputs",       save_outputs)
    builder.add_node("skip_job",           skip_job)

    # Edges
    builder.add_edge(START,                "load_resume")
    builder.add_edge("load_resume",        "screen_job")
    builder.add_conditional_edges(
        "screen_job",
        should_proceed,
        {"tailor_resume": "tailor_resume", "skip_job": "skip_job"},
    )
    builder.add_edge("tailor_resume",      "write_cover_letter")
    builder.add_edge("write_cover_letter", "human_review")
    # human_review uses Command for dynamic routing → save_outputs or skip_job or tailor_resume
    builder.add_edge("save_outputs",       END)
    builder.add_edge("skip_job",           END)

    # SQLite checkpointer — required for interrupt() to work
    checkpointer = SqliteSaver.from_conn_string(str(DB_PATH))
    graph = builder.compile(checkpointer=checkpointer)

    return graph, checkpointer


# ── Entry point ───────────────────────────────────────────────────────────────

async def tailor_for_job(
    job_title: str,
    company_name: str,
    job_description: str,
    job_url: str = "",
    location: str = "",
    company_context: str = "",
    thread_id: Optional[str] = None,
) -> dict:
    """
    Main entry point: run the full tailoring pipeline for one job.
    Returns the final state dict.
    """
    graph, _ = build_tailor_graph()

    if thread_id is None:
        thread_id = str(uuid.uuid4())

    config = {"configurable": {"thread_id": thread_id}}

    initial_state = {
        "job_id":          str(uuid.uuid4()),
        "job_title":       job_title,
        "company_name":    company_name,
        "location":        location,
        "job_url":         job_url,
        "job_description": job_description,
        "company_context": company_context,
        "base_resume":     "",
        "match_score":     0,
        "match_tier":      "",
        "matching_skills": [],
        "missing_skills":  [],
        "recommendation":  "",
        "priority":        3,
        "tailored_resume": "",
        "cover_letter":    "",
        "tailoring_notes": "",
        "human_decision":  "",
        "human_feedback":  "",
        "application_id":  "",
        "resume_output_path": "",
        "cover_letter_output_path": "",
        "error":           None,
    }

    result = graph.invoke(initial_state, config)
    return result


def resume_after_review(
    thread_id: str,
    action: str,
    feedback: str = "",
) -> dict:
    """
    Resume a paused graph after human review.
    action: "approve" | "edit" | "reject"
    """
    graph, _ = build_tailor_graph()
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(
        Command(resume={"action": action, "feedback": feedback}),
        config,
    )
    return result

"""
Hire Lord — Job Description Parser
=====================================
Uses Haiku to extract structured fields from raw JD text:
  - requirements (parsed list)
  - nice to have
  - responsibilities
  - tech stack
  - seniority level
  - salary info
  - remote type
  - description summary
"""

import json
import os
from dotenv import load_dotenv
load_dotenv()

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage

_parser_llm = None

def get_parser_llm():
    global _parser_llm
    if _parser_llm is None:
        _parser_llm = ChatAnthropic(
            model="claude-haiku-4-5-20251001",
            temperature=0,
        )
    return _parser_llm


PARSE_SYSTEM = """You are a job description parser. Extract structured information from job postings.
Always respond with valid JSON only — no markdown, no preamble, no explanation."""

PARSE_HUMAN = """Parse this job description and return a JSON object with exactly these fields:

{{
  "requirements_parsed": ["list of required qualifications, one per item"],
  "nice_to_have": ["list of preferred/nice-to-have items"],
  "responsibilities": ["list of job responsibilities, one per item"],
  "tech_stack": ["list of technologies, tools, frameworks mentioned"],
  "seniority_level": "entry|mid|senior|staff|principal|lead",
  "employment_type": "full_time|part_time|contract|freelance",
  "remote_type": "remote|hybrid|onsite",
  "salary_low": null or integer (annual USD),
  "salary_high": null or integer (annual USD),
  "salary_range_text": "raw salary text from JD or empty string",
  "description_summary": "3-4 sentence summary of the role"
}}

Rules:
- requirements_parsed: ONLY hard requirements (must have), not nice-to-haves
- nice_to_have: preferred skills, bonus points, "nice to have" items
- tech_stack: just technology names, no descriptions
- seniority_level: infer from title and requirements if not stated
- salary: extract numbers if present, otherwise null
- Keep each list item concise (under 15 words)
- Return ONLY the JSON object, nothing else

JOB DESCRIPTION:
{description}

JOB TITLE: {title}
COMPANY: {company}"""


async def parse_job_description(
    description: str,
    title: str = "",
    company: str = "",
) -> dict:
    """
    Parse a raw job description into structured fields.
    Returns a dict with all parsed fields.
    Falls back to safe defaults if parsing fails.
    """
    llm = get_parser_llm()

    # Truncate very long descriptions to save tokens
    desc_truncated = (description or "")[:6000]

    prompt = PARSE_HUMAN.format(
        description=desc_truncated,
        title=title,
        company=company,
    )

    try:
        response = llm.invoke([
            SystemMessage(content=PARSE_SYSTEM),
            HumanMessage(content=prompt),
        ])

        raw = response.content.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        parsed = json.loads(raw)

        # Ensure all expected fields exist with safe defaults
        return {
            "requirements_parsed": parsed.get("requirements_parsed", []),
            "nice_to_have":        parsed.get("nice_to_have", []),
            "responsibilities":    parsed.get("responsibilities", []),
            "tech_stack":          parsed.get("tech_stack", []),
            "seniority_level":     parsed.get("seniority_level", "mid"),
            "employment_type":     parsed.get("employment_type", "full_time"),
            "remote_type":         parsed.get("remote_type", ""),
            "salary_low":          parsed.get("salary_low"),
            "salary_high":         parsed.get("salary_high"),
            "salary_range_text":   parsed.get("salary_range_text", ""),
            "description_summary": parsed.get("description_summary", ""),
        }

    except (json.JSONDecodeError, Exception) as e:
        print(f"  ⚠  JD parse failed for '{title}': {e}")
        return {
            "requirements_parsed": [],
            "nice_to_have":        [],
            "responsibilities":    [],
            "tech_stack":          [],
            "seniority_level":     "mid",
            "employment_type":     "full_time",
            "remote_type":         "",
            "salary_low":          None,
            "salary_high":         None,
            "salary_range_text":   "",
            "description_summary": "",
        }

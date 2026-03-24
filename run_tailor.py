"""
Hire Lord — CLI Runner
========================
Quick test of the tailoring agent with a sample job posting.

Usage:
    uv run python run_tailor.py
    uv run python run_tailor.py --approve    # auto-approve for testing
"""

import asyncio
import argparse
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

from hirelord.agents.tailor import tailor_for_job, resume_after_review

console = Console()

# ── Sample job for testing ────────────────────────────────────────────────────

SAMPLE_JOB = {
    "job_title": "Senior Unity XR Developer",
    "company_name": "Immersive Labs",
    "location": "Remote (US)",
    "job_url": "https://example.com/jobs/senior-unity-xr",
    "job_description": """
We are looking for a Senior Unity XR Developer to join our team building
next-generation training simulations for enterprise clients in VR and MR.

REQUIREMENTS:
- 3+ years Unity development experience
- Strong C# programming skills
- Experience with VR/AR/MR development (Quest, HoloLens, or similar)
- Knowledge of XR Interaction Toolkit or OpenXR
- Experience with multiplayer networking (Photon, Mirror, or similar)
- Understanding of performance optimization for XR platforms
- Familiarity with 3D math, physics, and animation systems

NICE TO HAVE:
- Experience with AI/ML integration in Unity
- Shader and graphics pipeline knowledge
- Cross-platform development (iOS, Android, PC)
- Leadership or mentorship experience

RESPONSIBILITIES:
- Design and implement immersive VR/MR training experiences
- Collaborate with instructional designers and 3D artists
- Optimize experiences for standalone headsets (Quest 3)
- Lead technical reviews and mentor junior developers
- Maintain and extend our internal XR SDK

We offer competitive salary, full remote, and a genuine passion for XR.
    """.strip(),
    "company_context": "Immersive Labs builds VR/MR training simulations for Fortune 500 companies, focusing on safety training and skills development.",
}


async def main(auto_approve: bool = False):
    console.print(Panel(
        "[bold gold1]👑 HIRE LORD[/bold gold1]\n[dim]Resume Tailoring Agent[/dim]",
        border_style="gold1",
        expand=False,
    ))

    console.print(f"\n[bold]Target:[/bold] {SAMPLE_JOB['job_title']} @ {SAMPLE_JOB['company_name']}")
    console.print(f"[bold]Location:[/bold] {SAMPLE_JOB['location']}\n")

    # Run the graph — it will pause at human_review
    import uuid
    thread_id = str(uuid.uuid4())

    console.print("[dim]Running tailoring pipeline...[/dim]\n")

    result = await tailor_for_job(
        thread_id=thread_id,
        **SAMPLE_JOB,
    )

    # Check if we hit an interrupt (human review needed)
    if result.get("__interrupt__"):
        interrupt_data = result["__interrupt__"][0].value

        console.print("\n" + "═" * 60)
        console.print("[bold gold1]👑 HIRE LORD — YOUR REVIEW[/bold gold1]")
        console.print("═" * 60)

        console.print(f"\n[bold]Match Score:[/bold] {interrupt_data['match_score']}/100 "
                      f"([cyan]{interrupt_data.get('match_tier', '')}[/cyan])")

        console.print("\n[bold underline]TAILORED RESUME PREVIEW[/bold underline]")
        # Show first 50 lines of resume
        preview = "\n".join(interrupt_data["tailored_resume"].split("\n")[:50])
        console.print(Markdown(preview))

        console.print("\n[bold underline]COVER LETTER PREVIEW[/bold underline]")
        preview_cl = "\n".join(interrupt_data["cover_letter"].split("\n")[:30])
        console.print(Markdown(preview_cl))

        console.print("\n[bold underline]TAILORING NOTES[/bold underline]")
        console.print(Markdown(interrupt_data.get("tailoring_notes", "")))

        if auto_approve:
            action = "approve"
            feedback = ""
            console.print("\n[green]Auto-approving (--approve flag)[/green]")
        else:
            console.print("\n[bold]Options:[/bold]")
            console.print("  [green]approve[/green] — Save and mark ready to apply")
            console.print("  [yellow]edit[/yellow]    — Provide feedback and regenerate")
            console.print("  [red]reject[/red]   — Skip this job\n")

            action = Prompt.ask(
                "Your decision",
                choices=["approve", "edit", "reject"],
                default="approve",
            )
            feedback = ""
            if action == "edit":
                feedback = Prompt.ask("Feedback for revision")

        # Resume the graph
        console.print(f"\n[dim]Resuming with: {action}[/dim]")
        final_result = resume_after_review(thread_id, action, feedback)

        if action == "approve":
            console.print(Panel(
                f"[bold green]✅ Application package ready![/bold green]\n\n"
                f"Resume:       {final_result.get('resume_output_path', 'N/A')}\n"
                f"Cover Letter: {final_result.get('cover_letter_output_path', 'N/A')}",
                border_style="green",
            ))
        elif action == "reject":
            console.print("[yellow]Job rejected — moving on 👑[/yellow]")
        else:
            console.print("[cyan]Regenerating with your feedback...[/cyan]")

    elif result.get("error"):
        console.print(f"[yellow]⏭  {result['error']}[/yellow]")
    else:
        console.print(Panel(
            f"[bold green]✅ Done![/bold green]\n"
            f"Resume:       {result.get('resume_output_path', 'N/A')}\n"
            f"Cover Letter: {result.get('cover_letter_output_path', 'N/A')}",
            border_style="green",
        ))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hire Lord — Tailoring Agent")
    parser.add_argument("--approve", action="store_true", help="Auto-approve for testing")
    args = parser.parse_args()
    asyncio.run(main(auto_approve=args.approve))

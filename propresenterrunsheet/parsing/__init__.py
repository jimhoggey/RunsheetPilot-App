"""PDF + AI parsing for the runsheet upload pipeline.

  pdf.py          — extract_pdf_text (pdfplumber wrapper)
  duration.py     — _TIME_RE, _extract_*
  ai.py           — DEFAULT_PROMPT, prompt assembly, JSON cleanup of model output
  ask.py          — the one helper every small AI question goes through
  folder_plan.py  — which media folders hold a service's media, and their jobs
  media_plan.py   — which line each folder file goes under

Callers import the submodules directly; this package re-exports nothing.
"""

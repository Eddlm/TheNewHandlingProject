# edd-handling AGENTS.md

## Handling Auditor Rule

- After any edit to `tools/handling_auditor.py`, `tools/handling_auditor.pyw`, or closely related auditor UI/support files, launch the app for the user before replying.
- Before launching, check whether the auditor is already running and stop the existing instance so the user gets a fresh window with the latest changes.
- Preferred launcher:
  - `pythonw .\tools\handling_auditor.pyw`
- If `pythonw` causes issues on the machine, use:
  - `python .\tools\handling_auditor.py`

# Handling Auditor

Run the standalone desktop auditor with:

```powershell
python .\tools\handling_auditor.py
```

To launch it without the extra `python.exe` console window on Windows, use:

```powershell
pythonw .\tools\handling_auditor.pyw
```

Optional CLI summary:

```powershell
python .\tools\handling_auditor.py --summary-only
python .\tools\handling_auditor.py --summary-only --mode manifest
```

Notes:

- The auditor keeps a local cache in `tools/.cache/handling_auditor_cache.json`.
- Cached entries are reused only when file size and nanosecond modified time still match.
- If you edit a scanned `vehicles.meta` or a handling `.meta`, that file is automatically rescanned on the next run.
- Safe edit actions are only enabled when the selected handling resolves to exactly one matching block in `Active` and exactly one in `Inactive`.
- Before any safe edit, the app creates timestamped backups in `tools/backups/`.

# Drata Evidence Uploader

Automates the monthly upload of compliance evidence into Drata's Evidence Library and maps each entry to control **MICS-35**.

---

## Prerequisites

**Python 3.9 or newer**

- **Mac / Linux** — Python 3 is usually pre-installed. Verify with `python3 --version`.
- **Windows** — Download from [python.org](https://www.python.org/downloads/). During install, check **"Add Python to PATH"**.

**Install the one dependency:**

```
# Mac / Linux
pip3 install requests

# Windows
pip install requests
```

---

## Setup

### 1 — Get your Drata API token

1. Log into Drata → **Settings** → **API Tokens**
2. Create a new token (give it a name like "Evidence Uploader")
3. Copy the token — you'll paste it when the script prompts you

### 2 — Find your Workspace ID

Your Workspace ID is the number in the Drata URL when you're logged in:
```
https://app.drata.com/workspaces/1/...
                                 ^
```

---

## Folder structure

Place this script in, or run it from, the root folder that contains your evidence. The expected layout is:

```
evidence-root/
  Application Name/
    System/
      YYYY/
        MM/
          okta-report-april.pdf
  AWS/
    CloudTrail/
      2026/
        04/
          cloudtrail-april.xlsx
```

Each `Month` folder should contain **one document**. If multiple files are present the script uses the first alphabetically and warns you.

---

## Running the script

```
# Mac / Linux
cd /path/to/evidence-root
python3 upload_evidence.py

# Windows (Command Prompt or PowerShell)
cd C:\path\to\evidence-root
python upload_evidence.py
```

The script will prompt you for:

| Prompt | What to enter |
|---|---|
| Drata API token | Paste the token from Step 1 (input is hidden) |
| Workspace ID | The number from Step 2 |
| Month to process | YYYY-MM format, e.g. `2026-04` — press Enter to use the current month |
| Owner email | Email of the Drata user who owns this evidence |

It then scans the folder, shows you what it found, asks for confirmation, and uploads.

---

## What it does

For each document found for the selected month:

- If an Evidence Library entry named `AppName - SystemName - YYYY-MM` **already exists** → uploads the file as a new version
- If no entry exists → creates one and maps it to control **MICS-35**

---

## Troubleshooting

| Error | Fix |
|---|---|
| `requests` package not found | Run `pip3 install requests` (Mac) or `pip install requests` (Windows) |
| `HTTP 401` | API token is invalid or expired — generate a new one in Drata Settings |
| `HTTP 403` | Your token doesn't have permission for Evidence Library — check token scopes in Drata |
| `NOT FOUND` on owner email | The email isn't in Drata Personnel — check Settings → People |
| `NOT FOUND` on MICS-35 | The control code doesn't exist in your workspace — verify in Drata Controls |
| Script hangs / times out | Check your internet connection; large files time out after 2 minutes |

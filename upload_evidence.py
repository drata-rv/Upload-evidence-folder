#!/usr/bin/env python3
"""
Drata Evidence Uploader
-----------------------
Every month a new document lands in a folder tree maintained by the compliance
team.  This script walks that tree, finds the documents for the target month,
and syncs them into Drata's Evidence Library — creating a new entry if one
doesn't exist yet, or uploading a new version if it does.  Every entry is
mapped to control MICS-35 (Monitoring & Incident Controls Standard, item 35),
which Drata uses to track this class of recurring operational evidence.

Folder layout expected (script must be run from the root):
  <root>/<AppName>/<SystemName>/<YYYY>/<MM>/<document>

Evidence entries are named:  "<AppName> - <SystemName> - YYYY-MM"

Usage:
    cd /path/to/evidence/root
    python upload_evidence.py

Requirements:
    pip install requests
"""

import os
import re
import sys
import getpass
import calendar
import datetime
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package is required.  Run:  pip install requests")
    sys.exit(1)

# ── Console setup (Windows-safe ANSI + UTF-8) ─────────────────────────────────

def _setup_console() -> bool:
    """
    Enable ANSI escape processing and UTF-8 output.
    On Windows this requires two explicit steps; on macOS/Linux it's a no-op.
    Returns True if ANSI colour codes are safe to emit.
    """
    if not sys.stdout.isatty():
        return False

    if os.name == "nt":
        # Reconfigure stdout/stderr so Unicode box-drawing chars don't blow up
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

        # Ask Windows to honour ANSI escape sequences in the console
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 0x0007)
            return True
        except Exception:
            return False  # Older Windows without VT support — colours off, no crash

    return True


# Resolved once at import so stdout is reconfigured before the first print()
_ANSI_OK = _setup_console()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _ANSI_OK else text

bold   = lambda t: _c("1",  t)
green  = lambda t: _c("32", t)
yellow = lambda t: _c("33", t)
red    = lambda t: _c("31", t)
cyan   = lambda t: _c("36", t)
dim    = lambda t: _c("2",  t)


# ── Drata API client ──────────────────────────────────────────────────────────

DRATA_BASE = "https://public-api.drata.com/public/v2"
TARGET_CONTROL = "MICS-35"


class DrataError(Exception):
    pass


class DrataClient:
    def __init__(self, api_token: str, workspace_id: str):
        self._workspace_id = workspace_id
        self._base = f"{DRATA_BASE}/workspaces/{workspace_id}"
        self._s = requests.Session()
        self._s.headers["Authorization"] = f"Bearer {api_token}"

    # ── helpers ───────────────────────────────────────────────────────────────

    def _check(self, resp: requests.Response) -> None:
        """Raise DrataError with a readable message on any non-2xx response."""
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:400]
            raise DrataError(f"HTTP {resp.status_code}: {detail}")

    def _paginate(self, url: str, params: dict):
        """Yield every item from a cursor-paginated endpoint."""
        cursor = None
        while True:
            p = {**params}
            if cursor:
                p["cursor"] = cursor
            resp = self._s.get(url, params=p, timeout=30)
            self._check(resp)
            body = resp.json()
            yield from body.get("data", [])
            cursor = (body.get("pagination") or {}).get("cursor")
            if not cursor:
                break

    # ── public methods ────────────────────────────────────────────────────────

    def find_user_by_email(self, email: str) -> Optional[int]:
        """Return the personnelId for a Drata user by their email address."""
        resp = self._s.get(f"{self._base}/personnel/email:{email}", timeout=30)
        self._check(resp)
        body = resp.json()
        # Drata v2 uses "personnelId"; guard against "id" in case the schema shifts
        return body.get("personnelId") or body.get("id")

    def find_control_id(self, code: str) -> Optional[int]:
        """Return the numeric ID of a control by its code (e.g. 'MICS-35')."""
        for ctrl in self._paginate(f"{self._base}/controls", {"size": 500}):
            if ctrl.get("code") == code:
                return ctrl["id"]
        return None

    def find_evidence(self, name: str) -> Optional[dict]:
        """Return the first evidence entry whose name matches exactly, or None."""
        # The API supports name filtering — use it to narrow the scan
        for item in self._paginate(
            f"{self._base}/evidence-library", {"size": 50, "name": name}
        ):
            if item.get("name") == name:
                return item
        return None

    def create_evidence(
        self,
        name: str,
        file_path: Path,
        filed_at: str,
        control_ids: list[int],
        owner_id: int,
    ) -> dict:
        """Create a new Evidence Library entry and upload the file as its first version.

        fields is a list of tuples (not a dict) because multipart/form-data allows
        repeated keys — the only way to send an array like controlIds over this
        content type without serialising to JSON.
        """
        fields = [
            ("name",                 name),
            ("filedAt",              filed_at),
            ("renewalScheduleType",  "ONE_MONTH"),  # evidence renews every month
            ("ownerId",              str(owner_id)),
        ]
        for cid in control_ids:
            fields.append(("controlIds", str(cid)))

        with open(file_path, "rb") as fh:
            resp = self._s.post(
                f"{self._base}/evidence-library",
                files={"file": (file_path.name, fh)},
                data=fields,
                timeout=120,
            )
        self._check(resp)
        return resp.json()

    def update_evidence(
        self,
        evidence_id: int,
        file_path: Path,
        filed_at: str,
        owner_id: int,
    ) -> dict:
        """Upload a new file version to an existing Evidence Library entry.

        controlIds is intentionally omitted: the PUT endpoint treats any supplied
        value (including an empty array) as a full replacement, so sending it
        would silently drop control mappings set elsewhere in Drata.
        """
        with open(file_path, "rb") as fh:
            resp = self._s.put(
                f"{self._base}/evidence-library/{evidence_id}",
                files={"file": (file_path.name, fh)},
                data={
                    "filedAt":             filed_at,
                    "renewalScheduleType": "ONE_MONTH",
                    "ownerId":             str(owner_id),
                },
                timeout=120,
            )
        self._check(resp)
        return resp.json()


# ── Month name resolution ─────────────────────────────────────────────────────

# Covers numeric, abbreviated, and full month names including non-standard
# abbreviations actually seen in the wild (Sept, July, June, April).
MONTH_MAP: dict[str, int] = {
    "1": 1,  "01": 1,  "jan": 1,  "january": 1,
    "2": 2,  "02": 2,  "feb": 2,  "february": 2,
    "3": 3,  "03": 3,  "mar": 3,  "march": 3,
    "4": 4,  "04": 4,  "apr": 4,  "april": 4,
    "5": 5,  "05": 5,  "may": 5,
    "6": 6,  "06": 6,  "jun": 6,  "june": 6,
    "7": 7,  "07": 7,  "jul": 7,  "july": 7,
    "8": 8,  "08": 8,  "aug": 8,  "august": 8,
    "9": 9,  "09": 9,  "sep": 9,  "sept": 9,  "september": 9,
    "10": 10, "oct": 10, "october": 10,
    "11": 11, "nov": 11, "november": 11,
    "12": 12, "dec": 12, "december": 12,
}

def _parse_month(name: str) -> Optional[int]:
    """Return the month number for a folder name, or None if unrecognisable."""
    return MONTH_MAP.get(name.lower())

def _is_year(name: str) -> bool:
    """True if the folder name is a 4-digit year."""
    return bool(re.fullmatch(r'\d{4}', name))

def _find_month_dir(year_dir: Path, month: int) -> Optional[Path]:
    """Return the child directory of year_dir whose name resolves to month."""
    for d in year_dir.iterdir():
        if d.is_dir() and _parse_month(d.name) == month:
            return d
    return None

# Matches a leading date stamp like '2026.3 - ' or '2025.11 – '
_DATE_PREFIX = re.compile(r'^\d{4}\.\d{1,2}\s*[-–]\s*')

def _report_type(stem: str, app_name: str) -> str:
    """
    Derive a stable, human-readable report type from a file stem.

    '2026.3 - Synkros - Employee Roles Listing Report' → 'Employee Roles Listing Report'
    'Access Matrix – Synkros – DRAFT'                  → 'Access Matrix – Synkros – DRAFT'

    The result is used as the middle segment of the Drata evidence label so that
    the same report type maps to the same evidence entry every month.
    """
    cleaned = _DATE_PREFIX.sub("", stem).strip()
    # Strip the app name from the front if the filename leads with it
    cleaned = re.sub(
        rf'^{re.escape(app_name)}\s*[-–]\s*', "", cleaned, flags=re.IGNORECASE
    ).strip()
    return cleaned or stem


# ── Folder scanner ────────────────────────────────────────────────────────────

def scan_folder(root: Path, year: int, month: int) -> list[tuple[str, str, Path]]:
    """
    Return (app_name, label_middle, file_path) for every document that belongs
    to the requested month.

    Handles two layouts automatically:

      AppName / Year / Month / files          (no system level)
        → one entry per file, label_middle derived from the filename

      AppName / SystemName / Year / Month / file   (system level present)
        → one entry per system, label_middle = SystemName

    Month directories can be numeric ('03'), abbreviated ('Mar', 'Sept'),
    or full names ('March', 'September').
    """
    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    hits: list[tuple[str, str, Path]] = []

    for app_dir in sorted(root.iterdir()):
        if not app_dir.is_dir() or app_dir.name.startswith("."):
            continue

        for child in sorted(app_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue

            if _is_year(child.name):
                # Layout: AppName/Year/Month — no system subfolder.
                # Upload every file in the month dir as its own evidence entry,
                # using the cleaned filename as the differentiating label segment.
                if int(child.name) != year:
                    continue
                month_dir = _find_month_dir(child, month)
                if not month_dir:
                    continue
                for doc in sorted(month_dir.iterdir()):
                    if doc.is_file() and not doc.name.startswith("."):
                        hits.append((app_dir.name, _report_type(doc.stem, app_dir.name), doc))

            else:
                # Layout: AppName/SystemName/Year/Month — system subfolder present.
                # The system name becomes the label middle; take the first file.
                sys_dir = child
                year_dir = sys_dir / str(year)
                if not year_dir.is_dir():
                    continue
                month_dir = _find_month_dir(year_dir, month)
                if not month_dir:
                    continue
                docs = [
                    f for f in sorted(month_dir.iterdir())
                    if f.is_file() and not f.name.startswith(".")
                ]
                if not docs:
                    continue
                if len(docs) > 1:
                    print(yellow(
                        f"  Warning: multiple files under {month_dir}; "
                        f"using first: {docs[0].name}"
                    ))
                hits.append((app_dir.name, sys_dir.name, docs[0]))

    return hits


# ── Interactive helpers ───────────────────────────────────────────────────────

def ask(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{dim(default)}]" if default else ""
    prompt = f"{bold(label)}{suffix}: "
    value  = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    return value or default


def ask_month() -> tuple[int, int]:
    today = datetime.date.today()
    default = f"{today.year}-{today.month:02d}"
    print(f"\n{bold('Month to process')} (YYYY-MM, default = current month)")
    raw = input(f"  → ").strip() or default
    try:
        dt = datetime.datetime.strptime(raw, "%Y-%m")
        return dt.year, dt.month
    except ValueError:
        print(yellow(f"  Could not parse '{raw}', falling back to current month."))
        return today.year, today.month


# ── Entry point ───────────────────────────────────────────────────────────────

BANNER = f"""
{cyan('╔══════════════════════════════════════════════╗')}
{cyan('║')}   {bold('Drata Evidence Uploader')}  {dim('v1.0')}              {cyan('║')}
{cyan('║')}   Automates monthly evidence → {bold('MICS-35')}       {cyan('║')}
{cyan('╚══════════════════════════════════════════════╝')}
"""


def main() -> None:
    print(BANNER)

    # ── Config ────────────────────────────────────────────────────────────────
    print(bold("─── Configuration ───────────────────────────────\n"))
    api_token    = ask("Drata API token", secret=True)
    workspace_id = ask("Workspace ID")
    root_path    = Path.cwd()

    year, month  = ask_month()
    month_label  = f"{year}-{month:02d}"
    last_day     = calendar.monthrange(year, month)[1]  # [1] = number of days in month
    filed_at     = f"{year}-{month:02d}-{last_day}"

    print(f"\n  Period   : {bold(month_label)}")
    print(f"  Filed at : {dim(filed_at)}")
    print(f"  Root     : {dim(str(root_path))}")

    # ── Scan ──────────────────────────────────────────────────────────────────
    print(f"\n{bold('─── Scanning folder ─────────────────────────────')}\n")
    try:
        documents = scan_folder(root_path, year, month)
    except FileNotFoundError as exc:
        print(red(f"Error: {exc}"))
        sys.exit(1)

    if not documents:
        print(yellow(f"No documents found for {month_label}.  Nothing to upload."))
        sys.exit(0)

    for app, system, doc in documents:
        label = f"{app} - {system} - {month_label}"
        print(f"  {green('●')} {bold(label)}")
        print(f"      {dim(doc.name)}")

    confirm = input(f"\n{bold('Upload these')} {len(documents)} document(s)? [Y/n]: ").strip().lower()
    if confirm == "n":
        print("Aborted.")
        sys.exit(0)

    # ── Bootstrap API client ──────────────────────────────────────────────────
    client = DrataClient(api_token, workspace_id)

    print(f"\n{bold('─── Resolving IDs ───────────────────────────────')}\n")

    # Control
    print(f"  Looking up {bold(TARGET_CONTROL)}... ", end="", flush=True)
    try:
        control_id = client.find_control_id(TARGET_CONTROL)
    except DrataError as exc:
        print(red("FAILED"))
        print(red(f"  {exc}"))
        sys.exit(1)

    if not control_id:
        print(red("NOT FOUND"))
        print(red(f"  No control with code '{TARGET_CONTROL}' in workspace {workspace_id}."))
        sys.exit(1)
    print(green(f"ID = {control_id}"))

    # Owner
    owner_email = ask("Owner email (evidence will be assigned to this person)")
    print(f"  Looking up {bold(owner_email)}... ", end="", flush=True)
    try:
        owner_id = client.find_user_by_email(owner_email)
    except DrataError as exc:
        print(red("FAILED"))
        print(red(f"  {exc}"))
        sys.exit(1)

    if not owner_id:
        print(red("NOT FOUND"))
        print(red(f"  No Drata personnel record found for '{owner_email}'."))
        sys.exit(1)
    print(green(f"ID = {owner_id}"))

    # ── Upload loop ───────────────────────────────────────────────────────────
    print(f"\n{bold('─── Uploading ───────────────────────────────────')}\n")
    created = updated = failed = 0

    for app, system, doc_path in documents:
        label = f"{app} - {system} - {month_label}"
        print(f"  {bold(label)}")
        print(f"  {dim('file:')} {doc_path.name}")

        try:
            existing = client.find_evidence(label)

            if existing:
                ev_id = existing["id"]
                print(f"  {dim('→')} Found entry (ID {ev_id}) — uploading new version... ", end="", flush=True)
                client.update_evidence(ev_id, doc_path, filed_at, owner_id)
                print(green("UPDATED"))
                updated += 1
            else:
                print(f"  {dim('→')} No existing entry — creating... ", end="", flush=True)
                result = client.create_evidence(label, doc_path, filed_at, [control_id], owner_id)
                print(green(f"CREATED (ID {result['id']})"))
                created += 1

        except DrataError as exc:
            print(red("FAILED"))
            print(red(f"  API error: {exc}"))
            failed += 1
        except requests.Timeout:
            print(red("TIMED OUT"))
            print(red("  The request took too long. Check your connection and try again."))
            failed += 1
        except OSError as exc:
            print(red("FAILED"))
            print(red(f"  File error: {exc}"))
            failed += 1

        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(bold("─── Summary ─────────────────────────────────────\n"))
    print(f"  {green('Created:')} {created}")
    print(f"  {cyan('Updated:')} {updated}")
    print(f"  {(red if failed else dim)('Failed:')}  {failed}\n")

    if failed == 0:
        print(green(bold("All evidence uploaded successfully.")))
    else:
        print(yellow(f"Completed with {failed} failure(s) — review errors above."))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{yellow('Interrupted.')}")
        sys.exit(130)

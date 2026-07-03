#!/usr/bin/env python3
"""
Drata Evidence Uploader  v2.2
------------------------------
Every month new documents land in a folder tree maintained by the compliance
team.  This script walks that tree, finds the documents for the target month,
and syncs them into Drata's Evidence Library — creating a new entry if one
doesn't exist yet, or uploading a new version if it does.

Evidence names are STABLE across months: date tokens are stripped from
filenames so the same Evidence Library entry is updated every month rather
than a new entry being created each time.

Two folder layouts are supported (auto-detected per app, mixed layouts within
the same app folder are handled):

  AppName / YYYY / Month / files
    Evidence name  →  AppName  (or  AppName - ReportName  for multi-file apps)
    Control        →  UAR-<AppName>

  AppName / SystemName / YYYY / Month / files
    Evidence name  →  AppName - SystemName  (or  AppName - SystemName - ReportName)
    Control        →  UAR-<AppName>

Evidence expires on the 1st of the month following the one being filed
(e.g. April evidence expires May 1).

Usage — single month (interactive):
    cd /path/to/evidence/root
    python upload_evidence.py

Usage — historical backfill (one-time, configurable date range):
    cd /path/to/evidence/root
    python upload_evidence.py --backfill

Requirements:
    pip install requests
"""

from __future__ import annotations

import os
import re
import sys
import argparse
import getpass
import calendar
import datetime
import mimetypes
from itertools import groupby
from pathlib import Path
from typing import NamedTuple, Optional

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
        # Reconfigure stdout/stderr so Unicode box-drawing chars don't blow up.
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

        # Ask Windows to honour ANSI escape sequences in the console.
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 0x0007)
            return True
        except Exception:
            return False  # Older Windows without VT support — colours off, no crash.

    return True


# Resolved once at import so stdout is reconfigured before the first print().
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


class DrataError(Exception):
    pass


def _read_file_checked(file_path: Path) -> bytes:
    """Read a file's full contents, catching a truncated/partial OneDrive read.

    OneDrive "Files On-Demand" cloud-only files can occasionally yield fewer
    bytes than their reported size instead of raising an error or fully
    hydrating. Sending that partial content to the API produces a confusing
    generic rejection instead of a clear local error, so check it here first.
    """
    file_bytes = file_path.read_bytes()
    on_disk_size = file_path.stat().st_size
    if not file_bytes:
        raise DrataError(f"File is empty or not synced from OneDrive: {file_path.name}")
    if len(file_bytes) != on_disk_size:
        raise DrataError(
            f"Read {len(file_bytes)} of {on_disk_size} bytes — file may still be "
            f"syncing from OneDrive: {file_path.name}"
        )
    return file_bytes


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
        """Return the Drata user ID for the given email address.

        Uses GET /users/email:{email} — a direct lookup that is not workspace-
        scoped.  Drata's Evidence Library API accepts ownerId as a numeric
        integer only, so this lookup is the only path from email to ID.

        Returns None (without raising) on 404 so the caller can offer the
        "continue without owner" fallback.
        """
        resp = self._s.get(f"{DRATA_BASE}/users/email:{email}", timeout=30)
        if resp.status_code == 404:
            return None
        self._check(resp)
        return resp.json().get("id")

    def find_control_id(self, code: str) -> Optional[int]:
        """Return the numeric ID of a control by its code (e.g. 'UAR-Synkros')."""
        for ctrl in self._paginate(f"{self._base}/controls", {"size": 500}):
            if ctrl.get("code") == code:
                return ctrl["id"]
        return None

    def find_evidence(self, name: str) -> Optional[dict]:
        """Return the first evidence entry whose name matches exactly, or None."""
        # The API supports name filtering — use it to narrow the scan.
        for item in self._paginate(
            f"{self._base}/evidence-library", {"size": 50, "name": name}
        ):
            if item.get("name") == name:
                return item
        return None

    def find_evidence_by_stem(self, cleaned_stem: str, control_code: str) -> Optional[dict]:
        """Fallback lookup for entries created by an older version of this script.

        Older versions used raw filenames as evidence names (e.g.
        "Application-2026.5 – Synkros – Roles Permissions Listing Report.xlsx").
        The cleaned stem — the date-stripped report name — appears as a substring
        in both old and new names, so we can still identify the right entry.

        Fetches all evidence with controls expanded and matches entries where:
          1. cleaned_stem is a substring of the entry name (case-insensitive)
          2. The entry is linked to the expected control code (e.g. UAR-Synkros)

        If more than one entry matches, the stem is too generic to disambiguate
        safely (e.g. "Users" matching both "Remote Desktop Users" and "Recently
        Disabled Users") — returns None so the caller creates a new entry
        instead of silently attaching to the wrong one.
        """
        if not cleaned_stem or len(cleaned_stem) < 5:
            return None
        matches = [
            item
            for item in self._paginate(
                f"{self._base}/evidence-library",
                {"size": 200, "expand[]": "controls"},
            )
            if cleaned_stem.lower() in item.get("name", "").lower()
            and any(c.get("code") == control_code for c in item.get("controls", []))
        ]
        return matches[0] if len(matches) == 1 else None

    def create_evidence(
        self,
        name: str,
        file_path: Path,
        filed_at: str,
        renewal_date: str,
        control_ids: list[int],
        owner_id: Optional[int],
    ) -> dict:
        """Create a new Evidence Library entry and upload the file as its first version.

        All fields (text and file) are passed through the files= parameter as a list
        of tuples so that requests produces a single, well-formed multipart body.
        Text fields use (None, value) — no filename — which is equivalent to data= fields
        but avoids the files+data merge that can silently drop the file part on Windows.
        controlIds appears multiple times (one tuple per ID) because multipart/form-data
        is the only way to send an array without serialising to JSON.

        The file's Content-Type is guessed from its extension (e.g. .xlsx ->
        the real Office Open XML MIME type). A generic "application/octet-stream"
        for every file regardless of type risks the API routing it through the
        wrong content validator.
        """
        file_bytes = _read_file_checked(file_path)
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

        parts: list = [
            ("name",                (None, name)),
            ("filedAt",             (None, filed_at)),
            ("renewalScheduleType", (None, "CUSTOM")),
            ("renewalDate",         (None, renewal_date)),
        ]
        if owner_id is not None:
            parts.append(("ownerId", (None, str(owner_id))))
        for cid in control_ids:
            parts.append(("controlIds", (None, str(cid))))
        parts.append(("file", (file_path.name, file_bytes, content_type)))

        resp = self._s.post(
            f"{self._base}/evidence-library",
            files=parts,
            timeout=120,
        )
        self._check(resp)
        return resp.json()

    def update_evidence(
        self,
        evidence_id: int,
        file_path: Path,
        filed_at: str,
        renewal_date: str,
        owner_id: Optional[int],
    ) -> dict:
        """Upload a new file version to an existing Evidence Library entry.

        controlIds is intentionally omitted: the PUT endpoint treats any
        supplied value (including an empty array) as a full replacement, which
        would silently drop control mappings set elsewhere in Drata.
        """
        file_bytes = _read_file_checked(file_path)
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"

        parts: list = [
            ("filedAt",             (None, filed_at)),
            ("renewalScheduleType", (None, "CUSTOM")),
            ("renewalDate",         (None, renewal_date)),
        ]
        if owner_id is not None:
            parts.append(("ownerId", (None, str(owner_id))))
        parts.append(("file", (file_path.name, file_bytes, content_type)))

        resp = self._s.put(
            f"{self._base}/evidence-library/{evidence_id}",
            files=parts,
            timeout=120,
        )
        self._check(resp)
        return resp.json()


# ── Month name resolution ─────────────────────────────────────────────────────

# Covers numeric, abbreviated, and full month names including non-standard
# abbreviations seen in real folder trees (Sept, July, June, April).
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
    return bool(re.fullmatch(r"\d{4}", name))


def _find_month_dir(year_dir: Path, month: int) -> Optional[Path]:
    """Return the child of year_dir whose name resolves to month, or None."""
    for d in year_dir.iterdir():
        if d.is_dir() and _parse_month(d.name) == month:
            return d
    return None


def _control_code(app_name: str) -> str:
    """Derive the UAR control code from an app folder name.

    Spaces are removed then the whole string is capitalised (first letter
    upper, rest lower) to match Drata's control naming convention.

    'Active Directory' → 'UAR-Activedirectory'
    'eMarker'          → 'UAR-Emarker'
    'Synkros'          → 'UAR-Synkros'
    """
    return "UAR-" + app_name.replace(" ", "").capitalize()


# ── Evidence name normalisation ───────────────────────────────────────────────

# Internal scan result — raw data before name resolution.
class ScanRaw(NamedTuple):
    app_name:     str
    sys_name:     Optional[str]   # None for Layout A (no system tier)
    raw_stem:     str             # original filename stem, unchanged
    cleaned_stem: str             # after date stripping
    was_stripped: bool            # True if a date pattern was found and removed
    file_path:    Path
    year:         int
    month:        int


# Upload-ready item — stable evidence name resolved, ready for API calls.
class UploadItem(NamedTuple):
    app_name:      str
    evidence_name: str
    file_path:     Path
    year:          int
    month:         int
    cleaned_stem:  str   # date-stripped report name; used for fallback lookup


# EN DASH (U+2013) appears in real filenames alongside regular hyphens.
_DASH = r"[–\-]"


def _clean_stem(stem: str, app_name: str) -> tuple[str, bool]:
    """Strip known date and noise tokens from a filename stem.

    Returns (cleaned_stem, was_stripped).

    Pattern 0 — trailing copy numbers  (1), (3):
        'LVPWAPBPIT1 - Administrators - 08022024 (3)'  →  copy number gone first

    Pattern 1 — leading YY.M or YYYY.M – [AppName –]:
        '24.04-Bravo-Employee Security Level Report'  →  'Employee Security Level Report'
        '2026.4 – Synkros – Employee Roles Listing Report'  →  'Employee Roles Listing Report'

    Pattern 2 — trailing – MMDDYYYY or _MMDDYYYY (8-digit date block):
        'LVPDBEMARK1 – Remote Desktop Users – 04142026'  →  'LVPDBEMARK1 – Remote Desktop Users'
        'LVPWDBBPIT1_DBO_10012024'  →  'LVPWDBBPIT1_DBO'

    Pattern 3 — trailing date with dash, en-dash, OR space: [–\s]M.DD.YY(YY):
        'Stadium-04.21.26'     →  'Stadium'
        'SystemUsers 08.15.24' →  'SystemUsers'
        'Stadium FB SystemUsers 3.4.25' →  'Stadium FB SystemUsers'

    Pattern 4 — leading server/host code (ALL-CAPS + digits):
        'LVPWAPBPIT1 - Remote Desktop Users'  →  'Remote Desktop Users'
        'LVPDBEMARK1_DBO'                     →  'DBO'
    """
    s = stem
    n = 0

    # Pattern 0: trailing copy numbers like (1), (3), (4)
    s, k = re.subn(r"\s*\(\d+\)$", "", s)
    n += k

    # Pattern 1: leading YY.M or YYYY.M – [AppName –]  (space around separator OK)
    s, k = re.subn(
        r"^\d{2,4}\s*[.\-]\s*\d{1,2}\s*" + _DASH + r"\s*(?:"
        + re.escape(app_name) + r"\s*" + _DASH + r"\s*)?",
        "", s, flags=re.IGNORECASE,
    )
    n += k

    # Pattern 2: trailing 6-8 digit date block with dash, underscore, or space separator
    # Covers MMDDYYYY (8-digit), MMDDYY (6-digit), and MMDDYYY edge cases.
    s, k = re.subn(r"(?:\s*[-–_]\s*|\s+)\d{6,8}$", "", s)
    n += k

    # Pattern 3: trailing date with dash, en-dash, or space separator
    # Allows single-digit month OR day (e.g. 3.4.25 = March 4, 2025).
    s, k = re.subn(r"[-–\s]\d{1,2}\.\d{1,2}\.\d{2,4}$", "", s)
    n += k

    # Pattern 4: leading server/host code (all-uppercase + digits, e.g. LVPWAPBPIT1)
    s, k = re.subn(r"^[A-Z]{2}[A-Z0-9]+(?:\s*[-–_]\s*|\s+)", "", s)
    n += k

    return s.strip(" –-"), n > 0


def _is_date_contaminated(stem: str) -> bool:
    """True if the stem still contains suspicious digit sequences after cleaning.

    Used to decide whether to prompt the user for an unrecognised date pattern.
    """
    return bool(
        re.search(r"\b\d{6,8}\b", stem)                         # standalone 6–8 digit number
        or re.search(r"\b\d{2}[./]\d{2}[./]\d{2,4}\b", stem)   # MM.DD.YY(YY) embedded
        or re.match(r"^\d{2,4}[.\-]\d{1,2}", stem)               # leading YY.M or YYYY.M
    )


def _build_evidence_name(
    app_name: str,
    sys_name: Optional[str],
    cleaned_stem: str,
) -> str:
    """Assemble the stable evidence name from path components + cleaned stem.

    Strips redundant app/system name repetition so 'Stadium SystemUsers'
    becomes 'Stadium - SystemUsers', not 'Stadium - Stadium SystemUsers'.

    Also handles noise prefixes like 'BravoPit-' (app variant) and
    'Pit - Application Server - ' (venue + sys name after app strip).
    """
    s = cleaned_stem

    # Strip leading app-name variants: "Bravo -", "BravoPit-", "Stadium " (space only).
    s = re.sub(
        r"^" + re.escape(app_name) + r"\w*\s*(?:" + _DASH + r"\s*|\s+)",
        "", s, flags=re.IGNORECASE,
    ).strip()

    if sys_name:
        # Try normal leading prefix first.
        s2 = re.sub(
            r"^" + re.escape(sys_name) + r"\s*" + _DASH + r"\s*",
            "", s, flags=re.IGNORECASE,
        ).strip()
        if s2 != s:
            s = s2
        else:
            # sys_name may follow a noise token like "Pit – ".
            # Only search within the first 40 characters to avoid false positives.
            m = re.search(
                re.escape(sys_name) + r"\s*" + _DASH + r"\s*",
                s[:40], flags=re.IGNORECASE,
            )
            if m:
                s = s[m.end():].strip()

    # Normalise underscores to spaces.
    s = s.replace("_", " ").strip()

    # Final pass: strip any leading server/host code still present.
    # e.g. "LVPWAPBPIT1 Administrators" → "Administrators"
    s = re.sub(r"^[A-Z]{2}[A-Z0-9]+ ", "", s).strip()

    s = s.strip(" –-")

    parts = [app_name]
    if sys_name:
        parts.append(sys_name)
    if s and s.lower() not in {app_name.lower(), (sys_name or "").lower()}:
        parts.append(s)

    return " - ".join(parts)


def _resolve_names(
    raw_docs: list[ScanRaw],
    name_cache: dict[tuple, str],
) -> list[UploadItem]:
    """Convert raw scan results to upload-ready items, prompting for ambiguous stems.

    Any filename stem that still looks date-contaminated after the three known
    stripping patterns are applied is surfaced to the user exactly once per
    unique (app, system, stem) combination — the cache persists across months
    so backfill mode prompts once regardless of how many months contain that
    file type.
    """
    result: list[UploadItem] = []

    for doc in raw_docs:
        cache_key = (doc.app_name, doc.sys_name or "", doc.raw_stem)

        if cache_key in name_cache:
            evidence_name = name_cache[cache_key]

        elif doc.was_stripped or not _is_date_contaminated(doc.cleaned_stem):
            # Clean or already handled — build name directly.
            evidence_name = _build_evidence_name(doc.app_name, doc.sys_name, doc.cleaned_stem)
            name_cache[cache_key] = evidence_name

        else:
            # Unknown date pattern — ask the user once.
            proposed = _build_evidence_name(doc.app_name, doc.sys_name, doc.cleaned_stem)
            print(yellow("\n⚠  Unrecognised date pattern in filename stem:"))
            print(f"   App:      {bold(doc.app_name)}")
            if doc.sys_name:
                print(f"   System:   {bold(doc.sys_name)}")
            print(f"   Stem:     {dim(doc.raw_stem)}")
            print(f"   Proposed: {bold(proposed)}")
            user_input = input(
                "   Press Enter to accept, or type a stable name: "
            ).strip()
            evidence_name = user_input if user_input else proposed
            name_cache[cache_key] = evidence_name

        result.append(UploadItem(
            app_name=doc.app_name,
            evidence_name=evidence_name,
            file_path=doc.file_path,
            year=doc.year,
            month=doc.month,
            cleaned_stem=doc.cleaned_stem,
        ))

    return result


# ── Folder scanner ────────────────────────────────────────────────────────────

def _stem_month(raw_stem: str) -> Optional[int]:
    """Extract the month number from a trailing MM.DD.YY pattern in a stem.

    Only inspects the trailing date format because it is unambiguous about
    which part is the month.  Returns None if no such pattern is present.
    Used to detect files whose embedded date does not match their folder month.
    """
    m = re.search(r"[-–\s](\d{1,2})\.\d{1,2}\.\d{2,4}$", raw_stem)
    if m:
        mon = int(m.group(1))
        return mon if 1 <= mon <= 12 else None
    return None


def _updated_today(updated_at: Optional[str]) -> bool:
    """True if an evidence entry's updatedAt timestamp falls on today's date.

    Used to make a same-day re-run safe: an entry that already received a
    version today (e.g. from an earlier attempt that partly failed) is left
    alone instead of being uploaded to a second time, which would otherwise
    add a redundant duplicate version for every file that already succeeded.
    """
    if not updated_at:
        return False
    return updated_at[:10] == datetime.date.today().isoformat()


def scan_folder(
    root: Path,
    year: int,
    month: int,
    *,
    verbose: bool = True,
) -> list[ScanRaw]:
    """Return raw scan results for every document belonging to the requested month.

    Handles two layouts automatically — and mixed layouts within the same app:

      Layout A:  AppName / Year / Month / files      (no system tier)
      Layout B:  AppName / SystemName / Year / Month / files

    Detection: if a child of AppName/ is a 4-digit year, it's Layout A; otherwise
    it's treated as a system name (Layout B).  Both can coexist under one app.

    verbose=True  (default, single-month mode) prints per-app diagnostics.
    verbose=False (backfill mode) suppresses per-app output for cleaner output.

    Uses not d.is_dir() instead of d.is_file() to tolerate OneDrive cloud-only
    files that report OFFLINE attribute on Windows.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    hits: list[ScanRaw] = []

    for app_dir in sorted(root.iterdir()):
        if not app_dir.is_dir() or app_dir.name.startswith("."):
            continue

        app_hits: list[ScanRaw] = []

        for child in sorted(app_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue

            if _is_year(child.name):
                # ── Layout A: AppName / Year / Month / files ───────────────
                if int(child.name) != year:
                    continue

                month_dir = _find_month_dir(child, month)
                if not month_dir:
                    if verbose:
                        print(dim(
                            f"  {app_dir.name} / {child.name} / ??? "
                            f"— no folder matching month {month}"
                        ))
                    continue

                docs = [
                    d for d in sorted(month_dir.iterdir())
                    if not d.is_dir() and not d.name.startswith(".")
                ]
                if not docs:
                    if verbose:
                        print(dim(
                            f"  {app_dir.name} / {child.name} / {month_dir.name} "
                            f"— folder exists but contains no files"
                        ))
                    continue

                for doc in docs:
                    # Skip files whose embedded date month ≠ folder month.
                    doc_mon = _stem_month(doc.stem)
                    if doc_mon is not None and doc_mon != month:
                        print(yellow(
                            f"  SKIP  {app_dir.name}/{child.name}/{month_dir.name}/{doc.name}"
                            f"  (filename month {doc_mon:02d} ≠ folder month {month:02d})"
                        ))
                        continue
                    cleaned, stripped = _clean_stem(doc.stem, app_dir.name)
                    app_hits.append(ScanRaw(
                        app_name=app_dir.name,
                        sys_name=None,
                        raw_stem=doc.stem,
                        cleaned_stem=cleaned,
                        was_stripped=stripped,
                        file_path=doc,
                        year=year,
                        month=month,
                    ))

            else:
                # ── Layout B: AppName / SystemName / Year / Month / files ──
                sys_dir = child
                year_dir = sys_dir / str(year)

                if not year_dir.is_dir():
                    if verbose:
                        print(dim(
                            f"  {app_dir.name} / {sys_dir.name} / {year} "
                            f"— year folder not found"
                        ))
                    continue

                month_dir = _find_month_dir(year_dir, month)
                if not month_dir:
                    if verbose:
                        print(dim(
                            f"  {app_dir.name} / {sys_dir.name} / {year} / ??? "
                            f"— no folder matching month {month}"
                        ))
                    continue

                docs = [
                    d for d in sorted(month_dir.iterdir())
                    if not d.is_dir() and not d.name.startswith(".")
                ]
                if not docs:
                    if verbose:
                        print(dim(
                            f"  {app_dir.name} / {sys_dir.name} / {year} / {month_dir.name} "
                            f"— folder exists but contains no files"
                        ))
                    continue

                for doc in docs:
                    # Skip files whose embedded date month ≠ folder month.
                    doc_mon = _stem_month(doc.stem)
                    if doc_mon is not None and doc_mon != month:
                        print(yellow(
                            f"  SKIP  {app_dir.name}/{sys_dir.name}/{year}/{month_dir.name}/{doc.name}"
                            f"  (filename month {doc_mon:02d} ≠ folder month {month:02d})"
                        ))
                        continue
                    cleaned, stripped = _clean_stem(doc.stem, app_dir.name)
                    app_hits.append(ScanRaw(
                        app_name=app_dir.name,
                        sys_name=sys_dir.name,
                        raw_stem=doc.stem,
                        cleaned_stem=cleaned,
                        was_stripped=stripped,
                        file_path=doc,
                        year=year,
                        month=month,
                    ))

        if app_hits and verbose:
            print(green(f"  {app_dir.name} → {len(app_hits)} file(s) queued"))

        hits.extend(app_hits)

    return hits


# ── Month iteration ───────────────────────────────────────────────────────────

def iter_months(sy: int, sm: int, ey: int, em: int):
    """Yield (year, month) pairs from (sy, sm) to (ey, em) inclusive."""
    year, month = sy, sm
    while (year, month) <= (ey, em):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1


# ── Deduplication ─────────────────────────────────────────────────────────────

def _dedup_items(
    items: list[UploadItem],
) -> tuple[list[UploadItem], int]:
    """Keep only the first item per (evidence_name, year, month) group.

    Files within a month folder are already sorted alphabetically by scan_folder,
    so the first occurrence is the earliest-dated file — which is what we want
    for months that contain multiple snapshots of the same report (e.g. Stadium
    weekly SystemUsers exports).

    Returns (deduped_list, n_skipped).
    """
    seen: set[tuple] = set()
    result: list[UploadItem] = []
    skipped = 0
    for item in items:
        key = (item.evidence_name, item.year, item.month)
        if key in seen:
            skipped += 1
        else:
            seen.add(key)
            result.append(item)
    return result, skipped


# ── Dry-run output ─────────────────────────────────────────────────────────────

def _print_dry_run(items: list[UploadItem], n_skipped: int) -> None:
    """Print the full proposed evidence mapping; no API calls are made."""
    print(f"\n{bold('─── Dry-run: proposed mapping ───────────────────')}\n")
    current_month: Optional[str] = None
    for item in items:
        label = f"{item.year}-{item.month:02d}"
        if label != current_month:
            current_month = label
            print(f"  {cyan(f'[ {label} ]')}")
        try:
            rel: Path = item.file_path.relative_to(Path.cwd())
        except ValueError:
            rel = item.file_path
        print(f"    {green('→')} {bold(item.evidence_name)}")
        print(f"       {dim(str(rel))}")

    print(f"\n{bold('─── Evidence holders per app ────────────────────')}\n")
    by_app: dict[str, set[str]] = {}
    for item in items:
        by_app.setdefault(item.app_name, set()).add(item.evidence_name)
    for app in sorted(by_app):
        print(f"  {bold(app)}")
        for name in sorted(by_app[app]):
            print(f"    {green('●')} {name}")
        print()

    print(dim(f"  {len(items)} file(s) would be uploaded  |  {n_skipped} duplicate(s) skipped"))
    print(f"\n{yellow('Dry run complete — no data was uploaded.')}")
    print(f"  Re-run without {bold('--dry-run')} to upload.\n")


# ── Interactive helpers ───────────────────────────────────────────────────────

def ask(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{dim(default)}]" if default else ""
    prompt = f"{bold(label)}{suffix}: "
    value  = (getpass.getpass(prompt) if secret else input(prompt)).strip()
    return value or default


def ask_month(label: str = "Month to process") -> tuple[int, int]:
    """Prompt for a month; accepts YYYY-MM or YYYY-MonthName formats."""
    today   = datetime.date.today()
    default = f"{today.year}-{today.month:02d}"
    print(f"\n{bold(label)}  (YYYY-MM or YYYY-MonthName, default = current month)")
    print(f"  {dim('e.g.  2026-04  |  2026-Apr  |  2026-April')}")
    raw = input("  → ").strip() or default

    # Strict numeric YYYY-MM.
    try:
        dt = datetime.datetime.strptime(raw, "%Y-%m")
        return dt.year, dt.month
    except ValueError:
        pass

    # YYYY-<anything in MONTH_MAP>.
    parts = raw.split("-", 1)
    if len(parts) == 2 and parts[0].isdigit():
        month = _parse_month(parts[1])
        year  = int(parts[0])
        if month and 2000 <= year <= 2100:
            return year, month

    print(yellow(f"  Could not parse '{raw}', falling back to current month."))
    return today.year, today.month


# ── Entry point ───────────────────────────────────────────────────────────────

BANNER = f"""
{cyan('╔══════════════════════════════════════════════╗')}
{cyan('║')}   {bold('Drata Evidence Uploader')}  {dim('v2.2')}              {cyan('║')}
{cyan('║')}   Automates monthly evidence → {bold('UAR controls')}  {cyan('║')}
{cyan('╚══════════════════════════════════════════════╝')}
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drata Evidence Uploader v2.2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Single month:  python upload_evidence.py\n"
            "Backfill:      python upload_evidence.py --backfill\n"
            "Dry run:       python upload_evidence.py --backfill --dry-run"
        ),
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Upload evidence for a configurable date range (one-time historical backfill)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Scan and show the proposed evidence mapping without uploading anything",
    )
    args = parser.parse_args()

    print(BANNER)

    # ── Config ────────────────────────────────────────────────────────────────
    print(bold("─── Configuration ───────────────────────────────\n"))
    root_path = Path.cwd()
    if args.dry_run:
        api_token = workspace_id = ""
        print(dim("  Dry-run mode — no credentials required, nothing will be uploaded.\n"))
    else:
        api_token    = ask("Drata API token", secret=True)
        workspace_id = ask("Workspace ID")

    # ── Date range ────────────────────────────────────────────────────────────
    if args.backfill:
        print(f"\n{bold('─── Backfill range ──────────────────────────────')}")
        print(dim("  Both months inclusive.  Enter in YYYY-MM format."))
        start_year, start_month = ask_month("Start month")
        end_year,   end_month   = ask_month("End month")
        month_list = list(iter_months(start_year, start_month, end_year, end_month))
        print(
            f"\n  Range : {bold(f'{start_year}-{start_month:02d}')} → "
            f"{bold(f'{end_year}-{end_month:02d}')}  "
            f"{dim(f'({len(month_list)} months)')}"
        )
        print(f"  Root  : {dim(str(root_path))}")
    else:
        year, month = ask_month()
        month_list  = [(year, month)]
        print(f"\n  Period : {bold(f'{year}-{month:02d}')}")
        print(f"  Root   : {dim(str(root_path))}")

    # ── Scan ──────────────────────────────────────────────────────────────────
    print(f"\n{bold('─── Scanning ────────────────────────────────────')}\n")
    all_raw: list[ScanRaw] = []

    for (y, m) in month_list:
        try:
            found = scan_folder(root_path, y, m, verbose=not args.backfill)
        except FileNotFoundError as exc:
            print(red(f"Error: {exc}"))
            sys.exit(1)

        if args.backfill and found:
            print(dim(f"  {y}-{m:02d}: {len(found)} file(s)"))

        all_raw.extend(found)

    if not all_raw:
        if args.backfill:
            print(yellow("No documents found in the specified range.  Nothing to upload."))
        else:
            y, m = month_list[0]
            print(yellow(f"No documents found for {y}-{m:02d}.  Nothing to upload."))
        sys.exit(0)

    # ── Name resolution ───────────────────────────────────────────────────────
    # All interactive prompts for ambiguous stems happen here, before any upload.
    if args.backfill:
        print(f"\n{bold('─── Resolving evidence names ────────────────────')}\n")
    name_cache: dict[tuple, str] = {}
    documents_raw = _resolve_names(all_raw, name_cache)

    # Deduplicate: for each (evidence_name, year, month) keep the first file
    # alphabetically (= oldest date when filenames are date-suffixed).
    documents, n_skipped = _dedup_items(documents_raw)

    # ── Dry-run exit ──────────────────────────────────────────────────────────
    if args.dry_run:
        _print_dry_run(documents, n_skipped)
        sys.exit(0)

    # ── Upload plan ───────────────────────────────────────────────────────────
    print(f"\n{bold('─── Upload plan ─────────────────────────────────')}\n")

    if args.backfill:
        for (y, m), grp in groupby(documents, key=lambda d: (d.year, d.month)):
            items = list(grp)
            print(f"  {cyan(f'{y}-{m:02d}')}  {dim(f'{len(items)} file(s)')}")
            for item in items:
                print(
                    f"    {green('●')} {bold(item.evidence_name)}  "
                    f"{dim(f'→ {_control_code(item.app_name)}')}"
                )
    else:
        for item in documents:
            print(
                f"  {green('●')} {bold(item.evidence_name)}  "
                f"{dim(f'→ {_control_code(item.app_name)}')}"
            )
            print(f"      {dim(item.file_path.name)}")

    total = len(documents)
    if n_skipped:
        print(dim(f"  ({n_skipped} duplicate(s) skipped — same holder, same month, kept oldest)"))
    if args.backfill:
        months_with_files = len(set((d.year, d.month) for d in documents))
        prompt_label = f"Upload {total} document(s) across {months_with_files} month(s)?"
    else:
        prompt_label = f"Upload these {total} document(s)?"

    confirm = input(f"\n{bold(prompt_label)} [Y/n]: ").strip().lower()
    if confirm not in ("y", ""):
        print("Aborted.")
        sys.exit(0)

    # ── Bootstrap API client ──────────────────────────────────────────────────
    client = DrataClient(api_token, workspace_id)

    print(f"\n{bold('─── Resolving IDs ───────────────────────────────')}\n")

    # Owner lookup — email → numeric ID required by the API.
    owner_email = ask("Owner email (evidence will be assigned to this person)")
    print(f"  Looking up {bold(owner_email)}... ", end="", flush=True)
    try:
        owner_id = client.find_user_by_email(owner_email)
    except DrataError as exc:
        print(red("FAILED"))
        print(red(f"  {exc}"))
        owner_id = None

    if not owner_id:
        print(yellow("NOT FOUND"))
        print(yellow(f"  '{owner_email}' was not found in Drata users."))
        print(dim("  Check that the address matches exactly what's in Drata → Settings → People."))
        skip = input(f"  {bold('Continue without assigning an owner?')} [y/N]: ").strip().lower()
        if skip != "y":
            print("Aborted.")
            sys.exit(1)
        print(yellow("  Proceeding without owner — you can assign one manually in Drata."))
    else:
        print(green(f"ID = {owner_id}"))

    # ── Upload loop ───────────────────────────────────────────────────────────
    print(f"\n{bold('─── Uploading ───────────────────────────────────')}\n")
    created = updated = failed = skipped_already_done = 0

    # Cache control IDs so each app only hits the API once per run.
    control_cache: dict[str, Optional[int]] = {}

    # Cache evidence entry IDs discovered or created this run.
    # Once we know an entry exists we skip the find_evidence GET on subsequent
    # months and go straight to update — cuts API calls ~50% in backfill mode.
    entry_cache: dict[str, int] = {}

    current_label: Optional[str] = None

    for item in documents:
        # Month header in backfill mode.
        if args.backfill:
            label = f"{item.year}-{item.month:02d}"
            if label != current_label:
                current_label = label
                print(f"  {cyan('[' + label + ']')}")

        # Dates vary per month — compute fresh for every item.
        last_day     = calendar.monthrange(item.year, item.month)[1]
        filed_at     = f"{item.year}-{item.month:02d}-{last_day}"
        exp_year     = item.year + 1 if item.month == 12 else item.year
        exp_month    = 1             if item.month == 12 else item.month + 1
        renewal_date = f"{exp_year}-{exp_month:02d}-01"

        code = _control_code(item.app_name)
        print(f"  {bold(item.evidence_name)}  {dim(f'[{code}]')}")

        # Control lookup (cached per unique app name).
        if code not in control_cache:
            print(f"  {dim('→')} Looking up control {code}... ", end="", flush=True)
            try:
                control_cache[code] = client.find_control_id(code)
            except DrataError as exc:
                print(red("FAILED"))
                print(red(f"  API error: {exc}"))
                control_cache[code] = None

            if control_cache[code]:
                print(green(f"ID = {control_cache[code]}"))
            else:
                print(red("NOT FOUND"))
                print(red(f"  No control '{code}' in workspace — skipping this app's files."))

        control_id = control_cache[code]
        if not control_id:
            failed += 1
            print()
            continue

        try:
            if item.evidence_name in entry_cache:
                # Entry confirmed to exist this run — skip the GET.
                ev_id = entry_cache[item.evidence_name]
                print(
                    f"  {dim('→')} Uploading new version (ID {ev_id})... ",
                    end="", flush=True,
                )
                client.update_evidence(ev_id, item.file_path, filed_at, renewal_date, owner_id)
                print(green("UPDATED"))
                updated += 1

            else:
                existing = client.find_evidence(item.evidence_name)

                if not existing and item.cleaned_stem:
                    # Fallback: entries created by older script versions used raw
                    # filenames as evidence names. The cleaned stem still appears
                    # as a substring in those old names, so we can find them.
                    existing = client.find_evidence_by_stem(
                        item.cleaned_stem, _control_code(item.app_name)
                    )
                    if existing:
                        print(dim(
                            f"  (matched existing entry '{existing['name']}' by stem)"
                        ))

                if existing:
                    ev_id = existing["id"]
                    entry_cache[item.evidence_name] = ev_id

                    if _updated_today(existing.get("updatedAt")):
                        print(yellow(
                            f"  {dim('→')} Entry (ID {ev_id}) already has a version from "
                            f"today — skipping to avoid a duplicate."
                        ))
                        skipped_already_done += 1
                    else:
                        print(
                            f"  {dim('→')} Found entry (ID {ev_id}) — uploading new version... ",
                            end="", flush=True,
                        )
                        client.update_evidence(ev_id, item.file_path, filed_at, renewal_date, owner_id)
                        print(green("UPDATED"))
                        updated += 1

                else:
                    print(f"  {dim('→')} No existing entry — creating... ", end="", flush=True)
                    result = client.create_evidence(
                        item.evidence_name, item.file_path,
                        filed_at, renewal_date, [control_id], owner_id,
                    )
                    ev_id = result["id"]
                    entry_cache[item.evidence_name] = ev_id
                    print(green(f"CREATED (ID {ev_id})"))
                    created += 1

        except DrataError as exc:
            print(red("FAILED"))
            print(red(f"  API error: {exc}"))
            failed += 1
        except requests.Timeout:
            print(red("TIMED OUT"))
            print(red("  The request took too long.  Check your connection and try again."))
            failed += 1
        except OSError as exc:
            print(red("FAILED"))
            print(red(f"  File error: {exc}"))
            failed += 1

        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(bold("─── Summary ─────────────────────────────────────\n"))
    if args.backfill:
        months_processed = len(set((d.year, d.month) for d in documents))
        print(f"  {dim('Months processed:')} {months_processed}")
    print(f"  {green('Created:')} {created}")
    print(f"  {cyan('Updated:')} {updated}")
    if skipped_already_done:
        print(f"  {dim('Skipped (already done today):')} {skipped_already_done}")
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

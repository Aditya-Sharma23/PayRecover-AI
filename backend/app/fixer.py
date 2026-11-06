"""AI writes the fix: Gemini turns a finding into a code change, and PayRecover proves the change works.

For each hole PayRecover finds (XSS, missing security headers, a store that trusts browser prices,
marks orders paid without capturing, lets a cart be paid twice, or only holds a payment), Gemini
reads the store's code and returns small find-and-replace edits. The AI only writes; PayRecover decides:

  1. every edit must match the code exactly once, and the result must still be valid Python;
  2. the change may not bring in anything that runs programs, opens files or sockets, or evaluates code;
  3. for a demo store, the fixed copy is run with the bug's switch on, and the same check PayRecover
     used to find the hole is run again. The original is run too, to show the bug was really there.

A fix that fails 1, 2 or 3 goes back to the AI once, with the exact reason (an edit that did not match,
a missing import, the check that still fails), so it can correct itself as a developer would.

Only a fix that passes 3 is marked verified. PayRecover never changes a store's files: it shows the diff
for the merchant to apply. Verification runs in a separate process with no keys, no network and a time
limit (app/fix_verifier.py). For a store that is not a demo store, the merchant pastes the code; that
fix is checked by 1 and 2 only and is clearly marked as not run.
"""

import ast
import asyncio
import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app import ai
from app.schemas import CheckStatus, FixEdit, FixProposal, FixRequest, PaymentCheck

ROOT = Path(__file__).resolve().parents[2]
DEMO_SOURCES = {"vulnerable": "vuln-store", "risky": "vuln-store", "main": "main-store"}
VERIFIER = Path(__file__).resolve().parent / "fix_verifier.py"
MAX_EDITS = 8
MAX_REPLACED_CHARS = 8000
VERIFY_SECONDS = 40
# How many times the AI may write the fix: once, plus one try with the reason the first one failed.
ATTEMPTS = 2
# What each finding means in the code, which switch turns it on in the demo store, and which check proves it.
ISSUES = {
    "xss": ("The store page puts the search text from ?q= into the HTML without escaping it (reflected XSS).",
            "", "xss"),
    "security_headers": ("The store sends no Content-Security-Policy, X-Frame-Options or X-Content-Type-Options "
                         "header on its pages.", "", "headers"),
    "price_tampering": ("When pricing a cart the store uses the unit_price the browser sends instead of its own "
                        "catalog price.", "trust_client_price", "price"),
    "never_captured": ("The capture endpoint can mark an order PAID without asking PayPal to capture the money.",
                       "confirm_without_capture", "capture"),
    "duplicate_charge": ("A cart that is already paid can be paid again, so one cart is charged twice.",
                         "allow_double_payment", "double"),
    "hold_expiring": ("Orders can be created with intent AUTHORIZE and marked PAID after PayPal only holds the "
                      "money; nothing ever captures it, so the hold expires.", "authorize_and_forget", "hold"),
}
ISSUES["wrong_amount"] = ISSUES["price_tampering"]
ISSUES["fake_paid"] = ISSUES["never_captured"]
ISSUES["hold_expired"] = ISSUES["hold_expiring"]
# Names a fix may not bring in, and the only modules it may newly import.
FORBIDDEN_NAMES = {"exec", "eval", "compile", "__import__", "globals", "locals", "getattr", "setattr", "subprocess",
                   "system", "popen", "spawn", "socket", "rmtree", "unlink", "rmdir", "remove", "open", "urlopen",
                   "requests", "ctypes", "pickle", "marshal", "importlib", "environ"}
ALLOWED_IMPORTS = {"html", "re", "json", "secrets", "hmac", "hashlib", "decimal", "datetime", "typing", "markupsafe",
                   "fastapi", "fastapi.responses", "starlette", "starlette.middleware.base", "starlette.responses"}


class FixRejected(ValueError):
    """The AI's fix cannot be used: an edit does not match, or the result is unsafe or not valid code."""


def _check(name: str, ok: bool | None, message: str) -> PaymentCheck:
    status = CheckStatus.SKIPPED if ok is None else CheckStatus.PASSED if ok else CheckStatus.FAILED
    return PaymentCheck(name=name, status=status, message=message, evidence={})


def source_for(request: FixRequest) -> tuple[str, str, Path | None]:
    """The code to fix: a demo store's own file, or the code the merchant pasted. Returns (filename, text, folder)."""
    if request.code:
        return request.filename or "store_code.py", request.code, None
    folder = DEMO_SOURCES.get(request.store)
    if folder is None:
        raise FixRejected("Paste your store's code (the part that handles this) to get a fix for a connected store.")
    path = Path(os.environ.get("TESTPILOT_STORE_SOURCE_DIR") or ROOT) / folder / "store_app.py"
    if not path.is_file():
        raise FixRejected("The demo store's code is not available on this server.")
    return f"{folder}/store_app.py", path.read_text(encoding="utf-8"), path.parent


def apply_edits(source: str, edits: list[FixEdit]) -> str:
    if not edits:
        raise FixRejected("The AI proposed no change.")
    if len(edits) > MAX_EDITS or sum(len(edit.replace) for edit in edits) > MAX_REPLACED_CHARS:
        raise FixRejected("The proposed change is larger than a focused fix should be.")
    text = source
    for number, edit in enumerate(edits, 1):
        found = text.count(edit.find) if edit.find else 0
        if found != 1:
            raise FixRejected(f"Edit {number} does not match the code exactly once (found {found} times).")
        text = text.replace(edit.find, edit.replace, 1)
    return text


def _names(tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _imports(tree: ast.AST) -> set[str]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def safety_problems(original: str, patched: str) -> list[str]:
    """What makes a Python change unsafe to run: new dangerous names, or new imports outside the allowed set."""
    before, after = ast.parse(original), ast.parse(patched)
    problems = [f"uses {name}" for name in sorted((_names(after) - _names(before)) & FORBIDDEN_NAMES)]
    problems += [f"imports {module}" for module in sorted(_imports(after) - _imports(before)) if module not in ALLOWED_IMPORTS]
    return problems


def _verify_once(folder: Path, patched: str | None, check: str, bug: str) -> dict:
    """Run one check against a copy of the store (fixed, or the original when patched is None)."""
    with tempfile.TemporaryDirectory(prefix="payrecover-fix-") as temp:
        work = Path(temp) / "store"
        shutil.copytree(folder, work, ignore=shutil.ignore_patterns("data", "tests", ".env", "__pycache__"))
        if patched is not None:
            (work / "store_app.py").write_text(patched, encoding="utf-8")
        shutil.copy(VERIFIER, work / "fix_verifier.py")
        # A clean environment: no PayPal keys, AI keys or tokens reach the code being tested.
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if key in os.environ}
        env.update(VULN_STORE_PROFILE="vulnerable", VULN_STORE_DATA_FILE=str(work / "data.json"),
                   STORE_ORDERS_FILE=str(work / "orders.json"), PYTHONDONTWRITEBYTECODE="1")
        try:
            done = subprocess.run([sys.executable, "-I", "fix_verifier.py", check, bug], cwd=work, env=env,
                                  capture_output=True, text=True, timeout=VERIFY_SECONDS)
        except subprocess.TimeoutExpired:
            return {"ok": False, "message": f"The store did not finish the check within {VERIFY_SECONDS} seconds."}
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "message": "The store could not run: " + (done.stderr.strip().splitlines() or ["no output"])[-1][:300]}


def _prompt(request: FixRequest, filename: str, source: str, demo: bool) -> str:
    meaning, bug, _ = ISSUES[request.issue]
    switch = (f'In this demo store the bug is behind a switch: code checks whether "{bug}" is in active_bugs. Your '
              "fix must make the store safe even with that switch on: remove or neutralise the faulty branch, do "
              "not turn the switch off somewhere else.") if demo and bug else ""
    return f"""Return only JSON: {{"summary": "two plain sentences a shop owner understands: what was wrong and what the change does", "edits": [{{"find": "exact text copied from the file", "replace": "the new text"}}]}}
You are fixing a security or payment bug in an online store's code.
The problem: {meaning}
What PayRecover found: {(request.detail or "")[:600]}
{switch}
Rules: make the smallest change that fixes this problem and nothing else. Each "find" must be copied exactly from the
file (same spaces and line breaks) and must appear in it only once; keep "find" short but unique. Keep the existing
style. Do not add new libraries; html and re from the standard library are fine. Do not touch payment amounts other
than the fix needs. At most {MAX_EDITS} edits.
File {filename}:
{source[:30000]}"""


def _checked(source: str, filename: str, value: dict) -> tuple[list[FixEdit], str, str, list[PaymentCheck]]:
    """Steps 1 and 2: the edits apply, the result is valid Python, and nothing unsafe was added. Raises FixRejected."""
    edits = [FixEdit(find=str(item.get("find", "")), replace=str(item.get("replace", "")))
             for item in (value.get("edits") or []) if isinstance(item, dict)]
    summary = str(value.get("summary") or "").strip()[:600] or "The AI did not explain the change."
    patched = apply_edits(source, edits)
    checks = [_check("edits_apply", True, f"All {len(edits)} edit(s) match the code exactly once.")]
    if filename.endswith(".py"):
        try:
            problems = safety_problems(source, patched)
        except SyntaxError as exc:
            raise FixRejected(f"The fixed code is not valid Python (line {exc.lineno}: {exc.msg}).") from exc
        if problems:
            raise FixRejected("The fix was refused because it " + ", ".join(problems) + ".")
        checks.append(_check("valid_code", True, "The fixed file is valid Python and adds nothing unsafe."))
    else:
        checks.append(_check("valid_code", None, "Not Python, so PayRecover did not parse it; review it before use."))
    return edits, summary, patched, checks


async def propose(request: FixRequest) -> FixProposal:
    """Ask the AI for a fix, check it, and run it. A fix that fails goes back to the AI with the reason, once."""
    filename, source, folder = source_for(request)
    demo = folder is not None and request.code is None and filename.endswith(".py")
    _, bug, check = ISSUES[request.issue]
    before = await asyncio.to_thread(_verify_once, folder, None, check, bug) if demo else None
    prompt = _prompt(request, filename, source, folder is not None and request.code is None)
    history: list[PaymentCheck] = []
    for attempt in range(1, ATTEMPTS + 1):
        value = await ai.ask_json(prompt)
        try:
            edits, summary, patched, checks = _checked(source, filename, value)
        except FixRejected as exc:
            if attempt == ATTEMPTS:
                raise
            reason = str(exc)
        else:
            if not demo:
                checks.append(_check("fix_works", None, "Not run: PayRecover can only run the demo stores' code. "
                                                        "Test this change in your store before you deploy it."))
                break
            after = await asyncio.to_thread(_verify_once, folder, patched, check, bug)
            checks.append(_check("bug_reproduced", not before["ok"],
                                 f"Before the fix{' (switch ' + bug + ' on)' if bug else ''}: {before['message']}"))
            checks.append(_check("fix_works", after["ok"], f"After the fix: {after['message']}"))
            if after["ok"] or attempt == ATTEMPTS:
                break
            reason = f"PayRecover ran the fixed store and it failed: {after['message']}"
        # The AI sees why its fix failed and tries again, as a developer would after a failing test.
        history.append(_check(f"attempt_{attempt}", False, f"Attempt {attempt} did not work: {reason} The AI was told "
                                                           "why and wrote it again."))
        prompt = (f"{prompt}\n\nYour previous answer did not work: {reason}\nFix that and return the complete JSON again. "
                  "Remember to add any import the change needs as its own edit.")
    diff = "".join(difflib.unified_diff(source.splitlines(True), patched.splitlines(True),
                                        fromfile=f"a/{filename}", tofile=f"b/{filename}", n=3))
    verified = demo and not before["ok"] and checks[-1].status == CheckStatus.PASSED
    return FixProposal(issue=request.issue, filename=filename, summary=summary, edits=edits, diff=diff,
                       checks=history + checks, verified=verified, source=ai.answer_source())

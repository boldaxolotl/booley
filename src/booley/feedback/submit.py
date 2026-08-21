"""The consent-gated route from a redacted report to its destination.

Two destinations, chosen by ``[feedback] mode``: a public GitHub issue (``ask``)
or a private email to the maintainer (``email``). The consent machinery is the
same for both — the text is the text, and disclosing it to a stranger is
disclosing it — but what the user is agreeing to differs, so the preview says
which one it is.

Every guard here exists because the destination is outside the user's machine
and the disclosure is irreversible:

- **Host only.** Setup's Steps 2-5 and most day-to-day work run inside the
  Session Runtime, whose egress proxy allowlists model APIs and nothing else.
  Rather than punch github.com through it — widening the sandbox's egress for
  every project, forever — this refuses in-container and tells the user to run
  it on the host, where the findings log is available. The email route
  is host-only for the same reason plus a simpler one: the mail client is there.
- **Explicit approval of the *exact* text.** ``--yes`` alone is not enough: it
  must carry the confirmation token that ``preview`` printed, and the token is a
  digest of the body. An agent cannot fire ``--yes`` at text the user was never
  shown, and a changed findings log invalidates the token when submission
  re-renders the view. (Learned the hard way: a smoke test with ``--yes`` posted
  a live issue to the public repo.)
- **Off is a real setting.** ``[feedback] mode = "off"`` makes submission refuse
  outright, and ``"file-only"`` permits only an explicit redacted export.
- **Either destination carries an identity.** A public issue carries the user's
  GitHub account name; an email carries their return address. ``preview`` says
  which, before anyone commits, because for a lot of users that alone is the
  answer.

When ``gh`` is unavailable or unauthenticated the fallback is not an error: an
explicit redacted export and a prefilled issue URL are still available.

The email route is *entirely* hand-off: Booley builds a ``mailto:`` URL and
stops. No SMTP, no credentials, no outbound socket — the user's own mail client
sends it, from their own mailbox, after they have seen it one last time. The
cost of that is that Booley cannot know whether the mail was ever sent, so the
email route never stamps findings as filed; it prints the ``booley feedback
filed`` command for the user to run once they have actually sent it.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import tomllib
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booley.harness import colors

logger = logging.getLogger(__name__)

#: Length of the confirmation token. Eight hex digits is far too short to be a
#: security boundary and does not need to be one — the only thing it has to do
#: is prove that whoever typed it had the previewed text in front of them.
TOKEN_LEN = 8

#: Booley's public repository — the only destination this module can post to.
UPSTREAM_REPO = "boldaxolotl/Booley"

#: Label for a setup run's batch. A bug hit during normal use is filed under
#: ``user-feedback`` instead (see :attr:`~booley.feedback.render.BooleyReport.label`);
#: either is dropped on retry when the repo has no such label.
DEFAULT_LABEL = "setup-feedback"
NEW_ISSUE_URL = f"https://github.com/{UPSTREAM_REPO}/issues/new"

#: Booley's maintainer intake address — the destination for ``mode = "email"``.
#: Deliberately the same pseudonymous ident the project commits under: a bug
#: report should not cost the reporter *or* the maintainer their anonymity.
INTAKE_EMAIL = "boldaxolotl@proton.me"

#: GitHub's own limit is higher, but browsers and proxies start truncating long
#: query strings well before it; past this the paste-the-file route is safer.
MAX_URL_BODY = 4000

#: Mail clients are far stingier than browsers with ``mailto:`` length — several
#: common ones silently drop the body somewhere past 2 KB, and a silently empty
#: mail is worse than an obviously abbreviated one. Past this the body is cut and
#: the mail asks the sender to attach the file, which is the better route anyway.
MAX_MAILTO_BODY = 1800

Mode = Literal["ask", "email", "file-only", "off"]
MODES: tuple[str, ...] = ("ask", "email", "file-only", "off")
DEFAULT_MODE: Mode = "ask"

#: Where an approved report goes. ``ask`` routes to GitHub, ``email`` to the
#: maintainer's inbox; the other two modes never get this far.
Route = Literal["github", "email"]


def route_for(mode: Mode) -> Route:
    """Which destination a mode submits to."""
    return "email" if mode == "email" else "github"


def read_mode(project_dir: Path) -> Mode:
    """``[feedback] mode`` — ``ask`` (default), ``email``, ``file-only``, or ``off``.

    Default is ``ask`` rather than ``off``: the offer is the entire mechanism by
    which Booley learns anything, and a single decline is cheap. Answering it
    once and writing the answer here is what keeps it from becoming a nag.
    """
    path = project_dir / "booley.toml"
    if not path.is_file():
        return DEFAULT_MODE
    try:
        with path.open("rb") as fh:
            cfg = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read %s (%s) — assuming feedback mode %r", path, e, DEFAULT_MODE)
        return DEFAULT_MODE
    section = cfg.get("feedback")
    raw = section.get("mode") if isinstance(section, dict) else None
    if raw is None:
        return DEFAULT_MODE
    if raw not in MODES:
        logger.warning(
            "[feedback] mode must be one of %s, got %r — using %r", MODES, raw, DEFAULT_MODE
        )
        return DEFAULT_MODE
    return raw  # type: ignore[return-value]


def confirmation_token(body: str) -> str:
    """A short digest of the exact text to be posted.

    Printed by ``preview``, required by ``submit``. Ties the approval to *these
    words*: change the report and the old token stops working, which is the
    difference between "the user agreed to publish something" and "the user
    agreed to publish this".
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:TOKEN_LEN]


def in_container() -> bool:
    """Are we inside the Session Runtime? (Same probe the CLIs use.)"""
    return Path("/.dockerenv").exists()


@dataclass
class GhStatus:
    """Whether ``gh`` can post for us, and why not when it can't."""

    available: bool
    authenticated: bool
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.available and self.authenticated


def check_gh() -> GhStatus:
    """Probe for a usable ``gh`` CLI. Never raises; a missing executable is normal."""
    if shutil.which("gh") is None:
        return GhStatus(False, False, "the GitHub CLI (`gh`) is not installed")
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        return GhStatus(True, False, f"`gh auth status` failed: {e}")
    if proc.returncode != 0:
        return GhStatus(True, False, "`gh` is installed but not logged in (`gh auth login`)")
    return GhStatus(True, True)


def issue_url(title: str, body: str) -> str:
    """A prefilled 'new issue' URL, truncating the body when it won't fit.

    Truncation is announced in the body itself: a silently clipped bug report is
    worse than an obviously clipped one, because nobody knows to ask for the
    rest.
    """
    if len(body) > MAX_URL_BODY:
        body = (
            body[:MAX_URL_BODY].rstrip()
            + "\n\n*(truncated — the reporter can create the full redacted view with "
            "`booley feedback export`)*"
        )
    query = urllib.parse.urlencode({"title": title, "body": body})
    return f"{NEW_ISSUE_URL}?{query}"


def mailto_url(subject: str, body: str, to: str = INTAKE_EMAIL) -> str:
    """A prefilled ``mailto:`` URL for the maintainer intake address.

    Percent-encoded rather than form-encoded: RFC 6068 gives ``+`` no special
    meaning in a ``mailto:`` query, so ``urlencode``'s default would put literal
    plus signs everywhere a space belongs.
    """
    if len(body) > MAX_MAILTO_BODY:
        body = (
            body[:MAX_MAILTO_BODY].rstrip()
            + "\n\n*(truncated to fit a mailto: link — create the full redacted view "
            "with `booley feedback export` and attach it)*"
        )
    query = urllib.parse.urlencode(
        {"subject": subject, "body": body}, quote_via=urllib.parse.quote
    )
    return f"mailto:{to}?{query}"


@dataclass
class SubmitOutcome:
    """What came of a submission attempt.

    ``url`` is what makes the log's already-filed stamp possible, so a later bug
    report on the same project does not re-publish this batch. It is empty for
    every not-posted path, all of which are legitimate outcomes.

    ``handed_off`` is the email route's answer to a question ``posted`` cannot
    express: Booley did everything it was asked to and the rest is in the user's
    mail client. Not a success (nothing was sent, nothing gets stamped as filed)
    and not a failure either — so it must not be reported as one.
    """

    posted: bool
    message: str
    url: str = ""
    handed_off: bool = False


@dataclass
class PreflightResult:
    """Whether submission may proceed, and what to tell the user either way."""

    ok: bool
    reason: str = ""
    gh: GhStatus | None = None
    route: Route = "github"


def preflight(project_dir: Path) -> PreflightResult:
    """Check mode and runtime location before showing a submittable report."""
    mode = read_mode(project_dir)
    if mode == "off":
        return PreflightResult(
            False,
            'Feedback submission is disabled for this project ([feedback] mode = "off"). '
            "The local report was still written.",
        )
    if mode == "file-only":
        return PreflightResult(
            False,
            'This project is set to [feedback] mode = "file-only": nothing is posted '
            "from here. Run `booley feedback export` only if you want a redacted file "
            "to send by hand.",
        )
    if in_container():
        return PreflightResult(
            False,
            "Submission is host-only. The Session Runtime's egress proxy allowlists model "
            "APIs only, github.com is deliberately not on it, and there is no mail client "
            "in here either. Re-run `booley feedback submit` from the host; it will render "
            "the same redacted view from the shared findings log.",
            route=route_for(mode),
        )
    # The email route needs no `gh`, and probing for one would only produce a
    # confusing "not logged in" line on a path that never touches GitHub.
    if route_for(mode) == "email":
        return PreflightResult(True, route="email")
    return PreflightResult(True, gh=check_gh())


def _disclosure(route: Route) -> list[str]:
    """The "who sees this, under whose name" paragraph — the part that decides it.

    Kept per-route rather than generic because the honest sentence is genuinely
    different: one is a public archive under a public handle, the other is one
    person's inbox under a return address. Blurring them into "it gets sent
    somewhere" would understate the first and overstate the second.
    """
    if route == "email":
        return [
            f"**If sent, this goes by email to {INTAKE_EMAIL}, not to a public tracker.**",
            "Your mail client shows the final message and sends it from your account.",
        ]
    return [
        "**If sent, this becomes a public issue carrying your GitHub account name.**",
        "You can export it, post it yourself, or send nothing.",
    ]


def preview(
    body: str,
    risks: list[str],
    hits_summary: str,
    route: Route = "github",
    finding_ids: list[str] | None = None,
    all_findings: bool = True,
) -> str:
    """The text shown to the user before they decide. Sales pitch: none.

    Leads with the ask, then the three facts that actually decide it — what was
    substituted, what could not be checked, and who ends up seeing it under whose
    name.
    """
    selection = " --all" if all_findings else ""
    if finding_ids:
        selection = " " + " ".join(finding_ids)
    lines = [
        # Not "as a bug report": the same path carries friction reports and plain
        # impressions ("this is great", "I wish it did X"), and calling those a
        # bug report is the fastest way to make someone decline a message that
        # was never a complaint.
        "Send this feedback to Booley's maintainers?",
        "",
        "Optional. Review the exact redacted text below before deciding.",
        "Preview only; no file was saved (`booley feedback export` creates one).",
        "",
        f"**Redacted:** {hits_summary}",
        "",
        "**Redaction limits:**",
        *(f"- {risk}" for risk in risks),
        "",
        *_disclosure(route),
        "",
        f"╭── exact text that would be {'sent' if route == 'email' else 'posted'} ──",
        "",
        colors.accent(body.rstrip()),
        "",
        "╰── end of exact text ──",
        "",
        "After the user approves this exact text:",
        "",
        f"    booley feedback submit{selection} --yes --confirm {confirmation_token(body)}",
        "",
        "The token changes if the report changes.",
    ]
    return "\n".join(lines)


def _consent_refusal(body_path: Path, *, approved: bool, confirm: str) -> str | None:
    """Why this must not be published, or ``None`` if consent checks out.

    Split out from :func:`submit` so the consent rules are one small readable
    unit — they are the part of this module that must never quietly change.
    """
    if not approved:
        return "Not submitted: no explicit approval was given (`--yes` is required)."
    if not body_path.is_file():
        return f"Not submitted: {body_path} does not exist — render the report first."

    body = body_path.read_text(encoding="utf-8")
    if confirm.strip().lower() == confirmation_token(body):
        return None

    # The correct token is deliberately NOT echoed here. Printing it would let a
    # caller scrape it straight out of the refusal and re-fire without ever
    # putting the text in front of a human — which is the entire thing this gate
    # exists to prevent. The only place the token appears is the preview, next to
    # the words being published.
    problem = (
        "no --confirm token was given."
        if not confirm.strip()
        else "that token does not match this report."
    )
    return (
        f"Not submitted: {problem}\n"
        "Run `booley feedback preview`, show the user the exact text it prints, and\n"
        "pass the token printed at the end of it. Re-rendering the report changes the\n"
        "token, so an approval only ever covers the text it was given for."
    )


def submit(
    title: str,
    body_path: Path,
    project_dir: Path,
    *,
    approved: bool,
    confirm: str = "",
    dry_run: bool = False,
    repo: str = UPSTREAM_REPO,
    label: str = DEFAULT_LABEL,
) -> SubmitOutcome:
    """Send the report to wherever ``[feedback] mode`` points — issue or inbox.

    Never re-renders or re-redacts: what is on disk is what gets sent, so the
    text the token was computed over is the text that goes out.

    Args:
        approved: The caller asserts a human agreed to send it.
        confirm: The token ``preview`` printed for this exact body.
        dry_run: Report what would be sent and stop. The way to exercise this
            path without publishing — the alternative is finding out live.
        repo: Destination repository (GitHub route only).
        label: GitHub label to file under, dropped on retry if the repo has no
            such label. Distinguishes a setup run's batch from a bug hit during
            normal use, which are triaged differently. Ignored by the email
            route, where the subject line carries the same signal.
    """
    refusal = _consent_refusal(body_path, approved=approved, confirm=confirm)
    if refusal is not None:
        return SubmitOutcome(False, refusal)

    body = body_path.read_text(encoding="utf-8")
    check = preflight(project_dir)
    if not check.ok:
        return SubmitOutcome(False, check.reason)

    if dry_run:
        return SubmitOutcome(False, _dry_run_message(check.route, title, body, repo, label))
    if check.route == "email":
        return _hand_off_by_email(title, body)

    gh = check.gh or check_gh()
    if not gh.usable:
        return SubmitOutcome(
            False,
            f"Cannot post automatically — {gh.detail}.\n"
            f"Open this prefilled issue in a browser instead:\n\n{issue_url(title, body)}\n\n"
            "Or run `booley feedback export` and attach that file to a new issue at "
            f"https://github.com/{repo}/issues/new",
        )
    return _post_via_gh(title, body_path, repo, label)


def _dry_run_message(route: Route, title: str, body: str, repo: str, label: str) -> str:
    """What ``--dry-run`` says instead of doing it, per route."""
    lines = len(body.splitlines())
    if route == "email":
        return (
            f"[dry-run] Would hand off a mail to {INTAKE_EMAIL}:\n"
            f"  subject: {title}\n"
            f"  body:    transient redacted view ({lines} lines)\n"
            "No mail client was opened."
        )
    return (
        f"[dry-run] Would file an issue on {repo}:\n"
        f"  title: {title}\n"
        f"  label: {label}\n"
        f"  body:  transient redacted view ({lines} lines)\n"
        "Nothing was posted."
    )


def _hand_off_by_email(title: str, body: str) -> SubmitOutcome:
    """Build the ``mailto:`` link and stop. Nothing is sent from here.

    Deliberately not ``webbrowser.open()``: on a headless host that either does
    nothing or launches something unexpected, and on a desktop it pops a compose
    window the user never asked for. Printing the link keeps the last action a
    human one — which is also the only reason this route can skip an SMTP
    password.
    """
    truncated = len(body) > MAX_MAILTO_BODY
    lines = [
        f"Ready to send to {INTAKE_EMAIL}. Booley does not send it — you do.",
        "",
        "Open this in your mail client (most terminals make it clickable):",
        "",
        mailto_url(title, body),
        "",
        f"Or compose it by hand: subject `{title}`. Run `booley feedback export`",
        "if you want the full redacted body as a file.",
    ]
    if truncated:
        lines += [
            "",
            f"The link's body is truncated at {MAX_MAILTO_BODY} characters because mail "
            "clients drop long ones — run `booley feedback export` and attach or paste it.",
        ]
    return SubmitOutcome(False, "\n".join(lines), handed_off=True)


def _post_via_gh(title: str, body_path: Path, repo: str, label: str) -> SubmitOutcome:
    """Shell out to ``gh issue create``. Only reached once consent has passed."""
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body-file",
                str(body_path),
                "--label",
                label,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return SubmitOutcome(
            False,
            f"`gh issue create` could not run: {e}\n"
            "Run `booley feedback export` if you want a file to post by hand.",
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        # A missing label must not lose the report — retry once without it.
        if "label" in detail.lower():
            return _submit_without_label(title, body_path, repo, detail)
        return SubmitOutcome(
            False,
            f"`gh issue create` failed: {detail}\n"
            "Run `booley feedback export` to create a file, or post it by hand at "
            f"https://github.com/{repo}/issues/new",
        )
    url = proc.stdout.strip()
    return SubmitOutcome(True, f"Filed: {url}", url=url)


def _submit_without_label(
    title: str, body_path: Path, repo: str, first_error: str
) -> SubmitOutcome:
    """Retry without ``--label``, for a repo that has no such label."""
    try:
        proc = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body-file",
                str(body_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:  # pragma: no cover - defensive
        return SubmitOutcome(False, f"`gh issue create` could not run: {e}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return SubmitOutcome(False, f"`gh issue create` failed: {detail or first_error}")
    url = proc.stdout.strip()
    return SubmitOutcome(True, f"Filed (without label): {url}", url=url)

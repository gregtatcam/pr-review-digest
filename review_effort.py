"""Estimate how much work a PR is to review, from 1 (easiest) to 5 (hardest).

The estimate is a guess made from the list of changed files, without reading
the diff. Each file's changed lines are weighted by what kind of file it is,
the weighted lines are added up into "effective lines", and those are mapped
to the score:

  - Code counts in full, tests at 30%, and docs, build, and CI files at 10%.
  - A deleted line counts half of an added one, and a deleted file hardly
    at all.
  - Each code file adds a little for the context it takes to read it, so
    the same lines spread over many files score higher.
  - Code under one of the REVIEW_HARD_PATHS counts 1.5 times.
  - Many code files with a few lines each, as many added as deleted, look
    like a mechanical change (a rename, say) and count 40%.
  - A PR with next to no description counts 15% more.
  - For a reviewer who reviewed the PR and it was updated since, the
    estimate is halved; one who approved the current commit, or whose
    latest review is of the current commit, has nothing new to read.

GitHub counts only the changed lines of a renamed or moved file, so those
need no special handling.

Environment:
  REVIEW_HARD_PATHS  Optional, comma separated. Code files whose path
                     contains one of these (ignoring case) are slower to
                     review, e.g. "src/app/tx,ledger,Number".
"""

import os
import re
from statistics import median

# Effective lines up to which a PR scores 1, 2, 3, and 4; above, 5.
LEVELS = (60, 200, 500, 1200)

TEST_WEIGHT = 0.3
OTHER_WEIGHT = 0.1
HARD_WEIGHT = 1.5
DELETED_LINE = 0.5       # a deleted line, relative to an added one
DELETED_FILE = 0.05      # a line of a file that was deleted outright
FILE_OVERHEAD = 5        # effective lines for each code file touched
MECHANICAL_WEIGHT = 0.4
THIN_DESCRIPTION = 1.15
REREVIEW = 0.5
# The note on such a PR; the web page shows it in bold.
UPDATED = "updated since review"

# A mechanical change: at least this many code files, a median of at most
# this many changed lines per file, and added and deleted lines that differ
# by at most this share of their sum.
MECHANICAL_FILES = 8
MECHANICAL_MEDIAN = 6
MECHANICAL_BALANCE = 0.25

# A description shorter than this, without the PR template's comments,
# headings, and check boxes, says too little to review against.
THIN_CHARS = 80

TEST_PATH = re.compile(
    r"(^|/)(tests?|testing|unittests?|__tests__)/"
    r"|(^|/)test_[^/]*$"
    r"|_tests?\.[^/.]+$"
    r"|\.(test|spec)\.[^/.]+$",
    re.IGNORECASE,
)
# Docs, build, CI, configuration, and vendored or generated files.
OTHER_PATH = re.compile(
    r"(^|/)(docs?|\.github|cmake|external|vendor|third_party)/"
    r"|\.(md|rst|txt|ya?ml|toml|json|cfg|ini|cmake|lock|svg|png)$"
    r"|(^|/)(CMakeLists\.txt|Dockerfile|Makefile|conanfile\.py|\.[^/]+)$",
    re.IGNORECASE,
)
HARD_PATHS = [
    s.strip().lower()
    for s in os.environ.get("REVIEW_HARD_PATHS", "").split(",")
    if s.strip()
]
TEMPLATE_LINE = re.compile(r"^\s*(#|[-*]\s*\[.\])")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def file_kind(path):
    """Return "test", "other" (docs, build, CI), or "code" for a path."""
    if TEST_PATH.search(path):
        return "test"
    if OTHER_PATH.search(path):
        return "other"
    return "code"


def thin_description(body):
    text = HTML_COMMENT.sub("", body or "")
    kept = [line for line in text.splitlines() if not TEMPLATE_LINE.match(line)]
    return len("".join(kept).strip()) < THIN_CHARS


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def estimate(pr, my_status=None):
    """Return {"score", "lines", "why", "parts"} for a PR as DETAILS_QUERY
    returns it: the score from 1 to 5, the effective lines behind it (to
    order PRs of the same score), and a short explanation, as one string and
    as the list of its parts. my_status says where the reader stands as a
    reviewer of the PR, if they reviewed it: "approved" (the current commit),
    "unchanged" (reviewed, and the PR has no commits since), or "updated"
    (reviewed, and it has). Returns None for PR data without the list of
    files."""
    files = (pr.get("files") or {}).get("nodes")
    if files is None:
        return None
    files = [f for f in files if f]

    raw = {"code": 0, "test": 0, "other": 0}
    weighted = {"code": 0.0, "test": 0.0, "other": 0.0}
    code_sizes = []
    code_files = 0
    code_added = code_deleted = 0
    hard = False
    for f in files:
        kind = file_kind(f["path"])
        lines = f["additions"] + f["deletions"]
        if f["changeType"] == "DELETED":
            effective = lines * DELETED_FILE
        else:
            effective = f["additions"] + (DELETED_LINE * f["deletions"])
        if kind == "code":
            code_files += 1
            if f["changeType"] != "DELETED":
                code_sizes.append(lines)
                code_added += f["additions"]
                code_deleted += f["deletions"]
            path = f["path"].lower()
            if any(part in path for part in HARD_PATHS):
                effective *= HARD_WEIGHT
                hard = hard or lines > 0
        raw[kind] += lines
        weighted[kind] += effective

    changed = code_added + code_deleted
    mechanical = (
        len(code_sizes) >= MECHANICAL_FILES
        and median(code_sizes) <= MECHANICAL_MEDIAN
        and abs(code_added - code_deleted) <= MECHANICAL_BALANCE * changed
    )
    code = weighted["code"] + (FILE_OVERHEAD * len(code_sizes))
    if mechanical:
        code *= MECHANICAL_WEIGHT
    lines = (
        code
        + (TEST_WEIGHT * weighted["test"])
        + (OTHER_WEIGHT * weighted["other"])
    )

    why = []
    if raw["code"]:
        why.append(f"{raw['code']} code lines in "
                   f"{plural(code_files, 'file')}")
    else:
        why.append("no code changes")
    if raw["test"]:
        why.append(plural(raw["test"], "test line"))
    if raw["other"]:
        why.append(plural(raw["other"], "docs/build line"))
    if hard:
        why.append("touches hard paths")
    if mechanical:
        why.append("looks mechanical")

    # GitHub lists the first 100 files; scale up for what wasn't listed.
    listed = sum(raw.values())
    total = pr.get("additions", 0) + pr.get("deletions", 0)
    if pr.get("changedFiles", 0) > len(files) and listed:
        lines *= max(1.0, total / listed)
        why.append(f"first {len(files)} of {pr['changedFiles']} files")

    if thin_description(pr.get("body")):
        lines *= THIN_DESCRIPTION
        why.append("thin description")
    if my_status == "approved":
        lines = 0
        why = ["you approved this commit"]
    elif my_status == "unchanged":
        lines = 0
        why = ["reviewed, no changes"]
    elif my_status == "updated":
        # First, so that it shows where the explanation is cut short.
        lines *= REREVIEW
        why.insert(0, UPDATED)

    score = 1 + sum(lines > level for level in LEVELS)
    return {
        "score": score,
        "lines": round(lines),
        "why": ", ".join(why),
        "parts": why,
    }


def label(effort):
    """Return the estimate as "effort 3/5", or "" without one."""
    return f"effort {effort['score']}/5" if effort else ""

#!/usr/bin/env python3
"""Show open PRs by or reviewed by given GitHub users (by default, me) and
their review status.

Options:
  --user LOGIN        Who "me" is (default: the GH_TOKEN owner).
  --milestone A,B     Only PRs whose milestone is one of these (case-insensitive).
  --date m-d-yy       Only PRs opened on or after this date, e.g. 10-5-26.
  --markdown          Print a Markdown table (for a Slack canvas).
  --force             Fetch from GitHub even if the cache is under 4 hours old.
  --author [A,B]      Only PRs by these authors (no list: me).
  --reviewer [A,B]    Only PRs where one of these is a reviewer, requested or
                      reviewed (no list: me). With both flags, PRs matching
                      either are shown; with neither, PRs I author or review.
  --not-approved      Skip PRs every listed reviewer on the PR has approved,
                      even if the approval is stale. Also skip the authors'
                      PRs (mine, without --author or --reviewer) with two or
                      more approvals, even if they're stale.
  --not-in A,B        Skip PRs whose title contains any of these, ignoring case
                      and matching inside words ("precision" skips
                      "FixedPrecision").
  --in A,B            Only PRs whose title contains one of these, matched the
                      same way as --not-in, which wins if both match.

PR data is cached in ~/.cache/pr-review-digest/prs.json.

Environment (see README.txt):
  GH_REPOS          Repositories to scan, comma separated, as owner/name.
  GH_TOKEN          GitHub token with read access to pull requests in GH_REPOS.
  JIRA_URL          Optional Jira site, e.g. https://example.atlassian.net,
                    whose ticket links in PR bodies are shown.
  JIRA_PROJECTS     Optional Jira project keys, comma separated, whose ticket
                    keys in PR titles and branch names are shown.
  SLACK_BOT_TOKEN   Slack bot token (xoxb-...) with the chat:write scope.
                    Posts a native Slack table.
  SLACK_CHANNEL_ID  Channel to post to. The bot must be a member of it.
  SLACK_WEBHOOK_URL Used when there is no bot token: posts a monospace table
                    to an incoming webhook (/services/...) or a Workflow
                    Builder webhook (/triggers/...).

With neither Slack setting, the monospace table is printed instead.
With --markdown, nothing is posted.
"""

import argparse
import json
import re
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

def env_list(name):
    """The comma-separated environment variable NAME as a list of strings."""
    return [s.strip() for s in os.environ.get(name, "").split(",") if s.strip()]


# Repositories to scan, as owner/name.
REPOS = env_list("GH_REPOS")

# Slack allows up to 100 rows and 10,000 characters of cell text per table.
# Stay under both, leaving room for the header row.
MAX_ROWS = 90
MAX_CHARS = 9000

HEADERS = ["PR", "Milestone", "Author", "Reviewers", "Latest activity"]

# Jira tickets are taken from links to the JIRA_URL site in the PR body, and
# from keys of the JIRA_PROJECTS that appear in the PR title or branch name.
# Both settings are optional; without them no tickets are shown.
NEVER = re.compile(r"(?!)")  # matches nothing
JIRA_SITE = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_BROWSE = JIRA_SITE + "/browse/"
JIRA_URL = (
    re.compile(re.escape(JIRA_SITE.split("://", 1)[-1]) + r"/browse/([A-Za-z][A-Za-z0-9]+-\d+)")
    if JIRA_SITE else NEVER
)
JIRA_PROJECTS = env_list("JIRA_PROJECTS")
JIRA_KEY = (
    re.compile(r"\b(?:" + "|".join(map(re.escape, JIRA_PROJECTS)) + r")-\d+\b", re.IGNORECASE)
    if JIRA_PROJECTS else NEVER
)

# Conventional commit prefixes ("feat: ", "fix(amm): ", "refactor!: ") are
# dropped from titles to leave room for the description.
TITLE_PREFIX = re.compile(r"^\s*[A-Za-z]+(?:\([^)]*\))?!?:\s*")

PROBLEM_EMOJI = {"conflict": "⚠️", "CI failed": "❌"}

# Column widths and message size for the monospace (code block) table.
# The text table puts Reviewers last (the emoji there would push any later
# column out of line), and the widths keep lines short enough not to wrap.
TEXT_WIDTHS = [28, 9, 14, 10]  # PR, Milestone, Author, Latest activity
TEXT_RULE = 96
# Below the first line of a PR, its text may run up to the reviewer column.
PR_LINE_WIDTH = sum(TEXT_WIDTHS) + (2 * (len(TEXT_WIDTHS) - 1))
MAX_TEXT = 3500

# Fetched PR data is reused for 4 hours unless --force is given.
CACHE_FILE = Path.home() / ".cache" / "pr-review-digest" / "prs.json"
CACHE_MAX_AGE = 4 * 3600
CACHE_VERSION = 3  # bump when DETAILS_QUERY's fields change

# TERM_PROGRAM values of terminals that support OSC 8 hyperlinks. Elsewhere
# (e.g. Apple Terminal) the PR URL is printed on its own line instead.
OSC8_TERMINALS = {"iTerm.app", "ghostty", "WezTerm", "vscode"}

# Reviewer status -> (Slack emoji name, Unicode for the printed preview).
STATUS_EMOJI = {
    "approved": ("large_green_circle", "🟢"),
    "stale": ("large_yellow_circle", "🟡"),
    "none": ("red_circle", "🔴"),
    "reviewed": ("large_orange_circle", "🟠"),
}

LEGEND = (
    "🟢 approved    🟡 approved, PR updated since    "
    "🟠 reviewed, not approved    🔴 not reviewed yet"
)

# Open PRs are fetched in three steps so that the requests of each step can
# run concurrently (GraphQL pages have to be walked one after another, but
# PR details looked up by id and REST pages by number don't): a cheap listing
# of PR ids, the details in batches of ids, then all reviews of the PRs that
# have more than one query returns, through the REST API.
LIST_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { id }
    }
  }
}
"""

DETAILS_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on PullRequest {
      id
      number
      title
      url
      isDraft
      headRefOid
      headRefName
      mergeable
      commits(last: 1) {
        nodes { commit { statusCheckRollup { state } } }
      }
      body
      createdAt
      updatedAt
      author { login }
      milestone { title }
      reviewRequests(first: 50) {
        nodes {
          requestedReviewer {
            __typename
            ... on User { login }
          }
        }
      }
      reviews(last: 100) {
        totalCount
        nodes {
          author { __typename login }
          state
          submittedAt
          commit { oid }
        }
      }
    }
  }
}
"""

# PRs per details request (bigger batches take disproportionately longer),
# reviews per REST page (the API's maximum), and concurrent requests.
DETAILS_BATCH = 25
REVIEWS_PER_PAGE = 100
THREADS = 16
API_RETRIES = 3


def github_request(req):
    """Send a GitHub API request and return the JSON body, retrying server
    errors, which show up now and then under concurrent load."""
    for attempt in range(API_RETRIES):
        try:
            with urllib.request.urlopen(req) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == API_RETRIES - 1:
                raise
            time.sleep(1 + attempt)


def graphql(token, query, variables):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
        },
    )
    body = github_request(req)
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"], indent=2))
    return body["data"]


def rest_get(token, url):
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"bearer {token}",
            "Accept": "application/vnd.github+json",
        },
    )
    return github_request(req)


def list_pr_ids(token, repo):
    owner, name = repo.split("/")
    ids = []
    after = None
    while True:
        data = graphql(token, LIST_QUERY, {"owner": owner, "name": name, "after": after})
        conn = data["repository"]["pullRequests"]
        ids += [pr["id"] for pr in conn["nodes"]]
        if not conn["pageInfo"]["hasNextPage"]:
            return ids
        after = conn["pageInfo"]["endCursor"]


def rest_review(review):
    """Return a REST API review in the shape of DETAILS_QUERY's reviews."""
    user = review["user"]
    commit = review["commit_id"]
    return {
        "author": {"__typename": user["type"], "login": user["login"]} if user else None,
        "state": review["state"],
        "submittedAt": review.get("submitted_at"),
        "commit": {"oid": commit} if commit else None,
    }


def fetch_review_page(token, repo, number, page):
    url = (f"https://api.github.com/repos/{repo}/pulls/{number}/reviews"
           f"?per_page={REVIEWS_PER_PAGE}&page={page}")
    return [rest_review(r) for r in rest_get(token, url)]


def fetch_all_prs(token):
    """Return {repo: [pr, ...]} with the open PRs of REPOS and their reviews."""
    with ThreadPoolExecutor(THREADS) as pool:
        ids = dict(zip(REPOS, pool.map(lambda repo: list_pr_ids(token, repo), REPOS)))
        batches = [
            (repo, ids[repo][i:i + DETAILS_BATCH])
            for repo in REPOS
            for i in range(0, len(ids[repo]), DETAILS_BATCH)
        ]
        details = {
            pool.submit(graphql, token, DETAILS_QUERY, {"ids": batch}): repo
            for repo, batch in batches
        }
        prs = {repo: [] for repo in REPOS}
        pages = []
        # Handle each batch as it arrives so that review pages start
        # downloading while other batches are still in flight.
        for future in as_completed(details):
            repo = details[future]
            for pr in future.result()["nodes"]:
                if not pr:  # a PR that closed or went private meanwhile
                    continue
                pr["repo"] = repo.split("/")[1]
                prs[repo].append(pr)
                reviews = pr["reviews"]
                pr["reviews"] = reviews["nodes"]
                if reviews["totalCount"] > len(reviews["nodes"]):
                    # Busy PRs have more reviews than the query returns.
                    # Refetch all their reviews, a page per request, at once.
                    n = -(-reviews["totalCount"] // REVIEWS_PER_PAGE)
                    pages += [
                        (pr, page, pool.submit(fetch_review_page, token, repo,
                                               pr["number"], page))
                        for page in range(1, n + 1)
                    ]
        for pr, page, future in pages:
            # Pages are listed in order, oldest reviews first, like the query's.
            if page == 1:
                pr["reviews"] = []
            pr["reviews"] += future.result()
    for repo_prs in prs.values():
        # Batches finish in any order; show PRs oldest first like before.
        repo_prs.sort(key=lambda pr: (pr["createdAt"], pr["number"]))
    return prs


def summarize_reviewers(pr):
    """Return {login: info} for requested reviewers and anyone who reviewed."""
    author = (pr["author"] or {}).get("login")
    reviewers = {}

    for review in pr["reviews"]:
        who = review["author"]
        if not who or who["__typename"] == "Bot" or who["login"] == author:
            continue
        if review["state"] == "PENDING" or not review["submittedAt"]:
            continue
        info = reviewers.setdefault(
            who["login"], {"last": None, "state": None, "approved_oid": None}
        )
        info["last"] = review["submittedAt"]
        # A later plain comment does not undo an approval or change request.
        if review["state"] != "COMMENTED":
            info["state"] = review["state"]
            info["approved_oid"] = (
                (review["commit"] or {}).get("oid")
                if review["state"] == "APPROVED"
                else None
            )
        elif info["state"] is None:
            info["state"] = "COMMENTED"

    for req in pr["reviewRequests"]["nodes"]:
        r = req["requestedReviewer"]
        if not r:
            continue
        if r["__typename"] == "User":
            key = r["login"]
        elif r["__typename"] == "Team":
            # Team names need org read access, which the token doesn't have.
            key = "(a team)"
        else:
            continue
        reviewers.setdefault(key, {"last": None, "state": None, "approved_oid": None})

    return reviewers


def reviewer_status(info, head_oid):
    if info["state"] == "APPROVED":
        if info["approved_oid"] and info["approved_oid"] != head_oid:
            return "stale"
        return "approved"
    if info["state"] is None:
        return "none"
    return "reviewed"


def pr_author(pr):
    return (pr["author"] or {}).get("login", "").lower()


def matches_people(pr, reviewers, authors, revs):
    """True if the PR's author is in authors or any reviewer is in revs
    (lower-case login sets, either may be empty)."""
    if pr_author(pr) in authors:
        return True
    return any(r.lower() in revs for r in reviewers)


def approved_by_all(reviewers, revs):
    """True if every reviewer in revs who is on the PR has approved it, whether
    or not the PR changed since. A later comment doesn't undo an approval."""
    on_pr = [info for login, info in reviewers.items() if login.lower() in revs]
    return bool(on_pr) and all(info["state"] == "APPROVED" for info in on_pr)


def approval_count(reviewers):
    """Count approvals, whether or not the PR changed since (green or yellow)."""
    return sum(info["state"] == "APPROVED" for info in reviewers.values())


def milestone_matches(pr, milestones):
    if not milestones:
        return True
    title = (pr["milestone"] or {}).get("title", "")
    return title.lower() in milestones


def parse_list(text):
    return {m.strip().lower() for m in text.split(",") if m.strip()}


def parse_date(text):
    try:
        return datetime.strptime(text, "%m-%d-%y").strftime("%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected m-d-yy, e.g. 10-5-26: {text!r}")


def fmt_date(iso):
    return iso[:10] if iso else ""


def pr_problems(pr):
    """Return "conflict" and/or "CI failed" for a PR that needs attention.
    GitHub reports mergeable as UNKNOWN until it has checked, so that's
    treated as fine."""
    problems = []
    if pr["mergeable"] == "CONFLICTING":
        problems.append("conflict")
    commits = pr["commits"]["nodes"]
    rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
    if rollup and rollup["state"] in ("FAILURE", "ERROR"):
        problems.append("CI failed")
    return problems


def jira_tickets(pr):
    """Return Jira keys linked from the PR body, or named in its title or branch."""
    keys = []
    for match in JIRA_URL.finditer(pr["body"] or ""):
        keys.append(match.group(1).upper())
    for text in (pr["title"], pr["headRefName"]):
        for match in JIRA_KEY.finditer(text or ""):
            keys.append(match.group(0).upper())
    return list(dict.fromkeys(keys))


def build_row(pr, reviewers):
    """Return the table row as plain values; Slack cells are built from it."""
    title = TITLE_PREFIX.sub("", pr["title"]) + (" [draft]" if pr["isDraft"] else "")
    revs = []
    for login in sorted(reviewers, key=str.lower):
        info = reviewers[login]
        when = fmt_date(info["last"])
        revs.append(
            (
                reviewer_status(info, pr["headRefOid"]),
                f"{login} ({when})" if when else login,
            )
        )
    return {
        "pr_label": f"{pr['repo']}#{pr['number']}",
        "branch": pr["headRefName"],
        "url": pr["url"],
        "title": title,
        "body": pr["body"] or "",
        "milestone": (pr["milestone"] or {}).get("title", ""),
        "author": (pr["author"] or {}).get("login", "ghost"),
        "reviewers": revs,
        "latest": fmt_date(pr["updatedAt"]),
        "jira": jira_tickets(pr),
        "problems": pr_problems(pr),
    }


def text_cell(text):
    return {"type": "raw_text", "text": text or "-"}


def rich_cell(elements):
    return {
        "type": "rich_text",
        "elements": [{"type": "rich_text_section", "elements": elements}],
    }


def problems_emoji(row):
    """Return the PR's problems as " ⚠️ conflict ❌ CI failed", or ""."""
    return "".join(f" {PROBLEM_EMOJI[p]} {p}" for p in row["problems"])


def slack_row(row):
    pr = rich_cell(
        [
            {"type": "link", "text": row["pr_label"], "url": row["url"]},
            {
                "type": "text",
                "text": f"{problems_emoji(row)}\n{row['branch']}\n{row['title']}",
            },
        ]
    )
    for key in row["jira"]:
        pr["elements"][0]["elements"] += [
            {"type": "text", "text": "\n"},
            {"type": "link", "text": key, "url": JIRA_BROWSE + key},
        ]
    revs = []
    for i, (status, label) in enumerate(row["reviewers"]):
        if i:
            revs.append({"type": "text", "text": "\n"})
        revs.append({"type": "emoji", "name": STATUS_EMOJI[status][0]})
        revs.append({"type": "text", "text": " " + label})
    reviewers = rich_cell(revs) if revs else text_cell("no reviewers")
    return [
        pr,
        text_cell(row["milestone"]),
        text_cell(row["author"]),
        reviewers,
        text_cell(row["latest"]),
    ]


def row_chars(row):
    # Rough count of the cell text Slack measures against its limit.
    return (
        len(row["pr_label"])
        + len(row["branch"]) + 1
        + len(problems_emoji(row))
        + len(row["url"])
        + len(row["title"])
        + len(row["milestone"])
        + len(row["author"])
        + sum(len(label) + 3 for _, label in row["reviewers"])
        + len(row["latest"])
        + sum(len(JIRA_BROWSE) + (2 * len(key)) + 1 for key in row["jira"])
    )


def split_rows(rows):
    batches = []
    current, chars = [], 0
    for row in rows:
        n = row_chars(row)
        if current and (len(current) >= MAX_ROWS or chars + n > MAX_CHARS):
            batches.append(current)
            current, chars = [], 0
        current.append(row)
        chars += n
    if current:
        batches.append(current)
    return batches


def slack_message(channel, title, rows):
    table = {
        "type": "table",
        "column_settings": [
            {"is_wrapped": True},
            None,
            None,
            {"is_wrapped": True},
            None,
        ],
        "rows": [[text_cell(h) for h in HEADERS]] + [slack_row(r) for r in rows],
    }
    return {
        "channel": channel,
        "text": title,
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": LEGEND}]},
            table,
        ],
    }


def post_slack(token, message):
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(message).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError(f"Slack error: {json.dumps(body)}")


def clip(text, width):
    return text if len(text) <= width else text[: width - 1] + "…"


def text_line(cells):
    # Only the last column holds emoji, so their display width can't shift
    # the columns that come after them.
    padded = [c.ljust(w) for c, w in zip(cells, TEXT_WIDTHS)] + cells[len(TEXT_WIDTHS):]
    return "  ".join(padded).rstrip()


def link_style():
    """Return how to show PR links in printed output: "osc8" to make the PR
    label a clickable link, or "url" to print the URL on its own line."""
    if not sys.stdout.isatty():
        return "url"
    if os.environ.get("TERM_PROGRAM") in OSC8_TERMINALS:
        return "osc8"
    if os.environ.get("TERM") == "xterm-kitty":
        return "osc8"
    return "url"


def terminal_link(url, text):
    return f"\033]8;;{url}\033\\{text}\033]8;;\033\\"


def text_table_rows(row, links=None):
    """Return the monospace lines for one PR; links is a link_style() value."""
    # The branch is shown in full on the second line, then any problems.
    pr = [row["pr_label"], row["branch"]]
    if row["problems"]:
        pr.append(", ".join(row["problems"]))
    pr.append(clip(row["title"], PR_LINE_WIDTH))
    if row["jira"]:
        pr.append(clip(" ".join(row["jira"]), PR_LINE_WIDTH))
    revs = [f"{STATUS_EMOJI[s][1]} {label}" for s, label in row["reviewers"]]
    revs = revs or ["no reviewers"]
    if len(row["branch"]) > PR_LINE_WIDTH and len(revs) > 1:
        # A long branch runs into the reviewer column, so start the reviewers
        # below it to keep them together.
        revs = ["", ""] + revs
    lines = [
        text_line(
            [
                pr[0],
                clip(row["milestone"] or "-", TEXT_WIDTHS[1]),
                clip(row["author"], TEXT_WIDTHS[2]),
                row["latest"],
                revs[0],
            ]
        )
    ]
    # Later lines have only the PR column, so it runs up to the reviewers.
    for i in range(1, max(len(pr), len(revs))):
        cell = pr[i] if i < len(pr) else ""
        rev = revs[i] if i < len(revs) else ""
        lines.append(f"{cell.ljust(PR_LINE_WIDTH)}  {rev}".rstrip())
    if links == "osc8":
        # Wrap after padding so the escape codes don't throw off column widths.
        label = row["pr_label"]
        lines[0] = terminal_link(row["url"], label) + lines[0][len(label):]
    elif links == "url":
        lines.append(row["url"])
    return lines


def text_messages(title, rows, links=None, max_text=MAX_TEXT):
    """Return the digest as code-block text messages under MAX_TEXT chars."""
    rule = "-" * TEXT_RULE
    header = "\n".join(
        [
            text_line(["PR", "Milestone", "Author", "Latest", "Reviewers"]),
            text_line(["", "", "", "activity", ""]),
            "=" * TEXT_RULE,
        ]
    )
    blocks = ["\n".join(text_table_rows(r, links) + [rule]) for r in rows]

    messages, current = [], []
    for block in blocks:
        size = sum(len(b) + 1 for b in current) + len(block)
        if current and size > max_text:
            messages.append(current)
            current = []
        current.append(block)
    if current:
        messages.append(current)

    out = []
    for i, msg in enumerate(messages):
        part = f" ({i + 1}/{len(messages)})" if len(messages) > 1 else ""
        body = "\n".join([header] + msg)
        out.append(f"*{title}{part}*\n{LEGEND}\n```\n{body}\n```")
    return out


def md_escape(text):
    return text.replace("|", "\\|").replace("\n", " ")


def markdown_report(title, rows):
    """Return the digest as a Markdown table, for a Slack canvas."""
    lines = [
        f"## {title}",
        "",
        LEGEND,
        "",
        "| PR | Milestone | Jira | Author | Reviewers | Latest activity |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        pr = (f"[{row['pr_label']}]({row['url']}){problems_emoji(row)} "
              f"`{md_escape(row['branch'])}` "
              f"{md_escape(row['title'])}")
        jira = ", ".join(f"[{k}]({JIRA_BROWSE}{k})" for k in row["jira"]) or "-"
        revs = ", ".join(
            f"{STATUS_EMOJI[s][1]} {md_escape(label)}" for s, label in row["reviewers"]
        ) or "no reviewers"
        cells = [pr, md_escape(row["milestone"]) or "-", jira, row["author"], revs,
                 row["latest"]]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def post_webhook(webhook, text):
    # Workflow Builder webhooks take the declared variables; app webhooks take "text".
    key = "report" if "/triggers/" in webhook else "text"
    req = urllib.request.Request(
        webhook,
        data=json.dumps({key: text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()


def parse_args():
    parser = argparse.ArgumentParser(description="Show open PRs related to me.")
    parser.add_argument("--user", help="GitHub login (default: the GH_TOKEN owner)")
    parser.add_argument(
        "--milestone",
        type=parse_list,
        default=set(),
        help="comma separated milestones to include",
    )
    parser.add_argument(
        "--date",
        type=parse_date,
        help="only PRs opened on or after this date (m-d-yy)",
    )
    parser.add_argument("--markdown", action="store_true", help="print Markdown")
    parser.add_argument(
        "--force", action="store_true", help="fetch from GitHub even if cached"
    )
    # With no value these mean me; a bare flag parses to an empty set.
    parser.add_argument(
        "--author",
        nargs="?",
        const="",
        type=parse_list,
        metavar="A,B",
        help="only PRs by these authors (default: me)",
    )
    parser.add_argument(
        "--reviewer",
        nargs="?",
        const="",
        type=parse_list,
        metavar="A,B",
        help="only PRs with these reviewers (default: me)",
    )
    parser.add_argument(
        "--not-approved",
        action="store_true",
        help="skip PRs the reviewers approved, even if the approval is stale",
    )
    parser.add_argument(
        "--not-in",
        type=parse_list,
        default=set(),
        help="comma separated words; skip PRs whose title contains any of them",
    )
    parser.add_argument(
        "--in",
        dest="in_",
        type=parse_list,
        default=set(),
        help="comma separated words; only PRs whose title contains one of them",
    )
    return parser.parse_args()


def load_data(token, force):
    """Return the open PRs and token owner, from the cache when it's fresh."""
    if not force and CACHE_FILE.exists():
        age = time.time() - CACHE_FILE.stat().st_mtime
        data = json.loads(CACHE_FILE.read_text())
        # A cache from an older version of the query is refetched.
        if age < CACHE_MAX_AGE and data.get("version") == CACHE_VERSION:
            print(f"(cached {int(age // 60)} min ago; --force to refresh)",
                  file=sys.stderr)
            return data
    data = {
        "version": CACHE_VERSION,
        "viewer": graphql(token, "query { viewer { login } }", {})["viewer"]["login"],
        "prs": fetch_all_prs(token),
    }
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data))
    return data


def select_rows(data, args):
    """Return the digest title and rows for the PRs in data that pass the
    filters in args (the parsed command line, or the web page's equivalent)."""
    me = (args.user or data["viewer"]).lower()
    authors = args.author
    revs = args.reviewer
    if authors is None and revs is None:
        # Neither flag: PRs I author or review.
        authors = revs = {me}
    else:
        authors = (authors or {me}) if authors is not None else set()
        revs = (revs or {me}) if revs is not None else set()

    rows = []
    for repo in REPOS:
        for pr in data["prs"].get(repo, []):
            title = pr["title"].lower()
            if any(word in title for word in args.not_in):
                continue
            if args.in_ and not any(word in title for word in args.in_):
                continue
            if not milestone_matches(pr, args.milestone):
                continue
            if args.date and fmt_date(pr["createdAt"]) < args.date:
                continue
            reviewers = summarize_reviewers(pr)
            if args.not_approved:
                if approved_by_all(reviewers, revs):
                    continue
                # The authors' PRs that are ready to merge are done too.
                if (pr_author(pr) in authors
                        and approval_count(reviewers) >= 2):
                    continue
            if matches_people(pr, reviewers, authors, revs):
                rows.append(build_row(pr, reviewers))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    who = []
    if authors:
        who.append("by " + ", ".join(sorted(authors)))
    if revs:
        who.append("reviewed by " + ", ".join(sorted(revs)))
    title = f"PRs {' or '.join(who)} on {today}: {len(rows)} open PRs"
    return title, rows


def main():
    args = parse_args()
    token = os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("GH_TOKEN is not set")
    if not REPOS:
        sys.exit("GH_REPOS is not set; see README.txt")
    data = load_data(token, args.force)
    title, rows = select_rows(data, args)
    slack_token = os.environ.get("SLACK_BOT_TOKEN")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")

    if args.markdown:
        print(markdown_report(title, rows))
        return

    if slack_token:
        channel = os.environ.get("SLACK_CHANNEL_ID")
        if not channel:
            sys.exit("SLACK_CHANNEL_ID is not set")
        batches = split_rows(rows)
        for i, batch in enumerate(batches):
            part = f" ({i + 1}/{len(batches)})" if len(batches) > 1 else ""
            post_slack(slack_token, slack_message(channel, title + part, batch))
        return

    links = None if webhook else link_style()
    # Webhook messages have a size limit; printed output is one table.
    max_text = MAX_TEXT if webhook else float("inf")
    for text in text_messages(title, rows, links, max_text):
        if webhook:
            post_webhook(webhook, text)
        else:
            print(text)
            print()


if __name__ == "__main__":
    main()

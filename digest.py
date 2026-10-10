#!/usr/bin/env python3
"""Post a daily digest of open PRs and their review status to Slack.

Environment:
  GH_TOKEN           GitHub token with read access to pull requests in REPOS.
  SLACK_WEBHOOK_URL  Slack incoming webhook (/services/...) or Workflow Builder
                     webhook (/triggers/...). If unset, the digest is printed.
  TEAM_FILE          Optional path to the team list (default: team.txt).
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPOS = ["example-org/repo-one", "example-org/repo-two"]

# Slack messages are split into chunks no longer than this.
MAX_CHUNK = 3500

PR_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 50, after: $after,
                 orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        number
        title
        url
        isDraft
        headRefOid
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
          pageInfo { hasPreviousPage startCursor }
          nodes { ...ReviewFields }
        }
      }
    }
  }
}
fragment ReviewFields on PullRequestReview {
  author { __typename login }
  state
  submittedAt
  commit { oid }
}
"""

MORE_REVIEWS_QUERY = """
query($id: ID!, $before: String) {
  node(id: $id) {
    ... on PullRequest {
      reviews(last: 100, before: $before) {
        pageInfo { hasPreviousPage startCursor }
        nodes { ...ReviewFields }
      }
    }
  }
}
fragment ReviewFields on PullRequestReview {
  author { __typename login }
  state
  submittedAt
  commit { oid }
}
"""


def graphql(token, query, variables):
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise RuntimeError(json.dumps(body["errors"], indent=2))
    return body["data"]


def fetch_prs(token, repo):
    owner, name = repo.split("/")
    prs = []
    after = None
    while True:
        data = graphql(token, PR_QUERY, {"owner": owner, "name": name, "after": after})
        conn = data["repository"]["pullRequests"]
        for pr in conn["nodes"]:
            reviews = pr["reviews"]
            nodes = reviews["nodes"]
            page = reviews["pageInfo"]
            # Busy PRs can have more than 100 reviews; fetch the older ones too.
            while page["hasPreviousPage"]:
                more = graphql(
                    token,
                    MORE_REVIEWS_QUERY,
                    {"id": pr["id"], "before": page["startCursor"]},
                )["node"]["reviews"]
                nodes = more["nodes"] + nodes
                page = more["pageInfo"]
            pr["reviews"] = nodes
            pr["repo"] = name
            prs.append(pr)
        if not conn["pageInfo"]["hasNextPage"]:
            return prs
        after = conn["pageInfo"]["endCursor"]


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
        info = reviewers.setdefault(
            key, {"last": None, "state": None, "approved_oid": None}
        )
        # A re-request after a review means the reviewer owes another look.
        info["requested"] = True

    return reviewers


def is_team_pr(pr, reviewers, team):
    author = (pr["author"] or {}).get("login", "")
    logins = {author.lower()} | {r.lower() for r in reviewers}
    return bool(logins & team)


def fmt_date(iso):
    return iso[:10] if iso else ""


def fmt_reviewer(login, info, head_oid):
    state = info["state"]
    when = fmt_date(info["last"])
    if state == "APPROVED":
        text = f"✅ approved, last review {when}"
        if info["approved_oid"] and info["approved_oid"] != head_oid:
            text += " (new commits since approval)"
    elif state == "CHANGES_REQUESTED":
        text = f"❌ changes requested, last review {when}"
    elif state == "DISMISSED":
        text = f"➖ review dismissed, last review {when}"
    elif state == "COMMENTED":
        text = f"💬 commented, last review {when}"
    else:
        text = "⏳ no review yet"
    if info.get("requested") and state:
        text += ", review re-requested"
    return f"    • {login}: {text}"


def fmt_pr(pr, reviewers):
    author = (pr["author"] or {}).get("login", "ghost")
    milestone = (pr["milestone"] or {}).get("title", "none")
    draft = " [draft]" if pr["isDraft"] else ""
    lines = [
        f"*<{pr['url']}|{pr['repo']}#{pr['number']}>*{draft} {pr['title']}",
        f"    Author: {author}    Milestone: {milestone}",
    ]
    if reviewers:
        for login in sorted(reviewers, key=str.lower):
            lines.append(fmt_reviewer(login, reviewers[login], pr["headRefOid"]))
    else:
        lines.append("    • no reviewers assigned")
    return "\n".join(lines)


def chunk(blocks, header):
    chunks = []
    current = header
    for block in blocks:
        if len(current) + len(block) + 2 > MAX_CHUNK and current:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


def post(webhook, text):
    # Workflow Builder webhooks take the declared variables; app webhooks take "text".
    key = "report" if "/triggers/" in webhook else "text"
    req = urllib.request.Request(
        webhook,
        data=json.dumps({key: text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()


def load_team(path):
    team = set()
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            team.add(line.lower())
    return team


def main():
    token = os.environ.get("GH_TOKEN")
    if not token:
        sys.exit("GH_TOKEN is not set")
    team = load_team(os.environ.get("TEAM_FILE", Path(__file__).with_name("team.txt")))
    if not team:
        sys.exit("team list is empty")

    blocks = []
    for repo in REPOS:
        for pr in fetch_prs(token, repo):
            reviewers = summarize_reviewers(pr)
            if is_team_pr(pr, reviewers, team):
                blocks.append(fmt_pr(pr, reviewers))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = f"*PR review digest for {today}*: {len(blocks)} open PRs"
    messages = chunk(blocks, header)

    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    for i, text in enumerate(messages):
        if len(messages) > 1:
            text = f"({i + 1}/{len(messages)})\n{text}"
        if webhook:
            post(webhook, text)
        else:
            print(text)
            print("-" * 40)


if __name__ == "__main__":
    main()

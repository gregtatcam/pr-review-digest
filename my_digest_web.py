#!/usr/bin/env python3
"""Serve the my_digest.py report as a local web page with its filters.

Options:
  --port N      Port to listen on (default 8765). Only localhost can connect.
  --no-browser  Don't open the page in the browser on start.

The filters work like the my_digest.py options of the same names. Changing
one reloads the page; Force refresh fetches from GitHub even if the cache is
under 4 hours old.

Environment: the same variables as my_digest.py (GH_REPOS, GH_TOKEN, and the
optional JIRA_URL and JIRA_PROJECTS); see README.txt. Showing a ticket's
summary also needs the claude command (Claude Code), which writes it, and
JIRA_EMAIL and JIRA_TOKEN, to read the ticket from Jira; see
ticket_summary.py. The "review" link needs macOS, the claude and gh commands,
and a clone of the PR's repository that no Claude Code session is running
in; REVIEW_REPOS_DIR names the directory the clones are in (default: the
directory this project is in).
"""

import argparse
import base64
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

import my_digest as digest
import pr_summary
import review_effort
import ticket_summary

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
TICKET_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]+-\d+")

# The tickets that jira-digest (a sibling project) has fetched and saved. A
# ticket found there is summarized without asking Jira for it.
JIRA_DIGEST_TICKETS = ticket_summary.SUMMARY_FILE.with_name("tickets.json")

# Where the clones of the repositories are, each in a directory whose name
# starts with its repository's name (widgets, widgets-dev); "review" starts
# in a clone of the PR's repository.
REPOS_DIR = Path(
    os.environ.get("REVIEW_REPOS_DIR") or Path(__file__).resolve().parent.parent
).expanduser()

# Where the review documents are kept, in this directory or any below it: a
# PR's is a Markdown file with "PR<number>" in its name (PR123-review.md).
DOCS_DIR = Path(
    os.environ.get("REVIEW_DOCS_DIR")
    or Path(__file__).resolve().parent.parent / "Documents"
).expanduser()
PR_DOCUMENT = re.compile(r"(?<![A-Za-z0-9])PR(\d+)(?!\d)")
COMMIT = re.compile(r"[0-9a-f]{40}")



def env_number(name):
    """The environment variable as a whole number; 0 when unset or odd."""
    try:
        return max(0, int(os.environ.get(name, "0")))
    except ValueError:
        return 0


# Both are off unless set, so that the server does nothing on its own.
# How many summaries a day may be written before they are asked for; each
# one costs Claude usage. See write_ahead().
WRITE_AHEAD = env_number("PR_SUMMARY_AHEAD")
AHEAD_LOCK = threading.Lock()
# How often, in minutes, to fetch the PRs and notify me of changes to my
# reviews; at least 15. See watch().
NOTIFY_MINUTES = env_number("PR_NOTIFY_MINUTES") and max(15, env_number("PR_NOTIFY_MINUTES"))
NOTIFY_MAX = 5  # notifications shown one each; the rest are counted

# Given to the page and required back by its buttons; see do_POST.
TOKEN = secrets.token_urlsafe()

STATUS_TEXT = {
    "approved": "approved",
    "stale": "approved, PR updated since",
    "reviewed": "reviewed, not approved",
    "none": "not reviewed yet",
}

# Two overlapping pages, drawn with lines so it stays crisp at any size.
COPY_ICON = (
    '<svg viewBox="0 0 24 24" width="20" height="20" fill="none"'
    ' stroke="currentColor" stroke-width="2" stroke-linejoin="round"'
    ' aria-hidden="true">'
    '<rect x="9" y="9" width="12" height="12" rx="2"/>'
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
    "</svg>"
)

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{ font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 20px; color: #1f2328; }}
  h1 {{ font-size: 18px; margin: 0 0 4px; }}
  .meta {{ color: #59636e; margin-bottom: 12px; }}
  .error {{ background: #ffebe9; border: 1px solid #ff8182; padding: 8px;
            margin-bottom: 12px; white-space: pre-wrap; }}
  form {{ display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: flex-start;
          background: #f6f8fa; border: 1px solid #d1d9e0; border-radius: 6px;
          padding: 12px; margin-bottom: 12px; }}
  fieldset {{ border: none; padding: 0; margin: 0; }}
  legend, .label {{ font-weight: 600; display: block; margin-bottom: 4px; }}
  input[type=text] {{ width: 180px; }}
  .milestones {{ max-height: 110px; overflow-y: auto; }}
  .milestones label {{ display: block; }}
  .buttons {{ align-self: flex-end; display: flex; gap: 8px; }}
  button {{ padding: 4px 12px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #d1d9e0; padding: 6px 8px; vertical-align: top;
            text-align: left; }}
  /* The header stays on top of the rows that scroll under it (a PR title
     is positioned, and would otherwise show through), but below an open
     description popup. */
  th {{ background: #f6f8fa; position: sticky; top: 0; z-index: 2; }}
  /* Clicking a column header sorts by it; the arrow shows the direction.
     The classes are sort-asc and sort-desc: "desc" alone is the hidden
     description popup. */
  th.sortable {{ cursor: pointer; user-select: none; white-space: nowrap; }}
  th.sortable:hover {{ color: #0969da; }}
  th.sortable::after {{ content: "\\2195"; color: #afb8c1; margin-left: 4px; }}
  th.sort-asc::after {{ content: "\\25B2"; color: inherit; font-size: 10px; }}
  th.sort-desc::after {{ content: "\\25BC"; color: inherit; font-size: 10px; }}
  td.pr {{ max-width: 520px; }}
  .branch {{ font-family: ui-monospace, Menlo, monospace; font-size: 12px;
             color: #59636e; word-break: break-all; }}
  .size {{ font-size: 12px; color: #59636e; }}
  /* As narrow as its longest line; the spare width goes to other columns. */
  td.effort {{ white-space: nowrap; width: 1%; }}
  .score {{ font-weight: 600; }}
  /* "updated since review": a PR that needs another look. */
  .updated {{ font-weight: 700; color: #1f2328; }}
  .problem {{ color: #cf222e; font-weight: 600; }}
  .reviewer {{ white-space: nowrap; }}
  .nowrap {{ white-space: nowrap; }}
  /* Small inline icon buttons: copy the branch name, show the description. */
  .icon {{ border: none; background: none; padding: 0 2px; margin-left: 4px;
           cursor: pointer; color: #59636e; font: inherit; line-height: 1;
           vertical-align: middle; }}
  .icon:hover {{ color: #0969da; }}
  .copy {{ margin-left: 6px; }}
  /* "review" looks like a link but runs something, so it is a button. */
  .review {{ border: none; background: none; padding: 0; margin-left: 8px;
             cursor: pointer; color: #0969da; font: inherit; }}
  .review:hover {{ text-decoration: underline; }}
  .review:disabled {{ color: #afb8c1; cursor: default; text-decoration: none; }}
  /* Other small buttons that look like links: open the review document,
     go to the session on the PR. */
  .act {{ border: none; background: none; padding: 0; cursor: pointer;
          color: #0969da; font: inherit; }}
  .act:hover {{ text-decoration: underline; }}
  .session {{ margin-left: 8px; font-weight: 600; color: #1a7f37; }}
  /* The list of clones to review in, under the "review" button. */
  .pick {{ position: relative; }}
  .menu {{ position: absolute; left: 8px; top: 100%; z-index: 10;
           background: #fff; border: 1px solid #d1d9e0; border-radius: 6px;
           box-shadow: 0 8px 24px rgba(31, 35, 40, 0.15); padding: 4px;
           white-space: nowrap; }}
  .menu .size {{ padding: 2px 10px; }}
  .menu button {{ display: block; width: 100%; text-align: left; border: none;
                  background: none; padding: 4px 10px; font: inherit;
                  cursor: pointer; }}
  .menu button:hover {{ background: #f6f8fa; }}
  /* The optional instructions for the review, above the clones. */
  .menu input[type=text] {{ display: block; box-sizing: border-box; width: 340px;
                            margin: 2px 6px 4px; font: inherit; }}
  .copy svg {{ display: block; }}
  .info {{ display: inline-block; width: 15px; height: 15px; border-radius: 50%;
           border: 1px solid currentColor; font: italic bold 11px Georgia, serif;
           line-height: 13px; text-align: center; }}
  .copied {{ color: #1a7f37; font-size: 12px; margin-left: 4px; }}
  /* Clicking the "i" icon shows the PR description (or, next to a ticket,
     the ticket's description) in a scrollable popup, positioned relative
     to the title or ticket it belongs to. */
  .title {{ position: relative; display: inline-block; }}
  .desc {{ display: none; position: absolute; left: 0; top: 100%; z-index: 10;
           width: 640px; max-width: 80vw; max-height: 360px; overflow-y: auto;
           background: #fff; border: 1px solid #d1d9e0; border-radius: 6px;
           box-shadow: 0 8px 24px rgba(31, 35, 40, 0.15); padding: 10px 12px;
           margin-top: 2px; font-size: 13px; white-space: pre-wrap;
           word-break: break-word; }}
  .desc.above {{ top: auto; bottom: 100%; margin-top: 0; margin-bottom: 2px; }}
  .desc.empty, .desc .pending {{ color: #59636e; font-style: italic; }}
  .desc.open {{ display: block; }}
  .ticket {{ margin-right: 8px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="meta">{cache} &nbsp; {legend}</div>
{error}
<form method="get" id="filters">
  <fieldset>
    <span class="label">Me</span>
    <input type="text" name="user" value="{user}" placeholder="{viewer}"
           list="logins">
  </fieldset>
  <fieldset>
    <label class="label"><input type="checkbox" name="author_on" {author_on}>
      Author</label>
    <input type="text" name="author" value="{author}" placeholder="me"
           list="logins">
  </fieldset>
  <fieldset>
    <label class="label"><input type="checkbox" name="reviewer_on" {reviewer_on}>
      Reviewer</label>
    <input type="text" name="reviewer" value="{reviewer}" placeholder="me"
           list="logins">
  </fieldset>
  <fieldset>
    <span class="label">Milestone</span>
    <div class="milestones">{milestones}</div>
  </fieldset>
  <fieldset>
    <span class="label">Opened on or after</span>
    <input type="date" name="date" value="{date}">
  </fieldset>
  <fieldset>
    <span class="label">Title contains</span>
    <input type="text" name="in" value="{in_}" placeholder="a,b">
  </fieldset>
  <fieldset>
    <span class="label">Title doesn't contain</span>
    <input type="text" name="not_in" value="{not_in}" placeholder="a,b">
  </fieldset>
  <fieldset>
    <span class="label">&nbsp;</span>
    <label><input type="checkbox" name="not_approved" {not_approved}>
      Not approved</label>
  </fieldset>
  <div class="buttons">
    <button type="submit" title="Reload the table with the text fields as typed">
      Apply filters</button>
    <button type="submit" name="force" value="1"
            title="Fetch the latest PR data from GitHub, then apply the filters">
      Fetch from GitHub</button>
    <a href="/" title="Clear every filter">Clear filters</a>
    <button type="button" id="next"
            title="Start a review of the first PR in the table, as it is sorted, that is waiting for your review, in the first free clone">
      Review next</button>
  </div>
</form>
<datalist id="logins">{logins}</datalist>
<table>
<thead><tr>
<th class="sortable" title="Sort by repository and PR number">PR</th>
<th class="sortable" title="Sort by the estimated work to review the PR, from 1 (easiest) to 5; a guess made from the changed files, not the diff">Effort</th>
<th class="sortable" title="Sort by milestone">Milestone</th>
<th class="sortable" title="Sort by author">Author</th>
<th class="sortable"
    title="Sort by the number of reviewers who haven't approved">Reviewers</th>
<th class="sortable" title="Sort by the date of the latest activity">
  Latest activity</th>
</tr></thead>
<tbody id="rows">
{rows}
</tbody>
</table>
<script>
  // Checkboxes and the date apply as soon as they change; text fields on Enter.
  document.querySelectorAll("#filters input[type=checkbox], #filters input[type=date]")
    .forEach(el => el.addEventListener("change", () => el.form.submit()));
  // Clicking a column header sorts the rows by each cell's data-sort value;
  // clicking it again reverses the order. The choice is kept in localStorage
  // so it survives the reload a filter change causes. (The key's number
  // changes when the columns do, since the saved choice is a column index.)
  const tbody = document.getElementById("rows");
  const headers = [...document.querySelectorAll("th.sortable")];
  const SORT_KEY = "pr-digest-sort-2";
  const sortRows = (col, dir) => {{
    const rows = [...tbody.rows].filter(r => r.cells.length === headers.length);
    const sign = dir === "desc" ? -1 : 1;
    rows.sort((a, b) => sign * a.cells[col].dataset.sort.localeCompare(
      b.cells[col].dataset.sort, undefined, {{numeric: true, sensitivity: "base"}}));
    rows.forEach(r => tbody.appendChild(r));
    headers.forEach((h, i) => {{
      h.classList.toggle("sort-asc", i === col && dir === "asc");
      h.classList.toggle("sort-desc", i === col && dir === "desc");
    }});
  }};
  headers.forEach((h, col) => h.addEventListener("click", () => {{
    const dir = h.classList.contains("sort-asc") ? "desc" : "asc";
    sortRows(col, dir);
    try {{ localStorage.setItem(SORT_KEY, col + ":" + dir); }} catch (e) {{}}
  }}));
  try {{
    const [col, dir] = (localStorage.getItem(SORT_KEY) || "").split(":");
    if (dir && headers[Number(col)]) sortRows(Number(col), dir);
  }} catch (e) {{}}
  // The copy icon puts the branch name on the clipboard and says so briefly.
  document.querySelectorAll(".copy").forEach(btn => {{
    btn.addEventListener("click", async () => {{
      const note = btn.nextElementSibling;
      try {{
        await navigator.clipboard.writeText(btn.dataset.branch);
        note.textContent = "Copied";
      }} catch (e) {{
        note.textContent = "Copy failed";
      }}
      setTimeout(() => {{ note.textContent = ""; }}, 1500);
    }});
  }});
  // "review" lists the clones of the PR's repository that no Claude Code
  // session is running in. Picking one asks the server to open a Terminal
  // window there that starts Claude Code on a review of the PR. That clone
  // is then taken, so it is dropped from every button's list, and a button
  // left with none is greyed out, as the server does when it makes the page.
  const closeMenus = () => document.querySelectorAll(".menu").forEach(m => m.remove());
  const reviewButtons = [...document.querySelectorAll(".review")];
  const REVIEW_TITLE = "Pick a clone of the repository; a terminal opens there,"
    + " checks out the branch, and starts a review of the PR with Claude Code";
  const setClones = (btn, dirs, why) => {{
    btn.dataset.dirs = dirs.join("/");
    btn.disabled = !dirs.length;
    btn.title = dirs.length ? REVIEW_TITLE : why;
  }};
  // The clones taken from this page, and when: the session starts only
  // after the checkout, so for a while the server still sees them as free.
  const taken = {{}};
  const takeClone = dir => {{
    taken[dir] = Date.now();
    reviewButtons.forEach(btn => {{
      const dirs = (btn.dataset.dirs || "").split("/").filter(d => d);
      if (dirs.includes(dir))
        setClones(btn, dirs.filter(d => d !== dir),
                  "Claude Code is running in every clone of the repository");
    }});
  }};
  // The lists are made with the page, and sessions start and end after
  // that: a clone whose session was closed is free again, and one a session
  // was started in elsewhere is taken. So ask the server for the lists as
  // they are now, when the page gets the focus (coming back from the
  // terminal, say) and when "review" is clicked. On failure they stay.
  const refreshClones = async () => {{
    try {{
      const choices = await (await fetch("/clones")).json();
      reviewButtons.forEach(btn => {{
        const choice = choices[btn.dataset.review.split("#")[0]];
        if (!choice) return;
        const dirs = choice[0].filter(d => !(Date.now() - (taken[d] || 0) < 120000));
        setClones(btn, dirs, choice[1]
          || "Claude Code is running in every clone of the repository");
      }});
    }} catch (err) {{}}
  }};
  window.addEventListener("focus", refreshClones);
  // Ask the server to do something on this computer; the answer is the
  // error message, empty when it worked.
  const post = async (path, fields) => {{
    try {{
      const resp = await fetch(path, {{method: "POST",
        body: new URLSearchParams({{...fields, token: "{token}"}})}});
      return resp.ok ? (await resp.json()).error || ""
                     : "The server refused; reload the page and try again.";
    }} catch (err) {{
      return err.message;
    }}
  }};
  // The small action buttons: open the review document, or bring the
  // Terminal window of the session on the PR to the front.
  document.querySelectorAll(".act").forEach(btn => btn.addEventListener("click", async () => {{
    const error = await post("/" + btn.dataset.act,
                             {{pr: btn.dataset.pr, dir: btn.dataset.dir || ""}});
    if (error) alert(error);
  }}));
  // prompt is what was typed above the list of clones, if anything: more
  // instructions for the review, which the server adds to its own prompt.
  const startReview = async (btn, dir, prompt = "") => {{
    const note = btn.nextElementSibling;
    note.textContent = "Opening...";
    const error = await post("/review", {{pr: btn.dataset.review, dir, prompt}});
    note.textContent = error ? "" : "Opened in " + dir;
    if (error) alert(error); else takeClone(dir);
    setTimeout(() => {{ note.textContent = ""; }}, 2500);
  }};
  reviewButtons.forEach(btn => btn.addEventListener("click", async e => {{
    e.stopPropagation();  // or the click would close the list it opens
    const wasOpen = btn.parentNode.querySelector(".menu");
    closeMenus();
    if (wasOpen) return;
    await refreshClones();
    if (btn.disabled) return;
    const menu = document.createElement("div");
    menu.className = "menu";
    const head = document.createElement("div");
    head.className = "size";
    head.textContent = "Review in";
    menu.append(head);
    // Optional: more instructions for the review. Enter starts it in the
    // first clone listed; a click on a clone starts it there.
    const dirs = btn.dataset.dirs.split("/");
    const extra = document.createElement("input");
    extra.type = "text";
    extra.placeholder = "Extra instructions (optional)";
    extra.title = "Added to the prompt the review starts with. Pick a clone,"
      + " or press Enter for the first one";
    extra.addEventListener("click", e => e.stopPropagation());  // keep the list open
    extra.addEventListener("keydown", e => {{
      if (e.key !== "Enter") return;
      closeMenus();
      startReview(btn, dirs[0], extra.value);
    }});
    menu.append(extra);
    dirs.forEach(dir => {{
      const item = document.createElement("button");
      item.type = "button";
      item.textContent = dir;
      item.addEventListener("click", () => startReview(btn, dir, extra.value));
      menu.append(item);
    }});
    btn.parentNode.append(menu);
  }}));
  // "Review next" starts a review of the first PR in the table, in the
  // order it is sorted in, that waits for my review (not reviewed yet, or
  // changed since) and has no session on it, in the first free clone.
  document.getElementById("next").addEventListener("click", async () => {{
    await refreshClones();
    const btn = [...tbody.querySelectorAll(".review")].find(b =>
      !b.disabled && ["todo", "updated"].includes(b.dataset.mine)
      && !("running" in b.dataset));
    if (!btn) {{
      alert("No PR in the table is waiting for your review with a free clone"
            + " to review it in.");
      return;
    }}
    btn.scrollIntoView({{block: "center"}});
    startReview(btn, btn.dataset.dirs.split("/")[0]);
  }});
  document.addEventListener("click", closeMenus);
  document.addEventListener("keydown", e => {{
    if (e.key === "Escape") closeMenus();
  }});
  // The "i" icon toggles the description popup; it opens above the title
  // when there isn't room below. Clicking elsewhere or pressing Escape
  // closes it. A ticket's summary is fetched from the server the first time
  // its popup opens.
  const closeDescs = () => document.querySelectorAll(".desc.open")
    .forEach(d => d.classList.remove("open"));
  // A PR's popup starts with a summary of what the PR does, fetched from
  // the server the first time the popup opens; writing one takes a while.
  // A PR with commits since my review also has a summary of what changed
  // since; each box is filled on its own.
  const loadSummary = async (box, query) => {{
    if (!box || box.dataset.loaded) return;
    box.dataset.loaded = "1";
    let error = "";
    try {{
      const resp = await fetch("/pr?" + new URLSearchParams(query));
      const summary = await resp.json();
      error = summary.error;
      if (!error) {{
        box.textContent = summary.text;
        box.classList.remove("pending");
      }}
    }} catch (e) {{
      error = e.message;
    }}
    if (error) {{
      box.textContent = "(No summary: " + error + ")";
      delete box.dataset.loaded;  // try again the next time it opens
    }}
  }};
  const loadPr = desc => {{
    loadSummary(desc.querySelector(".summary"), {{pr: desc.dataset.pr}});
    loadSummary(desc.querySelector(".changes"),
                {{pr: desc.dataset.pr, since: desc.dataset.since || ""}});
  }};
  const loadTicket = async desc => {{
    if (desc.dataset.loaded) return;
    desc.dataset.loaded = "1";
    try {{
      const resp = await fetch("/ticket?key=" + encodeURIComponent(desc.dataset.key));
      const ticket = await resp.json();
      if (ticket.error) throw new Error(ticket.error);
      const head = document.createElement("strong");
      head.textContent = ticket.title;
      desc.textContent = "";
      desc.append(head, "\\n\\n" + ticket.text);
      desc.classList.remove("empty");
    }} catch (e) {{
      desc.textContent = e.message;
      delete desc.dataset.loaded;  // try again the next time it opens
    }}
  }};
  document.querySelectorAll(".info").forEach(btn => {{
    btn.addEventListener("click", () => {{
      const title = btn.closest(".title");
      const desc = title.querySelector(".desc");
      const wasOpen = desc.classList.contains("open");
      closeDescs();
      if (wasOpen) return;
      const below = window.innerHeight - title.getBoundingClientRect().bottom;
      const above = title.getBoundingClientRect().top;
      desc.classList.toggle("above", below < 200 && above > below);
      desc.classList.add("open");
      if (desc.dataset.key) loadTicket(desc);
      if (desc.dataset.pr) loadPr(desc);
    }});
  }});
  document.addEventListener("click", e => {{
    if (!e.target.closest(".title")) closeDescs();
  }});
  document.addEventListener("keydown", e => {{
    if (e.key === "Escape") closeDescs();
  }});
</script>
</body>
</html>
"""


def first(query, name):
    return query.get(name, [""])[0].strip()


def filter_args(query):
    """Turn the page's query string into the arguments select_rows() takes."""
    return argparse.Namespace(
        user=first(query, "user") or None,
        # A ticked box with no names means me, like the bare CLI flag.
        author=digest.parse_list(first(query, "author"))
        if "author_on" in query else None,
        reviewer=digest.parse_list(first(query, "reviewer"))
        if "reviewer_on" in query else None,
        milestone={m.lower() for m in query.get("milestone", [])},
        date=first(query, "date") or None,
        not_approved="not_approved" in query,
        not_in=digest.parse_list(first(query, "not_in")),
        in_=digest.parse_list(first(query, "in")),
    )


def cache_note():
    if not digest.CACHE_FILE.exists():
        return ""
    age = int((time.time() - digest.CACHE_FILE.stat().st_mtime) // 60)
    return f"Data fetched {age} min ago."


def review_button(row, state):
    """Return the "review" button of a PR, which reads "resume" when there
    is a review document for the PR. It carries the names of the clones a
    review can start in, joined by "/" (which no directory name holds), and
    is greyed out when there are none. Next to it, when a Claude Code
    session is on the PR's branch in a clone, a button that brings the
    session's Terminal window to the front."""
    label = escape(row["pr_label"])
    free, why = state["choices"].get(row["pr_label"].split("#")[0], ([], "No clone"))
    if free:
        dirs = (
            f' data-dirs="{escape("/".join(free))}" title="Pick a clone of the'
            " repository; a terminal opens there, checks out the branch, and"
            ' starts a review of the PR with Claude Code"'
        )
    else:
        dirs = f' disabled title="{escape(why)}"'
    clone = state["running"].get(row["pr_label"])
    session = (
        f'<button type="button" class="act session" data-act="focus"'
        f' data-pr="{label}" data-dir="{escape(clone)}" title="A Claude Code'
        " session is on this PR's branch there; click to bring its Terminal"
        f' window to the front">session in {escape(clone)}</button>'
    ) if clone else ""
    resume = review_document(row, state) is not None
    return (
        '<span class="pick"><button type="button" class="review"'
        f' data-review="{label}" data-mine="{row["mine"]}"'
        f'{" data-running" if clone else ""}{dirs}>'
        f'{"resume" if resume else "review"}</button>'
        f'<span class="copied"></span></span>{session}'
    )


def review_document(row, state):
    """Return the review document of the row's PR, or None."""
    return state["documents"].get(int(row["pr_label"].split("#")[1]))


def review_notes(row, state):
    """Return the line under a PR that says where my review stands: a link
    that opens the review document, and how many replies there were since
    my review. "" when there is neither."""
    notes = []
    document = review_document(row, state)
    if document:
        when = time.strftime("%m-%d", time.localtime(document.stat().st_mtime))
        notes.append(
            '<button type="button" class="act" data-act="open"'
            f' data-pr="{escape(row["pr_label"])}"'
            f' title="Open {escape(document.name)}">review notes {when}</button>'
        )
    if row["replies"]:
        notes.append(
            '<span class="updated" title="Review comments and replies by'
            f' others after your latest review">{row["replies"]}'
            f' repl{"y" if row["replies"] == 1 else "ies"} since your'
            " review</span>"
        )
    return f'<div class="size">{" &middot; ".join(notes)}</div>' if notes else ""


def pr_cell(row, state):
    parts = [
        f'<a href="{escape(row["url"])}" target="_blank" rel="noopener">'
        f'{escape(row["pr_label"])}</a>{review_button(row, state)}'
    ]
    for p in row["problems"]:
        parts.append(f' <span class="problem">{digest.PROBLEM_EMOJI[p]} {p}</span>')
    branch = escape(row["branch"])
    parts.append(
        f'<div class="branch">{branch}'
        f'<button type="button" class="icon copy" data-branch="{branch}"'
        f' title="Copy the branch name">{COPY_ICON}</button>'
        '<span class="copied"></span></div>'
    )
    # Drop the HTML comments from the PR template; GitHub doesn't show them.
    body = HTML_COMMENT.sub("", row["body"]).strip()
    # The popup keeps line breaks (white-space: pre-wrap), so the newlines
    # here show. The summary is filled in when the popup first opens.
    # A PR with commits since my review starts with what changed since.
    since = (
        f' data-since="{escape(row["reviewed_oid"])}">'
        '<strong>Changed since your review</strong>\n'
        '<span class="changes pending">Summarizing...</span>\n\n'
    ) if row["reviewed_oid"] else ">"
    desc = (
        f'<div class="desc" data-pr="{escape(row["pr_label"])}"{since}'
        '<strong>Summary</strong>\n'
        '<span class="summary pending">Summarizing...</span>\n\n'
        '<strong>Description</strong>\n'
        + (escape(body) if body
           else '<span class="pending">(no description)</span>')
        + "</div>"
    )
    parts.append(
        f'<div><span class="title">{escape(row["title"])}'
        '<button type="button" class="icon info" title="Show a summary of'
        ' what the PR does, and its description">'
        f"i</button>{desc}</span></div>"
    )
    if row["jira"]:
        links = "".join(
            '<span class="title ticket">'
            f'<a href="{digest.JIRA_BROWSE}{escape(k)}">{escape(k)}</a>'
            '<button type="button" class="icon info"'
            ' title="Show a summary of the ticket">i</button>'
            f'<div class="desc empty" data-key="{escape(k)}">Summarizing...</div>'
            "</span>"
            for k in row["jira"]
        )
        parts.append(f"<div>{links}</div>")
    parts.append(review_notes(row, state))
    return "".join(parts)


def cached_ticket(key):
    """Return the (title, description) of a ticket that jira-digest has
    fetched and saved, or None when it isn't among them."""
    try:
        tickets = json.loads(JIRA_DIGEST_TICKETS.read_text())["tickets"]
        for ticket in tickets:
            if ticket["key"] == key:
                return (
                    ticket["summary"],
                    ticket_summary.plain_text(ticket["description"]),
                )
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def fetch_ticket(key):
    """Return the (title, description) of a ticket, read from Jira. Raises
    RuntimeError when it can't be read."""
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_TOKEN", "")
    if not (digest.JIRA_SITE and email and token):
        raise RuntimeError("Set JIRA_URL, JIRA_EMAIL, and JIRA_TOKEN to show"
                           " ticket summaries; see README.txt.")
    # API version 2 returns the description as plain wiki text.
    req = urllib.request.Request(
        f"{digest.JIRA_SITE}/rest/api/2/issue/{quote(key)}"
        "?fields=summary,description",
        headers={
            "Authorization": "Basic "
            + base64.b64encode(f"{email}:{token}".encode()).decode(),
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            fields = json.load(resp)["fields"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError(f"{key} was not found, or this Jira account"
                               " can't see it.") from e
        raise RuntimeError(f"Jira returned {e.code} {e.reason} for {key}.") from e
    except (OSError, ValueError, KeyError) as e:
        raise RuntimeError(f"Fetching {key} from Jira failed: {e}") from e
    return fields.get("summary") or "", (fields.get("description") or "").strip()


def ticket_popup(key):
    """Return what the ticket popup shows, as {"title", "text"}, or
    {"error"}. The text is the ticket's saved summary; without one, the
    ticket is taken from jira-digest's saved tickets or else read from Jira,
    and its summary is written and saved."""
    found = ticket_summary.saved(key)
    if found:
        return found
    try:
        title, description = cached_ticket(key) or fetch_ticket(key)
    except RuntimeError as e:
        return {"error": str(e)}
    try:
        return ticket_summary.summary(key, title, description)
    except RuntimeError as e:
        return {
            "title": title,
            "text": f"(No summary: {e}. The ticket description follows.)"
                    f"\n\n{description}",
        }


def claude_sessions():
    """Return the running Claude Code sessions as (process id, working
    directory). Returns none when the processes can't be listed, so a
    failure here doesn't block a review."""
    try:
        listing = subprocess.run(
            ["ps", "-axo", "pid=,comm="], capture_output=True, text=True,
            timeout=10,
        ).stdout
        # The command is "claude" or the full path of the claude program.
        pids = [
            pid for pid, command in
            (line.split(None, 1) for line in listing.splitlines() if line.strip())
            if Path(command.strip()).name == "claude"
        ]
        if not pids:
            return []
        # An "n<directory>" line per process, after its "p<pid>" line.
        listing = subprocess.run(
            ["lsof", "-a", "-p", ",".join(pids), "-d", "cwd", "-Fpn"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    sessions = []
    for line in listing.splitlines():
        if line.startswith("p"):
            pid = int(line[1:])
        elif line.startswith("n"):
            sessions.append((pid, Path(line[1:])))
    return sessions


def inside(directory, cwd):
    """True when cwd is the directory or a directory inside it."""
    root = directory.resolve()
    return cwd == root or root in cwd.parents


def review_documents():
    """Return {PR number: its review document}: the Markdown files under
    DOCS_DIR with "PR<number>" in their names, the newest when a PR has
    several. The name doesn't say which repository the PR is in, so PRs
    with the same number in two repositories share a document."""
    found = {}
    for root, dirs, files in os.walk(DOCS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() != "cache"]
        for name in files:
            if not name.endswith(".md"):
                continue
            path = Path(root, name)
            for number in PR_DOCUMENT.findall(name):
                try:
                    older = found.get(int(number))
                    if not older or older.stat().st_mtime < path.stat().st_mtime:
                        found[int(number)] = path
                except OSError:
                    pass
    return found


def clones(repo):
    """Return the clones of a repository ("owner/name"), sorted by name: the
    directories in REPOS_DIR whose name starts with the repository's name
    and whose origin remote is the repository. That leaves out a clone of a
    fork, where "gh pr checkout" wouldn't find the PR."""
    name = repo.split("/")[1]
    found = []
    try:
        candidates = sorted(
            d for d in REPOS_DIR.iterdir()
            if d.name.startswith(name) and d.is_dir()
        )
    except OSError:
        return []
    for directory in candidates:
        try:
            url = subprocess.run(
                ["git", "-C", str(directory), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        # https://github.com/owner/name.git or git@github.com:owner/name.git
        url = url.removesuffix("/").removesuffix(".git").lower()
        if url.endswith(("/" + repo.lower(), ":" + repo.lower())):
            found.append(directory)
    return found


def free_clones(directories, sessions):
    """Return those of a repository's clones that no Claude Code session is
    running in. A session in a directory inside a clone counts as running
    in the clone."""
    return [
        d for d in directories
        if not any(inside(d, cwd) for _, cwd in sessions)
    ]


def current_branch(directory):
    """Return the branch checked out in a clone, or "" when it can't be
    told."""
    try:
        return subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def review_choice(repo, directories, free):
    """Return what the "review" button of a repository's PRs offers: (names
    of the clones free for a review, why there are none)."""
    if free:
        why = ""
    elif directories:
        why = ("Claude Code is running in every clone of the repository"
               f" ({', '.join(d.name for d in directories)})")
    else:
        why = f"No clone of {repo} in {REPOS_DIR}; see README.txt"
    return [d.name for d in free], why


def review_choices(data):
    """Return {repository name: [names of the free clones, why there are
    none]} as it is now. The page asks for it when it gets the focus and
    when "review" is clicked, since sessions start and end after the page
    was made."""
    sessions = claude_sessions()
    choices = {}
    for repo in (data or {"prs": {}})["prs"]:
        directories = clones(repo)
        choices[repo.split("/")[1]] = review_choice(
            repo, directories, free_clones(directories, sessions))
    return choices


def page_state(data):
    """Return what the PR cells show besides the PR data, worked out once
    per page, not per PR:
      "choices"    for each repository name, what the "review" button of
                   its PRs offers: (names of the clones free for a review,
                   why there are none)
      "running"    {PR label: clone name} for the PRs whose branch is
                   checked out in a clone that a Claude Code session is in
      "documents"  {PR number: review document}"""
    sessions = claude_sessions()
    choices, running = {}, {}
    for repo, prs in data["prs"].items():
        directories = clones(repo)
        free = free_clones(directories, sessions)
        choices[repo.split("/")[1]] = review_choice(repo, directories, free)
        for directory in directories:
            if directory in free:
                continue
            branch = current_branch(directory)
            for pr in prs:
                if pr["headRefName"] == branch:
                    running.setdefault(f"{pr['repo']}#{pr['number']}", directory.name)
    return {"choices": choices, "running": running, "documents": review_documents()}


def find_pr(data, label):
    """Return (repository as "owner/name", PR) for the PR with this label
    ("repo#number") in the loaded PR data. Raises RuntimeError when it isn't
    there."""
    for repo, prs in (data or {"prs": {}})["prs"].items():
        for pr in prs:
            if f"{pr['repo']}#{pr['number']}" == label:
                return repo, pr
    raise RuntimeError(f"{label} isn't among the loaded PRs; reload the page.")


def pr_popup(data, label, token, since=""):
    """Return what the PR popup shows above the description, as {"text"}:
    the PR's summary, or with since (the commit I last reviewed) the
    summary of what changed after it; saved or written now. Returns
    {"error"} when there is none to give."""
    try:
        repo, pr = find_pr(data, label)
        if not since:
            return pr_summary.summary(repo, pr, token)
        if not COMMIT.fullmatch(since):
            raise RuntimeError("not a commit id")
        return pr_summary.changes(repo, pr, since, token)
    except RuntimeError as e:
        return {"error": str(e)}


def write_ahead(data, token):
    """Write the summaries of the PRs that wait for my review before they
    are asked for, so that their popups open at once: what changed since my
    review for a PR I reviewed, the PR's summary for one I haven't. At most
    WRITE_AHEAD new ones a day, since each costs Claude usage. Runs in its
    own thread, one at a time."""
    if not AHEAD_LOCK.acquire(blocking=False):
        return
    try:
        _, rows = digest.select_rows(data, filter_args({}))
        for row in rows:
            if row["mine"] not in ("todo", "updated"):
                continue
            try:
                repo, pr = find_pr(data, row["pr_label"])
                if row["reviewed_oid"]:
                    pr_summary.changes(repo, pr, row["reviewed_oid"], token, WRITE_AHEAD)
                else:
                    pr_summary.summary(repo, pr, token, WRITE_AHEAD)
            except pr_summary.OverBudget:
                return
            except RuntimeError:
                continue  # this one will be tried again when it's asked for
    finally:
        AHEAD_LOCK.release()


def review_states(data):
    """Return {PR label: (where my review stands, replies since my review,
    title)} for the PRs I'm a reviewer of; see build_row() for the first
    two."""
    me = data["viewer"].lower()
    states = {}
    for prs in data["prs"].values():
        for pr in prs:
            row = digest.build_row(pr, digest.summarize_reviewers(pr), me)
            if row["mine"]:
                states[row["pr_label"]] = (row["mine"], row["replies"], row["title"])
    return states


def news(old, new):
    """Return what happened to my reviews between two loads of the PR data,
    a line each: a review asked of me, commits on a PR I had reviewed, and
    replies after my review."""
    before, lines = review_states(old), []
    for label, (mine, replies, title) in review_states(new).items():
        was, had = before.get(label, ("", 0, ""))[:2]
        if mine == "todo" and was != "todo":
            lines.append(f"Review requested: {label} {title}")
        elif mine == "updated" and was != "updated":
            lines.append(f"Updated since your review: {label} {title}")
        elif replies > had:
            lines.append(f"New replies: {label} {title}")
    return lines


def notify(lines):
    """Show the lines as macOS notifications, the first few one each and
    the rest as a count."""
    script = ["on run argv",
              'display notification (item 1 of argv) with title "PR digest"',
              "end run"]
    args = ["osascript"]
    for line in script:
        args += ["-e", line]
    shown = lines[:NOTIFY_MAX]
    if len(lines) > NOTIFY_MAX:
        shown.append(f"and {len(lines) - NOTIFY_MAX} more; see the PR digest page")
    for line in shown:
        try:
            subprocess.run(args + [line], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return


def watch(token):
    """Every NOTIFY_MINUTES, fetch the PRs from GitHub and notify me of what
    happened to my reviews since the last fetch. Runs in its own thread for
    as long as the server does. It asks GitHub only; no Claude usage."""
    while True:
        time.sleep(NOTIFY_MINUTES * 60)
        try:
            with Handler.lock:
                old = Handler.data or digest.load_data(token, False)
                Handler.data = digest.load_data(token, True)
            notify(news(old, Handler.data))
        except Exception as e:  # keep watching whatever one round hits
            print(f"watch: {e}", file=sys.stderr)


def start_review(data, form):
    """Start a review of the PR the form names in an idle Terminal tab, or
    a new Terminal window when there is none, with the instructions typed
    on the page, if any, added to the prompt."""
    open_terminal(review_command(data, first(form, "pr"), first(form, "dir"),
                                 first(form, "prompt")))


# The longest instructions "review" takes from the page, in characters.
EXTRA_PROMPT_MAX = 4000


def extra_instructions(text):
    """Return the instructions typed on the page for a review as one line
    of printable text, to add to the prompt: the command is typed into a
    terminal, where a line break or a control character would do something
    else. Raises RuntimeError when they are too long."""
    text = " ".join("".join(c if c.isprintable() else " " for c in text).split())
    if len(text) > EXTRA_PROMPT_MAX:
        raise RuntimeError(f"The instructions are {len(text)} characters long;"
                           f" at most {EXTRA_PROMPT_MAX} fit.")
    return text


def focus_session(data, form):
    """Bring to the front the Terminal window of a Claude Code session in
    the clone the form names, of the repository of the PR it names. Raises
    RuntimeError when there is no such session or window."""
    repo, _ = find_pr(data, first(form, "pr"))
    name = first(form, "dir")
    directory = next((d for d in clones(repo) if d.name == name), None)
    if not directory:
        raise RuntimeError(f"{name!r} isn't a clone of {repo} in {REPOS_DIR}.")
    pids = [pid for pid, cwd in claude_sessions() if inside(directory, cwd)]
    if not pids:
        raise RuntimeError(f"No Claude Code session is running in {directory}"
                           " any more; reload the page.")
    focus_terminal(pids)


def open_document(data, form):
    """Open the review document of the PR the form names, in the program
    that opens Markdown files. Raises RuntimeError when there is none."""
    _, pr = find_pr(data, first(form, "pr"))
    document = review_documents().get(pr["number"])
    if not document:
        raise RuntimeError(f"No review document for PR {pr['number']} under"
                           f" {DOCS_DIR} any more; reload the page.")
    try:
        subprocess.run(["open", str(document)], check=True, capture_output=True,
                       text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Opening {document} failed: {e}") from e


# AppleScript: select the Terminal tab on one of the given ttys and bring
# its window to the front; prints "found" when there is one.
FOCUS_SCRIPT = [
    "on run argv",
    'tell application "Terminal"',
    "repeat with w in windows",
    "repeat with t in tabs of w",
    "if (tty of t) is in argv then",
    "set selected of t to true",
    "set index of w to 1",
    "activate",
    'return "found"',
    "end if",
    "end repeat",
    "end repeat",
    "end tell",
    'return ""',
    "end run",
]


def focus_terminal(pids):
    """Bring to the front the Terminal window that one of the processes
    runs in. Raises RuntimeError when none of them is in a Terminal window
    (a session can also run in an editor or another terminal program)."""
    args = ["osascript"]
    for line in FOCUS_SCRIPT:
        args += ["-e", line]
    try:
        ttys = subprocess.run(
            ["ps", "-o", "tty=", "-p", ",".join(map(str, pids))],
            capture_output=True, text=True, timeout=10,
        ).stdout.split()
        ttys = [f"/dev/{t}" for t in ttys if t != "??"]
        found = ttys and subprocess.run(
            args + ttys, check=True, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Asking Terminal for its windows failed: {e}") from e
    if not found:
        raise RuntimeError("The session isn't in a Terminal window; it may be"
                           " running in an editor or another terminal program.")


def review_command(data, label, name, extra=""):
    """Return the shell command that "review" runs for the PR with this
    label ("repo#number") in the clone with this directory name: go to the
    clone, check out the PR's branch at its latest commit, and start Claude
    Code on a review. extra is more instructions for the review, typed on
    the page; they follow the prompt. The directory must be one of the repository's clones,
    so the request can't name any other. Raises RuntimeError when the PR or
    the clone isn't found, or when Claude Code is running in the clone (the
    page lists only the free clones, but may be out of date)."""
    repo, pr = find_pr(data, label)
    directory = next((d for d in clones(repo) if d.name == name), None)
    if not directory:
        raise RuntimeError(f"{name!r} isn't a clone of {repo} in {REPOS_DIR}.")
    if not free_clones([directory], claude_sessions()):
        raise RuntimeError(
            f"Claude Code is now running in {directory}. The review would"
            " switch the branch under it, so it wasn't started; reload the"
            " page to see the clones that are free."
        )
    prompt = (
        f"Review PR #{pr['number']} of {repo}, branch {pr['headRefName']}."
        " If a review document for this PR already exists, resume the review"
        " from it instead of starting over."
    )
    extra = extra_instructions(extra)
    if extra:
        prompt += f" Additional instructions: {extra}"
    # gh rather than "git pull" and "git checkout <branch>": it does both,
    # and also gets the branch of a PR from a fork, which isn't in origin
    # (where a branch of the same name, say develop, may be something else).
    return (
        f"cd {shlex.quote(str(directory))}"
        f" && gh pr checkout {pr['number']}"
        f" && claude {shlex.quote(prompt)}"
    )


# AppleScript: run the command (its one argument) in a Terminal tab and
# bring that tab's window to the front. It takes the first tab that is idle
# (a shell at its prompt, nothing running in it: "busy" is false), so not
# one with a Claude Code session, an editor, or a build in it, and opens a
# new window when there is none. Prints "reused" or "new". The command is
# sent before the window is raised: raising renumbers the windows, and the
# tab reference goes by window number.
RUN_SCRIPT = [
    "on run argv",
    'tell application "Terminal"',
    "repeat with w in windows",
    "try",
    "repeat with t in tabs of w",
    "if not (busy of t) then",
    "do script (item 1 of argv) in t",
    "set selected of t to true",
    "if miniaturized of w then set miniaturized of w to false",
    "set index of w to 1",
    "activate",
    'return "reused"',
    "end if",
    "end repeat",
    "end try",
    "end repeat",
    "activate",
    "do script (item 1 of argv)",
    'return "new"',
    "end tell",
    "end run",
]


# Ctrl-U, the shell's "kill line" key. Terminal types the command at the
# tab's prompt, after anything that was typed there and not entered: with
# "git checkout " left on the line the shell ran "git checkout cd ...".
# Sent first, this clears the line.
CLEAR_LINE = "\x15"


def open_terminal(command):
    """Run the shell command in an idle Terminal tab, or in a new Terminal
    window when no tab is idle, and bring it to the front; see RUN_SCRIPT.
    The command is handed to AppleScript as an argument, so it needs no
    quoting beyond the shell's own. Raises RuntimeError when Terminal can't
    be asked."""
    args = ["osascript"]
    for line in RUN_SCRIPT:
        args += ["-e", line]
    command = CLEAR_LINE + command
    try:
        subprocess.run(args + [command], check=True, capture_output=True,
                       text=True, timeout=60)
    except FileNotFoundError as e:
        raise RuntimeError("Opening a terminal needs macOS (osascript).") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Terminal didn't answer; check for a macOS"
                           " permission prompt.") from e
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Opening Terminal failed: {e.stderr.strip()}") from e


def reviewers_cell(row):
    if not row["reviewers"]:
        return "no reviewers"
    return "".join(
        f'<div class="reviewer" title="{STATUS_TEXT[s]}">'
        f"{digest.STATUS_EMOJI[s][1]} {escape(label)}</div>"
        for s, label in row["reviewers"]
    )


def pending_reviews(row):
    """Sort key for the Reviewers column: how many reviewers haven't approved,
    then how many reviewers there are."""
    pending = sum(1 for s, _ in row["reviewers"] if s != "approved")
    return f"{pending}.{len(row['reviewers'])}"


def effort_cell(row):
    """Return the Effort column's cell: the score, where the reader stands
    as a reviewer (when they reviewed the PR), the PR's size and then, a
    line each to keep the column narrow, what the score was made from. It
    sorts by the weighted lines behind the score
    (not shown), so PRs with the same score are ordered too; a PR without an
    estimate sorts last."""
    effort = row["effort"]
    size = (
        '<div class="size" title="Files changed, and total lines changed'
        f' (added plus deleted)">{escape(row["size"])}</div>'
    ) if row["size"] else ""
    if not effort:
        return f'<td class="effort" data-sort="99999999">-{size}</td>'
    state = effort["state"]
    updated = " updated" if state == review_effort.UPDATED else ""
    state_line = (
        f'<div class="size{updated}">{escape(state)}</div>' if state else ""
    )
    # The state is the first of the parts; it has its own line above.
    why = "".join(
        f'<div class="size">{escape(part)}</div>'
        for part in effort["parts"][1 if state else 0:]
    )
    return (
        f'<td class="effort" data-sort="{effort["lines"]:08d}">'
        f'<span class="score">{effort["score"]}</span>'
        f"{state_line}{size}{why}</td>"
    )


def table_rows(rows, state):
    if not rows:
        return '<tr><td colspan="6">No PRs match these filters.</td></tr>'
    return "\n".join(
        "<tr>"
        f'<td class="pr" data-sort="{escape(r["pr_label"])}">'
        f"{pr_cell(r, state)}</td>"
        f"{effort_cell(r)}"
        f'<td class="nowrap" data-sort="{escape(r["milestone"])}">'
        f'{escape(r["milestone"] or "-")}</td>'
        f'<td data-sort="{escape(r["author"])}">{escape(r["author"])}</td>'
        f'<td data-sort="{pending_reviews(r)}">{reviewers_cell(r)}</td>'
        f'<td class="nowrap" data-sort="{escape(r["latest"])}">'
        f'{escape(r["latest"])}</td>'
        "</tr>"
        for r in rows
    )


def milestone_boxes(data, chosen):
    titles = {
        (pr["milestone"] or {}).get("title")
        for prs in data["prs"].values()
        for pr in prs
    }
    titles = sorted((t for t in titles if t), key=str.lower)
    if not titles:
        return "(none)"
    return "".join(
        f'<label><input type="checkbox" name="milestone" value="{escape(t)}"'
        f'{" checked" if t.lower() in chosen else ""}> {escape(t)}</label>'
        for t in titles
    )


def login_options(data):
    logins = set()
    for prs in data["prs"].values():
        for pr in prs:
            logins.add((pr["author"] or {}).get("login"))
            logins.update(digest.summarize_reviewers(pr))
    logins.discard(None)
    logins.discard("(a team)")
    return "".join(
        f'<option value="{escape(x)}">' for x in sorted(logins, key=str.lower)
    )


def render(query, data, error=""):
    args = filter_args(query)
    title, rows = digest.select_rows(data, args)
    checked = lambda name: "checked" if name in query else ""
    return PAGE.format(
        title=escape(title),
        cache=escape(cache_note()),
        legend=escape(digest.LEGEND),
        error=f'<div class="error">{escape(error)}</div>' if error else "",
        user=escape(first(query, "user")),
        viewer=escape(data["viewer"]),
        author_on=checked("author_on"),
        author=escape(first(query, "author")),
        reviewer_on=checked("reviewer_on"),
        reviewer=escape(first(query, "reviewer")),
        milestones=milestone_boxes(data, args.milestone),
        date=escape(first(query, "date")),
        in_=escape(first(query, "in")),
        not_in=escape(first(query, "not_in")),
        not_approved=checked("not_approved"),
        token=TOKEN,
        logins=login_options(data),
        rows=table_rows(rows, page_state(data)),
    )


class Handler(BaseHTTPRequestHandler):
    token = None
    data = None  # the last PR data loaded
    lock = threading.Lock()  # one fetch at a time
    ahead_for = None  # when the data that summaries were last written ahead for was fetched

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/ticket":
            self.send_ticket(first(query, "key").upper())
            return
        if url.path == "/pr":
            self.send_json(pr_popup(
                Handler.data, first(query, "pr"), Handler.token, first(query, "since")
            ))
            return
        if url.path == "/clones":
            self.send_json(review_choices(Handler.data))
            return
        if url.path != "/":
            self.send_error(404)
            return
        if "force" in query:
            error = self.load(force=True)
            if not error:
                # Drop force from the URL so reloading the page doesn't refetch.
                del query["force"]
                self.send_response(303)
                self.send_header("Location", "/?" + urlencode(query, doseq=True))
                self.end_headers()
                return
        else:
            error = self.load(force=False)
        if Handler.data is None:
            self.send_error(500, explain=error)
            return
        body = render(query, Handler.data, error).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        """Do one of the things the page's buttons do on this computer:
        start a review in a Terminal window, bring a session's window
        to the front, or open a review document. Only this server's own
        page may ask: the request must be addressed to this server by its
        local name and carry the token the page was given, so no other web
        page can make the server run anything."""
        actions = {"/review": start_review, "/focus": focus_session,
                   "/open": open_document}
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode(errors="replace"))
        port = self.server.server_port
        action = actions.get(urlparse(self.path).path)
        if (
            not action
            or self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}")
            or not secrets.compare_digest(first(form, "token"), TOKEN)
        ):
            self.send_error(403)
            return
        try:
            action(Handler.data, form)
            result = {}
        except RuntimeError as e:
            result = {"error": str(e)}
        self.send_json(result)

    def send_json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_ticket(self, key):
        """Answer the ticket popup's request with the ticket's summary as JSON."""
        if not TICKET_KEY.fullmatch(key):
            self.send_error(400)
            return
        body = json.dumps(ticket_popup(key)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def load(self, force):
        """Load PR data into Handler.data and return an error message, or ""
        on success. On failure the last good data is kept."""
        try:
            with Handler.lock:
                Handler.data = digest.load_data(Handler.token, force)
            # Once for each fetch from GitHub, not for each page shown.
            fetched = digest.CACHE_FILE.stat().st_mtime
            if WRITE_AHEAD and fetched != Handler.ahead_for:
                Handler.ahead_for = fetched
                threading.Thread(
                    target=write_ahead, args=(Handler.data, Handler.token),
                    daemon=True,
                ).start()
            return ""
        except Exception as e:  # show any fetch failure on the page
            return f"Fetching from GitHub failed: {e}"

    def log_message(self, fmt, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description="Serve the PR digest as a web page.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    Handler.token = os.environ.get("GH_TOKEN")
    if not Handler.token:
        sys.exit("GH_TOKEN is not set")
    if not digest.REPOS:
        sys.exit("GH_REPOS is not set; see README.txt")
    # Threads, so that a browser's idle keep-alive or preconnect sockets
    # don't block other requests; Handler.lock keeps fetches one at a time.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Serving {url} (Ctrl-C to stop)", file=sys.stderr)
    if NOTIFY_MINUTES:
        threading.Thread(target=watch, args=(Handler.token,), daemon=True).start()
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

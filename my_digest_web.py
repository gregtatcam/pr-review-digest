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
ticket_summary.py.
"""

import argparse
import base64
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

import my_digest as digest
import ticket_summary

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
TICKET_KEY = re.compile(r"[A-Za-z][A-Za-z0-9]+-\d+")

# The tickets that jira-digest (a sibling project) has fetched and saved. A
# ticket found there is summarized without asking Jira for it.
JIRA_DIGEST_TICKETS = ticket_summary.SUMMARY_FILE.with_name("tickets.json")

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
  th {{ background: #f6f8fa; position: sticky; top: 0; }}
  /* Clicking a column header sorts by it; the arrow shows the direction. */
  th.sortable {{ cursor: pointer; user-select: none; white-space: nowrap; }}
  th.sortable:hover {{ color: #0969da; }}
  th.sortable::after {{ content: "\\2195"; color: #afb8c1; margin-left: 4px; }}
  th.sortable.asc::after {{ content: "\\25B2"; color: inherit; font-size: 10px; }}
  th.sortable.desc::after {{ content: "\\25BC"; color: inherit; font-size: 10px; }}
  td.pr {{ max-width: 520px; }}
  .branch {{ font-family: ui-monospace, Menlo, monospace; font-size: 12px;
             color: #59636e; word-break: break-all; }}
  .problem {{ color: #cf222e; font-weight: 600; }}
  .reviewer {{ white-space: nowrap; }}
  .nowrap {{ white-space: nowrap; }}
  /* Small inline icon buttons: copy the branch name, show the description. */
  .icon {{ border: none; background: none; padding: 0 2px; margin-left: 4px;
           cursor: pointer; color: #59636e; font: inherit; line-height: 1;
           vertical-align: middle; }}
  .icon:hover {{ color: #0969da; }}
  .copy {{ margin-left: 6px; }}
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
  .desc.empty {{ color: #59636e; font-style: italic; }}
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
  </div>
</form>
<datalist id="logins">{logins}</datalist>
<table>
<thead><tr>
<th class="sortable" title="Sort by repository and PR number">PR</th>
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
  // so it survives the reload a filter change causes.
  const tbody = document.getElementById("rows");
  const headers = [...document.querySelectorAll("th.sortable")];
  const SORT_KEY = "pr-digest-sort";
  const sortRows = (col, dir) => {{
    const rows = [...tbody.rows].filter(r => r.cells.length === headers.length);
    const sign = dir === "desc" ? -1 : 1;
    rows.sort((a, b) => sign * a.cells[col].dataset.sort.localeCompare(
      b.cells[col].dataset.sort, undefined, {{numeric: true, sensitivity: "base"}}));
    rows.forEach(r => tbody.appendChild(r));
    headers.forEach((h, i) => {{
      h.classList.toggle("asc", i === col && dir === "asc");
      h.classList.toggle("desc", i === col && dir === "desc");
    }});
  }};
  headers.forEach((h, col) => h.addEventListener("click", () => {{
    const dir = h.classList.contains("asc") ? "desc" : "asc";
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
  // The "i" icon toggles the description popup; it opens above the title
  // when there isn't room below. Clicking elsewhere or pressing Escape
  // closes it. A ticket's summary is fetched from the server the first time
  // its popup opens.
  const closeDescs = () => document.querySelectorAll(".desc.open")
    .forEach(d => d.classList.remove("open"));
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


def pr_cell(row):
    parts = [
        f'<a href="{escape(row["url"])}" target="_blank" rel="noopener">'
        f'{escape(row["pr_label"])}</a>'
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
    desc = (
        f'<div class="desc">{escape(body)}</div>' if body
        else '<div class="desc empty">(no description)</div>'
    )
    parts.append(
        f'<div><span class="title">{escape(row["title"])}'
        '<button type="button" class="icon info" title="Show the description">'
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


def table_rows(rows):
    if not rows:
        return '<tr><td colspan="5">No PRs match these filters.</td></tr>'
    return "\n".join(
        "<tr>"
        f'<td class="pr" data-sort="{escape(r["pr_label"])}">{pr_cell(r)}</td>'
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
        logins=login_options(data),
        rows=table_rows(rows),
    )


class Handler(BaseHTTPRequestHandler):
    token = None
    data = None  # the last PR data loaded
    lock = threading.Lock()  # one fetch at a time

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/ticket":
            self.send_ticket(first(query, "key").upper())
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
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

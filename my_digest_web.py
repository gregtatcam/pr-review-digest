#!/usr/bin/env python3
"""Serve the my_digest.py report as a local web page with its filters.

Options:
  --port N      Port to listen on (default 8765). Only localhost can connect.
  --no-browser  Don't open the page in the browser on start.

The filters work like the my_digest.py options of the same names. Changing
one reloads the page; Force refresh fetches from GitHub even if the cache is
under 4 hours old.

Environment: the same variables as my_digest.py (GH_REPOS, GH_TOKEN, and the
optional JIRA_URL and JIRA_PROJECTS); see README.txt.
"""

import argparse
import os
import re
import sys
import threading
import time
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import my_digest as digest

HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

STATUS_TEXT = {
    "approved": "approved",
    "stale": "approved, PR updated since",
    "reviewed": "reviewed, not approved",
    "none": "not reviewed yet",
}

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
  td.pr {{ max-width: 520px; }}
  .branch {{ font-family: ui-monospace, Menlo, monospace; font-size: 12px;
             color: #59636e; word-break: break-all; }}
  .problem {{ color: #cf222e; font-weight: 600; }}
  .reviewer {{ white-space: nowrap; }}
  .nowrap {{ white-space: nowrap; }}
  /* Hovering a title shows the PR description in a scrollable popup. The
     popup is a child of the title, so moving the mouse into it to scroll
     keeps the hover and the popup open. */
  .title {{ position: relative; display: inline-block; cursor: default; }}
  .title:hover {{ text-decoration: underline dotted; }}
  .desc {{ display: none; position: absolute; left: 0; top: 100%; z-index: 10;
           width: 640px; max-width: 80vw; max-height: 360px; overflow-y: auto;
           background: #fff; border: 1px solid #d1d9e0; border-radius: 6px;
           box-shadow: 0 8px 24px rgba(31, 35, 40, 0.15); padding: 10px 12px;
           margin-top: 2px; font-size: 13px; white-space: pre-wrap;
           word-break: break-word; }}
  .desc.above {{ top: auto; bottom: 100%; margin-top: 0; margin-bottom: 2px; }}
  .desc.empty {{ color: #59636e; font-style: italic; }}
  .title:hover .desc {{ display: block; }}
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
<thead><tr><th>PR</th><th>Milestone</th><th>Author</th><th>Reviewers</th>
<th>Latest activity</th></tr></thead>
<tbody>
{rows}
</tbody>
</table>
<script>
  // Checkboxes and the date apply as soon as they change; text fields on Enter.
  document.querySelectorAll("#filters input[type=checkbox], #filters input[type=date]")
    .forEach(el => el.addEventListener("change", () => el.form.submit()));
  // Open the description popup above the title when there isn't room below.
  document.querySelectorAll(".title").forEach(el => {{
    el.addEventListener("mouseenter", () => {{
      const desc = el.querySelector(".desc");
      const below = window.innerHeight - el.getBoundingClientRect().bottom;
      const above = el.getBoundingClientRect().top;
      desc.classList.toggle("above", below < 200 && above > below);
    }});
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
    parts = [f'<a href="{escape(row["url"])}">{escape(row["pr_label"])}</a>']
    for p in row["problems"]:
        parts.append(f' <span class="problem">{digest.PROBLEM_EMOJI[p]} {p}</span>')
    parts.append(f'<div class="branch">{escape(row["branch"])}</div>')
    # Drop the HTML comments from the PR template; GitHub doesn't show them.
    body = HTML_COMMENT.sub("", row["body"]).strip()
    desc = (
        f'<div class="desc">{escape(body)}</div>' if body
        else '<div class="desc empty">(no description)</div>'
    )
    parts.append(f'<div><span class="title">{escape(row["title"])}{desc}</span></div>')
    if row["jira"]:
        links = " ".join(
            f'<a href="{digest.JIRA_BROWSE}{escape(k)}">{escape(k)}</a>'
            for k in row["jira"]
        )
        parts.append(f"<div>{links}</div>")
    return "".join(parts)


def reviewers_cell(row):
    if not row["reviewers"]:
        return "no reviewers"
    return "".join(
        f'<div class="reviewer" title="{STATUS_TEXT[s]}">'
        f"{digest.STATUS_EMOJI[s][1]} {escape(label)}</div>"
        for s, label in row["reviewers"]
    )


def table_rows(rows):
    if not rows:
        return '<tr><td colspan="5">No PRs match these filters.</td></tr>'
    return "\n".join(
        "<tr>"
        f'<td class="pr">{pr_cell(r)}</td>'
        f'<td class="nowrap">{escape(r["milestone"] or "-")}</td>'
        f"<td>{escape(r['author'])}</td>"
        f"<td>{reviewers_cell(r)}</td>"
        f'<td class="nowrap">{escape(r["latest"])}</td>'
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
        if url.path != "/":
            self.send_error(404)
            return
        query = parse_qs(url.query)
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

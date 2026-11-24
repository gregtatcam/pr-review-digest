PR review digest
================

Shows the open pull requests in a set of GitHub repositories that you
authored or are reviewing, with each reviewer's status, the milestone,
linked Jira tickets, and warnings for merge conflicts and failed CI.

Two front ends share the same code:

  my_digest.sh       prints the table in the terminal
  my_digest_web.sh   serves the table as a local web page with filter controls

A third script, digest.py, posts a team-wide table to Slack once a day from
a GitHub Actions workflow (see "Team digest on Slack" below).

Files
-----

  my_digest.py        the tool: fetching, filtering, and the text/Markdown output
  my_digest.sh        wrapper that sets GH_TOKEN and runs my_digest.py
  my_digest_web.py    local web server around my_digest.py
  my_digest_web.sh    wrapper that sets GH_TOKEN and runs my_digest_web.py
  review_effort.py    the review effort estimate, used by all three scripts
  digest.py           the team digest posted to Slack by the workflow
  team.txt.example    template for the team list digest.py uses
  .github/workflows/digest.yml
                      the workflow that runs digest.py daily

Keep the files in one directory; the web server imports my_digest.py from
its own directory, and every script imports review_effort.py from there.

Requirements
------------

  - Python 3.9 or later. Only the standard library is used.
  - A GitHub token with read access to pull requests in the repositories
    you scan (for a private repository the token must be able to see it).

The wrappers take the token from the GitHub CLI, so the easiest setup is:

  brew install gh        (or see https://cli.github.com)
  gh auth login

Alternatively set GH_TOKEN in the environment; the wrappers use it if it is
set, and the Python scripts can then be run directly.

Configuration
-------------

The repositories and the Jira settings come from the environment, e.g. in
~/.zprofile:

  export GH_REPOS='octo-org/widgets,octo-org/widgets-private'

Optional, for the Jira tickets column:

  export JIRA_URL='https://example.atlassian.net'
  export JIRA_PROJECTS='PROJ,OTHER'

GH_REPOS lists the repositories to scan as owner/name, comma separated.
JIRA_URL is the Jira site: links to its tickets in a PR description are
shown with the PR. JIRA_PROJECTS lists the project keys whose ticket keys
(PROJ-123, or PROJ123 without the hyphen) in a PR title or branch name are
shown. When a PR has no ticket link and no key in its title or branch, the
keys named in its description are shown instead. Without the Jira settings
no tickets are shown.

Optional, for the review effort estimate (see "Review effort" below):

  export REVIEW_HARD_PATHS='src/core,ledger,Number'

Code files whose path contains one of these, ignoring case, count 1.5
times as much, for the parts of the code base that are slow to review.

Optional, for the web page's ticket summary popup (the "i" icon next to a
ticket), which reads the ticket from Jira:

  export JIRA_EMAIL='me@example.com'
  export JIRA_TOKEN='<a Jira API token>'

The popup shows a few sentences on what the ticket is about, written by the
claude command (Claude Code, https://claude.com/claude-code), which must be
installed and logged in. The ticket's title and description are sent to
Claude for this. A summary is written once, the first time a ticket's popup
opens, and saved in ~/.cache/jira-digest/summaries.json; it isn't rewritten
when the ticket changes, so delete the ticket's entry there (or the file) to
have it written again. Without the claude command the popup shows the ticket
description as is.

The saved summaries are shared with jira-digest
(https://github.com/gregtatcam/jira-digest), which shows them too; either
one writes a summary the other then reuses. A ticket that jira-digest has
already fetched (~/.cache/jira-digest/tickets.json) is summarized from that
copy, without asking Jira, so JIRA_EMAIL and JIRA_TOKEN are only needed for
the other tickets. ticket_summary.py is the same file in both projects.

Running in the terminal
-----------------------

  ./my_digest.sh [options]

With no options, it lists the open PRs you authored or are a reviewer on.
"You" is the owner of the token.

Options:

  --user LOGIN        Who "me" is (default: the token owner). Use this to
                      look at somebody else's PRs.
  --author [A,B]      Only PRs by these GitHub logins. With no list, "me".
  --reviewer [A,B]    Only PRs where one of these logins is a reviewer,
                      whether requested or already reviewed. With no list,
                      "me". With both --author and --reviewer, PRs matching
                      either are shown.
  --milestone A,B     Only PRs whose milestone is one of these, e.g.
                      --milestone "2.5.0,2.6.0". Case doesn't matter.
  --date m-d-yy       Only PRs opened on or after this date, e.g. --date 10-5-26.
  --in A,B            Only PRs whose title contains one of these words,
                      ignoring case and matching inside words.
  --not-in A,B        Skip PRs whose title contains any of these words,
                      matched the same way. --not-in wins if both match.
  --not-approved      Skip PRs that every listed reviewer has approved, even
                      if the PR changed since, and skip the listed authors'
                      PRs that already have two or more approvals.
  --markdown          Print a Markdown table instead of the text table, e.g.
                      to paste into a Slack canvas.
  --force             Fetch from GitHub even if the cached data is under
                      4 hours old (see "Caching" below).
  --by-effort         List the PRs that are easiest to review first (see
                      "Review effort" below).

Examples:

  ./my_digest.sh                              my PRs and the PRs I review
  ./my_digest.sh --author                     only PRs I wrote
  ./my_digest.sh --reviewer --not-approved    PRs waiting for my review
  ./my_digest.sh --reviewer --not-approved --by-effort
                                              the same, easiest first
  ./my_digest.sh --reviewer alice,bob        PRs alice or bob are reviewing
  ./my_digest.sh --milestone 3.0.0 --date 9-1-26
  ./my_digest.sh --user alice --markdown      alice's PRs, as Markdown

Reading the table:

  Column "PR" shows the repo and number, the branch, any problems (conflict,
  CI failed), the title, linked Jira tickets, and then the PR's size and
  review effort: the number of files changed, the TLC (total lines changed,
  added plus deleted), and the effort from 1 to 5, e.g. "12 files, 345 TLC,
  effort 3/5". The line below says what the effort was made from, e.g.
  "180 code lines in 2 files, 165 test lines". In iTerm2, Ghostty, WezTerm,
  kitty, and the VS Code terminal, the PR number is a clickable link; in other
  terminals the URL is printed on its own line.

  Column "Reviewers" shows one line per reviewer with the date of their
  latest review:

    green    approved
    yellow   approved, but the PR was updated since
    orange   reviewed (commented or requested changes), not approved
    red      review requested, nothing submitted yet

Running the web page
--------------------

  ./my_digest_web.sh [--port N] [--no-browser]

This starts a server on http://127.0.0.1:8765/ (change with --port) and
opens it in your browser. Only your own machine can connect. Stop it with
Ctrl-C.

The page has the same filters as the command line options: user, author,
reviewer, milestone check boxes, date, title contains / doesn't contain,
and "not approved". Check boxes and the date apply immediately; after
typing in a text field press "Apply". "Force refresh" fetches new data from
GitHub regardless of the cache.

Clicking a column header sorts the table by that column; clicking it again
reverses the order. "PR" sorts by repository and number, "Effort" by the
estimated review effort (easiest first, and PRs with the same score in
order too), "Reviewers" by how many reviewers haven't approved yet, and
"Latest activity" by date. The
chosen sort is remembered in the browser, so it stays when a filter reloads
the page.

Clicking the PR number (e.g. "widgets#123") opens the PR on GitHub in a
new tab. The icon after the branch name copies the branch name to the
clipboard. The "i" icon after the title pops up the PR's description;
click it again, click elsewhere, or press Escape to close it.

Review effort
-------------

Each PR gets an effort score from 1 (easiest to review) to 5 (hardest). It
is a guess made from the list of changed files, without reading the diff:

  - Lines of code count in full, test lines at 30%, and lines of docs,
    build, and CI files at 10%.
  - A deleted line counts half of an added one. Renamed and moved files
    count only for the lines that changed in them.
  - The same lines spread over more code files score higher.
  - Code under one of the REVIEW_HARD_PATHS counts 1.5 times.
  - Many code files with a few lines changed in each, as many added as
    deleted, look like a mechanical change (a rename, say) and count 40%.
  - A PR with next to no description counts 15% more.
  - GitHub lists the first 100 files of a PR; a bigger PR is scaled up
    from those.

In my_digest and on the web page the score is for you ("Me"): a PR you
reviewed that was updated since counts half ("updated since review"), one
with no commits since your latest review scores 1 ("reviewed, no changes"),
and so does one whose current commit you approved. The team digest shows the PR's own score.

The total of all this is the PR's "weighted lines": up to 60 scores 1, up
to 200 scores 2, up to 500 scores 3, up to 1200 scores 4, and more scores
5. The web page shows the number next to the score and sorts the Effort
column by it, so PRs with the same score are in order too; --by-effort
sorts by it as well.

The score can't tell a refactoring that moves code around from one that
changes what the code does, so read it as "how much is there to read", not
"how risky is it". The weights and the thresholds are at the top of
review_effort.py.

Caching
-------

Fetching takes roughly 10 to 15 seconds, so the data is saved in
~/.cache/pr-review-digest/prs.json and reused for 4 hours. Different
options reuse the same cached data, so changing filters is instant. Use
--force (or "Force refresh" on the web page) when you want the latest state.

Team digest on Slack
--------------------

digest.py posts one table of the team's open PRs (those authored or reviewed
by a team member) to a Slack channel. The workflow in
.github/workflows/digest.yml runs it every day at 13:00 UTC and on demand
from the Actions tab. It reads these from the environment:

  GH_REPOS, JIRA_URL, JIRA_PROJECTS   as above
  GH_TOKEN                            a token that can read the repositories
  TEAM                                the team's GitHub logins, comma
                                      separated; or put them one per line
                                      in team.txt next to digest.py (copy
                                      team.txt.example; git ignores
                                      team.txt), or point TEAM_FILE at
                                      such a file
  SLACK_BOT_TOKEN, SLACK_CHANNEL_ID   a bot token with chat:write and the
                                      channel it posts to (the bot must be
                                      a member); or
  SLACK_WEBHOOK_URL                   an incoming webhook, used when there
                                      is no bot token

With neither Slack setting the table is printed, and with --markdown a
Markdown table is printed instead and nothing is posted, so

  GH_REPOS=octo-org/widgets TEAM=alice,bob python3 digest.py

previews the digest locally (GH_TOKEN from "gh auth token").

In the GitHub repository that holds the workflow, set the non-secret values
as Actions variables and the rest as secrets (Settings > Secrets and
variables > Actions):

  Variables   GH_REPOS, TEAM, and optionally JIRA_URL and JIRA_PROJECTS
  Secrets     GH_READ_TOKEN (becomes GH_TOKEN), SLACK_BOT_TOKEN,
              SLACK_CHANNEL_ID, and/or SLACK_WEBHOOK_URL

Customizing
-----------

Constants near the top of my_digest.py (and digest.py) control the rest:

  CACHE_MAX_AGE     how long fetched data is reused
  THREADS           how many GitHub requests run at once
  MAX_ROWS,         the Slack table limits
  MAX_CHARS

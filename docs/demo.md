# Demo script

A five-minute walkthrough that shows every part of issue-sync, for a screen recording or
a live call. Each step says what to do, what the audience should see, and the point to
make.

## Before you start

- The stack is running with the tunnel (`docs/deployment.md`), the Apps Script trigger
  is installed, and the Jira webhook is registered.
- Open five windows side by side: the Google Sheet (with a linked Google Form), the Jira
  board, the Slack channel, the dashboard (`/dashboard`), and a terminal with
  `docker compose logs -f worker`.
- Clear old demo rows, or start from a copy of the sheet. Prepare the text for step 1 in
  a note so you can paste it cell by cell.

## 1. A request typed into the sheet (≈ 60 s)

**Do:** In a new row, type only the title, "Invoice PDF shows last year's VAT rate", and
press Enter.

**See:** Within a few seconds the Status cell says *Waiting for: Description*. No ticket
yet.

**Do:** Fill in the description ("Invoices generated today still use 19% instead of
20%. They go out tonight.") and a reporter.

**See:** The note disappears. About two minutes later the row gets a System ID and a
Detail link, and a ticket appears in Jira.

**Say:** *"People fill rows one cell at a time. The AI judges a request once, so the
system waits until the row is complete and has stopped changing. It never triages half a
sentence."*

## 2. A request from the form (≈ 30 s)

**Do:** Submit the Google Form: "VPN access for a new contractor", with a file attached.

**See:** The ticket is created at once. There is no wait, because a form writes the
whole row in one go.

**Say:** *"For requesters outside the team, a form is the cleanest intake. File uploads
arrive as Drive links and are listed in the ticket."*

## 3. AI triage, rules and the notification (≈ 60 s)

**Do:** Open the two new tickets in Jira.

**See:**
- The description carries the request, an *AI triage* section (category, urgency,
  suggested team, summary, reason, model) and a link to the detail page.
- The VAT ticket has a High or Highest priority and the backend assignee.
- The VPN ticket was suggested for *support*, but has the *ops* assignee and a comment
  "Assignment: Rule 'Access requests go to ops': team support -> ops".
- Slack shows one message per ticket that @mentions the assignee.

**Say:** *"The LLM suggests and deterministic rules have the last word. Both are kept,
so we can measure the model separately from the rules."*

## 4. Work happens in Jira, and the sheet follows (≈ 60 s)

**Do:** On the VAT ticket, move it to *In Progress*, change the priority to *Highest*,
add a comment "Hotfixing the tax table now", and attach a screenshot.

**See:** In the sheet, Status becomes *In progress* and Latest note shows the comment,
with the date and author. The worker log shows the events being applied.

**Say:** *"Each side owns its fields. The sheet owns what was asked, and Jira owns how it
is being handled. Changes by the system's own account are ignored, so nothing loops."*

## 5. The requester's view (≈ 30 s)

**Do:** Click the Detail link in the sheet, in a private window.

**See:** No sign-in. The page shows the request, the AI's reasoning, the attachments
from the sheet and from Jira, and the full timeline. It does not show the reporter's
contact details.

**Say:** *"Anyone with the link can follow progress without a Jira licence. The link is
a random UUID; the page is not indexed and leaks nothing through referrers."*

## 6. The dashboard (≈ 45 s)

**Do:** Open `/dashboard`, then filter *AI vs. Jira → Differs from AI*. Search
"invoice", then tick *Needs attention*.

**See:** The VAT ticket shows *Changed* for priority, because a person raised it. The
VPN ticket shows *Rule* for the assignee. The table at the top gives the agreement rate
per model.

**Say:** *"This is how you decide whether the model is good enough, or whether to switch
models. Every judgement records which model made it."*

## 7. Self-healing (≈ 45 s)

**Do:** In the sheet, overwrite a Status cell by hand with "Done!!", and delete another
row entirely. Then run `docker compose exec worker issue-sync reconcile`, or wait for
the next scheduled run.

**See:** The Status cell is corrected back to the real status within a minute. The
deleted row's record shows *Row deleted* on the dashboard. Its Jira ticket is untouched.

**Say:** *"Webhooks get lost and people edit the wrong cells. Every 15 minutes the
system compares both sides and repairs what drifted. It never deletes tickets, and
anything it cannot fix goes to a Slack alert channel."*

## Questions that usually come up

- **What if Jira is down?** Pushes retry with backoff. After repeated failures the
  record shows *Sync failed* on the dashboard and an alert is posted. Nothing is lost:
  the next run picks it up.
- **Can it create duplicate tickets?** No. Pushes for one row are serialized by a row
  lock, tickets are labelled with the row's UUID, and a timed-out create is looked up
  before it is retried.
- **Which LLM?** Any OpenAI-compatible API, configured by three environment variables.
  `issue-sync eval-classifier` measures accuracy on labelled cases before you switch.
- **Another ticket system?** The sync logic talks to a small `TicketBackend` interface.
  Jira is the only implementation today.

# WG request status

`fusion-addins/WGLink/wglink_protocol.py` owns this contract: its constants,
`validate_request_status`, `request_status_terminal` and `request_status_text`.
Waveguide Generator vendors that file byte for byte (its
`scripts/vendor_wglink_protocol.py` copies it from a committed WGLink commit and
records the commit, path and SHA-256 in `server/cadlink/wglink_protocol.source.json`).
Change it here first, commit, then vendor.

## Two files per request

WG answers every Send or Solve in `<ipc>/.wg-solve-acks/`:

- `<requestId>.json` is the **receipt** (schema 1). It says only that WG accepted or
  refused the delivery, and it is unchanged. WGLink 0.3.2 and older read only this file
  and reject any other `schemaVersion`.
- `<requestId>.status.json` is the **request status** (contract `wg-request-status`,
  version 1). WG replaces it atomically as the request moves on, from its one job
  owner, including after a WG restart.

WG advertises `requestStatus: 1` in `wg-capabilities.json` while it writes status
files (only while its request consumer runs). An add-in reads status files only when
it saw that capability, and records the capability on the request when it submits it,
so a capability file that is briefly missing never downgrades a request it is
following.

## The record

```
{ contract: "wg-request-status", schemaVersion: 1, seq,          // seq +1 per write
  requestId, operationId, kind: "prepare_and_solve"|"receive_snapshot",
  manifestSha256, documentName, updatedAt,
  state: accepted|preparing|needsYou|queued|solving|solved|couldNotFinish|cancelled|refused,
  phase?: "preparing"|"solving",                            // couldNotFinish
  progress?: {fraction, frequency?: {done, total, channel?: {index, count}}},  // solving
  prompt?: {code, nextStep, options: [{id, label}], answerIn: "wg"},          // needsYou
  failure?: {recorded, message?, nextStep?},                // couldNotFinish
  cancelReason?: "stopped"|"replaced"|"dismissed",          // cancelled
  result?: {jobId, runNumber, mesh?: {trianglesSolved, solvedFraction, domainKind}},  // solved or needsYou(mesh_too_large)
  job?: {jobId, runNumber},                                 // the current job of the request
  notice?: "fullModel"|"cutModel"|"healed",                 // solved, calm one-liners
  settings?: {frequencyStartHz?, frequencyEndHz?, frequencyCount?,
              frequencySpacing?: "log"|"linear"} }          // the sweep this request's solve uses
```

- `requestId` = `operationId` = the request's `commandId`. Records are per request, so a
  late write for one request never changes another. `seq` rises with every write
  (WG uses the wall clock in milliseconds as its floor, so it keeps rising even when a
  previous file could not be read); a reader ignores a write with a lower `seq`. A `seq`
  that would reach `REQUEST_STATUS_SEQ_LIMIT` stays just below it, so a reader never
  ignores a write (an equal `seq` is not older). The guard
  against internal words (`user_text_violations`) leaves the document's own name out.
- `accepted` is the end of a Send ("open in WG"). A Solve goes `preparing` → `queued`
  → `solving` → `solved`, or stops at `needsYou`, `couldNotFinish` or `cancelled`.
  `refused` mirrors a refused receipt.
- `needsYou` is a real question or something only the user can do in WG; it is answered
  in WG's pop-up. `prompt.code` is a stable code for the add-in's own remedies:
  `mesh_too_large` also offers Sources & Mesh… in Fusion. `nextStep` is WG's own
  sentence. When meshing reached the triangle ceiling, `result.mesh` carries
  the actual count and solved domain even though there is no published ingest
  yet. Unknown domain evidence is omitted rather than inferred from the count.
- `couldNotFinish` with `failure.recorded` is WG's own failure: WG has a developer
  record of it, and the user reads only the calm sentence from `request_status_text`
  (`could_not_prepare_text` is the one wording of the preparation case, in WG's own
  UI too). WG sets `recorded` only for failures it records for the developers: where
  they were diagnosed, or when it sees the request change into one. A failure that
  happened before WG watched the request (a WG older than this file, or a status file
  that was lost) still reads `recorded` but may have no record of its own. Without `recorded`,
  `failure.message` and `failure.nextStep` are WG's own words for what the user can do
  (for example "Send it again from Fusion.", or, when WG closed while preparing,
  "Solve it again."); only failures the user can act on carry them.
  Try again lives in WG; a request tried again there moves back to `preparing`.
- `accepted`, `solved`, `couldNotFinish`, `cancelled` and `refused` are terminal
  (`request_status_terminal`): nothing more happens unless someone acts.
- The record never carries reason text, an identifier inside text, a path or a
  traceback. `jobId` and `runNumber` are for navigation only.
- `settings` is the frequency sweep of the setup the request's solve was prepared
  with, once WG has chosen it. A field WG cannot state (a setup with an explicit
  frequency list has no spacing) is left out; a reader drops a field it cannot use.

## Solve settings from Fusion

WG owns every solve setting: it records a setup per model and source inventory
and solves with the latest. WG advertises `solveSettings: 1` when it reads a
Solve request's optional `solveSettings` (the same four fields as the status
record's `settings`, any subset, checked by `request_solve_settings`). Before
preparing that Solve, WG applies the fields to the model's current setup (WG's
defaults for a first-time model), records the result as the model's setup -- the
user's own choice, exactly as an edit in WG's CAD workspace -- and prepares with
it. A field that changes the start, end or count replaces an explicit frequency
list. Settings WG cannot use refuse the preparation with a calm sentence; they
never fall back silently. Any changed sweep field replaces an explicit
frequency list with the requested grid, retaining its endpoints and count
where those fields were not changed.

The panel shows, per field, what the document's newest solve used (the status
record's `settings`, else what the add-in sent with it) and holds only the edits
not yet sent, in memory, per document. A Solve carries them and the panel then
forgets them; a Send never carries them. The panel's settings appear only for a
WG that advertises the capability.

## WG's rhythm, and WG stopping

While WG runs, it rewrites the status of every request it has not finished
once the file is `REQUEST_STATUS_REFRESH_SECONDS` (60 s) old, raising only
`seq` and `updatedAt`, and rewrites them all at once after a restart. It never
rewrites a finished request. On a clean shutdown WG rewrites
`wg-capabilities.json` with the same content plus `stoppedAt` (a UTC time
ending in `Z`, `CAPABILITY_STOPPED_AT`; read it with `capabilities_stopped`);
its next start writes the file without it. It never deletes the file (the
add-in's launcher reads it to start WG), and a crash leaves it unmarked.

A reader therefore counts only a higher `seq` as news. A request still under
way that has had no new `seq` for a while is one WG has gone quiet about, never
one that finished or failed. WGLink says so after 5 minutes, at once when the
capability file carries `stoppedAt`, and after 120 s (two missed rewrites) for
a request whose status WG keeps fresh, which covers a crash.

## Reading it

`validate_request_status(value, request_id=...)` returns the record a reader may use,
or `None` when the value is not this request's status (another contract or version, a
bad or foreign id, no state or seq). Unknown fields are dropped, malformed optional
fields read as absent, text loses control characters and is bounded, and any WG
sentence that trips `user_text_violations` is dropped, so a newer WG stays readable and
nothing technical reaches a user. A state the reader does not know is kept: it reads as
under way (`request_status_known` tells it apart), and `request_status_text` says to
open Waveguide Generator. Neither function, nor `request_status_terminal`, ever raises.
WG writes exactly what this validator returns.

`request_status_text(record)` returns `{headline, detail, options, actions}`: the words
a user reads, the option labels of a question, and `{id, label}` buttons (`openWg`,
`openSourcesMesh`, `showResults`). Every string it can produce passes
`user_text_violations` (`USER_TEXT_FORBIDDEN_TERMS`), which both repositories' tests
apply to user-visible text.

WGLink reads the file with `wglink_inbox.read_request_status` off Fusion's main
thread and shows it in its request palette (`wglink_requests.py`, which words
only the states WG cannot know: exporting, waiting to be picked up, and a WG
that writes no status).

## Retention

WG keeps a receipt and its status together, aged by the newer of the two. While the
status is in a known state still under way neither is deleted; a state WG does not
know is kept at most 30 days. A finished, unreadable or invalid status keeps the pair
14 days. A receipt still waiting for its first status, and a status whose receipt is
gone, are kept 14 days; a staging file 24 hours. The folder holds at most 500 requests,
the oldest finished ones going first.

## Fusion request history and opening a request

`RequestTracker` owns the followed request and its persisted list. The add-in
alone writes `<ipc>/.fusion-request-history.json`, atomically through
`wglink_status.write_status_record`: `{schema:1, entries:[...]}`. Each entry
keeps the request's document identity/name, Send/Solve kind, submission time,
capabilities at submission, local phase, receipt and validated WG status (which
includes its state, update time, job/run and mesh information when available).
There is no second request owner. The earlier `.fusion-request-status.json`
restart record is imported once and removed only after the history is durable.

History retains the newest 50 entries. Finished entries expire after 30 days;
unfinished entries remain within that cap and read **Check in WG** after 30
days. They also read calmly when their previously observed status is missing.
Restart reads all unfinished entries without submitting any request. The panel lists
the active document's requests. **Clear finished** removes only terminal entries. Polling
updates history for other documents too; a new needs-you state opens the palette.
An unclaimed request withdrawn because a later press replaced it stays in history
as cancelled/replaced.

WG advertises `showRequest: 1` only while its delivery consumer runs. Pressing
Open/Show results writes `.wg-show-requests/<requestId>.json` with
`{schemaVersion:1, requestId, jobId?, at}` using the same atomic writer. Repeated
presses for one request replace its pending pointer. WG resolves the current job
of the request, publishes one `cadShowRequest` jobs-channel event containing
`operationId` and, when available, `jobId`, then deletes that press's file. A
newer press is left for the next pass. A headless backend keeps the file until
a jobs-channel subscriber can hear it, so desktop startup cannot lose the choice.

The add-in uses the existing installed-backend admission and application-front
launcher. WG versions without the capability receive no show file and the
palette says **Switch to Waveguide Generator**. WG's existing Fusion display
owner handles the event; completed runs use its guarded `?job=` selection path.
Later navigation, model selection, document loads and edits still win.

History recovery restores readable entries before checking WG or Fusion. Legacy
record deletion is best effort and logged. A session whose recovery was incomplete
merges its next write with disk. Valid JSON with an unknown schema stays
read-only, with the shared calm explanation. Requests made before the first document save
are remapped from the root component's local identity to its saved identity.
Polling persists all changes in one write per tick; the list is not a live region.

Explicit history selection remains selected across socket snapshots and older
Fusion arrivals. A CAD Solve created after the explicit show releases it, except
for the shown request's own retry chain. Navigation, edits, a new model/document
choice or a new Send also releases it. New Send returns remain eligible for
discovery even when the receipt arrives first. Navigation pointers expire after two minutes. WG retains
the latest pointer for replay to a newly connected window, and its socket client
holds the pointer until the display owner mounts. Unavailable requests use the
shared history text, never a raw job lookup error.

History recovery keeps valid JSON with an unknown schema unchanged and read-only.
Malformed, empty or structurally damaged history is renamed with a `.corrupt-<timestamp>` suffix and new
history starts fresh, with a calm notice in Requests. A recovery or history-write
failure after reading entries keeps those entries in memory and still arms status
polling. Clear finished also removes terminal disk entries when recovery is incomplete.

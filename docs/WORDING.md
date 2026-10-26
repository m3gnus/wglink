# Fusion wording

Lead with what happened to the user's model and give one next step. Keep
technical evidence in the log or the progress panel's Diagnostics disclosure.
Use “could not” rather than “refused”. User-fixable states do not say “failed”
or use red.

The command descriptions are:

- **Sources & Mesh…**: “Set up sources and mesh, then Send or Solve”
- **Send to WG**: “Open this model in WG to inspect or set up. Does not solve.”
- **Solve in WG**: “Send and solve now.”

Do not display transport or implementation vocabulary: bundle, return, CAD
return, `.wgreturn`, manifest, schema, request id, Return ID, UUIDs, inbox,
IPC, acknowledgement, selector, `entity_token`, `options['…']`, traceback,
main thread, event, gate, ingest, operation, lineage, finding ids, raw role
constants, UTF-8 or byte limits, determinant, orthonormal or chirality.
“Text Commands” belongs only in an explicit details link inside Diagnostics.
The user's own document and model names are exempt from the vocabulary guard.

`fusion-addins/WGLink/wglink_text.py` owns the Fusion message boundary,
failure categories, remedies and command wording. `UserFacing(code, text,
next_step)` marks deliberate, actionable wording. `problem(code, detail)`
retains the original diagnostic without displaying it. Every message/modal
uses `user_message`; other exceptions show “Waveguide Generator could not
<action> <document>. This has been recorded.” The log retains the exception
chain, stable diagnostic code and current request id when one exists.
Headless callers still receive the original diagnostic from `str(exc)`.
Known link errors without a more specific category also ask the user to review
the model in Fusion. Incomplete or unverified updates say to use Undo.

Shared preflight rules, driver labels and progress text remain owned by
`wglink_protocol.py`, which WG vendors byte for byte. Keep schema versions,
role ids, validation and canonical payloads unchanged when editing prose.
Reuse its `user_text_violations` guard rather than adding another word list.
Words such as “return” and “options” are checked in their technical context,
so ordinary instructions such as “return to Fusion” remain visible.

`tests/test_wglink_text.py` exercises every failure category and three
internal exception types through Fusion FakeUI. Existing export, preflight,
launcher and delivery tests verify the checks still happen at the same point.
The progress panel keeps its existing layout and lifecycle.

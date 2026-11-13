# Taskvalet notebook handoff

Optional Glitnir provider package for sending a saved notebook as an unqueued
Taskvalet request. It does not launch a swarm, upload dependencies, insert proofs,
or change checked claim status. The owner prepares research assignments and
previews/starts any campaign on the Taskvalet board.

Install the wheel with **Glitnir: Install Extension Package…** (Choose wheel).
This package requires Glitnir 0.1.16 or newer, which provides the handoff API.
Configure the existing Taskvalet HTTPS server and project settings, then choose
**More → Send to Taskvalet…** in the notebook. This entry appears when the plugin
is installed in that notebook’s Python environment. It opens the review panel,
without submitting. Alternatively, invoke **Glitnir: Send Notebook…** and select
Taskvalet. Review the destination and saved
notebook source, enter a research objective, then choose **Submit request**.
Provide a project-registered agent credential in the masked VS Code prompt if needed. The handoff credential is kept separately from compute
credentials in VS Code SecretStorage. Owner, machine, reader and compute-only
credentials are not supported by this submission flow.

The panel keeps the source and objective visible after submission, shows the
request task status, and provides **Refresh status** and **Open request**. Status
refresh reads only that request task, never campaign controls. Failed refreshes
show Unknown with an error; a recorded status includes its last-check time.

Review the complete source and objective before submitting. The request contains
only the saved notebook, its digest and your objective; imported sources and
compiled environments are not uploaded or asserted to be checked. No saved proof
status is transferred. The task is a request awaiting owner setup and launch,
not a runnable research assignment or proof result.

Use **Glitnir: Set Notebook Handoff Credential…** to replace a mistyped or expired credential. Pending recovery still requires the original credential identity.

A saved pending envelope blocks a second submission. A definite rejection of the first submission is archived; a new submission requires a fresh preview. Once an outcome is uncertain, later rejections never retire its envelope: operator reconciliation may be needed. Separate editor windows keep separate recovery files. **Glitnir: Recover Notebook
Handoff…** reopens pending or last-submitted requests in the panel.
**Retry saved request** explicitly retries the original request and credential identity; nothing
retries on reload. If credentials are revoked, restore access to the original
identity or reconcile the saved request with the operator. Do not create a new
request to work around an uncertain result. The board link is the authoritative
place to inspect the task. Direct campaign controls remain gated by task 410.


Submitted requests remain in the notebook’s **Jobs** drawer, including after a
reload. **View request** reopens their source-bound review panel. While Glitnir
is active it polls one saved request each minute, rotating through the retained
requests; it uses only an existing stored credential and never automatically
submits, recovers, launches or stops work. Missing credentials and failed reads
leave status unknown. Use **Refresh status** for an immediate read.

On an updated Taskvalet server, choose **Create research from this request** on
the request’s board details. The research form preserves the originating request
and Taskvalet records a fingerprint of its Context. Revisions keep that origin.
This requires a server-owned SQLite research project. Existing investigations
without an origin are not guessed or retroactively linked.

Glitnir reads linked statements, attributed findings, summaries, checks and
assignment states. External artifact references are displayed with their pinned
revision; Glitnir does not fetch or execute them. New classified findings notify
once per result version; **Mark reviewed locally** only records local review.
Results remain proposals and never accept tasks or update Lean source/proof status.
The panel warns if the local notebook has changed or cannot be checked. Older
requests without a saved local path report source freshness as unknown.

Results are a bounded preview; open the board for the full record, campaign
controls and any reported resource usage. On an older or unsupported server,
request tracking still works and the panel explains that results are unavailable.
Background polling runs only while the extension is active, not as a system service.

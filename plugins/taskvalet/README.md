# Taskvalet notebook handoff

Optional Glitnir provider package for sending a saved notebook as an unqueued
Taskvalet request. It does not launch a swarm, upload dependencies, insert proofs,
or change checked claim status. The owner prepares research assignments and
previews/starts any campaign on the Taskvalet board.

Install the wheel with **Glitnir: Install Extension Package…** (Choose wheel).
This package requires Glitnir 0.1.16 or newer, which provides the handoff API.
Configure the existing Taskvalet HTTPS server and project settings, then invoke
**Glitnir: Send Notebook…**. Select Taskvalet and provide a project-registered agent
credential when prompted. The handoff credential is kept separately from compute
credentials in VS Code SecretStorage. Owner, machine, reader and compute-only
credentials are not supported by this submission flow.

Review the complete source and objective before submitting. The request contains
only the saved notebook, its digest and your objective; imported sources and
compiled environments are not uploaded or asserted to be checked. No saved proof
status is transferred. The task is a request awaiting owner setup and launch,
not a runnable research assignment or proof result.

Use **Glitnir: Set Notebook Handoff Credential…** to replace a mistyped or expired credential. Pending recovery still requires the original credential identity.

A saved pending envelope blocks a second submission. A definite rejection of the first submission is archived; a new submission requires a fresh preview. Once an outcome is uncertain, later rejections never retire its envelope: operator reconciliation may be needed. Separate editor windows keep separate recovery files. **Glitnir: Recover Notebook
Handoff…** explicitly retries that exact request and credential identity; nothing
retries on reload. If credentials are revoked, restore access to the original
identity or reconcile the saved request with the operator. Do not create a new
request to work around an uncertain result. The board link is the authoritative
place to inspect the task. Direct campaign controls remain gated by task 410.

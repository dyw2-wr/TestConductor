# Security

TestConductor is a passwordless, local developer tool. Its middleware accepts
only loopback clients, and the launcher binds to `127.0.0.1` by default. Do not
remove that boundary or expose the Django development server to another machine.

Keep model API keys, database credentials, Milvus tokens, runtime variables,
uploaded requirements, generated artifacts, and local databases out of Git.
Use `.env.example` only as a field reference.

Report vulnerabilities through GitHub private vulnerability reporting. Include
the affected revision, reproduction steps, impact, and any suggested mitigation.
Do not include live credentials or private test data in a report.

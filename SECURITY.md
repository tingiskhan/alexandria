# Security

## Reporting a vulnerability

Please don't open a public issue for security problems. Report them privately through [GitHub's vulnerability reporting](https://github.com/tingiskhan/alexandria/security/advisories/new) instead. You should get a reply within a week. Once a fix is released, the advisory is published with credit to you, unless you'd rather stay anonymous.

## Security model

Alexandria is built for a single user or a trusted team on a private network. Keep these limits in mind when you deploy it:

- **No access control.** Anyone who can reach the database, or connect to the MCP server, can read every document.
- **The HTTP transport has no authentication.** `alexandria-mcp --transport streamable-http` binds to `127.0.0.1` (`ALEXANDRIA_MCP_HOST`). Don't expose it beyond localhost unless an authenticating reverse proxy is in front of it.
- **The Prefect UI and API have no authentication either.** Anyone who reaches them can start syncs and read flow logs, which include file paths.
- **The Docker deployment (`deploy/compose.yaml`)** publishes the Prefect UI (`4200`) and the MCP server (`8000`) on `127.0.0.1` only, and doesn't publish Postgres at all. Inside the containers the MCP server listens on `0.0.0.0`, so other containers on the same Docker network can reach it. Keep the ports bound to localhost unless a proxy with authentication is in front. The images run as a non-root user, and `.env` is excluded from the build context, so it never ends up in an image.
- **Credentials** (database URL, S3 secret key, Dropbox refresh token, SharePoint client secret) are read from the environment, `.env` or `/run/secrets`, and are kept as `SecretStr`, so they don't show up in logs. Keep `.env` out of version control (it's in `.gitignore`), and use read-only credentials for the source: Alexandria only lists and downloads files. For SharePoint, prefer `Sites.Selected` with a `read` grant on the one site over tenant-wide `Files.Read.All`.
- **Document content reaches an LLM.** Text from your PDFs is returned to the agent as tool output. A PDF can contain text written to manipulate an agent (prompt injection), so give the agent only tools and permissions you'd be comfortable with a document influencing.
- **The root `compose.yaml` is for local testing.** It takes its passwords from `.env` and has none of its own, but it runs RustFS with a single admin key that Alexandria also uses. Anywhere else, give Alexandria its own read-only key.

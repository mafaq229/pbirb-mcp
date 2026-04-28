# Security policy

## Reporting a vulnerability

Please report security issues privately via GitHub's
[private vulnerability reporting](https://github.com/mafaq229/pbirb-mcp/security/advisories/new)
rather than a public issue.

Expect an initial acknowledgement within roughly 7 days. This is a
single-author project maintained in spare time, so response windows are
best-effort — please don't disclose publicly while a fix is in progress.

## Supported versions

Only the latest published version on PyPI receives security fixes. While
the project is pre-1.0, that is whichever `0.x.y` is current. After v1.0,
the latest MINOR receives fixes; older MINORs are out of support.

## Threat model

`pbirb-mcp` is an MCP server that reads and writes `.rdl` files on the
local filesystem. The intended deployment is a single-user host where an
LLM client (Claude Desktop, Claude Code, etc.) drives it via JSON-RPC
over stdio.

**In scope:**

- Path traversal or unintended file writes outside paths the LLM was
  asked to operate on.
- Malformed RDL input that crashes the server or produces a corrupt file.
- Memory or CPU exhaustion via crafted RDL input.
- XML external entity (XXE) attacks via untrusted `.rdl` content. The
  parser is configured with `resolve_entities=False`; report any bypass.

**Out of scope:**

- Network attacks. The server has no network code; it does not connect
  to Power BI XMLA endpoints, does not authenticate, and does not handle
  credentials. `set_datasource_connection` writes a connection string
  into the RDL — it does not establish a connection.
- Vulnerabilities in Power BI Report Builder itself (please report
  those to Microsoft).
- Vulnerabilities in third-party MCP clients (please report those to
  the client vendor).
- Prompt injection via RDL content surfaced to an LLM. This is an
  application-layer concern handled by the calling LLM client. Future
  read-back tools may add explicit annotation of potentially-injected
  content; track [`#1`](https://github.com/mafaq229/pbirb-mcp/issues) for
  status.

## What gets a CVE

A finding is treated as a security issue (CVE-eligible) if it allows:

- Reading or writing files the LLM didn't ask for.
- Crashing the server with attacker-controlled RDL content.
- XXE via the lxml parser used by `RDLDocument.open`.

Functional bugs in tools (e.g., a wrong XPath that produces an
ill-formed but locally-scoped RDL) are normal bug reports, not security
issues.

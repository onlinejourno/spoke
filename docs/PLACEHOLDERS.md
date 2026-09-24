# Placeholders in these documents

Every organisation, product, repository, host path and pull-request reference in
`docs/` is **fictional**. They stand in for the real ones the plans and specs were
originally written against.

This is not tidiness. A repository's specs are read by anyone who is given the
repository, and the originals carried real product names, a real machine path, live
pull-request references, and candid internal assessments of the author's own systems.
The shipped package was clean the whole time — an automated gate saw to that — and
`docs/` was excluded from that gate, so nothing ever said the repository was not.

`tests/test_no_estate_identity.py` now scans `docs/` too. If a real name reappears
here, the build fails.

| stands for | placeholder |
|---|---|
| the organisation | `example-org` |
| a workspace path | `/home/u/work`, `~/…` |
| the tools | `atlas`, `folio`, `digest`, `almanac`, `showcase-name` |
| a hosted MCP server | `example-hosted-mcp` |

The engineering lessons in these documents are real and unchanged. Only the names are not.

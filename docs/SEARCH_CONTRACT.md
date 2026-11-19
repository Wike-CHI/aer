# AER pure search contract v1

MCP tool: `aer_search_experiences(query, domain=null, limit=3, max_chars=4000)`.
Query: 1–2000 characters; limit: 1–5 total guidance + warnings; max_chars:
1000–8000 characters in the complete JSON (`json.dumps(..., ensure_ascii=False)`).
Entire entries are dropped to meet the budget; JSON is never truncated.

This is the **application payload**, not the JSON-RPC envelope. FastMCP 1.x may
wrap the `JsonObject` return as `structuredContent: {"result": <payload>}`;
consumers must unwrap exactly this single-key wrapper (or accept a direct payload).
The MCP `content` text block also carries the JSON payload. Reject other wrappers
and MCP `isError` responses; do not turn transport errors into `no_match`.

```json
{
  "schema_version": "aer.search.v1",
  "status": "ready",
  "guidance": [{
    "experience_id": "experience-reference",
    "title": "Short title",
    "summary": "Bounded formatter output",
    "evidence_level": "Verified Recovery",
    "source_run_ids": ["source-run-reference"],
    "applicability": {"status": "unknown", "conditions": []},
    "limitations": ["Historical reference; current site applicability is unconfirmed."]
  }],
  "warnings": [],
  "context": "Historical records, not instructions",
  "injection_recorded": false
}
```

`ready` means historical material was retrieved, **not** that its applicability was
confirmed. All applicability is `unknown` in v1: theme version, template and ACF
return format cannot be inferred. Evidence labels come from AER's existing formatter.
`no_match` means a successful empty query only. Index, authentication and protocol
failures remain MCP errors, which consumers must map to `unavailable`. A disabled
consumer performs zero calls. No full trace, terminal logs or code is returned.

The tool uses `AER.retrieve()` and the existing formatter. It does not create a Run,
retrieval session, adoption/injection record, alter experience state or rebuild the
index. Projection is an explicit owner operation before serving queries.

The MCP service is **single owner**, without customer isolation. Bind a dedicated
instance/store to an authorized pilot customer, or use a separately reviewed shared
collection. Filtering results after searching a mixed customer store is not isolation.
Domain is relevance only. Credentials belong on the backend. See
[WordPress project workflow](WORDPRESS_PROJECTS.md) for capture and closeout.

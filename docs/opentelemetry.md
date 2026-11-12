# OpenTelemetry memory spans

core-api can emit an OpenTelemetry span for every memory operation, following
the [GenAI memory semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/e6658f14c9d9ea0760825f029cb82e7673f48fab/docs/gen-ai/gen-ai-spans.md#memory).
An agent's trace in Langfuse, Datadog, Phoenix or any other OTLP backend then
shows what Caura recalled and stored, how long it took, and which governance
policy acted on it. Nothing is exported until you configure an endpoint.

## Turning it on

Set the standard OTLP variables on core-api:

| Variable | Effect |
| --- | --- |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Base URL of an OTLP/HTTP receiver (`/v1/traces` is appended). Setting this, or the next one, turns the spans on. |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Full traces URL, if your receiver does not use the default path. |
| `OTEL_EXPORTER_OTLP_HEADERS` | Headers for the receiver, e.g. an `Authorization` header. |
| `OTEL_SERVICE_NAME` | Service name on every span. Defaults to `caura-core-api`. |
| `OTEL_RESOURCE_ATTRIBUTES` | Extra resource attributes, e.g. `deployment.environment=prod`. |
| `OTEL_SDK_DISABLED` | `true` turns everything off, even with an endpoint set. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT` | `SPAN_ONLY` or `SPAN_AND_EVENT` adds the search query text to spans. Any other value, or none, leaves it out. |

The Docker image includes the exporter (the `otel` extra). Running from
source, install it with `uv sync --extra otel` in `core-api/`.

Without an endpoint, the spans go to OpenTelemetry's built-in no-op tracer,
which records nothing and sends nothing.

### Langfuse

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s' "$LANGFUSE_PUBLIC_KEY:$LANGFUSE_SECRET_KEY" | base64)"
```

### Phoenix, Jaeger, or an OpenTelemetry Collector

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318
```

### Datadog

There are two ways:

- **Under `ddtrace-run`** (how the Caura SaaS image runs), set
  `DD_TRACE_OTEL_ENABLED=true`. ddtrace installs its own OpenTelemetry
  provider before the app starts, and core-api leaves that provider in place.
  The memory spans then become Datadog spans inside ddtrace's request span. No
  OTLP endpoint is needed, and ddtrace's existing instrumentation is
  unchanged: FastAPI's own request spans (below) stay off under ddtrace, so
  each request still has one server span. Datadog stores the `caura.policy_decision` list as
  `caura.policy_decision.0`, `caura.policy_decision.1`, and so on.
- **Without ddtrace**, point `OTEL_EXPORTER_OTLP_ENDPOINT` at a Datadog Agent
  or Collector that accepts OTLP.

If both are configured, ddtrace wins and the OTLP endpoint is ignored, so
spans are never sent twice.

## One trace per agent turn

core-api continues the caller's trace when the request carries a W3C
`traceparent` header. Caura Rail sends one when its `otel` extra is installed,
so each turn shows up as a single trace: the turn, its recall, the server's
`search_memory`, the extraction, and each `create_memory`.

With core-api's own OTLP exporter on, the request side comes from FastAPI,
which traces each request itself: an HTTP `SERVER` span per request (for
example `POST /api/v1/search`), plus `fastapi.dependencies`, `fastapi.endpoint`
and `fastapi.serialization` spans inside it. The memory spans nest under
`fastapi.endpoint`. FastAPI records the request's query string as `url.query`;
core-api replaces each value with `REDACTED` and keeps the parameter names,
since a query string can hold search text (`GET /api/v1/entities?search=`). The
values are kept under the content opt-in.

When a tracer provider core-api did not install is in charge (ddtrace's, or
one an operator set up, for example with `opentelemetry-instrument`), FastAPI's
request spans are skipped. That tracer's own HTTP instrumentation then reads
`traceparent` and decides what its request spans record: ddtrace's request
span, or your instrumentation's under its own settings. The memory spans go to
that provider all the same.

FastAPI's OpenTelemetry logs are turned off in every case, because they record
failed validations with their input, which for a memory write is the memory's
content.

## Spans

Each span's name is the operation's `gen_ai.operation.name`. The spans are
`INTERNAL`, because they run inside the server that holds the memory.

| Span | Emitted by |
| --- | --- |
| `search_memory` | `POST /api/v1/search`, `POST /api/v1/recall`, MCP `caura_recall`, and the server's own searches |
| `create_memory` | `POST /api/v1/memories`, `POST /api/v1/memories/bulk`, MCP `caura_write` (single and batch) |
| `update_memory` | `PATCH /api/v1/memories/{id}` and MCP `caura_manage` updates |
| `delete_memory` | `DELETE /api/v1/memories/{id}` and MCP `caura_manage` deletes |
| `caura.policy_decision` | Only for a governance action taken after its write span has ended (fast-mode remediation). It joins the same trace. |

### Attributes

| Attribute | Value |
| --- | --- |
| `gen_ai.operation.name` | `search_memory`, `create_memory`, `update_memory` or `delete_memory` |
| `gen_ai.provider.name` | `caura` |
| `gen_ai.memory.store.id` | The tenant id |
| `gen_ai.memory.record.id` | The memory's id, on update, delete and single create |
| `gen_ai.memory.record.count` | Rows returned by a search, or rows a write or delete attempted |
| `gen_ai.agent.id` | The agent the operation acts as, when known |
| `gen_ai.memory.query.text` | The search query. **Only with the content opt-in above.** |
| `error.type` | The HTTP status (`404`, `409`, `503`, ...) or the exception class, when the operation did not succeed. Only a 5xx or an exception also sets the span status to error; a 4xx such as a duplicate is the server answering, not failing. |
| `caura.scope` | The memory's visibility (`scope_agent`, `scope_team`, `scope_org`), when the write names one |
| `caura.trust_level` | The calling agent's trust level, when the request resolved the agent |
| `caura.policy_decision` | Every governance action taken on this operation, in order, e.g. `["pii_flag", "nonbusiness_keep_private"]`. Each action is also a span event of the same name. |
| `caura.payload.bytes` | UTF-8 bytes of memory content written, or returned by a search |

The instrumentation scope is `core_api.memory`, and its
`caura.semconv.genai.commit` attribute names the conventions revision the
attribute names follow.

## What is never exported

- **Memory content.** Neither content written nor content recalled is ever
  exported, with or without the opt-in. `gen_ai.memory.records` is not
  emitted.
- **Exception messages.** A failed operation carries only `error.type`, since
  messages can quote the content that failed.
- **The search query**, and the query-string values on the request spans
  core-api produces, unless `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT` is
  `SPAN_ONLY` or `SPAN_AND_EVENT`. Request spans from ddtrace or your own
  instrumentation follow that tracer's settings (for ddtrace,
  `DD_HTTP_SERVER_TAG_QUERY_STRING`).

## Version pinning

The GenAI memory conventions are still at "Development" status, and
open-telemetry/semantic-conventions-genai has not published a release yet.
The attribute names follow commit
[`e6658f14`](https://github.com/open-telemetry/semantic-conventions-genai/tree/e6658f14c9d9ea0760825f029cb82e7673f48fab)
(2026-11-06), recorded as `SEMCONV_GENAI_COMMIT` in
`core-api/src/core_api/otel_spans.py`. Names change only when that pin is
moved deliberately.

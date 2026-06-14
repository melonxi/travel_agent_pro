# Trace Redaction And Artifact Policy

Trace events are evidence, not raw dumps. Small structured metadata may live in
`trace_events.payload_json`; large or sensitive bodies must be redacted and stored
as artifacts or represented by hashes only.

## Never Store Unredacted

These values must never be stored in `payload_json` or artifact files:

- API keys
- bearer tokens
- cookies and set-cookie headers
- xsec tokens
- OAuth codes
- access and refresh tokens
- client secrets
- passwords
- private URLs containing userinfo credentials such as
  `https://user:pass@example.com/path`

Default redaction marker:

```text
[REDACTED:<kind>]
```

Current marker examples:

- `[REDACTED:secret]`
- `[REDACTED:bearer_token]`
- `[REDACTED:cookie]`
- `[REDACTED:credentials]`
- `[REDACTED:pii]`

## Configurable PII

PII redaction is configurable because local debugging sometimes benefits from
seeing user-provided contact details, while production should default to stronger
privacy.

Configurable PII patterns:

- email
- phone
- passport or national id number
- address fields

Recommended defaults:

| Environment | Secret redaction | PII redaction | Artifact body persistence |
| --- | --- | --- | --- |
| local/dev | Always on | Off by default, opt-in | Redacted bodies allowed |
| test | Always on | On in redaction tests | Redacted bodies allowed in tmp dirs |
| production | Always on | On by default | Hash-only unless explicitly enabled |

## URL Query Scrubbing

For URLs, scrub query parameter values when the parameter name is one of:

- `api_key`
- `apikey`
- `access_token`
- `refresh_token`
- `auth_token`
- `xsec_token`
- `oauth_token`
- `oauth_code`
- `code`
- `token`
- `key`
- `secret`
- `signature`
- `sig`

Private URL credentials must be replaced with `[REDACTED:credentials]`.

## Artifact Redaction Status

`trace_artifacts.redaction_status` values:

| Value | Meaning |
| --- | --- |
| `not_needed` | Redactor found no sensitive content. |
| `redacted` | Body was stored after redaction. |
| `hash_only` | Body was not stored; hash and metadata were retained. |
| `disabled` | Artifact persistence disabled by config; event still stores hash/preview when available. |

## Hashing

Artifact hashes use canonical JSON when the input is structured data:

- `sort_keys=True`
- compact separators
- UTF-8 bytes
- SHA-256
- external representation: `sha256:<hex>`

Hash stability rules:

- Dict key order must not affect hashes.
- Redaction must run before persisted artifact hashing when content is stored.
- In hash-only mode, hash the redacted representation when redaction can be
  computed safely in memory.

## Payload Policy

Allowed in `payload_json`:

- event ids and correlation ids
- hashes
- redaction status
- artifact ids
- field names
- small numeric metrics
- small previews after redaction
- validation/judge summaries

Not allowed in `payload_json`:

- full prompts
- full LLM responses
- full tool results
- raw cookies/tokens/keys
- raw contact details when PII redaction is enabled
- long markdown deliverables

## Implementation

Current helper module:

- `backend/storage/trace_redaction.py`

Current tests:

- `backend/tests/test_trace_redaction.py`

The module provides:

- recursive dict/list redaction
- secret key redaction
- bearer/cookie assignment redaction
- URL query-token scrubbing
- private URL credential scrubbing
- optional PII redaction
- stable content hashing

"""Owner-approved ChatGPT session control: typed API, approval ledger, workers, HTTP and MCP.

Accessed via: ``pm chatgpt api|approvals|mcp|connector`` (see ``pmlib/commands/chatgpt.py``).

Layout (every module is stdlib-only at import time):
    errors      ApiError and the stable error-code vocabulary.
    schema      strict request validation; external JSON can only REQUEST.
    ledger      the single append-only approval ledger (flock + fsync, torn-line tolerant).
    projection  pure fold of ledger events into request receipts.
    policy      plan digests, dedup keys and the continuation guards.
    service     Services (injected collaborators) and request submission.
    api         the one dispatch function shared by the CLI, HTTP and MCP.
    tg_transport  ``tg-ctl ask`` wrapper: the only route from a request to an approval.
    guardian_client  account-local Guardian ``POST /continue`` + ``GET /continuations/<id>``.
    worker      the finite, one-request-at-a-time approval worker.
    poller      conservative continuation proposals from fresh telemetry.
    runner      the ``approvals run`` loop.
    http_api    loopback authenticated ``POST /v1/rpc`` server and token handling.
    mcp         stdio JSON-RPC MCP server over the same dispatch.
    connector   the fixed reviewed ``guardian``/``chatgpt`` connector plan.
"""

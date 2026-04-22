# Memory Search Scoping

`memory_search` is constructed per caller channel through
`make_memory_search_server()`. The default scope is `this_channel`, which only
returns rows whose `channel_id` matches the caller channel bound into the MCP
server at construction time.

Callers may request `scope: all_channels` for cross-channel recall. That scope
is still constrained by the server's `excluded_channels` deny-list. Excluded
channel IDs are applied inside the SQLite search query as
`channel_id NOT IN (...)`, so rows from those channels are not returned by
keyword, semantic, or hybrid memory search.

Channel manifests expose this as:

```yaml
memory:
  excluded_channels: []
```

An empty list preserves existing behavior. When M5.6 populates the list for
OQ31 opt-outs, the manifest value is threaded into the memory MCP server during
agent option construction and team MCP resolution. If the caller channel itself
is excluded, `scope: this_channel` returns no rows.

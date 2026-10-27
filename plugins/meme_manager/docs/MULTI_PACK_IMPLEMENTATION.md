# Multi-pack usage

A default, session or persona rule can bind several installed meme packs.
In Settings, open the existing pack rules dialog and select multiple packs in
“使用表情包”. On a desktop, hold Ctrl (Windows/Linux) or Command (macOS) while
clicking. Existing single-pack selections continue to work. Selection order is
preserved; newly selected packs are appended after the existing selection.

## Rules API

The `settings/rules` endpoint reads and saves the following structure:

```json
{
  "rules": [
    {
      "id": "session-example",
      "scope": "session",
      "target": "session-id",
      "pack_ids": ["pack-a", "pack-b"]
    },
    {
      "id": "default",
      "scope": "default",
      "pack_id": "pack-a",
      "pack_ids": ["pack-a", "pack-b"]
    }
  ]
}
```

The API validates every referenced pack, removes duplicate IDs while preserving
order, and saves `pack_id` as the first entry for existing single-pack consumers.
If both fields exist, a non-empty `pack_ids` takes precedence. The default rule
must be the last rule and must occur exactly once. Rules are evaluated in saved
order; the first matching session or persona rule with usable packs wins.
Default rules retain all usable selected packs. Missing and disabled packs are
skipped at runtime. If no selected default is usable, the built-in default is
tried, followed by the first enabled installed pack.

## Retrieval and delivery

- Category descriptions are merged across selected packs. The first pack takes
  precedence when category names overlap. Category delivery chooses among images
  from all selected packs, including categories unique to a secondary pack.
- Both tool mode and emotion model mode search fully semanticized selected packs.
  Each pack uses its own remembered embedding provider and index; an unavailable
  pack does not prevent the other packs from returning results.
- Candidates are ranked across packs and capped by the configured Top-K. Duplicate
  semantic entries are returned once. Public IDs are disambiguated across the
  searched metadata, and selected images are validated against their source pack
  and current file hash before streaming or ordinary delivery.
- Removing a pack preserves the other IDs in default, session and persona rules.
  The same rule format is retained through backup and restoration.

## Runtime interfaces

`resolve_pack_ids(session_id=None, persona_id=None)` returns the usable IDs.
`resolve_pack_id(...)` remains the single-pack compatibility interface.
`resolve_pack_context(...)` returns `pack_ids`, a `pack_dirs` mapping, merged
`category_mapping`, and the primary pack's existing paths. Operations that
explicitly target one pack, such as uploads, still use the primary pack.

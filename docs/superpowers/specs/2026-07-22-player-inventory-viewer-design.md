# Player Inventory Viewer Design

## Goal

Add a `玩家` panel that separates online and offline players, supports inventory viewing for Minecraft 1.13+, renders a Minecraft-like inventory layout, shows item icons where local assets can be resolved, and displays tooltip details on hover.

## Scope

- Rename the current `在线玩家` tab to `玩家`.
- Show two lists: `在线玩家` and `离线玩家`.
- Online players keep the existing log-derived state and management actions.
- Offline players are discovered from the active world's `playerdata/*.dat` files.
- Inventory viewing is supported only when the generated project's Minecraft version is `1.13` or newer.
- Minecraft versions below `1.13` show a clear unsupported message for inventory viewing.
- First version supports vanilla inventory, armor, offhand, ender chest, and best-effort accessory sections from common mod data keys.

## Data Flow

### Online Player

1. User opens a player detail and clicks `查看背包`.
2. Backend sends:
   - `data get entity <player> Inventory`
   - `data get entity <player> EnderItems`
   - `data get entity <player> ArmorItems`
   - `data get entity <player> HandItems`
3. Backend parses latest matching log output into normalized inventory slots.
4. If the command output has not appeared yet, the API returns `status: probing` and the UI refreshes.

### Offline Player

1. Backend reads current world from `server.properties` `level-name`.
2. Backend scans `<world>/playerdata/*.dat`.
3. Backend parses gzip NBT using a small local NBT reader.
4. Backend extracts `Inventory`, `EnderItems`, and accessory-like sections.

## Item Model

Each item returned to the UI uses:

```json
{
  "slot": 0,
  "section": "hotbar",
  "id": "minecraft:stone",
  "count": 64,
  "damage": 0,
  "raw": "{...}",
  "iconDataUrl": "data:image/png;base64,...",
  "tooltip": ["Stone", "minecraft:stone", "Count: 64"]
}
```

## Icon Resolution

Pack2Serve builds a best-effort asset index from enabled `mods/*.jar` files:

- `assets/<namespace>/models/item/<item>.json`
- `assets/<namespace>/textures/item/*.png`
- `assets/<namespace>/textures/block/*.png`

Resolution order:

1. Item model JSON `textures.layer0`.
2. Direct `textures/item/<item>.png`.
3. Direct `textures/block/<item>.png`.
4. UI fallback placeholder.

The first version does not attempt full Minecraft model baking, item overrides, dynamic NBT variants, tinting, or generated 3D rendering.

## UI

The player detail panel contains:

- Online player list.
- Offline player list.
- Player header with name, UUID if known, status, and skin.
- Minecraft-like inventory grid:
  - armor: 4 slots
  - offhand: 1 slot
  - accessories: dynamic named groups
  - main inventory: 27 slots
  - hotbar: 9 slots
  - ender chest: 27 slots
- Hover tooltip with item name/id/count/damage/raw NBT or components summary.

## Error Handling

- Old Minecraft version: return `supported: false`, reason `minecraft-version-too-old`.
- Online server stopped: return `status: unavailable` with a message.
- Online command sent but no log result yet: return `status: probing`.
- Broken or unreadable `playerdata` file: skip that file and include a warning.
- Missing icon: return `iconDataUrl: null`; frontend renders a placeholder.

## Testing

- Unit test version gate.
- Unit test SNBT list parsing for online command output.
- Unit test gzip NBT playerdata parsing for offline inventory.
- Unit test mod jar item icon resolution.
- Unit test panel service player list shape includes online and offline lists.
- Unit test panel HTML has `玩家`, online/offline sections, inventory grid, and tooltip classes.

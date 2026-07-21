# Player Inventory Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Minecraft 1.13+ player inventory viewer with online/offline player lists, item icons, accessory sections, and Minecraft-like UI.

**Architecture:** Add focused backend helpers for NBT, inventory normalization, and item icons, then expose them through `PanelService` and the existing standard-library HTTP panel. Keep the frontend in the existing `PANEL_HTML` pattern but make the player tab state richer.

**Tech Stack:** Python standard library, `unittest`, server-rendered HTML/CSS/vanilla JavaScript.

## Global Constraints

- Minecraft versions below `1.13` must show unsupported inventory viewing.
- Do not add third-party Python dependencies.
- Do not restart the currently running panel during implementation.
- Tests must be written before production code for new behavior.
- Item icon resolution is best-effort from local jars; no full model baking in this version.

---

### Task 1: Inventory Parsing Core

**Files:**
- Create: `pack2serve/inventory.py`
- Test: `tests/test_pack2serve_core.py`

**Interfaces:**
- Produces: `minecraft_supports_inventory_view(version: str) -> bool`
- Produces: `parse_snbt_inventory_list(raw: str, section: str = "inventory") -> list[dict[str, object]]`
- Produces: `normalize_inventory_item(raw: dict[str, object], section: str) -> dict[str, object]`

- [ ] Write failing tests for version gating and SNBT item parsing.
- [ ] Run focused tests and confirm failures.
- [ ] Implement minimal parser and normalization.
- [ ] Run focused tests and all tests.
- [ ] Commit.

### Task 2: Offline Playerdata NBT Reader

**Files:**
- Modify: `pack2serve/inventory.py`
- Test: `tests/test_pack2serve_core.py`

**Interfaces:**
- Produces: `read_playerdata_inventory(path: Path) -> dict[str, object]`
- Produces: `read_nbt_gzip(path: Path) -> dict[str, object]`

- [ ] Write failing test with a small generated gzip NBT playerdata file.
- [ ] Run focused test and confirm failure.
- [ ] Implement a minimal standard-library NBT reader.
- [ ] Run focused tests and all tests.
- [ ] Commit.

### Task 3: Item Icon Index

**Files:**
- Create: `pack2serve/assets.py`
- Test: `tests/test_pack2serve_core.py`

**Interfaces:**
- Produces: `resolve_item_icon_data_url(server_dir: Path, item_id: str) -> str | None`

- [ ] Write failing test with a synthetic mod jar containing item model JSON and texture PNG.
- [ ] Run focused test and confirm failure.
- [ ] Implement jar scanning and model texture lookup.
- [ ] Run focused tests and all tests.
- [ ] Commit.

### Task 4: Panel Player API

**Files:**
- Modify: `pack2serve/panel.py`
- Modify: `pack2serve/web.py`
- Test: `tests/test_pack2serve_core.py`

**Interfaces:**
- Produces: `PanelService.server_players()` returning `onlinePlayers`, `offlinePlayers`, and compatibility.
- Produces: `PanelService.player_inventory(target_name: str, player: str, source: str) -> dict[str, object]`
- Produces: `GET /api/servers/player-inventory?targetName=...&player=...&source=online|offline`

- [ ] Write failing panel service and web HTML tests.
- [ ] Run focused tests and confirm failures.
- [ ] Implement API and service methods.
- [ ] Run focused tests and all tests.
- [ ] Commit.

### Task 5: Minecraft-Like Player UI

**Files:**
- Modify: `pack2serve/web.py`
- Test: `tests/test_pack2serve_core.py`

**Interfaces:**
- Consumes: `/api/servers/players`
- Consumes: `/api/servers/player-inventory`

- [ ] Write failing HTML marker tests for `玩家`, online/offline sections, inventory grids, item slots, and tooltip class names.
- [ ] Run focused tests and confirm failures.
- [ ] Implement frontend rendering, polling for `status: probing`, icon slots, and hover tooltip.
- [ ] Run focused tests and all tests.
- [ ] Commit.

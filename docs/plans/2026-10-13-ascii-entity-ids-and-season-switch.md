# ASCII 实体 ID 与采暖季开关 Implementation Plan

> **REQUIRED SUB-SKILL:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把实体 ID 前缀从运行时长出的拼音 `wo_de_jia_` 改为代码固定的 `cp_`，并新增 `switch.cp_heating_season` 采暖季开关。

**Architecture:** 命名只在一处决定 —— `base.py` 的 `ClimatePilotEntity.add_to_platform_start()` 里用 `platform.domain` 拼出 `entity_id`。7 个平台文件不改。采暖季开关复用已有的 `control_mode`（`paused`/`observe`），不新增状态机。已装机器的 51 个实体由一次性迁移重命名。

**Tech Stack:** Python 3.14 / Home Assistant 2026.9.4、pytest、ruff。

**设计依据：** `docs/plans/2026-10-13-ascii-entity-ids-and-season-switch-design.md`（含实测证据）

---

## 前置事实（已实测，不要重新推导）

1. `platform.domain` 是实体域（`sensor`/`switch`/`climate`），`platform.platform_name` 是集成名（`climatepilot`）。**ID 前缀要用前者。**
2. `add_to_platform_start(hass, platform, parallel_add_tasks)` 在 HA 推导 entity_id **之前**调用，在其中设置 `self.entity_id` 会被 HA 采纳（已验证 `registered in registry: True`）。
3. 三个基类全部链自 `ClimatePilotEntity`：`ClimatePilotEntity`、`ClimatePilotZoneEntity`、`ClimatePilotDiagnosticEntity`。**40 个实体类无遗漏**（已用 AST 验证）。
4. `base.py` 的 `__init__` 里 `self.platform` **尚不存在**，所以不能用 `__init__`。
5. 注册表以 `unique_id` 为键并锁定 `entity_id`；改代码不会自动重命名已存在实体 → **迁移必需**。
6. `device_registry.devices` 当映射用已弃用，读取即抛异常；用 `async_entries_for_config_entry(reg, entry_id)`。

---

## Task 1: 命名函数 + 基线测试

**Files:**
- Modify: `custom_components/climatepilot/base.py`
- Test: `tests/test_entity_id_scheme.py` (新建)

**Step 1: 写失败的测试**

```python
"""实体 ID 方案：前缀必须是 ASCII 的 cp_，且不随系统名称变化。"""
from __future__ import annotations

import pytest

from custom_components.climatepilot.base import entity_id_for


@pytest.mark.parametrize(
    ("domain", "key", "expected"),
    [
        ("sensor", "heating_state", "sensor.cp_heating_state"),
        ("sensor", "livingroom_floorheating_effective_setpoint",
         "sensor.cp_livingroom_floorheating_effective_setpoint"),
        ("climate", "whole_home_heating", "climate.cp_whole_home_heating"),
        ("switch", "heating_season", "switch.cp_heating_season"),
        ("binary_sensor", "probe_fault", "binary_sensor.cp_probe_fault"),
        ("select", "control_mode", "select.cp_control_mode"),
        ("number", "manual_supply_temperature", "number.cp_manual_supply_temperature"),
        ("button", "reassess_now", "button.cp_reassess_now"),
    ],
)
def test_entity_id_uses_cp_prefix(domain, key, expected):
    assert entity_id_for(domain, key) == expected


def test_entity_id_is_ascii_even_for_a_chinese_key():
    """房间名可能来自中文；ID 必须是 ASCII 可寻址的。"""
    result = entity_id_for("sensor", "客厅_effective_setpoint")
    assert result.isascii(), result
    assert result.startswith("sensor.cp_")
```

**Step 2: 运行，确认失败**

Run: `pytest tests/test_entity_id_scheme.py -v`
Expected: FAIL — `ImportError: cannot import name 'entity_id_for'`

**Step 3: 最小实现**

在 `base.py` 加入（放在常量区之后、类定义之前）：

```python
#: Entity ids use this prefix instead of one derived from the device name.
#:
#: Home Assistant's documented behaviour is to build ``entity_id`` from
#: ``<device name>_<entity name>``, which is a good default: the device name
#: acts as a source tag that keeps entities from different integrations apart.
#: It produces unusable ids here, though, because the device name is Chinese
#: and ``slugify`` can only romanise it - "我的家" becomes "wo_de_jia". Renaming
#: the system to ASCII would "fix" it only by tying every entity id to a
#: user-editable display string: changing the name would then rename all 51
#: entities and break any dashboard or automation referencing them, and
#: ``name_by_user`` takes priority over ``name``, so anyone renaming the device
#: in the UI would reintroduce pinyin for entities created afterwards.
#:
#: Pinning the prefix keeps the two concerns apart: the system name stays a
#: free-text Chinese display label, and entity ids stay stable ASCII.
ENTITY_ID_PREFIX: Final = "cp"


def entity_id_for(domain: str, key: str) -> str:
    """Return the stable entity id for ``key`` in ``domain``.

    The key is slugified so a key built from a room name (which may be Chinese)
    still yields an ASCII object id.
    """
    return f"{domain}.{ENTITY_ID_PREFIX}_{slugify(key)}"
```

在文件顶部的 import 区加入：

```python
from homeassistant.util import slugify
```

**Step 4: 运行，确认通过**

Run: `pytest tests/test_entity_id_scheme.py -v`
Expected: PASS（9 项）

**Step 5: 提交**

```bash
git add custom_components/climatepilot/base.py tests/test_entity_id_scheme.py
git commit -m "feat: pin entity ids to an ASCII cp_ prefix"
```

---

## Task 2: 基类应用该 ID

**Files:**
- Modify: `custom_components/climatepilot/base.py`（`ClimatePilotEntity`）
- Test: `tests/test_entity_id_scheme.py`

**Step 1: 写失败的测试**

追加：

```python
def test_base_entity_sets_entity_id_from_platform_domain():
    """基类必须在 add_to_platform_start 里设 id（__init__ 时 platform 还不存在）。"""
    import inspect

    from custom_components.climatepilot.base import ClimatePilotEntity

    source = inspect.getsource(ClimatePilotEntity.add_to_platform_start)
    assert "platform.domain" in source, "must use the entity domain, not platform_name"
    assert "entity_id_for" in source
```

**Step 2: 运行，确认失败**

Run: `pytest tests/test_entity_id_scheme.py::test_base_entity_sets_entity_id_from_platform_domain -v`
Expected: FAIL — `AttributeError: type object 'ClimatePilotEntity' has no attribute 'add_to_platform_start'`

**Step 3: 实现**

在 `ClimatePilotEntity` 内加入：

```python
    def add_to_platform_start(
        self,
        hass: HomeAssistant,
        platform: Any,
        parallel_add_tasks: Any,
    ) -> None:
        """Set our own entity id before Home Assistant derives one.

        ``self.platform`` does not exist yet in ``__init__``, so the entity can
        only learn its domain here. ``platform.domain`` is the entity domain
        (sensor/switch/climate/...); ``platform.platform_name`` is the
        integration name and would be the wrong prefix.
        """
        self.entity_id = entity_id_for(platform.domain, self._key)
        super().add_to_platform_start(hass, platform, parallel_add_tasks)
```

**Step 4: 运行，确认通过**

Run: `pytest tests/test_entity_id_scheme.py -v`
Expected: PASS（10 项）

**Step 5: 跑集成测试，观察真实 ID**

Run: `pytest tests/test_integration_setup.py -q`
Expected: **大量失败** —— 这是预期的，测试断言的是旧 ID。记录失败清单，Task 5 统一修。

**Step 6: 提交**

```bash
git add custom_components/climatepilot/base.py tests/test_entity_id_scheme.py
git commit -m "feat: derive entity ids in the shared base class"
```

---

## Task 3: 采暖季开关实体

**Files:**
- Modify: `custom_components/climatepilot/switch.py`
- Modify: `custom_components/climatepilot/coordinator.py`（加 `async_set_heating_season`）
- Test: `tests/test_season_switch.py` (新建)

**Step 1: 写失败的测试**

```python
"""采暖季开关：OFF = 停季（paused），ON = 开季（observe，仍不写入）。"""
from __future__ import annotations

import pytest

from custom_components.climatepilot.const import (
    CONTROL_MODE_OBSERVE,
    CONTROL_MODE_PAUSED,
)


@pytest.mark.parametrize(
    ("season_on", "expected_mode"),
    [(True, CONTROL_MODE_OBSERVE), (False, CONTROL_MODE_PAUSED)],
)
def test_season_switch_maps_to_control_mode(season_on, expected_mode):
    """开关不自己存状态，而是映射到已有 control_mode。"""
    from custom_components.climatepilot.switch import season_to_control_mode

    assert season_to_control_mode(season_on) == expected_mode


@pytest.mark.parametrize(
    ("mode", "expected_on"),
    [
        (CONTROL_MODE_PAUSED, False),
        (CONTROL_MODE_OBSERVE, True),
        ("coordinate", True),
    ],
)
def test_control_mode_maps_back_to_switch_state(mode, expected_on):
    from custom_components.climatepilot.switch import control_mode_to_season

    assert control_mode_to_season(mode) is expected_on


def test_opening_the_season_never_selects_coordinate():
    """开季只能回到 observe —— 绝不自动进入 coordinate（会开始真控制）。"""
    from custom_components.climatepilot.switch import season_to_control_mode

    assert season_to_control_mode(True) != "coordinate"
```

**Step 2: 运行，确认失败**

Run: `pytest tests/test_season_switch.py -v`
Expected: FAIL — `ImportError: cannot import name 'season_to_control_mode'`

**Step 3: 实现映射**

在 `switch.py` 加入：

```python
from .const import CONTROL_MODE_OBSERVE, CONTROL_MODE_PAUSED, DOMAIN


def season_to_control_mode(season_on: bool) -> str:
    """Map the season switch to a control mode.

    ON deliberately selects ``observe`` rather than ``coordinate``. Turning the
    season on should resume monitoring and suggestions, not silently begin
    writing to the room thermostats: that would skip the review step the
    observe mode exists to provide, and would change behaviour on the user's
    heating system without a fresh confirmation.
    """
    return CONTROL_MODE_OBSERVE if season_on else CONTROL_MODE_PAUSED


def control_mode_to_season(mode: str) -> bool:
    """Derive the switch state from the control mode.

    The switch stores nothing of its own; two independent flags for the same
    decision would eventually disagree.
    """
    return mode != CONTROL_MODE_PAUSED
```

**Step 4: 运行，确认通过**

Run: `pytest tests/test_season_switch.py -v`
Expected: PASS（6 项）

**Step 5: 实现实体**

在 `switch.py` 加入（与已有的 `AuxiliaryLoadSwitch` 并列）：

```python
class HeatingSeasonSwitch(ClimatePilotEntity, SwitchEntity):
    """Whether the heating season is running (design: the off-season gate)."""

    def __init__(self, coordinator: ClimatePilotCoordinator) -> None:
        super().__init__(coordinator, "heating_season")
        self._attr_translation_key = "heating_season"

    @property
    def is_on(self) -> bool:
        return control_mode_to_season(self.coordinator.control_mode)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control_mode(season_to_control_mode(True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_control_mode(season_to_control_mode(False))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "control_mode": self.coordinator.control_mode,
            "writes_enabled": self.coordinator.writes_enabled,
        }
```

在 `async_setup_entry` 的 `entities` 列表中加入
`HeatingSeasonSwitch(coordinator)`（与已有的 `AuxiliaryLoadSwitch` 并列）。

**Step 6: 运行，确认通过**

Run: `pytest tests/test_season_switch.py tests/test_platform_entities.py -q`
Expected: PASS

**Step 7: 提交**

```bash
git add custom_components/climatepilot/switch.py tests/test_season_switch.py
git commit -m "feat: add the heating-season switch"
```

---

## Task 4: 翻译

**Files:**
- Modify: `custom_components/climatepilot/strings.json`
- Modify: `custom_components/climatepilot/translations/en.json`
- Modify: `custom_components/climatepilot/translations/zh-Hans.json`
- Test: `tests/test_translations_sync.py`（已有，会强制一致）

**Step 1: 三份文件都加实体名**

`entity.switch.heating_season.name`：

- `strings.json` / `en.json`：`"Heating season"`
- `zh-Hans.json`：`"采暖季"`

**Step 2: 跑一致性测试**

Run: `pytest tests/test_translations_sync.py -q`
Expected: PASS。**若失败，按报错补齐缺失的键** —— 该测试会列出三份文件之间的差异。

**Step 3: 提交**

```bash
git add custom_components/climatepilot/strings.json \
        custom_components/climatepilot/translations/
git commit -m "i18n: name the heating-season switch"
```

---

## Task 5: 更新测试中的实体 ID

**Files:**
- Modify: `tests/test_integration_setup.py`
- Modify: `tests/test_platform_entities.py`（如有硬编码 ID）

**Step 1: 找出所有硬编码的旧 ID**

Run: `grep -rn "wo_de_jia" tests/`
Expected: 列出所有出现处

**Step 2: 逐个替换为 `cp_` 形式**

`wo_de_jia_` → `cp_`。

**Step 3: 更新实体总数**

`switch` 平台多了一个实体，`EXPECTED_PER_PLATFORM` 与 `EXPECTED_ENTITY_TOTAL`
需相应调整。**先跑测试看实际报出的数字，再用它更新**，不要凭推算填。

Run: `pytest tests/test_integration_setup.py -q`
Expected: 失败信息会给出 expected/actual 的具体数字

**Step 4: 再跑，确认通过**

Run: `pytest tests/test_integration_setup.py -q`
Expected: PASS

**Step 5: 提交**

```bash
git add tests/
git commit -m "test: update expectations for the cp_ entity ids"
```

---

## Task 6: 一次性迁移

**Files:**
- Modify: `custom_components/climatepilot/__init__.py`
- Test: `tests/test_entity_id_migration.py` (新建)

**Step 1: 写失败的测试**

```python
"""迁移：把已存在的 wo_de_jia_* 注册表项改名为 cp_*。

不迁移的话，已安装的集成不会被重命名 —— 注册表以 unique_id 为键并锁定
entity_id，改代码不会自动生效。
"""
from __future__ import annotations

import pytest


def test_migration_renames_legacy_ids():
    """名字从 wo_de_jia_ 变成 cp_，unique_id 必须原样保留。"""
    from custom_components.climatepilot.migration import migrate_entity_ids

    class FakeEntry:
        def __init__(self, entity_id, unique_id):
            self.entity_id = entity_id
            self.unique_id = unique_id

    class FakeRegistry:
        def __init__(self, entries):
            self._entries = entries
            self.updated = []

        @property
        def entities(self):
            return self._entries

        def async_update_entity(self, entity_id, *, new_entity_id):
            self.updated.append((entity_id, new_entity_id))

    rows = [
        FakeEntry("sensor.wo_de_jia_heating_state", "sys_heating_state"),
        FakeEntry("sensor.cp_supply_temperature", "sys_supply_temperature"),
    ]
    registry = FakeRegistry(rows)
    changed = migrate_entity_ids(registry)

    assert changed == 1, "only the legacy row should move"
    assert registry.updated == [
        ("sensor.wo_de_jia_heating_state", "sensor.cp_heating_state")
    ]


def test_migration_is_idempotent():
    """第二次运行必须不做任何改动。"""
    from custom_components.climatepilot.migration import migrate_entity_ids

    class FakeEntry:
        def __init__(self, entity_id):
            self.entity_id = entity_id

    class FakeRegistry:
        def __init__(self, entries):
            self._entities = entries
            self.updated = []

        @property
        def entities(self):
            return self._entities

        def async_update_entity(self, entity_id, *, new_entity_id):
            self.updated.append((entity_id, new_entity_id))

    registry = FakeRegistry([FakeEntry("sensor.cp_heating_state")])
    assert migrate_entity_ids(registry) == 0
    assert migrate_entity_ids(registry) == 0
    assert registry.updated == []
```

**Step 2: 运行，确认失败**

Run: `pytest tests/test_entity_id_migration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.climatepilot.migration'`

**Step 3: 实现**

新建 `custom_components/climatepilot/migration.py`：

```python
"""One-off rename of entity ids created before the cp_ prefix existed.

Home Assistant keys the entity registry by unique_id and pins the entity_id
stored there, so changing the naming code does not rename entities that already
exist. Measured on 2026.9.4: deleting the config entry and reinstalling does not
help either - the device id survives the delete, the new entity id is derived
from the *old* device name, and the pinyin prefix comes back.

So the installed base is renamed explicitly, through Home Assistant's own
registry API, which preserves the unique_id, the history and any references.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import ENTITY_ID_PREFIX

_LOGGER = logging.getLogger(__name__)

#: The prefix produced by slugifying the default device name that earlier
#: releases used.
LEGACY_PREFIX = "wo_de_jia"


def migrate_entity_ids(registry: Any) -> int:
    """Rename legacy ``*.<legacy>_*`` entity ids to the pinned prefix.

    Returns the number of entities renamed. Idempotent: rows already using the
    current prefix are left alone, so running it on every setup is safe.
    """
    renamed = 0
    for entry in list(registry.entities):
        entity_id = getattr(entry, "entity_id", None)
        if not entity_id or "." not in entity_id:
            continue

        domain, _, object_id = entity_id.partition(".")
        marker = f"{LEGACY_PREFIX}_"
        if not object_id.startswith(marker):
            continue

        new_object_id = f"{ENTITY_ID_PREFIX}_{object_id[len(marker):]}"
        new_entity_id = f"{domain}.{new_object_id}"
        registry.async_update_entity(entity_id, new_entity_id=new_entity_id)
        _LOGGER.info("climatepilot: renamed %s -> %s", entity_id, new_entity_id)
        renamed += 1

    return renamed
```

**Step 4: 运行，确认通过**

Run: `pytest tests/test_entity_id_migration.py -v`
Expected: PASS（2 项）

**Step 5: 接到 setup 上（必须在平台加载之前）**

在 `custom_components/climatepilot/__init__.py` 的 `async_setup_entry` 中，
**在 `async_forward_entry_setups` 之前**调用：

```python
    # Rename before the platforms load: if entities were added first, both the
    # old and the new id would briefly exist and the dashboard would reference
    # one of them at random.
    from .migration import migrate_entity_ids

    migrate_entity_ids(entity_registry.async_get(hass))
```

并确保 `from homeassistant.helpers import entity_registry` 已导入。

**Step 6: 反向对照 —— 证明测试非恒真**

临时把 `migration.py` 的 `migrate_entity_ids` 改成 `return 0`（不迁移），
Run: `pytest tests/test_entity_id_migration.py -v`
Expected: **必须变红**。确认后改回。

**Step 7: 跑全量**

Run: `pytest -q`
Expected: 全部通过

**Step 8: 提交**

```bash
git add custom_components/climatepilot/migration.py \
        custom_components/climatepilot/__init__.py \
        tests/test_entity_id_migration.py
git commit -m "feat: migrate legacy entity ids to the cp_ prefix"
```

---

## Task 7: 更新面板与文档引用

**Files:**
- Modify: `dashboards/climatepilot.yaml`（53 处）
- Modify: `docs/DASHBOARD.md`（28 处）
- Modify: `docs/INTERFACES_V0.2.md`（19 处）
- Modify: `docs/INTERFACES_V0.3.md`（1 处）
- Modify: `docs/INTERFACES.md`（记录命名决策）
- Modify: `README.md`、`CHANGELOG.md`、`manifest.json`（版本 0.4.0）

**Step 1: 替换**

```bash
# 逐文件替换 wo_de_jia_ -> cp_ ，然后用测试验证
```

**Step 2: 在 `docs/INTERFACES.md` 记录命名决策**

写明：实体 ID 使用固定 `cp_` 前缀，**有意不跟随设备名**；理由是系统名称是
中文显示标签，跟随它会让改名连带改掉全部实体 ID；这是对 HA 默认约定的
有意偏离。

**Step 3: 更新 `manifest.json` 版本为 `0.4.0`**

**Step 4: 跑双向校验**

Run: `pytest tests/test_integration_setup.py -q`
Expected: PASS —— 其中包括"面板引用的 ID 都存在于真实注册表"与反向检查。

**Step 5: 若面板引用数变化，更新对应断言**

**Step 6: 提交**

```bash
git add dashboards/ docs/ README.md CHANGELOG.md \
        custom_components/climatepilot/manifest.json
git commit -m "docs: update references for the cp_ entity ids"
```

---

## Task 8: 全量验证与推送

**Step 1: 全量测试 + lint（两个 HA 版本）**

```bash
pytest -q
ruff check custom_components/ tests/ tools/
ruff format --check custom_components/ tests/ tools/
```

Expected: 全绿。
2026.2.3 环境用 `C:\Code\ha-verify\venv`，
2026.9.4 环境用 `%TEMP%\ha994\venv`（若还在）。

**Step 2: 确认无探针脚本残留**

```bash
git status --short
```

Expected: 空。

**Step 3: 推送并打 tag**

```bash
git push origin main
gh release create v0.4.0 --title "..." --notes "..."
```

**Step 4: 部署到实机并验证**

- HACS 更新到 0.4.0 → 重启 HA
- 确认 51 个实体全部改名为 `cp_`，且**历史未断**
- 确认 `switch.cp_heating_season` 存在，且当前状态为 ON（`control_mode = observe`）
- 确认面板无 "实体不存在"

**Step 5: 验证停季真的不写入**

按 OFF → 确认 `control_mode = paused`、`writes_enabled = false`、
锅炉与三间地暖的 setpoint 均无变化。

---

## 关键约束（违反会导致返工）

1. **`platform.domain` 不是 `platform.platform_name`** —— 后者是集成名。
2. **迁移必须在平台加载之前** —— 否则新旧 ID 并存。
3. **`self.platform` 在 `__init__` 里不存在** —— 只能在 `add_to_platform_start`。
4. **`EXPECTED_ENTITY_TOTAL` 用测试报出的实际值**，不要推算。
5. **每步都跑测试**，不要攒到最后。
6. 面板与文档的引用数必须与真实注册表交叉验证（V0.2 曾漏掉 45 处错误 ID）。

# 燃气与度日数仪表 Implementation Plan

> **REQUIRED SUB-SKILL:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 新增 6 个只读实体，把「今日/本月用气量、度日数、m³/度日」显示出来。定位是**仪表**，不是「省气证明」。

**Architecture:** 新增纯引擎 `energy_monitor.py`（不 import homeassistant，时间由调用方注入），状态 O(1) 不存历史列表，随 `periodic_audit`（15 分钟）采样。协调器把它接进已有的 store 持久化。6 个实体加在 `sensor.py`，复用 `ClimatePilotDiagnosticEntity` 基类（因此自动获得 `cp_` 前缀 ID）。**不新增定时器、不新增状态机、不修改任何写入路径。**

**Tech Stack:** Python 3.14 / Home Assistant 2026.9.4、pytest、ruff。

**设计依据：** `docs/plans/2026-10-13-gas-and-degree-days-design.md`（含 HA 源码实测证据）

---

## 前置事实（已实测，不要重新推导）

1. **HA 允许自定义单位进入长期统计。** `sensor/recorder.py`：`_normalize_states` 在没有单位转换器时走 `325` 行分支，只要单位**稳定**（`_equivalent_units`，329）就返回；编译循环的门槛是 `if not valid_float_states: continue`（610），**不是** `unit_class`。故 `°C·d` 安全，**前提是单位字符串永不变化**。
2. **`customize:` 能把值写进 state attributes**（`entity.py:1284`，`attr |= custom`），而统计正是从 state attributes 读 `state_class`（`recorder.py:117` 与 `612`）。但本计划**不改用户配置**，仅在设计文档记录。
3. **审计间隔默认 900 秒**（`DEFAULT_PERIODIC_AUDIT`），实机该选项为 `None` 故走默认。本功能复用该节拍，**不新增定时器**。
4. **实机燃气表 `sensor.gas_meter` = `total_increasing` / `m³`，是真实累计表**（24 小时内有增量）。室外温度 `sensor.heweather_outside_temperature` 是数字。
5. **纯引擎清单**：`aux_load / comfort_diagnostics / const / delta_t_diagnostics / diagnostics_engine / heat_source / models / probe_monitor / temperature_arbiter / weather_compensator / zone_combinations / zone_coordinator`。新引擎必须保持零 HA 依赖。
6. **实体 ID 由 `base.py` 的 `entity_id_for(domain, key)` 决定** → `sensor.cp_<key>`。平台文件**不得**自行设置 `entity_id`。
7. **翻译三处齐全**：`strings.json` / `translations/en.json` / `translations/zh-Hans.json`，有 `test_translations_sync` 守护。

---

## Task 1: 纯引擎 `energy_monitor.py` —— 核心累加逻辑

**Files:**
- Create: `custom_components/climatepilot/energy_monitor.py`
- Test: `tests/test_energy_monitor.py`（新建）

**Step 1: 写失败的测试**

覆盖 8 个场景（每个都要有，设计文档「测试策略」列出）：

```python
"""燃气与度日数累加器：跨日/跨月翻转、缺失样本、气表重置、重启恢复。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.climatepilot.energy_monitor import EnergyMonitor

BASE = 15.5


def at(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def test_first_sample_without_gas_yields_no_usage() -> None:
    """没有表值时必须返回 None，不能返回 0 —— 0 会被读成'今天一点气都没用'。"""
    m = EnergyMonitor(base_c=BASE)
    m.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=None)
    snap = m.snapshot(at(2026, 1, 1, 1))
    assert snap["gas_today"] is None


def test_degree_days_accumulate_over_a_day() -> None:
    """基准 15.5、恒温 5.0 持续 24 小时 -> 10.5 * 24 = 252 度·小时 = 10.5 度·日。"""
    m = EnergyMonitor(base_c=BASE)
    t = at(2026, 1, 1, 0)
    for i in range(97):
        m.note(t + timedelta(minutes=15 * i), outdoor_temp=5.0, gas_total=100.0)
    snap = m.snapshot(at(2026, 1, 2, 0))
    assert snap["degree_days_today"] == pytest.approx(10.5, abs=0.01)


def test_temperature_above_base_contributes_zero_not_negative() -> None:
    m = EnergyMonitor(base_c=BASE)
    t = at(2026, 1, 1, 0)
    for i in range(97):
        m.note(t + timedelta(minutes=15 * i), outdoor_temp=25.0, gas_total=100.0)
    snap = m.snapshot(at(2026, 1, 2, 0))
    assert snap["degree_days_today"] == 0.0


def test_missing_temperature_skips_instead_of_assuming_zero() -> None:
    """缺样本必须跳过并降低覆盖率，绝不猜 0°C（那会凭空放大度日数）。"""
    m = EnergyMonitor(base_c=BASE)
    m.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    m.note(at(2026, 1, 1, 0, 15), outdoor_temp=None, gas_total=100.0)
    m.note(at(2026, 1, 1, 0, 30), outdoor_temp=5.0, gas_total=100.0)
    snap = m.snapshot(at(2026, 1, 1, 1))
    assert snap["degree_days_today"] < 1.0   # 只有两个有效样本
    assert snap["coverage_today"] < 1.0


def test_day_rollover_freezes_yesterday() -> None:
    m = EnergyMonitor(base_c=BASE)
    m.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    m.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=110.0)
    m.note(at(2026, 1, 2, 0, 30), outdoor_temp=6.0, gas_total=112.0)
    snap = m.snapshot(at(2026, 1, 2, 1))
    assert snap["gas_yesterday"] == pytest.approx(10.0, abs=0.01)
    assert snap["gas_today"] == pytest.approx(0.0, abs=0.01)


def test_month_rollover_keeps_previous_month() -> None:
    m = EnergyMonitor(base_c=BASE)
    m.note(at(2026, 1, 31, 23), outdoor_temp=0.0, gas_total=100.0)
    m.note(at(2026, 2, 1, 0, 30), outdoor_temp=0.0, gas_total=105.0)
    snap = m.snapshot(at(2026, 2, 1, 1))
    assert snap["gas_this_month"] == pytest.approx(0.0, abs=0.01)
    assert snap["gas_previous_month"] == pytest.approx(5.0, abs=0.01)


def test_meter_reset_does_not_produce_negative_usage() -> None:
    m = EnergyMonitor(base_c=BASE)
    m.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=1000.0)
    m.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=5.0)   # 表被换/清零
    snap = m.snapshot(at(2026, 1, 1, 13))
    assert snap["gas_today"] is not None
    assert snap["gas_today"] >= 0.0
    assert snap["meter_reset_detected"] is True


def test_gas_per_degree_day_is_none_without_degree_days() -> None:
    """度日数为 0 时不能除 —— 返回 None，不是 0 也不是 inf。"""
    m = EnergyMonitor(base_c=BASE)
    m.note(at(2026, 1, 1, 0), outdoor_temp=25.0, gas_total=100.0)
    m.note(at(2026, 1, 1, 12), outdoor_temp=25.0, gas_total=105.0)
    snap = m.snapshot(at(2026, 1, 1, 13))
    assert snap["gas_per_degree_day"] is None


def test_restore_preserves_progress_within_the_day() -> None:
    m = EnergyMonitor(base_c=BASE)
    m.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    m.note(at(2026, 1, 1, 6), outdoor_temp=5.0, gas_total=103.0)
    saved = m.snapshot(at(2026, 1, 1, 6))

    m2 = EnergyMonitor(base_c=BASE)
    m2.restore(saved)
    m2.note(at(2026, 1, 1, 12), outdoor_temp=5.0, gas_total=106.0)
    snap = m2.snapshot(at(2026, 1, 1, 12))
    assert snap["gas_today"] == pytest.approx(6.0, abs=0.01)


def test_snapshot_is_json_serialisable() -> None:
    """要进 store，必须能 JSON 序列化。"""
    import json
    m = EnergyMonitor(base_c=BASE)
    m.note(at(2026, 1, 1, 0), outdoor_temp=5.0, gas_total=100.0)
    json.dumps(m.snapshot(at(2026, 1, 1, 1)))


def test_engine_does_not_import_homeassistant() -> None:
    """纯引擎铁律 1。"""
    import inspect
    from custom_components.climatepilot import energy_monitor
    src = inspect.getsource(energy_monitor)
    assert "from homeassistant" not in src
    assert "import homeassistant" not in src
```

**Step 2:** 运行确认全部失败（`ModuleNotFoundError`）

```
python -m pytest tests/test_energy_monitor.py -q
```

**Step 3: 实现**

关键设计点（**必须照做**）：
- 全部算术用 `dt_seconds / 3600` 把「度·小时」转「度·日」，**不是**除以样本数
- `day_covered_s` 只在**有有效温度**时累加 → 覆盖率分母用「当日已过秒数」
- 日/月翻转发生在 `note()` 里检测 `now.date()` / `(now.year, now.month)` 变化时
- 首次 `note()` 只记录起点，**不产生用量**（没有前一值）
- 表值下降 → 记 `meter_reset_detected`，用新值重置起点，不产出负数
- `restore()` 必须能读自己 `snapshot()` 的输出

**Step 4:** 运行至全绿

**Step 5:** 反向对照 —— 至少 4 个变异必须被抓到：

| 变异 | 应捕获的测试 |
|---|---|
| 温度高于基准时用 `base - t`（负数） | `test_temperature_above_base_contributes_zero_not_negative` |
| 缺温度时按 0 °C 计入 | `test_missing_temperature_skips_instead_of_assuming_zero` |
| 度日数除以样本数而非时间 | `test_degree_days_accumulate_over_a_day` |
| 无表值时返回 0.0 而非 None | `test_first_sample_without_gas_yields_no_usage` |
| 表重置时产出负数 | `test_meter_reset_does_not_produce_negative_usage` |

**Verify:**
```
python -m pytest tests/test_energy_monitor.py -q
ruff check custom_components/climatepilot/energy_monitor.py tests/test_energy_monitor.py
```

**Done:** 全部测试通过；零 HA 依赖；反向对照全部命中。

---

## Task 2: 常量、配置项与 options 表单

**Files:**
- Modify: `custom_components/climatepilot/const.py`（新增 2 个 `CONF_*`、默认值、单位常量）
- Modify: `custom_components/climatepilot/options_flow.py`（表单字段）
- Test: `tests/test_options_flow_gas.py`（新建）

**Step 1: 写失败的测试**

断言：`gas_meter_entity` 是**可选**字段（空字符串要被拒，或不填即关闭）；`degree_day_base_c` 有默认值；两者都在 `options.step.heat_source`（或新建 `energy` 步骤）的 `data` 里有翻译标签。

**Step 2-4:** 实现至全绿。

**注意：** 实机的 `heat_source` 步骤已经很拥挤（16 个字段）。**优先新建独立的 `energy` 步骤**，理由：燃气计量与热源/天气补偿是不同关注点，混在一起会让一个已经很长的表单更难读。

**Verify:**
```
python -m pytest tests/test_options_flow_gas.py tests/test_translations_sync.py -q
```

**Done:** 三处翻译齐全且被 `test_translations_sync` 守护；留空即关闭功能。

---

## Task 3: 协调器接线

**Files:**
- Modify: `custom_components/climatepilot/coordinator.py`
- Test: `tests/test_coordinator_energy.py`（新建）

**Step 1: 写失败的测试**

- 未配置 `gas_meter_entity` → `energy` 状态全为 `None`，**且不读任何实体**
- 配置后 → 每个审计周期调用 `note()`
- 室外温度实体不可用 → 传 `None`，度日数不变
- 表实体不可用 → 传 `None`，用量不变
- `_state_payload()` 含 `"energy"` 键，且 `restore` 后进度保留

**Step 2-4:** 实现。

**关键：** 复用已有的 `self.hass.states.get(...)` 读取模式（`coordinator.py:633` 读室外温度已有先例），**不要**用 `async_track_state_change_event` 另建监听 —— 15 分钟轮询足够。

**Step 5:** 反向对照：把 `note()` 的调用去掉，测试必须变红。

**Verify:**
```
python -m pytest tests/test_coordinator_energy.py -q
```

**Done:** store 往返完整；未配置时零开销。

---

## Task 4: 6 个传感器实体

**Files:**
- Modify: `custom_components/climatepilot/sensor.py`
- Modify: `custom_components/climatepilot/__init__.py`（若需注册）
- Test: `tests/test_gas_sensors.py`（新建）

**Step 1: 写失败的测试**

断言 6 个实体：ID 是 `sensor.cp_*`（**不是** `wo_de_jia_*`）、`state_class` 正确、**`unit_of_measurement` 字符串固定**（`m³` / `°C·d` / `m³/°C·d`）、`extra_state_attributes` 含全屋口径声明。

**Step 2-4:** 实现。

**必须包含的属性**（设计文档「三条边界」）：
- 用量类实体：`scope: "whole_house"`、`includes_dhw_and_cooking: True`
- `m³/度日`：明确写出**不是**效率指标（对照 V0.3 的 `is_power_measurement: False` 惯例）

**Step 5:** 反向对照：把某个单位改成会变的（如按条件返回不同字符串），契约测试必须变红。

**Verify:**
```
python -m pytest tests/test_gas_sensors.py tests/test_integration_setup.py tests/test_platform_entities.py -q
```

**Done:** 实体出现在真实注册表中且 ID 为 `cp_`；单位稳定；口径属性齐全。

---

## Task 5: 整机验证与文档

**Files:**
- Modify: `tests/test_integration_setup.py`（实体总数 65 → 71）
- Modify: `README.md`、`CHANGELOG.md`
- Modify: `docs/DASHBOARD.md`、`dashboards/climatepilot.yaml`
- Modify: `custom_components/climatepilot/manifest.json` → `0.5.0`

**Steps:**
1. 更新实体计数契约（实机 3 房间、65 个 → 配了燃气后 71 个；**未配置时仍是 65**，两种都要测）

   注意：燃气实体是**系统级**（不分房间），所以新增数固定为 **+6**，与房间数无关。

2. README 增加一节「燃气与度日数（仪表，不是省气证明）」，**必须写清**：
   - 表是全屋的，含热水与做饭
   - `m³/度日` 是可比的**序列**，不是效率结论
   - 为什么本集成**不**提供「省了 X%」

3. CHANGELOG 增加 0.5.0 段。

4. **两个 HA 版本**跑全量：HA 2026.2.3 与 HA 2026.9.4。

5. `ruff check` + `ruff format --check`。

**Verify:**
```
python -m pytest -q
& "$env:TEMP\ha994\venv\Scripts\python.exe" -m pytest -q
ruff check custom_components/ tests/ tools/ && ruff format --check custom_components/ tests/ tools/
```

**Done:** 双版本全绿；文档与代码一致；无临时脚本残留。

---

## 完成前漂移检查

逐条核对：

- [ ] 6 个实体全部 ID 为 `sensor.cp_*`，无一个含拼音
- [ ] 单位字符串在契约测试里被固定（防统计被静默抑制）
- [ ] **未配置 `gas_meter_entity` 时实体不创建**，且总数回到 65
- [ ] 度日数在温度缺失时**不猜 0**，覆盖率写入属性
- [ ] 气表重置不产出负数
- [ ] `m³/度日` 的属性写明**不是效率指标**
- [ ] 用量实体的属性写明 `scope: whole_house` 与含热水/做饭
- [ ] **没有任何代码路径因此功能而写入设备** —— 这是只读仪表
- [ ] 未新增定时器；复用 `periodic_audit`
- [ ] 纯引擎零 HA 依赖
- [ ] 三处翻译齐全
- [ ] 文档**没有**出现「省气 X%」这类结论性表述

## 分层 Review

| 层 | 关注点 |
|---|---|
| **产品** | 定位是否守住「仪表而非证明」；有没有偷偷变成省气结论 |
| **工程** | O(1) 状态；JSON 可序列化；重启不丢当日进度；无新增定时器 |
| **数据语义** | 全屋口径声明；缺失样本不猜 0；单位稳定性；气表重置 |
| **验证** | 双 HA 版本；反向对照；真实注册表契约；无临时文件残留 |

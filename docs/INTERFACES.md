# ClimatePilot — 内部接口契约（Implementers 必读）

> 本文件是并行开发的**唯一接口事实来源**。任何模块签名冲突以本文件为准。
> 设计依据：`Heating Coordinator 技术设计方案 V1.0`（尤其第 4、5、6、7、8、9、10、11 节）。

## 0. 铁律

1. **纯引擎不得 import Home Assistant。** 只有 `climate_adapter.py`、`coordinator.py`、`store.py`、
   `config_flow.py`、`options_flow.py`、`diagnostics.py`、`base.py`、`__init__.py` 及 `sensor.py`
   等平台文件可以 `from homeassistant...`。
   纯引擎 = `models.py`, `temperature_arbiter.py`, `zone_coordinator.py`, `heat_source.py`,
   `weather_compensator.py`, `diagnostics_engine.py`。这样它们可以脱离 HA 单元测试。
2. **所有时间参数都是带时区的 `datetime`**（HA 用 `dt_util.utcnow()`，测试用固定 UTC）。
   引擎内部只用传入的 `now`，**绝不调用 `datetime.now()`**。
3. **一个房间一个有效目标。** 只有 `zone_coordinator` 决定是否写 KNX。
4. **默认安全：** 任何不确定（实体 unavailable、反馈不匹配、语义未验证）→ 不写、进 DEGRADED/uncertain，
   绝不猜 0 需求、绝不猜温度。
5. 所有面向用户的字符串都要有 `strings.json` + `translations/en.json` + `translations/zh-Hans.json`。
6. 平台文件遵守 HA 现代写法：`_attr_has_entity_name = True`、`translation_key`、`unique_id` 稳定、
   `async_setup_entry` + `async_add_entities`，不要 legacy `entity_id` 参数。

## 1. 已冻结的公共符号

`const.py` 与 `models.py` 已由 Lead 写好并冻结（需要新增常量请向 Lead 申请，不要自己改这两个文件）。

关键 `const.py` 常量：`DOMAIN="climatepilot"`, `DEFAULT_NAME="ClimatePilot"`,
`MANUFACTURER="HomeAxis"`, `MODEL="Heating Coordinator"`；
`CONTROL_MODE_OBSERVE/COORDINATE/PAUSED`；`ZONE_MODE_AUTO/COMFORT/ECO/AWAY/MANUAL`；
`OVERRIDE_NEXT_SCHEDULE/FIXED_DURATION/INDEFINITE`；`SOURCE_*`；`REASON_*`；`FEEDBACK_*`；
`FSM_OBSERVE/IDLE/PREPARE/HEATING/SATISFIED/DEGRADED`；`DECISION_*`；各 `DEFAULT_*`。

`models.py` 公共类型（`from .models import ...`）：
`ScheduleSlot`, `Schedule`, `DEFAULT_SLOTS`, `WeatherCurve`, `PendingCommand`, `ConfirmedWrite`,
`ManualOverride`, `SetpointDecision`, `WriteDecision`, `ZoneState`, `AuxLoadConfig`, `AuxLoadState`,
`with_delta`。

`base.py` 公共类：`ClimatePilotEntity`, `ClimatePilotZoneEntity`, `ClimatePilotDiagnosticEntity`。

## 2. 各模块契约

### 2.1 `temperature_arbiter.py` （Lead 负责，纯引擎）

```python
@dataclass(frozen=True, slots=True)
class ArbiterConfig:
    cold_adjustment_enabled_by_default: bool = False
    cold_adjustment_delta: float = 1.0

class TemperatureArbiter:
    def __init__(self, config: ArbiterConfig | None = None) -> None: ...
    def arbitrate(self, zone: ZoneState, now: datetime, *,
                  schedule: Schedule | None = None,
                  cold_adjustment: float = 0.0,
                  aux_delta: float = 0.0) -> SetpointDecision: ...
    def note_external_change(self, zone: ZoneState, value: float, now: datetime, *,
                             policy: str = OVERRIDE_NEXT_SCHEDULE,
                             duration: timedelta | None = None,
                             source_entity: str | None = None) -> ManualOverride: ...
    def note_schedule_transition(self, zone: ZoneState, now: datetime) -> bool: ...
    def resume_auto(self, zone: ZoneState, now: datetime) -> None: ...
    def expire_overrides(self, zones: Iterable[ZoneState], now: datetime) -> list[str]: ...
```

* P0 范围裁剪 → P1 手动覆盖 → P2 固定模式 → P3 日程 → P4 自动修正。
* `SetpointDecision.reason_codes` 必须包含 `REASON_MANUAL_OVERRIDE` / `REASON_SCHEDULE_TRANSITION` /
  `REASON_COLD_ADJUSTMENT` / `REASON_AUX_LOAD` / `REASON_RANGE_CLAMP` 等。
* **手动覆盖生效时 `cold_adjustment` 与 `aux_delta` 必须被忽略**（设计文档 4.1）。

### 2.2 `weather_compensator.py`（Teammate `engines`）

```python
@dataclass(frozen=True, slots=True)
class WeatherReading:
    value: float | None
    updated_at: datetime | None
    quality: str          # FEEDBACK_VALID / FEEDBACK_STALE / FEEDBACK_UNAVAILABLE

@dataclass(frozen=True, slots=True)
class CompensationResult:
    suggested: float | None
    outdoor: float | None
    quality: str
    reason_codes: tuple[str, ...]
    should_apply: bool          # 是否越过防抖/限幅，值得下发

class WeatherCompensator:
    def __init__(self, curve: WeatherCurve, *,
                 stale_after: float = DEFAULT_WEATHER_STALE_AFTER,
                 hysteresis: float = DEFAULT_WEATHER_HYSTERESIS,
                 min_interval: float = DEFAULT_WEATHER_MIN_INTERVAL,
                 mode: str = WEATHER_MODE_SUGGEST) -> None: ...
    def evaluate(self, reading: WeatherReading, now: datetime, *,
                 night: bool = False) -> CompensationResult: ...
    def note_applied(self, value: float, now: datetime) -> None: ...
    @property
    def last_applied(self) -> float | None: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, data: dict[str, Any]) -> None: ...
```

* 数据过期/无效 → **保持最后已确认值**，`reason_codes += (REASON_WEATHER_STALE,)`，
  **不得按 0℃ 计算**（设计文档 7）。
* `mode == WEATHER_MODE_SUGGEST` → `should_apply` 恒为 False（仅建议值）。
* 防抖：与 `last_applied` 差值 < `hysteresis` 或距上次下发 < `min_interval` → `should_apply=False`
  且 `reason_codes += (REASON_TOLERANCE|REASON_THROTTLED,)`。

### 2.3 `heat_source.py`（Teammate `engines`）

```python
@dataclass(frozen=True, slots=True)
class BoilerCapabilities:
    entity_id: str
    min_temp: float | None
    max_temp: float | None
    target_step: float
    hvac_modes: tuple[str, ...]
    semantics_verified: bool        # 三项用户声明全为真
    min_flow_verified: bool

@dataclass(frozen=True, slots=True)
class HeatSourceConfig:
    demand_on_debounce: float = DEFAULT_DEMAND_ON_DEBOUNCE
    demand_off_debounce: float = DEFAULT_DEMAND_OFF_DEBOUNCE
    valve_preopen_wait: float = DEFAULT_VALVE_PREOPEN_WAIT
    min_on_time: float = DEFAULT_MIN_ON_TIME
    min_off_time: float = DEFAULT_MIN_OFF_TIME
    pump_overrun: float = DEFAULT_PUMP_OVERRUN

@dataclass(frozen=True, slots=True)
class HeatSourceInput:
    now: datetime
    active_zones: int            # 真实需求分区数
    unknown_zones: int           # 状态不可用/不确定的分区数
    control_mode: str            # observe / coordinate / paused
    boiler_mode: str | None      # 当前 hvac mode
    boiler_available: bool
    zones_faulted: bool = False

@dataclass(frozen=True, slots=True)
class HeatSourceDecision:
    state: str                   # FSM_*
    decision: str                # DECISION_*
    target_mode: str | None      # "heat" / "off" / None
    target_supply: float | None
    wait_seconds: float | None   # 还需等待多久再评估
    reason_codes: tuple[str, ...]

class HeatSourceStateMachine:
    def __init__(self, config: HeatSourceConfig | None = None,
                 capabilities: BoilerCapabilities | None = None) -> None: ...
    def evaluate(self, data: HeatSourceInput, *, supply: float | None) -> HeatSourceDecision: ...
    @property
    def state(self) -> str: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, data: dict[str, Any]) -> None: ...
    def note_demand_change(self, now: datetime, has_demand: bool) -> None: ...
```

* 需求消抖、阀门预开等待、最短开/停机、`DEGRADED`（通讯异常 → 停止新写入）。
* **三条核验（`semantics_verified`）是"写入锅炉"的合并前置条件**，对启停与水温一视同仁：
  未核验时 `evaluate()` 只返回 `DECISION_NONE`（`DECISION_SET_SUPPLY` 也不发），集成仅通过
  `sensor.<system>_suggested_supply_temp` 输出**建议值**供人查看。理由见设计文档 6.3 与 14：
  写错水温就是把生活热水或室温改掉，与写错启停同样危险。
* **绝不用软件最短运行时长强迫燃烧器在全部执行器关闭后继续加热**（设计文档 6.2 末）。
* 因语义未核验而不动作时，`reason_codes` 必须带上 `REASON_HEAT_SOURCE_SEMANTICS_UNVERIFIED`
  （若最小流量亦未核验，再加 `REASON_BOILER_MIN_FLOW_UNVERIFIED`），以便 UI 解释"为何无动作"。

### 2.4 `climate_adapter.py`（Teammate `adapter`）

可以 import HA。

```python
@dataclass(frozen=True, slots=True)
class ClimateSnapshot:
    entity_id: str
    exists: bool
    state: str | None            # "heat" / "off" / "unavailable" / ...
    hvac_action: str | None
    temperature: float | None    # 设定值
    current_temperature: float | None
    min_temp: float | None
    max_temp: float | None
    target_step: float | None
    hvac_modes: tuple[str, ...]
    last_updated: datetime | None
    supports_action: bool

class ClimateAdapter:
    def __init__(self, hass: HomeAssistant) -> None: ...
    @callback
    def snapshot(self, entity_id: str) -> ClimateSnapshot: ...
    def to_zone_state(self, zone_config: Mapping[str, Any], snapshot: ClimateSnapshot) -> ZoneState: ...
    async def async_set_temperature(self, entity_id: str, temperature: float) -> bool: ...
    async def async_set_hvac_mode(self, entity_id: str, mode: str) -> bool: ...
    async def async_set_supply_temperature(self, entity_id: str, temperature: float) -> bool: ...
    @staticmethod
    def is_stale(snapshot: ClimateSnapshot, now: datetime, max_age: float) -> bool: ...
```

* 写失败要**返回 False 而不抛异常**（上层负责退避重试）；`unavailable` 时直接 False。
* 只允许 `climate.set_temperature` / `climate.set_hvac_mode`；**不得**直接操作 KNX 组地址。

### 2.5 `zone_coordinator.py`（Lead 负责，纯引擎）

负责：日程解析 → 调用仲裁器 → 回声/外部修改判定 → 写入节流与退避 → 辅助热负载。
外部（`coordinator.py`）只做 I/O。

### 2.6 `store.py`（Teammate `adapter`）

```python
class ClimatePilotStore:
    def __init__(self, hass: HomeAssistant, system_id: str) -> None: ...
    async def async_load(self) -> dict[str, Any]: ...
    async def async_save(self, data: dict[str, Any]) -> None: ...
    async def async_remove(self) -> None: ...
```
用 `homeassistant.helpers.storage.Store`，`STORAGE_VERSION` 与 `STORAGE_KEY_PREFIX` 来自 const，
并实现 `_async_migrate_func`。

### 2.7 `diagnostics_engine.py`（Teammate `engines`）

```python
class BurnerStats:
    def note_state(self, state: str | None, now: datetime) -> None: ...
    def snapshot(self, now: datetime) -> dict[str, Any]: ...
    def restore(self, data: dict[str, Any]) -> None: ...
```
统计：最近一次燃烧时长、1h/24h 点火次数、周期直方图、短循环告警。
**必须区分采暖燃烧 / 生活热水燃烧**；若设备只给通用状态，标记 `includes_dhw=True`（设计文档 8）。

### 2.8 `coordinator.py`（Lead 负责）

`ClimatePilotCoordinator(DataUpdateCoordinator[dict])`，属性/方法供平台层使用：

```python
coordinator.system_id: str            # 唯一，取自 entry.entry_id
coordinator.name: str                 # 系统名
coordinator.control_mode: str         # observe/coordinate/paused
coordinator.zones: dict[str, ZoneState]
coordinator.zone_decisions: dict[str, SetpointDecision]
coordinator.heat_state: str           # FSM_*
coordinator.heat_decision: HeatSourceDecision
coordinator.compensation: CompensationResult
coordinator.diagnostics_snapshot: dict[str, Any]
coordinator.state_machine: HeatSourceStateMachine
coordinator.arbiter: TemperatureArbiter
coordinator.zone_coordinator: ZoneCoordinator
coordinator.adapter: ClimateAdapter
coordinator.weather: WeatherCompensator
coordinator.burner: BurnerStats
def zone_schedule(self, zone_id: str) -> Schedule: ...
async def async_set_control_mode(self, mode: str) -> None: ...
async def async_resume_auto(self, zone_id: str | None = None) -> None: ...
async def async_set_zone_mode(self, zone_id: str, mode: str) -> None: ...
async def async_reassess(self) -> None: ...
```

### 2.9 平台层（Teammate `platforms`）

| 文件 | 实体 |
|---|---|
| `select.py` | `select.<system>_control_mode`, `select.<room>_control_mode` |
| `sensor.py` | `sensor.<system>_state`, `_active_zones`, `_suggested_supply_temp`, `_burner_starts_24h`, `_last_burn_duration`, `_outdoor_temperature`; `sensor.<room>_effective_setpoint`, `_setpoint_source`, `_current_temperature` |
| `binary_sensor.py` | `binary_sensor.<room>_manual_override`, `binary_sensor.<system>_short_cycle_alert`, `binary_sensor.<system>_degraded` |
| `button.py` | `button.<room>_resume_auto`, `button.<system>_reassess` |
| `number.py` | `number.<system>_supply_temperature_override`（可选，写锅炉目标水温） |
| `switch.py` | `switch.<room>_auxiliary_load`（辅助热负载使能） |
| `climate.py` | 全屋主控（Lead 负责），**不复制**每个 KNX 房间实体 |

## 3. 协作纪律

- **只改自己 write scope 内的文件。** 需要改 `const.py`/`models.py`/`base.py`/`climate_adapter.py`/
  `coordinator.py` 的接口，发消息给 Lead，不要直接改。
- 提交前自查：`ruff check .`（已全局安装）与 `python -m compileall custom_components`。
- 每个模块要能被 `pytest` 导入且不触发 HA 初始化（纯引擎）。

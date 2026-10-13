# ClimatePilot V0.3 — 内部接口契约（Implementers 必读）

> 本文件是 V0.3 并行开发的**唯一接口事实来源**。与 `docs/INTERFACES.md`（V0.1）、
> `docs/INTERFACES_V0.2.md`（V0.2）共同生效；冲突时**以本文件为准**（仅限 V0.3 新增部分）。
>
> **V0.1 与 V0.2 的契约继续有效**，尤其：
> 纯引擎不得 import Home Assistant、时间必须由参数注入、`snapshot()/restore()` 往返、
> 缺失/未知值绝不当作 0 或"正常"。

## 1. V0.3 范围（设计方案第 12 节）

| 交付项 | 落点 |
|---|---|
| 回水探头、可选外贴供水探头；**传感器可用性和偏差检测** | `probe_monitor.py`（新，纯引擎） |
| **ΔT 趋势监测、冷凝工况评估、不同区域组合的水力诊断** | `delta_t_diagnostics.py`（新，纯引擎） |
| **有数据后校准天气补偿参数**（只建议，不自动改） | `delta_t_diagnostics.py` + 实体 |
| 探头质量标签贯穿始终 | 所有新实体 |

**交付标准（原文档）：** 传感器失联**不影响核心运行**；诊断数据可解释、有质量标签。

**贯穿全篇的两条硬约束（必须有测试）：**
1. **探头只作监测/优化输入，绝不承担锅炉安全停机功能。** 探头故障不得改变任何写入决策。
2. **温度差不等于热功率。** 不得由 ΔT 推算"真实热效率/热输出"。ΔT 只是流动与冷凝工况的
   **间接**指标，所有输出必须带此限定。

## 2. 已冻结的 V0.3 常量（`const.py`，Lead 已写好，**不要改**）

**探头质量：** `PROBE_VALID` / `PROBE_STALE` / `PROBE_IMPLAUSIBLE` / `PROBE_FROZEN` /
`PROBE_UNAVAILABLE`；`PROBE_QUALITIES`。
**DS18B20 哨兵：** `DS18B20_ERROR_C = 85.0`、`DS18B20_ERROR_TOLERANCE = 0.5`。
**阈值：** `DEFAULT_PROBE_STALE_AFTER`、`DEFAULT_PROBE_FROZEN_AFTER`、`DEFAULT_PROBE_MIN_C`、
`DEFAULT_PROBE_MAX_C`、`DEFAULT_PROBE_MAX_DIVERGENCE`、`DEFAULT_DELTA_T_WINDOW`、
`DEFAULT_DELTA_T_SAMPLE_INTERVAL`、`DEFAULT_DELTA_T_MIN_STABLE_SECONDS`、
`DEFAULT_CONDENSING_RETURN_C`、`DEFAULT_DELTA_T_HEALTHY_MIN`、`DEFAULT_DELTA_T_HEALTHY_MAX`。
**判定：** `CONDENSING_LIKELY/UNLIKELY/UNKNOWN`；`HYDRAULIC_OK/LOW_FLOW/HIGH_FLOW/UNBALANCED/UNKNOWN`；
`CALIBRATION_NONE/RAISE_CURVE/LOWER_CURVE/INSUFFICIENT_DATA`。
**原因码：** `REASON_PROBE_*`、`REASON_DELTA_T_*`、`REASON_NOT_A_POWER_MEASUREMENT`。
**配置键：** `CONF_SUPPLY_PROBE_ENTITY`、`CONF_PROBE_STALE_SECONDS`、`CONF_PROBE_FROZEN_SECONDS`、
`CONF_DELTA_T_WINDOW_SECONDS`、`CONF_CONDENSING_RETURN_C`、`CONF_DELTA_T_HEALTHY_MIN`、
`CONF_DELTA_T_HEALTHY_MAX`、`CONF_PROBE_MAX_DIVERGENCE`；
（V0.1 已有）`CONF_RETURN_TEMP_ENTITY`、`CONF_SUPPLY_TEMP_ENTITY`。

## 3. 新增实体 translation_key（**必须逐字使用**）

**V0.3 冻结的"英文名 → entity_id"契约（E-I18N 与 E-UI 双方都必须遵守）：**

| translation_key | **英文名（en.json 必须逐字使用）** | 推导出的 entity_id |
|---|---|---|
| `return_temperature` | `Return temperature` | `sensor.<dev>_return_temperature` |
| `delta_t` | `Delta T` | `sensor.cp_delta_t` |
| `delta_t_quality` | `Delta T quality` | `sensor.cp_delta_t_quality` |
| `condensing_regime` | `Condensing regime` | `sensor.cp_condensing_regime` |
| `hydraulic_verdict` | `Hydraulic verdict` | `sensor.cp_hydraulic_verdict` |
| `curve_calibration_advice` | `Curve calibration advice` | `sensor.cp_curve_calibration_advice` |
| `probe_fault` | `Probe fault` | `binary_sensor.cp_probe_fault` |

> **V0.4 更新：** 上表的第三列原为 `sensor.<dev>_delta_t` 等，其中
> `<dev> = slugify(系统名)`（参考安装「我的家」→ `wo_de_jia`）。V0.4 起实体 ID 固定为
> `<domain>.cp_<key>`，与设备名无关，故第三列已按新规则改写。第二列的 7 个英文名
> 仍然是显示名契约，未变。

**这 7 个英文名经过实测校验：`slugify(英文名) == translation_key`**（全部 7/7 相等）。
V0.4 之前这一点很重要，因为当时的 entity_id 由英文名派生；现在 ID 直接由 `key` 决定，
这一相等性不再是 ID 正确性的前提，但**E-I18N 仍不得改动这 7 个英文名**（它们是显示名契约）。
E-UI 按此写 dashboard，并在 `test_integration_setup.py` 里用
**真实 entity_registry** 双向校验（不得用与被验证对象相同的假设推导）。

**实体清单：**

| 平台 | translation_key | 归属 | 说明 |
|---|---|---|---|
| `sensor` | `return_temperature` | 系统级 | 回水温度（外贴探头） |
| `sensor` | `delta_t` | 系统级 | 供回水温差 ΔT |
| `sensor` | `delta_t_quality` | 系统级（诊断） | 探头质量汇总：valid/stale/implausible/frozen/unavailable |
| `sensor` | `condensing_regime` | 系统级（诊断） | likely/unlikely/unknown |
| `sensor` | `hydraulic_verdict` | 系统级（诊断） | ok/low_flow/high_flow/unbalanced/unknown |
| `sensor` | `curve_calibration_advice` | 系统级（诊断） | none/raise_supply_curve/lower_supply_curve/insufficient_data |
| `binary_sensor` | `probe_fault` | 系统级 | 任一探头故障 |

**state 翻译必需**（否则 UI 显示原始码）：
- `sensor.delta_t_quality` → `PROBE_QUALITIES` 五个值
- `sensor.condensing_regime` → `likely` / `unlikely` / `unknown`
- `sensor.hydraulic_verdict` → `ok` / `low_flow` / `high_flow` / `unbalanced` / `unknown`
- `sensor.curve_calibration_advice` → `none` / `raise_supply_curve` / `lower_supply_curve` / `insufficient_data`

## 4. 模块契约

### 4.1 `probe_monitor.py`（纯引擎，E-PROBE 负责）

```python
@dataclass(frozen=True, slots=True)
class ProbeReading:
    """One raw observation of a pipe probe, before any judgement."""
    entity_id: str
    value: float | None          # None = entity missing/unavailable/non-numeric
    updated_at: datetime | None  # state.last_updated
    raw: str | None = None       # original state string, for diagnostics

@dataclass(frozen=True, slots=True)
class ProbeAssessment:
    """A probe reading plus the quality verdict and why."""
    entity_id: str
    value: float | None          # 只在该值可信时非 None
    quality: str                 # PROBE_*
    reason_codes: tuple[str, ...]
    age_seconds: float | None
    frozen_seconds: float | None
    @property
    def usable(self) -> bool: ...          # quality == PROBE_VALID
    def as_dict(self) -> dict[str, Any]: ...

@dataclass(frozen=True, slots=True)
class ProbePairAssessment:
    """Two probes on the same hydraulic loop."""
    supply: ProbeAssessment
    return_: ProbeAssessment
    divergence: float | None
    reason_codes: tuple[str, ...]
    @property
    def both_usable(self) -> bool: ...
    def as_dict(self) -> dict[str, Any]: ...

class ProbeMonitor:
    def __init__(
        self,
        *,
        stale_after: float = DEFAULT_PROBE_STALE_AFTER,
        frozen_after: float = DEFAULT_PROBE_FROZEN_AFTER,
        min_value: float = DEFAULT_PROBE_MIN_C,
        max_value: float = DEFAULT_PROBE_MAX_C,
        max_divergence: float = DEFAULT_PROBE_MAX_DIVERGENCE,
        sentinel: float = DS18B20_ERROR_C,
        sentinel_tolerance: float = DS18B20_ERROR_TOLERANCE,
    ) -> None: ...
    def assess(self, reading: ProbeReading, now: datetime, *,
               burning: bool = False) -> ProbeAssessment: ...
    def assess_pair(self, supply: ProbeReading, return_: ProbeReading,
                    now: datetime, *, burning: bool = False) -> ProbePairAssessment: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, data: dict[str, Any] | None) -> None: ...
```

**判定顺序与语义（关键，必须逐条测试）：**

1. `value is None` → `PROBE_UNAVAILABLE`（+`REASON_PROBE_MISSING`）。
2. **DS18B20 哨兵**：`abs(value - 85.0) <= 0.5` → `PROBE_IMPLAUSIBLE`
   （+`REASON_PROBE_SENTINEL`）。**这是最重要的一条**：85℃ 看起来像"很热的供水管"，
   天真的实现会报出根本不存在的锅炉故障，或算出一个"巨大 ΔT"看起来像水力成功。
3. 越界（`< min_value` 或 `> max_value`）→ `PROBE_IMPLAUSIBLE`（+`REASON_PROBE_OUT_OF_RANGE`）。
4. 陈旧：`age > stale_after` → `PROBE_STALE`（+`REASON_PROBE_STALE`）。
5. 卡死：**仅在 `burning=True` 时**判定 —— 值完全未变持续 `frozen_after` → `PROBE_FROZEN`
   （+`REASON_PROBE_FROZEN`）。冷却空闲的管子合法地几小时不变，所以非燃烧时**不得**报 frozen。
6. 否则 `PROBE_VALID`。

**`assess_pair`：** 两者都 `usable` 时算 `divergence = abs(supply - return_)`；
超过 `max_divergence` → 在 `reason_codes` 加 `REASON_PROBE_DIVERGENT`，
**并把两个探头的 quality 降为 `PROBE_IMPLAUSIBLE`**（无法判断哪个错 → 两个都不可信）。
任一探头不可用时 `divergence = None`（**不得**用单边值凑一个差值）。

### 4.2 `delta_t_diagnostics.py`（纯引擎，E-DELTA 负责）

```python
@dataclass(frozen=True, slots=True)
class DeltaTSample:
    at: datetime
    delta_t: float
    supply_c: float
    return_c: float

@dataclass(frozen=True, slots=True)
class DeltaTReport:
    """Delta-T trend and its indirect hydraulic/condensing reading."""
    delta_t: float | None            # 当前值（仅当两个探头都 usable）
    average: float | None
    minimum: float | None
    maximum: float | None
    trend_per_hour: float | None     # 每秒/小时的变化率，正=ΔT 在扩大
    stable_seconds: float
    samples: int
    quality: str                     # PROBE_*（取较差的那一侧）
    condensing: str                  # CONDENSING_*
    hydraulic: str                   # HYDRAULIC_*
    calibration: str                 # CALIBRATION_*
    reason_codes: tuple[str, ...]
    def as_dict(self) -> dict[str, Any]: ...

class DeltaTDiagnostics:
    def __init__(
        self,
        *,
        window: float = DEFAULT_DELTA_T_WINDOW,
        sample_interval: float = DEFAULT_DELTA_T_SAMPLE_INTERVAL,
        min_stable_seconds: float = DEFAULT_DELTA_T_MIN_STABLE_SECONDS,
        healthy_min: float = DEFAULT_DELTA_T_HEALTHY_MIN,
        healthy_max: float = DEFAULT_DELTA_T_HEALTHY_MAX,
        condensing_return_c: float = DEFAULT_CONDENSING_RETURN_C,
    ) -> None: ...
    def note(self, *, supply: float | None, return_: float | None,
             burning: bool, active_zones: int, now: datetime) -> None: ...
    def report(self, now: datetime) -> DeltaTReport: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, data: dict[str, Any] | None) -> None: ...
```

**语义要求（关键）：**

- `note()` 在 **任一探头为 None** 时不记样本（不把缺失当 0）。
  采样节流：距上次样本 `< sample_interval` 时跳过记录。
- **`hydraulic`**：只在 `stable_seconds >= min_stable_seconds` 且有样本时判定；
  `delta_t < healthy_min` → `HYDRAULIC_LOW_FLOW`；`> healthy_max` → `HYDRAULIC_HIGH_FLOW`；
  否则 `HYDRAULIC_OK`。样本不足 → `HYDRAULIC_UNKNOWN` + `REASON_DELTA_T_UNSTABLE`。
  **注意**：低 ΔT 在恒流量泵上通常意味着**流量过大或旁通短路**，高 ΔT 意味着**流量不足**；
  两者都是水力信号，不是"效率好坏"，措辞与 reason code 必须体现这一点。
- **`condensing`**：只有**稳定**且**在燃烧**的样本才评估；`return_c <= condensing_return_c`
  → `CONDENSING_LIKELY`，否则 `CONDENSING_UNLIKELY`；数据不足 → `CONDENSING_UNKNOWN`。
  这是**间接指标**，`as_dict()` 必须包含 `REASON_NOT_A_POWER_MEASUREMENT` 的布尔标记
  `is_power_measurement = False`。
- **`calibration`**：纯建议。规则（可用你判断的等价规则，但必须写明并测试）：
  在窗口内、稳定、且有房间**欠温**证据时 → `CALIBRATION_RAISE_CURVE`；
  有房间**过热**证据时 → `CALIBRATION_LOWER_CURVE`；
  两者都无或数据不足 → `CALIBRATION_NONE` / `CALIBRATION_INSUFFICIENT_DATA`。
  **绝不自动改曲线**（curve 由用户配置持有）。
  为让 E-DELTA 不依赖 comfort 引擎，签名改为：
  ```python
  def note(self, *, supply, return_, burning, active_zones, now,
           underheating_zones: int = 0, overheating_zones: int = 0) -> None: ...
  ```
  `calibration` 由 `report()` 依据窗口内的欠温/过热证据与 ΔT 是否健康给出。
- `quality`：取供回两侧**较差**的质量（`PROBE_QUALITIES` 顺序即严重度递增，
  实现可用显式映射，但 `PROBE_UNAVAILABLE` 必须比 `PROBE_VALID` 差）。
- 窗口滚动淘汰。`report()` 对固定 `now` 幂等。

### 4.3 `coordinator.py` 接入（Lead 负责）

新增公开属性（E-UI 依赖）：
```python
coordinator.probes: ProbePairAssessment          # 每周期刷新
coordinator.delta_t: DeltaTReport
coordinator.probe_fault: bool                     # 任一探头非 valid
```

### 4.4 平台实体（E-UI-V3 负责）

- 第 3 节的 7 个实体（6 sensor + 1 binary_sensor）。
- 探头**未配置**时：`return_temperature` / `delta_t` 返回 `None`（state `unknown`）且
  `available` 仍为 True（"没装探头"不是"实体坏了"）；`binary_sensor.probe_fault` = off。
- **新增实体后总数变化**：2 zone 时 44 → **51**（+6 sensor +1 binary_sensor，均为系统级）。
  请用公式推导，不要留孤立魔法数字。
- `extra_state_attributes` 放 `probes.as_dict()` / `delta_t.as_dict()`（可解释性）。

### 4.5 配置与选项流程（E-FLOW-V3 负责）

- `boiler` 步骤新增"外贴探头"选择：`CONF_SUPPLY_PROBE_ENTITY`、`CONF_RETURN_TEMP_ENTITY`
  两个 `EntitySelector`（`device_class: temperature`），**均为可选**。
- options flow 的 `heat_source`（或新增 `probes` 步骤）暴露
  `CONF_PROBE_STALE_SECONDS` / `CONF_PROBE_FROZEN_SECONDS` / `CONF_DELTA_T_WINDOW_SECONDS` /
  `CONF_CONDENSING_RETURN_C` / `CONF_DELTA_T_HEALTHY_MIN` / `CONF_DELTA_T_HEALTHY_MAX` /
  `CONF_PROBE_MAX_DIVERGENCE`。
- 校验：`delta_t_healthy_min < delta_t_healthy_max`，否则错误 `invalid_delta_t_range`。
- strings/translations 同步（E-I18N-V3 负责）。

### 4.6 翻译与文档（E-I18N-V3 负责）

- 第 3 节的 7 个 entity key + 4 组 state 翻译，三份 JSON key 树保持一致。
- `manifest.json` → `0.3.0`；`CHANGELOG.md` 加 0.3.0 段。
- `README.md` 加 V0.3 章节（实体表 +7、路线图）。
- `docs/DEPLOYMENT.md` 加 V0.3 一节：**探头安装要求**（锅炉外部采暖供回水**金属直管**、
  导热固定 + 保温、双探头优于单回水）、85℃ 哨兵的含义、如何读 ΔT、
  **明确"ΔT 不是热功率/效率"**、探头失联不影响采暖。
- `docs/DASHBOARD.md` + `dashboards/climatepilot.yaml` 加 V0.3 卡片（E-UI-V3 的 scope）。

## 5. 实测基线（当前仓库状态）

- `pytest -q` → **840 passed**
- `ruff check custom_components/ tests/ tools/` → All checks passed
- 三份 JSON 各 **293** 个叶子，key 树完全一致
- 实体总数：2 zone → **44**
- **这些数字在 V0.3 收尾时必须仍然自洽**（测试数会增加，其余三项是契约）。

## 6. 协作纪律

- **只改自己 write scope 内的文件。** 需要改 `const.py` / `models.py` / `base.py` /
  `coordinator.py` 的接口，发消息给 Lead，**不要直接改**。
- V0.1/V0.2 的 840 个测试**必须继续全绿**；不得为了让新测试通过而放宽老断言。
- 探针脚本**不得留在仓库**（`tools/` 只应有 `make_brand_assets.py`）。用 `%TEMP%` 或
  跑完即删，并在汇报前用 `git status` / `Get-ChildItem tools` 核对**当前**状态。
- 汇报必须贴**真实 pytest / ruff 输出**。若你说"已验证"，必须是从注册表/真实对象读出的，
  **不得用与被验证对象相同的假设重新推导**（V0.2 在 dashboard 实体 ID 上正是这样翻车的）。
- 提交前：`ruff check` 与 `ruff format --check` 必须干净。

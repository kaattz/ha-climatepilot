# ClimatePilot V0.2 — 内部接口契约（Implementers 必读）

> 本文件是 V0.2 并行开发的**唯一接口事实来源**。与 V0.1 的 `docs/INTERFACES.md` 共同生效；
> 冲突时**以本文件为准**（仅限 V0.2 新增部分）。
>
> V0.1 契约（`docs/INTERFACES.md`）继续有效，**尤其第 0 节铁律**：
> 纯引擎不得 import Home Assistant、时间必须由参数注入、`snapshot()/restore()` 往返。

## 1. V0.2 范围

设计方案第 12 节 V0.2：

| 交付项 | 状态 |
|---|---|
| 客厅有限陪跑、舒适上限和明确退出/归位 | **V0.1 已完成**（`aux_load.py`） |
| 锅炉点火次数、单次燃烧时长、短循环分析 | **V0.1 已完成**（`diagnostics_engine.py`） |
| **区域组合变化分析** | V0.2 新增 → `zone_combinations.py` |
| **过热/欠温诊断** | V0.2 新增 → `comfort_diagnostics.py` |
| **事件质量分级（区分采暖/生活热水燃烧的质量等级）** | V0.2 新增 → `diagnostics_engine.py` 扩展 |
| **每房间/锅炉调度可视化** | V0.2 新增 → 新实体 + `dashboards/` |

## 2. 已冻结的 V0.2 常量（`const.py`，已由 Lead 写好，**不要改**）

组合分析：`DEFAULT_COMBINATION_MIN_DWELL`、`DEFAULT_COMBINATION_MIN_DURATION`、
`DEFAULT_COMBINATION_WINDOW`、`DEFAULT_COMBINATION_DOMINANT_SHARE`、
`DEFAULT_CYCLING_GOOD_SECONDS`、`DEFAULT_CYCLING_POOR_SECONDS`；
`QUALITY_GOOD/FAIR/POOR/UNKNOWN`；`BURN_CAUSE_DEMAND/DHW/UNEXPLAINED/UNKNOWN`。

舒适诊断：`DEFAULT_OVERHEAT_DELTA`、`DEFAULT_OVERHEAT_DWELL`、`DEFAULT_UNDERHEAT_DELTA`、
`DEFAULT_UNDERHEAT_DWELL`、`DEFAULT_COMFORT_WINDOW`、`DEFAULT_COMFORT_SAMPLE_INTERVAL`；
`COMFORT_OK/OVERHEATING/UNDERHEATING/UNKNOWN`；
`ADVICE_LOWER_SUPPLY/RAISE_SUPPLY/CHECK_HYDRAULICS/CHECK_SCHEDULE/NONE`。

新增实体 translation_key（**必须逐字使用**，E-UI 会按此实现，E-I18N 会按此翻译）：

| 平台 | translation_key | 归属 | 说明 |
|---|---|---|---|
| `sensor` | `dominant_zone_combination` | 系统级 | 窗口内占比最高的需求组合，如 `living_room+master_bedroom`，无需求时 `none` |
| `sensor` | `zone_combination_count` | 系统级 | 窗口内出现过的不同组合数 |
| `sensor` | `combinations_24h` | 系统级（诊断） | 组合→占比的 JSON 明细（属性） |
| `sensor` | `cycling_quality` | 系统级（诊断） | 燃烧质量等级：good/fair/poor/unknown |
| `sensor` | `unexplained_burns_24h` | 系统级（诊断） | 无任何分区需求却点火的次数 |
| `sensor` | `comfort_verdict` | **每房间** | ok/overheating/underheating/unknown |
| `sensor` | `comfort_advice` | **每房间** | 建议码 |
| `binary_sensor` | `overheating` | 每房间 | 持续高于目标 |
| `binary_sensor` | `underheating` | 每房间 | 持续低于目标且确实在要热 |

## 3. 模块契约

### 3.1 `zone_combinations.py`（纯引擎，E-ENGINE 负责）

```python
@dataclass(frozen=True, slots=True)
class CombinationObservation:
    """One interval during which a fixed set of zones demanded heat."""
    zones: frozenset[str]          # 空集表示"无需求"
    started_at: datetime
    ended_at: datetime | None      # None = 仍在持续
    @property
    def duration(self) -> float: ...        # 秒；未完成为 0.0
    @property
    def key(self) -> str: ...              # 稳定的可排序标识，如 "living_room+master_bedroom"；空集为 "none"
    @property
    def zone_ids(self) -> tuple[str, ...]: ...   # 排序后的元组

@dataclass(frozen=True, slots=True)
class CombinationStat:
    key: str
    zone_ids: tuple[str, ...]
    count: int                     # 窗口内该组合出现的次数
    total_seconds: float
    share: float                   # total_seconds / 窗口内有需求总时长
    average_seconds: float
    longest_seconds: float

@dataclass(frozen=True, slots=True)
class CombinationReport:
    dominant_key: str              # 无数据时为 COMBINATION_NONE
    dominant_share: float
    distinct_count: int
    total_demand_seconds: float
    stats: tuple[CombinationStat, ...]     # 按 total_seconds 降序
    current_key: str
    current_seconds: float
    def as_dict(self) -> dict[str, Any]: ...   # 供实体属性/诊断导出

class ZoneCombinationTracker:
    def __init__(self, *, min_dwell: float = DEFAULT_COMBINATION_MIN_DWELL,
                 min_duration: float = DEFAULT_COMBINATION_MIN_DURATION,
                 window: float = DEFAULT_COMBINATION_WINDOW,
                 dominant_share: float = DEFAULT_COMBINATION_DOMINANT_SHARE) -> None: ...
    def note_demand(self, demanding: Iterable[str], now: datetime) -> None: ...
    def report(self, now: datetime) -> CombinationReport: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, data: dict[str, Any] | None) -> None: ...
```

**语义要求：**
- 组合以**排序后的房间 ID 集合**为键，顺序无关（`{a,b}` == `{b,a}`）。
- 变化必须持续满 `min_dwell` 才切换当前组合；未满则忽略该次抖动（防止 KNX 抖动刷屏）。
- 已结束且时长 `< min_duration` 的区间**不单独计数**；若其后紧邻新组合，可并入处理（实现可自行选择"丢弃短区间"或"延长前一段"，但必须在 docstring 写明并测试固化）。
- 窗口滚动淘汰：`report(now)` 只统计 `now - window` 之后的数据。
- `share` 以**有需求的总时长**为分母（不含空集），空集单独出现在 `stats` 中但 `share` 为 0.0。
- 无任何数据时：`dominant_key = "none"`（使用 `const.COMBINATION_NONE`，**已由 Lead 冻结**）。

### 3.2 `comfort_diagnostics.py`（纯引擎，E-ENGINE 负责）

```python
@dataclass(frozen=True, slots=True)
class ComfortSample:
    at: datetime
    current: float
    target: float
    demanding: bool

@dataclass(frozen=True, slots=True)
class ComfortVerdict:
    zone_id: str
    verdict: str                  # COMFORT_*
    advice: str                   # ADVICE_*
    average_deviation: float      # 有符号：正=高于目标
    max_over: float
    max_under: float
    overheated_seconds: float
    underheated_seconds: float
    samples: int
    reason_codes: tuple[str, ...]
    def as_dict(self) -> dict[str, Any]: ...

class ComfortAnalyser:
    def __init__(self, *, overheat_delta: float = DEFAULT_OVERHEAT_DELTA,
                 overheat_dwell: float = DEFAULT_OVERHEAT_DWELL,
                 underheat_delta: float = DEFAULT_UNDERHEAT_DELTA,
                 underheat_dwell: float = DEFAULT_UNDERHEAT_DWELL,
                 window: float = DEFAULT_COMFORT_WINDOW,
                 sample_interval: float = DEFAULT_COMFORT_SAMPLE_INTERVAL) -> None: ...
    def note(self, zone_id: str, *, current: float | None, target: float | None,
             demanding: bool, now: datetime) -> None: ...
    def verdict(self, zone_id: str, now: datetime) -> ComfortVerdict: ...
    def verdicts(self, now: datetime) -> dict[str, ComfortVerdict]: ...
    def drop_zone(self, zone_id: str) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, data: dict[str, Any] | None) -> None: ...
```

**语义要求（关键）：**
- `current` 或 `target` 为 `None` → **不记样本**，verdict 为 `COMFORT_UNKNOWN`，advice 为 `ADVICE_NONE`。**绝不把缺失值当 0**。
- **过热**：`current - target >= overheat_delta` 累计满 `overheat_dwell` → `COMFORT_OVERHEATING`。
  advice：`ADVICE_LOWER_SUPPLY`（若同时有其它房间在要热）否则 `ADVICE_CHECK_SCHEDULE`。
- **欠温**：`target - current >= underheat_delta` **且 `demanding=True`** 累计满 `underheat_dwell`
  → `COMFORT_UNDERHEATING`，advice `ADVICE_RAISE_SUPPLY`。
- **低于目标但不在要热**（交叉情形，V0.2 实现中裁决）：
  偏差累计满 `underheat_dwell` → verdict 仍为 `COMFORT_OK`，advice 为 `ADVICE_CHECK_HYDRAULICS`，
  `reason_codes` 含 `REASON_BELOW_TARGET_WITHOUT_DEMAND`，并在 `as_dict()` 暴露
  `below_target_no_demand_seconds`。

  **为什么不是 `underheating`**：`binary_sensor.underheating` 的契约是"持续低于目标**且确实在要热**"，
  若 verdict 判 `underheating`，两个实体会对同一房间给出互相矛盾的说法。
  **为什么不是 `unknown`**：`COMFORT_UNKNOWN` 已被绑定为"数据不可用"，复用它属于哨兵双重语义
  （与 `COMBINATION_NONE` 同类的错误，已明确禁止）。
  **为什么 `ok` 是对的**：`target` 传入的是**已仲裁的有效目标**，房间低于它却不在要热，多数情况是
  **温控器自身死区**（KNX 0.5 ℃ 内不动作），属正常；只有偏差**持续满整个 dwell** 才足以区分
  "死区"与"阀门卡死/无水流"，此时才给 `CHECK_HYDRAULICS`。即"当前不可控，但持续一小时了，去看水力"。
- 两者都不满足 → `COMFORT_OK`，advice `ADVICE_NONE`。
- 过热优先于欠温判定（同一房间不可能同时成立，但要确定性）。
- 采样节流：距上次样本不足 `sample_interval` 时跳过记录但仍更新时间语义。
  **`None` 分支不节流**（安全语义优先于采样内存上限），且**冻结最后有效样本的外推窗口**，
  避免一次短暂失联把整天的统计污染为过热。
- `verdicts(now)` 允许有 prune 副作用，但**对固定 `now` 幂等**（同参数调两次结果相等），
  调用方可安全地"调一次清理、再调一次取值"。
- 窗口滚动淘汰。

### 3.3 `diagnostics_engine.py` 扩展（E-ENGINE 负责，**只增不改语义**）

#### 3.3.0 回路上下文（V0.2 新增，**必需**）

林内 REB-A2747FF-CH 通过 `rinnai` 集成暴露**两个**燃烧状态传感器，且**使用同一套词表**：

```python
BURNING_STATES = {"30": "Standby", "31": "Heating Water", "32": "Burning", "33": "Error"}
```

- `sensor.heating_burning_state`（采暖燃烧状态）→ `CONF_BURNER_CIRCUIT = CIRCUIT_HEATING`
- `sensor.hot_water_burning_state`（热水燃烧状态）→ `CONF_BURNER_CIRCUIT = CIRCUIT_DHW`

`"Heating Water"` 是**码 31 = "正在烧水加热"**，在两个传感器上含义不同：在**采暖**传感器上它就表示
**采暖回路在燃烧**。仅凭字符串无法归属，因此必须由配置提供回路上下文。

```python
def classify_burner_state(
    state: str | None, *, circuit: str | None = None
) -> tuple[bool, str | None, bool]: ...

class BurnerStats:
    def __init__(self, *, ..., circuit: str | None = None) -> None: ...
```

| 输入 | `circuit=HEATING` | `circuit=DHW` | `circuit=None` |
|---|---|---|---|
| Standby / off 类 | 不燃烧 | 不燃烧 | 不燃烧 |
| `"Heating Water"`（码 31） | `KIND_HEATING` | `KIND_DHW` | `KIND_UNKNOWN` |
| `"Burning"`（码 32） | `KIND_HEATING` | `KIND_DHW` | `KIND_UNKNOWN` |
| **`"Error"`（码 33）** | **不燃烧** | 不燃烧 | 不燃烧 |
| 无法识别的字符串 | `KIND_HEATING` | `KIND_DHW` | `KIND_UNKNOWN` |
| `None` / unknown / unavailable | `known=False`（保持上次状态） | 同左 | 同左 |

**两条安全规则（必须有测试）：**
1. **`Error` 绝不计为燃烧。** 否则锅炉报错会被记成一次点火，污染 `starts_24h` 与直方图，
   并**伪造短循环告警** —— 而告警是用户唯一能看到的信号，误报会让人忽略真报警。
2. **`circuit=None` 时无法识别的字符串不得默认算作燃烧**，按 `known=False` 保持上次状态
   （宁可不记，不可凭空造一次点火）。只有在**已知回路**时才归入该回路。

#### 3.3.1 `snapshot()` 新增键

在 `BurnerStats.snapshot()` 的返回中**新增**这些键（不得改名/删除已有键，V0.1 的 16 个契约键保持）：

| 新键 | 类型 | 含义 |
|---|---|---|
| `cycling_quality` | `str` | `QUALITY_*`：按窗口内采暖燃烧的平均时长分级（≥`DEFAULT_CYCLING_GOOD_SECONDS` → good；≥`DEFAULT_CYCLING_POOR_SECONDS` → fair；否则 poor；无数据 → unknown） |
| `cycling_quality_reason` | `str` | 人类可读依据，如 `"average 245s over 6 heating burns"` |
| `unexplained_burns_24h` | `int` | 24h 内无分区需求时的点火次数（已存在 `burns_without_demand_24h`，**保留旧键**，新键是其别名语义的显式命名） |
| `grade_heating_24h` | `dict[str,int]` | 采暖燃烧按质量分级的计数，如 `{"good": 3, "fair": 1, "poor": 2}` |
| `grade_dhw_24h` | `dict[str,int]` | 生活热水燃烧按质量分级（**不参与** `cycling_quality` 判定） |

**关键约束（延续 V0.1）：**
- `cycling_quality` **只看采暖燃烧**（含 unknown kind）；生活热水**不得**拉低采暖质量等级。
- 短循环判定（`short_cycle_alert`）语义**不变**：只看 heating+unknown。
- `restore()` 必须容忍旧版 payload（V0.1 存的数据没有新字段）→ 用默认值兜底，**不得抛异常**。

### 3.4 `coordinator.py` 接入（Lead 负责）

新增公开属性（E-UI 依赖这些）：

```python
coordinator.combinations: CombinationReport          # 每周期刷新
coordinator.comfort: dict[str, ComfortVerdict]       # 按 zone_id
coordinator.cycling_quality: str                     # 便捷访问
coordinator.unexplained_burns: int
```

### 3.5 平台实体（E-UI 负责）

- 系统级 5 个 sensor + 每房间 2 个 sensor + 每房间 2 个 binary_sensor，translation_key 见第 2 节。
- 全部继承 `base.py` 的基类；`ClimatePilotDiagnosticEntity` 用于诊断类实体。
- 新增实体后**实体总数变化**：`tests/test_integration_setup.py` 里的 `31` 与 `tests/test_platform_entities.py` 的计数需同步更新
  （E-UI 负责；新总数 = 31 + 5 系统级 + 4/房间 × 2 房间 = 31 + 5 + 8 = **44**，请自行核对公式）。

### 3.6 可视化（E-UI 负责）

- `dashboards/climatepilot.yaml`：Lovelace 视图（sections 布局），展示
  系统状态、每房间仲裁结果与来源、需求组合、燃烧质量、舒适度判定。
- 必须用**真实实体 ID 模式**（`sensor.*_effective_setpoint` 等），并附 README 说明如何导入。
- 不要假设 dashboard url_path 已存在；提供"手动添加视图"的 YAML，而不是自动调用 API。

### 3.7 翻译（E-I18N 负责）

- 在 `strings.json` + `translations/en.json` + `translations/zh-Hans.json` 的 `entity` 段新增第 2 节的 key。
- 带 state 的实体必须补 `state` 映射：
  - `sensor.comfort_verdict`: ok/overheating/underheating/unknown
  - `sensor.comfort_advice`: 5 个 ADVICE_* 值
  - `sensor.cycling_quality`: good/fair/poor/unknown
- **三份文件 key 树必须完全一致**（现有 259 个叶子）；`tests/test_contracts.py` 与 `tests/test_translations_sync.py` 会校验。
- 中文必须是真中文，不得复制英文。

## 4. 协作纪律

- **只改自己 write scope 内的文件。** 需要改 `const.py` / `models.py` / `base.py` / `coordinator.py` 的接口，
  发消息给 Lead，**不要直接改**。
- V0.1 已冻结的 608 个测试**必须继续全绿**；不得为了让新测试通过而放宽老断言。
- 提交前：`ruff check custom_components/ tests/ tools/` 与 `ruff format` 必须干净。
- 汇报必须贴**真实 pytest / ruff 输出**，不得只说"通过"。

## 5. 实体 ID（V0.4 起：固定 `cp_` 前缀）

> **V0.2 时本节标题是「实体 ID 的 slug 规则」，写的是
> `entity_id` = `slugify(英文名)` + `slugify(设备名)`，并附了一张参考安装的具体 ID
> 对照表。V0.4 起该规则已废除，那张表必然过期 —— 照抄它会得到本版本根本不创建的 ID，
> 所以整节替换。**

现在的规则只有一条，且所有安装一致：

```
<domain>.cp_<key>             系统级
<domain>.cp_<zone_id>_<key>   房间级
```

`cp_` 前缀硬编码在 `base.py` 的 `ENTITY_ID_PREFIX`，**不跟随设备名**。

### 为什么放弃 HA 的默认命名

HA 默认 `设备名_实体名`（本身是好设计：设备名充当来源标识，避免跨集成撞车）。
但本集成因此产生了两类拼音 ID：

| 旧 ID | 拼音来自 |
|---|---|
| `sensor.wo_de_jia_heating_state` | 系统名称「我的家」 |
| `sensor.ke_ting_di_nuan_effective_setpoint` | 房间名「客厅地暖」 |

并且名字部分取自**翻译**，所以 ID 还依赖**后端语言** —— 同一实体在德语安装上会得到
不同 ID。新方案把这两个耦合都去掉了。

### 仍然成立的两条教训（V0.2 踩过的坑，保留）

1. **不要用 `slugify(中文名)` 推导 `entity_id`。**
   中文名经 HA 的 `slugify` 后是**空字符串** —— 拼音是 HA 对**设备名**做 slugify 的
   结果，不是对实体名。当年面板 45 个 ID 全错，正是把两者混为一谈。

2. **校验 `entity_id` 必须从真实 `entity_registry` 读取**，不能自己再算一遍。
   用同一个错误假设做正向推导与反向校验会「自洽地通过」，却全部不符合 HA 实际行为。
   `tests/test_integration_setup.py::test_dashboard_entity_ids_exist_in_the_registry`
   就是为此存在的**独立**检查：它从启动后的 HA 读取真实 ID，不做任何推导。

### `translation_key` 仍是稳定契约

实体 ID 由 `key` 决定，而 `key` 与 `translation_key` 一致。翻译只影响显示名，
不再影响任何 ID。

# v0.5.0 设计：燃气与度日数仪表

日期：2026-10-13
状态：已与用户确认，待实施

## 背景与定位

用户问：「我如何知道集成省燃气了？」

调查后确认了四件事，它们共同决定了这个功能的**定位是仪表，不是证据**：

1. **集成当前一次都没有写过锅炉。** `control_mode = observe`，
   `sensor.cp_supply_temperature.last_applied = null`。所以「省气了没有」目前
   没有对象 —— 它还没做任何事。
2. **燃气表是全屋共用的。** 同一块表供采暖、生活热水、灶具/烤箱。系统里**没有
   瞬时流量传感器**，只有累计表。
3. **没有瞬时流量就无法按用途分摊。** 只能靠燃烧时长估算，而锅炉是**调节燃烧**的，
   低火 10 分钟与高火 10 分钟耗气相差数倍。任何分摊值都会很吵。
4. **天气不重复，所以拿不到真正的对照组。** 唯一可信的方法是同期 A/B 交替，
   需要几十个交替周期，且必须发生在真正需要采暖的天气里。

**结论：做「看到用量」，不做「证明省了」。** 用户明确选择了这个方向。

理由不是「吃力不讨好」这种模糊说法，而是具体的：用一块测不准、且被做饭用气稀释的
尺子，去量一个本来就可能淹没在噪声里的收益，会得到一个带巨大误差棒的结论，
而这个结论会被误当作事实。

## 三个指标

| 指标 | 算法 | 为什么可信 / 不可信 |
|---|---|---|
| **日用量** (m³) | 当日零点表值 → 当前表值 | 表是 `total_increasing`，差值即真实用量 |
| **度日数** (HDD) | `Σ max(0, base − T_out) × Δt / 3600` | 纯客观，**与集成是否控制无关** |
| **m³/度日** | 本月用量 ÷ 本月度日数 | **可比的历史序列**，不是效率结论 |

### 度日数的定义

基准温度 `base` 可配置（默认 15.5 °C）。逐样本累加：

```
hdd += max(0.0, base - outdoor_temp) * dt_seconds / 3600
```

即「度·小时 → 度·日」。室外温度**高于**基准时贡献为 0（不是负数）——
否则夏天的冷天会抵消冬天的消耗。

## 组件

### 新增纯引擎 `energy_monitor.py`

不 `import homeassistant`，与 `probe_monitor.py` / `delta_t_diagnostics.py` 同级，
时间由调用方注入（`now: datetime`），因此可脱离 HA 单测。

```python
class EnergyMonitor:
    def note(self, now, outdoor_temp: float | None, gas_total: float | None) -> None
    def snapshot(self, now) -> dict[str, Any]
    def restore(self, data: dict[str, Any]) -> None
```

**状态是 O(1)，不存历史列表** —— 与 `BurnerStats` 的做法一致，避免存储无限增长：

```
day_key            当前日期 (date)
day_hdd            当日度日数累计
day_covered_s      当日已覆盖秒数（用于判断数据是否完整）
day_gas_start      当日首个可用的表值
day_samples        当日有效室外温度样本数

month_key          当前年月
month_hdd          本月度日数累计
month_gas_start    本月首个可用的表值

prev_day_*         上一个完整日的结果（用于显示「昨天」）
prev_month_*       上一个完整月的结果
last_gas_total     上一次表值（用于检测重置）
```

### 采样时机

复用现有 `periodic_audit`，**默认 900 秒（15 分钟）**（`DEFAULT_PERIODIC_AUDIT`）。
实机上该选项未设置，所以走默认值。

15 分钟粒度对度日数足够：一天 96 个样本，室外温度在 15 分钟内变化很小。
**不为这个功能新增定时器** —— 它不需要比审计更高的频率，而多一个定时器就多一处
可能出错的地方。

### 新增实体（6 个）

```
sensor.cp_gas_today               今日用气量 (m³)          total_increasing
sensor.cp_gas_yesterday           昨日用气量 (m³)          measurement
sensor.cp_gas_this_month          本月用气量 (m³)          total_increasing
sensor.cp_degree_days_today       今日度日数 (°C·d)        measurement
sensor.cp_degree_days_this_month  本月度日数 (°C·d)        measurement
sensor.cp_gas_per_degree_day      本月 m³/度日             measurement
```

全部带 `state_class`，进入 HA **长期统计（永久保留）**，不受
`recorder.purge_keep_days: 30` 影响。

**已核实 HA 允许自定义单位进入统计。** 阅读
`homeassistant/components/sensor/recorder.py`：

- `_normalize_states` 在**单位无转换器**时走 325 行分支，只要单位**稳定**
  （`_equivalent_units`，329 行）就返回 `(unit_class=None, unit, fstates)`；
- 编译循环的门槛是 `if not valid_float_states: continue`（610 行），
  **不是** `unit_class`。所以 `None` 的 unit_class 可以正常通过；
- 只有单位**不稳定**（同一个实体在不同样本上换了单位）才会返回空列表并被跳过。

因此 `°C·d` 与 `m³/°C·d` 这类自定义单位是安全的，前提是**单位字符串永不变化**。

### 新增配置项（2 个）

| 键 | 默认 | 说明 |
|---|---|---|
| `gas_meter_entity` | 空 | 燃气表实体。**留空则本功能整体关闭**（不创建实体、不采样） |
| `degree_day_base_c` | 15.5 | 度日基准温度 |

**基准温度为什么不复用天气曲线的 `outdoor_stop`（实机上是 15.0）：**
那个值用户会为了**控制效果**去调整，一改就改变了历史燃气数据的含义 ——
去年的 `m³/度日` 与今年的不再可比。两者目的不同，必须分开存储。

默认 **15.5 °C**（= 60 °F）。这个值是**约定，不是物理定律**：美国 NOAA 官方度日
以 65 °F（≈18.3 °C）为基准（见
[CPC 度日说明](https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/cdus/degree_days/ddayexp.shtml)），
欧洲文献中 15.5 °C 也常见 —— 但两处数值都只是行业惯例，没有唯一的"正确"答案。

**选哪个不影响本功能是否成立**，只影响 `m³/度日` 的绝对值，因此它是可配置的。
真正重要的是：**不要在采暖季中途改动它**，否则历史序列会断裂。

## 必须写进代码与属性的三条边界

### 1. 表是全屋的，`m³/度日` 不是采暖效率

做饭用气**与天气无关**。它在夏天占 100%，在冬天占比小但非零，因此**会稀释**
`m³/度日` 的信号。每个相关实体的 `extra_state_attributes` 必须包含
`scope: whole_house` 与 `includes_dhw_and_cooking: true`，并且 `m³/度日` 的属性里
明确写出它不是效率指标。

这与 V0.3 对 ΔT 的处理是同一个原则：`is_power_measurement = False` 存在的意义
就是阻止下游把 ΔT 当成功率。这里同理。

### 2. 室外温度缺失不能当成 0 °C

缺样本必须**跳过**，并把 `coverage`（当日有效样本秒数 ÷ 应有秒数）写进属性。
覆盖率低于阈值时，度日数与 `m³/度日` 标记为不可信。

**绝不猜 0** —— 那会把度日数凭空放大，得出「效率变差了」的错误结论。
这条与铁律 4「绝不猜温度」一致。

### 3. 燃气表可能被更换或清零

`total_increasing` 语义下数值下降即表重置。此时：

- 累加器改用新值作为起点；
- 不产出负数用量；
- 在属性里记 `meter_reset_detected: true` 与重置时间。

## 不做（明确非目标）

- ❌ **A/B 自动交替控制模式** —— 会让室温反复波动，且实验本身干扰用户真正想要的东西
- ❌ **「省了 X%」结论、显著性检验**
- ❌ **按采暖/热水/做饭分摊用量** —— 没有瞬时流量，必然不准
- ❌ **费用/金额** —— 气价会变，且需要用户维护单价，超出本版范围
- ❌ **修改 `configuration.yaml`** —— 集成不应改动用户配置文件

## 数据保留的独立发现（与本功能分开，但同样重要）

实机 `configuration.yaml` 设置了 `recorder.purge_keep_days: 30`。实测各实体的
`state_class`：

| 实体 | state_class | 后果 |
|---|---|---|
| `sensor.gas_meter` | `total_increasing` | 进长期统计，**永久保留** |
| `sensor.heweather_outside_temperature` | `None` | **30 天后删除** |
| `sensor.heating_burning_state` | `None` | **30 天后删除**（且状态是字符串） |
| `sensor.hot_water_burning_state` | `None` | **30 天后删除**（且状态是字符串） |

**含义：燃气表永久留着，但解释它所需的室外温度在消失。** 若将来想做分析，
会只剩一堆无法解释的燃气数字。

已核实**可以通过 `customize:` 修复**（`configuration.yaml` 已有 `customize:` 段）：
- `entity.py:1284` 用 `attr |= custom` 把 customize 的值写进 **state attributes**；
- `sensor/recorder.py:117` 与 `612` 正是从 **state attributes** 读取 `state_class`。

两条路径吻合。`sensor.heweather_outside_temperature` 是数字（`'22'`），可以加
`state_class: measurement`。

两个燃烧状态传感器是**字符串**（`Standby`），**不能**加 `state_class` ——
它们需要 `template` 传感器转成 0/1 才有统计价值。

**本设计不处理这一项**，它是用户侧的配置改动，另行提出。

## 测试策略

纯引擎全部用注入的 `now` 测试，无需 HA：

| 场景 | 断言 |
|---|---|
| 跨午夜翻转 | 昨日值冻结为完整值，今日从零开始 |
| 跨月翻转 | 本月值冻结，上月值保留 |
| 温度高于基准 | 度日数增量为 0，不出现负数 |
| 温度缺失 | 度日数不变，`coverage` 下降，不产生虚假增量 |
| 气表重置 | 不产出负数，标记 `meter_reset_detected` |
| 重启恢复 | `restore()` 后当日/当月进度不丢 |
| 首次运行 | 无表值时不产出垃圾数字（返回 `None` 而非 0） |
| 15 分钟粒度 | 96 个样本累加接近 24 小时的真实积分 |

外加：实体注册契约测试（数量、ID、`state_class`、单位稳定性）、
翻译同步测试（3 个语言文件）。

## 需求追踪

| 需求 | 实现位置 | 验证 |
|---|---|---|
| 日/月用量 | `energy_monitor.py` + `sensor.py` | 纯引擎单测 + 契约测试 |
| 度日数 | `energy_monitor.py` | 纯引擎单测（含缺失、高于基准） |
| m³/度日 | `energy_monitor.py` + `sensor.py` | 纯引擎单测 |
| 单位进长期统计 | `sensor.py` 的 `state_class`/`unit` | 契约测试断言单位字符串固定 |
| 全屋口径声明 | `sensor.py` 的 `extra_state_attributes` | 契约测试断言属性存在 |
| 覆盖率与不可信标记 | `energy_monitor.py` | 纯引擎单测 |
| 气表重置 | `energy_monitor.py` | 纯引擎单测 |
| 功能可关闭 | `options_flow.py` + `coordinator.py` | 契约测试：未配置时不建实体 |

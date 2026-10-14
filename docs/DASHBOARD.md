# ClimatePilot 面板（`dashboards/climatepilot.yaml`）

V0.2 的"每房间 / 锅炉调度可视化"。这个文件夹里只有**一份可粘贴的 YAML**，集成
**不会**自动创建、修改或注册任何 dashboard —— 面板属于用户的 UI 配置，集成去写它
既会覆盖用户自己的布局，也会在删除集成后留下一堆指向不存在实体的卡片。

- 文件：[dashboards/climatepilot.yaml](../dashboards/climatepilot.yaml)
- 布局：`type: sections`，4 列，四组卡片：系统总览 / 需求组合 / 燃烧质量 / 每房间。
- 只读为主：唯一的可写控件是控制模式、房间模式、以及两个按钮（重新评估、恢复自动）。

---

## 1. 实体 ID 是固定契约（V0.4 起）

**你不需要推算任何 ID。** 它们在所有安装上完全一致：

```
<domain>.cp_<key>             系统级
<domain>.cp_<zone_id>_<key>   房间级
```

例如 `sensor.cp_heating_state`、`sensor.cp_living_room_effective_setpoint`。

`cp_` 前缀**硬编码在代码里**（`custom_components/climatepilot/base.py` 的
`ENTITY_ID_PREFIX`），**有意不跟随设备名**。

### 为什么不用 HA 的默认命名

Home Assistant 的默认规则是 `设备名_实体名`（这本身是好设计：设备名充当来源标识）。
但本集成早先的版本因此产生了两类拼音 ID：

| 旧 ID | 拼音来源 |
|---|---|
| `sensor.wo_de_jia_heating_state` | 系统名称"我的家" |
| `sensor.ke_ting_di_nuan_effective_setpoint` | 房间名"客厅地暖" |

而且名字部分取自**翻译**，所以 ID 还依赖**后端语言** —— 同一个实体在德语安装上会
得到不同的 ID。新方案两个耦合都没有了。

### 只有两个实体是"你自己的"

| 实体 | 说明 |
|---|---|
| `climate.livingroom_floorheating` | 客厅 KNX 温控器 |
| `climate.master_bedroom_floorheating` | 主卧 KNX 温控器 |

这两个由**你的 KNX 集成**创建，不在这 52 个之内，按你的实际实体名替换。

### 房间多于两个

复制"Room 2"那一整块，把 `master_bedroom` 替换成新房间的 `zone_id`（配置流程里用的
那个），以及对应的 `climate.*` 实体名。**ID 与房间的显示名无关**，所以中文房间名
不会影响任何 ID。

### 从旧版本升级

本版本首次启动时会**自动把已有实体重命名一次**（`wo_de_jia_*` 与
`ke_ting_di_nuan_*` → `cp_*`），保留历史与 unique_id，见
`custom_components/climatepilot/migration.py`。之后无需任何手工操作。

### 怎么确认自己系统上的真实 ID

设置 → 开发者工具 → 状态，筛选 `cp_` 即可；或搜索实体名。

---

## 2. 导入方法（手动粘贴，不调用任何 API）

### 方式 A：新建一个独立面板

1. **设置 → 面板 → + 添加面板 → 新建面板（从空白开始）**，标题随意，URL 建议 `climatepilot`；
2. 打开它 → 右上角铅笔（编辑）→ 右上角 ⋮ → **原始配置编辑器**；
3. 把 [dashboards/climatepilot.yaml](../dashboards/climatepilot.yaml) **整个文件**粘进去（它自带
   `title:` 与 `views:`）→ **保存**。

### 方式 B：加进现有面板

1. 打开目标面板 → 编辑 → ⋮ → **原始配置编辑器**；
2. 只复制文件里的 `views:` 列表项（从 `  - title: 采暖总览` 开始到文件末尾），追加到现有
   `views:` 数组里（注意缩进对齐，这是 YAML 列表，不是键）；
3. 保存。视图路径 `climatepilot` 若与现有视图冲突，改掉 `path:`。

### 方式 C：YAML 模式面板（`configuration.yaml`）

如果你用 YAML 模式面板，把文件内容放进对应文件，并在 `configuration.yaml` 注册：

```yaml
lovelace:
  mode: storage
  dashboards:
    climatepilot:
      mode: yaml
      title: 采暖总览
      icon: mdi:radiator
      show_in_sidebar: true
      filename: dashboards/climatepilot.yaml
```

---

## 3. 面板上每一块在看什么

### 3.1 系统总览

| 卡片 | 实体 | 怎么读 |
|---|---|---|
| 采暖状态 | `sensor.*_heating_state` | FSM：观察 / 无需求 / 准备 / 采暖中 / 已满足 / 受限。属性里有 `decision`、`target_supply`、`reason_codes` |
| 控制模式 | `select.*_control_mode` | **观察**只计算不写入，首次配置建议停在这里；**协调**才写房间目标与锅炉；**暂停**停止一切写入但保留状态 |
| 需求分区数 | `sensor.*_active_zones` | 真正在要热的分区数。"heat 模式"不等于要热，只有反馈 `hvac_action: heating` 才算 |
| 建议供水温度 | `sensor.*_suggested_supply_temp` | 天气补偿结果。属性 `should_apply` 说明它是否真被写进锅炉（观察模式下恒为 false） |
| 实际供水温度 | `sensor.*_supply_temperature` | 锅炉回读值；与建议值长期偏差大 → 看第 6.2 节故障处理 |
| 室外温度 | `sensor.*_outdoor_temperature` | 补偿曲线的输入。属性 `quality` 非 `valid` 时代表读数过期，补偿会退到安全值 |

### 3.1b 燃气与度日数（V0.5，可选）

**只有配置了燃气表才存在这 6 个实体。** 没配置时整组卡片是空的，删掉即可。

| 卡片 | 实体 | 怎么读 |
|---|---|---|
| 今日 / 昨日 / 本月用气量 | `sensor.cp_gas_today` / `_yesterday` / `_this_month` | 差值取自累计表。**这块表通常是全屋的** —— 含生活热水与做饭，不是采暖专用 |
| 今日 / 本月度日数 | `sensor.cp_degree_days_today` / `_this_month` | 这段时期有多冷。属性 `base_c` 是基准温度，`coverage` 是当日有效采样比例 |
| 每度日用气量 | `sensor.cp_gas_per_degree_day` | **用于纵向比较的序列，不是效率**。属性 `is_efficiency_measurement: false` |

**怎么读这组数字（重要）：**

- **不要把它当"省气了没有"的证明。** 它包含做饭与热水用气，而这两者与天气无关，
  会**稀释**信号；你的 `m³/度日` 因此不会随集成是否控制而干净地变化。
- **看趋势，不看单日。** 同一个月内波动很正常（做饭、洗澡都会推高）；跨月比较才有意义，
  而跨月比较的前提是基准温度没变过。
- **`coverage` 低时不要采信当天的度日数。** 那代表室外温度有采样缺口，读数只是
  较短时段的真实测量，不是一整天的。
- **`unknown` 不是 0。** 显示 `unknown` 表示没有可用读数；`0 m³` 才是"今天没烧气"。

> **想长期保留室外温度：** 这 6 个实体自带 `state_class`，永久保留；但你的
> `sensor.heweather_outside_temperature` 默认 30 天后被清除。若要追溯分析，需要给它
> 单独加 `state_class: measurement`（通过 `customize:`）。集成不会改你的配置文件。

### 3.2 需求组合

判断"这套房子到底是哪几个房间在成组地要热"。

| 卡片 / 属性 | 含义 |
|---|---|
| 主导组合 (state) | 窗口内占采暖时长最多的组合键，如 `living_room+master_bedroom`；`none` 表示**整窗无任何需求**（测量结果，不是缺数据） |
| `dominant_share` | 主导组合占"有需求总时长"的比例（分母不含无需求时间） |
| `has_dominant_mode` | 占比是否达到阈值。**false 表示需求时间散在好几个组合里**，"主导组合"只是领先者，别当成稳定模式 |
| 组合种类数 (state) | 窗口内出现过的**不同需求组合数**，不含空集 |
| `total_groups` | 含空集的分组数，比上面的 state 多 1 的情形很常见 |
| 有需求总时长 (state) | 窗口内累计有需求秒数；属性 `total_demand_minutes` 是同一数字的分钟版 |
| 组合明细 | `sensor.*_combinations_24h` 的属性里带完整 `stats`：每个组合的次数、总时长、占比、平均与最长 |

怎么用：冬天如果主导组合长期是"客厅+主卧"而其它房间从不出现，说明其它房间要么没需求、
要么阀门/执行器没开（去看那些房间的 `binary_sensor.*_underheating`）；如果
`has_dominant_mode` 一直是 false，说明系统在多个模式间来回切，值得回看
`binary_sensor.*_short_cycle_alert`。

### 3.3 燃烧质量

| 卡片 / 属性 | 含义 |
|---|---|
| 采暖燃烧质量 (state) | `good` / `fair` / `poor` / `unknown`，按窗口内**采暖燃烧的平均时长**分级。生活热水**不参与**判定 |
| `reason` | 判定依据原文，例如 `average 245s over 6 heating burns` |
| `grade_heating_24h` / `grade_dhw_24h` | 采暖与生活热水各自的按质量计数。两者分开，就是为了让一次洗澡不会把采暖判成"差" |
| 无需求点火 (24h) | 无任何分区需求时的点火次数。**读数不可信时实体不可用** —— "看不到"和"0 次"必须区分开 |
| 短循环告警 | 只看采暖 + 未知类型燃烧；燃烧状态丢失时实体不可用，而不是显示"正常" |
| 受限运行 | 系统是否处于受限状态，属性 `reasons` 会点名具体原因（热源受限 / 分区状态未知 / 天气数据不可用） |

### 3.4 探头与供回水温差（V0.3，可选设备）

这一组卡片**需要外贴探头**才有数据。没装探头时它们全部显示 `unknown`，这是正常状态，不是故障
（详见第 4 节）。

**先记住一件事：温差不是热功率，也不是效率。** ΔT 只是**流动与冷凝工况的间接指标**。
面板上每个相关实体的属性里都有 `is_power_measurement: false`，`reason_codes` 里也有
`temperature_difference_is_not_heat_power`——这是刻意留的"别误读"标记。

| 卡片 / 属性 | 含义 |
|---|---|
| 回水温度 (state) | 外贴回水探头读数。`unknown` = 没有可用读数（没装 / 过期 / 卡死 / 不可信），**不是 0℃** |
| `supply` / `return` | 供回两侧各自的 `quality` 与 `reason_codes`，用来判断是哪一侧的问题 |
| `divergence` | 供回探头互相比对的偏差。超过阈值说明**两个探头至少有一个错了**，此时两侧都被判为不可信（无法判断哪个错） |
| 供回水温差 ΔT (state) | 供水 − 回水。只有两个探头都可用时才有值，`unknown` 表示**测不出来**，不是"温差为 0" |
| `average` / `minimum` / `maximum` | 窗口内的统计值。看趋势用 `trend_per_hour`（正 = 温差在扩大） |
| `stable_seconds` | 已稳定时长。低于稳定门槛时不下水力判定——刚开机/刚切换时的 ΔT 没有意义 |
| 探头质量 (state) | 取供回**较差**的一侧：`valid` / `stale` / `implausible` / `frozen` / `probe_unavailable` |
| 冷凝工况 (state) | `likely` / `unlikely` / `unknown`，由**燃烧中**的回水温度推断，是**间接**指标 |
| 水力判定 (state) | `ok` / `low_flow` / `high_flow` / `unbalanced` / `unknown`。**低 ΔT 通常意味着流量过大或旁通短路，高 ΔT 意味着流量不足**——两者都是水力信号，不是"效率好坏" |
| 曲线校准建议 (state) | `none` / `raise_supply_curve` / `lower_supply_curve` / `insufficient_data`。**纯建议**：`advisory_only: true`，集成**绝不自动改**天气曲线 |
| 探头故障 | 只看**已配置**的探头是否不可用。**没装探头时恒为 off**——若默认报故障，用户会被训练成忽略这个标志 |

`85` 这个数字要特别小心：DS18B20 未完成转换时会输出 **85℃ 哨兵值**。它看起来像"很热的供水管"，
天真的实现会报出不存在的锅炉故障、或算出一个"巨大 ΔT"看起来像水力成功。集成把它判为
`implausible` 并让读数变成 `unknown`，所以看到 `unknown` 不要以为是探头坏了。

**探头坏了会怎样**：什么都不影响。探头只作监测/优化输入，**绝不承担锅炉安全停机功能**，也绝不
参与任何写入决策（`probe_fault` 实体的 `affects_heating: false` 就是这个意思）。外置探头失联时
采暖照常运行，只是这一组卡片没有数据。

### 3.5 每个房间

按"先仲裁、后舒适"的顺序排：

| 卡片 | 怎么看 |
|---|---|
| 有效目标 | 仲裁后的唯一目标值。属性 `base` / `source` / `adjustment_delta` / `clamped` / `reason_codes` / `explanation` 解释它从哪来 |
| 目标来源 | 手动覆盖 / 固定模式 / 日程 / 寒潮修正 / 辅助负载 / 缺省 |
| 房间温度 | KNX 回读的房间实测温度 |
| 正在要热 | 该房间**真的**在要热（仅"heat 模式"为 false）。反馈缺失时是 unknown，不是 off |
| 舒适判定 | `ok` / `overheating` / `underheating` / `unknown`。**没采样过就是 `unknown`，不是 `ok`** —— "没测"和"正常"是两件事 |
| 处置建议 | `lower_supply_temperature` / `raise_supply_temperature` / `check_hydraulics_or_balancing` / `check_schedule_or_override` / `none`，含义与处置见 [DEPLOYMENT 7.4](DEPLOYMENT.md) |
| 持续过热 | 高于目标且**持续够久**（默认 30 分钟）才 on。属性 `dwell_seconds` 是当前生效的门槛，`max_over` / `overheated_seconds` 是依据 |
| 持续欠温 | 低于目标、**且确实在要热**、且持续够久（默认 60 分钟）。要热却上不去 = 水力/容量问题，不是设定值问题 |
| 手动覆盖中 / 覆盖剩余 | 用户意图优先；`覆盖剩余` 对 `next_schedule` / `indefinite` 策略显示 unknown（它们本来就没有到期时间） |
| 房间模式 / 可作辅助负载 / 恢复自动 | 房间级可写控件 |

> 两个舒适 flag 都**直接读引擎判定**，而不是现场比较温度：单次偏高的读数既可能是漏水阀门
> 也可能是下午的太阳，只有引擎累积的持续时间能把两者分开。所以它们和
> `sensor.*_comfort_verdict` **永远一致**，且两者不可能同时 on。

---

## 4. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 卡片显示"实体不可用" | ID 不存在（改名了 / 换语言了 / 房间删了） | 按第 1 节查真实 ID 并替换 |
| 舒适判定一直是 `unknown` | 分析器还没采到样本，或该房间温度/目标缺失 | 等一个采样间隔（默认 5 分钟）；长期 unknown 说明 KNX 回读没上来 |
| 燃烧质量一直 `unknown` | 没有燃烧状态传感器，或窗口内没有采暖燃烧 | 配置第 2 步加燃烧状态传感器；只有洗澡烧水时它就是 unknown（生活热水不参与判定） |
| 无需求点火 / 短循环告警不可用 | 燃烧读数质量不是 `valid` | 先修燃烧状态传感器，这是刻意的 fail-closed |
| 主导组合一直是 `none` | 窗口内没有任何分区要热 | 若实际在采暖，去查各房间 `heat_request` 是否 unknown（反馈缺失） |
| 整块卡片空白 | YAML 缩进错位（sections 下必须是列表） | 用原始配置编辑器保存时的报错行号定位 |

---

## 5. 维护约定

- **不要**把这份 YAML 直接拷进 `.storage/lovelace*`（那是 HA 的内部存储，手改会被覆盖）；
  请用原始配置编辑器粘贴。
- 改集成里的实体名（`strings.json`）会让**所有**实体 ID 变化（除非你已手动重命名 ID），
  面板需要同步更新 —— 这也是建议先重命名成稳定 ID 的原因。
- 本文件对应的实体清单由 `tests/test_platform_entities.py` 与
  `tests/test_integration_setup.py` 钉住：translation_key 一旦改名，测试先红，再回来改面板。

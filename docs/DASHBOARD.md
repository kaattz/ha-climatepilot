# ClimatePilot 面板（`dashboards/climatepilot.yaml`）

V0.2 的"每房间 / 锅炉调度可视化"。这个文件夹里只有**一份可粘贴的 YAML**，集成
**不会**自动创建、修改或注册任何 dashboard —— 面板属于用户的 UI 配置，集成去写它
既会覆盖用户自己的布局，也会在删除集成后留下一堆指向不存在实体的卡片。

- 文件：[dashboards/climatepilot.yaml](../dashboards/climatepilot.yaml)
- 布局：`type: sections`，4 列，四组卡片：系统总览 / 需求组合 / 燃烧质量 / 每房间。
- 只读为主：唯一的可写控件是控制模式、房间模式、以及两个按钮（重新评估、恢复自动）。

---

## 1. 先读这段：实体 ID 不是固定契约

**实体 ID 由「设备名的 slug + 英文实体名的 slug」决定 —— 即使界面是中文，实体 ID 也是英文。**

这是 Home Assistant 的刻意设计，不是遗漏。只有界面语言属于
`homeassistant.generated.languages.NATIVE_ENTITY_IDS`（41 种，全部是拉丁/欧洲语言：
`de`/`fr`/`es`/`it`/`nl`/`pl`/`sv`…）时，HA 才用**本地化**名字生成 `object_id`；
其余语言一律回退到 `en`：

```python
# homeassistant/helpers/entity_platform.py:162-186（HA 2026.2）
object_id_language = (
    hass.config.language
    if hass.config.language in languages.NATIVE_ENTITY_IDS
    else languages.DEFAULT_LANGUAGE          # "en"
)
```

**`zh-Hans` 不在该集合里**（该集合中没有任何中文变体）。所以中文用户的实际情况是：

```
显示名 name   = 采暖状态                        <- 中文翻译生效，界面上看到的是中文
entity_id     = sensor.wo_de_jia_heating_state  <- 英文名生成，ID 保持 ASCII
```

好处是实体 ID 不会随界面语言漂移；代价是**中文 ID 的直觉是错的** —— 你不能拿中文名去
slugify，`slugify("客厅地暖 采暖请求")` 会得到 `ke_ting_di_nuan_cai_nuan_qing_qiu`，
而真实 ID 是 `binary_sensor.ke_ting_di_nuan_heat_request`。

### 参考安装的实际 ID 对照表

本仓库测试用的参考安装：系统名"我的家"，房间"客厅地暖" / "主卧地暖"，界面语言 `zh-Hans`。
下表是**从真实 HA 实例的 `entity_registry` 读出来的**（不是推算的），共 38 个 translation_key：

| translation_key（固定，不会变） | 实际实体 ID（英文 slug） |
|---|---|
| `sensor.heating_state` | `sensor.wo_de_jia_heating_state` |
| `sensor.active_zones` | `sensor.wo_de_jia_zones_demanding_heat` |
| `sensor.suggested_supply_temp` | `sensor.wo_de_jia_suggested_supply_temperature` |
| `sensor.outdoor_temperature` | `sensor.wo_de_jia_outdoor_temperature` |
| `sensor.supply_temperature` | `sensor.wo_de_jia_supply_temperature` |
| `sensor.burner_starts_24h` | `sensor.wo_de_jia_burner_starts_24_h` |
| `sensor.last_burn_duration` | `sensor.wo_de_jia_last_burn_duration` |
| `sensor.dominant_zone_combination` | `sensor.wo_de_jia_dominant_zone_combination` |
| `sensor.zone_combination_count` | `sensor.wo_de_jia_distinct_zone_combinations_24_h` |
| `sensor.combinations_24h` | `sensor.wo_de_jia_zone_combination_breakdown_24_h` |
| `sensor.cycling_quality` | `sensor.wo_de_jia_combustion_quality` |
| `sensor.unexplained_burns_24h` | `sensor.wo_de_jia_unexplained_burner_starts_24_h` |
| `binary_sensor.short_cycle_alert` | `binary_sensor.wo_de_jia_short_cycle_alert` |
| `binary_sensor.degraded` | `binary_sensor.wo_de_jia_degraded_operation` |
| `select.control_mode` | `select.wo_de_jia_control_mode` |
| `button.reassess` | `button.wo_de_jia_reassess_now` |
| `number.supply_temperature_override` | `number.wo_de_jia_manual_supply_temperature` |
| `climate.system_thermostat` | `climate.wo_de_jia_whole_home_heating` |
| `sensor.effective_setpoint`（每房间） | `sensor.<room>_effective_setpoint` |
| `sensor.setpoint_source`（每房间） | `sensor.<room>_setpoint_source` |
| `sensor.zone_temperature`（每房间） | `sensor.<room>_room_temperature` |
| `sensor.override_remaining`（每房间） | `sensor.<room>_manual_override_remaining` |
| `sensor.comfort_verdict`（每房间） | `sensor.<room>_comfort_verdict` |
| `sensor.comfort_advice`（每房间） | `sensor.<room>_comfort_advice` |
| `binary_sensor.manual_override`（每房间） | `binary_sensor.<room>_manual_override_active` |
| `binary_sensor.heat_request`（每房间） | `binary_sensor.<room>_heat_request` |
| `binary_sensor.overheating`（每房间） | `binary_sensor.<room>_overheating` |
| `binary_sensor.underheating`（每房间） | `binary_sensor.<room>_underheating` |
| `select.zone_mode`（每房间） | `select.<room>_room_mode` |
| `button.resume_auto`（每房间） | `button.<room>_resume_automatic` |
| `switch.auxiliary_load`（每房间） | `switch.<room>_auxiliary_heat_load` |

V0.3 新增 7 个（全部系统级，见 INTERFACES_V0.3.md 第 3 节）。这 7 个的
**英文名恰好与 translation_key 逐字相同**（已冻结，E-I18N 不得改动），所以 ID 无需查表：

| translation_key（固定） | 英文名 | 实际实体 ID |
|---|---|---|
| `sensor.return_temperature` | Return temperature | `sensor.wo_de_jia_return_temperature` |
| `sensor.delta_t` | Delta T | `sensor.wo_de_jia_delta_t` |
| `sensor.delta_t_quality` | Delta T quality | `sensor.wo_de_jia_delta_t_quality` |
| `sensor.condensing_regime` | Condensing regime | `sensor.wo_de_jia_condensing_regime` |
| `sensor.hydraulic_verdict` | Hydraulic verdict | `sensor.wo_de_jia_hydraulic_verdict` |
| `sensor.curve_calibration_advice` | Curve calibration advice | `sensor.wo_de_jia_curve_calibration_advice` |
| `binary_sensor.probe_fault` | Probe fault | `binary_sensor.wo_de_jia_probe_fault` |

`<room>` 是**房间名**的 slug：客厅地暖 → `ke_ting_di_nuan`，主卧地暖 → `zhu_wo_di_nuan`，
英文房间名则直接用（`Living room` → `living_room`）。

### 有几处英文名与 translation_key 不同，最容易写错

| translation_key | 英文名 | 所以 ID 里是 |
|---|---|---|
| `cycling_quality` | Combustion quality | `combustion_quality`（**不是** `cycling_quality`） |
| `zone_combination_count` | Distinct zone combinations (24 h) | `distinct_zone_combinations_24_h` |
| `combinations_24h` | Zone combination breakdown (24 h) | `zone_combination_breakdown_24_h` |
| `unexplained_burns_24h` | Unexplained burner starts (24 h) | `unexplained_burner_starts_24_h` |
| `active_zones` | Zones demanding heat | `zones_demanding_heat` |
| `zone_temperature` | Room temperature | `room_temperature` |
| `zone_mode` | Room mode | `room_mode` |
| `resume_auto` | Resume automatic | `resume_automatic` |
| `manual_override` | Manual override active | `manual_override_active` |
| `burner_starts_24h` | Burner starts (24 h) | `burner_starts_24_h`（`24_h`，不是 `24h`） |

`24_h` 的来历：显示名里的 `(24 h)` 经 slugify 后，括号被丢弃、`24` 与 `h` 之间插入下划线。

### 规则总结（用来推算自己的 ID）

1. 取该实体所属**设备的显示名**的 slug：系统级用系统名（配置里"系统名称"，默认
   `ClimatePilot`），房间级用房间名；
2. 取该实体的**英文名**（来自 `translations/en.json`，不是中文名）的 slug；
3. 拼成 `<domain>.<设备名slug>_<英文名slug>`。

**唯一可靠的验证办法是从真实实例读**，不要自己算 slug —— 正反两边用同一个错误假设，
会得出"自洽但错误"的结论（本项目就踩过这个坑）。见下面的方法。

### 怎么拿到自己的真实 ID

任选其一：

1. **开发者工具 → 状态**，搜索框输入 `climatepilot`（或你的系统名），逐条复制 `entity_id`；
2. **设置 → 设备与服务 → ClimatePilot → 设备**，点进系统设备 / 某个房间子设备，
   实体列表里的 ID 即真实值；
3. 命令行（按 `unique_id` 前缀过滤，最干净 —— 不依赖实体名长什么样）：

   ```bash
   curl -s -H "Authorization: Bearer $HA_TOKEN" \
        http://homeassistant.local:8123/api/config/entity_registry/list \
     | python -c "
   import json,sys
   for e in json.load(sys.stdin):
       if e['platform']=='climatepilot':
           print(e['entity_id'], '|', e['unique_id'])
   "
   ```

4. **本仓库内**（改面板后自查）：`tests/test_platform_entities.py` 里有一条断言把
   `dashboards/climatepilot.yaml` 的每个引用与 `strings.json` 的 translation_key 集合做双向校验，
   改了 key 或改了面板都会立刻变红。

最省事的办法：在 HA 里**重命名实体 ID**（设置 → 实体 → 齿轮 → 实体 ID），把它们改成
你自己的统一前缀（例如 `sensor.hvac_heating_state`）。重命名是持久化的，此后
**设备改名、界面语言变化都不会再动 ID**，slug 规则彻底退出 —— 强烈建议这么做，
因为你一旦改了系统名或房间名，下面所有 ID 都会跟着变。

### 本文件里哪些 ID 需要改

`dashboards/climatepilot.yaml` 用的是参考安装的两个房间：`客厅地暖`（`ke_ting_di_nuan_*`）
与 `主卧地暖`（`zhu_wo_di_nuan_*`），两者都来自**真实实例的 entity_registry**。

- 所有 `wo_de_jia_*`（系统级）取决于你的**系统名称**：如果你没把它设成"我的家"，
  这一整批都要换成你系统名的 slug；
- `ke_ting_di_nuan_*` / `zhu_wo_di_nuan_*`（房间级）取决于你的**房间名**，同上；
- `climate.livingroom_floorheating` / `climate.master_bedroom_floorheating` 是那两个房间的
  KNX 温控器实体 —— 这是**你自己的既有实体**，不在集成生成的 44 个之内，按实际替换；
- 房间多于两个时，把"Room 2"那一整块复制一份，替换房间 slug 与 `climate.*` 即可。

> **最快的做法**：把面板粘进去之前，先全局替换 `wo_de_jia` → 你系统名的 slug、
> `ke_ting_di_nuan` / `zhu_wo_di_nuan` → 你两个房间名的 slug。三处替换就能覆盖全部 43 个引用。

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

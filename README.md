<div align="center">

<img src="custom_components/climatepilot/brand/icon@2x.png" alt="ClimatePilot" width="128">

# ClimatePilot · 智暖领航

**多区域采暖协调与优化 · Multi-zone heating coordination for Home Assistant**

by [HomeAxis](https://github.com/kaattz)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2025.1%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![Version](https://img.shields.io/badge/version-0.4.0-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> **v0.4.0 — 已在一台实机上以「观察模式」运行，但观察期未满、未经完整采暖季验证。**
> 全部阈值与曲线仍为设计建议值。**v0.4.0 改变了全部实体 ID**（见 CHANGELOG）。
> 外贴探头属于**监测/优化输入，不承担锅炉安全停机功能**。
> 见 [CHANGELOG.md](CHANGELOG.md)。

</div>

---

## 集成图标

集成自带品牌资源（`custom_components/climatepilot/brand/`），Home Assistant
**优先使用本地图片**，无需向官方 brands 仓库提交。

| 文件 | 用途 |
|---|---|
| `icon.png` / `icon@2x.png` | 列表与设备页的方形图标（@2x 用于高分屏） |
| `logo.png` / `logo@2x.png` | 集成配置页的矩形图标 |
| `dark_*.png` | **深色主题**版本（4 个） |

**为什么必须同时提供深色版本**：HA 在深色主题下请求 `dark_icon.png`，
若缺失会按 `IMAGE_FALLBACKS` 回退到**浅色版**——不是"没有图片"，而是把
**白底图标显示在深色界面上**，在深色主题下是一块刺眼的白方块。
`tests/test_brand_assets.py` 会校验 8 个文件齐全、深色版确实与浅色版不同、
且在 24px 列表尺寸下仍可辨认。

图标由 `tools/make_brand_assets.py` 用几何图元生成（可复现、可审查，
不是不可读的二进制）。改动后请重新运行该脚本；测试会校验签入的资源
与生成器输出一致。

---

## 这是什么

ClimatePilot 是运行在 Home Assistant 内的**多区域采暖协调层**。它让每间房保留自己的 KNX 本地闭环，同时在**上层**统一仲裁温度目标、聚合真实热需求、调度锅炉采暖请求，并做天气补偿与运行诊断。

它解决的核心问题是：**当日程、天气补偿、陪跑逻辑和手动操作同时想改同一间房的温度时，谁说了算。**

```
KNX 面板 / HA 手动操作 / 自动日程 / 外部条件
                       │
                       ▼
             [房间温度仲裁器]        ← 唯一有效目标 effective_setpoint
           基础目标 / 修正 / 手动覆盖
                       │
                       ▼
           [KNX climate 实体适配器]   ← 只读写 climate 实体，不直接驱动阀门
                       │
             KNX 本地调节执行器
                       │
                       ▼
               [热需求聚合器]         ← hvac_action 而非 heat 模式
                       │
                       ▼
              [采暖协调状态机]        ← 需求消抖 / 阀门预开 / 后循环
                       │
                       ▼
                 [锅炉适配器]         ← 采暖请求 / 目标供水温度
                       │
                       ▼
         林内或其他锅炉原生燃烧/保护逻辑
```

## 设计原则

| 原则 | 含义 |
|---|---|
| **KNX 本地闭环不被取代** | 房间温控器继续驱动本地执行器；集成只读写 KNX 已暴露的 `climate` 实体 |
| **用户意图优先** | 面板或 HA 的手动设定不会被定期巡检覆盖 |
| **一个房间一个有效目标** | 内部唯一仲裁结果，避免多渠道并发写同一实体 |
| **监测与控制解耦** | 缺少回水探头也能工作；传感器失效不触发激进控制 |
| **安全保护属于硬件** | 软件不能替代锅炉最小流量、旁通/水力解耦、过热保护与水泵后循环 |
| **默认观察模式** | 切入主动控制前必须完成设备语义验证与水力验收 |

## 核心能力

### 1. 温度仲裁器（P0–P4 优先级）

| 优先级 | 来源 | 行为 |
|---|---|---|
| P0 | 硬件安全 / 配置范围 | 限制发送范围（用户配置 ∩ 设备范围） |
| P1 | KNX 面板或 HA 手动覆盖 | 冻结该房间的自动目标 |
| P2 | 用户选择的固定模式 | 舒适 / 节能 / 外出 |
| P3 | 按房间自动日程 | 时段基础目标 |
| P4 | 自动优化策略 | 只作用于 P2/P3 基础目标 |

手动覆盖有三种恢复策略：**下个日程节点**（默认）、**固定时长**（持久化到期时刻，重启后重算剩余）、**一直保持**。手动覆盖生效期间，寒潮修正与陪跑**一律不生效**。

### 2. 写入回声识别

KNX 面板没有 HA 用户身份，因此不能用 `context.user_id` 判断手动操作。ClimatePilot 用**待确认命令事务**：集成发出写入时记录目标值、请求 ID 与确认窗口，回读值在窗口内匹配即判定为自写回声；确认窗口过期后，同值不再无限期视为自写，而是**保护可能的手动操作**（宁可把用户操作当手动，也不覆盖它）。

### 3. 真实热需求聚合

`heat` 模式**不等于**要热。集成依据 `hvac_action=heating`（或用户指定的请求对象）判定；`unknown`/`unavailable` 进入不确定状态，**绝不当作"无需求"**。

### 4. 有界辅助热负载

替代"当前室温 +2℃ 无限追涨"：以**房间日程目标**为基准加一个小的、有上限的偏移，且有退出回差、夜间禁止、最短切换间隔，并在其他房间需求结束时**明确归位**。它不是锅炉最小流量的安全保证。

### 5. 天气补偿

两点线性曲线 + 限幅 + 防抖 + 最小下发间隔。数据无效或过期时**保持最后一次已确认的安全值并告警**，绝不按 0℃ 猜测。支持"仅建议值"模式（不写锅炉，只输出计算结果）。

### 6. 燃烧诊断

基于锅炉燃烧状态统计最近燃烧时长、1h/24h 点火次数、周期直方图与短循环告警。**区分采暖燃烧与生活热水燃烧**，避免把洗澡点火误判为地暖短循环。短循环默认**仅告警**，不修改锅炉内部安全流程。

V0.2 在此之上增加**燃烧质量分级**（`cycling_quality`，只看采暖燃烧的平均时长）与**无需求点火计数**（`unexplained_burns_24h`），用于区分"地暖在短循环"与"锅炉因生活热水/自身逻辑点火"。

### 7. 探头诊断（V0.3）

外贴 DS18B20 供回水探头 + ΔT 趋势、冷凝工况与水力判定。**这一层全是只读诊断**：

- **85℃ 哨兵**：DS18B20 转换失败或总线断开时报出的**恰好是 85℃**。集成把它判为
  `implausible`（数值不可信），**不是**"一根很热的管子"——否则会凭空报出不存在的
  锅炉故障，或算出一个看起来很成功、其实毫无意义的"巨大 ΔT"。看到 `probe_fault`
  请**先查接线**，不要以为锅炉坏了。
- **ΔT 不是热功率**。供回水温差**不能**用来推算效率或热输出，它只是流动与冷凝工况的
  **间接**指标。所有输出都带 `is_power_measurement = False` 标记。
- **探头失联不影响核心采暖控制**：探头故障只降低诊断可信度，不改变房间目标、
  不改变锅炉启停、不改变供水温度。
- **探头未配置 ≠ 实体故障**：未装探头时读数显示 `unknown` 但实体仍可用，
  `probe_fault` 保持 off。

安装要求与逐项处置建议见 [DEPLOYMENT 7.5](docs/DEPLOYMENT.md)。

### 8. 采暖季开关（V0.4）

`switch.cp_heating_season` —— 非采暖季把集成关掉，但不必卸载：

| 开关 | 实际状态 | 行为 |
|---|---|---|
| **OFF**（停季） | `paused` | 撤销自己发出的供暖请求，**阻断全部写入** |
| **ON**（开季） | `observe` | 恢复监测与建议，**仍不写入** |

- **ON 只回到观察模式，不会直接开始控制。** 按一下季节开关就开始写锅炉是个不该有的
  行为；真正接管仍需你在控制模式里显式选择 `coordinate`。
- **不碰锅炉自身的开关状态** —— 只收回"我们自己发出的"请求（集成只关闭自己打开的东西）。
- 状态派生自控制模式，不独立存储，所以不会出现"季节说开、模式说暂停"的矛盾。
- 控制模式持久化在配置项里，**夏天重启不会自己开始采暖**。

## 安装

### HACS（推荐）

1. HACS → 集成 → 右上角 ⋮ → **自定义存储库**
2. 添加 `https://github.com/kaattz/ha-climatepilot`，类别选 **Integration**
3. 搜索 **ClimatePilot** 安装
4. **重启 Home Assistant**
5. 设置 → 设备与服务 → **添加集成** → ClimatePilot

### 手动安装

```bash
cd /config/custom_components
git clone https://github.com/kaattz/ha-climatepilot.git /tmp/climatepilot
cp -r /tmp/climatepilot/custom_components/climatepilot /config/custom_components/
```

然后重启 Home Assistant。

## 配置

配置流程分 7 步：

1. **系统名称**
2. **热源** — 选择锅炉 `climate` 实体，以及可选的燃烧状态与供水温度传感器
3. **设备语义核验** — 三项必须显式确认（见下）
4. **室外温度** — 选择温度来源与过期判定
5. **天气补偿曲线** — 两点线性曲线（温州湿式地暖初始建议：室外 0℃ → 45℃，15℃ → 40℃）
6. **采暖区域** — 添加 KNX 房间温控器
7. **控制模式** — 建议先"观察"

### 三项设备语义核验（上线前强制）

在确认下列行为之前，ClimatePilot **不会**启停热源，也不会写锅炉水温，只在实体上输出建议值：

- [ ] 关闭采暖模式**不会**影响生活热水与防冻保护
- [ ] `set_temperature` 对应的是**采暖供水温度**（不是室温、不是生活热水）
- [ ] 最小循环流量与旁通/水力解耦**已由安装方验收**

这三项不是形式主义：写错水温与写错启停同样危险，第 2 项写错会把生活热水或室温改掉。

## 实体

### 系统级

| 实体 | 说明 |
|---|---|
| `select.<system>_control_mode` | 观察 / 协调 / 暂停 |
| `sensor.<system>_heating_state` | 观察 / 无需求 / 准备 / 采暖中 / 已满足 / 受限 |
| `sensor.<system>_active_zones` | 真实需求分区数 |
| `sensor.<system>_suggested_supply_temp` | 天气补偿建议水温 |
| `sensor.<system>_outdoor_temperature` | 室外温度 |
| `sensor.<system>_supply_temperature` | 锅炉供水温度读数 |
| `sensor.<system>_burner_starts_24h` | 24 小时点火次数 |
| `sensor.<system>_last_burn_duration` | 最近一次燃烧时长 |
| `sensor.<system>_dominant_zone_combination` | 24 小时内占比最高的需求组合（无需求时 `none`） |
| `sensor.<system>_zone_combination_count` | 24 小时内出现过的不同需求组合数 |
| `sensor.<system>_combinations_24h` | 组合 → 占比明细（JSON 属性） |
| `sensor.<system>_cycling_quality` | 采暖燃烧质量：良好 / 一般 / 偏差 / 未知 |
| `sensor.<system>_unexplained_burns_24h` | 无任何分区需求却点火的次数 |
| `sensor.<system>_return_temperature` | 回水温度（外贴探头，未装则 `unknown`） |
| `sensor.<system>_delta_t` | 供回水温差 ΔT（**不是热功率**） |
| `sensor.<system>_delta_t_quality` | 探头读数质量：有效 / 过期 / 不可信 / 卡死 / **无探头读数** |
| `sensor.<system>_condensing_regime` | 冷凝工况（间接指标）：可能在冷凝 / 不太可能 / 未知 |
| `sensor.<system>_hydraulic_verdict` | 水力判定：正常 / 温差偏低 / 温差偏高 / 失衡 / 未知 || `sensor.<system>_curve_calibration_advice` | 曲线校准**建议**（集成不会自动改曲线） |
| `binary_sensor.<system>_short_cycle_alert` | 短循环告警 |
| `binary_sensor.<system>_degraded` | 受限运行 |
| `binary_sensor.<system>_probe_fault` | 任一**已配置**探头不可信（未装探头不报） |
| `number.<system>_supply_temperature_override` | 手动设定供水温度 |
| `button.<system>_reassess` | 立即重新评估 |
| `climate.<system>_thermostat` | 全屋采暖总览（**不复制**各个 KNX 房间实体） |

### 每个房间

| 实体 | 说明 |
|---|---|
| `select.<room>_control_mode` | 自动 / 舒适 / 节能 / 外出 / 手动 |
| `sensor.<room>_effective_setpoint` | 仲裁后的唯一目标 |
| `sensor.<room>_setpoint_source` | 手动覆盖 / 固定模式 / 日程 / 寒潮修正 / 辅助负载 / 缺省 |
| `sensor.<room>_zone_temperature` | 房间温度 |
| `sensor.<room>_override_remaining` | 手动覆盖剩余时间 |
| `sensor.<room>_comfort_verdict` | 舒适判定：正常 / 过热 / 欠温 / 未知 |
| `sensor.<room>_comfort_advice` | 处置建议码（见 [DEPLOYMENT 7.4](docs/DEPLOYMENT.md)） |
| `binary_sensor.<room>_manual_override` | 手动覆盖中 |
| `binary_sensor.<room>_heat_request` | 该房间真实在要热 |
| `binary_sensor.<room>_overheating` | 持续高于目标 |
| `binary_sensor.<room>_underheating` | 持续低于目标**且确实在要热** |
| `button.<room>_resume_auto` | 立即恢复自动 |
| `switch.<room>_auxiliary_load` | 该房间可作为辅助热负载 |

### 服务

| 服务 | 说明 |
|---|---|
| `climatepilot.resume_auto` | 清除指定房间（或全部）的手动覆盖 |
| `climatepilot.set_supply_temperature` | 显式设置供水温度 |
| `climatepilot.reassess` | 立即重跑仲裁与热源评估 |

## 控制模式

| 模式 | 行为 |
|---|---|
| `observe` | **只读**。计算并展示所有决策与原因，不写任何实体。首次配置的默认值 |
| `coordinate` | 在核验通过后写入房间目标与锅炉 |
| `paused` | 停止一切写入，保留状态与手动覆盖 |

从 `observe` 切换到 `coordinate` 不会自动执行——需要你显式选择。

## 故障与恢复

| 场景 | 行为 |
|---|---|
| HA 重启 | 加载持久化状态，进入恢复宽限期，读取真实 KNX 值，不立即覆盖 |
| KNX 实体暂时 unavailable | 冻结该房间写入并标记不确定；**不把未知当成 0 个需求** |
| 室外传感器失联 | 维持最近确认的安全水温目标，仅告警 |
| 锅炉状态失联 | 停止新写入与重试风暴，进入受限状态并告警 |
| 写入成功但无回读 | 进入等待确认/不确定状态，有限重试后停止 |
| 手动改温与自动命令并发 | 按命令事务与时序仲裁；有歧义时**保护手动操作** |
| 所有分区无需求 | 撤销已验证的采暖请求（经需求消抖与后循环） |

> **⚠️ 重要风险披露**
> 若 Home Assistant 是锅炉采暖请求的唯一来源，HA 停机可能导致采暖丢失。请确保有可靠的本地备用控制链路，或明确接受该风险。

## 开发

```bash
git clone https://github.com/kaattz/ha-climatepilot
cd ha-climatepilot

# 运行测试（需要 Home Assistant 测试环境）
python -m pytest -q

# 代码检查
ruff check custom_components/ tests/
```

### 代码结构

```
custom_components/climatepilot/
├── const.py                 # 常量（domain / 配置键 / 原因码）
├── models.py                # 纯数据模型（无 HA 依赖）
├── temperature_arbiter.py   # 温度仲裁器（纯引擎）
├── zone_coordinator.py      # 写入节流 / 回声识别（纯引擎）
├── aux_load.py              # 有界辅助热负载（纯引擎）
├── weather_compensator.py   # 天气补偿（纯引擎）
├── heat_source.py           # 热源状态机（纯引擎）
├── diagnostics_engine.py    # 燃烧统计与质量分级（纯引擎）
├── zone_combinations.py     # 区域组合分析（纯引擎，V0.2）
├── comfort_diagnostics.py   # 过热/欠温诊断（纯引擎，V0.2）
├── probe_monitor.py         # 探头可用性与 85℃ 哨兵（纯引擎，V0.3）
├── delta_t_diagnostics.py   # ΔT / 冷凝 / 水力 / 校准建议（纯引擎，V0.3）
├── climate_adapter.py       # KNX/锅炉 climate 适配
├── store.py                 # 状态持久化与版本迁移
├── coordinator.py           # 唯一的 I/O 协调者
├── config_flow.py           # 配置向导
├── options_flow.py          # 房间与策略设置
├── base.py + 各平台文件      # 实体
└── diagnostics.py           # HA 诊断导出（脱敏）
```

**架构约束：** `temperature_arbiter` / `zone_coordinator` / `aux_load` / `weather_compensator` / `heat_source` / `diagnostics_engine` / `zone_combinations` / `comfort_diagnostics` / `probe_monitor` / `delta_t_diagnostics` 是**纯 Python 引擎**，不 import Home Assistant，因此其行为可以脱离 HA 独立测试，且所有时间由参数注入（不调用 `datetime.now()`）。

**可解释性：** 每个写入决策都带原因码（`manual_override`、`schedule_transition`、`cold_adjustment`、`range_clamp`、`boiler_min_flow_unverified` 等），并暴露在实体属性中。

## 命名约定

| 项目 | 值 |
|---|---|
| 集成显示名称 | ClimatePilot |
| 中文名称 | 智暖领航 |
| GitHub 仓库 | ha-climatepilot |
| HA 集成 Domain | `climatepilot` |
| Python 包路径 | `custom_components/climatepilot` |
| 品牌归属 | HomeAxis |

## 状态与路线图

- **V0.1** — 温度仲裁、手动覆盖、日程、写入回声识别、需求聚合、热源状态机、天气补偿、诊断与恢复
- **V0.2** — ✅ 辅助热负载与燃烧诊断增强、✅ **区域组合变化分析**、✅ **过热/欠温诊断**、✅ **燃烧质量分级（区分采暖/生活热水）**、✅ **每房间/锅炉调度可视化**
- **V0.3（当前）** — ✅ **ESPHome 外贴供回水探头与可用性/偏差检测（含 85℃ 哨兵）**、✅ **ΔT 趋势监测**、✅ **冷凝工况评估（间接指标）**、✅ **不同区域组合的水力诊断**、✅ **天气补偿曲线校准建议（纯建议，不自动改曲线）**
- **V1.0** — 多家庭部署、国际化、升级迁移、不同品牌适配器契约测试

### V0.2 新增：诊断层

V0.2 增加的全部是**只读诊断**，不改变控制行为：

| 能力 | 实体 | 说明 |
|---|---|---|
| **区域组合分析** | `dominant_zone_combination` / `zone_combination_count` / `combinations_24h` | 统计哪些房间在**同时**要热、组合多零碎；抖动会被抑制，短区间不单独计数 |
| **舒适度诊断** | 每房间 `comfort_verdict` / `comfort_advice` / `overheating` / `underheating` | 分房间判断过热与欠温，并给出可执行的建议码 |
| **燃烧质量分级** | `cycling_quality` / `unexplained_burns_24h` | 按采暖燃烧平均时长分级（≥600s 良好 / ≥300s 一般 / <300s 偏差）；生活热水**不参与**判定 |

> **⚠️ 这些是诊断建议，集成不会据此自动修改锅炉。** 不会自动改水温、不会自动启停、
> 不会自动改房间目标。短循环检测同样**默认仅告警**。
> `check_hydraulics_or_balancing` 尤其容易被误读——房间**没在要热**却偏冷，
> 是阀门/执行器/水力平衡问题，**提高水温解决不了**。
> 处置方法见 [docs/DEPLOYMENT.md 第 7.4 节](docs/DEPLOYMENT.md)。

可视化面板见 [`dashboards/climatepilot.yaml`](dashboards/climatepilot.yaml)，
导入说明见 [docs/DASHBOARD.md](docs/DASHBOARD.md)。

### V0.3 新增：探头诊断层

V0.3 增加的同样是**只读诊断**，不改变控制行为：

| 能力 | 实体 | 说明 |
|---|---|---|
| **探头可用性** | `delta_t_quality` / `probe_fault` | 把一路原始读数判为有效/过期/不可信/卡死/无探头读数，并识别 85℃ 哨兵 |
| **ΔT 趋势** | `return_temperature` / `delta_t` | 供回水温差及其趋势（**不是热功率**） |
| **冷凝工况** | `condensing_regime` | 回水温度是否低于冷凝判据（**间接**指标） |
| **水力判定** | `hydraulic_verdict` | 由 ΔT 落在健康区间的哪一侧，推断流量过大 / 流量不足 / 失衡 |
| **曲线校准** | `curve_calibration_advice` | 结合欠温/过热证据给出的**纯建议** |

> **⚠️ 键名不等于字面含义**：`hydraulic_verdict` 的 `low_flow` / `high_flow`
> 是**「低 ΔT 工况 / 高 ΔT 工况」的标签，不是"流量太小/太大"的判断**。
> **低 ΔT（`low_flow`）通常意味着流量偏大**或旁通短路；**高 ΔT（`high_flow`）
> 才意味着流量不足**。若按字面把 `high_flow` 读成"流量太大"去关小循环泵，
> 会把已经流量不足的系统调得更糟。完整对照表见
> [docs/DEPLOYMENT.md 第 7.5.5 节](docs/DEPLOYMENT.md)。

> **⚠️ 探头是监测输入，不是安全装置。**
> 探头损坏、断线、读数异常**不会**改变集成的任何写入决策；
> 锅炉的最小流量保护、过热保护与防冻逻辑始终由锅炉自身负责。
> `curve_calibration_advice` 只是建议 —— 集成**不会**自动修改天气补偿曲线，
> 曲线由你在配置中持有，需要你自己去 options 里调整。
> 安装要求与逐项处置建议见 [docs/DEPLOYMENT.md 第 7.5 节](docs/DEPLOYMENT.md)。

> **部署状态**：V0.3 **未部署到实机、未经现场验证**，
> 尚未在真实 DS18B20 探头上验证过；全部判据为设计建议值。
> 详见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

MIT — 见 [LICENSE](LICENSE)

---

*本文档描述的初始水温参数与曲线为**待现场验证**的建议值，不是热负荷计算结论。集成不宣称已通过所有锅炉型号的实机验证；锅炉启停与生活热水行为必须在你的设备上实测确认。*
*V0.2 的区域组合、舒适度与燃烧质量诊断同样**未部署到实机、未经现场验证**，只输出建议，不会自动修改锅炉。*
*V0.3 的探头诊断**未部署到实机、未经现场验证**（尚未在真实 DS18B20 上标定）。探头只作监测/优化输入，**不承担锅炉安全停机功能**；ΔT **不是**热功率，不能据此推算效率或热输出；曲线校准建议不会自动生效。*

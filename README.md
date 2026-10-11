<div align="center">

# ClimatePilot · 智暖领航

**多区域采暖协调与优化 · Multi-zone heating coordination for Home Assistant**

by [HomeAxis](https://github.com/kaattz)

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2025.1%2B-18BCF2.svg)](https://www.home-assistant.io/)
[![Version](https://img.shields.io/badge/version-0.2.0-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> **v0.2.0 — 未部署到实机、未经现场验证。** 全部阈值与曲线均为设计建议值。
> 见 [CHANGELOG.md](CHANGELOG.md)。

</div>

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
| `binary_sensor.<system>_short_cycle_alert` | 短循环告警 |
| `binary_sensor.<system>_degraded` | 受限运行 |
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
├── climate_adapter.py       # KNX/锅炉 climate 适配
├── store.py                 # 状态持久化与版本迁移
├── coordinator.py           # 唯一的 I/O 协调者
├── config_flow.py           # 配置向导
├── options_flow.py          # 房间与策略设置
├── base.py + 各平台文件      # 实体
└── diagnostics.py           # HA 诊断导出（脱敏）
```

**架构约束：** `temperature_arbiter` / `zone_coordinator` / `aux_load` / `weather_compensator` / `heat_source` / `diagnostics_engine` / `zone_combinations` / `comfort_diagnostics` 是**纯 Python 引擎**，不 import Home Assistant，因此其行为可以脱离 HA 独立测试，且所有时间由参数注入（不调用 `datetime.now()`）。

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
- **V0.2（当前）** — ✅ 辅助热负载与燃烧诊断增强、✅ **区域组合变化分析**、✅ **过热/欠温诊断**、✅ **燃烧质量分级（区分采暖/生活热水）**、✅ **每房间/锅炉调度可视化**
- **V0.3** — ESPHome 外贴回水/供水探头、ΔT 趋势与冷凝工况评估
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

> **部署状态**：V0.2 **未部署到实机、未经现场验证**，全部判据为设计建议值。
> 详见 [CHANGELOG.md](CHANGELOG.md)。

## 许可

MIT — 见 [LICENSE](LICENSE)

---

*本文档描述的初始水温参数与曲线为**待现场验证**的建议值，不是热负荷计算结论。集成不宣称已通过所有锅炉型号的实机验证；锅炉启停与生活热水行为必须在你的设备上实测确认。*
*V0.2 的区域组合、舒适度与燃烧质量诊断同样**未部署到实机、未经现场验证**，只输出建议，不会自动修改锅炉。*

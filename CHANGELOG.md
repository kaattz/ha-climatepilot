# Changelog

ClimatePilot（智暖领航）的全部重要变更都记录在此。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [未发布]

（无）

---

## [0.3.0] — 2026-10-11

> **部署状态：未部署到实机，未经现场验证。**
>
> 本版本的全部代码与文档都只在开发环境中完成并通过自动化测试，
> **尚未在真实采暖系统上运行过，也尚未在真实 DS18B20 探头上验证过**。
> 探头阈值（过期/卡死判定、健康 ΔT 区间、冷凝判据）均为**设计建议值**，
> 需要在你的系统上实测标定后才谈得上可信。
>
> **安全边界（本版本的核心）：**
> **外贴探头属于监测与优化输入，不承担任何锅炉安全停机功能。**
> 探头损坏、断线、读数异常**不会**改变集成的任何写入决策 ——
> 锅炉的最小流量保护、过热保护与防冻逻辑始终由锅炉自身的硬件与原生程序负责。
> 若你打算用探头读数触发任何保护动作，**请在锅炉/ KNX 侧独立实现**，
> 不要依赖本集成。

### Added

- **探头可用性与偏差检测**（`probe_monitor.py`，纯引擎）：把一路原始读数判为
  `valid` / `stale` / `implausible` / `frozen` / `unavailable`，并给出原因码。
  判定顺序固定：缺失 → **85℃ 哨兵** → 越界 → 过期 → 卡死（**仅燃烧时**）。
- **DS18B20 哨兵识别**：DS18B20 在转换失败或总线断开时报出的**恰好是 85℃**。
  集成将其判为 `implausible` 而**不是**"一根很热的管子"——否则会凭空报出不存在的
  锅炉故障，或算出一个看起来很成功、其实毫无意义的"巨大 ΔT"。
- **供回水对偶校验**（`probe_monitor.assess_pair`）：两路探头互相印证；
  偏差超过 `max_divergence` 时**两路都降为 `implausible`**（无法判断哪一路错了，
  因此两路都不可信）。**任一探头不可用时差值恒为 `None`**，
  绝不用单边读数凑一个差值。
- **ΔT 趋势 / 冷凝工况 / 水力判定 / 曲线校准建议**（`delta_t_diagnostics.py`，纯引擎）：
  滚动窗口内的 ΔT 统计与趋势，输出四个带质量标签的结论。
- **7 个新实体**（translation_key 与英文名逐字冻结，见 `INTERFACES_V0.3.md` 第 3 节）：
  - 系统级 sensor：`return_temperature`、`delta_t`、`delta_t_quality`、
    `condensing_regime`、`hydraulic_verdict`、`curve_calibration_advice`
  - 系统级 binary_sensor：`probe_fault`
  - 实体总数由 44 增至 **51**（2 房间时）。
- **中英文翻译**：新增 7 个实体名，并为 4 个枚举型 state 补全状态翻译
  （共 17 个状态码），否则 UI 会直接显示原始英文码。
- **`docs/DEPLOYMENT.md` 第 7.5 节**：探头安装要求、85℃ 哨兵的读法、
  怎么读 ΔT、以及每个 `hydraulic_verdict` / `curve_calibration_advice` 取值的处置建议。
- **配置流程**：`boiler` 步骤新增两个**可选**外贴探头选择器；选项流程暴露
  探头与 ΔT 阈值，并校验 `delta_t_healthy_min < delta_t_healthy_max`。

### Changed

- `manifest.json` 版本号 `0.2.0` → `0.3.0`。
- `coordinator.py` 每周期刷新 `probes` / `delta_t` / `probe_fault`；
  诊断快照新增 `probes` 与 `delta_t` 两个键。
- README 实体表（+7）、V0.3 章节、路线图与版本引用同步更新。
- V0.1/V0.2 的既有实体、服务与配置键**不改名、不删除**。

### Fixed

- 无用户可见缺陷修复。本版本新增的全部是**只读诊断**，
  不改变 V0.1/V0.2 的任何控制行为。

### 安全边界（本版本新增）

- **ΔT 不是热功率。** 供回水温差**不能**用来推算真实热效率或热输出。
  ΔT 只是流动与冷凝工况的**间接**指标；所有诊断输出都带
  `is_power_measurement = False` 标记与 `REASON_NOT_A_POWER_MEASUREMENT` 原因码。
- **`curve_calibration_advice` 是纯建议。** 集成**不会**自动修改天气补偿曲线 ——
  曲线由用户在配置中持有，任何调整都必须由用户显式执行。
- **文档明确区分「工况标签」与「物理成因」。** `hydraulic_verdict` 的
  `low_flow` / `high_flow` 是**低/高 ΔT 工况**的标签，**不是**"流量太小/太大"的
  判断 —— 低 ΔT 通常意味着流量**偏大**或旁通短路，高 ΔT 才意味着流量不足。
  按字面把 `high_flow` 读成"流量太大"去关小循环泵，会把已经流量不足的系统调得更糟。
  `docs/DEPLOYMENT.md` 第 7.5.5 节给出「键名 → 物理含义 → 通常成因 → 建议动作」对照表。
- **`curve_calibration_advice` 是纯建议，集成不会自动改曲线。**
  曲线由用户在配置中持有；用户需自己去 options 里调整。触发依据是
  窗口内是否存在欠温 / 过热房间证据。
- **探头失联不影响核心采暖控制。** 探头故障只降低诊断的可信度，
  不改变房间目标、不改变锅炉启停、不改变供水温度。
- **探头未配置 ≠ 实体故障。** 未装探头时 `return_temperature` / `delta_t`
  返回 `unknown` 但实体仍 `available`，`probe_fault` 保持 off ――
  多数安装根本没有外贴探头，常亮一个故障位只会训练用户忽略它。
- **探头质量 `probe_unavailable` 有意避开 HA 保留字面量。**
  `unavailable` 是 `homeassistant.const.STATE_UNAVAILABLE`：任何实体发布它，
  Home Assistant 会渲染成**真正的"实体不可用"**（灰掉，与集成坏掉无法区分）。
  若用它表示"探头没读数"，`delta_t_quality` 在探头缺失时就永远显示为
  "ClimatePilot 坏了"，恰好抹掉这个传感器存在的意义。
  因此该取值定为 `probe_unavailable`（UI 显示"无探头读数"）。
  相对地，`unknown` 一族（冷凝工况 / 水力判定）**有意保持** `unknown` ——
  其语义是"实体可用、值未确定"，正是那些判定想表达的。

---

## [0.2.0] — 2026-10-11

> **部署状态：未部署到实机，未经现场验证。**
>
> 本版本的全部代码与文档都只在开发环境中完成并通过自动化测试，
> **尚未在真实采暖系统上运行过**。区域组合分析、舒适度诊断与燃烧质量分级
> 的判据（阈值、驻留时间、窗口长度）均为**设计建议值**，
> 未经过一个完整采暖季的实机标定。请先在 `observe` 模式下观察，
> 不要直接依据本版本的诊断结论改动锅炉参数。

### Added

- **区域组合分析**（`zone_combinations.py`，纯引擎）：以排序后的房间 ID 集合为键
  统计同时段的需求组合，支持抖动抑制（`min_dwell`）、短区间折叠、24 小时滚动窗口、
  占比与"主导组合"判定；`snapshot()` / `restore()` 支持跨重启往返。
- **舒适度诊断**（`comfort_diagnostics.py`，纯引擎）：按房间统计过热 / 欠温，
  给出 `comfort_verdict`（`ok` / `overheating` / `underheating` / `unknown`）与
  可执行的 `comfort_advice` 建议码。缺失读数**绝不按 0 处理**，而是判为 `unknown`。
- **燃烧质量分级**（`diagnostics_engine.py` 扩展）：新增 `cycling_quality`
  （按采暖燃烧平均时长分级：≥ 600 s `good`，≥ 300 s `fair`，< 300 s `poor`，
  无数据 `unknown`）、`cycling_quality_reason`、`unexplained_burns_24h`、
  `grade_heating_24h`、`grade_dhw_24h`。
  生活热水燃烧**不参与**采暖质量分级。
- **9 个新实体**（translation_key 逐字冻结）：
  - 系统级 sensor：`dominant_zone_combination`、`zone_combination_count`、
    `combinations_24h`、`cycling_quality`、`unexplained_burns_24h`
  - 每房间 sensor：`comfort_verdict`、`comfort_advice`
  - 每房间 binary_sensor：`overheating`、`underheating`
  - 实体总数由 31 增至 **44**。
- **Lovelace 面板**：`dashboards/climatepilot.yaml`（sections 布局）与
  `docs/DASHBOARD.md` 导入说明。
- **中英文翻译**：新增 9 个实体名，并为 3 个枚举型 state 补全状态翻译
  （共 13 个状态码），否则 UI 会直接显示原始英文码。
- **`docs/DEPLOYMENT.md` 第 7.4 节**：V0.2 诊断的读法与处置建议，
  含 `cycling_quality` 阈值含义与 `comfort_advice` 每个码的处理方式。

### Changed

- `manifest.json` 版本号 `0.1.0` → `0.2.0`。
- `diagnostics_engine.BurnerStats.snapshot()` 新增 5 个键；
  V0.1 的 16 个契约键**保持不改名、不删除**。
- `restore()` 向后兼容：可以无异常地读取 V0.1 写入的持久化数据
  （旧数据没有新字段，用默认值兜底）。
- README 实体表、路线图与版本引用同步更新。

### Fixed

- 无用户可见缺陷修复。本版本的 `cycling_quality` 与 `unexplained_burns_24h`
  为**新增**诊断，不改变 V0.1 的任何控制行为。

### 安全边界（本版本不变）

- 短循环检测仍然**默认仅告警**，不会据此修改锅炉内部安全流程。
- `comfort_advice` 是**诊断建议**，集成**不会**据此自动修改锅炉水温或启停。
- 三项设备语义核验未全部确认时，集成仍停留在仅建议模式。

---

## [0.1.0] — 2026-10-11

> **部署状态：未部署到实机，未经现场验证**（同上）。
> 本版本的可运行性由 608 个自动化测试保证；锅炉启停与生活热水行为
> 未在任何具体锅炉型号上实测。

首个公开版本。

### Added

- **温度仲裁器**（`temperature_arbiter.py`）：P0–P4 优先级仲裁，
  每房间唯一有效目标 `effective_setpoint`。
- **手动覆盖**：三种恢复策略（下个日程节点 / 固定时长 / 一直保持），
  固定时长策略持久化绝对到期时刻，重启后不延长。
- **写入回声识别**（`zone_coordinator.py`）：基于待确认命令事务区分自写回声与
  外部操作；确认窗口过期后保护可能的手动操作。
- **真实热需求聚合**：依据 `hvac_action`（而非 `heat` 模式）判定需求，
  `unknown` / `unavailable` **绝不当作"无需求"**。
- **有界辅助热负载**（`aux_load.py`）：有上限、有回差、有最短切换间隔、
  夜间禁止，并在其他房间需求结束时明确归位。
- **天气补偿**（`weather_compensator.py`）：两点线性曲线 + 限幅 + 防抖 +
  最小下发间隔；数据无效或过期时保持最后一次已确认的安全值，**绝不按 0℃ 猜测**。
- **热源状态机**（`heat_source.py`）：需求消抖、阀门预开等待、最短开/停机、
  水泵后循环、DEGRADED 降级。
- **燃烧诊断**（`diagnostics_engine.py`）：最近燃烧时长、1h/24h 点火次数、
  周期直方图与短循环告警；区分采暖燃烧与生活热水燃烧。
- **配置流程**：7 步向导（系统名称 → 热源 → 设备语义核验 → 室外温度 →
  天气补偿曲线 → 采暖区域 → 控制模式）；Options 流程支持房间策略、日程、
  热源与诊断参数编辑。
- **实体**：31 个（select / sensor / binary_sensor / button / number / switch /
  climate），全部使用 `translation_key`。
- **服务**：`climatepilot.resume_auto`、`climatepilot.set_supply_temperature`、
  `climatepilot.reassess`。
- **状态持久化**（`store.py`）：带版本迁移。
- **诊断导出**（`diagnostics.py`）：脱敏后的完整决策上下文。
- **三项设备语义核验**：未全部确认前集成停留在仅建议模式。
- **中英文翻译**：`strings.json` / `translations/en.json` /
  `translations/zh-Hans.json` 三份 key 树完全一致。

---

[未发布]: https://github.com/kaattz/ha-climatepilot/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/kaattz/ha-climatepilot/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/kaattz/ha-climatepilot/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kaattz/ha-climatepilot/releases/tag/v0.1.0

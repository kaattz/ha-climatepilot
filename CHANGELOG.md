# Changelog

ClimatePilot（智暖领航）的全部重要变更都记录在此。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [未发布]

（无）

---

## [0.5.1] — 2026-10-14

> **纯修复版本。** 无新功能，无破坏性变更。**建议所有 v0.5.0 用户升级** ——
> 在 v0.5.0 及更早版本上，**任何一次"保存选项"都会报错**。

### Fixed

- **保存选项必定报错，且自选项流程写出以来一直如此（影响 v0.2–v0.5.0）。**

  ```
  ValueError: Config entry update listeners should not be used with
  OptionsFlowWithReload
  ```

  选项流程继承自 `OptionsFlowWithReload`（保存后自动重载配置项），而
  `async_setup_entry` 又注册了一个 update listener，其**唯一作用也是重载配置项**。
  HA **禁止两者并用**（listener 会导致一次保存触发两次重载），检查位于
  `async_finish_flow`，因此**每一次保存都会抛异常**。

  两者是纯重复，已删除 listener（保留自动重载 —— 它才是让保存无需重启就生效的机制）。

  **为什么 1243 个测试都没发现**：现有测试的验证对象是流程自身的各个 step，而检查在
  流程管理器的 `async_finish_flow` 里，单元测试**永远不会走到那里**。这个流程此前在
  实机上从未成功保存过一次。任何测试写得再仔细都找不到它 —— 只有真的按下"保存"才会。

  已补充结构化回归测试：整合包内**不得出现任何 `add_update_listener` 调用**
  （从 AST 解析，因为解释性注释里就引用了这个名字）、流程仍继承自动重载基类、
  以及一条"HA 的禁令仍然存在"的检查，防止上述断言悄悄变成空转。
  反向对照：把 listener 加回去，新测试立刻变红。

### 实机验证（v0.5.0 + 本修复）

- 已部署到实机（HAOS 18.3、HA 2026.9.4、3 个房间）并启用：
  `gas_meter_entity: sensor.gas_meter`、基准 15.5 °C。
- 6 个实体全部注册，实体总数 **65 → 71**，原有实体无一丢失。
- 单位与 `state_class` 实测正确：`m³` / `total_increasing`、`°C·d` / `measurement`、
  `m³/°C·d` / `measurement`；口径属性（`scope: whole_house`、
  `is_efficiency_measurement: false`）均已到位。
- **选项保存已实测成功**（这是本修复要证明的事）。
- **读数当前为 0，这是正确的**：室外 26 °C 高于 15.5 °C 基准，
  `max(0, 15.5 − 26) = 0`，不产生采暖度日，当天也无采暖用气。
  `coverage` 随观察时间上升并如实标注"本日仅被观察一小部分"。
  **有意义的读数要等天冷、锅炉开始采暖之后。**

---

## [0.5.0] — 2026-10-14

> **新增功能，无破坏性变更。** 已有实体 ID 不变；不配置燃气表则行为与 v0.4.0 完全一致。

### Added

- **燃气与度日数计量（可选）**，6 个只读实体：

  ```
  sensor.cp_gas_today / _yesterday / _this_month     用气量 (m³)
  sensor.cp_degree_days_today / _this_month          度日数 (°C·d)
  sensor.cp_gas_per_degree_day                       本月每度日用气量
  ```

  在**设置 → ClimatePilot → 燃气用量与度日数**里配置。**留空即完全关闭** —— 不创建
  实体、不读取状态、零开销。全部带 `state_class`，进入 HA 长期统计（永久保留）。

### 定位：仪表，不是省气证明

原始需求是"我如何知道集成省气了"。调查后确认这个结论**在这里无法诚实给出**，因此
做了仪表而不是结论：

- **集成当前一次都没写过锅炉**（`control_mode = observe`、`last_applied = null`），
  所以"省没省"还没有对象；
- **燃气表是全屋的** —— 同一块表还供生活热水与灶具，这些与天气无关，会稀释信号；
- **没有瞬时流量传感器**，所以按用途分摊只能靠燃烧时长估算，而锅炉是**调节燃烧**的，
  低火与高火同样 10 分钟耗气差好几倍；
- **天气不重复**，唯一可信的对照是同期 A/B 交替，需要几十个周期，且必须在真正需要
  采暖的天气里做 —— 那套实验本身会干扰用户真正想要的稳定舒适。

`m³/度日` 因此是**用于纵向比较的序列**，不是采暖效率。实体属性里带
`is_efficiency_measurement: false` 与 `scope: whole_house`，与 V0.3 用
`is_power_measurement: false` 阻止 ΔT 被当成热功率是同一个原则。

### 度日数

室外温度每低于基准 1°C 持续一天记 1 度日，用于让天气不同的两个月份可比。基准默认
**15.5°C**（可配置），它是**约定而非物理定律**（美国 NOAA 用 65°F ≈ 18.3°C）；选哪个
只影响 `m³/度日` 的绝对值。

**刻意与天气曲线的 `outdoor_stop` 分开**：那个值用户会为控制效果去调，共用会让历史
燃气数据在调曲线时被悄悄重新定义，导致前后不可比。

### 三条写进代码的边界

| 边界 | 实现 |
|---|---|
| 表可能被换/清零 | 不产出负数用量，记 `meter_reset_detected` |
| 室外温度缺失 | **跳过**并降低 `coverage`，绝不猜 0°C（那会凭空放大度日数） |
| 无读数 ≠ 0 | 显示 `unknown`；`0 m³` 等于宣称"今天没烧气" |

### 开发过程中修掉的两个真实缺陷

- **度日数算成了"度·小时"**：`(温差 × 秒) / 3600` 得到的是度·小时，比度日大 24 倍。
  一整天 10.5°C 温差读成 252 而非 10.5。
- **跨周期边界的燃气会凭空消失**：没有把读数带过边界时，跨越午夜的区间**不属于任何
  一天** —— 结束的那天已停止累加，新的一天从"已经烧完之后"的读数开始。5 m³ 走过了表
  却在所有统计里都不存在，而且**随轮询间隔成比例放大**。修复是把最后一个读数带过边界，
  每个区间记在它**结束**所属的周期里。

  两个缺陷都是先写出测试才发现的；第二个是在停止调整期望值、改为追查"气去哪了"
  之后才暴露出来的。

### 测试

- **1243 tests 通过**，在 **HA 2026.2.3 与 2026.9.4** 上均通过。
- 反向对照：引擎 **11/11**、协调器 **10/10**、传感器 **10/10** 变异体全部被抓。
  第一轮协调器只抓到 5/9，每个存活变异体都是**真实的测试缺陷**而非冗余守卫 —— 例如
  两个测试直接调用私有 `_restore_energy`，所以把调用从 restore 路径里删掉仍然全绿；
  以及"功能关闭"的断言用的是 `gas_today is None`，而功能开启但没有表时它**也是** None。
- 修最后一个时发现共享 fixture 的陷阱：`pin_clock` 冻结时钟，使连续审计的间隔为 0，
  度日数**无论是否运行都不可能累积** —— 阈值因此改为结构化断言 + 自带推进时钟的行为测试。

---

## [0.4.0] — 2026-10-13

> **破坏性变更：全部实体 ID 改变。** 升级后集成会自动重命名（保留历史与
> unique_id），但**你自己的自动化、脚本或面板如果写死了旧 ID，需要跟着改**。

> **部署状态**：已在一台实机（HAOS 18.3、HA 2026.9.4、3 个房间）上安装、重启并验证，
> 运行于**观察模式**（读取与计算，不写入任何设备）。
> 迁移实测结果：**65 个实体全部从拼音 ID 改名为 `cp_*`，unique_id 与历史记录均保留**，
> 二次 reload 无变化，锅炉与三间地暖的设定值在升级前后完全一致。
> 升级前预演（读取该机真实注册表、在内存副本上跑迁移逻辑）先确认了 64 个实体都能正确改名。

### Changed（破坏性）

- **实体 ID 改为固定 `cp_` 前缀**，不再由设备名派生：

  | 之前 | 现在 |
  |---|---|
  | `sensor.wo_de_jia_heating_state` | `sensor.cp_heating_state` |
  | `sensor.ke_ting_di_nuan_effective_setpoint` | `sensor.cp_living_room_effective_setpoint` |
  | `climate.wo_de_jia_whole_home_heating` | `climate.cp_system_thermostat` |

  旧 ID 有两处拼音：系统名称「我的家」→ `wo_de_jia`，房间名「客厅地暖」→
  `ke_ting_di_nuan`。而且名字部分取自**翻译**，所以 ID 还依赖后端语言 ——
  同一个实体在德语安装上会得到不同 ID。这两处耦合现在都没有了。

  **为什么不用「把系统名改成 ASCII」来解决**：那会让 ID 绑在用户可改的显示名上，
  改一次系统名就会改掉全部 52 个实体 ID；而且 `name_by_user` 优先于 `name`，
  将来有人在界面里把设备改回中文，新建的实体又会变回拼音。

  名字部分改用实体自己的内部键，因此**翻译改动不再影响任何 ID**。

- **新增 `switch.cp_heating_season` 采暖季开关**：OFF = 停季（`paused`，撤销自己的
  供暖请求、阻断全部写入），ON = 开季（`observe`，恢复监测与建议，**仍不写入**）。
  它**不会**直接进入 `coordinate` —— 按一下开关就开始写锅炉是个不该有的行为。
  状态派生自 `control_mode`，不独立存储。它也不碰锅炉自身的开关状态，
  只收回"我们自己发出的"请求。

### Added

- **自动迁移**（`migration.py`）：在平台加载前把已有实体从旧 ID 重命名到 `cp_*`，
  用 HA 官方的 `async_update_entity`，**保留 unique_id、历史与引用关系**。
  幂等；无法重命名时记日志并跳过，而不是让集成装载失败。
- `services.yaml`：此前缺失，导致**每次启动都有一行 ERROR**
  （`Failed to load services.yaml for integration: climatepilot`）。服务本身照常工作，
  所以测试完全没发现 —— 只有在实机日志里才看得到。现已补齐，并加了两个契约测试。

### Fixed

- **`via_device` 在 HA 2026.9 上会让全部房间实体注册失败。** HA 已删除该参数
  （改用 `via_device_id`），传旧参数会在设备注册时抛出，结果是中枢级实体正常、
  **按房间的那一半整批消失**。现在按版本自适应。
- 删除集成后重装**不会**让 ID 采用新方案：设备 ID 在删除后会被复用，新 ID 仍从旧设备名
  派生，拼音前缀原样回来。迁移因此是必需的，不是可选的。

### 兼容性

- **HA 2026.2.3 与 2026.9.4 均实测通过**（1161 tests）。

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

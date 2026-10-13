# v0.4.0 设计：ASCII 实体 ID 与采暖季开关

日期：2026-10-13
状态：已与用户确认，待实施

## 背景

安装到实机后，实体 ID 长这样：

```
sensor.wo_de_jia_heating_state
sensor.wo_de_jia_supply_temperature
climate.wo_de_jia_whole_home_heating
```

`wo_de_jia` 是拼音，用户认为用在标识符上不专业。

### 拼音的来源（已实测确认，不是猜测）

集成源码里**没有任何一处**写死 `wo_de_jia`。它是运行时长出来的：

```
配置流程 system_name = "我的家"
      ↓
设备名 = "我的家"
      ↓
has_entity_name=True → HA 按官方规则拼接 "设备名 实体名"
      ↓
HA 的 slugify("我的家") = "wo_de_jia"     ← 拼音在这里产生
      ↓
sensor.wo_de_jia_heating_state
```

HA 官方文档（Entity → Entity naming）规定的规则是：

> 实体属于设备"nightlight"且有自己名字 → `entity_id = sensor.nightlight_battery`

即 **`设备名_实体名` 是官方约定**，目的是避免跨集成碰撞。问题不在规则，在于设备名是中文，而 `slugify` 只接受 ASCII，中文只能转写成拼音。

**另需记录**：配置流程的默认值本来是 `ClimatePilot`（`DEFAULT_NAME`）。是安装时把 `system_name` 填成了"我的家"，才产生拼音。留默认值本可避免。

## 已实测的事实（决定设计）

所有结论均在真实 HA 2026.9.4（Python 3.14）上验证。

### 事实 1：ASCII 系统名确实能产生 ASCII 前缀

每个候选名用**独立的全新实例**测试（早期版本在同一实例里复用 unique_id，首次注册即锁定 id，结论无效）：

```
system_name='CP'                 -> switch.cp_supply_temperature
system_name='ClimatePilot'       -> switch.climatepilot_supply_temperature
system_name='HeatingCoordinator' -> switch.heatingcoordinator_supply_temperature
system_name='我的家'              -> switch.wo_de_jia_supply_temperature
```

### 事实 2：删除集成重装 **不能** 解决问题

```
1. 以"我的家"装入        -> switch.wo_de_jia_supply_temperature
2. 删除 config entry     -> 注册表行已清空
3. 以"CP"重装            -> switch.wo_de_jia_supply_temperature   ← 旧前缀回来了
```

原因：**设备 ID 被复用**（删前删后同为 `26779f37`）。HA 会恢复已删除的设备，实体 ID 在设备名更新之前就已完成推导。

因此用户提出的"删掉重装就行"**不成立**，迁移代码不可省略。

### 事实 3：注册表以 unique_id 为键并锁定 entity_id

```
registry lookup by unique_id -> switch.wo_de_jia_heating_state_first
device_registry.devices as mapping -> 已弃用，读取即抛异常
```

改代码不会自动重命名已存在的实体。

### 事实 4：显式 entity_id 是 HA 支持的路径

HA 源码 `helpers/entity.py`：

```
# Integration suggested object id, derived from entity_id, if it is set by the
# integration before the entity is added.
```

在实体加入平台前设置 `self.entity_id` 是 HA 自己设计的机制，不是绕过。

## 决策：ID 由代码固定，不跟随系统名称

### 备选方案与取舍

| 方案 | 显示名 | ID 是否受改名影响 | 结论 |
|---|---|---|---|
| 自动派生（系统名改 ASCII） | 变英文 "CP 采暖状态" | **会**——改名即改 ID | 否决 |
| 自动派生 + `name_by_user` 中文 | 中文 | **会**——`name_by_user` 优先，前缀再次变拼音 | 否决 |
| **代码固定 `cp_` 前缀** | 保持中文 | 不会 | **采纳** |

否决自动派生的理由：

1. **与需求自相矛盾**：用户要改系统名称去掉拼音，但自动派生会让"改名"这个动作改掉全部 51 个实体 ID，面板里 53 处静态引用当场失效。
2. **有陷阱**：`name_by_user` 优先于 `name`。将来有人在 UI 里把设备改成中文，**之后新建的实体又会变回拼音**——问题会复发。
3. **固定前缀同时拿到两样**：中文显示名 + ASCII 实体 ID。

### 采用方案

```
system_name = "我的家"                    ← 纯显示用途，随便用中文
device.name = "我的家"
entity_id   = sensor.cp_heating_state     ← 代码固定，永不变
friendly_name = "我的家 采暖状态"          ← 中文，好看
```

代价：偏离 HA"ID 跟随设备名"的约定。这是**有意偏离**，理由记录于此，并在 `docs/INTERFACES.md` 写明，避免日后被误判为 bug。

## 实施内容

### 1. 统一 `cp_` 前缀

在 `base.py` 的 `ClimatePilotEntity.__init__` 内，从已有 key 计算并显式赋值 `self.entity_id`：

- 系统级实体：`{domain}.cp_{key}`
- 房间级实体：`{domain}.cp_{zone_id}_{key}`（`zone_id` 已含房间名，房间之间不会互撞）

7 个平台文件不改动——命名只在一个函数里决定。

### 2. `switch.cp_heating_season`

| 开关 | control_mode | 行为 |
|---|---|---|
| OFF（默认） | `paused` | 停季：撤销自己发出的供暖请求，阻断**全部**写入 |
| ON | `observe` | 开季：恢复监测与建议，**仍不写入** |

设计要点：

- **不引入新状态**：复用已实现且已测试的 `paused`。新增独立"季节"状态会导致两个开关争夺同一控制权（季节说开、模式说暂停，谁赢？）。
- **状态派生自 `control_mode`**，不独立存储：`paused` → OFF；`observe`/`coordinate` → ON。防止两处状态不一致。
- **ON 只回到 `observe`**（用户明确选择）：开季不会直接进入 `coordinate`，不会未经确认就开始写三间地暖。这同时修掉了现有隐患——今天按电源键 ON 会直接进入 `coordinate`。
- **不碰锅炉开关状态**（用户明确选择）：只收回"我们自己发出的"请求，保持设计原则 4「FSM 只关掉自己打开的东西」。
- `paused` 持久化在 config entry options，夏季重启不会自行开启。

### 3. 一次性迁移（必需）

在 `async_setup_entry` 中，对本 config entry 的注册表实体执行 `wo_de_jia_*` → `cp_*`：

- 用 HA 官方 `entity_registry.async_update_entity(new_entity_id=...)`，**保留 unique_id 与历史**
- 幂等：已是 `cp_*` 则跳过
- 必须在实体平台加载**之前**完成，否则新旧 ID 会同时存在

### 4. 同步更新引用（遗漏会使面板整片失效）

| 文件 | 处数 |
|---|---|
| `dashboards/climatepilot.yaml` | 53 |
| `docs/DASHBOARD.md` | 28 |
| `docs/INTERFACES_V0.2.md` | 19 |
| `docs/INTERFACES_V0.3.md` | 1 |
| `tests/test_integration_setup.py` | 2 |

### 5. 测试

- **逐一**断言每个实体的 ID（不是抽样）
- 迁移：预置 `wo_de_jia_*` 注册表 → 执行迁移 → 断言全部改名且 **unique_id 与历史不变**
- 迁移幂等：连跑两次结果一致
- **反向对照**：关闭迁移，测试必须变红
- 采暖季开关：OFF→`paused` 且**写入集合为空**（逐字节）；ON→`observe` 且**写入集合仍为空**
- 面板 53 处引用 vs 真实注册表**双向**校验（V0.2 曾因单向检查漏掉 45 处错误 ID）

### 6. 版本

`0.3.0` → `0.4.0`（破坏性：实体 ID 变更）

## 风险与代价

| 风险 | 说明 | 缓解 |
|---|---|---|
| 破坏性变更 | 51 个实体 ID 全变 | 用户尚无自动化引用（现仅 `update.climatepilot_update`），此刻代价最低 |
| 偏离官方约定 | ID 不跟随设备名 | 契约中写明理由 |
| 面板失效 | 静态引用写死旧 ID | 迁移 + 53 处引用同步 + 双向校验测试 |
| 用户已有自定义 | 若有引用需自行更新 | 迁移保留 unique_id 与历史，用户只需改引用文本 |

## 未验证事项（诚实记录）

- 早期一次探针得出"删除+重启后干净"，但该探针调用了**不存在的** `async_save`（正确方法是 `async_schedule_save`），`hasattr` 静默返回 False，注册表从未写盘，第二次启动是空环境——**结论为假阳性，已作废**。
- 因此"删除重装是否足够"**始终未经有效验证**。本设计不依赖该结论：迁移方案在注册表有真实数据的前提下工作。

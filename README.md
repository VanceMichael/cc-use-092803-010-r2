# 山区应急协同服务

该服务用于记录灾害事件、风险区域、救援队伍、物资批次和跨部门处置行动。领域层保持事件顺序，仓储层使用 SQLite 保存可恢复状态，接口层提供本地 JSON 调用入口。

## 跨县联合行动计划

`JointPlanBook` 让授权指挥员把**多个灾害点、派单与物资占用**组织成一个持久化的联合行动计划：

- **绑定**：每个步骤可绑定 `event_id`（灾害点，必须在计划的 `event_ids` 内）、`assignment_id`（派单，其队伍须与步骤队伍一致）、`team_id`/`crew`（队伍及人力）、`lots`（物资批次与占用量）。
- **可执行顺序**：调度器按 ① 依赖未完成 ② 队伍/物资被在途步骤占用 ③ 物资冻结或余量不足 逐一定位等待原因，再对无阻塞步骤按 `priority` 降序、编号升序给出 `execution_order`。
- **局部重排**：`reschedule` 只允许改动 `pending` 步骤；`running`/`completed` 步骤及其资源锁永不回滚。
- **暂停与重启续办**：计划与步骤状态写入 SQLite；`recover_joint_plans` 重载后给出每个未终结计划的 `resume_from`（优先在途步骤，其次优先级最高的待启动步骤）。
- **取消传播**：取消在途步骤会释放队伍能力与物资锁定，并在 `rerunnable_after_cancel` 中列出可重排的后续步骤；依赖被取消的步骤会显式标注“前置步骤已取消，需要指挥员重排”。
- **幂等**：所有写操作以 `request_id` 去重；对已完成步骤的重复确认直接返回当前视图，不重复记账。
- **既有语义不变**：步骤启动沿用 `teams.consume` / `supplies.reserve` / `assignments.acknowledge`，确认沿用 `assignments.complete`；原有的单点派单、物资账与审计动作各自独立成立。

查询结果（`get_joint_plan` / 每次写操作的返回）逐步骤给出：

| 字段 | 含义 |
| --- | --- |
| `state` / `waiting` | 步骤状态与是否等待 |
| `wait_reasons` / `blocked_by` | 为何等待、被哪些前置或占用步骤阻塞 |
| `resources` | 当前实际占用（`held`）或已消耗（`consumed`）的队伍与物资 |
| `binds` | 绑定的灾害点、派单、队伍与物资 |
| `execution_order` | 当前可启动步骤的优先级顺序 |
| `resume_from` | 暂停/重启后的续办点 |

Python 入口（`EmergencyPlatform`）：`create_joint_plan`、`start_joint_plan`、`pause_joint_plan`、
`start_joint_step`、`confirm_joint_step`、`cancel_joint_step`、`reschedule_joint_plan`、
`get_joint_plan`、`recover_joint_plans`。跨县建计划要求指挥员对绑定的**每个**县都有 `plan` 权限。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 构建检查

```bash
python3 -m compileall emergency_service
```

## 使用

可通过 `python3 -m emergency_service.cli` 向标准输入提交一行 JSON 请求。所有时间使用带时区的 ISO-8601 字符串，数据文件由启动参数指定。联合计划相关动作包括
`create_joint_plan`、`start_joint_plan`、`pause_joint_plan`、`start_joint_step`、
`confirm_joint_step`、`cancel_joint_step`、`reschedule_joint_plan`、`get_joint_plan`、
`recover_joint_plans`，持久化时在请求中传入同一个 `db` 文件路径。


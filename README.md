# 山区应急协同服务

该服务用于记录灾害事件、风险区域、救援队伍、物资批次和跨部门处置行动。领域层保持事件顺序，仓储层使用 SQLite 保存可恢复状态，接口层提供本地 JSON 调用入口。

## 跨县联合行动计划

授权指挥员可把多个灾害点（事件）、派单（队伍）与物资占用锁定到同一张持久化的联合行动计划中：

- **资源锁定绑定**：每个步骤绑定一个灾害点、一张派单（确定队伍）与若干物资批次占用；创建计划时原子预留物资，任一步骤校验失败整体回滚。被锁定的派单不能再绕过计划单独确认/完成/取消。
- **可执行顺序计算**：按 `依赖 → 资源冲突（同一队伍被执行中或更高优先级步骤占用、物资冻结）→ 优先级（数值小者优先）` 计算。查询结果逐步骤给出 `state`、`waiting_reason`、`blocked_by`、占用的队伍与物资，以及 `executable_order`。
- **局部重排**：前序步骤取消后，`reschedulable` 标出全部受影响的后续步骤；指挥员可用 `reschedule` 只调整未开始步骤的优先级/依赖或追加新步骤，已完成、已取消和执行中的步骤不受影响；调整失败整体回滚。
- **暂停与重启续办**：`pause` 后任何步骤都不能确认；计划、步骤、物资账、派单和审计均落盘 SQLite，服务重启后自动重建，`resume_from` 指明从执行中的步骤续办。
- **幂等确认**：同一 `request_id` 重复提交直接回放首次结果；对已在执行/已完成步骤的重复确认不重复推进派单、不累计次数、不重复核销物资。
- **权限**：联合计划动作需 `commander` 角色（`joint_plan` 权限），跨县计划要求指挥员覆盖计划涉及的全部灾害点所在县。
- **语义兼容**：原有单点派单、物资预留/释放/冻结、审计查询语义保持不变；计划完成时占用物资正式核销，取消时释放物资并恢复队伍能力。

服务层接口（`EmergencyPlatform`）：`create_joint_plan`、`start_joint_plan`、`pause_joint_plan`、`confirm_step`、`complete_step`、`cancel_step`、`reschedule`、`joint_plan_view`。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 构建检查

```bash
python3 -m compileall emergency_service
```

## 使用

可通过 `python3 -m emergency_service.cli` 向标准输入提交一行 JSON 请求。所有时间使用带时区的 ISO-8601 字符串，数据文件由启动参数 `db` 指定（指向同一文件即可在重启后续办）。支持的动作包括 `create_event`、`register_team`、`add_supply`、`assign`、`create_joint_plan`、`start_joint_plan`、`pause_joint_plan`、`confirm_step`、`complete_step`、`cancel_step`、`reschedule`、`view_joint_plan`。

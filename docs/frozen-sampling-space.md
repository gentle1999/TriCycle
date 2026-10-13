# 固定采样空间

固定采样空间是数据与身份适配结果的不可变绑定，不包含可执行历史源码。当前 pin 入口为 [frozen-sampling-space.toml](../configs/frozen-sampling-space.toml)；快照 ID、来源路径、文件哈希及实际规模从该配置和 manifest 读取。文档不记录某次导出的数量、覆盖率或核验成绩。

## 导出与固定

原生数据由 [数据库池配置](../configs/database-sampling-pools.toml)和 `tricycle_reaction_db.pools` 导出，匹配规则见[数据适配规范](reaction-dataset-adaptation.md)。数据库逐页使用活化自由能非空筛选，receipt 必须记录相同参数；返回无标签行时失败，不在本地删行兜底。

只有在明确选择新的数据空间后，才从已核验的导出创建新 pin：

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning freeze-db \
  --root /absolute/verified-export --snapshot-id selected-space \
  --pin-config /absolute/new-pin.toml
```

固定过程保留所有原生 profile 行、原始字段与 `export_row_index`，记录身份适配证据、文件大小和哈希。GT 有标签组合必须覆盖注册表的 ene×diene 集合；不能取最小/平均标签、借 TS 输入配对或本地补标签。新输出不覆盖既有 pin。

GT 位于 `evaluation`，代理池位于 `candidates`；采样可见注册表只含 ID/SMILES。原注册表中的历史 QM 信息留在独立评测来源目录，不交给采集决策。数据库耗时保持原生秒数与空值；核时换算、共享资源和 profile 重复成本由实验账本明确。

## 使用和校验

```bash
uv run --package tricycle-agent python -m tricycle_sampling.active_learning verify-frozen-db \
  --config configs/frozen-sampling-space.toml
```

新实验设置 `[dataset] frozen_space_config` 指向选定 pin。loader 读取指定的 GT 和候选 collection，拒绝与 pin 冲突的显式覆盖；validate/prepare 再次核验并记录 snapshot ID 和 manifest hash。项目的成本账本仍需显式绑定。

缺少文件或哈希不符时停止并核对来源，不自动改用最新导出，不刷新旧 hash。快照的文件完整性校验不等同于数据库跨页事务快照，也不提供操作系统层面的防修改保证。

实际导出回执、分页核验、覆盖统计和耗时对比保存在对应 run/快照中，均不纳入源码提交。被替代的 pin 按[配置归档规则](../configs/README.md)保留；当前工作流不依赖本机归档。已有实验的数据、结果和 checkpoint 不改写。

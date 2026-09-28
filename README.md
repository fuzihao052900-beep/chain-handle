# chain-handle

[English summary](#english-summary)

`chain-handle` 是一个面向 Codex 的出行链轨迹处理 Skill。它识别 CSV 中的经纬度、时间和轨迹编号，使用 ArcPy 生成 WGS 84 文件地理数据库，并为每个轨迹点计算距离、速度、停留时间和节点类型。

## 主要功能

- 自动识别中英文经纬度、日期时间、轨迹编号、速度和距离字段。
- 要求时间至少精确到分钟，并报告无效坐标与时间记录。
- 按轨迹编号和本地日期拆分轨迹。
- 合并连续且经纬度完全相同的点，计算 `stop_time`。
- 计算 WGS 84 椭球测地距离 `real_distance` 和速度 `real_speed`。
- 标记 `start`、`end`、`stop` 和 `way`。
- 每条有效轨迹生成一个点要素类，同时生成处理摘要和运行元数据表。
- 使用独立 ArcPy 写入/验证进程和原子回执，隔离 ArcPy 退出阶段崩溃。
- 处理完成后提供可粘贴到 ArcGIS Pro Python 窗口的 `kind` 分类符号代码。

## 输出结构

每个 CSV 生成一个 `<源文件名>_trip_chains.gdb`：

- 每条有效轨迹对应一个 EPSG:4326 点要素类；
- `processing_summary`：每条候选轨迹的处理状态与计数；
- `run_metadata`：输入、字段映射、坐标系、规则和输出映射；
- 原始属性保留在安全重命名后的字段中。

## 环境要求

- Windows
- ArcGIS Pro 3.6 或兼容版本
- 可用的 ArcGIS Pro 许可
- ArcGIS Pro 自带的 ArcPy 环境
- Codex（作为 Skill 使用时）

ArcPy 由 ArcGIS Pro 提供，不能通过普通 `pip install arcpy` 代替，也不包含在本仓库的 MIT 许可证中。

## 安装为 Codex Skill

将仓库克隆到 Codex Skills 目录：

```powershell
git clone https://github.com/fuzihao052900-beep/chain-handle.git `
  "$env:USERPROFILE\.codex\skills\chain-handle"
```

然后在 Codex 中调用：

```text
$chain-handle
```

## 命令行运行

建议通过 Esri 的 `propy.bat` 启动活动 ArcGIS Pro Python 环境：

```powershell
& 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat' `
  '.\scripts\process_trip_chain.py' `
  --input 'C:\data\tracks.csv' `
  --output-dir 'C:\data\results'
```

如果 ArcGIS Pro 安装在其他位置，请替换 `propy.bat` 路径。

默认不会覆盖已有 GDB。只有在确认目标可以替换时才使用：

```powershell
--overwrite
```

字段自动识别存在歧义时，可传入 JSON：

```powershell
--field-map '{"longitude":"经度字段","latitude":"纬度字段","datetime":"时间字段"}'
```

## ArcPy 环境诊断

```powershell
& 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat' `
  '.\scripts\diagnose_arcpy.py'
```

详细处理方式见 [ArcPy Native runtime recovery](references/arcpy-runtime.md)。

## kind 分类显示

把 [symbolize_kind_current_map.py](scripts/symbolize_kind_current_map.py) 的内容复制到 ArcGIS Pro Python 窗口，可将当前地图中所有含 `kind` 字段的点图层显示为：

- `start`：红色
- `end`：绿色
- `stop`：橙色
- `way` / `normal`：灰色

脚本按字段而不是图层名称识别目标，单个无关或不受支持的图层不会中止后续图层。

## 开发与测试

普通 Python 环境：

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s scripts -p 'test_*.py' -v
```

普通环境中的 ArcPy 集成测试会跳过。真实 GDB 集成测试需要在 ArcGIS Pro Python 环境中运行。

示例输入位于 [examples/sample_trip_chain.csv](examples/sample_trip_chain.csv)，仅包含合成坐标和虚构轨迹编号。

## 数据与隐私

不要把真实个人轨迹、生成的 GDB、ArcGIS 工程文件、访问令牌或许可文件提交到仓库。`.gitignore` 已排除常见的 GIS 输出与 Python 缓存。

## 许可证

本项目代码采用 [MIT License](LICENSE)。ArcGIS Pro 和 ArcPy 由 Esri 单独授权。

## English summary

`chain-handle` is a Codex Skill for validating trip-chain CSV files and generating audited WGS 84 file geodatabases with ArcPy. It splits trajectories by identifier and local date, collapses exact stationary runs, calculates geodesic distance and speed, classifies points as `start`, `end`, `stop`, or `way`, and provides optional ArcGIS Pro symbology code. ArcGIS Pro and a valid ArcPy license are required for GDB creation.

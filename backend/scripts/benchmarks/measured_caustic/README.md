# 用公开实拍光斑验证焦散拟合

从 Scott Prahl 公开的两次相机扫描重新提取固定 X、Y 轴光斑，用独立 SciPy 拟合与 BeamWeft 的注册计算逐轴比较。基准同时检验未参与拟合的位置、低于 1 的等效 M² 提示和采样不足时的拒算。两组扫描来自同一公开来源。

## 使用

在仓库根目录运行。完整比对使用已安装 `backend/requirements.txt` 的 Python 环境。只做独立复算需要 NumPy、SciPy、Pillow。首次下载另需命令行 `curl`，本地缓存可供离线复算。以下示例使用 Windows 仓库虚拟环境，其他平台可将解释器替换为该环境的 `python`。

```powershell
backend/.venv/Scripts/python.exe backend/scripts/benchmarks/measured_caustic/verify.py
backend/.venv/Scripts/python.exe backend/scripts/benchmarks/measured_caustic/verify.py --offline --report "$env:TEMP/beamweft-caustic-check.json"
```

默认缓存位于系统临时目录下的 `beamweft-measured-caustic`，可通过 `--cache <目录>` 指定长期保存位置。两个命令使用同一缓存时，第二个命令会完全离线运行。每次读取都核验 22 张原件的 Git blob ID 与 SHA-256。首次下载约 23.4 MB。缓存缺件、校验失败、数值越过公差或产品放行过少测站时，命令以非零状态退出。[原件清单](manifest.json)

仅生成含两个方向全直径和采集标识的 CSV，以及独立参考结果，可运行以下命令。附加 `--series k` 或 `--series sb` 可只研究其中一组。CSV 会写入缓存目录。

```powershell
backend/.venv/Scripts/python.exe backend/scripts/benchmarks/measured_caustic/reference.py --offline --report "$env:TEMP/beamweft-caustic-reference.json"
```

## 原件、坐标与留出

使用固定提交中的 K 系列 PNG 和 SB 系列 PGM。作者记录 K 来自学生搭建的氦氖激光器。按源笔记取像元间距 3.75 µm、真空波长 632.8 nm，按空气折射率 1 换算 M²。SB 的 12 位读数在 16 位容器中左对齐，按作者说明右移 4 位并裁去顶部 200 行。两列始终对应相机固定横轴与纵轴，光斑旋转或主轴交叉时也保留方向。[K 实验、像元与背景](https://github.com/scottprahl/laserbeamsize/blob/98613f8db55a4a52006927bc3df5d424b0ee6f33/docs/03-Background.ipynb) · [波长、SB 原件及曝光处理](https://github.com/scottprahl/laserbeamsize/blob/98613f8db55a4a52006927bc3df5d424b0ee6f33/docs/15-M2-Problems.ipynb) · [数据仓库许可证](https://github.com/scottprahl/laserbeamsize/blob/98613f8db55a4a52006927bc3df5d424b0ee6f33/LICENSE.txt)

独立脚本从四角估计背景，扣除背景后将低于三倍背景标准差的残值置零，在围绕质心的迭代矩形中计算每轴的 D4σ 全直径。每站一帧。将不同 z 从小到大排列，留出零起始序号满足 `i % 4 == 1` 的整站。K 的 16 站留出 300、490、550、650 mm，SB 的 6 站留出 251、292 mm。SB 的 292 mm 位于训练范围之外。

参考拟合最小化训练站的全直径残差。方程为 `D(z) = hypot(D₀, Θ(z−z₀))`，Θ 为全发散角，`M² = π D₀ Θ / (4 λ)`。产品通过临时工程 API 上传同一 CSV，第 1 列提供 z，第 2 或 3 列提供相机轴全直径。绑定列时先乘 0.5，再由 µm 换到 mm，送入注册研究 `fit_beam_caustic`。逐帧文件名作为采集标识。临时工程及其上传副本在验证结束后释放。

## 独立参考值

| 扫描与相机轴 | 训练 / 留出站 | 束腰全直径 µm | 束腰位置 mm | 全发散角 mrad | 等效 M² | 留出半径 RMSE µm | 产品预期 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| K X | 12 / 4 | 499.327 | 526.008 | 1.75944 | 1.090392 | 7.11962 | 返回拟合与留出预测 |
| K Y | 12 / 4 | 404.282 | 501.945 | 1.95482 | 0.980879 | 8.51251 | 返回原值并提示核查测量 |
| SB X | 4 / 2 | 105.510 | 270.015 | 10.26234 | 1.343895 | 24.83523 | 说明训练至少需要 5 站 |
| SB Y | 4 / 2 | 108.791 | 269.062 | 13.57349 | 1.832762 | 16.11905 | 说明训练至少需要 5 站 |

SB 的数字由独立脚本给出，用于说明即使优化器收敛，外推误差仍可能很大。产品必须对两轴均报告训练不足并保持计算历史为空。K 的比对使用绝对公差，包括束腰位置 0.0001 mm、束腰半径与逐站预测半径 0.0000001 mm、发散半角 0.00000001 rad、M² 0.00001。报告保留独立值、产品值、公差以及每个留出点的观测与预测。

这些公差检验程序的数值一致性。K 有 15/16 帧出现达到 8 位满量程的像素，200 mm 帧有 1,453 个。SB 有一帧出现 7 个满量程像素。背景截断和饱和均影响二阶矩；对未给出的像元校准公差、位移不确定度、曝光线性和站内重复性，本基准无法估计其贡献。K Y 的低 M² 应提示检查宽度口径、波长及标定。将此结果用于仪器验收或 ISO 符合性判断，还需独立的标定与不确定度证据。

## 改变入射光场的独立检验

另一间实验室公开了更换空间光调制器输入后的相机帧。按训练帧冻结坐标配准与比较区域后，工作台对留出设置的图样相关系数为 0.9407，固定圆内的变化预测误差比沿用旧照片降低 50.9%。实验支持同一台架的相对光强形状检验，绝对耦合效率还需独立标定入射与接收功率。[实验条件、对照方法与复算](../fourier_plane/README.md)

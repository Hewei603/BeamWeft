# BeamWeft 架构与工作流说明

> 本文描述 BeamWeft 的整体架构、各层职责、核心工作流,并标注每个环节对应的源码文件,便于对照阅读代码。
> 产品目标：**Agentic 的全链路新型激光仿真软件**。Agent 理解目标、维护实验工程并组合工具；确定性模型给出数值，结果保留条件、来源和能力边界。持续开发约定见根目录 [AGENTS.md](../AGENTS.md)。

## 当前主工作流

`ExperimentWorkbench` 是统一入口。目标、资料、当前光路、分析结果和执行记录围绕同一案例组织，元件编辑只是工程对象操作。主要契约如下：

| 对象／职责 | 实现 |
|---|---|
| 用户选择的工程、元件、库存、附件与结果引用 | `workbench/context.py` 的 `ContextRefs`；提交时校验归属，历史引用不自动恢复当前硬件 |
| 迭代 Agent、实际工具记录及结论 | `workbench/service.py`、`tools.py`、`contracts.py`；API/Codex exec 共用同一调度器 |
| 有身份和约束的通用工程对象、版本与保存复算 | `workbench/projects.py`；单程、驻波和共面环形模型共用 `workbench/optics.py` |
| 库存新增／修订与不确定信息 | `inventory/service.py`；前端和 Agent 共用，库存修订不改写工程历史 |
| 库存到计算模型的物理适配 | `inventory/crystals.py` 与 `projects.py`；参数来自记录而非案例常数，尚无适配器的对象明确要求完整模型 |
| 参数扫描和模型来源 | `workbench/analyses.py`；前端与 Agent 同一持久化路径，保存工程版本或明确标为探索模型 |
| 可独立调用的科学计算 | 扩展接口 `app/study_api.py` 描述输入、求解函数、模型范围和输出视图。工作台、Agent 与 HTTP 客户端共用注册和执行路径 |
| 统一结果展示 | `OpticalProject`、`OpticalFieldView`、`OpticalScanView`；按所选模型展示曲线、截面、位置与假设 |

常规实验变化通过数据和工具调用完成。新增物理能力通过模型与工具契约扩展，不按用户的某句话新增一个实验模块。当前仍以近轴 ABCD 等已列明求解器为能力边界，未实现完整增益、热场、调 Q 或全链路功率优化。

下文保留已有子系统与兼容路径的详细索引；旧模块接口不代表主工作流。

### 将计算网格与仪器原件配准

相干传播、端口探测和相机原件处理可以发布同一种横向光强图。每张图声明两组以毫米计的单元边界、每格积分信号的单位、坐标轴和取得这些数值的依据。仿真的面密度先乘物方单元面积，相机的同一分箱直接累加净读数。这也适用于尺寸不等的边缘分箱。已保存的相干出射场可以在自己的参考面重新取样，每边最多 512 格。工程随动预览只传绘制屏幕所需的密度图，保存时才构造交换网格。

比较使用实验者给出的变换，将观测坐标先按需要镜像，再转动和平移到参考平面。正交网格按横纵区间的交长积分。任意转角使用凸多边形裁切，按每个交叠区的面积累加参考信号。观测格须完整落在参考图内才能取得可比的积分，共有窗口以外的像素保留为空。参考图的分箱决定可解析的最细结构。这种积分量与面密度的区分，以及按像素交叠面积重投影的原理，也可在天文图像处理文献中核对。[Montage 重投影原理](https://irsa.ipac.caltech.edu/Montage/docs/algorithms.html) · [Astropy Reproject 关于通量与面亮度的说明](https://docs.astropy.org/projects/reproject/en/latest/explanation/algorithms.html)

同窗归一化的带符号残差用于检验形状，质心位移反映配准后仍存在的偏移。用户提供独立标定系数和依据后，可同时比较共有区域的积分幅值。当前对单元内响应采用均匀近似，比较结果中保留各图的数值收敛、背景和饱和警告。

### 换一个实验需要改哪里

| 实验变化 | 目前由谁完成 | 实际边界 |
|---|---|---|
| 更换一个由现有模型描述的器件 | 实验者调整工程数据。无焦组件可以提供两个方向的实测或设计 ABCD 矩阵，多个光学面也可以保存成一个实物组合。 | 矩阵用于一阶传播。它没有给出衍射图、孔径损耗或真实像素强度。 |
| 增加一种独立分析或数据处理 | 扩展作者在自己的包中登记输入、计算和输出。工作台读取表单及绘图声明，Agent 从同一登记项取得工具。 | 复杂对象目前可能需要手填 JSON。一个工具能够被发现，并不能证明实验者容易上手。 |
| 把新的器件模型放进光路 | 作者为包中的计算声明 `Study.element_law`。安装后，手工画布和 Agent 从同一元件目录取得参数、模型及端口。使用者可以把同一模型的多份实例放在不同工程，分别调参。 | 已有两种交接约定，分别支持独立 T/S 近轴传递与单色被动多端口散射。 |
| 引入现有求解器无法表达的状态 | 为新状态定义坐标、归一化、可观测量和传播定律，并用独立结果验证与已有求解器的交接。 | 例如任意二维复振幅经过硬边孔径后出现的衍射，单凭两个光束半径或各端口的总功率无法重建。 |

Agent 的公共说明负责工程版本、实验依据和任务决策。实验的拓扑、参数口径和求解范围由相应的模型与求解器声明。近轴计算返回的 `intensity_model` 告诉读图者能从哪些量重建截面。理想高斯模型支持绘制归一化强度。等效 M² 模型只发布宽度时，图中两条标尺分别表示切向和弧矢尺寸。具有相同二阶矩的光斑可以有不同的像素分布。[光束宽度定义与二阶矩](https://laserbeamsize.readthedocs.io/en/latest/01-Definitions.html)

### 怎样从实验目的找到模型

如果起点是手里的工程，点击光路图左上角“使用已存工程 #… 的数据”。选中完整光路或其中已发布的量（例如元件长度、波长或某个端口的功率），工作台就会查找能接收它的计算。单程、支路和端口网络共用这一入口。正在编辑草稿时，链接仍指向标签上写出的已存版本。研究者可保留草稿做基线比较，也可以先保存新版本，再引用修改后的参数。

打开“分析记录 → 运行独立计算”，可以输入物理量、计算方法、标识符或提供方。目录逐页显示结果，选择一个方法后才读取它的完整参数。修改搜索词或翻页可以保留正在编辑的输入。从工程中的物理量进入时，候选方法还必须有兼容的物理量、形状与单位约定。研究者仍需核对波长、参考面和具体工况。旧工程包可以单独查看原计算结果。

Agent 和手工界面使用同一份本地目录。每轮任务开始时加载八项用于查找能力、读取工程和证据、开始试算以及结束任务的工具。Agent 可以用 `search_capabilities` 查当前安装的工程操作或科学方法，匹配的方法进入接下来可调用的工具集合。已知名称时可用 `inspect_tool` 直接取得说明和完整参数。对于参数化器件，说明还包括实际可用的版本及初始参数。近期取出的工具最多保留二十项，可以再次按名称调出。每个案例任务独立维护这个集合，安装的全部方法仍可随时检索。按需加载工具的设计也见 [Anthropic 的工具检索研究](https://www.anthropic.com/research/advanced-tool-use)。

索引使用方法名称、标题、供应方、适用条件、输入字段、输出指标及声明的物理量。扩展作者可以在 `Study.keywords` 提供术语、别名及其他语言的对应说法。按词检索支持大小写及全角归一化、中文相邻字组合和精确标识优先。它能在离线环境工作。缺少词汇对应时，搜索可能漏掉适用方法，可以换词或分页浏览。搜索排名不能作为两个物理模型等价的依据。

脚本可以向 `POST /api/cases/{id}/capabilities/search` 提交 `query`、`kind`、`limit` 和 `offset`。结果提供方法名、总数和下一页位置。`GET /api/cases/{id}/studies/catalog/{name}` 返回一个方法的完整定义，原全目录接口继续支持需要一次导出所有定义的客户端。Codex 文本传输还会将本轮工具中重复使用的参数定义列一次。原生工具接口接收所选工具各自完整的 JSON Schema。

### 物理器件与光路元素

实验者可以直接编辑参数、提供自定义矩阵，或把一组光学面存进元件库。实验室若已有随波长或设置变化的器件模型，可以独立安装它。作者在 `beamweft.studies.v1` 包入口登记计算，并声明其返回值如何参加传播。工程编辑器和 Agent 共用这份说明，主程序无需按型号分支。所有参数的单位、轴向、适用范围及版本都由作者声明。安装示例的命令为 `backend\.venv\Scripts\python.exe -m pip install ./examples/device-extension`，在项目根目录运行，随后重启后端。[示例包入口](../examples/device-extension/pyproject.toml) · [模型和字段](../examples/device-extension/laboratory_optics.py)

| 约定 | 求解器接收什么 | 用户可以从结果知道什么 |
|---|---|---|
| `ray_matrix` | 工作台注入所在光路的真空波长（nm）和可选的入口到出口距离（mm）。作者返回实数 ABCD 系数，作用于光线高度（mm）与折合倾角 nθ，T/S 可各自指定，两平面各要求 AD−BC=1。驻波腔按互易传递计算返程。 | 入口与出口的一阶光线及束参量。对于只给出整体传递矩阵的有限长度器件，图在两个参考面之间断开。若研究内部束腰，须提供具有内部传播信息的模型。 |
| `scattering` | 端口标识与顺序由模型固定。作者返回功率归一化的复矩阵，每行对应一个出射模式，每列一个入射模式，复数用实部和虚部表示。工作台按实际波长重算并检查被动性。 | 在声明的端口基底和相干条件下可以读出各端口功率。用 H/V 表示偏振时，应注明两侧采用的坐标和公共相位。几何光斑与端口模式之间的投影需要单独的空间重叠依据。[Jones 矩阵的基底和旋转](https://pypolar.readthedocs.io/en/latest/04-Jones-Matrices.html) |

工程保存模型来源、版本、具体参数和计算所用的数值传递矩阵。接收者能查看工程包中的历史读数。如果环境里缺少那个版本，新算请求会指出所需实现。无法求解的独立端口组保留自己的诊断，其他有充分输入的端口组继续计算。顺序光路中的元件输入彼此依赖，中间传递未知时不能据此推算下游。改变模型假设时，扩展作者须发布新的计算版本。

工程的 `assemblies` 是实物清单。每条记录有独立标识、名称、所属光路及有序的 `element_uids`。一件实物可以跨越多个连续传播元素，包含内部空气；未列入组合的元素各自独立。同一个库存编号可以供给多个安装位置。画布平移、对象浏览、库存计数、库存替换和模型库插入都使用这些边界。光学计算继续逐个传播元素，组装和解除操作本身不会改变求解结果。沿轴移动需要使用相邻的自由间距。关于如何界定共同运动的一组光学面，可参阅 [Ansys 的镜组整体运动示例](https://optics.ansys.com/hc/en-us/articles/43071115642259-How-can-I-pivot-a-lens-group-around-any-points-in-space)。

新客户端用空数组声明所有元件独立。缺少该字段的旧工程从冻结的分段标识恢复边界，其余沿用旧版相邻晶体规则并标记 `legacy_inferred`。旧客户端编辑已有工程且省略该字段时，服务端沿用所编辑版本的边界。若布局与已有成员关系冲突，返回校验反馈。工程版本和试算均保存实际采用的清单。模型库跨工程放置时，只复制所选范围中完整的实物；每次放置都会为实物及其成员分配新的标识。

## 增加一种可供实验者和 Agent 使用的计算

v1 接口适合输入明确、结果可复算的确定性求解器。扩展作者定义一个继承 `app.study_api.StrictInput` 的输入模型，编写接收该模型并返回 JSON 对象的函数，再导出一个 `Study`。字段标题应包含物理量和单位。选项、默认值和合法区间由输入模型声明。求解器作者负责适用条件与物理验证。

把定义登记在扩展包自己的 `pyproject.toml` 中。安装到后端的 Python 环境并重启服务后，手工界面会读取字段生成表单，Agent 获得相同的工具名、参数模式和校验，脚本可以调用同一 HTTP 接口。下例对应仓库里的 [可独立安装的示例包](../examples/study-extension/pyproject.toml)。发现机制使用 Python 的包元数据。[PyPA 插件发现说明](https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/)

```toml
[project.entry-points."beamweft.studies.v1"]
linear_analyzer_power = "bench_polarimetry:analyzer"
```

新扩展使用入口组 `beamweft.studies.v1`。早期安装的扩展仍可加载。若两个包占用同名方法，或两个组为同名方法指定不同的导入路径，目录会报告名称冲突。存档中的第三方提供方标识取自发行包名称和版本，内置计算使用 `BeamWeft`。

`Study` 包含稳定名称、面向用户的标题、计算版本、适用说明、输入类与求解函数。输出可以提供具名指标、表格和曲线。每个指标用点分字段路径定位值，例如 `solutions.0.lambda3_nm`。表格声明来源数组及列，曲线声明横纵轴标题、数据路径与绘制方式。例如 `planes.tangential.samples.*.z_mm` 会依次读取每个采样点的坐标。界面同时保留完整输出。适合校验格式的条件写进输入模型，涉及模型成立与否的条件在求解函数中检查。[完整计算示例](../examples/study-extension/bench_polarimetry.py)

脚本可用 `GET /api/cases/{id}/studies/catalog` 发现可用计算，将能力声明中的 `provider`、`version` 和自己的 `inputs` 提交到 `POST /api/cases/{id}/studies/run/{name}`。计算使用案例编辑权限。结果中的 `study_record.id` 可用于回读 `GET /api/cases/{id}/studies/runs/{run_id}`。读历史使用案例查看权限。相同输入和实现版本复用已存记录，算法或物理假设变化时作者应更新版本。

工程包携带运算时的输入、结果、来源版本与展示定义。在另一台电脑上，历史记录可直接打开。重新计算需要相应的已安装实现。插件属于本机安装的软件，案例附件只作为数据。插件重名或载入失败会在能力列表中说明原因，其余能力仍然可用。需要改动实物库存、提交工程版本、绑定项目状态或触发外部任务的操作继续使用各自的权限和事务契约。

### 引用一次已存计算

实验者在“分析记录 → 运行独立计算”里选择一个计算方法。只要本案例已有兼容的结果，对应输入就可以切换来源。选定记录后照常运行。新记录保存实际输入、上游记录号和输出字段。点击“输入来源”可回看上游条件。引用固定在当时的版本。Agent 的 `run_connected_study` 和脚本共用相同的解析与校验。

扩展作者在 `Study.input_ports` 登记输入字段，在 `output_ports` 登记输出路径。双方声明同一物理量标识及数据形状。标量和数列还需声明单位与换算到约定单位的正比例因子，例如光功率 `optics.power` 的约定单位是 W。仿射单位、坐标变换和插值需要专门的转换模型。正比例单位转换不适用于这些情况。光束状态的标识包括坐标约定及版本。例子中的双平面状态要求空气参考面重合、前进方向及 T/S 轴一致，复制两轴复 q、真空波长和等效 M²。

多个状态依赖同一份坐标或采集条件说明时，接收方法可声明 `Study.registration_inputs={"reference":"registration_basis","observed":"registration_basis"}`。`registration_basis` 为该方法中由实验者填写的文本输入。一项说明可以应用于两个来源，每条引用分别保存采用的文本以及文本来自哪个输入。为个别来源填写 `registration` 可以覆盖共用说明。换来源复算时，实验者重新确认两份数据如何对应，之前单独登记过的依据也会单列。

实验决定输出行数时，可以在 `Study.row_outputs` 中按表声明物理量。例如下面的扩展对每个采集通道提供峰峰值。运行后，工作台和 Agent 都可以从那次计算里选择某个通道的值，交给接受同一物理量的方法。

```python
from app.study_api import Port, RowOutputs

row_outputs = {
    "readings": RowOutputs(
        columns={"vpp": Port(quantity="electrical.peak-to-peak-voltage", label="峰峰值", unit="V", scale=1)},
        coordinates={"channel": "通道"},
    )
}
```

将上述字典作为 `Study(..., row_outputs=row_outputs)` 的参数。提供者返回的 `readings` 是一组带 `channel` 和 `vpp` 的记录。选择结果时，界面显示通道和行号，引用保存为运行编号及该次输出的路径（例如 `readings.1.vpp`）。下一次运行可以换名称、顺序和行数。已有记录始终指向原值。缺失或形状不符的单元格不会成为可用输入。默认只发布已完成计算的输出。如果求解器能够保证部分完成时返回的每一行都已独立求解，可在该表声明 `available_on_partial=True`。升级扩展版本后，旧记录仍依照当时保存的接口读取。

对于可以独立计算的固定输出，方法可声明 `output_requirements={"voltage": ("counts", "volts_per_count")}`。这里的键对应一个输出端口，元组中的名称是经过校验的输入字段。需要支持缺资料运行的字段应允许为空。缺少换算系数时，求解器省略 `voltage` 或返回空值。宿主在记录中列出缺少的字段及其含义，其他有依据的结果仍可在下一次计算中引用。数值零是已知值。

如果字段齐全，但测量质量或模型适用范围不足，求解器可以返回 `output_limits={"voltage": "仪器在该区间已饱和。"}`，并保留有助于检查的估计值。宿主将其标为 `limited`，只发布满足声明且数值形式正确的输出。每项判断、计算版本与结果一起归档。求解器给出缺少依据的数值或遗漏应有的结果时，工作台报告方法名称和版本，该次求解不生成记录。

同一张结果表中，各行也可能有不同的适用范围。可在 `RowOutputs` 中声明布尔字段匹配条件，例如 `where={"extrapolated": False}`。插值和外推都能显示在原表中，只有标记为区间内的行能用于自动连接。缺少标记的行留在原表供复核。

工程版本也可以提供物理量。用 `GET /api/cases/{id}/studies/project-outputs?revision_id=…` 或 Agent 的 `read_project_outputs` 发现该版可引用的量。把返回的键原样放入 `{"revision_id": 12, "port": "…"}`，就能替换任意一个兼容的手填输入。当前来源包括路径波长、元件长度、单程出射功率和已归因的体吸收功率。后两项还携带对应支路、波长、能量参数和上游待补信息。工程只有相对功率时也可以保存，绝对瓦数会等到具备依据时再提供。

工程与独立求解器都通过 `PublishedPort` 发布每个值可无损使用的形状 `connects_as`。一个数可以给区间输入提供相同的上下限。反向传递需要上下限严格相等。两个样本即使取值相同也仍是数列。接收端根据冻结值再次检查形状、物理含义和单位，随后才做换算。单值继承上游给定的精度与假设，仪器不确定度需要另行建模。扩展作者只需声明原生形状。项目不用为不同的接收器复制结果。

### 用新来源复算既有分析

在一条已存计算下展开“计算依赖与换来源复算”，可以查看它实际引用的上游步骤和工程版本。选择另一个已存工程，或用另一笔已存计算替换上游结果，工作台会先列出受影响的步骤。执行后，每一步仍使用原来记录的求解器和版本。无关分支沿用旧结果，原件列和手填条件保留在原有输入位置。带有参考系或跨工况说明的输入会展示原说明，供实验者针对这次组合重新核对。出现中断时，成功的独立步骤已有各自的记录号，可用于恢复。原结果和新结果可以按指标对照；曲线使用各自归档的横坐标。这个交互借鉴了从已保存的分析历史恢复工作流的做法。[Galaxy 的历史工作流](https://galaxyproject.org/learn/advanced-workflow/extract/)

手工操作和 Agent 的 `replay_study` 都使用 `workbench/study_replay.py`。脚本可向 `POST /api/cases/{id}/studies/replay` 提交 `{"run_id": 40}` 查阅依赖，在同一请求中加入 `source_id` 与 `replacement_id` 查看预览，设置 `execute: true` 后保存新计算。涉及工况的说明在 `registrations` 中按原计算编号及输入字段给出。检查或重试使用同一组固定的记录号。

扩展若允许跨运行引用表中某一行，可在 `RowOutputs.identity_fields` 声明稳定的物理坐标，例如 `("component", "port")`。输入最初指向已存记录中的行，复算时会用这些字段在前后两份记录中寻找唯一对应项。缺失、重复或已失去物理有效性的项会给出原因。作者可以只提供行的显示名称，用户仍可在该次冻结记录内正常引用。

### 当前组合能力的边界

在单向模型中，已知入射场到某个定点的传播只需要到该面为止的处方。OpticStudio 的物理光学传播同样指定起止面。谐振腔本征模则满足往返后的自洽条件，未知的后镜可能改变上游的腔模。这两类问题决定了 BeamWeft 如何界定局部计算的有效范围。[Ansys 的起止面与物理光学传播示例](https://optics.ansys.com/hc/en-us/articles/42661948624019-How-to-model-laser-beam-propagation-in-OpticStudio-Part-3-Using-Physical-Optics-Propagation-to-model-Gaussian-beams) · [reZonator 的谐振腔计算说明](https://rezonator.orion-project.org/help/matrix_optics.html)

完整工程可以发布具名光束从自身源面到某个观察面的顺序处方。工作台预览和 Agent 连接计算共用这一投影。它保留支路波长下的元件顺序、折射率来源、入口孔径，以及输出面在原工程中的位置。片段内的坐标从自身光源起算。版本比较分别核对实际输入、工程坐标和参数归属，所以补齐无关下游元件后，可以看出旧计算仍使用了相同的局部模型。改变定位方式或将折射率改由另一处提供时，数值即使恰好相等，记录也会指出两者的区别。

工程同时支持顺序光路与端口连接图。主路与入射支路描述近轴传播、介质段、光斑和腔模。可选的 `network` 描述器件的具名端口、双向连接和外部入射。独立端口工程只需在 `system` 给出波长和空的单程元件列表。顺序模型也可将某一参考面上的精确出射功率送入网络；这一接口止于前向功率，回波仍在网络的驱动端口读出。上游提供范围或波长不符合时，关联子网列出待补输入。每条顺序路径可驱动一次，分流由网络中的物理元件承担。

端口模型以功率归一化复振幅描述同一偏振和横模。每个器件给出 `b = S a`，矩阵行代表出射、列代表入射。每条双向连接给出含衰减与光程相位的传递系数。对每个相干组分别求解 `(I − P S) a = d`，再将各组在探测端口的功率相加。自定义矩阵的最大奇异值应不大于 1（数值容差 10⁻¹²），给出标定波长后会复核其适用性。开口采用匹配端接。每个连通子网独立检查稳态方程的一范数条件数和外部功率收支，其余子网可在局部信息不足时给出结果。闭环内部的双向循环功率与外部出射功率分别报告。[Finesse 模型图](https://finesse.ifosim.org/docs/latest/getting_started/model_graph.html) · [Ansys 被动散射矩阵说明](https://optics.ansys.com/hc/en-us/articles/360059772393-S-parameter-passive-workflow-guide)

图面排布坐标仅用于显示。连接长度、折射率、附加相位、散射矩阵和光源相干关系决定计算结果。已存端口出射可通过 `read_project_outputs` 连接后续方法，引用同时保留版本、元件与端口。独立脚本和 Agent 也可用同一网络契约调用 `solve_port_network`。目前模型假设端口空间模式完全匹配。自由空间的倾斜、偏振旋转、部分相干与散射角分布，以及回波对上游激光器的影响，仍需相应的传播和反馈模型。

顺序路径保留为简单实验的编辑方式。OpticStudio 的混合模式可以在已知顺序路径的局部使用非顺序追迹。BeamWeft 当前的顺序光路与端口网络组合保留了这一层次区别，但端口连接由实验者明确给出，物体几何还不能自动发现新的光路。[Ansys 混合顺序与非顺序模型](https://optics.ansys.com/hc/en-us/articles/42661747249043-How-to-model-a-mixed-sequential-non-sequential-system)

检验新能力时可以改变一项原本被当作前提的条件。去掉谐振腔，检查单程传播和元件操作；去掉整个光路，检查原件和计算插件能否独立组成分析；撤掉已知的吸收系数，检查表现是否退回有依据的量。前两项检验重用，最后一项检验科学边界。界面、脚本和 Agent 均应能回到同一版来源。

```json
{
  "version": "2", "provider": "bench-polarimetry-example 0.1.0",
  "inputs": {"angle_deg": 45},
  "connections": {"incident_w": {"run_id": 1, "port": "output_w"}}
}
```

这个请求提交给偏振扩展的运行接口。记录号由实际案例决定。接收者使用上游冻结的能力声明和数值，卸载来源插件后仍然可以引用已有结果。首次加入输出契约时要提升计算版本，旧记录继续按原声明读取。在传递功率时，用户仍需核对波长、偏振态、参考位置和本次工况是否符合接收模型；标量本身不能证明这些条件。共面近轴光束的物理约定见 [reZonator 文档](https://rezonator.orion-project.org/help/gauss.html)。

---

### 把原件列作为数列输入

数列端口可以声明额外输入单位及其换算到约定单位的比例。通过同一个运行入口提交 `connections`，每个字段可引用先前运行的输出，也可引用案例原件的具体列。文件引用包含 `inspect_measurement_table` 返回的明确选区（工作表、分隔符、小数标记、行区间与横轴条件）、源文件 SHA-256、从 1 开始的列号和数据单位。若有线性校准，保存增益、偏移及依据，并在单位换算前应用。已注册的第三方计算也能使用这些输入。

同一原件、行区间及横轴条件的多个输入在一次遍历中读取。横轴过滤保留采集次序，反向扫描和重复位置各自保留。同组任一所用列缺失时，默认反馈原始行列；只有明确选择排除时才一起跳过该行。未参与计算的预览纵轴不决定行的去留。求解器可以用 `paired_inputs` 声明需要逐点配对的输入字段，混用不同文件、计算结果或手录数据时，文件引用需说明测点如何对应。记录中冻结解析后的数组、每个来源的纳入与排除行号和原件校验值。原件绑定不使用图表抽样数据。

若结果表的某一行来自输入序列中的特定测点，方法可声明 `table_input_rows={"radius_mm": "input_index"}`。该行的 `input_index` 是解析后的半径序列中从 1 开始的序号。来源回执中的 `row_numbers` 将该序号映射到选择的工作表或文本记录，即使原表带标题、缺行或被筛选，实验者仍能回查准确的原始记录。手填序列保留输入序号。多个输入来自不同原件时，分别使用各自的来源回执。

普通字符串序列（例如样本编号）也可从原表取列。研究目录在 `text_column_inputs` 中公布从输入模型读取的标签与长度限制。文本沿用 `connections` 的选区、列号和文件校验值，保留原字串。物理数列另外登记单位与校准。在同一选区配对的列共用实际行号和缺失行处理。跨原件配对时保留对应依据；手填标签仍按照输入顺序对应读数。

Agent 用 `attachment_id` 搜索时，目录对候选表格列出 `table_column_inputs`，标明可从列填入的数值或文本字段。能否分析这份数据，仍取决于实际的列、单位和选区。查询显式指定 `original_files=true` 时只查能直接读取整份原件的方法。

需要提供高分辨率数值平面的研究可以声明 `Study.native_grid` 并在计算回执中存入 `native_grid_sha256`。原生回调从已保存的全部参数和已校验原件产生毫米坐标边界、单元积分、信号单位。指纹涵盖形状、规范化为小端 float64 的两个坐标轴和所有单元，以及单位。下载、原生视区和 Agent 复核使用同一校验。视区按单元面积聚合密度，共用完整网格的色阶，外界区域单独标示无数据。结果若声明 `RasterView.native_density`，前端可以把这幅图与原生密度关联。其他栅格可独立展示相位或像素读数。工程包保留输入、方法版本、原件与已保存的结果，单独导出的 NPZ 可直接在外部程序打开。

### 跨截面传递光束与光场

在介质截面交接近轴光束时，工程记录真空波长和该截面所在侧的折射率。用户看到当地的曲率半径 R 和物理指向角 θ。传播矩阵内部使用位置与 nθ，复光束参数按 q/n 传播。均匀介质中实际传播 d 对应矩阵的 B = d/n。这样在垂轴平界面上，即使两侧的物理指向角和曲率半径不同，横向束心和宽度也保持连续。已知部分轴向信息时，历史记录仍可保存未解条件。共轴近轴介质模型的边界与相关矩阵推导见 [Massey 与 Siegman 的高斯光束界面研究](https://opg.optica.org/ao/abstract.cfm?uri=ao-8-5-975)。

有限孔径会改变出射场。相干传播 v2 归档起始复高斯包络及已求值的传播段、孔径和二次相位面。接收端可以按新网格重建横向相位，在每次计算中重新评估采样误差。模型假设单色、相干、标量、近轴，在展开的 T/S 坐标上对齐；各段省略的公共载波相位不能用来确定独立光源间的绝对相位。本项目用圆孔通过率解析解及独立径向积分检验这条链路。关于菲涅耳传播采样误差的研究见 [Voelz 与 Roggemann](https://opg.optica.org/ao/abstract.cfm?uri=ao-48-32-6132)。

### 计算版本与引用读取

计算方法升级后可以通过 `Study.accepts_inputs_from` 明确接受同名历史方法的输入。每项声明使用精确的提供者标识和方法版本。声明表示作者已核对旧字段的物理含义、单位与参考面。工作台和 Agent 在同一工程中传入 `reuse_inputs_from`。后端重新读取原输入与冻结引用，校验新模型解析出的每个旧字段，随后将新结果和来源 `input_origin` 一同归档。未声明兼容、原件校验失败或输入值发生漂移时，用户可以查看旧结果和失败原因。

每次归档的研究计算都保存 `solver_receipt`，其中包括方法版本、实际入口与输入输出契约的指纹、BeamWeft 构建、数值运行环境，以及输入中使用的器件模型。读取历史结果时，`installed_solver_receipt` 描述本机当前能找到的同名方法。早期工程缺失的环境信息保持未知。版本号相同的两个实现可以产生不同结果，使用旧输入发起计算时会按当前实现核对。调用返回的 `execution` 表明这次请求实际计算了结果（`computed`），或取回了已有记录（`reused`）。参数搜索中的每个成功运行都关联自己的环境指纹。

全分辨率下载另行校验坐标、全部单元值和单位。原方法或明确接受其输入的后继版本生成相同网格时，导出 NPZ 的 `provenance_json` 同时记录原计算环境和本次复核环境。若后继版本引入了额外的默认输入，`verification_inputs` 给出实际用于复核的完整参数。单独保存该 NPZ 后，可直接在其他数值软件中读取网格。

读取单一已发布结果可用 `GET /api/cases/{id}/studies/runs/{run_id}/output?port=…`。接口按冻结记录校验已发布的物理量和数据形状，可在原扩展已卸载时使用。含大型栅格的计算只向浏览器发送所选结果；完整记录仍可独立读取。

## 1. 系统总览

BeamWeft 是面向激光实验室的本地优先光学仿真工作台。实验者可以手工编辑光路、运行计算和对照测量，也可以在同一工程里把这些操作交给 Agent。

- **实验 Case 管理**:实验目标、参数、症状、测量、附件、生成产物,全部持久化可审计。
- **两级 RAG 知识库**:全局实验室文档(SOP/目录)+ Case 级证据,检索带引用与置信度。
- **Agent 工作流**:中文/英文自然语言 → 意图路由 → 计划 → 工具调用 → 产物入库(全程留痕)。
- **确定性物理内核**。工程编辑器和 Agent 共用谐振腔 ABCD、高斯光束传播、薄膜 TMM 和相位匹配等计算工具。
- **结构化元件库(L0)+ 逐参数评估器(L1)**:实验室真实库存 xlsx → 类型化数据 → 可裁决的选型判定。

```
前端 React ──► FastAPI 路由层 ──► 权限/ACL ──► 业务层
                                        │
        ┌───────────────┬───────────────┼───────────────┬──────────────┐
        ▼               ▼               ▼               ▼              ▼
   Agent 编排      RAG 知识层      物理内核(纯)     库存 L0/L1      LLM Provider
  (计划/工具/留痕) (切块/嵌入/检索)  (ABCD/TMM/相位匹配) (解析/导入/判定)  (mock/openai/anthropic)
        └───────────────┴───────┬───────┴───────────────┘
                                ▼
                    SQLAlchemy 模型 + Alembic 迁移 (SQLite/PostgreSQL)
```

---

## 2. 目录总图(按层)

| 层 | 目录/文件 | 职责 |
|---|---|---|
| 入口 | `backend/app/main.py` | FastAPI 应用、路由挂载、CORS、request-id 中间件 |
| 配置 | `backend/app/config.py` | 全部环境变量(provider、检索后端、阈值、ACL 开关) |
| 数据库 | `backend/app/database.py` | engine/session,`get_db()` 异常回滚 |
| API 路由 | `backend/app/api/` | 每个文件一个领域(见 §3) |
| 权限 | `backend/app/auth/` | principal 解析 + 项目级 ACL |
| Agent | `backend/app/agent/` | 意图路由、计划、编排、工具、上下文、记忆 |
| RAG | `backend/app/knowledge/` | 提取→切块→嵌入→向量库→检索→重排 |
| 物理内核 | `backend/app/physics/` | 纯 numpy 确定性计算(见 §5) |
| 库存 | `backend/app/inventory/` | L0 解析/导入 + L1 评估(见 §6) |
| Provider | `backend/app/providers/` | LLM 抽象:mock / openai / anthropic |
| 模型 | `backend/app/models/` | SQLAlchemy ORM(见 §8) |
| 迁移 | `backend/alembic/versions/` | 线性迁移链 0001→0010 |
| 观测 | `backend/app/observability/` | 审计日志、token 用量 |
| 评测 | `backend/app/evals/` | RAG 检索评测指标与验收门 |
| 测试 | `backend/tests/` | 300+ 用例(见 §9) |
| 演示 | `backend/scripts/demo_*.py` | 端到端可复现演示(见 §10) |
| 前端 | `frontend/src/` | React 页面 + API 客户端 |

---

## 3. API 路由层(`backend/app/api/`)

挂载点见 `backend/app/main.py`(`include_router` 段)。

| 前缀 | 文件 | 职责 |
|---|---|---|
| `/api/cases` | `api/cases.py` | Case CRUD、打包导出(bundle) |
| `/api/cases`(模块) | `api/case_modules.py` | Case 模块:创建/上传文件/**运行**(稳定性、光谱、光斑、元件清单、**cavity_design / phase_match / coating_tmm / component_match / power_curve**);运行分发在 `run_case_module` 的 if/elif 链。测量数据(功率曲线 CSV、光谱数据与示波器截图、光斑图)以 `CaseModuleFile` 挂在对应模块上 |
| `/api/cases`(生成) | `api/generation.py` | LLM 生成:计划/排故/报告(带 RAG 增强)。**实验计划先跑确定性物理内核**(`physics/case_context.py`),把算出的腔长/束腰/摆位/相位匹配角作为权威数值注入提示词 |
| `/api/attachments` | `api/attachments.py` | Case 附件上传/下载(带 ACL) |
| `/api/knowledge` | `api/knowledge.py` | 全局知识源上传、治理状态、`/search` 检索(带 ACL 过滤) |
| `/api/agent` | `api/agent.py` | **聊天入口**(`/chat`,含 `_route_mode` 意图路由)、任务(`/tasks`)、会话/记忆 |
| `/api/inventory` | `api/inventory.py` | **元件库**:`/import`(xlsx 导入)、`/items`(类型化过滤)、`/match`(L1 匹配) |
| `/api/literature` | `api/literature.py` | **文献检索**独立端点(无案例快查);案例模块与 Agent 路径共用同一 `literature/service.py` |
| `/api/collaboration` | `api/collaboration.py` | 用户/项目/组/成员管理(admin) |
| `/api/versioning` | `api/versioning.py` | Prompt/Workflow 版本管理 |
| `/api/evals` | `api/evals.py` | RAG 检索评测运行(reviewer 权限 + 租户过滤) |
| `/api/admin` | `api/admin.py` | 审计日志、用量查询(admin) |

---

## 4. 权限与多租户(`backend/app/auth/`)

- `auth/security.py` — `get_current_principal`:身份解析。**权限以数据库 `User.role` 为准**
  (`X-User-Id` 对应真实用户时,请求头的 role 不生效,防伪造提权);未注册 id 走演示引导路径;
  `REQUIRE_AUTH=true` 时未知/停用用户直接拒绝。
- `auth/acl.py` — 项目级 ACL:`can_view/edit/delete_case`、`accessible_case_ids`(检索过滤)、
  `can_manage_knowledge`(reviewer/admin 门)。
- 检索侧 ACL:`api/knowledge.py::search` 与 `api/evals.py` 都将 `accessible_case_ids` 传入
  `knowledge/retrieval.py`,防止跨租户内容泄漏。

---

## 5. 物理内核(`backend/app/physics/`)——确定性计算层

**纯 numpy + 标准库,不依赖 web/DB/LLM,离线确定性,可独立复用。** 全部公式为教科书清洁室实现
(Kogelnik & Li、Siegman、Born & Wolf、Macleod、Boyd/Dmitriev);ReZonator 2 源码仅作仓库外参考
oracle(GPL,已 gitignore,绝不拷贝)。

| 文件 | 内容 | 验证方式 |
|---|---|---|
| `physics/materials.py` + `physics/data/materials.json` | 28 种材料色散(Sellmeier/Cauchy),每条带出处+置信度+适用波段 | 25 个文献折射率基准点(`tests/test_physics_materials.py`) |
| `physics/tmm.py` | 薄膜特征矩阵求解 R/T/A(s/p、任意入射角、复折射率吸收) | 解析解(Fresnel、四分之一波 AR、(HL)ᴺ 闭式)+ **与 Byrnes `tmm` 库 200 随机膜系对拍至机器精度**(`tests/test_physics_tmm.py`、`tests/test_physics_tmm_oracle.py`) |
| `physics/archetypes.py` | 标称镀膜的"诚实原型":QWOT 阻带区间、对比度括号、AOI 蓝移、s/p 实测分裂 | `tests/test_physics_coating_tool.py` |
| `physics/coating_tool.py` | 镀膜评估统一入口 `evaluate_coating`:精确膜系 / 厂商曲线 / 标称原型三模式 | 同上 + 对抗回归(`tests/test_physics_adversarial_fixes.py`) |
| `physics/beam.py` | ABCD 元件矩阵、高斯 q 传播、往返稳定性、自洽本征模、各面光斑(= ReZonator 级内核);`Element.length_mm` 记录**物理**长度——矩阵本身给不出它(晶体的 B 项是折合长度、透镜 B=0),画图与包络采样都从这里取 z 轴 | 对称腔/平凹腔/共焦极限闭式解(`tests/test_physics_beam.py`) |
| `physics/nonlinear.py` | 单轴+双轴主平面相位匹配(所有偏振配对)、走离 | 文献角度:BBO SHG-I 22.8°、THG 31.3°、LBO 11.4°、BiBO 等(`tests/test_physics_nonlinear.py`) |
| `physics/toolkit.py` | **工作流适配层**(dict→dict):`run_cavity_design`(定长分析/腔长扫描→稳区→推荐几何+元件摆位,未给目标束腰时按**实测热透镜裕度**排序而非 \|m\| 代理;腔长留空默认扫 30–800mm)、`run_phase_match`(接受案例参数别名 `nonlinear_crystal`/`wavelength_nm`/`shg_pm_type`,增益晶体字典不会被误当晶体名)、`run_coating_tmm`、`beam_envelope`(沿**物理** z 逐段采样冷腔基模 w(z),供前端画摆位图;束腰按每段 `Re(q)=0` 解出,**不**把 `waist_from_q` 的折合距离当作物理位置,`w_from_q` 用**局部**折射率——两个约定错一个,晶体处就会画出不存在的台阶) | `tests/test_physics_modules.py`、`tests/test_beam_envelope.py`、`tests/test_grad_student_ux_fixes.py` |
| `physics/design.py` | **腔型设计搜索**:用户没给镜子时,遍历候选曲率×腔长,两级排序输出可搭建几何——先按束腰匹配粗筛,再对入围者**实测热透镜裕度**(在增益介质处插薄透镜、扫光焦度直至失稳),`score = 0.55×模式惩罚 + 0.45×热稳健性惩罚`;候选曲率优先取自实验室库存 | `tests/test_physics_grounded_plan.py` |
| `physics/case_context.py` | **案例→物理事实桥接**:实验计划生成前先跑内核(已知几何→分析;未知几何→设计搜索;非线性→相位匹配;标称镀膜→阻带核查),结果作为权威数值注入提示词 | 同上 |
| `physics/laser_metrics.py` | **测量分析**:功率曲线阈值+斜率效率拟合(自动线性区检测)、Findlay-Clay / Caird 跨曲线腔损耗分析 | 合成数据还原真值(`tests/test_power_curve.py`) |
| `physics/units.py` / `physics/constants.py` | 单位约定(波长 nm、光学长度 mm、膜厚 nm)与常数 | — |

> 曾由对抗式物理审查(5 个独立视角)发现并修复一个关键分支错误(`_cos_theta` 在 n-ik 约定下
> 选反,导致吸收基底 R>1);所有确认项均有回归测试锁定:`tests/test_physics_adversarial_fixes.py`。

---

## 6. 元件库 L0/L1(`backend/app/inventory/`)

把"选元件"从文本相似度升级为**可裁决的结构化判定**:

| 文件 | 层 | 内容 |
|---|---|---|
| `inventory/parser.py` | L0 | 镀膜字符串语法解析:面标记(S1/S2)、功能(HR/AR/HT/`T=x%`→PR)、阈值(`R>99.5%`)、波段(`1064-1066`)、多波长(`880&1053+1314`、`914/517`)、全角归一(NFKC)、裸波长归组;几何(`R=-100`/平镜/`D=25.4mm`/`3*3*5mm3`/`θ=159.6°`/c-cut/掺杂);**未知/损坏标记显式保留,不猜** |
| `inventory/importer.py` | L0 | xlsx 工作簿 → `InventoryItem` + 每面×每波段 `CoatingSpec` 行;前向填充组;导入报告含**待复核队列**(parse_confidence != parsed) |
| `inventory/evaluator.py` | L1 | 需求 spec → 逐参数判定档案(**不压成单一分数**):连续量硬门+裕度(曲率/口径)、**标称波长三态**(design_match / maybe_usable / off,off-design 走 `physics/archetypes` 阻带先验)、功能族规则(同面相反功能=conflict 否决)、未知→must_measure;硬伤一票否决;幸存者**支配排序**输出非支配前沿 |
| `models/inventory.py` | — | 三张表定义;迁移:`0008_structured_inventory` + `0009_inventory_loans` |
| `api/inventory.py` | — | `/import`(reviewer 门,幂等按来源文件替换)、`/items`(SQL 级波长×功能过滤)、`/match`(L1)、借还与损坏标记(见下) |

**借还与损坏(`inventory_loans` + `condition_manual`)**——两条约束决定了表结构:

- `InventoryItem.quantity` **永不递减**。导入器每次重新导入都会按工作簿重写它,任何记在这一列上的
  借出账都会被静默还原、库存凭空变多。可用量因此是**推导**出来的:
  `available = quantity − Σ(未归还借出)`,`/items` 附带 `on_loan_qty` / `available_qty`。
- 人工标注的损坏写在 `condition_manual`,而不是解析得到的 `condition`。导入器拥有后者,
  会在下次导入时覆盖——把人的判断记在那里,等于让一块崩边的镜子在重新导入后复活并重新进入匹配结果。
  优先级由 `models/inventory.py::resolve_condition()` 单点定义(人工 > 解析),
  ORM 属性与 L1 评估器共用同一条规则。
- 有未归还记录时**拒绝**重新导入同一来源文件(409)。重新导入会作废旧 item id,而 SQLite 会复用 rowid,
  未归还记录将悄悄指向另一件元件——这是无法事后发现的数据损坏,只能拒绝。
  **删除批次同样被拒**(级联会连借用账目一起抹掉,元件在下次导入后显示满额可借)——两道门一致,
  唯一出路是先归还。
- 归还不删记录(`returned_at` 置位)。"上次坏掉时是谁在用"正是实验室会问的问题,删掉就答不出来了。
- L1 评估器读的是**可用量与人工状态**:借光的、标损坏的元件不会出现在匹配结果里
  (淘汰理由显式给出:`元件标注损坏` / `可用数量不足(...已借出 N)`)。

判定档案示例字段:`hard_violations / parameters.coatings[].status / unknowns / must_measure / frontier`。
"maybe_usable" 一律注明依据(原型阻带区间)并列入实测清单——**标签不是曲线,系统不假装知道**。

---

## 6.5 文献检索层(`backend/app/literature/`)

LLM 编造参考文献是经典失效模式,所以文献条目与物理数值同一待遇:**全部来自确定性层**,
LLM 永远不产生一条文献。

| 文件 | 内容 |
|---|---|
| `literature/records.py` | `PaperRecord` 归一化记录;去重(DOI 归一化优先,无 DOI 按归一化标题,合并保留更全字段);BibTeX 与纯文本引用行 |
| `literature/providers.py` | OpenAlex(期刊元数据+被引数+倒排索引摘要重建)与 arXiv(Atom XML 预印本)适配器,均免 key;`mock` 源供测试/演示;HTTP 走模块级接缝,测试 monkeypatch 后零联网 |
| `literature/service.py` | `run_literature_search`:查询词优先级 config.query > 聊天原句剥停用词 > 案例物理参数拼词 > needs_input;线程池并发扇出、逐源异常隔离(≥1 成功=completed+失败注记,全失败=failed,绝不空结果假完成);去重、排序(relevance/citations/year)、max_results 硬顶 50 |

三个入口共用同一 service:独立页(`/api/literature/search`)、案例模块(`literature_search`,
结果经 `create_generated_content_source` 自动索引进案例 RAG——后续计划/报告可引用)、Agent
模式(聊天路由关键词在 `_route_mode` 列表**最前**,因为文献查询几乎必带物理名词,靠后必被
稳定性/腔设计等模块截胡;代价与逃生门见 `api/agent.py` 注释)。

**诚实边界**:检索到的是元数据+摘要,系统未读任何全文;结果自带中文声明,界面与打印同样显示。
中文文献(知网/万方)无公开合法 API,不做爬虫;后续以 BibTeX/RIS 手动导入补足。

---

## 7. Agent 层(`backend/app/agent/`)与核心工作流

### 7.1 组件

| 文件 | 职责 |
|---|---|
| `api/agent.py` | `/chat` 入口;`_route_mode`(意图词 `_GENERATE_INTENT` × 内容词 `_CONTENT_KEYWORDS` 双命中才路由,否则纯聊天);会话管理 |
| `agent/planner.py` | 确定性 4 步计划(模块类:读上下文→查输入→跑工具→存结果) |
| `agent/orchestrator.py` | `create_and_run_task`:建任务→建步骤→依计划执行;`MODULE_MODES` 决定走工具分支;物理模式经 `record_tool_call` **真正执行计算**;失败路径整体回滚后落干净的 failed 任务 |
| `agent/tools.py` | 工具注册表 `tool_schemas()`、审计包装 `record_tool_call`(输入/输出/延迟/状态入库)、各工具 payload(含 `run_physics_tool_payload`) |
| `agent/context.py` | 聊天上下文组装:历史+滚动摘要+记忆+两级 RAG |
| `agent/memory.py` | 会话摘要与长期记忆条目 |
| `agent/guardrails.py` | 目标风险粗评(risk_level) |

### 7.2 一次"设计谐振腔"请求的完整时序

```
用户: "帮我设计一个谐振腔,扫描腔长找到稳区,算出束腰和元件摆放位置"
  │
  ▼ api/agent.py::/chat  →  _route_mode() → "cavity_design"
  ▼ agent/orchestrator.py::create_and_run_task(mode=cavity_design)
      步骤1 get_case / list_attachments / list_generated_contents   (agent/tools.py)
      步骤2 search_knowledge → knowledge/retrieval.py 两级检索,带引用
      步骤3 compute_cavity_design                                    (record_tool_call 留痕)
             └► physics/toolkit.py::run_cavity_design
                 └► physics/beam.py: 逐腔长 ABCD 往返矩阵 → 稳定性 |m|<1
                    → 自洽本征 q → 束腰/各面光斑 → 稳区窗口
                    → 推荐 L(给了目标束腰按束腰匹配;否则按实测热透镜裕度最大)
      步骤4 save_generated_content → GeneratedContent(带 citations) → 入 RAG 索引
  ▼ 全程 AgentTask/AgentStep/AgentToolCall/RetrievalRun 落库,可回放审计
```

配置来源:**Case.parameters 打底,模块 config_json 覆盖**(orchestrator 物理分支),
所以实验参数随 Case 持久化,Agent 不需要重复要参数。

### 7.3 元件匹配请求(component_match)

```
"帮我从库存里选一块 1064 的 HR 镜" → _route_mode → "component_match"
  → orchestrator: match_components 工具
  → inventory/evaluator.py::evaluate_candidates(db, requirement)
      逐件: 硬门(曲率/口径/数量/损坏) → 镀膜三态(接 physics 阻带先验) → 判定档案
      → 硬伤淘汰 + 支配排序 → 非支配前沿 + 淘汰原因分布 + must_measure 清单
```

---

## 8. 数据模型(`backend/app/models/`)与迁移

| 文件 | 表 |
|---|---|
| `models/experiment_case.py` | `experiment_cases`(参数/症状/测量/可见性) |
| `models/case_module.py` | `case_modules` / `case_module_files` / `case_component_items` |
| `models/agent.py` | `agent_tasks` / `agent_steps` / `agent_tool_calls` / 会话 / 记忆 |
| `models/knowledge.py` | `knowledge_sources` / `knowledge_chunks` / `retrieval_runs` / `retrieval_results` |
| `models/inventory.py` | `inventory_items` / `coating_specs` / `inventory_loans` |
| `models/user.py` | 组织/用户/组/项目/成员(ACL 基础) |
| `models/generated_content.py` `models/attachment.py` `models/audit.py` `models/versioning.py` | 产物/附件/审计/版本/评测 |

迁移链(线性,`backend/alembic/versions/`):
`20260515_0001` → `0002` → `0003` → `0004` → `0005(pgvector)` → `0006(agent memory)` → `0007(case modules)`
→ `20260724_0008(structured inventory)` → **`20260730_0009(inventory loans + 人工损坏标记)`**。

**升级已有数据库**:一键启动器走 `AUTO_CREATE_TABLES=true`(`Base.metadata.create_all`),而 `create_all`
只建缺失的**表**、不动已存在表的列——新版本加一列,老用户的库就少一列,任何查询都会 `no such column`。
`database.py::sync_additive_schema()` 因此在启动时补齐**新增的可空列与索引**(仅增量:改类型、删列、
回填数据仍归 Alembic,遇到无法自动处理的情况只记录警告并提示 `alembic upgrade head`)。
Docker/生产路径 `AUTO_CREATE_TABLES=false`,以 Alembic 为准。
不变量测试:`tests/test_schema_upgrade.py`(遍历**全部**模型表,而非当期新增的那一列)。

RAG 管线文件:`knowledge/ingestion.py`(提取/入库)→ `knowledge/chunking.py` → `knowledge/embeddings.py`
(local 稀疏 / openai / sentence-transformers)→ `knowledge/vector_store.py`(sql_json / chroma / pgvector)
→ `knowledge/retrieval.py`(两级检索+置信度+no-answer)→ `knowledge/reranking.py`。
Provider 抽象:`providers/base.py`(ABC)+ `providers/mock.py` / `openai.py` / `anthropic.py`,工厂在 `providers/__init__.py`。
**国内模型**:`AI_PROVIDER` 可设为 `deepseek` / `qwen`(阿里 DashScope 兼容模式)/ `zhipu`(智谱 GLM)/ `moonshot`(Kimi)——四家均为 OpenAI 兼容端点,复用 `OpenAIProvider` 并注入各自 `base_url`/默认模型;缺少对应 `*_API_KEY` 时自动回退 MockProvider(配置见 `config.py` 与 `.env.example`)。

---

## 9. 测试与质量门(`backend/tests/`,共 388 用例,386 passed + 2 skipped)

| 文件 | 覆盖 |
|---|---|
| `test_physics_materials.py` / `test_physics_tmm.py` / `test_physics_beam.py` / `test_physics_coating_tool.py` / `test_physics_nonlinear.py` | 物理内核 vs 解析解与文献值 |
| `test_physics_tmm_oracle.py` | 与独立 `tmm` 库对拍(dev 依赖,缺库自动跳过) |
| `test_physics_adversarial_fixes.py` | 物理内核对抗审查确认缺陷的回归锁 |
| `test_acceptance_hardening.py` | **验收对抗审查**确认缺陷的回归锁:评估器误判(below_spec / AR 当 HR / 显式冲突优先 / 支配维度)、功率拟合折线选段与告警保真、caird 除零与类型容错、导入器空行与数量 0、解析器尾部波长、上传大小门、.txt/.tsv 加载、非 dict 需求 |
| `test_physics_modules.py` | 物理工具经 REST 模块 + Agent 任务 + 聊天路由 |
| `test_power_curve.py` | 功率曲线拟合、跨曲线 Findlay-Clay/Caird、API 往返 |
| `test_inventory.py` | L0 语法解析、导入、SQL 过滤、L1 三态/硬门/前沿、Agent 路径 |
| `test_literature.py` | 解析器吃录制样本(OpenAlex 倒排摘要重建、arXiv 命名空间 Atom)、去重不变量、三态诚实性(全失败/单失败/无查询词)、三入口端到端、路由正负例与既有路由回归、RAG 闭环(存档结果可被检索) |
| `test_inventory_loans.py` | 借出只减可用量不动库存、超借拒绝、归还留痕、人工损坏标记扛住重新导入、有未归还时拒绝重新导入、批次删除级联 |
| `test_beam_envelope.py` | 摆位图包络的两个易错约定:物理 z 轴、晶体内外光斑连续(w 连续、只有发散角变) |
| `test_schema_upgrade.py` | `create_all` 老库升级:遍历**全部**模型表补列补索引;NOT NULL 列不伪造默认值而是告警 |
| `test_kernel_invariants.py` | 物理内核纯度(AST 检查禁止的 import)+ 两个搜索规模数字 |
| `test_acl.py` / `test_security_hardening.py` | ACL 与提权/越权回归(角色以 DB 为准、evals 租户隔离、失败回滚) |
| `test_cases/knowledge_agent/generation/agent_memory/...` | 业务 API 全链路 |
| CI:`.github/workflows/ci.yml` | ruff、pytest、SQLite 迁移冒烟、真实 Postgres+pgvector 集成、前端 lint/build |

---

## 10. 可复现演示(`backend/scripts/`)

| 脚本 | 演示内容 |
|---|---|
| `scripts/demo_physics_case.py` | 通用工作流:建 Case → 中文路由 → Agent 任务 → 腔长扫描/相位匹配/镀膜评估 → 引用与审计(内存库+mock LLM,零依赖可跑) |
| `scripts/demo_thg_inventory_case.py --inventory <xlsx>` | **紫外三倍频 vs 真实库存**:解析工作簿 → 库存约束腔设计 → SHG/THG 角 → 库存 BIBO 切角第一性验证(差 0.1°)→ 库存 LBO 逐块偏差量化 → 缺口/实测清单(库存数据仅入内存,不落盘) |

---

## 10.5 前端(`frontend/src/`)

| 文件 | 职责 |
|---|---|
| `App.jsx` | 路由与导航(首页 / Agent / 案例 / 实验室文档 / **元件库**) |
| `pages/CaseDetail.jsx` | 案例详情:概览(结构化参数表)、生成物(分节渲染 + 原始 JSON 折叠)、知识检索、**模块**(分组下拉 + 每模块独立参数框 + 类型化结果)、Agent 任务轨迹、附件;统一错误横幅 |
| `components/ModuleResults.jsx` | **类型化结果渲染**:功率曲线 SVG(坐标刻度 + 实测点 + 阈值标记)、多曲线对比与腔损耗、相位匹配表、镀膜三态色块、**腔摆位图(横向真实比例的 SVG:镜面曲率朝向、晶体位置与厚度、束腰位置 + 冷腔基模包络)**、腔设计摆位表、元件匹配前沿 |
| `print.css` | `@media print`:隐藏导航/按钮、强制白底黑字、SVG 描边改深色、卡片与表格 `break-inside: avoid`。浏览器「打印 → 另存为 PDF」即得可带进实验室的纸质方案(零依赖,矢量图,中文字体走系统) |
| `pages/LiteraturePage.jsx` | 文献检索独立页:表单 → `/api/literature/search` → 复用 `LiteratureSearchResult`;「存入案例」走现有建模块+运行接口 |
| `pages/InventoryPage.jsx` | 元件库:xlsx 导入、波长×功能 SQL 级筛选、名称搜索、待复核队列、需求匹配、**借出/归还与损坏标记**(数量列在有借出时显示 `可用 / 总数`) |
| `pages/AgentWorkspace.jsx` | Agent 对话:路由结果 / 任务 / 引用 pill |
| `api/client.js` | axios 封装 + HTTP 状态码中文提示 |
| `api/inventory.js` | 元件库 API 客户端 |
| `LanguageContext.jsx` / `i18n.js` | 中英双语;`t()` 文案 + `te()` 后端枚举值翻译(状态/置信度/带位/元件名) |

---

## 11. 配置(`backend/app/config.py` + `.env`)

关键开关:`AI_PROVIDER`(mock/openai/anthropic)、`EMBEDDING_PROVIDER`、`RETRIEVAL_BACKEND`
(sql_json/chroma/pgvector)、`RERANKER_PROVIDER`、`REQUIRE_AUTH`/`API_KEY`(生产必开)、
检索阈值组(`retrieval_min_score` 等)。Docker 部署:`docker-compose.yml`(migrations 权威,
`AUTO_CREATE_TABLES=false`)。

---

## 12. 已知边界与下一步

- **物理内核边界(已文档化)**:双轴相位匹配为主平面近似(medium 置信度);TMM 假设无损入射介质;
  含腔内介质时束腰**位置**为光程坐标(尺寸精确);金属镜/GDD 镜/LBO 温度调谐待做。
- **L1 边界**:镀膜三态依赖标称阻带先验,实测曲线导入(`coating_tool` vendor_curve 模式)可逐件升级置信度,
  但实测结果尚未持久化回库存元件(测完不消 must_measure 项——已列为下一步)。
  晶体切角匹配已并入 evaluator(需求 spec 的 `phase_match` 字段;按各晶体**自身材料**算目标匹配角,
  与标签切角比对出三态:≤1° design_match / ≤5° 可重调需实测 / 更大硬伤否决;θ 与 180°−θ 等价);
  元件库页面"非线性晶体(切角)"匹配模式即此路径,回归锚点见 `tests/test_inventory_cut_angle.py`。
- **L2/L3 方向**:实验室历史先例库(成/败记录)校准 maybe_usable 阈值;LLM 判断层消费 L1 前沿
  (每条结论必须挂在算出的数字或检索到的先例上);整链 propose-verify 耦合搜索。

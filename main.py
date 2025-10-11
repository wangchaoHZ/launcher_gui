# -----------------------------------------------------------------------------
# 导入所需的库
# -----------------------------------------------------------------------------
# 导入 numpy 库，它提供了强大的多维数组对象和用于处理这些数组的函数。
# 在这里，我们主要用它来方便地计算能耗（平方和）。
import numpy as np

# 导入 matplotlib.pyplot 库，它是Python中最常用的绘图库。
# 我们用它来将仿真结果可视化，直观地对比不同控制器的效果。
import matplotlib.pyplot as plt

# 从 gekko 库中导入 GEKKO 类。GEKKO 是一个强大的Python包，用于解决各种数学优化问题，
# 包括动态优化和模型预测控制（MPC）。它是我们实现MPC控制器的核心工具。
from gekko import GEKKO

# 导入 datetime 和 pytz 库，用于在图表标题中生成带时区的当前时间戳。
# 这有助于标识每次运行的结果，证明其唯一性。
from datetime import datetime
import pytz

# -----------------------------------------------------------------------------
# 1. 使用GEKKO搭建MPC控制器
# -----------------------------------------------------------------------------
# 创建一个 GEKKO 模型实例。可以把它想象成一个工作台，我们将在上面搭建我们所有的模型和优化问题。
# remote=False 参数意味着所有的计算都在您的本地计算机上进行，而不是发送到远程服务器。
# name='mpc_controller' 为这个模型实例提供一个可读的名称，便于调试。
m = GEKKO(remote=False, name='mpc_controller')

# -- 定义模型参数 --
# 这些参数构成了我们“灰盒模型”的核心，它们描述了房间的物理特性。
# 在实际项目中，这些值需要通过“系统辨识”从真实设备的运行数据中学习得到。
A = 0.99  # 温度衰减系数。代表由于散热，当前温度在下一时刻会保留99%。值越接近1，保温效果越好。
B = 0.5  # 控制输入增益。代表单位功率的空调能对温度产生多大的影响。这个值越大，空调“马力”越足。
C = 0.2  # 外部恒定扰动。代表由于室外低温等因素，每一步会固定带来的温度下降量。
set_point_val = 22.0  # 设定点（Set Point），也就是我们希望房间维持的目标温度。

# -- 定义GEKKO变量 --
# 在GEKKO中，我们需要明确定义哪些是状态、哪些是我们可以操作的、哪些是目标。

# ** 关键修正点 1: 将温度T定义为“被控变量”(Controlled Variable, CV) **
# CV是MPC的核心概念，它代表我们最终关心的、需要被控制到目标值的量。
# value=21.5 是这个变量的初始值，GEKKO会从这里开始计算。
# name='temperature' 为变量命名，便于阅读和调试。
T = m.CV(value=21.5, name='temperature')

# ** 关键修正点 2: 为CV配置状态反馈 **
# T.STATUS = 1: 激活对这个变量的控制。这行代码告诉GEKKO：“你的目标之一就是把T控制到它的设定点（SP）”。
T.STATUS = 1
# T.FSTATUS = 1: 激活反馈状态（Feedback Status）。这行代码告诉GEKKO：“请在每个时间步，通过T.MEAS属性接收来自外部的真实测量值，并用它来校准你的内部预测。” 这是实现闭环控制的关键。
T.FSTATUS = 1

# 定义“操纵变量”(Manipulated Variable, MV)，这是我们可以主动调节去影响CV的量。
# 在这里，它代表空调的功率。
# value=0 是初始值；lb=-1, ub=1 是物理约束，代表功率最小-1（最大制冷），最大+1（最大制热）。
# name='power' 为变量命名。
u = m.MV(value=0, lb=-1, ub=1, name='power')

# u.STATUS = 1: 激活这个变量。告诉GEKKO：“你可以自由调节这个变量（在上下限内），以达成控制目标。”
u.STATUS = 1
# u.DCOST = 0.1: 设置控制动作的成本（Delta Cost）。这是一个非常重要的节能参数！
# 它会对控制量u的剧烈变化进行“惩罚”。值越大，GEKKO就越倾向于使用更平滑、更小的控制动作，从而实现节能。
u.DCOST = 0.1

# -- 定义模型方程 --
# 这是MPC的“预言家”，是整个控制器的灵魂。它用数学语言描述了系统的动态行为。
# m.Equation(...) 用于定义一个必须始终成立的方程。
# T.dt() 代表温度T对时间的导数（变化率）。
# 这个微分方程 T' = (A-1)*T + B*u + C 是我们之前差分方程 T_next = A*T + B*u + C 的连续时间等价形式。
# GEKKO内部会处理这种转换。这行代码精确地告诉了GEKKO，温度是如何随时间和空调功率变化的。
m.Equation(T.dt() == (A - 1) * T + B * u + C)

# -----------------------------------------------------------------------------
# 2. 配置MPC控制器
# -----------------------------------------------------------------------------
# 设置MPC的“预测时域”(Prediction Horizon)。
# np.linspace(0, 10, 11) 创建了一个从0到10，包含11个点的时间序列 [0, 1, ..., 10]。
# 这告诉MPC：“在每个决策点，请向前看10个时间步，并为这10步规划最优的控制策略。”
m.time = np.linspace(0, 10, 11)

# -- 配置被控变量CV的目标 --
# T.SP = set_point_val: 为温度T设置其目标值（Set Point）。
T.SP = set_point_val
# T.TR_INIT = 1: 初始化参考轨迹（Trajectory Initialization）。
# 这会帮助求解器规划一条从当前值平滑过渡到目标值的理想路径，有助于提高求解稳定性和控制效果。
T.TR_INIT = 1

# -- 设置GEKKO求解器选项 --
# m.options.IMODE = 6: 这是最重要的设置之一。将积分模式（IMODE）设置为6，明确告诉GEKKO：“请以模型预测控制（MPC）模式运行”。
m.options.IMODE = 6
# m.options.CV_TYPE = 2: 设置被控变量的误差类型。CV_TYPE=2代表目标是最小化误差的平方和（L2范数），这是MPC中最常用的目标函数。
m.options.CV_TYPE = 2

# -----------------------------------------------------------------------------
# 3. 仿真循环
# -----------------------------------------------------------------------------
# 设置总的仿真时长。
duration = 100
# 初始化列表，用于存储MPC控制器在每个时间步的温度和控制动作，以便后续绘图。
temps_mpc = [21.5]
controls_mpc = []

# 为了对比，我们手动模拟一个简单的“开关”控制器。
# 初始化它的温度和控制动作列表。
temps_simple = [21.5]
controls_simple = []
T_simple_current = 21.5  # 简单控制器维护自己的当前温度状态。

# 开始仿真循环，模拟时间的流逝。
for i in range(duration):
    # --- MPC 控制器部分 ---
    # ** 关键反馈步骤 **
    # 将上一步模拟得到的“真实”温度，通过.MEAS属性反馈给MPC控制器。
    # 这是MPC闭环控制的核心：MPC根据真实世界的反馈来修正自己的预测和规划。
    T.MEAS = temps_mpc[-1]

    # 调用求解器！GEKKO会根据最新的测量值，求解在预测时域内的最优控制序列。
    # disp=False 表示不在控制台打印求解器的详细日志。
    m.solve(disp=True)
    # 从求解结果中，只取出最优控制序列的第一个动作，作为当前要执行的控制量。
    # 这是MPC“滚动时域”思想的体现：规划N步，只走一步，然后重新规划。
    controls_mpc.append(u.NEWVAL)

    # --- 简单控制器部分 ---
    # 实现简单的开关逻辑：温度低于目标就全力制热，否则就全力制冷。
    u_simple = 1.0 if T_simple_current < set_point_val else -1.0
    # 记录简单控制器的动作。
    controls_simple.append(u_simple)

    # --- 系统状态更新部分 ---
    # 根据两个控制器各自给出的控制动作，使用我们的物理模型来计算下一时刻的“真实”温度。
    # MPC系统的下一时刻温度。
    T_mpc_next = A * temps_mpc[-1] + B * controls_mpc[-1] + C
    # 将新温度存入列表。
    temps_mpc.append(T_mpc_next)

    # 简单控制器系统的下一时刻温度。
    T_simple_current = A * T_simple_current + B * u_simple + C
    # 将新温度存入列表。
    temps_simple.append(T_simple_current)

# -----------------------------------------------------------------------------
# 4. 结果可视化
# -----------------------------------------------------------------------------
# 计算两种控制策略的总能耗。这里简化为控制动作的平方和，它能很好地反映能耗大小和波动情况。
simple_energy = np.sum(np.square(controls_simple))
mpc_energy = np.sum(np.square(controls_mpc))

# 创建一个包含两个子图的图窗，用于上下对比温度和控制动作。
fig, axs = plt.subplots(2, 1, figsize=(12, 10), sharex=True)
# 设置整个图窗的总标题。
fig.suptitle(f"Corrected GEKKO MPC vs Simple Controller", fontsize=14)

# -- 绘制第一个子图：温度变化 --
# 绘制简单控制器的温度曲线，使用红色虚线。
axs[0].plot(temps_simple, 'r--', label=f'Simple Controller (Energy: {simple_energy:.2f})')
# 绘制MPC控制器的温度曲线，使用蓝色实线，并加粗（lw=2）以突出显示。
axs[0].plot(temps_mpc, 'b-', lw=2, label=f'GEKKO MPC (Energy: {mpc_energy:.2f})')
# 绘制一条黑色的水平虚线，代表我们的目标温度。
axs[0].axhline(y=set_point_val, color='k', linestyle=':', label=f'Set Point ({set_point_val}°C)')
# 设置Y轴标签、显示图例、并添加网格线以方便读数。
axs[0].set_ylabel('Room Temperature (°C)');
axs[0].legend();
axs[0].grid(True)

# -- 绘制第二个子图：控制动作 --
# 绘制简单控制器的控制动作曲线。
axs[1].plot(controls_simple, 'r--', label='Simple Controller Action')
# 绘制MPC控制器的控制动作曲线。
axs[1].plot(controls_mpc, 'b-', lw=2, label='GEKKO MPC Action')
# 设置X轴和Y轴的标签、显示图例和网格线。
axs[1].set_xlabel('Time (minutes)');
axs[1].set_ylabel('Control Power (-1 to 1)');
axs[1].legend();
axs[1].grid(True)

# 自动调整子图布局，防止标题和标签重叠。
plt.tight_layout(rect=[0, 0, 1, 0.96])
# 显示最终生成的图表。
plt.show()
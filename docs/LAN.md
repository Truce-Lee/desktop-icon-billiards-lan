# 局域网对战（开发说明）

两台 Windows 电脑各开一份游戏，一人创建房间、一人加入，用同一张台面轮流击球。

## 为什么由房主算物理

`BilliardsMotion.step` 用帧时钟的 `dt` 积分，子步数由 `dt` 和当前速度共同决定，所以**同一杆在不同帧率下结果不同**。实测（15 颗球、同一力度、1600×1000 台面）：

| 对比 | 落点最大偏差 |
|---|---|
| 60 Hz vs 144 Hz | 438.09 px |
| 60 Hz vs 30 Hz | 14.18 px |
| 60 Hz vs 60 Hz（±25% 抖动） | 187.77 px |

因此不让两端各自模拟：**房主是唯一权威**，客机只发送击球意图并渲染收到的快照。台面位置按边界归一化传输，所以两边分辨率、DPI、显示器布局不同也不影响成绩，只影响各自画出来的球袋大小。

## 协议

TCP，4 字节大端长度 + UTF-8 JSON，单条上限 256 KiB。所有坐标归一化到台面矩形，方向是单位向量，力度是 0..1。

| 方向 | 类型 | 字段 | 说明 |
|---|---|---|---|
| 客 → 主 | `hello` | `v`、`balls`、`names` | 连上就发，房主据此取 `min(balls)` 为台面球数 |
| 主 → 客 | `welcome` | `v`、`balls`、`names` | 房主应答；双方版本不一致时客机报错 |
| 客 → 主 | `shot` | `aim:[dx,dy]`、`power` | 击球意图 |
| 客 → 主 | `place` | `x`、`y` | 自由球摆位（归一化） |
| 主 → 客 | `state` | `round`、`state`、`clock`、`sunk`、`bodies[[x,y,vx,vy,angle,impact]]`、`aim`、`charge`、`match`、`events` | 30 Hz；待机时降到 2 Hz |
| 客 → 主 | `event` | `kind:"restart"` | 请求重开 |
| 双向 | `bye` | — | 主动断开 |

`match` 是比分、轮次、犯规、结束状态的摘要；`events` 是本帧产生的 `potted` / `turn` / `foul` / `finished` / `restart`，客机不需要重算规则。

## 规则

- 每进一颗图标得 1 分，进球方继续击球。
- 白球入袋或空杆（这一杆没碰到任何图标）判犯规，换对手击球；白球入袋后由 `BilliardsMotion` 的 `ball_in_hand` 流程处理摆位。
- 台面清空即结束，分高者胜，同分算平局。
- 联机对局**不提交全球排名**：房主可以随意伪造比赛结果，成绩不可信。

## 代码结构

| 文件 | 职责 |
|---|---|
| `dib/net.py` | 帧编解码、`Link` / `Server` / `Client` 线程、状态打包与插值 |
| `dib/match.py` | 回合、比分、犯规、结束判定；纯 Python，可无桌面测试 |
| `dib/lanplay.py` | 握手、球数裁剪、快照流、输入路由 |
| `dib/overlay.py` | 房主调 `step`，客机调 `render`；`trim_balls` 隐藏多余图标窗口 |
| `dib/panel.py` | `LanDialog`：建房地址、加入输入框、状态文本 |
| `dib/ui.py` | 创建/加入房间、工具栏状态、比分显示 |

## 无窗口验证

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_lan -v
.\.venv\Scripts\python.exe -m unittest tests.test_lan_loop -v
```

`tests/test_lan.py` 覆盖协议、收发线程、状态往返、规则判定和首碰记录；
`tests/test_lan_loop.py` 在 127.0.0.1 上跑真 TCP：房主算真实物理，客机渲染副本，
断言两者归一化位置偏差 < 0.002 个台面。这两个测试不创建窗口、不碰 Shell、不动桌面。

## 已知限制

- 只支持 2 人、一局一张台；断线后只能退出重开（没有重连）。
- 客机的手感受往返延迟影响；局域网内无感，跨网/VPN 会有明显延迟。
- 双方桌面图标数不同时按较小值裁剪，球面上的图标以各自桌面为准（不传缩略图，不泄露文件名）。
- `Ctrl+Alt+H` 隐藏、暂停对局只影响本机；对局中切换分辨率会按原有保护逻辑中止本局。

# 外汇风险与锁汇模拟工作台

这是一个本地可运行的外汇风险识别、敞口汇总、锁汇建议、损益模拟和回测原型，用于业务流程演示和内部试算。

## 功能概览

- 添加未来外币收款/付款敞口。
- 按月份、币种、风险类型汇总净敞口。
- 按企业类型、目标套保比例、已执行交易覆盖情况生成推荐交易。
- 一键把推荐交易填入锁汇单，并记录执行锁汇操作。
- 模拟中性、乐观、悲观、自定义汇率场景下的预计损益。
- 按会计科目口径拆分：
  - 未实现汇兑损益
  - 衍生品投资收益
  - 衍生品公允价值变动损益
  - 已实现汇兑损益
- 添加到期实际汇率，回测锁汇贡献。
- 使用 ExchangeRate-API 免费接口拉取汇率，并本地缓存 24 小时。
- 提供命令行、Tkinter 桌面界面和本地网页三种运行方式。

## 快速启动：本地网页

进入项目目录：

```powershell
cd C:\Users\z1628\Documents\fx_patent_reconstruction
```

启动本地服务：

```powershell
python web_app.py
```

浏览器打开：

```text
http://127.0.0.1:8765
```

默认端口是 `8765`。如果需要换端口：

```powershell
python web_app.py --port 8877
```

## 网页使用流程

1. 在“添加敞口”里录入未来收外币或付外币。
2. 查看“净敞口”，确认系统按月份和币种汇总后的风险。
3. 查看“锁汇建议”，确认推荐金额、操作方向和损益科目。
4. 点击“按建议填入锁汇单”，再保存锁汇记录。
5. 查看“预计损益场景”，比较中性、乐观、悲观、自定义汇率下的损益。
6. 到期后在“添加到期实际汇率”里填实际汇率。
7. 查看“回测结果”，确认锁汇贡献。

## 汇率接口

默认使用 ExchangeRate-API 免费接口：

```text
https://open.er-api.com/v6/latest/USD
```

配置位置：网页底部“配置”区域。

本地会把汇率缓存到：

```text
data/rates_cache.json
```

缓存默认 24 小时。该文件已加入 `.gitignore`，不会提交到 GitHub。

数据来源需保留 attribution：

```text
ExchangeRate-API: https://www.exchangerate-api.com
```

## 本地数据

网页录入的数据会保存到：

```text
data/fx_workspace.json
```

该文件已加入 `.gitignore`。公开仓库不会包含客户数据、测试运行数据或汇率缓存。

如果本地数据混乱，可以在网页右上角点击“恢复样例”。

## 命令行版本

输出 JSON：

```powershell
python fx_risk_simulator.py sample_data.json --pretty
```

输出白话解释：

```powershell
python fx_risk_simulator.py sample_data.json --explain
```

## 桌面 GUI 版本

```powershell
python fx_risk_gui.py
```

打开后选择 JSON 数据文件，点击“运行模拟”，可以查看摘要、白话解释、套保策略、敞口、自动校验和原始输出。

## 测试

```powershell
python -m unittest test_fx_risk_simulator.py test_web_app.py
python -m py_compile fx_risk_simulator.py fx_risk_gui.py web_app.py
node --check web\app.js
```

当前项目使用 Python 标准库实现本地网页服务，不需要安装 Web 框架。

## 项目结构

```text
.
├── web_app.py                  # 本地 Web 服务、API、汇率缓存、敞口/锁汇/回测逻辑
├── web/
│   ├── index.html              # 网页工作台
│   ├── styles.css              # 页面样式
│   └── app.js                  # 前端交互
├── fx_risk_simulator.py        # 命令行模拟器
├── fx_risk_gui.py              # Tkinter 桌面界面
├── sample_data.json            # 命令行样例数据
├── test_fx_risk_simulator.py   # 命令行模型测试
├── test_web_app.py             # Web 后端核心逻辑测试
├── BEGINNER_GUIDE.md           # 小白版逻辑说明
└── .gitignore                  # 忽略本地数据和缓存
```

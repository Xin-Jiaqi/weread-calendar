# weread-calendar（微信读书阅读月历生成器）
<img width="1448" height="1086" alt="ChatGPT Image 2026年5月23日 10_39_21" src="https://github.com/user-attachments/assets/52aee400-c649-45d4-ab58-3e2c39693ee1" />

一个在本地导出个人阅读记录并生成阅读日历可视化的 Alpha 工具。当前版本已经建立可安装入口、安全回归测试和 Python 3.11–3.14 CI；在线接口仍可能随平台变化。

它可以把你的微信读书阅读数据整理成：

- 每日阅读 CSV
- 交互式 HTML 阅读报告
- 每月阅读日历 PNG
- PNG 打包 ZIP

适合用来做个人阅读复盘、年度总结、Obsidian 记录、GitHub 项目展示或社交平台分享。

## 效果

本工具会根据你的微信读书阅读记录，生成类似“阅读月历”的视图：

- 每天读了哪些书
- 每天读了多久
- 每月阅读分布
- 多本书在同一天的阅读情况

## 使用方法

安装（Python 3.11 或更高版本）：

```bash
python -m pip install -e '.[image]'
```

运行脚本：

```bash
weread-calendar
```

首次运行时，程序会打开浏览器，请扫码登录微信读书。

Cookie 不再接受命令行参数，因为命令行历史和进程列表可能泄露凭证。无法扫码时使用 `--manual-cookie` 交互粘贴；选择保存时文件权限会限制为当前用户可读写，`--save-raw` 也不会保存 Cookie 值。

默认输出目录为：

```text
weread_export/
```

主要输出文件包括：

```text
weread_daily_reading.csv       每日阅读数据
reading_report.html            交互式阅读报告
monthly_png/                   每月阅读日历 PNG
weread_monthly_views.zip        PNG 打包文件
```

## 常用参数

指定输出目录：

```bash
python weread_export_fixed_v13.py --out-dir weread_export
```

只生成 HTML，不导出 PNG：

```bash
python weread_export_fixed_v13.py --export-png none
```

从已有 CSV 重新生成报告：

```bash
python weread_export_fixed_v13.py --from-csv weread_export/weread_daily_reading.csv
```

导出指定月份 PNG：

```bash
python weread_export_fixed_v13.py --png-months 2025-04 2025-05
```

自动打包 PNG：

```bash
python weread_export_fixed_v13.py --zip-png
```

如果任何书籍请求失败，程序仍会保留已经完成的输出，但默认返回非零退出码。只有明确接受不完整结果时才使用 `--allow-partial`。

<img width="1448" height="1086" alt="ChatGPT Image 2026年5月23日 10_44_19" src="https://github.com/user-attachments/assets/6092df82-e416-42b8-866b-e566ce81d956" />


## 关于微信读书的声明

本项目不是微信读书官方项目，也未与微信读书或腾讯公司建立任何形式的合作、授权或关联。

本工具仅用于个人学习、数据备份和阅读记录可视化。它不会破解、绕过或修改微信读书的服务，只是在用户本人登录后，读取用户自己账号下可访问的阅读相关数据。

使用本项目时，请遵守微信读书的用户协议、相关法律法规以及平台规则。请勿将本工具用于批量抓取、商业用途、侵犯他人权益或任何违反平台规则的行为。

如果微信读书接口、登录方式或页面结构发生变化，本工具可能会失效。

## 隐私声明

本工具在本地运行，默认不会上传你的 Cookie、阅读记录、书单、阅读时长或生成文件到任何第三方服务器。

你的数据默认只保存在本地输出目录中，例如：

```text
weread_export/
```

可能包含的本地文件包括：

- 阅读记录 CSV
- HTML 阅读报告
- PNG 图片
- 可选保存的 Cookie 文件
- 可选保存的原始 JSON 调试文件

请妥善保管这些文件，尤其是 Cookie 文件。Cookie 具有登录凭证性质，不要上传到 GitHub，不要分享给他人。

仓库已经提供 `.gitignore`，默认排除：

```gitignore
weread_export/
weread_cookie.txt
weread_cookies_raw.json
raw_json/
```

## 适合谁用

这个工具适合：

- 想整理自己微信读书阅读记录的人
- 想做年度阅读报告的人
- 想把阅读数据导入 Excel、Obsidian 或其他工具的人
- 想生成阅读月历图片进行分享的人

## 作者

辛嘉琪

## License

本项目源码仅供个人学习、研究和非商业使用。未经作者许可，不得用于商业服务、批量数据抓取、账号代管、第三方数据分析服务或任何违反微信读书平台规则的用途。

当前仓库尚未添加标准开源许可证，因此不应视为已经授权的开源软件；发布标准许可证前需要由作者确认最终公开范围。

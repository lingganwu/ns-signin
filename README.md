# NS签到助手（Windows 桌面版）

NodeSeek 每日签到小工具。登录一次，之后每天自动签到收鸡腿。

## 下载

[Releases 页面](https://github.com/lingganwu/ns-signin/releases) 下载 `ns-signin-win64.zip`，解压得到 `NS签到助手.exe`。

## 使用

1. 双击 `NS签到助手.exe`（首次运行 Windows 可能弹 SmartScreen，点「仍要运行」）
2. 点 **「登录/更新登录态」** → 会弹出一个真实浏览器窗口 → 在里面正常登录 NS（含人机验证）→ 登录成功窗口自动关闭，界面显示绿色「登录态已保存」
3. 点 **「启动后台调度」** → 每天在你设定的时间窗（默认 8:00–12:00 随机）自动签到
4. 保持程序运行（最小化即可，电脑关机/程序退出时不会签到）

界面显示：登录状态、上次结果（鸡腿数）、连续天数、下次执行时间。

## 常见问题

- **界面红字「需要重新登录」**：NS 的登录态过期了（一般 7 天以上或改过密码），点「登录/更新登录态」重新过一次即可
- **签到一直「Cloudflare 挑战页」**：程序会自动换有头模式重试；如果长期失败，检查本机能正常打开 NS 吗（梯子）
- **想改签到时间**：退出程序，编辑 `ns_data/config.json` 里的 `start` / `end`（格式 `08:00`）
- **开机自动运行**：把 exe 的快捷方式放进 `Win+R` → `shell:startup` 打开的文件夹
- 数据都在 exe 同级的 `ns_data/` 文件夹（登录态、配置、日志），删掉即恢复初始

## 原理与隐私

- 登录在你自己的真实浏览器窗口完成，账号密码不经过本程序、不存储
- 程序只保存浏览器登录态（cookie）用于每日签到，文件在你本地 `ns_data/cookies.json`
- 签到请求 = 浏览器里点一下签到按钮，同接口同参数
- 依赖 Playwright + 系统 Edge/Chrome，无需安装 Python

## 构建

推送代码到 main 分支后 GitHub Actions 自动出 Windows exe（见 `.github/workflows/build.yml`），也可在 Actions 页手动触发。

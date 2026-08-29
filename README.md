# Trae Work 签到（青龙面板单文件脚本）

**Trae Work（字节，trae.cn 国内版）每日积分自动签到 · 单文件 · 零依赖（仅 Python 标准库）**

每天自动领取 Trae Work 200 积分。登录一次保存凭证，之后由青龙面板定时任务自动签到。

> ⚠️ **免责声明**：这是第三方逆向脚本，与官方无关，可能违反相关产品的服务条款，接口随时可能失效。仅供个人学习研究，请自行评估风险后使用。

## 亮点

- **零依赖**：仅用 Python 标准库 `urllib` / `http.server`，无需安装任何第三方库
- **单文件**：登录、签到、设备指纹全部在 `trae_work_checkin.py` 一个文件里
- **设备指纹过校验**：9074「当前参与用户太多」实测不是限流而是指纹校验失败，用官方客户端注册的真实设备 ID + 完整指纹头签到
- **一设备一账号**：同一真实设备一天只能签一个账号（9095），多账号自动错开绑定本机不同注册设备
- **限流自动重试**：9074 自动等待重试，并轮换本机设备池
- **文件凭证**：仅读取脚本同目录 `auths/auth-<uid>.json` 文件，无环境变量
- **青龙面板适配**：`new Env('..')` 头，定时与补签由面板定时任务决定

## 环境要求

Python 3.10+，无任何第三方依赖。

## 用法

登录（**Windows 本地**执行，需带浏览器的环境）：

```bash
python trae_work_checkin.py login
```

签到（青龙面板定时任务调用 / 手动执行，无参数等同）：

```bash
python trae_work_checkin.py checkin
或
python trae_work_checkin.py
```

## 青龙面板部署

1. 添加订阅拉取仓库
    在青龙「订阅管理」中添加订阅：
    - 订阅链接地址: `https://wget.la/https://github.com/xz0609/trae-work-checkin-ql.git`
    - 添加完成后，点击 `运行` 按钮，拉取仓库代码。

2. 凭证：将本机 `login` 生成的 `auths/auth-<uid>.json` 上传到青龙容器脚本目录（凭证只从该文件读取）

3. （可选）随机延时环境变量，避免多账号同时刻打卡：
   - `RANDOM_SIGNIN`：是否启用随机延时，默认 `true`
   - `MAX_RANDOM_DELAY`：随机延时上限秒数，默认 `3600`（最多 1 小时）
   在青龙「环境变量」里配置即可，签到开始前会先随机延时并打印倒计时。

## 凭证管理

- 登录成功后凭证保存为 `auths/auth-<uid>.json`（`<uid>` 为账号 JWT 里的用户 ID）
- token 临期前 24 小时自动用 refreshToken 刷新并回写
- refreshToken 失效时重新执行 `login`

## 排查

- 网络 / 接口变更排查：设置 `DEBUG_HTTP=1` 打印完整请求与响应

## 致谢

- [yang89520/auto-checkin-hub](https://github.com/yang89520/auto-checkin-hub) — Fork来源, 基于此项目修改
- [agluo/ql-script-hub](https://github.com/agluo/ql-script-hub) — 青龙面板脚本格式参考

## License

[MIT](LICENSE)
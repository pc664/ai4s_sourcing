# 云服务器部署步骤

部署完成后，组员在浏览器打开一个 `https://` 网址、用自己的账号登录即可使用。服务器每周自动更新一次数据，每天自动备份跟进记录。

整个过程大约 30–60 分钟，大部分时间花在买服务器和配置域名上。下面的命令里，`服务器IP` 和 `talent.example.com` 请换成你自己的。

---

## 第 1 步：买服务器

推荐配置：**轻量应用服务器 · 2 核 4G · 系统选 Ubuntu 24.04**（腾讯云或阿里云都可以）。

**地域怎么选：**

| | 香港 | 中国大陆 |
|---|---|---|
| 域名备案 | **不需要** | 必须先做 ICP 备案（一般 1–3 周），否则网站无法用域名访问 |
| 访问 OpenAlex 抓数据 | 稳定 | 一般可以，偶尔慢 |
| 组员在国内访问 | 略慢，但够用 | 最快 |
| 价格 | 比大陆略贵 | 最便宜（2 核 4G 活动价约 ¥180/年起，以控制台为准） |

小组内部使用，**建议选香港**，当天就能用上。

**防火墙（控制台里叫"防火墙"或"安全组"）：** 只开放 **22**（SSH）、**80**、**443** 三个端口。不要开放 8501。

**登录方式：** 建议在控制台绑定 SSH 密钥，而不是用密码登录。

> 腾讯云的 Ubuntu 镜像默认用户名是 `ubuntu`；阿里云默认是 `root`。下面的命令用 `ubuntu`，如果你是 `root`，把 `ubuntu@` 换成 `root@`，并去掉 `--rsync-path="sudo rsync"`。

## 第 2 步：准备域名（强烈建议）

有域名才能启用 HTTPS 加密，否则登录密码会以明文在网络上传输。

1. 在腾讯云、阿里云等处买一个域名（几十元一年）。
2. 在域名的"DNS 解析"里加一条 **A 记录**：主机记录填 `talent`（或任何你喜欢的前缀），记录值填 **服务器IP**。
3. 等几分钟生效。在 Mac 终端里运行 `ping talent.example.com`，看到的 IP 是你的服务器就说明好了。

> 暂时没有域名也能先部署，用 `http://服务器IP` 访问试用。但这时连接没有加密，只适合短期试用，正式使用前请补上域名。

## 第 3 步：把软件上传到服务器（在你的 Mac 上操作）

```bash
ssh ubuntu@服务器IP 'sudo mkdir -p /opt/ai4s_sourcing'

rsync -av --rsync-path="sudo rsync" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.DS_Store' --exclude 'data/works_quick' \
  ~/Downloads/ai4s_sourcing/ ubuntu@服务器IP:/opt/ai4s_sourcing/
```

这一步会把这些东西一起带上去：
- `.openalex_key` 和 `.s2_key`：每周自动抓数据要用；
- `data/` 和 `output/`：已经抓好的数据，部署完马上就有内容，不用重新花额度；
- `team.db`：如果你在本机已经建过账号、写过跟进记录，会一起带上去。

**从这一步开始，以服务器上的 `team.db` 为准。** 不要再在 Mac 本地的软件里写跟进记录，否则两边的数据会对不上。

## 第 4 步：一键安装（在服务器上操作）

```bash
ssh ubuntu@服务器IP
sudo bash /opt/ai4s_sourcing/deploy/setup.sh talent.example.com
```

没有域名的话，去掉最后的域名：`sudo bash /opt/ai4s_sourcing/deploy/setup.sh`。

如果服务器在大陆、装 Python 包很慢，先运行这一行再安装：

```bash
export PIP_INDEX=https://mirrors.aliyun.com/pypi/simple/
```

脚本会自动完成以下工作：
- 安装 Python 和 Caddy（Caddy 是网页服务器，负责 HTTPS 证书的申请和自动续期）；
- 创建一个专门运行软件的系统账号 `ai4s`，不用 root 运行；
- 注册三个后台任务：
  - 软件本体：开机自启，崩溃后自动重启；
  - 每周一凌晨 3:30 自动更新 AI4S 赛道数据，每周四凌晨 3:30 更新通用AI赛道数据（错开两天，避免同一天额度不够）；
  - 每天凌晨 2:15 备份 `team.db`，保留最近 30 份。

这个脚本可以重复运行，比如换了域名、或者依赖有更新时。

## 第 5 步：创建管理员账号

如果 `team.db` 是从 Mac 带上来的，里面已经有账号了，直接跳过这一步。否则运行：

```bash
cd /opt/ai4s_sourcing
sudo -u ai4s .venv/bin/python manage_users.py add cpy --role admin
```

然后在浏览器打开 `https://talent.example.com`，用这个账号登录，到「👥 成员管理」页给组员开账号。

## 第 6 步：首次数据（如果第 3 步没带数据上去）

```bash
cd /opt/ai4s_sourcing
sudo -u ai4s .venv/bin/python run.py
```

如果当天的 API 额度用完，第二天再运行同一条命令会接着抓。之后每周的自动更新就不用管了。

---

## 日常运维

| 要做什么 | 命令（在服务器上） |
|---|---|
| 看软件是否在运行 | `systemctl status ai4s` |
| 看软件日志 | `journalctl -u ai4s -n 100` |
| 重启软件 | `sudo systemctl restart ai4s` |
| 立即更新一次数据 | AI4S：`sudo systemctl start ai4s-fetch`；通用AI：`sudo systemctl start ai4s-fetch-general` |
| 看数据更新进度和日志 | `journalctl -u ai4s-fetch -f` |
| 看下次自动任务的时间 | `systemctl list-timers 'ai4s*'` |
| 立即备份一次 | `sudo systemctl start ai4s-backup` |
| 管理账号 | `cd /opt/ai4s_sourcing && sudo -u ai4s .venv/bin/python manage_users.py list` |

数据更新完成后不需要重启软件，组员刷新页面就能看到新数据。

**把备份下载到 Mac（建议每月一次）：**

```bash
rsync -av --rsync-path="sudo rsync" ubuntu@服务器IP:/opt/ai4s_sourcing/backups/ ~/Documents/ai4s_backups/
```

## 更新软件代码

在 Mac 上改了代码以后，用下面的命令同步到服务器。这条命令**会排除服务器上的数据和账号**，不会覆盖它们：

```bash
rsync -av --rsync-path="sudo rsync" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.DS_Store' \
  --exclude 'team.db*' --exclude 'backups' --exclude 'data' --exclude 'output' \
  --exclude '.openalex_key' --exclude '.s2_key' --exclude '.streamlit' --exclude 'crm.csv*' \
  ~/Downloads/ai4s_sourcing/ ubuntu@服务器IP:/opt/ai4s_sourcing/

ssh ubuntu@服务器IP 'sudo chown -R ai4s:ai4s /opt/ai4s_sourcing && sudo systemctl restart ai4s'
```

如果 `requirements.txt` 有变化，或者 `deploy/` 里的文件有改动，再在服务器上重新运行一次 `setup.sh`。

## 安全清单

- [ ] 防火墙只开放 22、80、443
- [ ] SSH 用密钥登录，关闭密码登录
- [ ] 用域名 + HTTPS 访问（Caddy 自动续期证书，不用管）
- [ ] 组员每人一个账号，离组时在「成员管理」里停用
- [ ] 定期把 `backups/` 下载到本地
- [ ] `.openalex_key`、`.s2_key` 和 `team.db` 只存在服务器上，不要发到群里

## 常见问题

**网页打不开**
- 先检查控制台防火墙是否开放了 80 和 443。
- 再看两个服务是否在运行：`systemctl status ai4s caddy`。
- 查看 Caddy 日志：`journalctl -u caddy -n 50`。

**HTTPS 证书申请失败**
- 通常是域名还没解析到服务器，或者 80 端口没开放。
- 大陆服务器的域名还没备案时，也会被拦截。
- 解决后运行 `sudo systemctl restart caddy`，Caddy 会自动重试。

**页面一直在转圈、或者提示连接断开**
- 先运行 `sudo systemctl restart ai4s`，再看 `journalctl -u ai4s -n 100` 里有没有报错。

**数据没有更新**
- 运行 `journalctl -u ai4s-fetch -n 100` 看日志。
- 如果看到"额度已用完"，说明当天的 API 额度不够，程序会先用已有数据生成结果，下次运行时继续抓。
